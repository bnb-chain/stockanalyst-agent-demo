from __future__ import annotations

import json
import unittest

import httpx

from stockanalyst.app.agent.competition_reporting import (
    CompetitionReporter,
    CompetitionReportingError,
    load_competition_reporting_config,
)


class CompetitionReportingConfigTests(unittest.TestCase):
    def test_absent_configuration_disables_reporting(self) -> None:
        self.assertIsNone(load_competition_reporting_config({}))

    def test_url_and_token_must_be_configured_together(self) -> None:
        for env in (
            {"COMPETITION_AI_CALLS_URL": "http://competition.internal/events"},
            {"COMPETITION_INTERNAL_TOKEN": "secret"},
        ):
            with self.subTest(env=env):
                with self.assertRaisesRegex(
                    CompetitionReportingError,
                    "must be set together",
                ):
                    load_competition_reporting_config(env)

    def test_complete_configuration_is_loaded(self) -> None:
        config = load_competition_reporting_config(
            {
                "COMPETITION_AI_CALLS_URL": (
                    "http://bnbagent-api-dex-app:3001"
                    "/internal/competition/ai-calls"
                ),
                "COMPETITION_INTERNAL_TOKEN": "secret",
            }
        )

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(
            config.endpoint_url,
            "http://bnbagent-api-dex-app:3001/internal/competition/ai-calls",
        )
        self.assertEqual(config.internal_token, "secret")


class CompetitionReporterTests(unittest.IsolatedAsyncioTestCase):
    def config(self):
        config = load_competition_reporting_config(
            {
                "COMPETITION_AI_CALLS_URL": (
                    "http://bnbagent-api-dex-app:3001"
                    "/internal/competition/ai-calls"
                ),
                "COMPETITION_INTERNAL_TOKEN": "secret",
            }
        )
        assert config is not None
        return config

    async def test_posts_the_expected_authenticated_event(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        reporter = CompetitionReporter(
            self.config(),
            transport=httpx.MockTransport(handle),
            retry_delay_seconds=0,
        )

        reported = await reporter.report(
            event_id="b402:97:0x1111111111111111111111111111111111111111:0xabc",
            address="0x1111111111111111111111111111111111111111",
            called_at=1_785_340_800_123,
        )

        self.assertIs(reported, True)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.headers["X-Internal-Token"], "secret")
        self.assertEqual(
            json.loads(request.content),
            {
                "eventId": (
                    "b402:97:0x1111111111111111111111111111111111111111:0xabc"
                ),
                "address": "0x1111111111111111111111111111111111111111",
                "calledAt": 1_785_340_800_123,
            },
        )

    async def test_retries_a_server_failure_with_the_same_event(self) -> None:
        bodies: list[dict] = []

        def handle(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(503 if len(bodies) == 1 else 200)

        reporter = CompetitionReporter(
            self.config(),
            transport=httpx.MockTransport(handle),
            retry_delay_seconds=0,
        )

        with self.assertLogs("seller-agent.competition", level="WARNING"):
            reported = await reporter.report(
                event_id="erc8183:97:0x3333333333333333333333333333333333333333:42",
                address="0x2222222222222222222222222222222222222222",
                called_at=1_785_340_800_123,
            )

        self.assertIs(reported, True)
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0], bodies[1])

    async def test_failure_is_fail_open_after_bounded_attempts(self) -> None:
        attempts = 0

        def handle(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503)

        reporter = CompetitionReporter(
            self.config(),
            transport=httpx.MockTransport(handle),
            retry_delay_seconds=0,
        )

        with self.assertLogs("seller-agent.competition", level="WARNING"):
            reported = await reporter.report(
                event_id="event-1",
                address="0x2222222222222222222222222222222222222222",
                called_at=1_785_340_800_123,
            )

        self.assertIs(reported, False)
        self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
