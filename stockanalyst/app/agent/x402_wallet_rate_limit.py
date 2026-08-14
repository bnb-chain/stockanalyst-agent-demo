from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from x402_job_store import JobConflict, StoredWalletRateLimit, X402JobStore

_CAPACITY = 30
_WINDOW_MILLISECONDS = 3_600_000
_DOMAIN = b"x402-wallet-rate-limit:v1:"
_RESERVATION_ID_RE = re.compile(r"x402_[0-9a-f]{32}\Z")
_MAX_TIMESTAMP_MILLISECONDS = 9_999_999_999_999
_DEFAULT_MAX_ATTEMPTS = 64


class WalletRateLimitExceeded(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("wallet rate limit exceeded")
        self.retry_after_seconds = retry_after_seconds


class WalletRateLimitUnavailable(Exception):
    pass


@dataclass(frozen=True)
class WalletRateReservation:
    wallet_digest: str
    reservation_id: str
    reserved_at: int
    state: Literal["reserved", "committed"]
    created_by_caller: bool = False


def _default_conflict_delay(attempt: int) -> float:
    ceiling = min(0.025, 0.001 * (2 ** (attempt - 1)))
    return ceiling * (0.5 + secrets.randbelow(501) / 1_000)


class WalletRateLimiter:
    def __init__(
        self,
        *,
        store: X402JobStore,
        token_secret: bytes,
        clock: Callable[[], int] | None = None,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        conflict_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        conflict_delay: Callable[[int], float] = _default_conflict_delay,
    ) -> None:
        if not isinstance(token_secret, bytes) or len(token_secret) < 32:
            raise ValueError("wallet rate-limit secret must contain at least 32 bytes")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 64:
            raise ValueError("wallet rate-limit attempts must be between 1 and 64")
        self._store = store
        self._token_secret = token_secret
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._max_attempts = max_attempts
        self._conflict_sleep = conflict_sleep
        self._conflict_delay = conflict_delay

    def _wallet_digest(self, wallet: str) -> str:
        if not isinstance(wallet, str) or not 1 <= len(wallet) <= 256:
            raise ValueError("invalid wallet rate-limit identity")
        return hmac.new(
            self._token_secret,
            _DOMAIN + wallet.lower().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _now(self) -> int:
        now = self._clock()
        if (
            type(now) is not int
            or not 1 <= now <= _MAX_TIMESTAMP_MILLISECONDS
        ):
            raise WalletRateLimitUnavailable("wallet rate limit unavailable")
        return now

    @staticmethod
    def _active_entries(
        stored: StoredWalletRateLimit,
        now: int,
    ) -> list[dict[str, object]]:
        cutoff = now - _WINDOW_MILLISECONDS
        return [
            dict(entry)
            for entry in stored.record["entries"]
            if entry["reservedAt"] > cutoff
        ]

    @staticmethod
    def _reservation(
        wallet_digest: str,
        entry: dict[str, object],
        *,
        created_by_caller: bool,
    ) -> WalletRateReservation:
        state = entry["state"]
        if state not in {"reserved", "committed"}:
            raise WalletRateLimitUnavailable("wallet rate limit unavailable")
        return WalletRateReservation(
            wallet_digest=wallet_digest,
            reservation_id=str(entry["reservationId"]),
            reserved_at=int(entry["reservedAt"]),
            state=state,
            created_by_caller=created_by_caller,
        )

    async def _backoff(self, attempt: int) -> None:
        try:
            delay = self._conflict_delay(attempt)
        except Exception as exc:
            raise WalletRateLimitUnavailable(
                "wallet rate limit unavailable"
            ) from exc
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not 0 <= delay <= 0.1
        ):
            raise WalletRateLimitUnavailable("wallet rate limit unavailable")
        await self._conflict_sleep(float(delay))

    async def reserve(
        self,
        wallet: str,
        reservation_id: str,
    ) -> WalletRateReservation:
        wallet_digest = self._wallet_digest(wallet)
        if (
            not isinstance(reservation_id, str)
            or not _RESERVATION_ID_RE.fullmatch(reservation_id)
        ):
            raise ValueError("invalid wallet rate-limit reservation")
        for _attempt in range(self._max_attempts):
            now = self._now()
            try:
                stored = await self._store.read_wallet_rate_limit(
                    wallet_digest
                )
                if stored is None:
                    entry: dict[str, object] = {
                        "reservationId": reservation_id,
                        "reservedAt": now,
                        "state": "reserved",
                    }
                    created = await self._store.create_wallet_rate_limit(
                        wallet_digest,
                        {"version": 1, "entries": [entry]},
                    )
                    if created is None:
                        if _attempt + 1 < self._max_attempts:
                            await self._backoff(_attempt + 1)
                        continue
                    return self._reservation(
                        wallet_digest,
                        entry,
                        created_by_caller=True,
                    )

                entries = self._active_entries(stored, now)
                matching = next(
                    (
                        entry
                        for entry in entries
                        if entry["reservationId"] == reservation_id
                    ),
                    None,
                )
                if matching is not None:
                    if len(entries) != len(stored.record["entries"]):
                        await self._store.replace_wallet_rate_limit(
                            wallet_digest,
                            stored,
                            {"version": 1, "entries": entries},
                        )
                    return self._reservation(
                        wallet_digest,
                        matching,
                        created_by_caller=False,
                    )

                if len(entries) >= _CAPACITY:
                    oldest = min(int(entry["reservedAt"]) for entry in entries)
                    remaining = oldest + _WINDOW_MILLISECONDS - now
                    retry_after = max(1, (remaining + 999) // 1000)
                    raise WalletRateLimitExceeded(retry_after)

                entry = {
                    "reservationId": reservation_id,
                    "reservedAt": now,
                    "state": "reserved",
                }
                await self._store.replace_wallet_rate_limit(
                    wallet_digest,
                    stored,
                    {"version": 1, "entries": [*entries, entry]},
                )
                return self._reservation(
                    wallet_digest,
                    entry,
                    created_by_caller=True,
                )
            except WalletRateLimitExceeded:
                raise
            except JobConflict:
                if _attempt + 1 < self._max_attempts:
                    await self._backoff(_attempt + 1)
                continue
            except WalletRateLimitUnavailable:
                raise
            except Exception as exc:
                raise WalletRateLimitUnavailable(
                    "wallet rate limit unavailable"
                ) from exc
        raise WalletRateLimitUnavailable("wallet rate limit unavailable")

    async def commit(self, reservation: WalletRateReservation) -> None:
        await self._mutate(reservation, commit=True)

    async def release(self, reservation: WalletRateReservation) -> None:
        await self._mutate(reservation, commit=False)

    async def confirm(
        self,
        reservation: WalletRateReservation,
    ) -> Literal["active", "expired"]:
        now = self._now()
        if not self._reservation_is_active(reservation, now):
            return "expired"
        state = await self._read_commit_state(reservation)
        if state != "reserved":
            raise WalletRateLimitUnavailable("wallet rate limit unavailable")
        return "active"

    async def _mutate(
        self,
        reservation: WalletRateReservation,
        *,
        commit: bool,
    ) -> None:
        for _attempt in range(self._max_attempts):
            now = self._now()
            try:
                stored = await self._store.read_wallet_rate_limit(
                    reservation.wallet_digest
                )
                if stored is None:
                    if commit and self._reservation_is_active(
                        reservation,
                        now,
                    ):
                        raise WalletRateLimitUnavailable(
                            "wallet rate limit unavailable"
                        )
                    return
                entries = self._active_entries(stored, now)
                matching_index = next(
                    (
                        index
                        for index, entry in enumerate(entries)
                        if entry["reservationId"] == reservation.reservation_id
                        and entry["reservedAt"] == reservation.reserved_at
                    ),
                    None,
                )
                if matching_index is None:
                    if commit and self._reservation_is_active(
                        reservation,
                        now,
                    ):
                        raise WalletRateLimitUnavailable(
                            "wallet rate limit unavailable"
                        )
                    if commit:
                        return
                elif commit:
                    entries[matching_index] = {
                        **entries[matching_index],
                        "state": "committed",
                    }
                else:
                    entries.pop(matching_index)
                if entries == stored.record["entries"]:
                    return
                try:
                    await self._store.replace_wallet_rate_limit(
                        reservation.wallet_digest,
                        stored,
                        {"version": 1, "entries": entries},
                    )
                except JobConflict:
                    if _attempt + 1 < self._max_attempts:
                        await self._backoff(_attempt + 1)
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if not commit:
                        raise
                    confirmation = await self._read_commit_state(
                        reservation
                    )
                    if confirmation in {"committed", "expired"}:
                        return
                    continue
                return
            except JobConflict:
                if _attempt + 1 < self._max_attempts:
                    await self._backoff(_attempt + 1)
                continue
            except WalletRateLimitUnavailable:
                raise
            except Exception as exc:
                raise WalletRateLimitUnavailable(
                    "wallet rate limit unavailable"
                ) from exc
        raise WalletRateLimitUnavailable("wallet rate limit unavailable")

    @staticmethod
    def _reservation_is_active(
        reservation: WalletRateReservation,
        now: int,
    ) -> bool:
        return reservation.reserved_at > now - _WINDOW_MILLISECONDS

    async def _read_commit_state(
        self,
        reservation: WalletRateReservation,
    ) -> Literal["reserved", "committed", "expired"]:
        now = self._now()
        if not self._reservation_is_active(reservation, now):
            return "expired"
        try:
            stored = await self._store.read_wallet_rate_limit(
                reservation.wallet_digest
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise WalletRateLimitUnavailable(
                "wallet rate limit unavailable"
            ) from exc
        if stored is None:
            raise WalletRateLimitUnavailable("wallet rate limit unavailable")
        matching = next(
            (
                entry
                for entry in self._active_entries(stored, now)
                if entry["reservationId"] == reservation.reservation_id
                and entry["reservedAt"] == reservation.reserved_at
            ),
            None,
        )
        if matching is None:
            raise WalletRateLimitUnavailable("wallet rate limit unavailable")
        state = matching["state"]
        if state not in {"reserved", "committed"}:
            raise WalletRateLimitUnavailable("wallet rate limit unavailable")
        return state
