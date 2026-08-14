from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import threading
import unittest
from typing import Any

from botocore.exceptions import ClientError

from x402_job_store import X402JobStore
from x402_wallet_rate_limit import (
    WalletRateLimiter,
    WalletRateLimitExceeded,
    WalletRateLimitUnavailable,
)

NOW = 1_700_000_000_000
WINDOW_MILLISECONDS = 3_600_000
SECRET = b"s" * 32
WALLET = "0xAbCdEf0123456789"


def s3_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}},
        "S3Operation",
    )


class ThreadSafeConditionalS3:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.put_errors: list[ClientError] = []
        self.get_errors: list[ClientError] = []
        self._next_etag = 1
        self._lock = threading.Lock()

    def put_object(self, **kwargs: Any) -> dict[str, str]:
        with self._lock:
            self.put_calls.append(kwargs)
            if self.put_errors:
                raise self.put_errors.pop(0)
            key = kwargs["Key"]
            existing = self.objects.get(key)
            if kwargs.get("IfNoneMatch") == "*" and existing is not None:
                raise s3_error("PreconditionFailed")
            if "IfMatch" in kwargs and (
                existing is None or kwargs["IfMatch"] != existing[1]
            ):
                raise s3_error("PreconditionFailed")
            etag = f'"etag-{self._next_etag}"'
            self._next_etag += 1
            self.objects[key] = (kwargs["Body"], etag)
            return {"ETag": etag}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            if self.get_errors:
                raise self.get_errors.pop(0)
            try:
                body, etag = self.objects[kwargs["Key"]]
            except KeyError as exc:
                raise s3_error("NoSuchKey") from exc
            return {"Body": io.BytesIO(body), "ETag": etag}


def make_store(s3: ThreadSafeConditionalS3) -> X402JobStore:
    return X402JobStore.from_env(
        {
            "X402_JOB_S3_BUCKET": "private-jobs",
            "X402_JOB_S3_PREFIX": "tenant/jobs",
        },
        s3_client=s3,
    )


def reservation_id(number: int) -> str:
    return f"x402_{number:032x}"


def only_record(s3: ThreadSafeConditionalS3) -> dict[str, Any]:
    assert len(s3.objects) == 1
    body, _etag = next(iter(s3.objects.values()))
    return json.loads(body)


