"""Best-effort delivery of privacy-limited x402 usage events."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from eth_utils import to_checksum_address

logger = logging.getLogger("seller-agent.x402.usage")

_ATTEMPT_EVENT_ID_RE = re.compile(r"usage-attempt:[0-9a-f]{32}\Z")
_JOB_ID_RE = re.compile(r"x402_[0-9a-f]{32}\Z")
_MAX_ACTIVE_TASKS = 32
_MAX_ATTEMPTS = 2
_TIMEOUT_SECONDS = 3.0


class UsageReportingError(ValueError):
    """Invalid usage reporting configuration or event data."""


@dataclass(frozen=True)
class UsageReportingConfig:
    base_url: str
    internal_token: str

    @property
    def endpoint_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/internal/x402/usage-events"


@dataclass
class UsageAttempt:
    event_id: str
    timestamp: int
    wallet: str | None = None

    def observe_verified_wallet(self, address: str) -> None:
        self.wallet = to_checksum_address(address).lower()


def load_usage_reporting_config(
    env: Mapping[str, str],
) -> UsageReportingConfig | None:
    """Load optional backend configuration without reading process globals."""
    base_url = env.get("API_BASE_URL", "").strip()
    internal_token = env.get("COMPETITION_INTERNAL_TOKEN", "").strip()
    if not base_url and not internal_token:
        return None
    if not base_url or not internal_token:
        raise UsageReportingError(
            "API_BASE_URL and COMPETITION_INTERNAL_TOKEN must be set together"
        )

    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise UsageReportingError(
            "API_BASE_URL must use HTTPS and contain a host without "
            "credentials, a query, or a fragment"
        )
    return UsageReportingConfig(
        base_url=base_url.rstrip("/"),
        internal_token=internal_token,
    )


class UsageEventReporter:
    """Construct and deliver strict usage events without blocking user calls."""

    def __init__(
        self,
        config: UsageReportingConfig,
        *,
        clock: Callable[[], int] | None = None,
        transport: Any = None,
        retry_delay_seconds: float = 0.1,
    ) -> None:
        self._config = config
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._transport = transport
        self._retry_delay_seconds = retry_delay_seconds
        self._active_tasks: set[asyncio.Task[None]] = set()

    def start_attempt(self) -> UsageAttempt:
        timestamp = self._clock()
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise UsageReportingError("usage event timestamp is invalid")
        return UsageAttempt(
            event_id=f"usage-attempt:{secrets.token_hex(16)}",
            timestamp=timestamp,
        )

    def submit_attempt(self, attempt: UsageAttempt) -> bool:
        """Snapshot and schedule one request attempt without awaiting delivery."""
        try:
            if not _ATTEMPT_EVENT_ID_RE.fullmatch(attempt.event_id):
                raise UsageReportingError("usage attempt event id is invalid")
            timestamp = self._validated_timestamp(attempt.timestamp)
            wallet = self._normalized_optional_wallet(attempt.wallet)
        except (TypeError, ValueError):
            logger.warning(
                "x402 usage event dropped event_type=attempt category=invalid"
            )
            return False
        return self._schedule(
            {
                "version": 1,
                "eventId": attempt.event_id,
                "eventType": "attempt",
                "timestamp": timestamp,
                "wallet": wallet,
            },
            event_type="attempt",
        )

    def submit_succeeded(
        self,
        *,
        job_id: str,
        wallet: str,
        timestamp: int,
    ) -> bool:
        """Schedule a stable event after a Job's durable succeeded transition."""
        try:
            if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
                raise UsageReportingError("usage succeeded job id is invalid")
            normalized_wallet = to_checksum_address(wallet).lower()
            normalized_timestamp = self._validated_timestamp(timestamp)
            digest = hashlib.sha256(job_id.encode("ascii")).hexdigest()
        except (TypeError, ValueError, UnicodeEncodeError):
            logger.warning(
                "x402 usage event dropped event_type=succeeded category=invalid"
            )
            return False
        return self._schedule(
            {
                "version": 1,
                "eventId": f"usage-succeeded:{digest}",
                "eventType": "succeeded",
                "timestamp": normalized_timestamp,
                "wallet": normalized_wallet,
            },
            event_type="succeeded",
        )

    @staticmethod
    def _validated_timestamp(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise UsageReportingError("usage event timestamp is invalid")
        return value

    @staticmethod
    def _normalized_optional_wallet(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise UsageReportingError("usage event wallet is invalid")
        return to_checksum_address(value).lower()

    def _schedule(self, payload: dict[str, object], *, event_type: str) -> bool:
        if len(self._active_tasks) >= _MAX_ACTIVE_TASKS:
            logger.warning(
                "x402 usage event dropped event_type=%s category=capacity",
                event_type,
            )
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "x402 usage event dropped event_type=%s category=no_loop",
                event_type,
            )
            return False
        task = loop.create_task(
            self._deliver(dict(payload), event_type=event_type),
            name=f"x402-usage:{event_type}",
        )
        self._active_tasks.add(task)
        task.add_done_callback(self._delivery_done)
        return True

    def _delivery_done(self, task: asyncio.Task[None]) -> None:
        self._active_tasks.discard(task)
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    async def _deliver(
        self,
        payload: dict[str, object],
        *,
        event_type: str,
    ) -> None:
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Token": self._config.internal_token,
        }
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                retryable = False
                try:
                    response = await client.post(
                        self._config.endpoint_url,
                        json=payload,
                        headers=headers,
                    )
                except httpx.RequestError:
                    category = "network"
                    retryable = True
                else:
                    if 200 <= response.status_code < 300:
                        return
                    retryable = response.status_code == 429 or (
                        500 <= response.status_code < 600
                    )
                    category = (
                        "retryable_http" if retryable else "non_retryable_http"
                    )
                logger.warning(
                    "x402 usage event delivery failed "
                    "event_type=%s category=%s attempt=%s/%s",
                    event_type,
                    category,
                    attempt,
                    _MAX_ATTEMPTS,
                )
                if not retryable or attempt == _MAX_ATTEMPTS:
                    return
                await asyncio.sleep(self._retry_delay_seconds)

    async def wait_for_idle(self) -> None:
        """Wait for currently scheduled metrics tasks; intended for tests."""
        while self._active_tasks:
            await asyncio.gather(
                *tuple(self._active_tasks),
                return_exceptions=True,
            )
            await asyncio.sleep(0)
