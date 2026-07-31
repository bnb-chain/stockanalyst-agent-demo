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
    B402IndeterminateError,
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


PAYMENT_REQUIREMENT = {
    "scheme": "exact",
    "network": "eip155:97",
    "amount": "1000000000000000000",
    "asset": "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565",
    "payTo": "0xd10bddc20e4dc42a1a19a9653e994991e25b8153",
    "maxTimeoutSeconds": 600,
    "extra": SUPPORTED_EXTRA,
}
PAYMENT_PAYLOAD = {
    "x402Version": 2,
    "resource": {
        "url": "https://api.example.test/x402/analyze/async",
        "description": "Stock analysis report",
        "mimeType": "application/json",
    },
    "accepted": PAYMENT_REQUIREMENT,
    "payload": {
        "signature": "0x" + "12" * 65,
        "authorization": {
            "from": "0x2222222222222222222222222222222222222222",
            "to": PAYMENT_REQUIREMENT["payTo"],
            "value": PAYMENT_REQUIREMENT["amount"],
            "validAfter": "0",
            "validBefore": "1785485400",
            "nonce": "0x" + "34" * 32,
        },
    },
}


class B402SettlementTests(unittest.IsolatedAsyncioTestCase):
    def client(self) -> B402Client:
        config = B402Config.from_env(_environment())
        assert config is not None
        client = B402Client(config)
        client.payment_extra = AsyncMock(return_value=SUPPORTED_EXTRA)
        return client

    async def test_verifies_before_settling_the_identical_envelope(self) -> None:
        client = self.client()
        client.post = AsyncMock(side_effect=[
            {
                "code": "000000",
                "message": "success",
                "data": {
                    "isValid": True,
                    "payer": "0x2222222222222222222222222222222222222222",
                },
            },
            {
                "code": "000000",
                "message": "success",
                "data": {
                    "success": True,
                    "transaction": "0x" + "12" * 32,
                    "payer": "0x2222222222222222222222222222222222222222",
                    "network": "eip155:97",
                    "amount": PAYMENT_REQUIREMENT["amount"],
                },
            },
        ])

        transaction = await client.verify_and_settle(PAYMENT_PAYLOAD)

        self.assertEqual(transaction, "0x" + "12" * 32)
        self.assertEqual(
            [call.args[0] for call in client.post.await_args_list],
            ["/papi/v2/b402/verify", "/papi/v2/b402/settle"],
        )
        verify_body = client.post.await_args_list[0].args[1]
        settle_body = client.post.await_args_list[1].args[1]
        self.assertEqual(verify_body, settle_body)
        self.assertEqual(verify_body["paymentPayload"], PAYMENT_PAYLOAD)
        self.assertEqual(
            verify_body["paymentRequirements"],
            PAYMENT_REQUIREMENT,
        )

    async def test_explicit_verification_rejection_stops_settlement(self) -> None:
        client = self.client()
        client.post = AsyncMock(return_value={
            "code": "000000",
            "message": "success",
            "data": {
                "isValid": False,
                "invalidReason": "insufficient_funds",
                "payer": "0x0000000000000000000000000000000000000000",
            },
        })

        with self.assertRaisesRegex(B402RejectedError, "insufficient_funds"):
            await client.verify_and_settle(PAYMENT_PAYLOAD)

        client.post.assert_awaited_once()

    async def test_prebroadcast_settlement_failure_is_explicit(self) -> None:
        client = self.client()
        client.post = AsyncMock(side_effect=[
            {
                "code": "000000",
                "message": "success",
                "data": {"isValid": True, "payer": "0x" + "22" * 20},
            },
            {
                "code": "000000",
                "message": "success",
                "data": {
                    "success": False,
                    "transaction": "",
                    "payer": "0x" + "22" * 20,
                    "network": "",
                    "errorReason": "insufficient_funds",
                },
            },
        ])

        with self.assertRaisesRegex(B402RejectedError, "insufficient_funds"):
            await client.verify_and_settle(PAYMENT_PAYLOAD)

    async def test_broadcast_pending_settlement_is_indeterminate(self) -> None:
        client = self.client()
        client.post = AsyncMock(side_effect=[
            {
                "code": "000000",
                "message": "success",
                "data": {"isValid": True, "payer": "0x" + "22" * 20},
            },
            {
                "code": "000000",
                "message": "success",
                "data": {
                    "success": False,
                    "transaction": "0x" + "56" * 32,
                    "payer": "0x" + "22" * 20,
                    "network": "eip155:97",
                    "errorReason": "invalid_transaction_state",
                },
            },
        ])

        with self.assertRaises(B402IndeterminateError):
            await client.verify_and_settle(PAYMENT_PAYLOAD)

    async def test_malformed_verify_envelope_is_indeterminate(self) -> None:
        client = self.client()
        client.post = AsyncMock(return_value={
            "code": "000000",
            "message": "success",
            "data": {},
        })

        with self.assertRaises(B402IndeterminateError):
            await client.verify_and_settle(PAYMENT_PAYLOAD)


if __name__ == "__main__":
    unittest.main()
