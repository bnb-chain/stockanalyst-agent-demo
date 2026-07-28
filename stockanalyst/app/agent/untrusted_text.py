"""Deterministic normalization for untrusted third-party text."""
from __future__ import annotations

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")


def normalize_untrusted_text(value: object, *, max_chars: int) -> str:
    """Return bounded, single-line text while preserving normal Unicode."""
    if not isinstance(value, str) or max_chars <= 0:
        return ""
    without_controls = "".join(
        " " if unicodedata.category(char) == "Cc" else char
        for char in value
    )
    normalized = _WHITESPACE.sub(" ", without_controls).strip()
    if len(normalized) <= max_chars:
        return normalized

    available = max_chars - 1
    candidate = normalized[:available].rstrip()
    boundary = candidate.rfind(" ")
    if boundary >= available // 2:
        candidate = candidate[:boundary].rstrip()
    return candidate + "…"