class MutableClock:
    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class WalletRateLimiterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.s3 = ThreadSafeConditionalS3()
        self.clock = MutableClock()
        self.limiter = WalletRateLimiter(
            store=make_store(self.s3),
            token_secret=SECRET,
            clock=self.clock,
        )

    async def test_wallet_digest_is_domain_separated_normalized_and_keyed(
        self,
    ) -> None:
        first = await self.limiter.reserve(WALLET, reservation_id(1))
        retry = await self.limiter.reserve(WALLET.lower(), reservation_id(1))
        expected = hmac.new(
            SECRET,
            b"x402-wallet-rate-limit:v1:" + WALLET.lower().encode(),
            hashlib.sha256,
        ).hexdigest()

        other_s3 = ThreadSafeConditionalS3()
        other = WalletRateLimiter(
            store=make_store(other_s3),
            token_secret=b"t" * 32,
            clock=self.clock,
        )
        keyed = await other.reserve(WALLET, reservation_id(1))

        self.assertEqual(first.wallet_digest, expected)
        self.assertEqual(retry.wallet_digest, expected)
        self.assertEqual(first, retry)
        self.assertNotEqual(keyed.wallet_digest, expected)

    async def test_rate_object_contains_only_bounded_non_secret_fields(self) -> None:
        reservation = await self.limiter.reserve(WALLET, reservation_id(1))
        record = only_record(self.s3)
        serialized = json.dumps(record, sort_keys=True)

        self.assertEqual(
            record,
            {
                "version": 1,
                "entries": [
                    {
                        "reservationId": reservation_id(1),
                        "reservedAt": NOW,
                        "state": "reserved",
                    }
                ],
            },
        )
        self.assertNotIn(WALLET, serialized)
        self.assertNotIn(WALLET.lower(), serialized)
        self.assertNotIn(reservation.wallet_digest, serialized)
        for forbidden in (
            "wallet",
            "ip",
            "proof",
            "signature",
            "nonce",
            "jobToken",
        ):
            self.assertNotIn(forbidden, serialized)

    async def test_thirty_reservations_fill_one_rolling_wallet_window(self) -> None:
        await self.limiter.reserve(WALLET, reservation_id(1))
        self.clock.now += 1_001
        for number in range(2, 31):
            await self.limiter.reserve(WALLET, reservation_id(number))

        with self.assertRaises(WalletRateLimitExceeded) as raised:
            await self.limiter.reserve(WALLET, reservation_id(31))

        self.assertEqual(raised.exception.retry_after_seconds, 3_599)
        self.assertEqual(len(only_record(self.s3)["entries"]), 30)

        other_wallet = await self.limiter.reserve(
            "0x0000000000000000000000000000000000000002",
            reservation_id(31),
        )
        self.assertEqual(other_wallet.state, "reserved")

    async def test_retry_after_has_a_minimum_one_second(self) -> None:
        for number in range(1, 31):
            await self.limiter.reserve(WALLET, reservation_id(number))
        self.clock.now = NOW + WINDOW_MILLISECONDS - 1

        with self.assertRaises(WalletRateLimitExceeded) as raised:
            await self.limiter.reserve(WALLET, reservation_id(31))

        self.assertEqual(raised.exception.retry_after_seconds, 1)

    async def test_exact_window_boundary_is_expired_and_pruned(self) -> None:
        await self.limiter.reserve(WALLET, reservation_id(1))
        self.clock.now = NOW + WINDOW_MILLISECONDS

        current = await self.limiter.reserve(WALLET, reservation_id(2))

        self.assertEqual(current.reserved_at, self.clock.now)
        self.assertEqual(
            [entry["reservationId"] for entry in only_record(self.s3)["entries"]],
            [reservation_id(2)],
        )

    async def test_same_reservation_is_idempotent_without_another_entry(self) -> None:
        first = await self.limiter.reserve(WALLET, reservation_id(1))
        self.clock.now += 10_000

        second = await self.limiter.reserve(WALLET, reservation_id(1))

        self.assertEqual(second, first)
        self.assertEqual(len(only_record(self.s3)["entries"]), 1)

    async def test_commit_changes_only_matching_entry_and_is_idempotent(self) -> None:
        first = await self.limiter.reserve(WALLET, reservation_id(1))
        await self.limiter.reserve(WALLET, reservation_id(2))

        await self.limiter.commit(first)
        await self.limiter.commit(first)

        entries = only_record(self.s3)["entries"]
        self.assertEqual(entries[0]["state"], "committed")
        self.assertEqual(entries[1]["state"], "reserved")

    async def test_release_removes_only_matching_entry_and_is_idempotent(self) -> None:
        first = await self.limiter.reserve(WALLET, reservation_id(1))
        second = await self.limiter.reserve(WALLET, reservation_id(2))

        await self.limiter.release(first)
        await self.limiter.release(first)

        entries = only_record(self.s3)["entries"]
        self.assertEqual(
            [entry["reservationId"] for entry in entries],
            [second.reservation_id],
        )

    async def test_two_instances_enforce_one_combined_limit(self) -> None:
        store = make_store(self.s3)
        first = WalletRateLimiter(
            store=store,
            token_secret=SECRET,
            clock=self.clock,
        )
        restarted = WalletRateLimiter(
            store=store,
            token_secret=SECRET,
            clock=self.clock,
        )
        for number in range(1, 31):
            limiter = first if number % 2 else restarted
            await limiter.reserve(WALLET, reservation_id(number))

        with self.assertRaises(WalletRateLimitExceeded):
            await restarted.reserve(WALLET, reservation_id(31))

    async def test_cas_retries_are_bounded_and_fail_closed(self) -> None:
        self.s3.put_errors = [s3_error("409")] * 6
        limiter = WalletRateLimiter(
            store=make_store(self.s3),
            token_secret=SECRET,
            clock=self.clock,
            max_attempts=3,
        )

        with self.assertRaises(WalletRateLimitUnavailable):
            await limiter.reserve(WALLET, reservation_id(1))

        self.assertEqual(len(self.s3.put_calls), 6)

    async def test_s3_read_uncertainty_fails_closed(self) -> None:
        self.s3.get_errors = [s3_error("ServiceUnavailable")]

        with self.assertRaises(WalletRateLimitUnavailable):
            await self.limiter.reserve(WALLET, reservation_id(1))

        self.assertEqual(self.s3.put_calls, [])

    async def test_concurrent_thirty_one_way_admission_accepts_exactly_thirty(
        self,
    ) -> None:
        async def admit(number: int) -> str:
            try:
                await self.limiter.reserve(WALLET, reservation_id(number))
            except WalletRateLimitExceeded:
                return "limited"
            except WalletRateLimitUnavailable:
                return "unavailable"
            return "reserved"

        results = await asyncio.gather(
            *(admit(number) for number in range(1, 32))
        )

        self.assertEqual(results.count("reserved"), 30)
        self.assertEqual(results.count("limited"), 1)
        self.assertEqual(results.count("unavailable"), 0)
        self.assertEqual(len(only_record(self.s3)["entries"]), 30)


if __name__ == "__main__":
    unittest.main()
