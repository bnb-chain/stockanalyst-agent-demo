from __future__ import annotations

import base64
import copy
import unittest
from dataclasses import dataclass
from typing import Any, Self
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
from stockanalyst.app.agent.x402_settlement import (
    SettlementOutcome,
    valid_settlement_reference,
)
from stockanalyst.app.agent.x402_tokens import (
    TOKENS,
    U_TOKEN,
    USDC_TOKEN,
    USDT_TOKEN,
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
    body: Any

    def json(self) -> Any:
        return self.body


class _RecordingHttpClient:
    def __init__(
        self,
        response: _FakeResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.requests: list[dict[str, Any]] = []
        self.response = response
        self.error = error

    async def __aenter__(self) -> Self:
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
        if self.error is not None:
            raise self.error
        return self.response or _FakeResponse(
            status_code=200,
            body={"code": "000000", "message": "success", "data": {}},
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
            {"network": "eip155:56", "x402Version": 2},
        )

        self.assertEqual(response["code"], "000000")
        self.assertEqual(len(http.requests), 1)
        request = http.requests[0]
        body = b'{"network":"eip155:56","x402Version":2}'
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
    "name": "United Stables",
    "version": "1",
    "assetTransferMethod": "eip3009",
    "signerAddress": "0x1111111111111111111111111111111111111111",
}
USD1_SUPPORTED_EXTRA = {
    "name": "World Liberty Financial USD",
    "version": "1",
    "assetTransferMethod": "eip3009",
    "signerAddress": "0x2222222222222222222222222222222222222222",
}
USDC_SUPPORTED_EXTRA = {
    "name": "USD Coin",
    "version": "1",
    "assetTransferMethod": "permit2-exact",
    "signerAddress": "0x3333333333333333333333333333333333333333",
    "spenderAddress": "0x4444444444444444444444444444444444444444",
}
USDT_SUPPORTED_EXTRA = {
    "name": "Tether USD",
    "version": "1",
    "assetTransferMethod": "permit2-exact",
    "signerAddress": "0x5555555555555555555555555555555555555555",
    "spenderAddress": "0x6666666666666666666666666666666666666666",
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
                    "network": "eip155:56",
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

    async def test_selects_exact_capabilities_in_token_order_and_copies(self) -> None:
        now = [100.0]
        client = self.client(now)
        client.post = AsyncMock(return_value=_supported_response(kinds=[
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "eip155:56",
                "extra": SUPPORTED_EXTRA,
            },
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "eip155:56",
                "extra": USDC_SUPPORTED_EXTRA,
            },
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "eip155:56",
                "extra": {**USDT_SUPPORTED_EXTRA, "spenderAddress": "malformed"},
            },
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "eip155:1",
                "extra": USDT_SUPPORTED_EXTRA,
            },
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "eip155:56",
                "extra": USDT_SUPPORTED_EXTRA,
            },
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "eip155:56",
                "extra": copy.deepcopy(USDT_SUPPORTED_EXTRA),
            },
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "eip155:56",
                "extra": {
                    **USDC_SUPPORTED_EXTRA,
                    "assetTransferMethod": "permit2-upto",
                },
            },
        ]))

        selected = await client.payment_extras("eip155:56", TOKENS)

        self.assertEqual(list(selected), ["U", "USDC"])
        self.assertEqual(selected["USDC"], USDC_SUPPORTED_EXTRA)
        selected["USDC"]["spenderAddress"] = "changed"
        self.assertEqual(
            (await client.payment_extras("eip155:56", TOKENS))["USDC"],
            USDC_SUPPORTED_EXTRA,
        )
        client.post.assert_awaited_once_with("/papi/v2/b402/supported", {})

    async def test_omits_permit2_capability_with_invalid_exact_key(self) -> None:
        now = [100.0]
        invalid_extras = (
            {**USDC_SUPPORTED_EXTRA, "signerAddress": "invalid"},
            {**USDC_SUPPORTED_EXTRA, "spenderAddress": "invalid"},
            {**USDC_SUPPORTED_EXTRA, "assetTransferMethod": "permit2-upto"},
            {**USDC_SUPPORTED_EXTRA, "name": "USDC"},
            {**USDC_SUPPORTED_EXTRA, "version": "2"},
        )

        for extra in invalid_extras:
            with self.subTest(extra=extra):
                client = self.client(now)
                client.post = AsyncMock(return_value=_supported_response(kinds=[{
                    "x402Version": 2,
                    "scheme": "exact",
                    "network": "eip155:56",
                    "extra": extra,
                }]))

                self.assertEqual(
                    await client.payment_extras("eip155:56", (USDC_TOKEN,)),
                    {},
                )

    async def test_omits_numeric_separator_and_non_hex_signer_addresses(self) -> None:
        now = [100.0]
        malformed_addresses = (
            "0x" + "1" * 38 + "_1",
            "0x" + "1" * 39 + "g",
        )

        for signer_address in malformed_addresses:
            with self.subTest(signer_address=signer_address):
                client = self.client(now)
                client.post = AsyncMock(return_value=_supported_response(kinds=[{
                    "x402Version": 2,
                    "scheme": "exact",
                    "network": "eip155:56",
                    "extra": {
                        **SUPPORTED_EXTRA,
                        "signerAddress": signer_address,
                    },
                }]))

                self.assertEqual(
                    await client.payment_extras("eip155:56", (U_TOKEN,)),
                    {},
                )

    async def test_omits_numeric_separator_and_non_hex_permit2_spenders(self) -> None:
        now = [100.0]
        malformed_addresses = (
            "0x" + "2" * 38 + "_2",
            "0x" + "2" * 39 + "g",
        )

        for spender_address in malformed_addresses:
            with self.subTest(spender_address=spender_address):
                client = self.client(now)
                client.post = AsyncMock(return_value=_supported_response(kinds=[{
                    "x402Version": 2,
                    "scheme": "exact",
                    "network": "eip155:56",
                    "extra": {
                        **USDC_SUPPORTED_EXTRA,
                        "spenderAddress": spender_address,
                    },
                }]))

                self.assertEqual(
                    await client.payment_extras("eip155:56", (USDC_TOKEN,)),
                    {},
                )

    async def test_accepts_mixed_case_hex_addresses(self) -> None:
        now = [100.0]
        extra = {
            **USDC_SUPPORTED_EXTRA,
            "signerAddress": "0x" + "aB" * 20,
            "spenderAddress": "0x" + "Cd" * 20,
        }
        client = self.client(now)
        client.post = AsyncMock(return_value=_supported_response(kinds=[{
            "x402Version": 2,
            "scheme": "exact",
            "network": "eip155:56",
            "extra": extra,
        }]))

        self.assertEqual(
            await client.payment_extras("eip155:56", (USDC_TOKEN,)),
            {"USDC": extra},
        )

    async def test_omits_duplicate_full_capability_key(self) -> None:
        now = [100.0]
        client = self.client(now)
        client.post = AsyncMock(return_value=_supported_response(kinds=[
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "eip155:56",
                "extra": USDT_SUPPORTED_EXTRA,
            },
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "eip155:56",
                "extra": copy.deepcopy(USDT_SUPPORTED_EXTRA),
            },
        ]))

        self.assertEqual(
            await client.payment_extras("eip155:56", (USDT_TOKEN,)),
            {},
        )

    async def test_refreshes_supported_kinds_after_one_hour(self) -> None:
        now = [100.0]
        client = self.client(now)
        client.post = AsyncMock(return_value=_supported_response())

        await client.payment_extras("eip155:56", TOKENS)
        now[0] += 3_601
        await client.payment_extras("eip155:56", TOKENS)

        self.assertEqual(client.post.await_count, 2)

    async def test_returns_empty_when_no_capability_matches(self) -> None:
        now = [100.0]
        client = self.client(now)
        client.post = AsyncMock(return_value=_supported_response(kinds=[]))

        self.assertEqual(await client.payment_extras("eip155:56", TOKENS), {})


