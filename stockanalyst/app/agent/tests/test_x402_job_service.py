from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
import unittest
from dataclasses import replace
from typing import Any
from unittest.mock import ANY, AsyncMock, patch

import httpx

from x402_handler import _settle_generic
from x402_job_service import (
    CreateJobResult,
    JobIdentityCollision,
    X402JobError,
    X402JobService,
    load_job_token_secret,
)
from x402_job_store import JobConflict, StoredJob
from x402_verify import CHAIN_ID, VerifiedPayment, validate_payment_proof

from tests.test_x402_verify import NOW as SIGNED_NOW
from tests.test_x402_verify import signed_proof


ADDRESS = "0x1111111111111111111111111111111111111111"
NONCE = "0x" + "22" * 32
PROOF = "signed-proof"
REQUEST = {"symbols": "bnb, btc-usd"}
NOW = 2_000_000_000_000
STALE_TIME = NOW + 120_001
TOKEN_SECRET = b"test-only-token-secret-with-32-bytes"


def verified_payment(*, valid_before: int = NOW // 1000 + 600) -> VerifiedPayment:
    return VerifiedPayment(
        proof={},
        from_address=ADDRESS,
        to_address="0x2222222222222222222222222222222222222222",
        value=10**18,
        valid_after=NOW // 1000 - 60,
        valid_before=valid_before,
        nonce=NONCE,
        nonce_bytes=bytes.fromhex(NONCE.removeprefix("0x")),
    )


class MemoryJobStore:
    def __init__(self, *, synchronize_creates: bool = False) -> None:
        self.jobs: dict[str, StoredJob] = {}
        self.create_calls = 0
        self.replace_calls = 0
        self.presign_calls = 0
        self.reports: dict[tuple[str, str | None], str] = {}
        self.accounting_markers: dict[str, dict[str, Any]] = {}
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

    return X402JobService(
        store=store or MemoryJobStore(),
        token_secret=TOKEN_SECRET,
        settle=settle or AsyncMock(return_value=(True, "0xtx")),
        authorization_used=authorization_used or AsyncMock(return_value=False),
        report=report or AsyncMock(return_value=True),
        stream_work=stream_work or idle_stream,
        clock=clock or (lambda: NOW),
        owner=owner,
        analysis_timeout_seconds=analysis_timeout_seconds,
        heartbeat_sleep=heartbeat_sleep,
        accounting_sleep=accounting_sleep or immediate_accounting_sleep,
        accounting_retry_attempts=accounting_retry_attempts,
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
) -> StoredJob:
    selected_payment = payment or verified_payment()
    identity = service.derive_identity(selected_payment)
    stored = await store.create(
        {
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
    )
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
        store = MemoryJobStore(synchronize_creates=True)
        settle = AsyncMock(return_value=(True, "0xtx"))
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
        settle.assert_awaited_once_with(PROOF)
        self.assertEqual(first.status, "queued")

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
            event_id=f"b402:{CHAIN_ID}:{ADDRESS}:{NONCE}",
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
        service = make_service(
            store=store,
            settle=AsyncMock(return_value=(False, "authorization rejected")),
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
        authorization_used.assert_awaited_once()
        settle.assert_not_awaited()
        report.assert_awaited_once()

    async def test_stale_settling_unused_authorization_retries_settlement(
        self,
    ) -> None:
        store = MemoryJobStore()
        authorization_used = AsyncMock(return_value=False)
        settle = AsyncMock(return_value=(True, "0xreconciled"))
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
        settle.assert_awaited_once_with(PROOF)
        stored = store.jobs[result.job_id].record
        self.assertEqual(stored["settlementReference"], "0xreconciled")

    async def test_concurrent_stale_claims_allow_one_facilitator_owner(self) -> None:
        store = ReconciliationRaceStore()
        authorization_used = AsyncMock(return_value=False)
        settle = AsyncMock(return_value=(True, "0xreconciled"))
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
        settle.assert_awaited_once_with(PROOF)

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
        settle = AsyncMock(return_value=(True, "0xtx"))
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

        settle.assert_awaited_once_with(PROOF)
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
        expected_event = f"b402:{CHAIN_ID}:{ADDRESS}:{NONCE}"
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

        async def ambiguous_settle(_proof: str) -> tuple[bool, str]:
            nonlocal submissions
            submissions += 1
            # Two HTTP submissions may observe the same successful nonce use;
            # they cannot represent two EIP-3009 transfers.
            if submissions == 1:
                first_submission_started.set()
                await release_first_submission.wait()
                return True, "0xeip3009-transfer"
            return True, "0xeip3009-transfer"

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
        ) -> tuple[bool, str]:
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
        ) -> tuple[bool, str]:
            self.assertEqual(timeout, 60)
            awaitable.close()
            raise asyncio.TimeoutError

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

        async def settle(_proof_header: str) -> tuple[bool, str]:
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

    async def test_missing_report_is_a_deterministic_failure(self) -> None:
        store = MemoryJobStore()

        async def no_report(
            _prompt: str,
            _session_id: str,
            _symbols: list[str],
        ) -> Any:
            yield "progress", {"stage": "collecting"}
            yield "done", {}

        service = make_service(store=store, stream_work=no_report)
        created = await seed_execution_job(
            service,
            store,
            status="queued",
        )

        service._spawn(created.job_id)
        await service.wait_for_idle()

        failed = store.jobs[created.job_id].record
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["errorCode"], "analysis_failed")
        self.assertFalse(failed["retryable"])
        self.assertFalse(service.is_busy())

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


if __name__ == "__main__":
    unittest.main()
