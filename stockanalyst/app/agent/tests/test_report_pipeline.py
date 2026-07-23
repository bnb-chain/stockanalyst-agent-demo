from __future__ import annotations

import json
import math
import unittest

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

    def test_unknown_fields_remain_ignored(self) -> None:
        payload = json.loads(_valid_report_json())
        payload["unknown_top_level"] = "ignored"
        payload["macro_snapshot"]["unknown_nested"] = "ignored"

        report = StockReport.model_validate(payload)

        self.assertFalse(hasattr(report, "unknown_top_level"))
        self.assertFalse(hasattr(report.macro_snapshot, "unknown_nested"))


class ReportPipelineTests(unittest.IsolatedAsyncioTestCase):
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
