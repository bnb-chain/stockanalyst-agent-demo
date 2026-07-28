from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterator

from pydantic import ValidationError

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
_FENCE_OPEN = re.compile(r"```(?:json\b)?\s*(?=\{)", re.IGNORECASE)
_SAFE_LOCATION_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MAX_MODEL_RESPONSE_BYTES = 2_097_152
_MAX_JSON_CANDIDATES = 64
_MAX_LOGGED_VALIDATION_ISSUES = 20


def _response_error_code(value: object) -> str | None:
    if not isinstance(value, str):
        return "invalid_response_type"
    if len(value) > _MAX_MODEL_RESPONSE_BYTES:
        return "response_too_large"
    try:
        byte_count = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return "invalid_utf8"
    if byte_count > _MAX_MODEL_RESPONSE_BYTES:
        return "response_too_large"
    return None


def _json_candidates(text: str) -> Iterator[dict[str, object]]:
    decoder = json.JSONDecoder()
    seen_offsets: set[int] = set()
    attempt_count = 0

    def decode_at(offset: int) -> dict[str, object] | None:
        nonlocal attempt_count
        if offset in seen_offsets or attempt_count >= _MAX_JSON_CANDIDATES:
            return None
        seen_offsets.add(offset)
        attempt_count += 1
        try:
            candidate, _ = decoder.raw_decode(text, offset)
        except (ValueError, RecursionError):
            return None
        return candidate if isinstance(candidate, dict) else None

    for match in _FENCE_OPEN.finditer(text):
        candidate = decode_at(match.end())
        if candidate is not None:
            yield candidate
        if attempt_count >= _MAX_JSON_CANDIDATES:
            return

    offset = text.find("{")
    while offset != -1 and attempt_count < _MAX_JSON_CANDIDATES:
        candidate = decode_at(offset)
        if candidate is not None:
            yield candidate
        offset = text.find("{", offset + 1)


def _sanitized_validation_issues(
    error: ValidationError,
) -> tuple[tuple[tuple[int | str, ...], str], ...]:
    sanitized: list[tuple[tuple[int | str, ...], str]] = []
    for issue in error.errors(include_input=False, include_url=False):
        location: list[int | str] = []
        for part in issue.get("loc", ()):
            if isinstance(part, int) or (
                isinstance(part, str)
                and len(part) <= 64
                and _SAFE_LOCATION_PART.fullmatch(part) is not None
            ):
                location.append(part)
            else:
                location.append("<field>")
        raw_type = issue.get("type")
        error_type = (
            raw_type
            if (
                isinstance(raw_type, str)
                and len(raw_type) <= 64
                and _SAFE_LOCATION_PART.fullmatch(raw_type) is not None
            )
            else "validation_error"
        )
        sanitized.append((tuple(location), error_type))
        if len(sanitized) == _MAX_LOGGED_VALIDATION_ISSUES:
            break
    return tuple(sanitized)


async def generate_validated_report(
    prompt: str,
    *,
    session_id: str,
    symbols: list[str] | None,
    call_runner: Callable[[str, str], Awaitable[str]],
) -> str:
    raw = await call_runner(prompt, session_id)

    def parse(value: object) -> StockReport | None:
        response_error = _response_error_code(value)
        if response_error is not None:
            _log.warning(
                "report parse/validation failed: code=%s",
                response_error,
            )
            return None

        validation_issues: tuple[
            tuple[tuple[int | str, ...], str],
            ...,
        ] = ()
        for candidate in _json_candidates(value):
            try:
                return StockReport.model_validate(candidate)
            except ValidationError as error:
                if not validation_issues:
                    validation_issues = _sanitized_validation_issues(error)
        if validation_issues:
            _log.warning(
                "report validation failed: issues=%s",
                validation_issues,
            )
        else:
            _log.warning(
                "report parse/validation failed: code=no_json_candidate",
            )
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
