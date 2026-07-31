from __future__ import annotations

import base64
import unittest
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

from stockanalyst.app.agent.b402_client import (
    B402Client,
    B402Config,
    B402ConfigurationError,
    B402RejectedError,
)


_PRIVATE_KEY = RSA.generate(1024)
PRIVATE_KEY_B64 = base64.b64encode(_PRIVATE_KEY.export_key(format="DER", pkcs=8)).decode()


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "B402_CLIENT_ID": "client",
        "B402_ACCESS_TOKEN": "token",
        "B402_BASE_URL": "https://sandbox.example.test/",
        "B402_PRIVATE_KEY": PRIVATE_KEY_B64,
    }
    values.update(overrides)
    return values


class B402ConfigTests(unittest.TestCase):
    def test_empty_environment_disables_b402(self) -> None:
        self.assertIsNone(B402Config.from_env({}))

    def test_complete_environment_enables_b402(self) -> None:
        config = B402Config.from_env(_environment())

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.base_url, "https://sandbox.example.test")

    def test_partial_environment_is_rejected(self) -> None:
        with self.assertRaisesRegex(B402ConfigurationError, "incomplete"):
            B402Config.from_env({"B402_CLIENT_ID": "client"})

    def test_non_https_base_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(B402ConfigurationError, "HTTPS"):
            B402Config.from_env(
                _environment(B402_BASE_URL="http://sandbox.example.test")
            )

    def test_invalid_private_key_is_rejected_without_echoing_it(self) -> None:
        invalid = "not-a-private-key"
        with self.assertRaises(B402ConfigurationError) as raised:
            B402Config.from_env(_environment(B402_PRIVATE_KEY=invalid))

        self.assertNotIn(invalid, str(raised.exception))


@dataclass
class _FakeResponse:
    status_code: int
    body: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self.body


class _RecordingHttpClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def __aenter__(self) -> _RecordingHttpClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        content: bytes,
        headers: dict[str, str],
    ) -> _FakeResponse:
        self.requests.append({
            "url": url,
            "content": content,
            "headers": headers,
        })
        return _FakeResponse(
            200,
            {"code": "000000", "message": "success", "data": {}},
        )


class B402TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_signs_the_exact_transmitted_json_body(self) -> None:
        config = B402Config.from_env(_environment())
        assert config is not None
        http = _RecordingHttpClient()
        client = B402Client(
            config,
            http_client_factory=lambda: http,
            now_ms=lambda: 1_785_484_800_123,
        )

        response = await client.post(
            "/papi/v2/b402/verify",
            {"network": "eip155:97", "x402Version": 2},
        )

        self.assertEqual(response["code"], "000000")
        self.assertEqual(len(http.requests), 1)
        request = http.requests[0]
        body = b'{"network":"eip155:97","x402Version":2}'
        self.assertEqual(request["content"], body)
        self.assertEqual(
            request["url"],
            "https://sandbox.example.test/papi/v2/b402/verify",
        )
        headers = request["headers"]
        self.assertEqual(headers["X-Tesla-ClientId"], "client")
        self.assertEqual(headers["X-Tesla-SignAccessToken"], "token")
        self.assertEqual(headers["X-Tesla-Timestamp"], "1785484800123")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("BinancePay-Signature", headers)
        pkcs1_15.new(_PRIVATE_KEY.public_key()).verify(
            SHA256.new(body + b"1785484800123"),
            base64.b64decode(headers["X-Tesla-Signature"]),
        )


SUPPORTED_EXTRA = {
    "name": "U",
    "version": "1",
    "assetTransferMethod": "eip3009",
    "signerAddress": "0x1111111111111111111111111111111111111111",
}


def _supported_response(
    *,
    kinds: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "code": "000000",
        "message": "success",
        "data": {
            "kinds": kinds if kinds is not None else [
                {
                    "x402Version": 2,
                    "scheme": "exact",
                    "network": "eip155:97",
                    "extra": SUPPORTED_EXTRA,
                }
            ],
            "extensions": [],
            "signers": {
                "eip155:*": [
                    "0x1111111111111111111111111111111111111111"
                ]
            },
        },
    }


class B402SupportedKindTests(unittest.IsolatedAsyncioTestCase):
    def client(self, now: list[float]) -> B402Client:
        config = B402Config.from_env(_environment())
        assert config is not None
        return B402Client(config, monotonic=lambda: now[0])

    async def test_selects_and_defensively_copies_eip3009_kind(self) -> None:
        now = [100.0]
        client = self.client(now)
        client.post = AsyncMock(return_value=_supported_response())

        first = await client.payment_extra("eip155:97", "U", "1")
        first["signerAddress"] = "changed"
        second = await client.payment_extra("eip155:97", "U", "1")

        self.assertEqual(second, SUPPORTED_EXTRA)
        client.post.assert_awaited_once_with(
            "/papi/v2/b402/supported",
            {},
        )

    async def test_refreshes_supported_kinds_after_one_hour(self) -> None:
        now = [100.0]
        client = self.client(now)
        client.post = AsyncMock(return_value=_supported_response())

        await client.payment_extra("eip155:97", "U", "1")
        now[0] += 3_601
        await client.payment_extra("eip155:97", "U", "1")

        self.assertEqual(client.post.await_count, 2)

    async def test_rejects_missing_matching_kind(self) -> None:
        now = [100.0]
        client = self.client(now)
        client.post = AsyncMock(return_value=_supported_response(kinds=[]))

        with self.assertRaisesRegex(B402RejectedError, "does not support"):
            await client.payment_extra("eip155:97", "U", "1")

    async def test_requires_a_valid_signer_address(self) -> None:
        now = [100.0]
        client = self.client(now)
        extra = {
            **SUPPORTED_EXTRA,
            "signerAddress": "invalid",
        }
        client.post = AsyncMock(return_value=_supported_response(kinds=[{
            "x402Version": 2,
            "scheme": "exact",
            "network": "eip155:97",
            "extra": extra,
        }]))

        with self.assertRaisesRegex(B402RejectedError, "does not support"):
            await client.payment_extra("eip155:97", "U", "1")


if __name__ == "__main__":
    unittest.main()
