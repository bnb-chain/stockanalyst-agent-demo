from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import AsyncMock, patch

from stockanalyst.app.agent import x402_handler as handler_module
from stockanalyst.app.agent.x402_handler import X402Handler
from stockanalyst.app.agent.x402_job_service import (
    CreateJobResult,
    SettlementIndeterminate,
)
from stockanalyst.app.agent.x402_settlement import SettlementOutcome

ADDRESS = "0x1111111111111111111111111111111111111111"
NONCE = f"0x{'22' * 32}"


def _payment_header() -> str:
    proof = {
        "x402Version": 2,
        "accepted": {
            "scheme": "exact",
            "network": "eip155:56",
            "amount": "210000000000000000",
            "asset": "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565",
            "payTo": "0xd10bddc20e4dc42a1a19a9653e994991e25b8153",
            "maxTimeoutSeconds": 600,
            "extra": {
                "name": "United Stables",
                "version": "1",
                "assetTransferMethod": "eip3009",
                "signerAddress": "0x3333333333333333333333333333333333333333",
            },
        },
        "payload": {
            "authorization": {
                "from": ADDRESS,
                "nonce": NONCE,
            }
        }
    }
    return base64.b64encode(json.dumps(proof).encode()).decode()


def _receive_json(payload: dict):
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {
            "type": "http.request",
            "body": json.dumps(payload).encode(),
            "more_body": False,
        }

    return receive


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: dict | None = None,
        *,
        malformed: bool = False,
    ) -> None:
        self.status_code = status_code
        self._body = body or {}
        self._malformed = malformed

    def json(self) -> dict:
        if self._malformed:
            raise ValueError("not JSON")
        return self._body


class _FakeHttpClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, *_args, **_kwargs) -> _FakeResponse:
        return self._response


