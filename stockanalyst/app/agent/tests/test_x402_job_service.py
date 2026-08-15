from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
import unittest
from dataclasses import replace
from typing import Any
from unittest.mock import ANY, AsyncMock, Mock, patch

import httpx

from tests.test_x402_permit2 import encoded_proof, permit2_proof
from tests.test_x402_verify import NOW as SIGNED_NOW
from tests.test_x402_verify import signed_proof
from x402_handler import _settle_generic
from x402_job_service import (
    CreateJobResult,
    JobIdentityCollision,
    SettlementIndeterminate,
    X402JobError,
    X402JobService,
    load_job_token_secret,
)
from x402_job_store import JobConflict, StoredJob, StoredWalletRateLimit
from x402_settlement import SettlementOutcome
from x402_tokens import U_TOKEN, USD1_TOKEN, USDC_TOKEN, USDT_TOKEN
from x402_verify import (
    CHAIN_ID,
    VerifiedPayment,
    validate_legacy_paid_payment_proof,
    validate_payment_proof,
)
from x402_wallet_rate_limit import (
    WalletRateLimiter,
    WalletRateLimitExceeded,
    WalletRateLimitUnavailable,
    WalletRateReservation,
)

ADDRESS = "0x1111111111111111111111111111111111111111"
NONCE = "0x" + "22" * 32
PROOF = "signed-proof"
REQUEST = {"symbols": "bnb, btc-usd"}
NOW = 2_000_000_000_000
STALE_TIME = NOW + 120_001
TOKEN_SECRET = b"test-only-token-secret-with-32-bytes"


def expected_competition_event_id(token=U_TOKEN) -> str:
    material = (
        f"{ADDRESS.lower()}:{NONCE.lower()}:{token.address.lower()}"
    ).encode("ascii")
    return f"b402:{CHAIN_ID}:{hashlib.sha256(material).hexdigest()}"


def verified_payment(
    *,
    valid_before: int = NOW // 1000 + 600,
    token=U_TOKEN,
) -> VerifiedPayment:
    return VerifiedPayment(
        proof={},
        from_address=ADDRESS,
        to_address="0x2222222222222222222222222222222222222222",
        value=210_000_000_000_000_000,
        valid_after=NOW // 1000 - 60,
        valid_before=valid_before,
        nonce=NONCE,
        nonce_bytes=bytes.fromhex(NONCE.removeprefix("0x")),
        asset=token.address,
        token_symbol=token.symbol,
        transfer_method=token.transfer_method,
    )


class MemoryJobStore:
    def __init__(self, *, synchronize_creates: bool = False) -> None:
        self.jobs: dict[str, StoredJob] = {}
        self.create_calls = 0
        self.replace_calls = 0
        self.presign_calls = 0
        self.reports: dict[tuple[str, str | None], str] = {}
        self.accounting_markers: dict[str, dict[str, Any]] = {}
        self.wallet_rate_limits: dict[str, StoredWalletRateLimit] = {}
        self.events: list[str] = []
        self._etag = 0
        self._create_barrier = (
            asyncio.Barrier(2) if synchronize_creates else None
        )

    def _next_etag(self) -> str:
        self._etag += 1
        return f'"etag-{self._etag}"'

    async def create(self, record: dict[str, Any]) -> StoredJob | None:
        self.create_calls += 1
        if self._create_barrier is not None and self.create_calls <= 2:
            await self._create_barrier.wait()
        job_id = str(record["jobId"])
        if job_id in self.jobs:
            return None
        stored = StoredJob(record=dict(record), etag=self._next_etag())
        self.jobs[job_id] = stored
        return stored

    async def read(self, job_id: str) -> StoredJob | None:
        return self.jobs.get(job_id)

    async def replace(
        self,
        stored: StoredJob,
        record: dict[str, Any],
    ) -> StoredJob:
        self.replace_calls += 1
        current = self.jobs.get(str(record["jobId"]))
        if current is None or current.etag != stored.etag:
            raise JobConflict("job changed concurrently")
        updated = StoredJob(record=dict(record), etag=self._next_etag())
        self.jobs[str(record["jobId"])] = updated
        self.events.append(f"replace:{record['status']}")
        return updated

    async def put_report(
        self,
        job_id: str,
        markdown: str,
        *,
        report_id: str | None = None,
    ) -> None:
        self.reports[(job_id, report_id)] = markdown
        self.events.append("put_report")

    async def presign_report(
        self,
        job_id: str,
        *,
        report_id: str | None = None,
    ) -> str:
        self.presign_calls += 1
        return (
            f"https://signed.example/{job_id}/{report_id}/"
            f"{self.presign_calls}"
        )

    async def read_accounting_marker(
        self,
        job_id: str,
    ) -> dict[str, Any] | None:
        marker = self.accounting_markers.get(job_id)
        return dict(marker) if marker is not None else None

    async def create_accounting_marker(
        self,
        job_id: str,
        marker: dict[str, Any],
    ) -> bool:
        if job_id in self.accounting_markers:
            return False
        self.accounting_markers[job_id] = dict(marker)
        return True

    async def read_wallet_rate_limit(
        self,
        wallet_digest: str,
    ) -> StoredWalletRateLimit | None:
        return self.wallet_rate_limits.get(wallet_digest)

    async def create_wallet_rate_limit(
        self,
        wallet_digest: str,
        record: dict[str, Any],
    ) -> StoredWalletRateLimit | None:
        if wallet_digest in self.wallet_rate_limits:
            return None
        stored = StoredWalletRateLimit(
            record={
                "version": record["version"],
                "entries": [dict(entry) for entry in record["entries"]],
            },
            etag=self._next_etag(),
        )
        self.wallet_rate_limits[wallet_digest] = stored
        self.events.append("rate:create")
        return stored

    async def replace_wallet_rate_limit(
        self,
        wallet_digest: str,
        stored: StoredWalletRateLimit,
        record: dict[str, Any],
    ) -> StoredWalletRateLimit:
        current = self.wallet_rate_limits.get(wallet_digest)
        if current is None or current.etag != stored.etag:
            raise JobConflict("wallet rate limit changed concurrently")
        updated = StoredWalletRateLimit(
            record={
                "version": record["version"],
                "entries": [dict(entry) for entry in record["entries"]],
            },
            etag=self._next_etag(),
        )
        self.wallet_rate_limits[wallet_digest] = updated
        state = (
            updated.record["entries"][0]["state"]
            if updated.record["entries"]
            else "released"
        )
        self.events.append(f"rate:{state}")
        return updated

    def report_for_record(self, record: dict[str, Any]) -> str:
        return self.reports[
            (str(record["jobId"]), record.get("reportId"))
        ]


class ReconciliationRaceStore(MemoryJobStore):
    def __init__(self) -> None:
        super().__init__()
        self._claim_barrier = asyncio.Barrier(2)
        self._claim_calls = 0

    async def replace(
        self,
        stored: StoredJob,
        record: dict[str, Any],
    ) -> StoredJob:
        if (
            record.get("status") == "settling"
            and record.get("leaseOwner") in {"owner-a", "owner-b"}
        ):
            self._claim_calls += 1
            if self._claim_calls <= 2:
                await self._claim_barrier.wait()
        return await super().replace(stored, record)


