from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unittest

import httpx
from stockanalyst.app.agent.usage_reporting import (
    UsageEventReporter,
    UsageReportingError,
    load_usage_reporting_config,
)


class UsageReportingConfigTests(unittest.TestCase):
    def test_both_missing_values_disable_reporting(self) -> None:
        self.assertIsNone(load_usage_reporting_config({}))

    def test_configuration_requires_base_url_and_write_token_together(self) -> None:
        for env in (
            {"API_BASE_URL": "https://bnbagent-api.bnbchain.world"},
            {"COMPETITION_INTERNAL_TOKEN": "secret"},
        ):
            with self.subTest(env=env), self.assertRaisesRegex(
                UsageReportingError,
                "must be set together",
            ):
                load_usage_reporting_config(env)

    def test_configuration_derives_the_fixed_write_endpoint(self) -> None:
        config = load_usage_reporting_config(
            {
                "API_BASE_URL": "https://bnbagent-api.bnbchain.world/",
                "COMPETITION_INTERNAL_TOKEN": "secret",
            }
        )

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(
            config.endpoint_url,
            "https://bnbagent-api.bnbchain.world/internal/x402/usage-events",
        )
        self.assertEqual(config.internal_token, "secret")

    def test_configuration_rejects_unsafe_or_ambiguous_base_urls(self) -> None:
        for base_url in (
            "ftp://backend.example",
            "https://user:pass@backend.example",
            "https://backend.example?query=1",
            "https://backend.example#fragment",
            "https:///missing-host",
        ):
            with self.subTest(base_url=base_url), self.assertRaisesRegex(
                UsageReportingError,
                "API_BASE_URL must use HTTPS",
            ):
                load_usage_reporting_config(
                    {
                        "API_BASE_URL": base_url,
                        "COMPETITION_INTERNAL_TOKEN": "secret",
                    }
                )

    def test_configuration_rejects_plaintext_http_for_every_host(self) -> None:
        for base_url in (
            "http://backend.example",
            "http://localhost",
            "http://127.0.0.1",
            "http://[::1]",
        ):
            with self.subTest(base_url=base_url), self.assertRaisesRegex(
                UsageReportingError,
                "API_BASE_URL must use HTTPS",
            ):
                load_usage_reporting_config(
                    {
                        "API_BASE_URL": base_url,
                        "COMPETITION_INTERNAL_TOKEN": "secret",
                    }
                )


class UsageAttemptTests(unittest.TestCase):
    def config(self):
        config = load_usage_reporting_config(
            {
                "API_BASE_URL": "https://bnbagent-api.bnbchain.world",
                "COMPETITION_INTERNAL_TOKEN": "secret",
            }
        )
        assert config is not None
        return config

    def test_attempt_has_random_128_bit_id_timestamp_and_no_wallet(self) -> None:
        reporter = UsageEventReporter(
            self.config(),
            clock=lambda: 1_786_960_440_123,
        )

        attempt = reporter.start_attempt()

        self.assertRegex(attempt.event_id, re.compile(r"usage-attempt:[0-9a-f]{32}\Z"))
        self.assertEqual(attempt.timestamp, 1_786_960_440_123)
        self.assertIsNone(attempt.wallet)

    def test_attempt_accepts_only_a_verified_wallet_and_normalizes_it(self) -> None:
        reporter = UsageEventReporter(self.config())
        attempt = reporter.start_attempt()

        attempt.observe_verified_wallet(
            "0x1111111111111111111111111111111111111111"
        )

        self.assertEqual(
            attempt.wallet,
            "0x1111111111111111111111111111111111111111",
        )

    def test_attempt_rejects_an_invalid_wallet(self) -> None:
        reporter = UsageEventReporter(self.config())
        attempt = reporter.start_attempt()

        with self.assertRaises(ValueError):
            attempt.observe_verified_wallet("not-a-wallet")


class UsageEventReporterDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def config(self):
        config = load_usage_reporting_config(
            {
                "API_BASE_URL": "https://bnbagent-api.bnbchain.world",
                "COMPETITION_INTERNAL_TOKEN": "super-secret-token",
            }
        )
        assert config is not None
        return config

    async def test_posts_an_exact_authenticated_attempt_payload(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(201, json={"accepted": True, "duplicate": False})

        reporter = UsageEventReporter(
            self.config(),
            clock=lambda: 1_786_960_440_123,
            transport=httpx.MockTransport(handle),
            retry_delay_seconds=0,
        )
        attempt = reporter.start_attempt()
        attempt.observe_verified_wallet(
            "0x1111111111111111111111111111111111111111"
        )

        self.assertIs(reporter.submit_attempt(attempt), True)
        await reporter.wait_for_idle()

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(
            str(request.url),
            "https://bnbagent-api.bnbchain.world/internal/x402/usage-events",
        )
        self.assertEqual(request.headers["X-Internal-Token"], "super-secret-token")
        self.assertEqual(
            json.loads(request.content),
            {
                "version": 1,
                "eventId": attempt.event_id,
                "eventType": "attempt",
                "timestamp": 1_786_960_440_123,
                "wallet": "0x1111111111111111111111111111111111111111",
            },
        )

    async def test_succeeded_event_hashes_job_id_without_exposing_it(self) -> None:
        bodies: list[dict] = []

        def handle(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"accepted": True, "duplicate": True})

        reporter = UsageEventReporter(
            self.config(),
            transport=httpx.MockTransport(handle),
            retry_delay_seconds=0,
        )
        job_id = "x402_0123456789abcdef0123456789abcdef"

        self.assertIs(
            reporter.submit_succeeded(
                job_id=job_id,
                wallet="0x2222222222222222222222222222222222222222",
                timestamp=1_786_960_440_000,
            ),
            True,
        )
        await reporter.wait_for_idle()

        expected = hashlib.sha256(job_id.encode("ascii")).hexdigest()
        self.assertEqual(
            bodies,
            [
                {
                    "version": 1,
                    "eventId": f"usage-succeeded:{expected}",
                    "eventType": "succeeded",
                    "timestamp": 1_786_960_440_000,
                    "wallet": "0x2222222222222222222222222222222222222222",
                }
            ],
        )
        self.assertNotIn(job_id, json.dumps(bodies))

    async def test_retries_only_retryable_failures_with_identical_payload(self) -> None:
        for first_status in (429, 500, 503):
            bodies: list[bytes] = []

            def handle(
                request: httpx.Request,
                bodies=bodies,
                first_status=first_status,
            ) -> httpx.Response:
                bodies.append(request.content)
                return httpx.Response(first_status if len(bodies) == 1 else 201)

            reporter = UsageEventReporter(
                self.config(),
                transport=httpx.MockTransport(handle),
                retry_delay_seconds=0,
            )
            with self.subTest(first_status=first_status), self.assertLogs(
                "seller-agent.x402.usage", level="WARNING"
            ):
                reporter.submit_attempt(reporter.start_attempt())
                await reporter.wait_for_idle()

            self.assertEqual(len(bodies), 2)
            self.assertEqual(bodies[0], bodies[1])

    async def test_retries_network_error_without_logging_sensitive_values(self) -> None:
        requests = 0

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            if requests == 1:
                raise httpx.ConnectError(
                    "private-network-error-detail",
                    request=request,
                )
            return httpx.Response(201)

        reporter = UsageEventReporter(
            self.config(),
            transport=httpx.MockTransport(handle),
            retry_delay_seconds=0,
        )
        attempt = reporter.start_attempt()
        attempt.observe_verified_wallet(
            "0x3333333333333333333333333333333333333333"
        )

        with self.assertLogs("seller-agent.x402.usage", level="WARNING") as logs:
            reporter.submit_attempt(attempt)
            await reporter.wait_for_idle()

        self.assertEqual(requests, 2)
        rendered = "\n".join(logs.output)
        for forbidden in (
            "super-secret-token",
            "bnbagent-api.bnbchain.world",
            "0x3333333333333333333333333333333333333333",
            "private-network-error-detail",
        ):
            self.assertNotIn(forbidden, rendered)

    async def test_does_not_retry_non_retryable_or_redirect_responses(self) -> None:
        for status in (300, 302, 307, 308, 400, 401, 403, 404, 422):
            requests = 0

            def handle(
                _request: httpx.Request,
                status=status,
            ) -> httpx.Response:
                nonlocal requests
                requests += 1
                return httpx.Response(status)

            reporter = UsageEventReporter(
                self.config(),
                transport=httpx.MockTransport(handle),
                retry_delay_seconds=0,
            )
            with self.subTest(status=status), self.assertLogs(
                "seller-agent.x402.usage", level="WARNING"
            ):
                reporter.submit_attempt(reporter.start_attempt())
                await reporter.wait_for_idle()
            self.assertEqual(requests, 1)

    async def test_caps_active_delivery_tasks_at_32(self) -> None:
        release = asyncio.Event()
        started = 0

        async def handle(_request: httpx.Request) -> httpx.Response:
            nonlocal started
            started += 1
            await release.wait()
            return httpx.Response(201)

        reporter = UsageEventReporter(
            self.config(),
            transport=httpx.MockTransport(handle),
            retry_delay_seconds=0,
        )

        accepted = [
            reporter.submit_attempt(reporter.start_attempt())
            for _ in range(32)
        ]
        with self.assertLogs("seller-agent.x402.usage", level="WARNING"):
            rejected = reporter.submit_attempt(reporter.start_attempt())
        await asyncio.sleep(0)

        self.assertEqual(accepted, [True] * 32)
        self.assertIs(rejected, False)
        self.assertEqual(started, 32)
        release.set()
        await reporter.wait_for_idle()


if __name__ == "__main__":
    unittest.main()