class X402CompetitionReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_b402_unknown_outcome_is_indeterminate_not_rejection(
        self,
    ) -> None:
        client = AsyncMock()
        client.verify_and_settle.side_effect = (
            handler_module.B402IndeterminateError("response lost")
        )
        with (
            patch.object(handler_module, "_B402_CLIENT", client),
            self.assertRaises(SettlementIndeterminate),
        ):
            await handler_module._settle_via_facilitator(
                _payment_header(),
                "verify-and-settle",
            )

    async def test_facilitator_malformed_response_is_indeterminate(self) -> None:
        response = _FakeResponse(200, malformed=True)
        with (
            patch.object(
                handler_module.httpx,
                "AsyncClient",
                return_value=_FakeHttpClient(response),
            ),
            self.assertRaises(SettlementIndeterminate),
        ):
            await handler_module._settle_generic({})

    async def test_generic_settlement_rejects_ambiguous_response_shapes(
        self,
    ) -> None:
        ambiguous = (
            {"success": "true", "transaction": "0xtx"},
            {"success": 1, "transaction": "0xtx"},
            {"transaction": "0xtx"},
            {"success": True},
            {"success": True, "transaction": ""},
            {"success": True, "transaction": None},
            {"success": False, "transaction": "0xtx"},
            {"success": False, "transaction": None},
            {"success": False, "transaction": []},
        )

        for body in ambiguous:
            with (
                self.subTest(body=body),
                patch.object(
                    handler_module.httpx,
                    "AsyncClient",
                    return_value=_FakeHttpClient(_FakeResponse(200, body)),
                ),
                self.assertRaises(SettlementIndeterminate),
            ):
                await handler_module._settle_generic({})

    async def test_generic_settlement_accepts_only_explicit_valid_outcomes(
        self,
    ) -> None:
        cases = (
            (
                {"success": True, "transaction": "0xtx"},
                (True, "0xtx"),
            ),
            (
                {"success": False, "errorReason": "rejected"},
                (False, "rejected"),
            ),
            (
                {
                    "success": False,
                    "transaction": "",
                    "errorReason": "rejected",
                },
                (False, "rejected"),
            ),
        )

        for body, expected in cases:
            with (
                self.subTest(body=body),
                patch.object(
                    handler_module.httpx,
                    "AsyncClient",
                    return_value=_FakeHttpClient(_FakeResponse(200, body)),
                ),
            ):
                self.assertEqual(
                    await handler_module._settle_generic({}),
                    expected,
                )

    async def test_structured_b402_rejection_remains_explicit(self) -> None:
        client = AsyncMock()
        client.verify_and_settle.side_effect = handler_module.B402RejectedError(
            "insufficient_funds"
        )
        with patch.object(handler_module, "_B402_CLIENT", client):
            outcome = await handler_module._settle_via_facilitator(
                _payment_header(),
                "verify-and-settle",
            )

        self.assertEqual(outcome.status, "rejected")
        self.assertIsNone(outcome.transaction)
        self.assertEqual(outcome.reason, "insufficient_funds")

    async def test_confirmed_b402_settlement_returns_transaction(self) -> None:
        client = AsyncMock()
        transaction = "0x" + "12" * 32
        client.verify_and_settle.return_value = SettlementOutcome(
            "settled",
            transaction=transaction,
        )
        with patch.object(handler_module, "_B402_CLIENT", client):
            outcome = await handler_module._settle_via_facilitator(
                _payment_header(),
                "verify-and-settle",
            )

        self.assertEqual(outcome.status, "settled")
        self.assertEqual(outcome.transaction, transaction)
        self.assertIsNone(outcome.reason)

    async def test_b402_outcome_logs_do_not_include_external_values(self) -> None:
        transaction = "0x" + "ab" * 32
        rejection = (
            "credential=private-access-token\n"
            "transaction=0x" + "cd" * 32
        )
        client = AsyncMock()
        client.verify_and_settle.side_effect = [
            SettlementOutcome("settled", transaction=transaction),
            handler_module.B402RejectedError(rejection),
        ]

        with (
            patch.object(handler_module, "_B402_CLIENT", client),
            self.assertLogs("seller-agent.x402", level="INFO") as captured,
        ):
            settled = (
                await handler_module._settle_via_facilitator(
                    _payment_header(),
                    "verify-and-settle",
                )
            )
            rejected = (
                await handler_module._settle_via_facilitator(
                    _payment_header(),
                    "verify-and-settle",
                )
            )

        self.assertEqual(settled.status, "settled")
        self.assertEqual(settled.transaction, transaction)
        self.assertEqual(rejected.status, "rejected")
        self.assertIsNone(rejected.transaction)
        self.assertEqual(rejected.reason, rejection)
        rendered = "\n".join(captured.output)
        self.assertIn("outcome=settled backend=b402", rendered)
        self.assertIn("outcome=rejected backend=b402", rendered)
        for secret in (
            transaction,
            rejection,
            "private-access-token",
            "0x" + "cd" * 32,
        ):
            self.assertNotIn(secret, rendered)

    async def test_generic_outcome_logs_do_not_include_external_values(
        self,
    ) -> None:
        transaction = "0x" + "ef" * 32
        rejection = "secret-proof\ntransaction=" + "0x" + "12" * 32
        responses = [
            _FakeResponse(200, {"success": True, "transaction": transaction}),
            _FakeResponse(200, {"success": False, "errorReason": rejection}),
        ]

        with self.assertLogs(
            "seller-agent.x402",
            level="INFO",
        ) as captured:
            results = []
            for response in responses:
                with patch.object(
                    handler_module.httpx,
                    "AsyncClient",
                    return_value=_FakeHttpClient(response),
                ):
                    results.append(await handler_module._settle_generic({}))

        self.assertEqual(results, [(True, transaction), (False, rejection)])
        rendered = "\n".join(captured.output)
        self.assertIn("outcome=settled backend=generic", rendered)
        self.assertIn("outcome=rejected backend=generic", rendered)
        for secret in (
            transaction,
            rejection,
            "secret-proof",
            "0x" + "12" * 32,
        ):
            self.assertNotIn(secret, rendered)

    async def test_async_create_delegates_accounting_to_job_service(self) -> None:
        job_service = AsyncMock()
        b402_client = AsyncMock()
        job_service.create_job.return_value = CreateJobResult(
            job_id="x402_" + "a" * 32,
            job_token="token",
            status="queued",
            expires_at=1_785_945_600_123,
        )
        handler = X402Handler(
            None,
            job_service=job_service,
            b402_client=b402_client,
        )
        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        await handler(
            {
                "type": "http",
                "method": "POST",
                "path": "/x402/analyze/async",
                "query_string": b"",
                "headers": [(b"payment-signature", b"proof")],
            },
            _receive_json({"symbols": ["AAPL"]}),
            send,
        )

        self.assertEqual(sent[0]["status"], 202)
        job_service.create_job.assert_awaited_once_with(
            "proof",
            {"symbols": ["AAPL"]},
        )
        b402_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