class PendingPersistenceStore(MemoryJobStore):
    def __init__(
        self,
        *,
        actions: list[str],
        concurrent_update: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.actions = list(actions)
        self.concurrent_update = dict(concurrent_update or {})
        self.pending_replace_calls = 0

    async def replace(
        self,
        stored: StoredJob,
        record: dict[str, Any],
    ) -> StoredJob:
        is_pending_write = (
            record.get("pendingSettlementReference") == "0xpending"
            and stored.record.get("pendingSettlementReference") is None
        )
        if is_pending_write:
            self.pending_replace_calls += 1
            action = self.actions.pop(0) if self.actions else "success"
            if action == "conflict":
                raise JobConflict("pending marker changed concurrently")
            if action == "apply-conflict":
                await super().replace(stored, record)
                raise JobConflict("same pending marker won concurrently")
            if action == "state-conflict":
                current = self.jobs[str(record["jobId"])]
                self.jobs[str(record["jobId"])] = StoredJob(
                    record={**current.record, **self.concurrent_update},
                    etag=self._next_etag(),
                )
                raise JobConflict("pending state changed concurrently")
            if action == "transient":
                raise RuntimeError("transient pending marker write failure")
        return await super().replace(stored, record)


class SettledTransitionFailureStore(MemoryJobStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_settled_transition = True

    async def replace(
        self,
        stored: StoredJob,
        record: dict[str, Any],
    ) -> StoredJob:
        if (
            self.fail_settled_transition
            and stored.record.get("paymentStatus") == "settling"
            and record.get("paymentStatus") == "settled"
        ):
            self.fail_settled_transition = False
            raise RuntimeError("simulated settled transition loss")
        return await super().replace(stored, record)


class AccountingMarkerFailureStore(MemoryJobStore):
    async def create_accounting_marker(
        self,
        job_id: str,
        marker: dict[str, Any],
    ) -> bool:
        raise RuntimeError("sensitive-marker-storage-failure")


class InFlightHeartbeatStore(MemoryJobStore):
    def __init__(self) -> None:
        super().__init__()
        self.heartbeat_written = asyncio.Event()
        self.release_heartbeat = asyncio.Event()
        self.heartbeat_returned = asyncio.Event()

    async def replace(
        self,
        stored: StoredJob,
        record: dict[str, Any],
    ) -> StoredJob:
        updated = await super().replace(stored, record)
        if (
            stored.record.get("status") == "running"
            and record.get("status") == "running"
        ):
            self.heartbeat_written.set()
            await self.release_heartbeat.wait()
            self.heartbeat_returned.set()
        return updated


class OvertakingReportStore(MemoryJobStore):
    def __init__(self) -> None:
        super().__init__()
        self.first_upload_started = asyncio.Event()
        self.release_first_upload = asyncio.Event()
        self._upload_count = 0

    async def put_report(
        self,
        job_id: str,
        markdown: str,
        *,
        report_id: str | None = None,
    ) -> None:
        self._upload_count += 1
        if self._upload_count == 1:
            self.first_upload_started.set()
            await self.release_first_upload.wait()
        await super().put_report(
            job_id,
            markdown,
            report_id=report_id,
        )


class TransientReadStore(MemoryJobStore):
    def __init__(self) -> None:
        super().__init__()
        self.read_calls = 0

    async def read(self, job_id: str) -> StoredJob | None:
        self.read_calls += 1
        if self.read_calls == 1:
            raise RuntimeError("transient-read-failure")
        return await super().read(job_id)


class TransientClaimStore(MemoryJobStore):
    def __init__(self) -> None:
        super().__init__()
        self.claim_calls = 0

    async def replace(
        self,
        stored: StoredJob,
        record: dict[str, Any],
    ) -> StoredJob:
        if (
            stored.record.get("status") == "queued"
            and record.get("status") == "running"
        ):
            self.claim_calls += 1
            if self.claim_calls == 1:
                raise JobConflict("transient-conditional-write-failure")
        return await super().replace(stored, record)


class AppliedResumeStore(MemoryJobStore):
    def __init__(self) -> None:
        super().__init__()
        self.resume_applied = asyncio.Event()
        self.release_resume = asyncio.Event()

    async def replace(
        self,
        stored: StoredJob,
        record: dict[str, Any],
    ) -> StoredJob:
        updated = await super().replace(stored, record)
        if (
            stored.record.get("status") == "running"
            and record.get("status") == "queued"
        ):
            self.resume_applied.set()
            await self.release_resume.wait()
        return updated


class ReadTrackingStore(MemoryJobStore):
    def __init__(self) -> None:
        super().__init__()
        self.read_calls = 0

    async def read(self, job_id: str) -> StoredJob | None:
        self.read_calls += 1
        return await super().read(job_id)


class AppliedCreateFailureStore(MemoryJobStore):
    def __init__(self, *, fail_reread: bool = False) -> None:
        super().__init__()
        self._fail_create_response = True
        self._fail_reread = fail_reread
        self.read_calls = 0

    async def read(self, job_id: str) -> StoredJob | None:
        self.read_calls += 1
        if self._fail_reread and self.read_calls == 2:
            raise RuntimeError("ambiguous job reread")
        return await super().read(job_id)

    async def create(self, record: dict[str, Any]) -> StoredJob | None:
        stored = await super().create(record)
        if stored is not None and self._fail_create_response:
            self._fail_create_response = False
            raise RuntimeError("create response lost")
        return stored


def make_service(
    *,
    store: MemoryJobStore | None = None,
    settle: AsyncMock | None = None,
    authorization_used: AsyncMock | None = None,
    report: AsyncMock | None = None,
    stream_work: Any = None,
    clock: Any = None,
    owner: str = "test-owner",
    analysis_timeout_seconds: float = 900,
    heartbeat_sleep: Any = asyncio.sleep,
    accounting_sleep: Any = None,
    accounting_retry_attempts: int = 3,
    accept_new_jobs: bool = True,
    rate_limiter: Any = None,
    reservation_observer_sleep: Any = None,
    reservation_observer_attempts: int = 3,
) -> X402JobService:
    async def idle_stream(
        _prompt: str,
        _session_id: str,
        _symbols: list[str],
    ) -> Any:
        await asyncio.Event().wait()
        if False:
            yield "", {}

    async def immediate_accounting_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    selected_store = store or MemoryJobStore()
    return X402JobService(
        store=selected_store,
        token_secret=TOKEN_SECRET,
        rate_limiter=rate_limiter
        or WalletRateLimiter(
            store=selected_store,
            token_secret=TOKEN_SECRET,
            clock=clock or (lambda: NOW),
        ),
        settle=settle
        or AsyncMock(
            return_value=SettlementOutcome("settled", transaction="0xtx")
        ),
        authorization_used=authorization_used or AsyncMock(return_value=False),
        report=report or AsyncMock(return_value=True),
        stream_work=stream_work or idle_stream,
        clock=clock or (lambda: NOW),
        owner=owner,
        analysis_timeout_seconds=analysis_timeout_seconds,
        heartbeat_sleep=heartbeat_sleep,
        accounting_sleep=accounting_sleep or immediate_accounting_sleep,
        accounting_retry_attempts=accounting_retry_attempts,
        accept_new_jobs=accept_new_jobs,
        reservation_observer_sleep=(
            reservation_observer_sleep or immediate_accounting_sleep
        ),
        reservation_observer_attempts=reservation_observer_attempts,
    )


async def wait_for_accounting(service: X402JobService) -> None:
    while True:
        with service._tasks_lock:
            tasks = tuple(service._accounting_tasks.values())
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)


async def seed_settling_job(
    service: X402JobService,
    store: MemoryJobStore,
    *,
    lease_expires_at: int,
    payment: VerifiedPayment | None = None,
    proof_header: str | None = None,
    pending_settlement_reference: str | None = None,
) -> StoredJob:
    selected_payment = payment or verified_payment()
    identity = service.derive_identity(selected_payment)
    record = {
        "version": 1,
        "jobId": identity.job_id,
        "paymentKey": identity.payment_key,
        "paymentStatus": "settling",
        "settlementReference": None,
        "address": selected_payment.from_address,
        "status": "settling",
        "request": {
            "symbols": ["BNB"],
            "analysisType": "comprehensive",
            "portfolio": [],
            "riskProfile": {},
        },
        "jobTokenHash": identity.job_token_hash,
        "attempt": 0,
        "leaseOwner": "dead-owner",
        "leaseExpiresAt": lease_expires_at,
        "createdAt": NOW,
        "updatedAt": NOW,
        "expiresAt": NOW + 7 * 24 * 60 * 60 * 1000,
        "errorCode": None,
        "retryable": None,
    }
    if proof_header is not None:
        record["paymentProofDigest"] = hashlib.sha256(
            proof_header.encode("ascii")
        ).hexdigest()
        record["pendingSettlementReference"] = pending_settlement_reference
    stored = await store.create(record)
    assert stored is not None
    return stored


async def seed_execution_job(
    service: X402JobService,
    store: MemoryJobStore,
    *,
    status: str,
    attempt: int = 0,
    retryable: bool | None = None,
    updated_at: int = NOW,
    expires_at: int = NOW + 7 * 24 * 60 * 60 * 1000,
    accounting_reported: bool = True,
) -> CreateJobResult:
    identity = service.derive_identity(verified_payment())
    event_id = f"b402:{CHAIN_ID}:{ADDRESS}:{NONCE}"
    stored = await store.create(
        {
            "version": 1,
            "jobId": identity.job_id,
            "paymentKey": identity.payment_key,
            "paymentStatus": "settled",
            "paymentMode": "paid",
            "asset": U_TOKEN.address.lower(),
            "settlementReference": "0xtx",
            "address": ADDRESS,
            "status": status,
            "request": {
                "symbols": ["BNB"],
                "analysisType": "comprehensive",
                "portfolio": [],
                "riskProfile": {},
            },
            "jobTokenHash": identity.job_token_hash,
            "competitionEventId": event_id,
            "settledAt": NOW,
            "attempt": attempt,
            "leaseOwner": "dead-owner" if status == "running" else None,
            "leaseExpiresAt": (
                updated_at + 120_000 if status == "running" else None
            ),
            "createdAt": NOW,
            "updatedAt": updated_at,
            "expiresAt": expires_at,
            "errorCode": "analysis_failed" if status == "failed" else None,
            "retryable": retryable,
        }
    )
    assert stored is not None
    if accounting_reported:
        created = await store.create_accounting_marker(
            identity.job_id,
            {
                "version": 1,
                "eventId": event_id,
                "settledAt": NOW,
            },
        )
        assert created
    return CreateJobResult(
        job_id=identity.job_id,
        job_token=identity.job_token,
        status=status,
        expires_at=expires_at,
    )


class X402JobIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_payment_derives_same_handle(self) -> None:
        service = make_service()
        payment = verified_payment()

        first = service.derive_identity(payment)
        second = service.derive_identity(payment)

        self.assertEqual(first, second)
        self.assertRegex(first.job_id, r"^x402_[0-9a-f]{32}$")
        self.assertEqual(len(base64.urlsafe_b64decode(first.job_token + "==")), 32)

    async def test_u_identity_keeps_legacy_material(self) -> None:
        service = make_service()
        payment = verified_payment(token=U_TOKEN)
        legacy_material = f"{CHAIN_ID}:{ADDRESS}:{NONCE}".encode()
        expected_key = hashlib.sha256(legacy_material).hexdigest()

        identity = service.derive_identity(payment)

        self.assertEqual(identity.payment_key, expected_key)
        self.assertEqual(identity.job_id, f"x402_{expected_key[:32]}")

    async def test_usd1_identity_is_asset_scoped(self) -> None:
        service = make_service()
        u_payment = verified_payment(token=U_TOKEN)
        usd1_payment = verified_payment(token=USD1_TOKEN)
        equivalent_usd1 = replace(
            usd1_payment,
            asset=USD1_TOKEN.address.upper().replace("0X", "0x"),
        )
        normalized_asset = USD1_TOKEN.address.lower()
        scoped_material = (
            f"asset:{normalized_asset}:{CHAIN_ID}:{ADDRESS}:{NONCE}".encode()
        )
        expected_key = hashlib.sha256(scoped_material).hexdigest()

        u_identity = service.derive_identity(u_payment)
        usd1_identity = service.derive_identity(usd1_payment)

        self.assertNotEqual(usd1_identity, u_identity)
        self.assertEqual(usd1_identity.payment_key, expected_key)
        self.assertEqual(
            service.derive_identity(equivalent_usd1),
            usd1_identity,
        )

    async def test_competition_event_ids_use_one_compact_hash_format(self) -> None:
        service = make_service()

        for token in (U_TOKEN, USD1_TOKEN, USDC_TOKEN, USDT_TOKEN):
            with self.subTest(token=token.symbol):
                payment = verified_payment(token=token)
                material = (
                    f"{ADDRESS.lower()}:{NONCE.lower()}:"
                    f"{token.address.lower()}"
                ).encode("ascii")
                expected = (
                    f"b402:{CHAIN_ID}:"
                    f"{hashlib.sha256(material).hexdigest()}"
                )

                actual = service._competition_event_id(payment)

                self.assertEqual(actual, expected)
                self.assertEqual(service._competition_event_id(payment), actual)
                self.assertEqual(len(actual), 72)
                self.assertNotIn(ADDRESS.lower(), actual)
                self.assertNotIn(NONCE.lower(), actual)
                self.assertNotIn(token.address.lower(), actual)

    async def test_competition_event_id_is_scoped_by_payment_parts(self) -> None:
        service = make_service()
        payment = verified_payment(token=U_TOKEN)
        original = service._competition_event_id(payment)

        different_wallet = replace(payment, from_address="0x" + "33" * 20)
        different_nonce = replace(
            payment,
            nonce="0x" + "44" * 32,
            nonce_bytes=bytes.fromhex("44" * 32),
        )
        different_asset = verified_payment(token=USD1_TOKEN)

        self.assertNotEqual(
            service._competition_event_id(different_wallet), original
        )
        self.assertNotEqual(
            service._competition_event_id(different_nonce), original
        )
        self.assertNotEqual(
            service._competition_event_id(different_asset), original
        )

    async def test_identity_rejects_unknown_asset(self) -> None:
        service = make_service()
        payment = replace(
            verified_payment(),
            asset="0x" + "99" * 20,
            token_symbol="UNKNOWN",
        )

        with self.assertRaisesRegex(X402JobError, "invalid_payment_identity"):
            service.derive_identity(payment)

    async def test_equivalent_payment_representations_derive_same_handle(
        self,
    ) -> None:
        payment = verified_payment()
        mixed_case_address = "0xAbCdEf0123456789aBCDef0123456789abCDef01"
        canonical = replace(
            payment,
            from_address=mixed_case_address.lower(),
            nonce="0x" + payment.nonce_bytes.hex(),
        )
        equivalent = replace(
            payment,
            from_address=mixed_case_address,
            nonce=payment.nonce_bytes.hex().upper(),
        )
        service = make_service()

        self.assertEqual(
            service.derive_identity(canonical),
            service.derive_identity(equivalent),
        )

    async def test_one_initial_reservation_submits_facilitator(self) -> None:
        store = MemoryJobStore()
        settle = AsyncMock(
            return_value=SettlementOutcome("settled", transaction="0xtx")
        )
        service = make_service(store=store, settle=settle)

        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            first, second = await asyncio.gather(
                service.create_job(PROOF, REQUEST),
                service.create_job(PROOF, REQUEST),
            )

        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(first.job_token, second.job_token)
        settle.assert_awaited_once_with(PROOF, "verify-and-settle")
        self.assertEqual(first.status, "queued")
        durable = store.jobs[first.job_id].record
        self.assertEqual(
            durable["paymentProofDigest"],
            hashlib.sha256(PROOF.encode("ascii")).hexdigest(),
        )
        self.assertIsNone(durable["pendingSettlementReference"])

    async def test_successful_settlement_reports_deterministic_event(self) -> None:
        report = AsyncMock(return_value=True)
        service = make_service(report=report)

        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            result = await service.create_job(PROOF, REQUEST)

        await wait_for_accounting(service)
        report.assert_awaited_once_with(
            event_id=expected_competition_event_id(),
            address=ADDRESS,
            called_at=ANY,
        )
        self.assertEqual(result.status, "queued")

    async def test_stored_job_contains_only_the_token_hash(self) -> None:
        store = MemoryJobStore()
        service = make_service(store=store)

        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            result = await service.create_job(PROOF, REQUEST)

        stored = store.jobs[result.job_id].record
        self.assertIn("jobTokenHash", stored)
        self.assertNotIn("jobToken", stored)
        self.assertEqual(
            stored["jobTokenHash"],
            hashlib.sha256(result.job_token.encode()).hexdigest(),
        )

    async def test_full_payment_key_mismatch_raises_identity_collision(self) -> None:
        store = MemoryJobStore()
        service = make_service(store=store)
        payment = verified_payment()
        identity = service.derive_identity(payment)
        store.jobs[identity.job_id] = StoredJob(
            record={
                "jobId": identity.job_id,
                "paymentKey": "f" * 64,
                "status": "queued",
                "expiresAt": NOW + 1,
            },
            etag='"etag-existing"',
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            self.assertRaises(JobIdentityCollision),
        ):
            await service.create_job(PROOF, REQUEST)


class X402JobSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_permit2_pending_settlement_is_bound_and_not_queued(
        self,
    ) -> None:
        store = MemoryJobStore()
        settle = AsyncMock(
            return_value=SettlementOutcome(
                "pending",
                transaction="0xpending",
            )
        )
        authorization_used = AsyncMock()
        stream_work = Mock(side_effect=AssertionError("work must not start"))
        payment = verified_payment(token=USDC_TOKEN)
        service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
            stream_work=stream_work,
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(raised.exception.code, "settlement_pending")
        self.assertIs(raised.exception.retryable, True)
        stored = store.jobs[service.derive_identity(payment).job_id].record
        self.assertEqual(
            {
                key: stored.get(key)
                for key in (
                    "paymentStatus",
                    "paymentProofDigest",
                    "pendingSettlementReference",
                    "settlementReference",
                )
            },
            {
                "paymentStatus": "settling",
                "paymentProofDigest": hashlib.sha256(
                    PROOF.encode("ascii")
                ).hexdigest(),
                "pendingSettlementReference": "0xpending",
                "settlementReference": None,
            },
        )
        settle.assert_awaited_once_with(PROOF, "verify-and-settle")
        authorization_used.assert_not_awaited()
        stream_work.assert_not_called()
        self.assertFalse(service._tasks)

    async def test_malformed_stored_permit2_pending_reference_fails_closed(
        self,
    ) -> None:
        invalid_references: tuple[tuple[str, Any], ...] = (
            ("empty", ""),
            ("boolean", True),
            ("object", {}),
            ("control-character", "0xpending\n"),
            ("overlong", "x" * 4_097),
        )
        for label, invalid_reference in invalid_references:
            with self.subTest(label=label):
                store = MemoryJobStore()
                payment = verified_payment(
                    token=USDC_TOKEN,
                    valid_before=STALE_TIME // 1000,
                )
                settle = AsyncMock(
                    return_value=SettlementOutcome(
                        "settled",
                        transaction="0xmust-not-settle",
                    )
                )
                authorization_used = AsyncMock()
                stream_work = Mock(
                    side_effect=AssertionError("work must not start")
                )
                service = make_service(
                    store=store,
                    settle=settle,
                    authorization_used=authorization_used,
                    stream_work=stream_work,
                    clock=lambda: STALE_TIME,
                )
                await seed_settling_job(
                    service,
                    store,
                    lease_expires_at=NOW,
                    payment=payment,
                    proof_header=PROOF,
                    pending_settlement_reference=invalid_reference,
                )

                caught: Exception | None = None
                with (
                    patch(
                        "x402_job_service.validate_payment_proof",
                        return_value=(payment, ""),
                    ),
                    patch.object(service, "_spawn") as spawn,
                ):
                    try:
                        await service.create_job(PROOF, REQUEST)
                    except Exception as exc:
                        caught = exc

                self.assertEqual(
                    getattr(caught, "code", None),
                    "job_state_unavailable",
                )
                self.assertIs(getattr(caught, "retryable", None), True)
                settle.assert_not_awaited()
                authorization_used.assert_not_awaited()
                stream_work.assert_not_called()
                spawn.assert_not_called()
                self.assertEqual(store.replace_calls, 0)

    async def test_pending_marker_retries_one_conflict_without_resettling(
        self,
    ) -> None:
        store = PendingPersistenceStore(actions=["conflict"])
        settle = AsyncMock(
            return_value=SettlementOutcome(
                "pending",
                transaction="0xpending",
            )
        )
        authorization_used = AsyncMock()
        stream_work = Mock(side_effect=AssertionError("work must not start"))
        payment = verified_payment(token=USDC_TOKEN)
        service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
            stream_work=stream_work,
        )

        caught: Exception | None = None
        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            patch.object(service, "_spawn") as spawn,
        ):
            try:
                await service.create_job(PROOF, REQUEST)
            except Exception as exc:
                caught = exc

        self.assertEqual(getattr(caught, "code", None), "settlement_pending")
        self.assertIs(getattr(caught, "retryable", None), True)
        durable = store.jobs[service.derive_identity(payment).job_id].record
        self.assertEqual(durable["pendingSettlementReference"], "0xpending")
        self.assertEqual(store.pending_replace_calls, 2)
        settle.assert_awaited_once_with(PROOF, "verify-and-settle")
        authorization_used.assert_not_awaited()
        stream_work.assert_not_called()
        spawn.assert_not_called()

    async def test_pending_marker_retries_one_transient_write(
        self,
    ) -> None:
        store = PendingPersistenceStore(actions=["transient"])
        settle = AsyncMock(
            return_value=SettlementOutcome(
                "pending",
                transaction="0xpending",
            )
        )
        payment = verified_payment(token=USDC_TOKEN)
        service = make_service(store=store, settle=settle)

        caught: Exception | None = None
        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            patch.object(service, "_spawn") as spawn,
        ):
            try:
                await service.create_job(PROOF, REQUEST)
            except Exception as exc:
                caught = exc

        self.assertEqual(getattr(caught, "code", None), "settlement_pending")
        self.assertEqual(store.pending_replace_calls, 2)
        durable = store.jobs[service.derive_identity(payment).job_id].record
        self.assertEqual(durable["pendingSettlementReference"], "0xpending")
        settle.assert_awaited_once_with(PROOF, "verify-and-settle")
        spawn.assert_not_called()

    async def test_pending_marker_accepts_same_concurrent_marker(
        self,
    ) -> None:
        store = PendingPersistenceStore(actions=["apply-conflict"])
        settle = AsyncMock(
            return_value=SettlementOutcome(
                "pending",
                transaction="0xpending",
            )
        )
        payment = verified_payment(token=USDC_TOKEN)
        service = make_service(store=store, settle=settle)

        caught: Exception | None = None
        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            patch.object(service, "_spawn") as spawn,
        ):
            try:
                await service.create_job(PROOF, REQUEST)
            except Exception as exc:
                caught = exc

        self.assertEqual(getattr(caught, "code", None), "settlement_pending")
        self.assertEqual(store.pending_replace_calls, 1)
        durable = store.jobs[service.derive_identity(payment).job_id].record
        self.assertEqual(durable["pendingSettlementReference"], "0xpending")
        settle.assert_awaited_once_with(PROOF, "verify-and-settle")
        spawn.assert_not_called()

    async def test_pending_marker_conflicting_state_fails_without_overwrite(
        self,
    ) -> None:
        cases = (
            (
                "different-marker",
                {"pendingSettlementReference": "0xdifferent"},
            ),
            ("malformed-marker", {"pendingSettlementReference": True}),
            (
                "terminal",
                {
                    "status": "failed",
                    "paymentStatus": "failed",
                    "pendingSettlementReference": "0xpending",
                },
            ),
            ("identity-mismatch", {"paymentKey": "different-payment-key"}),
            ("digest-mismatch", {"paymentProofDigest": "00" * 32}),
        )
        for label, concurrent_update in cases:
            with self.subTest(label=label):
                store = PendingPersistenceStore(
                    actions=["state-conflict"],
                    concurrent_update=concurrent_update,
                )
                settle = AsyncMock(
                    return_value=SettlementOutcome(
                        "pending",
                        transaction="0xpending",
                    )
                )
                authorization_used = AsyncMock()
                payment = verified_payment(token=USDC_TOKEN)
                service = make_service(
                    store=store,
                    settle=settle,
                    authorization_used=authorization_used,
                )

                caught: Exception | None = None
                with (
                    patch(
                        "x402_job_service.validate_payment_proof",
                        return_value=(payment, ""),
                    ),
                    patch.object(service, "_spawn") as spawn,
                ):
                    try:
                        await service.create_job(PROOF, REQUEST)
                    except Exception as exc:
                        caught = exc

                self.assertEqual(
                    getattr(caught, "code", None),
                    "job_state_unavailable",
                )
                durable = store.jobs[
                    service.derive_identity(payment).job_id
                ].record
                for key, value in concurrent_update.items():
                    self.assertEqual(durable[key], value)
                self.assertEqual(store.pending_replace_calls, 1)
                settle.assert_awaited_once_with(PROOF, "verify-and-settle")
                authorization_used.assert_not_awaited()
                spawn.assert_not_called()

    async def test_pending_marker_persistent_failure_is_bounded_and_retryable(
        self,
    ) -> None:
        store = PendingPersistenceStore(actions=["transient"] * 10)
        settle = AsyncMock(
            return_value=SettlementOutcome(
                "pending",
                transaction="0xpending",
            )
        )
        authorization_used = AsyncMock()
        stream_work = Mock(side_effect=AssertionError("work must not start"))
        payment = verified_payment(token=USDC_TOKEN)
        service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
            stream_work=stream_work,
        )

        caught: Exception | None = None
        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            patch.object(service, "_spawn") as spawn,
        ):
            try:
                await service.create_job(PROOF, REQUEST)
            except Exception as exc:
                caught = exc

        self.assertEqual(getattr(caught, "code", None), "settlement_pending")
        self.assertIs(getattr(caught, "retryable", None), True)
        self.assertEqual(store.pending_replace_calls, 3)
        durable = store.jobs[service.derive_identity(payment).job_id].record
        self.assertIsNone(durable["pendingSettlementReference"])
        settle.assert_awaited_once_with(PROOF, "verify-and-settle")
        authorization_used.assert_not_awaited()
        stream_work.assert_not_called()
        spawn.assert_not_called()

    async def test_stale_permit2_pending_recovery_is_settle_only(self) -> None:
        store = MemoryJobStore()
        payment = verified_payment(token=USDC_TOKEN)
        settle = AsyncMock(
            return_value=SettlementOutcome(
                "settled",
                transaction="0xsettled",
            )
        )
        authorization_used = AsyncMock()
        service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
            clock=lambda: STALE_TIME,
        )
        await seed_settling_job(
            service,
            store,
            lease_expires_at=NOW,
            payment=payment,
            proof_header=PROOF,
            pending_settlement_reference="0xpending",
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            patch.object(service, "_spawn") as spawn,
        ):
            recovered = await service.create_job(PROOF, REQUEST)

        self.assertEqual(recovered.status, "queued")
        settle.assert_awaited_once_with(PROOF, "settle-only")
        authorization_used.assert_not_awaited()
        durable = store.jobs[recovered.job_id].record
        self.assertEqual(durable["settlementReference"], "0xsettled")
        self.assertIsNone(durable["pendingSettlementReference"])
        spawn.assert_called_once_with(recovered.job_id)

    async def test_permit2_proof_digest_mismatch_rejects_before_recovery(
        self,
    ) -> None:
        store = MemoryJobStore()
        payment = verified_payment(token=USDC_TOKEN)
        settle = AsyncMock(
            return_value=SettlementOutcome(
                "settled",
                transaction="0xsettled",
            )
        )
        authorization_used = AsyncMock(return_value=False)
        service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
            clock=lambda: STALE_TIME,
        )
        await seed_settling_job(
            service,
            store,
            lease_expires_at=NOW,
            payment=payment,
            proof_header=PROOF,
            pending_settlement_reference="0xpending",
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job("different-signed-proof", REQUEST)

        self.assertEqual(raised.exception.code, "payment_rejected")
        settle.assert_not_awaited()
        authorization_used.assert_not_awaited()
        self.assertEqual(store.replace_calls, 0)

    async def test_settled_eip3009_job_rejects_a_different_bound_proof(
        self,
    ) -> None:
        store = MemoryJobStore()
        payment = verified_payment()
        settle = AsyncMock(
            return_value=SettlementOutcome("settled", transaction="0xsettled")
        )
        authorization_used = AsyncMock()
        service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            patch.object(service, "_spawn"),
        ):
            created = await service.create_job(PROOF, REQUEST)
            with self.assertRaises(X402JobError) as raised:
                await service.create_job("different-signed-proof", REQUEST)

        self.assertEqual(created.status, "queued")
        self.assertEqual(raised.exception.code, "payment_rejected")
        settle.assert_awaited_once_with(PROOF, "verify-and-settle")
        authorization_used.assert_not_awaited()

    async def test_permit2_pending_recovery_after_deadline_is_settle_only(
        self,
    ) -> None:
        store = MemoryJobStore()
        payment = verified_payment(
            token=USDC_TOKEN,
            valid_before=STALE_TIME // 1000,
        )
        settle = AsyncMock(
            return_value=SettlementOutcome(
                "settled",
                transaction="0xsettled-after-deadline",
            )
        )
        authorization_used = AsyncMock()
        service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
            clock=lambda: STALE_TIME,
        )
        await seed_settling_job(
            service,
            store,
            lease_expires_at=NOW,
            payment=payment,
            proof_header=PROOF,
            pending_settlement_reference="0xpending",
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            patch.object(service, "_spawn"),
        ):
            recovered = await service.create_job(PROOF, REQUEST)

        self.assertEqual(recovered.status, "queued")
        settle.assert_awaited_once_with(PROOF, "settle-only")
        authorization_used.assert_not_awaited()

    async def test_permit2_response_loss_before_marker_recovers_settle_only(
        self,
    ) -> None:
        store = MemoryJobStore()
        now = [NOW]
        payment = verified_payment(
            token=USDC_TOKEN,
            valid_before=STALE_TIME // 1000,
        )
        settle = AsyncMock(side_effect=[
            ConnectionError("settlement response lost before marker"),
            SettlementOutcome(
                "settled",
                transaction="0xreconciled-without-marker",
            ),
        ])
        authorization_used = AsyncMock()
        stream_work = Mock(side_effect=AssertionError("work must not start"))
        service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
            stream_work=stream_work,
            clock=lambda: now[0],
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            patch.object(service, "_spawn") as spawn,
        ):
            with self.assertRaises(X402JobError) as raised:
                await service.create_job(PROOF, REQUEST)

            self.assertEqual(raised.exception.code, "settlement_pending")
            reservation = store.jobs[service.derive_identity(payment).job_id].record
            self.assertEqual(reservation["paymentStatus"], "settling")
            self.assertIsNone(reservation["pendingSettlementReference"])
            spawn.assert_not_called()
            stream_work.assert_not_called()

            now[0] = STALE_TIME
            recovered = await service.create_job(PROOF, REQUEST)

        self.assertEqual(recovered.status, "queued")
        self.assertEqual(
            [call.args[1] for call in settle.await_args_list],
            ["verify-and-settle", "settle-only"],
        )
        authorization_used.assert_not_awaited()
        spawn.assert_called_once_with(recovered.job_id)

    async def test_permit2_settled_transition_loss_recovers_settle_only(
        self,
    ) -> None:
        store = SettledTransitionFailureStore()
        now = [NOW]
        payment = verified_payment(token=USDC_TOKEN)
        settle = AsyncMock(side_effect=[
            SettlementOutcome("settled", transaction="0xfirst-settlement"),
            SettlementOutcome("settled", transaction="0xreconciled-settlement"),
        ])
        authorization_used = AsyncMock()
        stream_work = Mock(side_effect=AssertionError("work must not start"))
        service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
            stream_work=stream_work,
            clock=lambda: now[0],
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            patch.object(service, "_spawn") as spawn,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "simulated settled transition loss",
            ):
                await service.create_job(PROOF, REQUEST)

            reservation = store.jobs[service.derive_identity(payment).job_id].record
            self.assertEqual(reservation["paymentStatus"], "settling")
            self.assertIsNone(reservation["pendingSettlementReference"])
            spawn.assert_not_called()
            stream_work.assert_not_called()

            now[0] = STALE_TIME
            recovered = await service.create_job(PROOF, REQUEST)

        self.assertEqual(recovered.status, "queued")
        self.assertEqual(
            [call.args[1] for call in settle.await_args_list],
            ["verify-and-settle", "settle-only"],
        )
        authorization_used.assert_not_awaited()
        spawn.assert_called_once_with(recovered.job_id)

    async def test_rejected_permit2_recovery_clears_pending_transaction(
        self,
    ) -> None:
        store = MemoryJobStore()
        payment = verified_payment(token=USDC_TOKEN)
        settle = AsyncMock(
            return_value=SettlementOutcome(
                "rejected",
                reason="transaction rejected",
            )
        )
        authorization_used = AsyncMock()
        service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
            clock=lambda: STALE_TIME,
        )
        stored = await seed_settling_job(
            service,
            store,
            lease_expires_at=NOW,
            payment=payment,
            proof_header=PROOF,
            pending_settlement_reference="0xpending",
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(raised.exception.code, "payment_rejected")
        failed = store.jobs[str(stored.record["jobId"])].record
        self.assertEqual(failed["paymentStatus"], "failed")
        self.assertIsNone(failed["pendingSettlementReference"])
        settle.assert_awaited_once_with(PROOF, "settle-only")
        authorization_used.assert_not_awaited()

    async def test_permit2_recovery_without_proof_digest_fails_closed(
        self,
    ) -> None:
        store = MemoryJobStore()
        payment = verified_payment(token=USDC_TOKEN)
        settle = AsyncMock()
        authorization_used = AsyncMock()
        service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
            clock=lambda: STALE_TIME,
        )
        await seed_settling_job(
            service,
            store,
            lease_expires_at=NOW,
            payment=payment,
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(raised.exception.code, "payment_rejected")
        settle.assert_not_awaited()
        authorization_used.assert_not_awaited()
        self.assertEqual(store.replace_calls, 0)

    async def test_non_ascii_proof_is_rejected_before_reservation(self) -> None:
        store = MemoryJobStore()
        settle = AsyncMock()
        service = make_service(store=store, settle=settle)

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(token=USDC_TOKEN), ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job("signed-prööf", REQUEST)

        self.assertEqual(raised.exception.code, "payment_rejected")
        self.assertEqual(store.create_calls, 0)
        settle.assert_not_awaited()

    async def test_concurrent_permit2_pending_recovery_has_one_settle_owner(
        self,
    ) -> None:
        store = ReconciliationRaceStore()
        payment = verified_payment(token=USDC_TOKEN)
        settle = AsyncMock(
            return_value=SettlementOutcome(
                "settled",
                transaction="0xsettled-once",
            )
        )
        authorization_used = AsyncMock()
        first_service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
            clock=lambda: STALE_TIME,
            owner="owner-a",
        )
        second_service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
            clock=lambda: STALE_TIME,
            owner="owner-b",
        )
        await seed_settling_job(
            first_service,
            store,
            lease_expires_at=NOW,
            payment=payment,
            proof_header=PROOF,
            pending_settlement_reference="0xpending",
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            patch.object(first_service, "_spawn"),
            patch.object(second_service, "_spawn"),
        ):
            first, second = await asyncio.gather(
                first_service.create_job(PROOF, REQUEST),
                second_service.create_job(PROOF, REQUEST),
            )

        self.assertEqual(first.job_id, second.job_id)
        settle.assert_awaited_once_with(PROOF, "settle-only")
        authorization_used.assert_not_awaited()

    async def test_settlement_reference_uses_visible_ascii_byte_boundary(
        self,
    ) -> None:
        accepted = make_service(
            settle=AsyncMock(
                return_value=SettlementOutcome(
                    "settled",
                    transaction="x" * 4_096,
                )
            ),
        )
        self.assertEqual(
            await accepted._settle_payment(PROOF, "verify-and-settle"),
            SettlementOutcome("settled", transaction="x" * 4_096),
        )

        invalid_references = (
            "x" * 4_094 + "\U0001f600",
            "contains space",
            "line\nbreak",
            "delete\x7f",
        )
        for reference in invalid_references:
            malformed = SettlementOutcome("settled", transaction="0xvalid")
            object.__setattr__(malformed, "transaction", reference)
            service = make_service(
                settle=AsyncMock(return_value=malformed),
            )
            with (
                self.subTest(reference=repr(reference)),
                self.assertRaises(SettlementIndeterminate),
            ):
                await service._settle_payment(PROOF, "verify-and-settle")

    async def test_successful_settlement_requires_a_nonempty_reference(
        self,
    ) -> None:
        store = MemoryJobStore()
        malformed = SettlementOutcome("settled", transaction="0xvalid")
        object.__setattr__(malformed, "transaction", "")
        service = make_service(
            store=store,
            settle=AsyncMock(return_value=malformed),
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(SettlementIndeterminate),
        ):
            await service.create_job(PROOF, REQUEST)

        identity = service.derive_identity(verified_payment())
        stored = store.jobs[identity.job_id].record
        self.assertEqual(stored["paymentStatus"], "settling")
        self.assertIsNone(stored["settlementReference"])

    async def test_overlong_settlement_reference_is_not_persisted(self) -> None:
        store = MemoryJobStore()
        malformed = SettlementOutcome("settled", transaction="0xvalid")
        object.__setattr__(malformed, "transaction", "x" * 4_097)
        service = make_service(
            store=store,
            settle=AsyncMock(return_value=malformed),
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(SettlementIndeterminate),
        ):
            await service.create_job(PROOF, REQUEST)

        identity = service.derive_identity(verified_payment())
        stored = store.jobs[identity.job_id].record
        self.assertEqual(stored["paymentStatus"], "settling")
        self.assertIsNone(stored["settlementReference"])

    async def test_overlong_stored_reference_is_not_publicly_projected(
        self,
    ) -> None:
        store = MemoryJobStore()
        service = make_service(store=store)
        stored = await seed_settling_job(
            service,
            store,
            lease_expires_at=NOW,
        )
        corrupted = StoredJob(
            record={
                **stored.record,
                "paymentStatus": "settled",
                "status": "queued",
                "settlementReference": "x" * 4_097,
            },
            etag=stored.etag,
        )
        identity = service.derive_identity(verified_payment())

        with self.assertRaises(X402JobError) as raised:
            service._create_result(identity, corrupted)

        self.assertEqual(raised.exception.code, "job_state_unavailable")

    async def test_indeterminate_settlement_stays_recoverable_after_response_loss(
        self,
    ) -> None:
        store = MemoryJobStore()
        now = [NOW]
        settle = AsyncMock(side_effect=ConnectionError("response lost"))
        authorization_used = AsyncMock(return_value=True)
        service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
            clock=lambda: now[0],
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(raised.exception.code, "settlement_pending")
        identity = service.derive_identity(verified_payment())
        reservation = store.jobs[identity.job_id].record
        self.assertEqual(reservation["status"], "settling")
        self.assertEqual(reservation["paymentStatus"], "settling")
        self.assertNotEqual(reservation.get("errorCode"), "payment_failed")

        now[0] = STALE_TIME
        settle.side_effect = AssertionError("must reconcile on-chain first")
        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            recovered = await service.create_job(PROOF, REQUEST)

        self.assertEqual(recovered.status, "queued")
        authorization_used.assert_awaited_once()
        self.assertEqual(settle.await_count, 1)
        for task in tuple(service._tasks.values()):
            task.cancel()
        await service.wait_for_idle()

    async def test_explicit_facilitator_rejection_persists_payment_failed(
        self,
    ) -> None:
        store = MemoryJobStore()
        stream_work = Mock(side_effect=AssertionError("work must not start"))
        service = make_service(
            store=store,
            settle=AsyncMock(
                return_value=SettlementOutcome(
                    "rejected",
                    reason="authorization rejected",
                )
            ),
            stream_work=stream_work,
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(raised.exception.code, "payment_rejected")
        identity = service.derive_identity(verified_payment())
        failed = store.jobs[identity.job_id].record
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["paymentStatus"], "failed")
        self.assertEqual(failed["errorCode"], "payment_failed")
        stream_work.assert_not_called()
        self.assertFalse(service._tasks)

    async def test_malformed_settlement_result_is_indeterminate(self) -> None:
        store = MemoryJobStore()
        service = make_service(
            store=store,
            settle=AsyncMock(return_value={"success": True}),
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(raised.exception.code, "settlement_pending")
        identity = service.derive_identity(verified_payment())
        self.assertEqual(store.jobs[identity.job_id].record["status"], "settling")

    async def test_unknown_typed_settlement_status_is_indeterminate(self) -> None:
        store = MemoryJobStore()
        malformed = SettlementOutcome("settled", transaction="0xunexpected")
        object.__setattr__(malformed, "status", "unknown")
        service = make_service(
            store=store,
            settle=AsyncMock(return_value=malformed),
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(raised.exception.code, "settlement_pending")
        identity = service.derive_identity(verified_payment())
        self.assertEqual(store.jobs[identity.job_id].record["status"], "settling")

    async def test_fresh_settling_job_returns_without_taking_ownership(self) -> None:
        store = MemoryJobStore()
        settle = AsyncMock()
        authorization_used = AsyncMock()
        service = make_service(
            store=store,
            settle=settle,
            authorization_used=authorization_used,
            clock=lambda: NOW,
        )
        await seed_settling_job(
            service,
            store,
            lease_expires_at=NOW + 1,
        )

        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            result = await service.create_job(PROOF, REQUEST)

        self.assertEqual(result.status, "settling")
        authorization_used.assert_not_awaited()
        settle.assert_not_awaited()
        self.assertEqual(store.replace_calls, 0)

    async def test_stale_settling_used_authorization_becomes_queued(self) -> None:
        store = MemoryJobStore()
        authorization_used = AsyncMock(return_value=True)
        settle = AsyncMock()
        report = AsyncMock(return_value=True)
        service = make_service(
            store=store,
            authorization_used=authorization_used,
            settle=settle,
            report=report,
            clock=lambda: STALE_TIME,
        )
        await seed_settling_job(service, store, lease_expires_at=NOW)

        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            result = await service.create_job(PROOF, REQUEST)

        await wait_for_accounting(service)
        self.assertEqual(result.status, "queued")
        self.assertEqual(
            result.payment_response,
            {
                "success": True,
                "transaction": "",
                "network": "eip155:56",
                "payer": ADDRESS,
            },
        )
        authorization_used.assert_awaited_once()
        settle.assert_not_awaited()
        report.assert_awaited_once()

    async def test_stale_settling_unused_authorization_retries_settlement(
        self,
    ) -> None:
        store = MemoryJobStore()
        authorization_used = AsyncMock(return_value=False)
        settle = AsyncMock(
            return_value=SettlementOutcome(
                "settled",
                transaction="0xreconciled",
            )
        )
        service = make_service(
            store=store,
            authorization_used=authorization_used,
            settle=settle,
            clock=lambda: STALE_TIME,
        )
        await seed_settling_job(service, store, lease_expires_at=NOW)

        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            result = await service.create_job(PROOF, REQUEST)

        self.assertEqual(result.status, "queued")
        authorization_used.assert_awaited_once()
        settle.assert_awaited_once_with(PROOF, "verify-and-settle")
        stored = store.jobs[result.job_id].record
        self.assertEqual(stored["settlementReference"], "0xreconciled")

    async def test_concurrent_stale_claims_allow_one_facilitator_owner(self) -> None:
        store = ReconciliationRaceStore()
        authorization_used = AsyncMock(return_value=False)
        settle = AsyncMock(
            return_value=SettlementOutcome(
                "settled",
                transaction="0xreconciled",
            )
        )
        first_service = make_service(
            store=store,
            authorization_used=authorization_used,
            settle=settle,
            clock=lambda: STALE_TIME,
            owner="owner-a",
        )
        second_service = make_service(
            store=store,
            authorization_used=authorization_used,
            settle=settle,
            clock=lambda: STALE_TIME,
            owner="owner-b",
        )
        await seed_settling_job(first_service, store, lease_expires_at=NOW)

        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            first, second = await asyncio.gather(
                first_service.create_job(PROOF, REQUEST),
                second_service.create_job(PROOF, REQUEST),
            )

        self.assertEqual(first.job_id, second.job_id)
        authorization_used.assert_awaited_once()
        settle.assert_awaited_once_with(PROOF, "verify-and-settle")

    async def test_expired_proof_reconciles_existing_job_without_validator_patch(
        self,
    ) -> None:
        store = MemoryJobStore()
        proof = signed_proof(valid_before=SIGNED_NOW - 1)
        payment, reason = validate_payment_proof(
            proof,
            now=SIGNED_NOW,
            allow_expired=True,
        )
        self.assertEqual(reason, "")
        assert payment is not None
        service = make_service(
            store=store,
            authorization_used=AsyncMock(return_value=False),
            settle=AsyncMock(),
            clock=lambda: SIGNED_NOW * 1000,
        )
        stored = await seed_settling_job(
            service,
            store,
            lease_expires_at=SIGNED_NOW * 1000 - 1,
            payment=payment,
        )

        with self.assertRaises(X402JobError) as raised:
            await service.create_job(proof, REQUEST)

        self.assertEqual(raised.exception.code, "payment_rejected")
        failed = store.jobs[str(stored.record["jobId"])].record
        self.assertEqual(failed["errorCode"], "payment_failed")

    async def test_expired_recovery_ignores_an_invalid_replay_body(self) -> None:
        store = MemoryJobStore()
        proof = signed_proof(valid_before=SIGNED_NOW - 1)
        payment, reason = validate_payment_proof(
            proof,
            now=SIGNED_NOW,
            allow_expired=True,
        )
        self.assertEqual(reason, "")
        assert payment is not None
        settle = AsyncMock()
        service = make_service(
            store=store,
            authorization_used=AsyncMock(return_value=True),
            settle=settle,
            clock=lambda: SIGNED_NOW * 1000,
        )
        stored = await seed_settling_job(
            service,
            store,
            lease_expires_at=SIGNED_NOW * 1000 - 1,
            payment=payment,
        )
        authoritative_request = dict(stored.record["request"])

        result = await service.create_job(proof, {"symbols": []})

        self.assertEqual(result.status, "queued")
        settle.assert_not_awaited()
        self.assertEqual(
            store.jobs[result.job_id].record["request"],
            authoritative_request,
        )

    async def test_exact_expiry_boundary_reconciles_to_payment_failed(
        self,
    ) -> None:
        store = MemoryJobStore()
        proof = signed_proof(valid_before=SIGNED_NOW)
        payment, reason = validate_payment_proof(
            proof,
            now=SIGNED_NOW,
            allow_expired=True,
        )
        self.assertEqual(reason, "")
        assert payment is not None
        settle = AsyncMock()
        service = make_service(
            store=store,
            authorization_used=AsyncMock(return_value=False),
            settle=settle,
            clock=lambda: SIGNED_NOW * 1000,
        )
        stored = await seed_settling_job(
            service,
            store,
            lease_expires_at=SIGNED_NOW * 1000 - 1,
            payment=payment,
        )

        with self.assertRaises(X402JobError) as raised:
            await service.create_job(proof, REQUEST)

        self.assertEqual(raised.exception.code, "payment_rejected")
        self.assertEqual(
            store.jobs[str(stored.record["jobId"])].record["errorCode"],
            "payment_failed",
        )
        settle.assert_not_awaited()

    async def test_new_expired_proof_is_rejected_without_reservation(self) -> None:
        store = MemoryJobStore()
        proof = signed_proof(valid_before=SIGNED_NOW - 1)
        service = make_service(
            store=store,
            clock=lambda: SIGNED_NOW * 1000,
        )

        with self.assertRaises(X402JobError) as raised:
            await service.create_job(proof, REQUEST)

        self.assertEqual(raised.exception.code, "payment_rejected")
        self.assertEqual(store.create_calls, 0)

    async def test_reconciliation_acquires_cas_before_reading_authorization(
        self,
    ) -> None:
        store = MemoryJobStore()

        async def assert_owned(_payment: VerifiedPayment) -> bool:
            current = next(iter(store.jobs.values())).record
            self.assertEqual(current["leaseOwner"], "new-owner")
            self.assertEqual(current["leaseExpiresAt"], STALE_TIME + 120_000)
            return True

        service = make_service(
            store=store,
            authorization_used=AsyncMock(side_effect=assert_owned),
            clock=lambda: STALE_TIME,
            owner="new-owner",
        )
        await seed_settling_job(service, store, lease_expires_at=NOW)

        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            result = await service.create_job(PROOF, REQUEST)

        self.assertEqual(result.status, "queued")

    async def test_expired_unused_authorization_is_stored_as_payment_failed(
        self,
    ) -> None:
        store = MemoryJobStore()
        payment = verified_payment(valid_before=STALE_TIME // 1000 - 1)
        service = make_service(
            store=store,
            authorization_used=AsyncMock(return_value=False),
            settle=AsyncMock(),
            clock=lambda: STALE_TIME,
        )
        stored = await seed_settling_job(
            service,
            store,
            lease_expires_at=NOW,
            payment=payment,
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(payment, ""),
            ),
            self.assertRaisesRegex(RuntimeError, "payment_rejected"),
        ):
            await service.create_job(PROOF, REQUEST)

        failed = store.jobs[str(stored.record["jobId"])].record
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["paymentStatus"], "failed")
        self.assertEqual(failed["errorCode"], "payment_failed")

    async def test_reporting_exception_logs_only_a_generic_warning(self) -> None:
        sensitive_error = "reporter-failed-with-sensitive-context"
        service = make_service(
            report=AsyncMock(side_effect=RuntimeError(sensitive_error))
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertLogs(
                "seller-agent.x402.jobs",
                level="WARNING",
            ) as captured,
        ):
            result = await service.create_job(PROOF, REQUEST)
            await wait_for_accounting(service)

        self.assertEqual(result.status, "queued")
        self.assertEqual(captured.records[0].levelname, "WARNING")
        self.assertNotIn(sensitive_error, "\n".join(captured.output))
        self.assertNotIn("Traceback", "\n".join(captured.output))

    async def test_false_accounting_result_stays_pending_and_retries(self) -> None:
        store = MemoryJobStore()
        settle = AsyncMock(
            return_value=SettlementOutcome("settled", transaction="0xtx")
        )
        report = AsyncMock(side_effect=[False, True])
        service = make_service(
            store=store,
            settle=settle,
            report=report,
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertLogs("seller-agent.x402.jobs", level="WARNING"),
        ):
            first = await service.create_job(PROOF, REQUEST)
            await wait_for_accounting(service)

        settle.assert_awaited_once_with(PROOF, "verify-and-settle")
        self.assertEqual(report.await_count, 2)
        self.assertEqual(
            report.await_args_list[0].kwargs["event_id"],
            report.await_args_list[1].kwargs["event_id"],
        )
        self.assertIn(first.job_id, store.accounting_markers)

    async def test_accounting_retries_autonomously_with_immutable_settlement_time(
        self,
    ) -> None:
        store = MemoryJobStore()
        now = [NOW]
        calls: list[tuple[str, str, int]] = []
        retry_started = asyncio.Event()

        async def report(*, event_id: str, address: str, called_at: int) -> bool:
            calls.append((event_id, address, called_at))
            return len(calls) >= 2

        async def accounting_sleep(delay: float) -> None:
            self.assertGreater(delay, 0)
            retry_started.set()
            now[0] += 60_000

        async def successful_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            yield "report", {"content": "# complete", "format": "markdown"}

        service = make_service(
            store=store,
            report=report,
            stream_work=successful_stream,
            clock=lambda: now[0],
            accounting_sleep=accounting_sleep,
        )
        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            created = await service.create_job(PROOF, REQUEST)

        await retry_started.wait()
        self.assertTrue(service.is_busy())
        await service.wait_for_idle()

        durable = store.jobs[created.job_id].record
        expected_event = expected_competition_event_id()
        self.assertEqual(
            store.accounting_markers[created.job_id],
            {
                "version": 1,
                "eventId": expected_event,
                "settledAt": NOW,
            },
        )
        self.assertEqual(durable["settledAt"], NOW)
        self.assertEqual(
            calls,
            [
                (expected_event, ADDRESS, NOW),
                (expected_event, ADDRESS, NOW),
            ],
        )
        self.assertFalse(service.is_busy())

    async def test_concurrent_existing_creates_share_one_accounting_task(
        self,
    ) -> None:
        store = MemoryJobStore()
        report_started = asyncio.Event()
        release_report = asyncio.Event()
        report_calls = 0

        async def report(**_kwargs: Any) -> bool:
            nonlocal report_calls
            report_calls += 1
            report_started.set()
            await release_report.wait()
            return True

        service = make_service(store=store, report=report)
        created = await seed_execution_job(
            service,
            store,
            status="succeeded",
            accounting_reported=False,
        )

        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            first = asyncio.create_task(service.create_job(PROOF, REQUEST))
            second = asyncio.create_task(service.create_job(PROOF, REQUEST))
            await report_started.wait()
            await asyncio.sleep(0)
            observed_calls = report_calls
            release_report.set()
            await asyncio.gather(first, second)
        await service.wait_for_idle()

        self.assertEqual(observed_calls, 1)
        self.assertIn(created.job_id, store.accounting_markers)

    async def test_authenticated_get_redrives_accounting_after_restart(
        self,
    ) -> None:
        store = MemoryJobStore()
        seeder = make_service(store=store)
        created = await seed_execution_job(
            seeder,
            store,
            status="succeeded",
            accounting_reported=False,
        )
        stored = store.jobs[created.job_id]
        store.jobs[created.job_id] = StoredJob(
            record={
                **stored.record,
                "settledAt": NOW - 5_000,
            },
            etag=stored.etag,
        )
        report = AsyncMock(return_value=True)
        restarted = make_service(store=store, report=report)

        await restarted.get_job(created.job_id, created.job_token)
        await restarted.wait_for_idle()

        report.assert_awaited_once_with(
            event_id=f"b402:{CHAIN_ID}:{ADDRESS}:{NONCE}",
            address=ADDRESS,
            called_at=NOW - 5_000,
        )
        self.assertEqual(
            store.accounting_markers[created.job_id]["settledAt"],
            NOW - 5_000,
        )

    async def test_accounting_marker_does_not_refresh_running_lease_activity(
        self,
    ) -> None:
        store = MemoryJobStore()
        service = make_service(
            store=store,
            report=AsyncMock(return_value=True),
            clock=lambda: STALE_TIME,
        )
        created = await seed_execution_job(
            service,
            store,
            status="running",
            attempt=1,
            updated_at=NOW,
            accounting_reported=False,
        )
        stored = store.jobs[created.job_id]
        store.jobs[created.job_id] = StoredJob(
            record={
                **stored.record,
                "settledAt": NOW - 10_000,
            },
            etag=stored.etag,
        )

        await service.get_job(created.job_id, created.job_token)
        await service.wait_for_idle()

        durable = store.jobs[created.job_id].record
        self.assertEqual(durable["updatedAt"], NOW)
        self.assertEqual(durable["leaseExpiresAt"], NOW + 120_000)
        self.assertEqual(durable["attempt"], 1)
        self.assertEqual(
            store.accounting_markers[created.job_id],
            {
                "version": 1,
                "eventId": f"b402:{CHAIN_ID}:{ADDRESS}:{NONCE}",
                "settledAt": NOW - 10_000,
            },
        )

    async def test_accounting_completion_never_changes_job_etag(self) -> None:
        store = MemoryJobStore()
        service = make_service(
            store=store,
            report=AsyncMock(return_value=True),
        )
        created = await seed_execution_job(
            service,
            store,
            status="running",
            attempt=1,
            updated_at=NOW,
            accounting_reported=False,
        )
        before = store.jobs[created.job_id]

        await service.get_job(created.job_id, created.job_token)
        await wait_for_accounting(service)

        after = store.jobs[created.job_id]
        self.assertEqual(after.etag, before.etag)
        self.assertEqual(after.record, before.record)
        self.assertIn(created.job_id, store.accounting_markers)

    async def test_cross_process_accounting_publishes_one_marker(
        self,
    ) -> None:
        store = MemoryJobStore()
        both_reporting = asyncio.Barrier(2)
        calls: list[tuple[str, str, int]] = []

        async def report(*, event_id: str, address: str, called_at: int) -> bool:
            calls.append((event_id, address, called_at))
            await both_reporting.wait()
            return True

        first = make_service(store=store, report=report, owner="process-a")
        second = make_service(store=store, report=report, owner="process-b")
        created = await seed_execution_job(
            first,
            store,
            status="succeeded",
            accounting_reported=False,
        )
        original = store.jobs[created.job_id]

        await asyncio.gather(
            first.get_job(created.job_id, created.job_token),
            second.get_job(created.job_id, created.job_token),
        )
        await asyncio.gather(
            wait_for_accounting(first),
            wait_for_accounting(second),
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(set(calls)), 1)
        self.assertEqual(len(store.accounting_markers), 1)
        self.assertEqual(store.jobs[created.job_id], original)

    async def test_accounting_marker_failure_does_not_block_job_delivery(
        self,
    ) -> None:
        store = AccountingMarkerFailureStore()
        service = make_service(
            store=store,
            report=AsyncMock(return_value=True),
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertLogs(
                "seller-agent.x402.jobs",
                level="WARNING",
            ) as captured,
        ):
            result = await service.create_job(PROOF, REQUEST)
            await wait_for_accounting(service)

        self.assertEqual(result.status, "queued")
        self.assertNotIn("competitionReported", store.jobs[result.job_id].record)
        self.assertNotIn(result.job_id, store.accounting_markers)
        output = "\n".join(captured.output)
        self.assertNotIn("sensitive-marker-storage-failure", output)
        self.assertNotIn("Traceback", output)

    async def test_overlapping_expired_owners_have_one_durable_transition(
        self,
    ) -> None:
        store = MemoryJobStore()
        now = [STALE_TIME]
        first_submission_started = asyncio.Event()
        release_first_submission = asyncio.Event()
        submissions = 0

        async def ambiguous_settle(
            _proof: str,
            _mode: str,
        ) -> SettlementOutcome:
            nonlocal submissions
            submissions += 1
            # Two HTTP submissions may observe the same successful nonce use;
            # they cannot represent two EIP-3009 transfers.
            if submissions == 1:
                first_submission_started.set()
                await release_first_submission.wait()
                return SettlementOutcome(
                    "settled",
                    transaction="0xeip3009-transfer",
                )
            return SettlementOutcome(
                "settled",
                transaction="0xeip3009-transfer",
            )

        authorization_used = AsyncMock(return_value=False)
        report = AsyncMock(return_value=True)
        settle = AsyncMock(side_effect=ambiguous_settle)
        old_owner = make_service(
            store=store,
            authorization_used=authorization_used,
            settle=settle,
            report=report,
            clock=lambda: now[0],
            owner="owner-a",
        )
        new_owner = make_service(
            store=store,
            authorization_used=authorization_used,
            settle=settle,
            report=report,
            clock=lambda: now[0],
            owner="owner-b",
        )
        await seed_settling_job(old_owner, store, lease_expires_at=NOW)

        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            old_attempt = asyncio.create_task(
                old_owner.create_job(PROOF, REQUEST)
            )
            await first_submission_started.wait()
            now[0] += 120_001
            winner = await new_owner.create_job(PROOF, REQUEST)
            release_first_submission.set()
            with self.assertRaises(JobConflict):
                await old_attempt

        self.assertEqual(winner.status, "queued")
        self.assertEqual(settle.await_count, 2)
        self.assertEqual(authorization_used.await_count, 2)
        self.assertEqual(report.await_count, 1)
        durable = store.jobs[winner.job_id].record
        self.assertEqual(
            durable["settlementReference"],
            "0xeip3009-transfer",
        )

    async def test_settlement_calls_have_a_sixty_second_wait_bound(self) -> None:
        service = make_service()

        async def bounded_wait(
            awaitable: Any,
            *,
            timeout: float,
        ) -> SettlementOutcome:
            self.assertEqual(timeout, 60)
            return await awaitable

        wait_for = AsyncMock(side_effect=bounded_wait)
        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            patch("x402_job_service.asyncio.wait_for", wait_for),
        ):
            result = await service.create_job(PROOF, REQUEST)

        self.assertEqual(result.status, "queued")
        wait_for.assert_awaited_once()

    async def test_settlement_timeout_leaves_a_recoverable_reservation(
        self,
    ) -> None:
        store = MemoryJobStore()
        service = make_service(store=store)

        async def local_timeout(
            awaitable: Any,
            *,
            timeout: float,
        ) -> SettlementOutcome:
            self.assertEqual(timeout, 60)
            awaitable.close()
            raise TimeoutError

        class Python310BuiltinTimeout(Exception):
            pass

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            patch(
                "x402_job_service.asyncio.wait_for",
                AsyncMock(side_effect=local_timeout),
            ),
            patch(
                "x402_job_service.TimeoutError",
                Python310BuiltinTimeout,
                create=True,
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(raised.exception.code, "settlement_pending")
        self.assertTrue(raised.exception.retryable)
        stored = next(iter(store.jobs.values())).record
        self.assertEqual(stored["status"], "settling")
        self.assertEqual(stored["paymentStatus"], "settling")

    async def test_facilitator_transport_timeout_keeps_settling_record(
        self,
    ) -> None:
        store = MemoryJobStore()
        original_client = httpx.AsyncClient

        def timeout_transport(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        transport = httpx.MockTransport(timeout_transport)
        timeouts: list[float] = []

        def client_with_timeout_transport(
            *args: Any,
            **kwargs: Any,
        ) -> httpx.AsyncClient:
            timeouts.append(float(kwargs["timeout"]))
            return original_client(*args, transport=transport, **kwargs)

        async def settle(
            _proof_header: str,
            _mode: str,
        ) -> SettlementOutcome:
            return await _settle_generic({})

        service = make_service(store=store, settle=settle)
        with (
            patch(
                "x402_handler.FACILITATOR_URL",
                "https://facilitator.example.test",
            ),
            patch(
                "x402_handler.httpx.AsyncClient",
                side_effect=client_with_timeout_transport,
            ),
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(raised.exception.code, "settlement_pending")
        self.assertTrue(raised.exception.retryable)
        stored = next(iter(store.jobs.values())).record
        self.assertEqual(stored["status"], "settling")
        self.assertEqual(stored["paymentStatus"], "settling")
        self.assertEqual(timeouts, [20.0])


class X402JobExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_get_redrives_queued_job_after_restart(
        self,
    ) -> None:
        store = MemoryJobStore()
        stream_calls = 0

        async def successful_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            nonlocal stream_calls
            stream_calls += 1
            yield "report", {"content": "# recovered", "format": "markdown"}

        service = make_service(store=store, stream_work=successful_stream)
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )

        first, second = await asyncio.gather(
            service.get_job(created.job_id, created.job_token),
            service.get_job(created.job_id, created.job_token),
        )
        self.assertEqual(first.status, "queued")
        self.assertEqual(second.status, "queued")
        await service.wait_for_idle()

        durable = store.jobs[created.job_id].record
        self.assertEqual(durable["status"], "succeeded")
        self.assertEqual(durable["attempt"], 1)
        self.assertEqual(stream_calls, 1)

    async def test_authenticated_resume_redrives_queued_job_after_crash(
        self,
    ) -> None:
        store = MemoryJobStore()

        async def successful_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            yield "report", {"content": "# recovered", "format": "markdown"}

        service = make_service(store=store, stream_work=successful_stream)
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )

        resumed = await service.resume_job(
            created.job_id,
            created.job_token,
        )
        self.assertIn(resumed.status, {"queued", "running", "succeeded"})
        await service.wait_for_idle()

        durable = store.jobs[created.job_id].record
        self.assertEqual(durable["status"], "succeeded")
        self.assertEqual(durable["attempt"], 1)

    async def test_cross_process_queued_resume_uses_one_execution_claim(
        self,
    ) -> None:
        store = MemoryJobStore()
        stream_calls = 0

        async def successful_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            nonlocal stream_calls
            stream_calls += 1
            yield "report", {"content": "# one owner", "format": "markdown"}

        first = make_service(
            store=store,
            stream_work=successful_stream,
            owner="process-a",
        )
        second = make_service(
            store=store,
            stream_work=successful_stream,
            owner="process-b",
        )
        created = await seed_execution_job(
            first,
            store,
            status="queued",
        )

        await asyncio.gather(
            first.resume_job(created.job_id, created.job_token),
            second.resume_job(created.job_id, created.job_token),
        )
        await asyncio.gather(first.wait_for_idle(), second.wait_for_idle())

        durable = store.jobs[created.job_id].record
        self.assertEqual(stream_calls, 1)
        self.assertEqual(durable["status"], "succeeded")
        self.assertEqual(durable["attempt"], 1)

    async def test_busy_status_is_thread_safe_while_tasks_mutate(self) -> None:
        owner_thread = threading.get_ident()

        class MainThreadTaskMap(dict[str, asyncio.Task[None]]):
            def values(self):
                if threading.get_ident() != owner_thread:
                    raise AssertionError(
                        "is_busy read the asyncio task map cross-thread"
                    )
                return super().values()

        service = make_service(store=MemoryJobStore())
        service._tasks = MainThreadTaskMap()
        release = asyncio.Event()

        async def block_until_released(_job_id: str) -> None:
            await release.wait()

        service._run_job = block_until_released
        service._spawn("job-0")

        stop = threading.Event()
        reader_started = threading.Event()
        reader_saw_idle = threading.Event()
        observations: list[bool] = []
        errors: list[BaseException] = []

        def read_busy() -> None:
            reader_started.set()
            while not stop.is_set():
                try:
                    busy = service.is_busy()
                except BaseException as exc:
                    errors.append(exc)
                    return
                observations.append(busy)
                if not busy:
                    reader_saw_idle.set()
                stop.wait(0.0001)

        reader = threading.Thread(target=read_busy)
        reader.start()
        try:
            self.assertTrue(
                await asyncio.to_thread(reader_started.wait, 1.0)
            )
            for index in range(1, 50):
                service._spawn(f"job-{index}")
                await asyncio.sleep(0)

            release.set()
            await service.wait_for_idle()
            self.assertTrue(
                await asyncio.to_thread(reader_saw_idle.wait, 1.0)
            )
        finally:
            stop.set()
            await asyncio.to_thread(reader.join, 1.0)
            release.set()
            await service.wait_for_idle()

        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])
        self.assertIn(True, observations)
        self.assertIn(False, observations)
        self.assertFalse(service.is_busy())

    async def test_worker_writes_final_report_before_succeeded(self) -> None:
        store = MemoryJobStore()

        async def successful_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            yield "progress", {"stage": "collecting"}
            yield "report", {"content": "# draft", "format": "markdown"}
            yield "report", {"content": "# complete", "format": "markdown"}
            yield "done", {}

        service = make_service(store=store, stream_work=successful_stream)
        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            created = await service.create_job(PROOF, REQUEST)

        self.assertTrue(service.is_busy())
        await service.wait_for_idle()
        view = await service.get_job(created.job_id, created.job_token)

        self.assertEqual(view.status, "succeeded")
        self.assertEqual(
            store.report_for_record(store.jobs[created.job_id].record),
            "# complete",
        )
        self.assertLess(
            store.events.index("put_report"),
            store.events.index("replace:succeeded"),
        )
        self.assertFalse(service.is_busy())

    async def test_query_authentication_hides_missing_and_wrong_token(
        self,
    ) -> None:
        store = MemoryJobStore()
        service = make_service(store=store)
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )

        errors = []
        for job_id, token in (
            (created.job_id, "wrong-token"),
            ("x402_" + "f" * 32, created.job_token),
            ("not-a-job-id", created.job_token),
        ):
            with self.assertRaises(X402JobError) as raised:
                await service.get_job(job_id, token)
            errors.append(raised.exception.code)

        self.assertEqual(errors, ["job_not_found"] * 3)
        self.assertFalse(service._tasks)

    async def test_expiration_is_enforced_before_presigning(self) -> None:
        store = MemoryJobStore()
        service = make_service(store=store)
        created = await seed_execution_job(
            service,
            store,
            status="succeeded",
            expires_at=NOW,
        )

        with self.assertRaises(X402JobError) as raised:
            await service.get_job(created.job_id, created.job_token)

        self.assertEqual(raised.exception.code, "job_expired")
        self.assertEqual(store.presign_calls, 0)

    async def test_succeeded_query_renews_thirty_minute_url(self) -> None:
        store = MemoryJobStore()
        now = [NOW]
        service = make_service(store=store, clock=lambda: now[0])
        created = await seed_execution_job(
            service,
            store,
            status="succeeded",
        )

        first = await service.get_job(created.job_id, created.job_token)
        now[0] += 1
        second = await service.get_job(created.job_id, created.job_token)

        self.assertNotEqual(first.download_url, second.download_url)
        self.assertEqual(first.download_url_expires_at, NOW + 1_800_000)
        self.assertEqual(
            second.download_url_expires_at,
            NOW + 1 + 1_800_000,
        )

    async def test_resume_stale_job_only_once_under_concurrency(self) -> None:
        store = MemoryJobStore()
        service = make_service(store=store, clock=lambda: STALE_TIME)
        created = await seed_execution_job(
            service,
            store,
            status="running",
            attempt=1,
            updated_at=NOW,
        )

        first, second = await asyncio.gather(
            service.resume_job(created.job_id, created.job_token),
            service.resume_job(created.job_id, created.job_token),
            return_exceptions=True,
        )

        self.assertEqual(
            sum(not isinstance(value, Exception) for value in (first, second)),
            1,
        )
        failure = next(
            value for value in (first, second) if isinstance(value, Exception)
        )
        self.assertIsInstance(failure, X402JobError)
        self.assertEqual(failure.code, "job_conflict")
        for task in tuple(service._tasks.values()):
            task.cancel()
        await service.wait_for_idle()

    async def test_fresh_running_job_cannot_resume(self) -> None:
        store = MemoryJobStore()
        service = make_service(store=store, clock=lambda: STALE_TIME)
        created = await seed_execution_job(
            service,
            store,
            status="running",
            attempt=1,
            updated_at=STALE_TIME - 119_999,
        )

        with self.assertRaises(X402JobError) as raised:
            await service.resume_job(created.job_id, created.job_token)

        self.assertEqual(raised.exception.code, "job_conflict")
        self.assertFalse(service.is_busy())

    async def test_fourth_execution_is_never_started(self) -> None:
        store = MemoryJobStore()
        service = make_service(store=store)
        created = await seed_execution_job(
            service,
            store,
            status="failed",
            attempt=3,
            retryable=True,
        )

        with self.assertRaises(X402JobError) as raised:
            await service.resume_job(created.job_id, created.job_token)

        self.assertEqual(raised.exception.code, "attempts_exhausted")
        self.assertFalse(service.is_busy())

    async def test_empty_analysis_retries_twice_then_succeeds(self) -> None:
        store = MemoryJobStore()
        settle = AsyncMock()
        authorization_used = AsyncMock()
        sessions: list[str] = []
        calls = 0

        async def stream(
            _prompt: str,
            session_id: str,
            _symbols: list[str],
        ) -> Any:
            nonlocal calls
            calls += 1
            sessions.append(session_id)
            if calls == 3:
                yield "report", {
                    "content": "usable report",
                    "format": "text",
                }
            yield "done", {}

        service = make_service(
            store=store,
            stream_work=stream,
            settle=settle,
            authorization_used=authorization_used,
        )
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )

        with patch(
            "x402_job_service.validate_payment_proof",
            side_effect=AssertionError("execution must not validate payment"),
        ) as validate:
            await service._run_job(created.job_id)

        record = store.jobs[created.job_id].record
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["attempt"], 1)
        self.assertEqual(calls, 3)
        self.assertEqual(
            sessions,
            [
                f"{created.job_id}-attempt-1-llm-1",
                f"{created.job_id}-attempt-1-llm-2",
                f"{created.job_id}-attempt-1-llm-3",
            ],
        )
        self.assertEqual(len(set(sessions)), 3)
        validate.assert_not_called()
        settle.assert_not_awaited()
        authorization_used.assert_not_awaited()
        self.assertFalse(service.is_busy())

    async def test_three_empty_calls_are_resumable_without_resettlement(
        self,
    ) -> None:
        store = MemoryJobStore()
        settle = AsyncMock()
        authorization_used = AsyncMock()
        sessions: list[str] = []

        async def empty_stream(
            _prompt: str,
            session_id: str,
            _symbols: list[str],
        ) -> Any:
            sessions.append(session_id)
            yield "done", {}

        service = make_service(
            store=store,
            stream_work=empty_stream,
            settle=settle,
            authorization_used=authorization_used,
        )
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )
        settled_fields = {
            key: store.jobs[created.job_id].record[key]
            for key in (
                "paymentStatus",
                "paymentMode",
                "asset",
                "settlementReference",
                "settledAt",
            )
        }

        with patch(
            "x402_job_service.validate_payment_proof",
            side_effect=AssertionError("resume must not validate payment"),
        ) as validate:
            await service._run_job(created.job_id)
            failed = store.jobs[created.job_id].record
            self.assertEqual(failed["errorCode"], "analysis_empty_response")
            self.assertIs(failed["retryable"], True)
            self.assertEqual(failed["attempt"], 1)
            self.assertEqual(len(sessions), 3)

            await service.resume_job(created.job_id, created.job_token)
            await service.wait_for_idle()

        resumed = store.jobs[created.job_id].record
        self.assertEqual(resumed["errorCode"], "analysis_empty_response")
        self.assertIs(resumed["retryable"], True)
        self.assertEqual(resumed["attempt"], 2)
        self.assertEqual(
            {key: resumed[key] for key in settled_fields},
            settled_fields,
        )
        self.assertEqual(len(sessions), 6)
        self.assertEqual(len(set(sessions)), 6)
        validate.assert_not_called()
        settle.assert_not_awaited()
        authorization_used.assert_not_awaited()

    async def test_three_outer_attempts_exhaust_at_nine_inner_calls(
        self,
    ) -> None:
        store = MemoryJobStore()
        settle = AsyncMock()
        authorization_used = AsyncMock()
        sessions: list[str] = []

        async def empty_stream(
            _prompt: str,
            session_id: str,
            _symbols: list[str],
        ) -> Any:
            sessions.append(session_id)
            yield "done", {}

        service = make_service(
            store=store,
            stream_work=empty_stream,
            settle=settle,
            authorization_used=authorization_used,
        )
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )

        with patch(
            "x402_job_service.validate_payment_proof",
            side_effect=AssertionError("retry must not validate payment"),
        ) as validate:
            await service._run_job(created.job_id)
            for _ in range(2):
                await service.resume_job(created.job_id, created.job_token)
                await service.wait_for_idle()
            with self.assertRaises(X402JobError) as raised:
                await service.resume_job(created.job_id, created.job_token)

        failed = store.jobs[created.job_id].record
        self.assertEqual(raised.exception.code, "attempts_exhausted")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["errorCode"], "analysis_empty_response")
        self.assertIs(failed["retryable"], True)
        self.assertEqual(failed["attempt"], 3)
        self.assertEqual(len(sessions), 9)
        self.assertEqual(len(set(sessions)), 9)
        self.assertEqual(
            sessions,
            [
                f"{created.job_id}-attempt-{outer}-llm-{inner}"
                for outer in range(1, 4)
                for inner in range(1, 4)
            ],
        )
        validate.assert_not_called()
        settle.assert_not_awaited()
        authorization_used.assert_not_awaited()

    async def test_settlement_happens_once_across_empty_retries_and_resumes(
        self,
    ) -> None:
        store = MemoryJobStore()
        settle = AsyncMock(
            return_value=SettlementOutcome(
                "settled",
                transaction="0xsettled-once",
            )
        )
        authorization_used = AsyncMock()
        calls = 0

        async def empty_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            nonlocal calls
            calls += 1
            yield "done", {}

        service = make_service(
            store=store,
            stream_work=empty_stream,
            settle=settle,
            authorization_used=authorization_used,
        )

        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ) as validate:
            created = await service.create_job(PROOF, REQUEST)
            await service.wait_for_idle()
            for _ in range(2):
                await service.resume_job(created.job_id, created.job_token)
                await service.wait_for_idle()

        durable = store.jobs[created.job_id].record
        self.assertEqual(durable["paymentStatus"], "settled")
        self.assertEqual(durable["settlementReference"], "0xsettled-once")
        self.assertEqual(durable["status"], "failed")
        self.assertEqual(durable["errorCode"], "analysis_empty_response")
        self.assertEqual(durable["attempt"], 3)
        self.assertEqual(calls, 9)
        self.assertEqual(validate.call_count, 1)
        settle.assert_awaited_once_with(PROOF, "verify-and-settle")
        authorization_used.assert_not_awaited()

    async def test_non_json_report_succeeds_without_retry(self) -> None:
        store = MemoryJobStore()
        sessions: list[str] = []

        async def raw_stream(
            _prompt: str,
            session_id: str,
            _symbols: list[str],
        ) -> Any:
            sessions.append(session_id)
            yield "report", {
                "content": "plain text, not JSON",
                "format": "text",
            }
            yield "done", {}

        service = make_service(store=store, stream_work=raw_stream)
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )

        await service._run_job(created.job_id)

        succeeded = store.jobs[created.job_id].record
        self.assertEqual(succeeded["status"], "succeeded")
        self.assertEqual(
            store.report_for_record(succeeded),
            "plain text, not JSON",
        )
        self.assertEqual(
            sessions,
            [f"{created.job_id}-attempt-1-llm-1"],
        )

    async def test_generator_exception_is_retryable_without_detail(self) -> None:
        store = MemoryJobStore()
        sensitive = "pipeline-secret-detail"

        async def failing_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            yield "progress", {"stage": "collecting"}
            raise RuntimeError(sensitive)

        service = make_service(store=store, stream_work=failing_stream)
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )

        service._spawn(created.job_id)
        await service.wait_for_idle()

        failed = store.jobs[created.job_id].record
        self.assertEqual(failed["errorCode"], "analysis_failed")
        self.assertTrue(failed["retryable"])
        self.assertNotIn(sensitive, str(failed))
        self.assertFalse(service.is_busy())

    async def test_exhausted_provider_rate_limit_has_stable_error_code(self) -> None:
        store = MemoryJobStore()

        async def rate_limited_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            yield "progress", {"stage": "collecting"}
            raise X402JobError("too_many_users", retryable=True)

        service = make_service(store=store, stream_work=rate_limited_stream)
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )

        service._spawn(created.job_id)
        await service.wait_for_idle()

        failed = store.jobs[created.job_id].record
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["errorCode"], "too_many_users")
        self.assertTrue(failed["retryable"])
        self.assertFalse(service.is_busy())

    async def test_analysis_timeout_is_retryable(self) -> None:
        store = MemoryJobStore()
        service = make_service(
            store=store,
            analysis_timeout_seconds=0,
        )
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )

        service._spawn(created.job_id)
        await service.wait_for_idle()

        failed = store.jobs[created.job_id].record
        self.assertEqual(failed["errorCode"], "analysis_timeout")
        self.assertTrue(failed["retryable"])
        self.assertFalse(service.is_busy())

    async def test_each_empty_retry_receives_a_fresh_timeout_budget(
        self,
    ) -> None:
        store = MemoryJobStore()
        calls = 0

        async def slow_then_successful_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.06)
            if calls == 2:
                yield "report", {
                    "content": "slow but usable",
                    "format": "text",
                }
            yield "done", {}

        service = make_service(
            store=store,
            stream_work=slow_then_successful_stream,
            analysis_timeout_seconds=0.1,
        )
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )

        await service._run_job(created.job_id)

        succeeded = store.jobs[created.job_id].record
        self.assertEqual(succeeded["status"], "succeeded")
        self.assertEqual(calls, 2)
        self.assertEqual(store.report_for_record(succeeded), "slow but usable")

    async def test_timed_out_inner_call_does_not_continue_empty_retries(
        self,
    ) -> None:
        store = MemoryJobStore()
        calls = 0

        async def blocked_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            nonlocal calls
            calls += 1
            await asyncio.Event().wait()
            if False:
                yield "done", {}

        service = make_service(
            store=store,
            stream_work=blocked_stream,
            analysis_timeout_seconds=0.01,
        )
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )

        await service._run_job(created.job_id)

        failed = store.jobs[created.job_id].record
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["errorCode"], "analysis_timeout")
        self.assertIs(failed["retryable"], True)
        self.assertEqual(calls, 1)

    async def test_cancelled_worker_leaves_recoverable_running_state(
        self,
    ) -> None:
        store = MemoryJobStore()
        started = asyncio.Event()

        async def blocking_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            started.set()
            await asyncio.Event().wait()
            if False:
                yield "", {}

        service = make_service(store=store, stream_work=blocking_stream)
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )
        service._spawn(created.job_id)
        await started.wait()

        task = service._tasks[created.job_id]
        task.cancel()
        await service.wait_for_idle()
        await asyncio.sleep(0)

        record = store.jobs[created.job_id].record
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["attempt"], 1)
        self.assertFalse(service.is_busy())

    async def test_heartbeat_extends_lease_after_exactly_thirty_seconds(
        self,
    ) -> None:
        store = MemoryJobStore()
        now = [NOW]
        first_sleep = True

        async def heartbeat_sleep(delay: float) -> None:
            nonlocal first_sleep
            self.assertEqual(delay, 30)
            if first_sleep:
                first_sleep = False
                now[0] += 30_000
                return
            await asyncio.Event().wait()

        service = make_service(
            store=store,
            clock=lambda: now[0],
            heartbeat_sleep=heartbeat_sleep,
        )
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )
        service._spawn(created.job_id)

        for _ in range(20):
            if store.jobs[created.job_id].record["updatedAt"] == NOW + 30_000:
                break
            await asyncio.sleep(0)

        record = store.jobs[created.job_id].record
        self.assertEqual(record["updatedAt"], NOW + 30_000)
        self.assertEqual(record["leaseExpiresAt"], NOW + 150_000)
        service._tasks[created.job_id].cancel()
        await service.wait_for_idle()

    async def test_lease_losing_worker_cannot_write_report_or_terminal_state(
        self,
    ) -> None:
        store = MemoryJobStore()
        release = asyncio.Event()
        started = asyncio.Event()

        async def delayed_report(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            started.set()
            await release.wait()
            yield "report", {"content": "# obsolete", "format": "markdown"}

        service = make_service(store=store, stream_work=delayed_report)
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )
        service._spawn(created.job_id)
        await started.wait()
        running = store.jobs[created.job_id]
        replacement = {
            **running.record,
            "status": "queued",
            "leaseOwner": None,
            "leaseExpiresAt": None,
            "updatedAt": NOW + 120_001,
        }
        await store.replace(running, replacement)
        release.set()

        await service.wait_for_idle()

        self.assertFalse(store.reports)
        self.assertEqual(store.jobs[created.job_id].record["status"], "queued")
        self.assertFalse(service.is_busy())

    async def test_completion_waits_for_an_inflight_heartbeat_cas(self) -> None:
        store = InFlightHeartbeatStore()
        release_report = asyncio.Event()
        now = [NOW]
        first_sleep = True

        async def heartbeat_sleep(_delay: float) -> None:
            nonlocal first_sleep
            if first_sleep:
                first_sleep = False
                now[0] += 30_000
                return
            await asyncio.Event().wait()

        async def successful_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            await release_report.wait()
            yield "report", {"content": "# complete", "format": "markdown"}

        service = make_service(
            store=store,
            stream_work=successful_stream,
            clock=lambda: now[0],
            heartbeat_sleep=heartbeat_sleep,
        )
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )
        service._spawn(created.job_id)
        await store.heartbeat_written.wait()
        release_report.set()
        await asyncio.sleep(0)
        store.release_heartbeat.set()

        await service.wait_for_idle()

        self.assertEqual(
            store.jobs[created.job_id].record["status"],
            "succeeded",
        )
        self.assertEqual(
            store.report_for_record(store.jobs[created.job_id].record),
            "# complete",
        )

    async def test_accounting_marker_does_not_conflict_with_inflight_heartbeat(
        self,
    ) -> None:
        store = InFlightHeartbeatStore()
        release_analysis = asyncio.Event()
        accounting_started = asyncio.Event()
        release_accounting = asyncio.Event()
        now = [NOW]
        first_sleep = True

        async def heartbeat_sleep(_delay: float) -> None:
            nonlocal first_sleep
            if first_sleep:
                first_sleep = False
                now[0] += 30_000
                return
            await asyncio.Event().wait()

        async def report(**_kwargs: Any) -> bool:
            accounting_started.set()
            await release_accounting.wait()
            return True

        async def successful_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            await release_analysis.wait()
            yield "report", {"content": "# complete", "format": "markdown"}

        service = make_service(
            store=store,
            report=report,
            stream_work=successful_stream,
            clock=lambda: now[0],
            heartbeat_sleep=heartbeat_sleep,
        )
        created = await seed_execution_job(
            service,
            store,
            status="queued",
            accounting_reported=False,
        )

        service._spawn(created.job_id)
        await accounting_started.wait()
        await store.heartbeat_written.wait()
        heartbeat_record = store.jobs[created.job_id]
        release_accounting.set()
        await wait_for_accounting(service)

        self.assertEqual(store.jobs[created.job_id], heartbeat_record)
        self.assertIn(created.job_id, store.accounting_markers)

        store.release_heartbeat.set()
        await store.heartbeat_returned.wait()
        release_analysis.set()
        await service.wait_for_idle()

        durable = store.jobs[created.job_id].record
        self.assertEqual(durable["status"], "succeeded")
        self.assertEqual(durable["attempt"], 1)
        self.assertEqual(store.report_for_record(durable), "# complete")

    async def test_stale_upload_cannot_overwrite_newer_success(self) -> None:
        store = OvertakingReportStore()
        now = [NOW]

        async def stale_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            yield "report", {"content": "# stale", "format": "markdown"}

        async def winning_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            yield "report", {"content": "# winner", "format": "markdown"}

        stale_service = make_service(
            store=store,
            stream_work=stale_stream,
            clock=lambda: now[0],
        )
        winning_service = make_service(
            store=store,
            stream_work=winning_stream,
            clock=lambda: now[0],
        )
        created = await seed_execution_job(
            stale_service,
            store,
            status="queued",
        )
        stale_service._spawn(created.job_id)
        await store.first_upload_started.wait()
        now[0] += 120_001

        await winning_service.resume_job(
            created.job_id,
            created.job_token,
        )
        await winning_service.wait_for_idle()
        winner = store.jobs[created.job_id].record
        self.assertEqual(winner["status"], "succeeded")
        self.assertEqual(store.report_for_record(winner), "# winner")

        store.release_first_upload.set()
        await stale_service.wait_for_idle()

        durable = store.jobs[created.job_id].record
        self.assertEqual(durable, winner)
        self.assertEqual(store.report_for_record(durable), "# winner")

    async def test_same_process_resume_hands_off_from_stale_worker(
        self,
    ) -> None:
        store = MemoryJobStore()
        now = [NOW]
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        stream_calls = 0

        async def resumable_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            nonlocal stream_calls
            stream_calls += 1
            if stream_calls == 1:
                first_started.set()
                await asyncio.Event().wait()
            second_started.set()
            yield "report", {"content": "# resumed", "format": "markdown"}

        service = make_service(
            store=store,
            stream_work=resumable_stream,
            clock=lambda: now[0],
        )
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )
        service._spawn(created.job_id)
        await first_started.wait()
        old_task = service._tasks[created.job_id]
        now[0] += 120_001

        await service.resume_job(created.job_id, created.job_token)
        for _ in range(10):
            if second_started.is_set():
                break
            await asyncio.sleep(0)

        self.assertTrue(old_task.cancelled())
        self.assertTrue(second_started.is_set())
        await service.wait_for_idle()
        durable = store.jobs[created.job_id].record
        self.assertEqual(durable["status"], "succeeded")
        self.assertEqual(durable["attempt"], 2)

    async def test_transient_initial_read_is_retried_before_task_exits(
        self,
    ) -> None:
        store = TransientReadStore()

        async def successful_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            yield "report", {"content": "# recovered", "format": "markdown"}

        service = make_service(
            store=store,
            stream_work=successful_stream,
        )
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )

        service._spawn(created.job_id)
        await service.wait_for_idle()

        durable = store.jobs[created.job_id].record
        self.assertEqual(store.read_calls, 2)
        self.assertEqual(durable["status"], "succeeded")
        self.assertEqual(durable["attempt"], 1)
        self.assertFalse(service.is_busy())

    async def test_transient_initial_claim_is_redriven_from_fresh_state(
        self,
    ) -> None:
        store = TransientClaimStore()

        async def successful_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            yield "report", {"content": "# recovered", "format": "markdown"}

        service = make_service(
            store=store,
            stream_work=successful_stream,
        )
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )

        service._spawn(created.job_id)
        await service.wait_for_idle()

        durable = store.jobs[created.job_id].record
        self.assertEqual(store.claim_calls, 2)
        self.assertEqual(durable["status"], "succeeded")
        self.assertEqual(durable["attempt"], 1)

    async def test_cancelled_resume_finishes_cas_and_worker_handoff(
        self,
    ) -> None:
        store = AppliedResumeStore()
        now = [NOW]
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        stream_calls = 0

        async def resumable_stream(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            nonlocal stream_calls
            stream_calls += 1
            if stream_calls == 1:
                first_started.set()
                await asyncio.Event().wait()
            second_started.set()
            yield "report", {"content": "# resumed", "format": "markdown"}

        service = make_service(
            store=store,
            stream_work=resumable_stream,
            clock=lambda: now[0],
        )
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )
        service._spawn(created.job_id)
        await first_started.wait()
        now[0] += 120_001
        resume = asyncio.create_task(
            service.resume_job(created.job_id, created.job_token)
        )
        await store.resume_applied.wait()

        resume.cancel()
        store.release_resume.set()
        with self.assertRaises(asyncio.CancelledError):
            await resume

        for _ in range(10):
            if second_started.is_set():
                break
            await asyncio.sleep(0)
        if not second_started.is_set():
            service._tasks[created.job_id].cancel()
            await service.wait_for_idle()

        self.assertTrue(resume.cancelled())
        self.assertTrue(second_started.is_set())
        await service.wait_for_idle()
        durable = store.jobs[created.job_id].record
        self.assertEqual(durable["status"], "succeeded")
        self.assertEqual(durable["attempt"], 2)


