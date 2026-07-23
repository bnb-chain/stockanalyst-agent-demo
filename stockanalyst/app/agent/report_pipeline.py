from __future__ import annotations

from collections.abc import Awaitable, Callable
import json
import logging

try:
    from .report_renderer import render_report
    from .report_schema import StockReport
except ImportError:
    from report_renderer import render_report
    from report_schema import StockReport


SAFE_FAILURE_REPORT = """# Report generation unavailable

The analysis engine could not produce a valid structured report. No unvalidated
model output was delivered. Please retry with a new job."""

_log = logging.getLogger("seller-agent.report_pipeline")


def _extract_json(text: str) -> str:
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in LLM response")
    candidate = text[start:]
    decoder = json.JSONDecoder()
    try:
        _, end = decoder.raw_decode(candidate)
    except json.JSONDecodeError as error:
        raise ValueError("Invalid JSON object in LLM response") from error
    return candidate[:end]


async def generate_validated_report(
    prompt: str,
    *,
    session_id: str,
    symbols: list[str] | None,
    call_runner: Callable[[str, str], Awaitable[str]],
) -> str:
    raw = await call_runner(prompt, session_id)

    def parse(value: str) -> StockReport | None:
        try:
            return StockReport.model_validate_json(_extract_json(value))
        except Exception as error:
            _log.warning("report parse/validation failed: %s", error)
            return None

    report = parse(raw)
    if report is None:
        correction = (
            "Your previous response could not be parsed as valid JSON matching "
            "the StockReport schema. Output ONLY the corrected JSON object — no "
            "text before or after it, no code fences. Ensure the analyses array "
            "has one entry per symbol and all required fields are present."
        )
        report = parse(await call_runner(correction, session_id))
    if report is None:
        return SAFE_FAILURE_REPORT

    if symbols:
        returned = {analysis.symbol.upper() for analysis in report.analyses}
        missing = [symbol for symbol in symbols if symbol.upper() not in returned]
        if missing:
            _log.warning("analyses missing for symbols %s (session %s)", missing, session_id)
    return render_report(report)
