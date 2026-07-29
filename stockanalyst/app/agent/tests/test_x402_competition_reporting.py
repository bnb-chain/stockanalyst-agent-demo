from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import ANY, AsyncMock, Mock, patch

from stockanalyst.app.agent import x402_handler as handler_module
from stockanalyst.app.agent.x402_handler import X402Handler, _payment_identity


ADDRESS = "0x1111111111111111111111111111111111111111"
NONCE = f"0x{'22' * 32}"


def _payment_header() -> str:
    proof = {
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


class X402PaymentIdentityTests(unittest.TestCase):
    def test_extracts_normalized_address_and_nonce_from_verified_proof(self) -> None:
        proof = {
            "payload": {
                "authorization": {
                    "from": "0x1111111111111111111111111111111111111111",
                    "nonce": "0xABCDEF",
                }
            }
        }
        encoded = base64.b64encode(json.dumps(proof).encode()).decode()

        self.assertEqual(
            _payment_identity(encoded),
            (
                "0x1111111111111111111111111111111111111111",
                f"0x{'0' * 58}abcdef",
            ),
        )


class X402CompetitionReportingTests(unittest.IsolatedAsyncioTestCase):
    def handler(self) -> X402Handler:
        return X402Handler(
            None,
            stream_work=Mock(),
            generator="test",
            free_stream_work=Mock(),
        )

    async def test_paid_settlement_reports_before_streaming(self) -> None:
        handler = self.handler()
        handler._stream_sse = AsyncMock()
        report = AsyncMock(return_value=True)
        with (
            patch.object(
                handler_module,
                "verify_payment_proof",
                return_value=(True, ""),
            ),
            patch.object(
                handler_module,
                "_settle_via_facilitator",
                new=AsyncMock(return_value=(True, "0xtx")),
            ),
            patch.object(
                handler_module,
                "report_competition_call",
                report,
                create=True,
            ),
        ):
            await handler._handle_analyze(
                {"headers": [(b"x-payment", _payment_header().encode())]},
                _receive_json({"symbols": ["AAPL"]}),
                AsyncMock(),
            )

        report.assert_awaited_once_with(
            event_id=f"b402:97:{ADDRESS}:{NONCE}",
            address=ADDRESS,
            called_at=ANY,
        )
        handler._stream_sse.assert_awaited_once()

    async def test_accounting_failure_does_not_deny_paid_request(self) -> None:
        handler = self.handler()
        handler._stream_sse = AsyncMock()
        with (
            patch.object(
                handler_module,
                "verify_payment_proof",
                return_value=(True, ""),
            ),
            patch.object(
                handler_module,
                "_settle_via_facilitator",
                new=AsyncMock(return_value=(True, "0xtx")),
            ),
            patch.object(
                handler_module,
                "report_competition_call",
                new=AsyncMock(side_effect=RuntimeError("reporting unavailable")),
                create=True,
            ),
        ):
            await handler._handle_analyze(
                {"headers": [(b"x-payment", _payment_header().encode())]},
                _receive_json({"symbols": ["AAPL"]}),
                AsyncMock(),
            )

        handler._stream_sse.assert_awaited_once()

    async def test_zero_value_free_payment_reports_before_streaming(self) -> None:
        handler = self.handler()
        handler._stream_free_sse = AsyncMock()
        report = AsyncMock(return_value=True)
        with (
            patch.object(
                handler_module,
                "verify_free_payment_proof",
                return_value=(True, "9 uses remaining today", ADDRESS),
            ),
            patch.object(
                handler_module,
                "report_competition_call",
                report,
                create=True,
            ),
        ):
            await handler._handle_free(
                {"headers": [(b"x-payment", _payment_header().encode())]},
                _receive_json({"symbol": "AAPL"}),
                AsyncMock(),
            )

        report.assert_awaited_once_with(
            event_id=f"b402-free:97:{ADDRESS}:{NONCE}",
            address=ADDRESS,
            called_at=ANY,
        )
        handler._stream_free_sse.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
