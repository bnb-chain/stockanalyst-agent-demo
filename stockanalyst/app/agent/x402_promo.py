from __future__ import annotations

import hmac
import math
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from ipaddress import ip_address


def promo_free_mode(env: Mapping[str, str]) -> bool:
    value = env.get("X402_PROMO_FREE_MODE")
    if value is None or value == "0":
        return False
    if value == "1":
        return True
    raise RuntimeError("X402_PROMO_FREE_MODE must be absent, 0, or 1")


class PromoRateLimitExceeded(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("promo_rate_limited")
        self.retry_after_seconds = retry_after_seconds


class PromoReservation:
    def __init__(
        self,
        limiter: PromoRateLimiter,
        key: bytes,
        reservation_id: str,
    ) -> None:
        self._limiter = limiter
        self._key = key
        self._reservation_id = reservation_id
        self._state = "pending"

    def commit(self) -> None:
        self._limiter._commit(self)

    def rollback(self) -> None:
        self._limiter._rollback(self)


class PromoRateLimiter:
    def __init__(
        self,
        *,
        limit: int = 30,
        window_seconds: int = 86_400,
        clock: Callable[[], float] = time.time,
        salt: bytes | None = None,
        max_reservations: int = 100_000,
    ) -> None:
        if max_reservations < 1:
            raise ValueError("max_reservations must be positive")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._salt = secrets.token_bytes(32) if salt is None else salt
        self._max_reservations = max_reservations
        self._lock = threading.Lock()
        self._events: dict[bytes, dict[str, float]] = {}
        self._expirations: list[tuple[float, str, bytes]] = []
        self._expiration_positions: dict[str, int] = {}

    def reserve(self, source_ip: str) -> PromoReservation:
        normalized = ip_address(source_ip).compressed
        key = hmac.digest(
            self._salt,
            normalized.encode("ascii"),
            "sha256",
        )
        now = self._clock()
        with self._lock:
            self._prune_expired(now)
            active = self._events.get(key, {})
            if len(active) >= self._limit:
                retry = max(
                    1,
                    math.ceil(min(active.values()) + self._window - now),
                )
                raise PromoRateLimitExceeded(retry)
            if len(self._expirations) >= self._max_reservations:
                retry = max(
                    1,
                    math.ceil(self._expirations[0][0] - now),
                )
                raise PromoRateLimitExceeded(retry)
            reservation_id = secrets.token_hex(16)
            while reservation_id in self._expiration_positions:
                reservation_id = secrets.token_hex(16)
            active[reservation_id] = now
            self._events[key] = active
            self._push_expiration(
                (now + self._window, reservation_id, key)
            )
        return PromoReservation(self, key, reservation_id)

    def _swap_expirations(self, left: int, right: int) -> None:
        heap = self._expirations
        heap[left], heap[right] = heap[right], heap[left]
        self._expiration_positions[heap[left][1]] = left
        self._expiration_positions[heap[right][1]] = right

    def _sift_expiration_up(self, index: int) -> None:
        while index:
            parent = (index - 1) // 2
            if self._expirations[parent] <= self._expirations[index]:
                return
            self._swap_expirations(parent, index)
            index = parent

    def _sift_expiration_down(self, index: int) -> None:
        size = len(self._expirations)
        while True:
            left = index * 2 + 1
            if left >= size:
                return
            right = left + 1
            smallest = (
                right
                if right < size
                and self._expirations[right] < self._expirations[left]
                else left
            )
            if self._expirations[index] <= self._expirations[smallest]:
                return
            self._swap_expirations(index, smallest)
            index = smallest

    def _push_expiration(self, entry: tuple[float, str, bytes]) -> None:
        index = len(self._expirations)
        self._expirations.append(entry)
        self._expiration_positions[entry[1]] = index
        self._sift_expiration_up(index)

    def _remove_expiration(
        self,
        reservation_id: str,
    ) -> tuple[float, str, bytes] | None:
        index = self._expiration_positions.pop(reservation_id, None)
        if index is None:
            return None
        removed = self._expirations[index]
        last = self._expirations.pop()
        if index < len(self._expirations):
            self._expirations[index] = last
            self._expiration_positions[last[1]] = index
            parent = (index - 1) // 2
            if index and self._expirations[index] < self._expirations[parent]:
                self._sift_expiration_up(index)
            else:
                self._sift_expiration_down(index)
        return removed

    def _prune_expired(self, now: float) -> None:
        while self._expirations and self._expirations[0][0] <= now:
            entry = self._remove_expiration(self._expirations[0][1])
            if entry is None:
                raise AssertionError("expiration index is inconsistent")
            _expires_at, reservation_id, key = entry
            active = self._events.get(key)
            if active is None:
                continue
            active.pop(reservation_id, None)
            if not active:
                self._events.pop(key, None)

    def _commit(self, reservation: PromoReservation) -> None:
        with self._lock:
            if reservation._state == "pending":
                reservation._state = "committed"

    def _rollback(self, reservation: PromoReservation) -> None:
        with self._lock:
            if reservation._state != "pending":
                return
            active = self._events.get(reservation._key)
            if active is not None:
                active.pop(reservation._reservation_id, None)
            if active is not None and not active:
                self._events.pop(reservation._key, None)
            self._remove_expiration(reservation._reservation_id)
            reservation._state = "rolled_back"