class X402JobValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_job_token_secret_requires_at_least_32_bytes(self) -> None:
        with self.assertRaisesRegex(X402JobError, "at least 32 bytes"):
            load_job_token_secret({"X402_JOB_TOKEN_SECRET": "short"})
        self.assertEqual(
            load_job_token_secret({"X402_JOB_TOKEN_SECRET": "x" * 32}),
            b"x" * 32,
        )

    async def test_request_is_normalized_before_storage(self) -> None:
        store = MemoryJobStore()
        service = make_service(store=store)

        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            result = await service.create_job(
                PROOF,
                {
                    "symbols": " bnb, btc-usd ",
                    "analysis_type": "technical",
                    "portfolio": [{"symbol": "BNB", "quantity": 1}],
                    "risk_profile": {"tolerance": "high"},
                },
            )

        self.assertEqual(
            store.jobs[result.job_id].record["request"],
            {
                "symbols": ["BNB", "BTC-USD"],
                "analysisType": "technical",
                "portfolio": [{"symbol": "BNB", "quantity": 1}],
                "riskProfile": {"tolerance": "high"},
            },
        )

    async def test_invalid_request_does_not_reserve_or_settle(self) -> None:
        store = MemoryJobStore()
        settle = AsyncMock()
        service = make_service(store=store, settle=settle)

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaisesRegex(X402JobError, "invalid_request"),
        ):
            await service.create_job(PROOF, {"symbols": []})

        self.assertEqual(store.create_calls, 0)
        settle.assert_not_awaited()

    async def test_paused_creation_rejects_only_valid_absent_payment(self) -> None:
        store = ReadTrackingStore()
        service = X402JobService(
            store=store,
            token_secret=TOKEN_SECRET,
            settle=AsyncMock(),
            authorization_used=AsyncMock(),
            report=AsyncMock(),
            stream_work=AsyncMock(),
            accept_new_jobs=False,
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ) as validate,
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(raised.exception.code, "async_jobs_paused")
        self.assertTrue(raised.exception.retryable)
        validate.assert_called_once()
        self.assertEqual(store.read_calls, 1)
        self.assertEqual(store.create_calls, 0)

    async def test_paused_service_returns_existing_same_proof_job(self) -> None:
        store = MemoryJobStore()
        enabled = make_service(store=store)
        created = await seed_execution_job(
            enabled,
            store,
            status="queued",
        )
        paused = X402JobService(
            store=store,
            token_secret=TOKEN_SECRET,
            settle=AsyncMock(),
            authorization_used=AsyncMock(),
            report=AsyncMock(return_value=True),
            stream_work=AsyncMock(),
            clock=lambda: NOW,
            accept_new_jobs=False,
        )

        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            result = await paused.create_job(PROOF, {"symbols": []})

        self.assertEqual(result.job_id, created.job_id)
        self.assertEqual(result.status, "queued")
        for task in tuple(paused._tasks.values()):
            task.cancel()
        await paused.wait_for_idle()

    async def test_paused_invalid_proof_does_not_probe_job_store(self) -> None:
        store = ReadTrackingStore()
        service = X402JobService(
            store=store,
            token_secret=TOKEN_SECRET,
            settle=AsyncMock(),
            authorization_used=AsyncMock(),
            report=AsyncMock(),
            stream_work=AsyncMock(),
            accept_new_jobs=False,
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(None, "signature mismatch"),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(raised.exception.code, "payment_rejected")
        self.assertEqual(store.read_calls, 0)


class X402WalletAdmissionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_job_reservation_observer_never_creates_settles_or_releases(
        self,
    ) -> None:
        store = MemoryJobStore()
        limiter = AsyncMock(spec=WalletRateLimiter)
        limiter.reserve.return_value = WalletRateReservation(
            wallet_digest="a" * 64,
            reservation_id=make_service(store=store).derive_identity(
                verified_payment()
            ).job_id,
            reserved_at=NOW,
            state="reserved",
            created_by_caller=False,
        )
        settle = AsyncMock()
        service = make_service(
            store=store,
            settle=settle,
            rate_limiter=limiter,
            reservation_observer_sleep=AsyncMock(),
            reservation_observer_attempts=2,
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(raised.exception.code, "job_state_unavailable")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(store.create_calls, 0)
        settle.assert_not_awaited()


class X402LegacyPaidDurableRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def _seed_legacy_job(
        self,
        service: X402JobService,
        store: MemoryJobStore,
        proof: str,
        request: dict[str, Any] = REQUEST,
    ) -> tuple[VerifiedPayment, CreateJobResult]:
        payment, reason = validate_legacy_paid_payment_proof(
            proof,
            now=NOW // 1_000,
            allow_expired=True,
        )
        self.assertEqual(reason, "")
        assert payment is not None
        identity = service.derive_identity(payment)
        normalized = service._normalize_request(request)
        stored = await store.create(
            {
                "version": 1,
                "jobId": identity.job_id,
                "paymentKey": identity.payment_key,
                "paymentStatus": "settled",
                "paymentMode": "paid",
                "asset": payment.asset.lower(),
                "paymentProofDigest": hashlib.sha256(
                    proof.encode("ascii")
                ).hexdigest(),
                "settlementReference": "0xlegacy",
                "address": payment.from_address,
                "status": "succeeded",
                "request": normalized,
                "jobTokenHash": identity.job_token_hash,
                "competitionEventId": "legacy-event",
                "settledAt": NOW - 1,
                "attempt": 1,
                "leaseOwner": None,
                "leaseExpiresAt": None,
                "createdAt": NOW - 1_000,
                "updatedAt": NOW - 1,
                "expiresAt": NOW + 86_400_000,
                "errorCode": None,
                "retryable": None,
            }
        )
        assert stored is not None
        return payment, CreateJobResult(
            job_id=identity.job_id,
            job_token=identity.job_token,
            status="succeeded",
            expires_at=NOW + 86_400_000,
        )

    async def test_existing_legacy_eip3009_job_recovers_without_side_effects(
        self,
    ) -> None:
        legacy_amount = 210_000_000_000_000_000
        proof = signed_proof(
            value=legacy_amount,
            accepted_overrides={"amount": str(legacy_amount)},
        )
        store = MemoryJobStore()
        limiter = AsyncMock(spec=WalletRateLimiter)
        settle = AsyncMock()
        report = AsyncMock()
        service = make_service(
            store=store,
            rate_limiter=limiter,
            settle=settle,
            report=report,
        )
        _payment, expected = await self._seed_legacy_job(
            service,
            store,
            proof,
        )

        with patch.object(service, "_spawn") as spawn:
            recovered = await service.create_job(proof, REQUEST)

        self.assertEqual(recovered.job_id, expected.job_id)
        self.assertEqual(recovered.job_token, expected.job_token)
        self.assertEqual(recovered.status, "succeeded")
        limiter.reserve.assert_not_awaited()
        limiter.commit.assert_not_awaited()
        limiter.release.assert_not_awaited()
        settle.assert_not_awaited()
        report.assert_not_awaited()
        spawn.assert_not_called()

    async def test_existing_legacy_permit2_job_recovers_without_side_effects(
        self,
    ) -> None:
        legacy_proof, _accepted = permit2_proof(
            USDT_TOKEN,
            amount="210000000000000000",
        )
        proof = encoded_proof(legacy_proof)
        store = MemoryJobStore()
        limiter = AsyncMock(spec=WalletRateLimiter)
        settle = AsyncMock()
        report = AsyncMock()
        service = make_service(
            store=store,
            rate_limiter=limiter,
            settle=settle,
            report=report,
        )
        _payment, expected = await self._seed_legacy_job(
            service,
            store,
            proof,
        )

        with patch.object(service, "_spawn") as spawn:
            recovered = await service.create_job(proof, REQUEST)

        self.assertEqual(recovered.job_id, expected.job_id)
        self.assertEqual(recovered.status, "succeeded")
        limiter.reserve.assert_not_awaited()
        settle.assert_not_awaited()
        report.assert_not_awaited()
        spawn.assert_not_called()

    async def test_legacy_usdc_is_rejected_without_side_effects(self) -> None:
        store = MemoryJobStore()
        limiter = AsyncMock(spec=WalletRateLimiter)
        settle = AsyncMock()
        report = AsyncMock()
        service = make_service(
            store=store,
            rate_limiter=limiter,
            settle=settle,
            report=report,
        )

        for version in ("1", "2"):
            with self.subTest(version=version):
                legacy_proof, _accepted = permit2_proof(
                    USDC_TOKEN,
                    amount="210000000000000000",
                    extra_fields={"version": version},
                )
                proof = encoded_proof(legacy_proof)

                with self.assertRaises(X402JobError) as rejected:
                    await service.create_job(proof, REQUEST)

                self.assertEqual(rejected.exception.code, "payment_rejected")

        self.assertEqual(store.create_calls, 0)
        limiter.reserve.assert_not_awaited()
        limiter.commit.assert_not_awaited()
        limiter.release.assert_not_awaited()
        settle.assert_not_awaited()
        report.assert_not_awaited()

    async def test_legacy_recovery_rejects_no_job_tamper_and_request_mismatch(
        self,
    ) -> None:
        legacy_amount = 210_000_000_000_000_000
        proof = signed_proof(
            value=legacy_amount,
            accepted_overrides={"amount": str(legacy_amount)},
        )
        store = MemoryJobStore()
        limiter = AsyncMock(spec=WalletRateLimiter)
        settle = AsyncMock()
        report = AsyncMock()
        service = make_service(
            store=store,
            rate_limiter=limiter,
            settle=settle,
            report=report,
        )

        with self.assertRaises(X402JobError) as no_job:
            await service.create_job(proof, REQUEST)
        self.assertEqual(no_job.exception.code, "payment_rejected")

        await self._seed_legacy_job(service, store, proof)
        decoded = json.loads(base64.b64decode(proof))
        decoded["resource"]["description"] = "tampered"
        tampered = base64.b64encode(
            json.dumps(decoded, separators=(",", ":")).encode()
        ).decode()
        for candidate, request in (
            (tampered, REQUEST),
            (proof, {"symbols": ["ETH"]}),
        ):
            with self.subTest(candidate=candidate == tampered, request=request):
                with self.assertRaises(X402JobError) as rejected:
                    await service.create_job(candidate, request)
                self.assertEqual(rejected.exception.code, "payment_rejected")

        limiter.reserve.assert_not_awaited()
        limiter.commit.assert_not_awaited()
        limiter.release.assert_not_awaited()
        settle.assert_not_awaited()
        report.assert_not_awaited()


class X402WalletAdmissionContinuationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_creator_reconfirms_durable_reservation_before_settlement(
        self,
    ) -> None:
        store = MemoryJobStore()
        identity = make_service(store=store).derive_identity(verified_payment())
        limiter = AsyncMock(spec=WalletRateLimiter)
        limiter.reserve.return_value = WalletRateReservation(
            wallet_digest="a" * 64,
            reservation_id=identity.job_id,
            reserved_at=NOW,
            state="reserved",
            created_by_caller=True,
        )
        limiter.confirm.side_effect = WalletRateLimitUnavailable("uncertain")
        settle = AsyncMock()
        service = make_service(store=store, settle=settle, rate_limiter=limiter)

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(WalletRateLimitUnavailable),
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(store.create_calls, 1)
        limiter.confirm.assert_awaited_once()
        settle.assert_not_awaited()

    async def test_cancelled_in_flight_create_retains_capacity_until_retry(
        self,
    ) -> None:
        class InFlightCreateStore(MemoryJobStore):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()
                self.release_create = asyncio.Event()
                self.in_flight: set[asyncio.Task[StoredJob | None]] = set()

            async def create(self, record: dict[str, Any]) -> StoredJob | None:
                async def apply_later() -> StoredJob | None:
                    self.started.set()
                    await self.release_create.wait()
                    return await super(InFlightCreateStore, self).create(record)

                task = asyncio.create_task(apply_later())
                self.in_flight.add(task)
                task.add_done_callback(self.in_flight.discard)
                return await asyncio.shield(task)

        store = InFlightCreateStore()
        settle = AsyncMock(
            return_value=SettlementOutcome("settled", transaction="0xtx")
        )
        observer_waiting = asyncio.Event()

        async def observer_sleep(_delay: float) -> None:
            observer_waiting.set()
            await store.release_create.wait()
            await asyncio.sleep(0)

        service = make_service(
            store=store,
            settle=settle,
            reservation_observer_sleep=observer_sleep,
        )
        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            request_task = asyncio.create_task(service.create_job(PROOF, REQUEST))
            await store.started.wait()
            observer_task = asyncio.create_task(
                service.create_job(PROOF, REQUEST)
            )
            await observer_waiting.wait()
            request_task.cancel()
            store.release_create.set()
            with self.assertRaises(asyncio.CancelledError):
                await request_task
            observed = await observer_task
            if store.in_flight:
                await asyncio.gather(*store.in_flight)

        rate_record = next(iter(store.wallet_rate_limits.values())).record
        self.assertEqual(len(rate_record["entries"]), 1)
        self.assertEqual(rate_record["entries"][0]["state"], "reserved")
        self.assertEqual(len(store.jobs), 1)
        self.assertEqual(store.create_calls, 1)
        self.assertEqual(observed.status, "settling")
        settle.assert_not_awaited()

    async def test_thirty_first_job_stops_before_all_paid_side_effects(self) -> None:
        store = MemoryJobStore()
        limiter = WalletRateLimiter(
            store=store,
            token_secret=TOKEN_SECRET,
            clock=lambda: NOW,
        )
        for number in range(30):
            await limiter.reserve(ADDRESS, f"x402_{number:032x}")
        settle = AsyncMock()
        report = AsyncMock()
        service = make_service(
            store=store,
            settle=settle,
            report=report,
            rate_limiter=limiter,
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            patch.object(service, "_spawn") as spawn,
            self.assertRaises(WalletRateLimitExceeded),
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(store.create_calls, 0)
        settle.assert_not_awaited()
        report.assert_not_awaited()
        spawn.assert_not_called()

    async def test_rate_store_uncertainty_stops_before_job_and_settlement(self) -> None:
        store = MemoryJobStore()
        limiter = AsyncMock(spec=WalletRateLimiter)
        limiter.reserve.side_effect = WalletRateLimitUnavailable("s3 down")
        settle = AsyncMock()
        report = AsyncMock()
        service = make_service(
            store=store,
            settle=settle,
            report=report,
            rate_limiter=limiter,
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            patch.object(service, "_spawn") as spawn,
            self.assertRaises(WalletRateLimitUnavailable),
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(store.create_calls, 0)
        settle.assert_not_awaited()
        report.assert_not_awaited()
        spawn.assert_not_called()

    async def test_applied_create_error_keeps_reservation_for_exact_job(self) -> None:
        store = AppliedCreateFailureStore()
        now = [NOW]
        settle = AsyncMock(
            return_value=SettlementOutcome("settled", transaction="0xtx")
        )
        service = make_service(store=store, settle=settle, clock=lambda: now[0])

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            patch.object(service, "_spawn"),
        ):
            first = await service.create_job(PROOF, REQUEST)
            now[0] = STALE_TIME
            retry = await service.create_job(PROOF, REQUEST)

        self.assertEqual(first.status, "settling")
        self.assertEqual(retry.job_id, first.job_id)
        self.assertEqual(retry.status, "queued")
        rate_record = next(iter(store.wallet_rate_limits.values())).record
        self.assertEqual(len(rate_record["entries"]), 1)
        self.assertEqual(rate_record["entries"][0]["state"], "committed")
        settle.assert_awaited_once_with(PROOF, "verify-and-settle")

    async def test_applied_create_with_uncertain_reread_keeps_reservation(self) -> None:
        store = AppliedCreateFailureStore(fail_reread=True)
        settle = AsyncMock()
        service = make_service(store=store, settle=settle)

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(raised.exception.code, "job_state_unavailable")
        self.assertTrue(raised.exception.retryable)
        rate_record = next(iter(store.wallet_rate_limits.values())).record
        self.assertEqual(len(rate_record["entries"]), 1)
        settle.assert_not_awaited()

    async def test_new_job_reserves_persists_settles_commits_then_queues(
        self,
    ) -> None:
        store = MemoryJobStore()

        async def settle(
            _proof: str,
            _mode: str,
        ) -> SettlementOutcome:
            store.events.append("settle")
            return SettlementOutcome("settled", transaction="0xtx")

        report = AsyncMock(return_value=True)
        service = make_service(
            store=store,
            settle=AsyncMock(side_effect=settle),
            report=report,
        )
        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            patch.object(service, "_spawn"),
        ):
            result = await service.create_job(PROOF, REQUEST)
            await wait_for_accounting(service)
            retried = await service.create_job(PROOF, REQUEST)
            await wait_for_accounting(service)

        record = store.jobs[result.job_id].record
        self.assertEqual(
            record["rateLimitReservationId"],
            result.job_id,
        )
        self.assertRegex(record["rateLimitWalletDigest"], r"^[0-9a-f]{64}$")
        self.assertEqual(record["rateLimitReservedAt"], NOW)
        self.assertEqual(record["rateLimitState"], "committed")
        self.assertEqual(record["status"], "queued")
        self.assertEqual(record["settledAt"], NOW)
        self.assertEqual(retried.job_id, result.job_id)
        report.assert_awaited_once_with(
            event_id=expected_competition_event_id(),
            address=ADDRESS,
            called_at=NOW,
        )
        self.assertEqual(
            store.events,
            [
                "rate:create",
                "settle",
                "replace:settling",
                "rate:committed",
                "replace:queued",
            ],
        )

    async def test_explicit_rejection_releases_durable_reservation(self) -> None:
        store = MemoryJobStore()
        service = make_service(
            store=store,
            settle=AsyncMock(
                return_value=SettlementOutcome(
                    "rejected",
                    reason="authorization rejected",
                )
            ),
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        identity = service.derive_identity(verified_payment())
        record = store.jobs[identity.job_id].record
        self.assertEqual(raised.exception.code, "payment_rejected")
        self.assertEqual(record["paymentStatus"], "failed")
        self.assertEqual(record["rateLimitState"], "released")
        rate_record = next(iter(store.wallet_rate_limits.values())).record
        self.assertEqual(rate_record["entries"], [])

    async def test_rejection_release_failure_is_retryable_and_redriven(self) -> None:
        store = MemoryJobStore()
        durable = WalletRateLimiter(
            store=store,
            token_secret=TOKEN_SECRET,
            clock=lambda: NOW,
        )

        class ReleaseFailsOnce:
            def __init__(self) -> None:
                self.reserve_calls = 0
                self.release_calls = 0

            async def reserve(self, wallet: str, reservation_id: str):
                self.reserve_calls += 1
                return await durable.reserve(wallet, reservation_id)

            async def commit(self, reservation) -> None:
                await durable.commit(reservation)

            async def confirm(self, reservation) -> str:
                return await durable.confirm(reservation)

            async def release(self, reservation) -> None:
                self.release_calls += 1
                if self.release_calls == 1:
                    raise WalletRateLimitUnavailable("release uncertain")
                await durable.release(reservation)

        limiter = ReleaseFailsOnce()
        settle = AsyncMock(
            return_value=SettlementOutcome(
                "rejected",
                reason="authorization rejected",
            )
        )
        service = make_service(
            store=store,
            settle=settle,
            rate_limiter=limiter,
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(WalletRateLimitUnavailable),
        ):
            await service.create_job(PROOF, REQUEST)

        failed = next(iter(store.jobs.values())).record
        self.assertEqual(failed["paymentStatus"], "failed")
        self.assertEqual(failed["rateLimitState"], "reserved")

        with patch(
            "x402_job_service.validate_payment_proof",
            return_value=(verified_payment(), ""),
        ):
            retried = await service.create_job(PROOF, REQUEST)

        self.assertEqual(retried.status, "failed")
        self.assertEqual(next(iter(store.jobs.values())).record["rateLimitState"], "released")
        self.assertEqual(limiter.reserve_calls, 1)
        self.assertEqual(limiter.release_calls, 2)
        settle.assert_awaited_once()

    async def test_indeterminate_settlement_keeps_reserved_metadata(self) -> None:
        store = MemoryJobStore()
        service = make_service(
            store=store,
            settle=AsyncMock(side_effect=ConnectionError("response lost")),
        )

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(SettlementIndeterminate),
        ):
            await service.create_job(PROOF, REQUEST)

        record = next(iter(store.jobs.values())).record
        self.assertEqual(record["status"], "settling")
        self.assertEqual(record["paymentStatus"], "settling")
        self.assertEqual(record["rateLimitState"], "reserved")
        rate_record = next(iter(store.wallet_rate_limits.values())).record
        self.assertEqual(rate_record["entries"][0]["state"], "reserved")

    async def test_stale_retry_confirms_durable_reservation_before_settlement(
        self,
    ) -> None:
        store = MemoryJobStore()
        now = [NOW]
        settle = AsyncMock(
            side_effect=[
                ConnectionError("response lost"),
                SettlementOutcome("settled", transaction="0xtx"),
            ]
        )
        service = make_service(
            store=store,
            settle=settle,
            clock=lambda: now[0],
        )
        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(SettlementIndeterminate),
        ):
            await service.create_job(PROOF, REQUEST)

        store.wallet_rate_limits.clear()
        now[0] = STALE_TIME
        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(WalletRateLimitUnavailable),
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(settle.await_count, 1)

    async def test_rate_limited_exact_retry_rejects_changed_request_binding(
        self,
    ) -> None:
        store = MemoryJobStore()
        settle = AsyncMock(side_effect=ConnectionError("response lost"))
        service = make_service(store=store, settle=settle)
        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(SettlementIndeterminate),
        ):
            await service.create_job(PROOF, REQUEST)

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, {"symbols": "ETH"})

        self.assertEqual(raised.exception.code, "payment_rejected")
        self.assertEqual(settle.await_count, 1)

    async def test_ambiguous_job_create_failure_retains_reservation(self) -> None:
        class FailingCreateStore(MemoryJobStore):
            async def create(self, record: dict[str, Any]) -> StoredJob | None:
                raise RuntimeError("job persistence unavailable")

        store = FailingCreateStore()
        service = make_service(store=store)

        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(X402JobError) as raised,
        ):
            await service.create_job(PROOF, REQUEST)

        self.assertEqual(raised.exception.code, "job_state_unavailable")
        self.assertTrue(raised.exception.retryable)
        rate_record = next(iter(store.wallet_rate_limits.values())).record
        self.assertEqual(len(rate_record["entries"]), 1)
        self.assertEqual(rate_record["entries"][0]["state"], "reserved")

    async def test_exact_retry_of_every_durable_status_does_not_reserve(self) -> None:
        for status in ("queued", "running", "succeeded", "failed", "settling"):
            with self.subTest(status=status):
                store = MemoryJobStore()
                limiter = AsyncMock(spec=WalletRateLimiter)
                service = make_service(store=store, rate_limiter=limiter)
                if status == "settling":
                    await seed_settling_job(
                        service,
                        store,
                        lease_expires_at=NOW + 1,
                        proof_header=PROOF,
                    )
                else:
                    await seed_execution_job(
                        service,
                        store,
                        status=status,
                        retryable=False,
                    )
                with (
                    patch(
                        "x402_job_service.validate_payment_proof",
                        return_value=(verified_payment(), ""),
                    ),
                    patch.object(service, "_spawn"),
                ):
                    await service.create_job(PROOF, REQUEST)
                limiter.reserve.assert_not_awaited()

    async def test_commit_failure_recovers_without_second_settlement(self) -> None:
        store = MemoryJobStore()
        durable = WalletRateLimiter(
            store=store,
            token_secret=TOKEN_SECRET,
            clock=lambda: NOW,
        )
        first_commit = True

        class CommitFailsOnce:
            async def reserve(self, wallet: str, reservation_id: str):
                return await durable.reserve(wallet, reservation_id)

            async def commit(self, reservation) -> None:
                nonlocal first_commit
                if first_commit:
                    first_commit = False
                    raise WalletRateLimitUnavailable("temporary")
                await durable.commit(reservation)

            async def confirm(self, reservation) -> str:
                return await durable.confirm(reservation)

            async def release(self, reservation) -> None:
                await durable.release(reservation)

        settle = AsyncMock(
            return_value=SettlementOutcome("settled", transaction="0xtx")
        )
        first = make_service(
            store=store,
            settle=settle,
            rate_limiter=CommitFailsOnce(),
        )
        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            self.assertRaises(WalletRateLimitUnavailable),
        ):
            await first.create_job(PROOF, REQUEST)

        pending = next(iter(store.jobs.values())).record
        settled_at = pending["settledAt"]
        event_id = pending["competitionEventId"]
        self.assertEqual(pending["paymentStatus"], "settled")
        self.assertEqual(pending["status"], "settling")
        self.assertEqual(pending["rateLimitState"], "reserved")

        restarted = make_service(
            store=store,
            settle=settle,
            rate_limiter=durable,
        )
        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            patch.object(restarted, "_spawn"),
        ):
            recovered = await restarted.create_job(PROOF, REQUEST)

        final = store.jobs[recovered.job_id].record
        self.assertEqual(final["rateLimitState"], "committed")
        self.assertEqual(final["status"], "queued")
        self.assertEqual(final["settledAt"], settled_at)
        self.assertEqual(final["competitionEventId"], event_id)
        settle.assert_awaited_once()

    async def test_same_job_create_race_reserves_and_settles_once(self) -> None:
        store = MemoryJobStore()
        settle = AsyncMock(
            return_value=SettlementOutcome("settled", transaction="0xtx")
        )
        service = make_service(store=store, settle=settle)
        with (
            patch(
                "x402_job_service.validate_payment_proof",
                return_value=(verified_payment(), ""),
            ),
            patch.object(service, "_spawn"),
        ):
            await asyncio.gather(
                service.create_job(PROOF, REQUEST),
                service.create_job(PROOF, REQUEST),
            )

        rate_record = next(iter(store.wallet_rate_limits.values())).record
        self.assertEqual(len(rate_record["entries"]), 1)
        self.assertEqual(rate_record["entries"][0]["state"], "committed")
        settle.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
