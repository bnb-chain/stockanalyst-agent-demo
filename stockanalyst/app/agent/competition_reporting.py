"""Best-effort reporting of successful competition payment events."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from eth_utils import to_checksum_address

logger = logging.getLogger("seller-agent.competition")


class CompetitionReportingError(ValueError):
    """Invalid competition reporting configuration."""


@dataclass(frozen=True)
class CompetitionReportingConfig:
    endpoint_url: str
    internal_token: str


class CompetitionReporter:
    """Send idempotent successful-payment events without failing user calls."""

    def __init__(
        self,
        config: CompetitionReportingConfig,
        *,
        transport: Any = None,
        timeout_seconds: float = 3.0,
        max_attempts: int = 2,
        retry_delay_seconds: float = 0.1,
    ) -> None:
        self._config = config
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds

    async def report(
        self,
        *,
        event_id: str,
        address: str,
        called_at: int,
    ) -> bool:
        """Return whether the event was accepted; never raise to the caller."""
        try:
            normalized_event_id = str(event_id).strip()
            if not normalized_event_id:
                raise ValueError("event_id is empty")
            normalized_address = to_checksum_address(address).lower()
            normalized_called_at = int(called_at)
            if normalized_called_at < 0:
                raise ValueError("called_at is negative")
        except (TypeError, ValueError):
            logger.warning("competition event rejected before reporting")
            return False

        payload = {
            "eventId": normalized_event_id,
            "address": normalized_address,
            "calledAt": normalized_called_at,
        }
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Token": self._config.internal_token,
        }

        for attempt in range(1, self._max_attempts + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout_seconds,
                    transport=self._transport,
                ) as client:
                    response = await client.post(
                        self._config.endpoint_url,
                        json=payload,
                        headers=headers,
                    )
            except httpx.RequestError:
                logger.warning(
                    "competition event delivery failed (attempt %s/%s)",
                    attempt,
                    self._max_attempts,
                )
            else:
                if 200 <= response.status_code < 300:
                    return True
                logger.warning(
                    "competition event rejected with HTTP %s (attempt %s/%s)",
                    response.status_code,
                    attempt,
                    self._max_attempts,
                )
                if response.status_code < 500:
                    return False

            if attempt < self._max_attempts:
                await asyncio.sleep(self._retry_delay_seconds)

        return False


def load_competition_reporting_config(
    env: Mapping[str, str],
) -> CompetitionReportingConfig | None:
    """Load optional reporting configuration, rejecting partial setup."""
    endpoint_url = env.get("COMPETITION_AI_CALLS_URL", "").strip()
    internal_token = env.get("COMPETITION_INTERNAL_TOKEN", "").strip()
    if not endpoint_url and not internal_token:
        return None
    if not endpoint_url or not internal_token:
        raise CompetitionReportingError(
            "COMPETITION_AI_CALLS_URL and COMPETITION_INTERNAL_TOKEN "
            "must be set together"
        )

    parsed = urlsplit(endpoint_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise CompetitionReportingError(
            "COMPETITION_AI_CALLS_URL must be an HTTP(S) URL "
            "without credentials or a fragment"
        )

    return CompetitionReportingConfig(
        endpoint_url=endpoint_url,
        internal_token=internal_token,
    )


_DEFAULT_CONFIG = load_competition_reporting_config(os.environ)
_DEFAULT_REPORTER = (
    CompetitionReporter(_DEFAULT_CONFIG) if _DEFAULT_CONFIG is not None else None
)


async def report_competition_call(
    *,
    event_id: str,
    address: str,
    called_at: int | None = None,
) -> bool:
    """Report through environment configuration, disabled when both vars are absent."""
    if _DEFAULT_REPORTER is None:
        return False
    return await _DEFAULT_REPORTER.report(
        event_id=event_id,
        address=address,
        called_at=called_at if called_at is not None else int(time.time() * 1000),
    )
