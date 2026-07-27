from __future__ import annotations

import json
import math
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from stockanalyst.app.agent.report_pipeline import (
    SAFE_FAILURE_REPORT,
    generate_validated_report,
)
from stockanalyst.app.agent.report_schema import (
    ClientPosition,
    MacroSnapshot,
    StockReport,
)


def _valid_report_json() -> str:
    return json.dumps({
        "executive_summary": "AAPL remains stable.",
        "macro_snapshot": {
            "vix": "20",
            "vix_signal": "neutral",
            "fed_rate": "4",
            "fed_rate_signal": "restrictive",
            "treasury_10y": "4",
            "treasury_10y_signal": "neutral",
            "cpi_yoy": "3",
            "unemployment": "4",
            "macro_posture": "Neutral backdrop.",
        },
        "analyses": [{
            "symbol": "AAPL",
            "company_name": "Apple",
            "rating": "Hold",
            "price_target": 200,
            "implied_return_pct": 5,
            "horizon_months": 12,
            "risk_level": "Moderate",
            "rating_rationale": "Valuation is balanced.",
            "fundamentals_commentary": "Fundamentals remain sound.",
            "technicals_commentary": "Momentum is neutral.",
            "upside_catalysts": ["Services", "AI", "Buybacks"],
            "principal_risks": ["Demand", "Regulation", "FX"],
            "insider_activity": "No material activity.",
            "sentiment_summary": "Sentiment is neutral.",
        }],
        "portfolio_actions": [],
        "stop_losses": [],
        "watchlist": [],
        "risk_factors": [],
    })


