from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import Mock, patch

from eth_account import Account
from stockanalyst.app.agent import x402_verify as verify
from stockanalyst.app.agent.tests.test_x402_async_handler import (
    call_handler,
    decode_header,
)
from stockanalyst.app.agent.tests.test_x402_verify import (
    NOW as SIGNED_NOW,
)
from stockanalyst.app.agent.tests.test_x402_verify import (
    signed_free_proof,
)
from stockanalyst.app.agent.x402_handler import X402Handler

B402_PAY_TO = verify.B402_PAY_TO_ADDRESS
SIGNER_ADDRESS = Account.from_key("0x" + "11" * 32).address.lower()


async def free_report_work(symbol: str):
    yield "progress", {"stage": "collecting"}
    yield "report", {
        "symbol": symbol,
        "name": f"{symbol} Inc.",
        "price": 100.0,
        "price_now": 100.0,
        "twelve_month_target": 110.0,
        "room_to_target": 10.0,
    }
    yield "done", {}


async def failing_work(symbol: str):
    yield "error", {"message": "upstream unavailable"}


async def empty_work(symbol: str):
    yield "progress", {"stage": "collecting"}
    yield "done", {}


def build_handler(work=free_report_work, **kwargs) -> X402Handler:
    inner = Mock()
    return X402Handler(inner, free_work=work, job_service=None, **kwargs)


class FreeChallengeTests(unittest.TestCase):
    def test_challenge_requires_exactly_zero_and_names_the_free_tier(self) -> None:
        challenge = verify.build_free_payment_challenge("aapl")
        accept = challenge["accepts"][0]

        self.assertEqual(challenge["x402Version"], 2)
        self.assertEqual(accept["maxAmountRequired"], "0")
        self.assertEqual(accept["payTo"], B402_PAY_TO.lower())
        self.assertEqual(accept["asset"], verify.U_TOKEN_ADDRESS)
        self.assertEqual(accept["extra"]["tier"], "free")
        self.assertEqual(
            accept["extra"]["rateLimit"],
            f"{verify.FREE_TIER_LIMIT}/24h",
        )
        self.assertIn("AAPL", accept["extra"]["description"])


class FreeProofVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        verify._used_nonces.clear()
        verify._free_tier_calls.clear()
        patcher = patch.object(verify.time, "time", return_value=SIGNED_NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_accepts_a_zero_value_identity_proof(self) -> None:
        ok, message, from_address = verify.verify_free_payment_proof(
            signed_free_proof(B402_PAY_TO)
        )

        self.assertTrue(ok, message)
        self.assertEqual(from_address, SIGNER_ADDRESS)
        self.assertIn("uses remaining today", message)

    def test_rejects_any_non_zero_value(self) -> None:
        ok, message, _ = verify.verify_free_payment_proof(
            signed_free_proof(B402_PAY_TO, value="1")
        )

        self.assertFalse(ok)
        self.assertEqual(message, verify._FREE_VALUE_REJECTION)

    def test_rejects_a_recipient_other_than_the_seller(self) -> None:
        ok, message, _ = verify.verify_free_payment_proof(
            signed_free_proof("0x" + "44" * 20)
        )

        self.assertFalse(ok)
        self.assertIn("wrong recipient", message)

    def test_rejects_a_replayed_nonce(self) -> None:
        proof = signed_free_proof(B402_PAY_TO)
        first_ok, _, _ = verify.verify_free_payment_proof(proof)
        second_ok, message, _ = verify.verify_free_payment_proof(proof)

        self.assertTrue(first_ok)
        self.assertFalse(second_ok)
        self.assertIn("replay blocked", message)

    def test_rejects_the_call_after_the_daily_limit(self) -> None:
        seeds = [f"{index:02x}" for index in range(verify.FREE_TIER_LIMIT + 1)]
        outcomes = [
            verify.verify_free_payment_proof(
                signed_free_proof(B402_PAY_TO, nonce_seed=seed)
            )
            for seed in seeds
        ]

        self.assertTrue(all(ok for ok, _, _ in outcomes[:-1]))
        last_ok, last_message, _ = outcomes[-1]
        self.assertFalse(last_ok)
        self.assertIn("rate limit", last_message)


class FreeRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        verify._used_nonces.clear()
        verify._free_tier_calls.clear()
        patcher = patch.object(verify.time, "time", return_value=SIGNED_NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_get_returns_a_zero_price_challenge(self) -> None:
        response = await call_handler(
            build_handler(),
            method="GET",
            path="/x402/free",
            scope_overrides={"query_string": b"symbol=aapl"},
        )

        self.assertEqual(response.status, 402)
        challenge = decode_header(response, "payment-required")
        self.assertEqual(challenge["accepts"][0]["maxAmountRequired"], "0")
        self.assertIn("AAPL", challenge["accepts"][0]["extra"]["description"])

    async def test_post_without_a_proof_returns_the_challenge(self) -> None:
        response = await call_handler(
            build_handler(),
            method="POST",
            path="/x402/free",
            json_body={"symbol": "AAPL"},
        )

        self.assertEqual(response.status, 402)
        self.assertEqual(
            response.json["paymentRequired"]["accepts"][0]["maxAmountRequired"],
            "0",
        )

    async def test_post_without_a_symbol_is_rejected(self) -> None:
        response = await call_handler(
            build_handler(),
            method="POST",
            path="/x402/free",
            json_body={},
        )

        self.assertEqual(response.status, 400)
        self.assertEqual(response.json["error"], "symbol is required")

    async def test_post_with_a_valid_proof_returns_the_quote(self) -> None:
        response = await call_handler(
            build_handler(),
            method="POST",
            path="/x402/free",
            headers={"payment-signature": signed_free_proof(B402_PAY_TO)},
            json_body={"symbol": "AAPL"},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.json["symbol"], "AAPL")
        self.assertEqual(response.json["name"], "AAPL Inc.")
        self.assertEqual(response.json["price"], 100.0)
        self.assertEqual(response.json["price_now"], 100.0)
        self.assertEqual(response.json["twelve_month_target"], 110.0)
        self.assertEqual(response.json["room_to_target"], 10.0)
        self.assertNotIn("content", response.json)
        self.assertNotIn("format", response.json)

    async def test_post_accepts_the_first_entry_of_a_symbols_list(self) -> None:
        response = await call_handler(
            build_handler(),
            method="POST",
            path="/x402/free",
            headers={"payment-signature": signed_free_proof(B402_PAY_TO)},
            json_body={"symbols": ["nvda", "tsla"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.json["symbol"], "NVDA")

    async def test_post_with_a_rejected_proof_does_not_run_the_work(self) -> None:
        work = Mock(side_effect=AssertionError("free work must not run"))
        response = await call_handler(
            build_handler(work),
            method="POST",
            path="/x402/free",
            headers={"payment-signature": signed_free_proof(B402_PAY_TO, value="1")},
            json_body={"symbol": "AAPL"},
        )

        self.assertEqual(response.status, 402)
        self.assertEqual(response.json["detail"], verify._FREE_VALUE_REJECTION)
        work.assert_not_called()

    async def test_post_with_an_unparseable_header_is_rejected(self) -> None:
        response = await call_handler(
            build_handler(),
            method="POST",
            path="/x402/free",
            headers={"payment-signature": "not-base64-json"},
            json_body={"symbol": "AAPL"},
        )

        self.assertEqual(response.status, 402)

    async def test_failing_work_reports_a_retryable_outage(self) -> None:
        response = await call_handler(
            build_handler(failing_work),
            method="POST",
            path="/x402/free",
            headers={"payment-signature": signed_free_proof(B402_PAY_TO)},
            json_body={"symbol": "AAPL"},
        )

        self.assertEqual(response.status, 503)
        self.assertEqual(response.json["errorCode"], "free_quote_unavailable")
        self.assertTrue(response.json["retryable"])

    async def test_markdown_payload_is_rejected(self) -> None:
        async def markdown_work(symbol: str):
            yield "report", {
                "content": f"# {symbol} quick quote",
                "format": "markdown",
            }

        response = await call_handler(
            build_handler(markdown_work),
            method="POST",
            path="/x402/free",
            headers={"payment-signature": signed_free_proof(B402_PAY_TO)},
            json_body={"symbol": "AAPL"},
        )

        self.assertEqual(response.status, 503)
        self.assertEqual(response.json["errorCode"], "free_quote_unavailable")

    async def test_work_without_a_report_reports_a_retryable_outage(self) -> None:
        response = await call_handler(
            build_handler(empty_work),
            method="POST",
            path="/x402/free",
            headers={"payment-signature": signed_free_proof(B402_PAY_TO)},
            json_body={"symbol": "AAPL"},
        )

        self.assertEqual(response.status, 503)
        self.assertEqual(response.json["errorCode"], "free_quote_unavailable")

    async def test_route_is_disabled_when_no_work_is_configured(self) -> None:
        response = await call_handler(
            build_handler(None),
            method="POST",
            path="/x402/free",
            headers={"payment-signature": signed_free_proof(B402_PAY_TO)},
            json_body={"symbol": "AAPL"},
        )

        self.assertEqual(response.status, 501)
        self.assertEqual(response.json["error"], "free tier not configured")

    async def test_unknown_x402_route_advertises_the_free_endpoint(self) -> None:
        response = await call_handler(
            build_handler(),
            method="GET",
            path="/x402/unknown",
        )

        self.assertEqual(response.status, 404)
        routes = " ".join(response.json["x402_routes"])
        self.assertIn("GET  /x402/free?symbol=AAPL", routes)
        self.assertIn("POST /x402/free", routes)


class FreeUsageReportingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        verify._used_nonces.clear()
        verify._free_tier_calls.clear()
        patcher = patch.object(verify.time, "time", return_value=SIGNED_NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_an_accepted_call_reports_the_verified_wallet(self) -> None:
        attempt = Mock()
        reporter = Mock()
        reporter.start_attempt.return_value = attempt

        response = await call_handler(
            build_handler(usage_reporter=reporter),
            method="POST",
            path="/x402/free",
            headers={"payment-signature": signed_free_proof(B402_PAY_TO)},
            json_body={"symbol": "AAPL"},
        )

        self.assertEqual(response.status, 200)
        reporter.submit_attempt.assert_called_once_with(attempt)
        attempt.observe_verified_wallet.assert_called_once()

    async def test_a_rejected_call_reports_nothing(self) -> None:
        reporter = Mock()

        response = await call_handler(
            build_handler(usage_reporter=reporter),
            method="POST",
            path="/x402/free",
            headers={"payment-signature": signed_free_proof(B402_PAY_TO, value="1")},
            json_body={"symbol": "AAPL"},
        )

        self.assertEqual(response.status, 402)
        reporter.start_attempt.assert_not_called()

    async def test_a_reporting_failure_never_breaks_the_quote(self) -> None:
        reporter = Mock()
        reporter.start_attempt.side_effect = RuntimeError("reporter down")

        response = await call_handler(
            build_handler(usage_reporter=reporter),
            method="POST",
            path="/x402/free",
            headers={"payment-signature": signed_free_proof(B402_PAY_TO)},
            json_body={"symbol": "AAPL"},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.json["symbol"], "AAPL")


class FreeProofStabilityTests(unittest.TestCase):
    """Malformed proofs must be rejected, never raise."""

    def setUp(self) -> None:
        verify._used_nonces.clear()
        verify._free_tier_calls.clear()

    def test_malformed_payloads_are_rejected_without_raising(self) -> None:
        base = json.loads(base64.b64decode(signed_free_proof(B402_PAY_TO)))
        cases: list[tuple[str, object]] = [
            ("payload is a list", []),
            ("payload is a string", "payload"),
            ("payload is null", None),
        ]
        for name, payload in cases:
            with self.subTest(name=name):
                encoded = base64.b64encode(
                    json.dumps({**base, "payload": payload}).encode()
                ).decode()
                ok, _, address = verify.verify_free_payment_proof(encoded)
                self.assertFalse(ok)
                self.assertEqual(address, "")

    def test_a_thousand_digit_value_is_rejected_without_raising(self) -> None:
        base = json.loads(base64.b64decode(signed_free_proof(B402_PAY_TO)))
        base["payload"]["authorization"]["value"] = "9" * 1000
        encoded = base64.b64encode(json.dumps(base).encode()).decode()

        ok, message, _ = verify.verify_free_payment_proof(encoded)

        self.assertFalse(ok)
        self.assertEqual(message, verify._FREE_VALUE_REJECTION)

    def test_a_non_object_root_is_rejected(self) -> None:
        encoded = base64.b64encode(json.dumps([1, 2, 3]).encode()).decode()

        ok, message, address = verify.verify_free_payment_proof(encoded)

        self.assertFalse(ok)
        self.assertEqual(message, verify._PAYMENT_SIGNATURE_REJECTION)
        self.assertEqual(address, "")


if __name__ == "__main__":
    unittest.main()