PERMIT2_PAYMENT_REQUIREMENT = {
    "scheme": "exact",
    "network": "eip155:56",
    "amount": "210000000000000000",
    "asset": USDC_TOKEN.address,
    "payTo": "0xd10bddc20e4dc42a1a19a9653e994991e25b8153",
    "maxTimeoutSeconds": 600,
    "extra": USDC_SUPPORTED_EXTRA,
}
PERMIT2_PAYMENT_PAYLOAD = {
    "x402Version": 2,
    "resource": {
        "url": "https://api.example.test/x402/analyze/async",
        "description": "Stock analysis report",
        "mimeType": "application/json",
    },
    "accepted": PERMIT2_PAYMENT_REQUIREMENT,
    "payload": {
        "permit2Authorization": {
            "from": "0x7777777777777777777777777777777777777777",
            "to": PERMIT2_PAYMENT_REQUIREMENT["payTo"],
            "value": PERMIT2_PAYMENT_REQUIREMENT["amount"],
            "nonce": "123",
            "deadline": "1785485400",
        },
        "signature": "0x" + "12" * 65,
    },
}


class SettlementOutcomeTests(unittest.TestCase):
    def test_accepts_typed_settlement_outcomes(self) -> None:
        self.assertEqual(
            SettlementOutcome("settled", transaction="0xsettled"),
            SettlementOutcome("settled", transaction="0xsettled"),
        )
        self.assertEqual(
            SettlementOutcome("pending", transaction="0xpending").status,
            "pending",
        )
        self.assertEqual(
            SettlementOutcome("rejected", reason="insufficient_funds").reason,
            "insufficient_funds",
        )

    def test_rejects_inconsistent_or_invalid_outcomes(self) -> None:
        invalid = (
            ("settled", None),
            ("pending", ""),
            ("pending", "contains space"),
            ("rejected", "0xunexpected"),
        )
        for status, transaction in invalid:
            with self.subTest(status=status, transaction=transaction):
                with self.assertRaises(ValueError):
                    SettlementOutcome(status, transaction=transaction)  # type: ignore[arg-type]

    def test_validates_bounded_printable_settlement_references(self) -> None:
        self.assertTrue(valid_settlement_reference("!"))
        self.assertTrue(valid_settlement_reference("~" * 4096))
        self.assertFalse(valid_settlement_reference(""))
        self.assertFalse(valid_settlement_reference("a" * 4097))
        self.assertFalse(valid_settlement_reference("contains space"))
        self.assertFalse(valid_settlement_reference(True))