class ReportBoundsTests(unittest.TestCase):
    def test_rejects_overlong_model_string(self) -> None:
        with self.assertRaises(ValidationError):
            MacroSnapshot(
                vix="20",
                vix_signal="neutral",
                fed_rate="4",
                fed_rate_signal="restrictive",
                treasury_10y="4",
                treasury_10y_signal="neutral",
                cpi_yoy="3",
                unemployment="4",
                macro_posture="x" * 8_193,
            )

    def test_rejects_non_finite_model_number(self) -> None:
        with self.assertRaises(ValidationError):
            ClientPosition(
                shares=math.inf,
                avg_cost=100,
                unrealised_pnl_pct=1,
                stop_loss=90,
                stop_loss_basis="MA-200",
                action_summary="Hold",
            )

    def test_caps_top_level_collections_without_new_lower_bounds(self) -> None:
        macro = MacroSnapshot(
            vix="20",
            vix_signal="neutral",
            fed_rate="4",
            fed_rate_signal="restrictive",
            treasury_10y="4",
            treasury_10y_signal="neutral",
            cpi_yoy="3",
            unemployment="4",
            macro_posture="neutral",
        )
        StockReport(
            executive_summary="ok",
            macro_snapshot=macro,
            analyses=[],
            portfolio_actions=[],
            stop_losses=[],
            watchlist=[],
            risk_factors=[],
        )
        with self.assertRaises(ValidationError):
            StockReport(
                executive_summary="ok",
                macro_snapshot=macro,
                analyses=[],
                portfolio_actions=[],
                stop_losses=[],
                watchlist=[],
                risk_factors=[{
                    "factor": "Liquidity",
                    "assessment": "Low",
                    "supporting_observation": "Liquid",
                    "threshold_to_act": "Spread widens",
                }] * 6,
            )

    def test_portfolio_actions_and_stop_losses_accept_50_and_reject_51(self) -> None:
        payload = json.loads(_valid_report_json())
        action = {
            "priority": 1,
            "action": "Hold",
            "symbol": "AAPL",
            "quantity": "10 shares",
            "price_level": "$200",
            "capital_impact": "$0",
            "rationale": "Position remains balanced.",
        }
        stop_loss = {
            "symbol": "AAPL",
            "avg_cost": 190,
            "stop_loss_level": 170,
            "risk_per_share": 20,
            "position_size": "10 shares",
            "max_loss_at_stop": "$200",
            "technical_basis": "MA-200",
        }
        payload["portfolio_actions"] = [action] * 50
        payload["stop_losses"] = [stop_loss] * 50

        StockReport.model_validate(payload)

        for field, entry in (
            ("portfolio_actions", action),
            ("stop_losses", stop_loss),
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                rejected = dict(payload)
                rejected[field] = [entry] * 51
                StockReport.model_validate(rejected)

    def test_nested_and_list_strings_accept_8192_and_reject_8193(self) -> None:
        payload = json.loads(_valid_report_json())
        payload["macro_snapshot"]["macro_posture"] = "m" * 8_192
        payload["analyses"][0]["upside_catalysts"][0] = "c" * 8_192
        StockReport.model_validate(payload)

        for path in ("nested", "list"):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                rejected = json.loads(_valid_report_json())
                if path == "nested":
                    rejected["macro_snapshot"]["macro_posture"] = "m" * 8_193
                else:
                    rejected["analyses"][0]["upside_catalysts"][0] = (
                        "c" * 8_193
                    )
                StockReport.model_validate(rejected)

    def test_every_report_collection_accepts_maximum_and_rejects_one_more(
        self,
    ) -> None:
        base = json.loads(_valid_report_json())
        entries = {
            "analyses": (base["analyses"][0], 10),
            "portfolio_actions": ({
                "priority": 1,
                "action": "Hold",
                "symbol": "AAPL",
                "quantity": "10 shares",
                "price_level": "$200",
                "capital_impact": "$0",
                "rationale": "Position remains balanced.",
            }, 50),
            "stop_losses": ({
                "symbol": "AAPL",
                "avg_cost": 190,
                "stop_loss_level": 170,
                "risk_per_share": 20,
                "position_size": "10 shares",
                "max_loss_at_stop": "$200",
                "technical_basis": "MA-200",
            }, 50),
            "watchlist": ({
                "ticker": "MSFT",
                "company": "Microsoft",
                "strategic_rationale": "Diversifies platform exposure.",
                "key_catalyst": "Cloud growth",
                "entry_zone": "$400-$420",
                "risk": "Valuation",
                "thesis": "Cloud demand is durable. Valuation needs support.",
            }, 5),
            "risk_factors": ({
                "factor": "Liquidity",
                "assessment": "Low",
                "supporting_observation": "Liquid",
                "threshold_to_act": "Spread widens",
            }, 5),
        }

        for field, (entry, maximum) in entries.items():
            with self.subTest(field=field, boundary="accepted"):
                accepted = json.loads(_valid_report_json())
                accepted[field] = [entry] * maximum
                StockReport.model_validate(accepted)
            with (
                self.subTest(field=field, boundary="rejected"),
                self.assertRaises(ValidationError),
            ):
                rejected = json.loads(_valid_report_json())
                rejected[field] = [entry] * (maximum + 1)
                StockReport.model_validate(rejected)

    def test_catalyst_and_risk_prose_keep_three_to_ten_boundary(self) -> None:
        for field in ("upside_catalysts", "principal_risks"):
            with self.subTest(field=field, boundary="ten"):
                accepted = json.loads(_valid_report_json())
                accepted["analyses"][0][field] = ["item"] * 10
                StockReport.model_validate(accepted)
            for count in (2, 11):
                with (
                    self.subTest(field=field, rejected_count=count),
                    self.assertRaises(ValidationError),
                ):
                    rejected = json.loads(_valid_report_json())
                    rejected["analyses"][0][field] = ["item"] * count
                    StockReport.model_validate(rejected)

    def test_unknown_fields_remain_ignored(self) -> None:
        payload = json.loads(_valid_report_json())
        payload["unknown_top_level"] = "ignored" * 8_193
        payload["macro_snapshot"]["unknown_nested"] = "ignored" * 8_193

        report = StockReport.model_validate(payload)

        self.assertFalse(hasattr(report, "unknown_top_level"))
        self.assertFalse(hasattr(report.macro_snapshot, "unknown_nested"))


class ReportPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_logs_exclude_invalid_model_values(self) -> None:
        first = json.loads(_valid_report_json())
        first["analyses"][0]["rating"] = "FIRST_SECRET_RATING"
        second = json.loads(_valid_report_json())
        second["analyses"][0]["rating"] = "SECOND_SECRET_RATING"
        outputs = iter((json.dumps(first), json.dumps(second)))

        async def call_runner(prompt: str, session_id: str) -> str:
            del prompt
            self.assertEqual(session_id, "42")
            return next(outputs)

        with self.assertLogs(
            "seller-agent.report_pipeline",
            level="WARNING",
        ) as captured:
            result = await generate_validated_report(
                "original prompt",
                session_id="42",
                symbols=["AAPL"],
                call_runner=call_runner,
            )

        logs = "\n".join(captured.output)
        self.assertEqual(result, SAFE_FAILURE_REPORT)
        self.assertNotIn("FIRST_SECRET_RATING", result)
        self.assertNotIn("SECOND_SECRET_RATING", result)
        self.assertNotIn("FIRST_SECRET_RATING", logs)
        self.assertNotIn("SECOND_SECRET_RATING", logs)
        self.assertNotIn("input_value", logs)
        self.assertNotIn("pydantic.dev", logs)
        self.assertIn("analyses", logs)
        self.assertIn("rating", logs)

    async def test_invalid_response_types_log_only_stable_codes(self) -> None:
        cases = (
            ({"secret": "NON_STRING_SECRET"}, "invalid_response_type"),
            ("SURROGATE_SECRET\ud800", "invalid_utf8"),
        )
        for output, code in cases:
            calls = 0

            async def call_runner(
                prompt: str,
                session_id: str,
                *,
                _output: object = output,
            ):
                nonlocal calls
                del prompt
                self.assertEqual(session_id, "42")
                calls += 1
                return _output

            with (
                self.subTest(code=code),
                self.assertLogs(
                    "seller-agent.report_pipeline",
                    level="WARNING",
                ) as captured,
            ):
                result = await generate_validated_report(
                    "original prompt",
                    session_id="42",
                    symbols=["AAPL"],
                    call_runner=call_runner,
                )

            logs = "\n".join(captured.output)
            self.assertEqual(result, SAFE_FAILURE_REPORT)
            self.assertEqual(calls, 2)
            self.assertIn(code, logs)
            self.assertNotIn("NON_STRING_SECRET", logs)
            self.assertNotIn("SURROGATE_SECRET", logs)

    async def test_scans_bare_report_after_unrelated_json_object(self) -> None:
        payload = json.loads(_valid_report_json())
        payload["executive_summary"] = "Bare report after metadata."
        response = f'{{"status":"ready"}}\n{json.dumps(payload)}'
        calls = 0

        async def call_runner(prompt: str, session_id: str) -> str:
            nonlocal calls
            del prompt
            self.assertEqual(session_id, "42")
            calls += 1
            return response

        result = await generate_validated_report(
            "original prompt",
            session_id="42",
            symbols=["AAPL"],
            call_runner=call_runner,
        )

        self.assertEqual(calls, 1)
        self.assertIn("Bare report after metadata.", result)
        self.assertNotEqual(result, SAFE_FAILURE_REPORT)

    async def test_tries_later_fenced_report_after_invalid_fenced_object(
        self,
    ) -> None:
        payload = json.loads(_valid_report_json())
        payload["executive_summary"] = "Second fenced report."
        response = (
            '```json\n{"status":"invalid"}\n```\n'
            f"```json\n{json.dumps(payload)}\n```\n"
        )
        calls = 0

        async def call_runner(prompt: str, session_id: str) -> str:
            nonlocal calls
            del prompt
            self.assertEqual(session_id, "42")
            calls += 1
            return response

        result = await generate_validated_report(
            "original prompt",
            session_id="42",
            symbols=["AAPL"],
            call_runner=call_runner,
        )

        self.assertEqual(calls, 1)
        self.assertIn("Second fenced report.", result)
        self.assertNotEqual(result, SAFE_FAILURE_REPORT)

    async def test_model_response_budget_counts_utf8_bytes(self) -> None:
        limit = 2_097_152
        valid = _valid_report_json()
        remaining = limit - len(valid.encode("utf-8"))
        exact = (
            valid
            + ("界" * (remaining // len("界".encode())))
            + ("x" * (remaining % len("界".encode())))
        )
        self.assertEqual(len(exact.encode("utf-8")), limit)

        exact_calls = 0

        async def exact_runner(prompt: str, session_id: str) -> str:
            nonlocal exact_calls
            del prompt
            self.assertEqual(session_id, "42")
            exact_calls += 1
            return exact

        accepted = await generate_validated_report(
            "original prompt",
            session_id="42",
            symbols=["AAPL"],
            call_runner=exact_runner,
        )

        self.assertEqual(exact_calls, 1)
        self.assertNotEqual(accepted, SAFE_FAILURE_REPORT)

        async def oversized_runner(prompt: str, session_id: str) -> str:
            del prompt
            self.assertEqual(session_id, "42")
            return exact + "x"

        with self.assertLogs("seller-agent.report_pipeline", level="WARNING"):
            rejected = await generate_validated_report(
                "original prompt",
                session_id="42",
                symbols=["AAPL"],
                call_runner=oversized_runner,
            )

        self.assertEqual(rejected, SAFE_FAILURE_REPORT)

    async def test_decoded_candidate_budget_accepts_64_and_rejects_65(
        self,
    ) -> None:
        valid = _valid_report_json()

        async def run_with_preamble(count: int) -> tuple[str, int]:
            response = "\n".join(
                [json.dumps({"status": index}) for index in range(count)]
                + [valid]
            )
            calls = 0

            async def call_runner(prompt: str, session_id: str) -> str:
                nonlocal calls
                del prompt
                self.assertEqual(session_id, "42")
                calls += 1
                return response

            if count == 64:
                with self.assertLogs(
                    "seller-agent.report_pipeline",
                    level="WARNING",
                ):
                    result = await generate_validated_report(
                        "original prompt",
                        session_id="42",
                        symbols=["AAPL"],
                        call_runner=call_runner,
                    )
            else:
                result = await generate_validated_report(
                    "original prompt",
                    session_id="42",
                    symbols=["AAPL"],
                    call_runner=call_runner,
                )
            return result, calls

        accepted, accepted_calls = await run_with_preamble(63)
        self.assertNotEqual(accepted, SAFE_FAILURE_REPORT)
        self.assertEqual(accepted_calls, 1)

        rejected, rejected_calls = await run_with_preamble(64)
        self.assertEqual(rejected, SAFE_FAILURE_REPORT)
        self.assertEqual(rejected_calls, 2)

    async def test_malformed_openings_share_the_64_attempt_budget(
        self,
    ) -> None:
        response = (
            ("```json\n{\n```\n" * 32)
            + ("x{" * 100_000)
            + "MALFORMED_OPENING_SECRET"
        )
        self.assertEqual(response.count("{"), 100_032)
        self.assertLessEqual(len(response.encode("utf-8")), 2_097_152)
        original_raw_decode = json.JSONDecoder.raw_decode
        attempts = 0
        attempted_offsets: list[int] = []
        calls = 0

        def counting_raw_decode(
            decoder: json.JSONDecoder,
            text: str,
            offset: int = 0,
        ):
            nonlocal attempts
            attempts += 1
            attempted_offsets.append(offset)
            return original_raw_decode(decoder, text, offset)

        async def call_runner(prompt: str, session_id: str) -> str:
            nonlocal calls
            del prompt
            self.assertEqual(session_id, "42")
            calls += 1
            return response if calls == 1 else "no JSON object"

        with (
            patch(
                "stockanalyst.app.agent.report_pipeline.json.JSONDecoder.raw_decode",
                new=counting_raw_decode,
            ),
            self.assertLogs(
                "seller-agent.report_pipeline",
                level="WARNING",
            ) as captured,
        ):
            result = await generate_validated_report(
                "original prompt",
                session_id="42",
                symbols=["AAPL"],
                call_runner=call_runner,
            )

        self.assertEqual(result, SAFE_FAILURE_REPORT)
        self.assertEqual(calls, 2)
        self.assertEqual(attempts, 64)
        self.assertEqual(len(set(attempted_offsets)), 64)
        self.assertIn("no_json_candidate", "\n".join(captured.output))
        self.assertNotIn(
            "MALFORMED_OPENING_SECRET",
            "\n".join(captured.output),
        )

    async def test_json_decoding_uses_offsets_without_suffix_slices(self) -> None:
        class NoSuffixSlices(str):
            def __getitem__(self, key):
                if isinstance(key, slice) and key.start is not None:
                    raise AssertionError("suffix slice attempted")
                return super().__getitem__(key)

        async def call_runner(prompt: str, session_id: str) -> str:
            del prompt
            self.assertEqual(session_id, "42")
            return NoSuffixSlices(_valid_report_json())

        result = await generate_validated_report(
            "original prompt",
            session_id="42",
            symbols=["AAPL"],
            call_runner=call_runner,
        )

        self.assertNotEqual(result, SAFE_FAILURE_REPORT)

    async def test_memory_error_during_candidate_scan_is_not_swallowed(
        self,
    ) -> None:
        async def call_runner(prompt: str, session_id: str) -> str:
            del prompt
            self.assertEqual(session_id, "42")
            return _valid_report_json()

        with (
            patch(
                "stockanalyst.app.agent.report_pipeline.json.JSONDecoder.raw_decode",
                side_effect=MemoryError("exhausted"),
            ),
            self.assertRaises(MemoryError),
        ):
            await generate_validated_report(
                "original prompt",
                session_id="42",
                symbols=["AAPL"],
                call_runner=call_runner,
            )

    async def test_deeply_nested_model_json_fails_closed(self) -> None:
        response = (
            '{"nested":'
            + ("[" * 500_000)
            + "0"
            + ("]" * 500_000)
            + "}"
        )

        async def call_runner(prompt: str, session_id: str) -> str:
            del prompt
            self.assertEqual(session_id, "42")
            return response

        with self.assertLogs(
            "seller-agent.report_pipeline",
            level="WARNING",
        ) as captured:
            result = await generate_validated_report(
                "original prompt",
                session_id="42",
                symbols=["AAPL"],
                call_runner=call_runner,
            )

        self.assertEqual(result, SAFE_FAILURE_REPORT)
        self.assertNotIn("maximum recursion", "\n".join(captured.output).lower())

    async def test_huge_integer_model_json_fails_closed(self) -> None:
        response = '{"value":' + ("9" * 5_000) + "}"
        calls = 0

        async def call_runner(prompt: str, session_id: str) -> str:
            nonlocal calls
            del prompt
            self.assertEqual(session_id, "42")
            calls += 1
            return response

        with self.assertLogs(
            "seller-agent.report_pipeline",
            level="WARNING",
        ) as captured:
            result = await generate_validated_report(
                "original prompt",
                session_id="42",
                symbols=["AAPL"],
                call_runner=call_runner,
            )

        logs = "\n".join(captured.output)
        self.assertEqual(result, SAFE_FAILURE_REPORT)
        self.assertEqual(calls, 2)
        self.assertIn("no_json_candidate", logs)
        self.assertNotIn("Exceeds the limit", logs)
        self.assertNotIn("9" * 100, logs)

    def test_safe_failure_report_matches_the_approved_literal(self) -> None:
        self.assertEqual(
            SAFE_FAILURE_REPORT,
            "# Report generation unavailable\n\n"
            "The analysis engine could not produce a valid structured report. "
            "No unvalidated\n"
            "model output was delivered. Please retry with a new job.",
        )

    async def test_fenced_report_allows_triple_backticks_inside_json_string(self) -> None:
        payload = json.loads(_valid_report_json())
        summary = "AAPL safely describes ```code``` markers in prose."
        payload["executive_summary"] = summary
        response = (
            'Context {requested}; metadata: {"status":"ready"}\n'
            f"```json\n{json.dumps(payload)}\n```\n"
        )
        calls = 0

        async def call_runner(prompt: str, session_id: str) -> str:
            nonlocal calls
            self.assertEqual(session_id, "42")
            calls += 1
            return response

        result = await generate_validated_report(
            "original prompt",
            session_id="42",
            symbols=["AAPL"],
            call_runner=call_runner,
        )

        self.assertEqual(calls, 1)
        self.assertIn(summary, result)
        self.assertNotEqual(result, SAFE_FAILURE_REPORT)

    async def test_prefers_fenced_report_after_invalid_braced_prose(self) -> None:
        payload = json.loads(_valid_report_json())
        payload["executive_summary"] = "Fenced report after braced prose."
        response = (
            "Here is {the requested report}:\n"
            f"```json\n{json.dumps(payload)}\n```\n"
        )
        calls = 0

        async def call_runner(prompt: str, session_id: str) -> str:
            nonlocal calls
            self.assertEqual(session_id, "42")
            calls += 1
            return response

        result = await generate_validated_report(
            "original prompt",
            session_id="42",
            symbols=["AAPL"],
            call_runner=call_runner,
        )

        self.assertEqual(calls, 1)
        self.assertIn("Fenced report after braced prose.", result)
        self.assertNotEqual(result, SAFE_FAILURE_REPORT)

    async def test_prefers_fenced_report_over_unrelated_json_preamble(self) -> None:
        payload = json.loads(_valid_report_json())
        payload["executive_summary"] = "Fenced report after JSON metadata."
        response = (
            'Metadata: {"status":"ready"}\n'
            f"```json\n{json.dumps(payload)}\n```\n"
        )
        calls = 0

        async def call_runner(prompt: str, session_id: str) -> str:
            nonlocal calls
            self.assertEqual(session_id, "42")
            calls += 1
            return response

        result = await generate_validated_report(
            "original prompt",
            session_id="42",
            symbols=["AAPL"],
            call_runner=call_runner,
        )

        self.assertEqual(calls, 1)
        self.assertIn("Fenced report after JSON metadata.", result)
        self.assertNotEqual(result, SAFE_FAILURE_REPORT)

    async def test_renders_fenced_json_with_braces_and_escapes_in_strings(self) -> None:
        payload = json.loads(_valid_report_json())
        summary = 'AAPL uses } plus an escaped "quote" and C:\\reports safely.'
        payload["executive_summary"] = summary
        fenced = f"Here is the report:\n```json\n{json.dumps(payload)}\n```\n"
        calls = 0

        async def call_runner(prompt: str, session_id: str) -> str:
            nonlocal calls
            self.assertEqual(session_id, "42")
            calls += 1
            return fenced

        result = await generate_validated_report(
            "original prompt",
            session_id="42",
            symbols=["AAPL"],
            call_runner=call_runner,
        )

        self.assertEqual(calls, 1)
        self.assertIn(summary, result)
        self.assertNotEqual(result, SAFE_FAILURE_REPORT)

    async def test_preserves_valid_structured_report_rendering(self) -> None:
        calls = 0

        async def call_runner(prompt: str, session_id: str) -> str:
            nonlocal calls
            self.assertEqual(prompt, "original prompt")
            self.assertEqual(session_id, "42")
            calls += 1
            return _valid_report_json()

        result = await generate_validated_report(
            "original prompt",
            session_id="42",
            symbols=["AAPL"],
            call_runner=call_runner,
        )

        self.assertEqual(calls, 1)
        self.assertIn("AAPL", result)
        self.assertIn("AAPL remains stable.", result)
        self.assertNotIn("PORTFOLIO REBALANCING PLAN", result)
        self.assertNotIn("STOP-LOSS SCHEDULE", result)
        self.assertNotIn("SECTOR WATCHLIST", result)
        self.assertNotIn("RISK DASHBOARD", result)
        self.assertNotEqual(result, SAFE_FAILURE_REPORT)

    async def test_returns_only_fixed_safe_report_after_two_invalid_outputs(self) -> None:
        outputs = iter([
            "FIRST SECRET RAW MODEL OUTPUT",
            "SECOND SECRET RAW MODEL OUTPUT",
        ])
        prompts: list[str] = []

        async def call_runner(prompt: str, session_id: str) -> str:
            self.assertEqual(session_id, "42")
            prompts.append(prompt)
            return next(outputs)

        with self.assertLogs(
            "seller-agent.report_pipeline",
            level="WARNING",
        ):
            result = await generate_validated_report(
                "original prompt",
                session_id="42",
                symbols=["AAPL"],
                call_runner=call_runner,
            )

        self.assertEqual(result, SAFE_FAILURE_REPORT)
        self.assertNotIn("FIRST SECRET", result)
        self.assertNotIn("SECOND SECRET", result)
        self.assertEqual(len(prompts), 2)
        self.assertIn("corrected JSON object", prompts[1])
