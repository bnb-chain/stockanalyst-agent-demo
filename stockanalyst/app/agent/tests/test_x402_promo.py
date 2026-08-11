from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from x402_promo import (
    PromoRateLimitExceeded,
    PromoRateLimiter,
    promo_free_mode,
)


class PromoFreeModeTests(unittest.TestCase):
    def test_flag_accepts_only_zero_one_or_absent(self) -> None:
        self.assertFalse(promo_free_mode({}))
        self.assertFalse(promo_free_mode({"X402_PROMO_FREE_MODE": "0"}))
        self.assertTrue(promo_free_mode({"X402_PROMO_FREE_MODE": "1"}))
        for value in ("", "true", "yes", "2", " 1 "):
            with self.subTest(value=value), self.assertRaisesRegex(
                RuntimeError, "X402_PROMO_FREE_MODE"
            ):
                promo_free_mode({"X402_PROMO_FREE_MODE": value})


class PromoRateLimiterTests(unittest.TestCase):
    def test_thirtieth_succeeds_and_thirty_first_reports_retry_after(
        self,
    ) -> None:
        now = [1_000.0]
        limiter = PromoRateLimiter(clock=lambda: now[0])
        for _ in range(30):
            limiter.reserve("198.51.100.8").commit()
        with self.assertRaises(PromoRateLimitExceeded) as raised:
            limiter.reserve("198.51.100.8")
        self.assertEqual(raised.exception.retry_after_seconds, 86_400)
        now[0] += 86_400
        limiter.reserve("198.51.100.8").commit()

    def test_rollback_removes_only_its_own_reservation(self) -> None:
        limiter = PromoRateLimiter(limit=2, clock=lambda: 1_000.0)
        first = limiter.reserve("198.51.100.8")
        second = limiter.reserve("198.51.100.8")

        first.rollback()
        replacement = limiter.reserve("198.51.100.8")
        with self.assertRaises(PromoRateLimitExceeded):
            limiter.reserve("198.51.100.8")

        second.commit()
        replacement.commit()
        first.rollback()
        with self.assertRaises(PromoRateLimitExceeded):
            limiter.reserve("198.51.100.8")

    def test_commit_is_idempotent_and_prevents_later_rollback(self) -> None:
        limiter = PromoRateLimiter(limit=1, clock=lambda: 1_000.0)
        reservation = limiter.reserve("198.51.100.8")

        reservation.commit()
        reservation.commit()
        reservation.rollback()

        with self.assertRaises(PromoRateLimitExceeded):
            limiter.reserve("198.51.100.8")

    def test_separate_ips_have_separate_limits(self) -> None:
        limiter = PromoRateLimiter(limit=1, clock=lambda: 1_000.0)
        limiter.reserve("198.51.100.8").commit()

        limiter.reserve("198.51.100.9").commit()

    def test_equivalent_ipv6_addresses_share_a_limit(self) -> None:
        limiter = PromoRateLimiter(limit=1, clock=lambda: 1_000.0)
        limiter.reserve("2001:0db8:0000:0000:0000:0000:0000:0042").commit()

        with self.assertRaises(PromoRateLimitExceeded):
            limiter.reserve("2001:db8::42")

    def test_raw_ip_is_not_stored(self) -> None:
        source_ip = "198.51.100.8"
        limiter = PromoRateLimiter(clock=lambda: 1_000.0, salt=b"s" * 32)

        limiter.reserve(source_ip).commit()

        self.assertNotIn(source_ip, repr(limiter.__dict__))
        self.assertEqual(len(limiter._events), 1)
        self.assertTrue(all(isinstance(key, bytes) for key in limiter._events))

    def test_new_ip_prunes_expired_buckets_for_all_other_ips(self) -> None:
        now = [1_000.0]
        limiter = PromoRateLimiter(
            limit=1,
            window_seconds=10,
            clock=lambda: now[0],
        )
        for suffix in range(1, 65):
            limiter.reserve(f"198.51.100.{suffix}").commit()

        self.assertEqual(len(limiter._events), 64)
        self.assertEqual(len(limiter._expirations), 64)
        self.assertEqual(len(limiter._expiration_positions), 64)

        now[0] += 10
        limiter.reserve("203.0.113.1").commit()

        self.assertEqual(len(limiter._events), 1)
        self.assertEqual(len(limiter._expirations), 1)
        self.assertEqual(len(limiter._expiration_positions), 1)

    def test_rollback_removes_expiration_index_entries_without_tombstones(
        self,
    ) -> None:
        limiter = PromoRateLimiter(limit=1, clock=lambda: 1_000.0)

        for suffix in range(1, 129):
            limiter.reserve(f"2001:db8::{suffix}").rollback()

        self.assertEqual(limiter._events, {})
        self.assertEqual(limiter._expirations, [])
        self.assertEqual(limiter._expiration_positions, {})

    def test_global_capacity_fails_closed_without_growing_structures(
        self,
    ) -> None:
        limiter = PromoRateLimiter(
            limit=30,
            max_reservations=2,
            clock=lambda: 1_000.0,
        )
        limiter.reserve("198.51.100.8").commit()
        limiter.reserve("198.51.100.9").commit()

        with self.assertRaises(PromoRateLimitExceeded) as raised:
            limiter.reserve("198.51.100.10")

        self.assertEqual(raised.exception.retry_after_seconds, 86_400)
        self.assertEqual(sum(map(len, limiter._events.values())), 2)
        self.assertEqual(len(limiter._expirations), 2)
        self.assertEqual(len(limiter._expiration_positions), 2)

    def test_pending_reservation_expires_at_window_boundary(self) -> None:
        now = [100.0]
        limiter = PromoRateLimiter(
            limit=1,
            window_seconds=10,
            clock=lambda: now[0],
        )
        pending = limiter.reserve("198.51.100.8")

        now[0] = 109.999
        trigger = limiter.reserve("198.51.100.9")
        trigger.rollback()
        self.assertEqual(len(limiter._events), 1)
        with self.assertRaises(PromoRateLimitExceeded):
            limiter.reserve("198.51.100.8")

        now[0] = 110.0
        trigger = limiter.reserve("198.51.100.10")
        trigger.rollback()
        self.assertEqual(limiter._events, {})
        self.assertEqual(limiter._expirations, [])
        self.assertEqual(limiter._expiration_positions, {})

        pending.commit()
        self.assertEqual(limiter._events, {})
        limiter.reserve("198.51.100.8").commit()

    def test_exactly_thirty_of_thirty_one_parallel_reservations_succeed(
        self,
    ) -> None:
        limiter = PromoRateLimiter(clock=lambda: 1_000.0)
        barrier = threading.Barrier(31)

        def reserve() -> bool:
            barrier.wait()
            try:
                limiter.reserve("198.51.100.8").commit()
            except PromoRateLimitExceeded:
                return False
            return True

        with ThreadPoolExecutor(max_workers=31) as executor:
            results = list(executor.map(lambda _index: reserve(), range(31)))

        self.assertEqual(results.count(True), 30)
        self.assertEqual(results.count(False), 1)


if __name__ == "__main__":
    unittest.main()