class B402SettlementTests(unittest.IsolatedAsyncioTestCase):
    def client(
        self,
        *,
        http: _RecordingHttpClient | None = None,
    ) -> B402Client:
        config = B402Config.from_env(_environment())
        assert config is not None
        client = B402Client(
            config,
            http_client_factory=(lambda: http) if http is not None else None,
        )
        client.payment_extras = AsyncMock(return_value={
            "USDC": USDC_SUPPORTED_EXTRA,
        })
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
                    "network": "eip155:56",
                    "amount": PERMIT2_PAYMENT_REQUIREMENT["amount"],
                },
            },
        ])

        outcome = await client.verify_and_settle(PERMIT2_PAYMENT_PAYLOAD)

        self.assertEqual(
            outcome,
            SettlementOutcome("settled", transaction="0x" + "12" * 32),
        )
        self.assertEqual(
            [call.args[0] for call in client.post.await_args_list],
            ["/papi/v2/b402/verify", "/papi/v2/b402/settle"],
        )
        verify_body = client.post.await_args_list[0].args[1]
        settle_body = client.post.await_args_list[1].args[1]
        self.assertEqual(verify_body, settle_body)
        self.assertEqual(verify_body["paymentPayload"], PERMIT2_PAYMENT_PAYLOAD)
        self.assertEqual(
            verify_body["paymentRequirements"],
            PERMIT2_PAYMENT_REQUIREMENT,
        )
        client.payment_extras.assert_awaited_once_with(
            "eip155:56",
            (USDC_TOKEN,),
        )

    async def test_settle_only_skips_verify(self) -> None:
        client = self.client()
        client.post = AsyncMock(return_value={
            "code": "000000",
            "message": "success",
            "data": {
                "success": True,
                "transaction": "0xsettled",
                "payer": "0x" + "22" * 20,
                "network": "eip155:56",
                "amount": PERMIT2_PAYMENT_REQUIREMENT["amount"],
            },
        })

        settled = await client.settle_only(PERMIT2_PAYMENT_PAYLOAD)

        self.assertEqual(
            settled,
            SettlementOutcome("settled", transaction="0xsettled"),
        )
        self.assertEqual(
            [call.args[0] for call in client.post.await_args_list],
            ["/papi/v2/b402/settle"],
        )

    async def test_explicit_verification_rejection_is_typed(self) -> None:
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

        outcome = await client.verify_and_settle(PERMIT2_PAYMENT_PAYLOAD)

        self.assertEqual(
            outcome,
            SettlementOutcome("rejected", reason="insufficient_funds"),
        )
        client.post.assert_awaited_once()

    async def test_substituted_exact_extra_is_rejected_before_verify(self) -> None:
        client = self.client()
        client.post = AsyncMock()
        substituted = copy.deepcopy(PERMIT2_PAYMENT_PAYLOAD)
        substituted["accepted"]["extra"]["signerAddress"] = "0x" + "22" * 20

        with self.assertRaisesRegex(
            B402RejectedError,
            "no longer supported",
        ):
            await client.verify_and_settle(substituted)

        client.post.assert_not_awaited()

    async def test_prebroadcast_settlement_failure_is_rejected(self) -> None:
        client = self.client()
        client.post = AsyncMock(return_value={
            "code": "000000",
            "message": "success",
            "data": {
                "success": False,
                "transaction": "",
                "payer": "0x" + "22" * 20,
                "network": "",
                "errorReason": "insufficient_funds",
            },
        })

        outcome = await client.settle_only(PERMIT2_PAYMENT_PAYLOAD)

        self.assertEqual(
            outcome,
            SettlementOutcome("rejected", reason="insufficient_funds"),
        )

    async def test_broadcast_settlement_is_pending(self) -> None:
        client = self.client()
        client.post = AsyncMock(return_value={
            "code": "000000",
            "message": "success",
            "data": {
                "success": False,
                "transaction": "0xpending",
                "payer": "0x" + "22" * 20,
                "network": "eip155:56",
                "errorReason": "invalid_transaction_state",
            },
        })

        outcome = await client.settle_only(PERMIT2_PAYMENT_PAYLOAD)

        self.assertEqual(
            outcome,
            SettlementOutcome("pending", transaction="0xpending"),
        )

    async def test_malformed_settlement_results_are_indeterminate(self) -> None:
        malformed_data = (
            {},
            {"success": True, "transaction": ""},
            {"success": True, "transaction": "contains space"},
            {"success": False, "transaction": None},
            {"success": "true", "transaction": "0xsettled"},
        )
        for data in malformed_data:
            with self.subTest(data=data):
                client = self.client()
                client.post = AsyncMock(return_value={
                    "code": "000000",
                    "message": "success",
                    "data": data,
                })

                with self.assertRaises(B402IndeterminateError):
                    await client.settle_only(PERMIT2_PAYMENT_PAYLOAD)

    async def test_malformed_verify_result_is_indeterminate(self) -> None:
        client = self.client()
        client.post = AsyncMock(return_value={
            "code": "000000",
            "message": "success",
            "data": {},
        })

        with self.assertRaises(B402IndeterminateError):
            await client.verify_and_settle(PERMIT2_PAYMENT_PAYLOAD)

    async def test_transport_auth_and_server_failures_are_indeterminate(self) -> None:
        failures = (
            _RecordingHttpClient(error=OSError("connection reset")),
            _RecordingHttpClient(_FakeResponse(401, {})),
            _RecordingHttpClient(_FakeResponse(403, {})),
            _RecordingHttpClient(_FakeResponse(500, {})),
        )
        for http in failures:
            with self.subTest(http=http):
                client = self.client(http=http)

                with self.assertRaises(B402IndeterminateError):
                    await client.settle_only(PERMIT2_PAYMENT_PAYLOAD)


if __name__ == "__main__":
    unittest.main()
