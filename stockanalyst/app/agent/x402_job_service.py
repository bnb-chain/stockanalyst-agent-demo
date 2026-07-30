from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from prompt_builder import _build_stock_analysis_prompt
from x402_job_store import JobConflict, StoredJob, X402JobStore
from x402_verify import CHAIN_ID, VerifiedPayment, validate_payment_proof

logger = logging.getLogger("seller-agent.x402.jobs")

_JOB_ID_RE = re.compile(r"x402_[0-9a-f]{32}\Z")
_HEARTBEAT_SECONDS = 30
_LEASE_MILLISECONDS = 120_000
_STALE_MILLISECONDS = 120_000
_DOWNLOAD_URL_MILLISECONDS = 1_800_000
_DEFAULT_ANALYSIS_TIMEOUT_SECONDS = 15 * 60
_MAX_EXECUTION_ATTEMPTS = 3
_CLAIM_DRIVE_ATTEMPTS = 3
_DEFAULT_ACCOUNTING_RETRY_ATTEMPTS = 3
_MAX_ACCOUNTING_BACKOFF_SECONDS = 30.0


class X402JobError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class JobIdentityCollision(X402JobError):
    pass


class SettlementIndeterminate(X402JobError):
    def __init__(self) -> None:
        super().__init__("settlement_pending", retryable=True)


@dataclass(frozen=True)
class JobIdentity:
    payment_key: str
    job_id: str
    job_token: str
    job_token_hash: str


@dataclass(frozen=True)
class CreateJobResult:
    job_id: str
    job_token: str
    status: str
    expires_at: int


@dataclass(frozen=True)
class JobView:
    job_id: str
    status: str
    expires_at: int
    error_code: str | None = None
    retryable: bool | None = None
    download_url: str | None = None
    download_url_expires_at: int | None = None


class _MissingReport(RuntimeError):
    pass


def load_job_token_secret(env: Mapping[str, str] = os.environ) -> bytes:
    value = env.get("X402_JOB_TOKEN_SECRET", "")
    secret = value.encode("utf-8")
    if len(secret) < 32:
        raise X402JobError("X402_JOB_TOKEN_SECRET must contain at least 32 bytes")
    return secret


class X402JobService:
    def __init__(
        self,
        *,
        store: X402JobStore,
        token_secret: bytes,
        settle: Callable[[str], Awaitable[tuple[bool, str]]],
        authorization_used: Callable[[VerifiedPayment], Awaitable[bool]],
        report: Callable[..., Awaitable[bool]],
        stream_work: Callable[..., Any],
        clock: Callable[[], int] | None = None,
        owner: str | None = None,
        accept_new_jobs: bool = True,
        analysis_timeout_seconds: float = _DEFAULT_ANALYSIS_TIMEOUT_SECONDS,
        heartbeat_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        accounting_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        accounting_retry_attempts: int = _DEFAULT_ACCOUNTING_RETRY_ATTEMPTS,
    ) -> None:
        self._store = store
        self._token_secret = token_secret
        self._settle = settle
        self._authorization_used = authorization_used
        self._report = report
        self._stream_work = stream_work
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._owner = owner or secrets.token_hex(16)
        self._accept_new_jobs = accept_new_jobs
        self._analysis_timeout_seconds = analysis_timeout_seconds
        self._heartbeat_sleep = heartbeat_sleep
        self._accounting_sleep = accounting_sleep
        if not 1 <= accounting_retry_attempts <= 10:
            raise X402JobError("invalid_accounting_retry_attempts")
        self._accounting_retry_attempts = accounting_retry_attempts
        self._tasks_lock = threading.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._accounting_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_tasks: set[asyncio.Task[None]] = set()

    @property
    def accept_new_jobs(self) -> bool:
        return self._accept_new_jobs

    @staticmethod
    def _token_matches(record: dict[str, Any], token: str) -> bool:
        token_text = token if isinstance(token, str) else ""
        supplied = hashlib.sha256(token_text.encode()).hexdigest()
        expected = str(record.get("jobTokenHash", ""))
        return hmac.compare_digest(supplied, expected)

    async def _authorized_job(
        self,
        job_id: str,
        token: str,
    ) -> StoredJob:
        if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
            raise X402JobError("job_not_found")
        stored = await self._store.read(job_id)
        if stored is None or not self._token_matches(stored.record, token):
            raise X402JobError("job_not_found")
        if int(stored.record.get("expiresAt") or 0) <= int(self._clock()):
            raise X402JobError("job_expired")
        return stored

    async def get_job(self, job_id: str, token: str) -> JobView:
        stored = await self._authorized_job(job_id, token)
        record = stored.record
        status = str(record["status"])
        if status == "queued":
            self._spawn(job_id)
        await self._redrive_accounting(stored)
        expires_at = int(record["expiresAt"])
        error_code = None
        retryable = None
        download_url = None
        download_url_expires_at = None
        if status == "failed":
            error_code = str(record.get("errorCode") or "analysis_failed")
            retryable = bool(record.get("retryable"))
        elif status == "succeeded":
            report_id = record.get("reportId")
            if isinstance(report_id, str):
                download_url = await self._store.presign_report(
                    job_id,
                    report_id=report_id,
                )
            else:
                download_url = await self._store.presign_report(job_id)
            download_url_expires_at = (
                int(self._clock()) + _DOWNLOAD_URL_MILLISECONDS
            )
        return JobView(
            job_id=job_id,
            status=status,
            expires_at=expires_at,
            error_code=error_code,
            retryable=retryable,
            download_url=download_url,
            download_url_expires_at=download_url_expires_at,
        )

    async def resume_job(self, job_id: str, token: str) -> JobView:
        stored = await self._authorized_job(job_id, token)
        await self._redrive_accounting(stored)
        now = int(self._clock())
        record = stored.record
        status = str(record["status"])
        if int(record.get("attempt") or 0) >= _MAX_EXECUTION_ATTEMPTS:
            raise X402JobError("attempts_exhausted")
        if status == "queued":
            self._spawn(job_id)
            return await self.get_job(job_id, token)
        stale = (
            status == "running"
            and int(record.get("updatedAt") or 0)
            <= now - _STALE_MILLISECONDS
        )
        allowed_failure = (
            status == "failed" and record.get("retryable") is True
        )
        if not stale and not allowed_failure:
            raise X402JobError("job_conflict")
        queued = {
            **record,
            "status": "queued",
            "leaseOwner": None,
            "leaseExpiresAt": None,
            "updatedAt": now,
            "errorCode": None,
            "retryable": None,
        }
        resume = asyncio.create_task(
            self._apply_resume_and_handoff(stored, queued),
            name=f"x402-resume:{job_id}",
        )
        try:
            await asyncio.shield(resume)
        except asyncio.CancelledError:
            await asyncio.gather(resume, return_exceptions=True)
            raise
        return await self.get_job(job_id, token)

    async def _apply_resume_and_handoff(
        self,
        stored: StoredJob,
        queued: dict[str, Any],
    ) -> None:
        try:
            await self._store.replace(stored, queued)
        except JobConflict as exc:
            raise X402JobError("job_conflict") from exc
        await self._handoff_and_spawn(str(queued["jobId"]))

    async def _handoff_and_spawn(self, job_id: str) -> None:
        with self._tasks_lock:
            existing = self._tasks.get(job_id)
            existing_is_active = existing in self._active_tasks
        if existing is not None and existing_is_active:
            existing.cancel()
            await asyncio.gather(existing, return_exceptions=True)
        self._spawn(job_id)

    def _spawn(self, job_id: str) -> None:
        with self._tasks_lock:
            existing = self._tasks.get(job_id)
            if existing is not None and existing in self._active_tasks:
                return
            task = asyncio.create_task(
                self._run_job(job_id),
                name=f"x402-job:{job_id}",
            )
            self._tasks[job_id] = task
            self._active_tasks.add(task)
        task.add_done_callback(
            lambda done, key=job_id: self._task_done(key, done)
        )

    def _task_done(self, job_id: str, task: asyncio.Task[None]) -> None:
        with self._tasks_lock:
            self._active_tasks.discard(task)
            if self._tasks.get(job_id) is task:
                self._tasks.pop(job_id, None)
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    def _accounting_task_done(
        self,
        job_id: str,
        task: asyncio.Task[None],
    ) -> None:
        with self._tasks_lock:
            self._active_tasks.discard(task)
            if self._accounting_tasks.get(job_id) is task:
                self._accounting_tasks.pop(job_id, None)
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    def is_busy(self) -> bool:
        with self._tasks_lock:
            return bool(self._active_tasks)

    async def wait_for_idle(self) -> None:
        while True:
            with self._tasks_lock:
                active = tuple(self._active_tasks)
            if not active:
                return
            await asyncio.gather(
                *active,
                return_exceptions=True,
            )
            await asyncio.sleep(0)

    @staticmethod
    def _canonical_payment_parts(
        payment: VerifiedPayment,
    ) -> tuple[str, str]:
        address_text = payment.from_address.strip()
        if address_text[:2].lower() == "0x":
            address_text = address_text[2:]
        try:
            address_bytes = bytes.fromhex(address_text)
            nonce_bytes = bytes(payment.nonce_bytes)
        except (TypeError, ValueError) as exc:
            raise X402JobError("invalid_payment_identity") from exc
        if (
            len(address_text) != 40
            or len(address_bytes) != 20
            or len(nonce_bytes) != 32
        ):
            raise X402JobError("invalid_payment_identity")
        return "0x" + address_bytes.hex(), "0x" + nonce_bytes.hex()

    def derive_identity(self, payment: VerifiedPayment) -> JobIdentity:
        canonical_address, canonical_nonce = self._canonical_payment_parts(
            payment
        )
        material = (
            str(CHAIN_ID).encode()
            + b":"
            + canonical_address.encode()
            + b":"
            + canonical_nonce.encode()
        )
        payment_key = hashlib.sha256(material).hexdigest()
        token_bytes = hmac.new(
            self._token_secret,
            b"x402-job-token:" + payment_key.encode(),
            hashlib.sha256,
        ).digest()
        job_token = base64.urlsafe_b64encode(token_bytes).decode().rstrip("=")
        return JobIdentity(
            payment_key=payment_key,
            job_id=f"x402_{payment_key[:32]}",
            job_token=job_token,
            job_token_hash=hashlib.sha256(job_token.encode()).hexdigest(),
        )

    def _normalize_request(self, request: dict[str, Any]) -> dict[str, Any]:
        raw_symbols = request.get("symbols")
        if isinstance(raw_symbols, str):
            symbols = [
                value.strip().upper()
                for value in raw_symbols.split(",")
                if value.strip()
            ]
        elif isinstance(raw_symbols, list):
            symbols = [
                str(value).strip().upper()
                for value in raw_symbols
                if str(value).strip()
            ]
        else:
            symbols = []
        if not 1 <= len(symbols) <= 10 or any(
            len(symbol) > 20
            or not all(ch.isalnum() or ch in ".^-" for ch in symbol)
            for symbol in symbols
        ):
            raise X402JobError("invalid_request")
        analysis_type = str(request.get("analysis_type") or "comprehensive")
        portfolio = request.get("portfolio") or []
        risk_profile = request.get("risk_profile") or {}
        if (
            len(analysis_type) > 64
            or not isinstance(portfolio, list)
            or not isinstance(risk_profile, dict)
        ):
            raise X402JobError("invalid_request")
        try:
            json.dumps(
                {"portfolio": portfolio, "riskProfile": risk_profile},
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise X402JobError("invalid_request") from exc
        return {
            "symbols": symbols,
            "analysisType": analysis_type,
            "portfolio": portfolio,
            "riskProfile": risk_profile,
        }

    async def create_job(
        self,
        proof_header: str,
        request: dict[str, Any],
    ) -> CreateJobResult:
        now = int(self._clock())
        payment, _reason = validate_payment_proof(proof_header, now=now // 1000)
        expired_recovery = False
        if payment is None:
            if _reason != "authorization expired":
                raise X402JobError("payment_rejected")
            payment, _recovery_reason = validate_payment_proof(
                proof_header,
                now=now // 1000,
                allow_expired=True,
            )
            if payment is None:
                raise X402JobError("payment_rejected")
            expired_recovery = True
        identity = self.derive_identity(payment)
        stored = await self._store.read(identity.job_id)
        if stored is not None:
            if stored.record.get("paymentKey") != identity.payment_key:
                raise JobIdentityCollision("job_identity_collision")
            stored = await self._reconcile_settling(
                stored,
                payment,
                proof_header,
            )
            stored = await self._retry_accounting(stored, payment)
            return self._create_result_and_spawn(identity, stored)
        if expired_recovery:
            raise X402JobError("payment_rejected")
        if not self._accept_new_jobs:
            raise X402JobError("async_jobs_paused", retryable=True)

        normalized_request = self._normalize_request(request)
        competition_event_id = self._competition_event_id(payment)
        record = {
            "version": 1,
            "jobId": identity.job_id,
            "paymentKey": identity.payment_key,
            "paymentStatus": "settling",
            "settlementReference": None,
            "reportId": None,
            "address": payment.from_address,
            "status": "settling",
            "request": normalized_request,
            "jobTokenHash": identity.job_token_hash,
            "competitionEventId": competition_event_id,
            "settledAt": None,
            "attempt": 0,
            "leaseOwner": self._owner,
            "leaseExpiresAt": now + 120_000,
            "createdAt": now,
            "updatedAt": now,
            "expiresAt": now + 7 * 24 * 60 * 60 * 1000,
            "errorCode": None,
            "retryable": None,
        }
        stored = await self._store.create(record)
        if stored is None:
            stored = await self._require_existing_identity(identity)
            stored = await self._reconcile_settling(
                stored,
                payment,
                proof_header,
            )
        else:
            ok, settlement_reference = await self._settle_payment(proof_header)
            if not ok:
                await self._fail_settlement(stored)
                raise X402JobError("payment_rejected")
            stored = await self._mark_settled(
                stored,
                payment,
                settlement_reference,
            )
        stored = await self._retry_accounting(stored, payment)
        return self._create_result_and_spawn(identity, stored)

    def _create_result_and_spawn(
        self,
        identity: JobIdentity,
        stored: StoredJob,
    ) -> CreateJobResult:
        result = self._create_result(identity, stored)
        if result.status == "queued":
            self._spawn(identity.job_id)
        return result

    @staticmethod
    def _create_result(
        identity: JobIdentity,
        stored: StoredJob,
    ) -> CreateJobResult:
        return CreateJobResult(
            job_id=identity.job_id,
            job_token=identity.job_token,
            status=str(stored.record["status"]),
            expires_at=int(stored.record["expiresAt"]),
        )

    async def _require_existing_identity(
        self,
        identity: JobIdentity,
    ) -> StoredJob:
        stored = await self._store.read(identity.job_id)
        if stored is None:
            raise X402JobError("job_state_unavailable", retryable=True)
        if stored.record.get("paymentKey") != identity.payment_key:
            raise JobIdentityCollision("job_identity_collision")
        return stored

    async def _reconcile_settling(
        self,
        stored: StoredJob,
        payment: VerifiedPayment,
        proof_header: str,
    ) -> StoredJob:
        if (
            stored.record.get("status") != "settling"
            or stored.record.get("paymentStatus") != "settling"
        ):
            return stored

        now = int(self._clock())
        lease_expires_at = stored.record.get("leaseExpiresAt")
        if isinstance(lease_expires_at, int) and lease_expires_at > now:
            return stored

        claimed = {
            **stored.record,
            "leaseOwner": self._owner,
            "leaseExpiresAt": now + 120_000,
            "updatedAt": now,
        }
        try:
            stored = await self._store.replace(stored, claimed)
        except JobConflict:
            identity = self.derive_identity(payment)
            return await self._require_existing_identity(identity)

        if await self._authorization_used(payment):
            return await self._mark_settled(stored, payment, None)

        if now // 1000 >= payment.valid_before:
            await self._fail_settlement(stored)
            raise X402JobError("payment_rejected")

        ok, settlement_reference = await self._settle_payment(proof_header)
        if not ok:
            await self._fail_settlement(stored)
            raise X402JobError("payment_rejected")
        return await self._mark_settled(
            stored,
            payment,
            settlement_reference,
        )

    async def _mark_settled(
        self,
        stored: StoredJob,
        payment: VerifiedPayment,
        settlement_reference: str | None,
    ) -> StoredJob:
        existing_settled_at = stored.record.get("settledAt")
        settled_at = (
            existing_settled_at
            if isinstance(existing_settled_at, int)
            and existing_settled_at > 0
            else int(self._clock())
        )
        settled = {
            **stored.record,
            "paymentStatus": "settled",
            "settlementReference": settlement_reference,
            "status": "queued",
            "competitionEventId": self._competition_event_id(payment),
            "settledAt": settled_at,
            "leaseOwner": None,
            "leaseExpiresAt": None,
            "updatedAt": int(self._clock()),
        }
        settled.pop("competitionReported", None)
        settled.pop("competitionReportedAt", None)
        return await self._store.replace(stored, settled)

    async def _fail_settlement(self, stored: StoredJob) -> StoredJob:
        failed = {
            **stored.record,
            "paymentStatus": "failed",
            "status": "failed",
            "leaseOwner": None,
            "leaseExpiresAt": None,
            "updatedAt": int(self._clock()),
            "errorCode": "payment_failed",
            "retryable": False,
        }
        return await self._store.replace(stored, failed)

    async def _settle_payment(
        self,
        proof_header: str,
    ) -> tuple[bool, str]:
        try:
            result = await asyncio.wait_for(
                self._settle(proof_header),
                timeout=60,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise SettlementIndeterminate() from exc
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], bool)
            or not isinstance(result[1], str)
        ):
            raise SettlementIndeterminate()
        return result

    def _competition_event_id(self, payment: VerifiedPayment) -> str:
        canonical_address, canonical_nonce = self._canonical_payment_parts(
            payment
        )
        return f"b402:{CHAIN_ID}:{canonical_address}:{canonical_nonce}"

    async def _retry_accounting(
        self,
        stored: StoredJob,
        _payment: VerifiedPayment,
    ) -> StoredJob:
        await self._redrive_accounting(stored)
        return stored

    @staticmethod
    def _accounting_is_pending(record: dict[str, Any]) -> bool:
        event_id = record.get("competitionEventId")
        settled_at = record.get("settledAt")
        return (
            record.get("paymentStatus") == "settled"
            and isinstance(event_id, str)
            and bool(event_id)
            and not isinstance(settled_at, bool)
            and isinstance(settled_at, int)
            and settled_at > 0
        )

    async def _redrive_accounting(self, stored: StoredJob) -> None:
        if not self._accounting_is_pending(stored.record):
            return
        job_id = str(stored.record.get("jobId") or "")
        try:
            marker = await self._store.read_accounting_marker(job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("x402 competition accounting failed")
            self._spawn_accounting(job_id, delay_before_first=True)
            return
        if marker is None:
            self._spawn_accounting(job_id)
            return
        if not self._accounting_marker_matches(stored.record, marker):
            logger.warning("x402 competition accounting failed")

    def _spawn_accounting(
        self,
        job_id: str,
        *,
        delay_before_first: bool = False,
    ) -> None:
        if not _JOB_ID_RE.fullmatch(job_id):
            return
        with self._tasks_lock:
            existing = self._accounting_tasks.get(job_id)
            if existing is not None and existing in self._active_tasks:
                return
            task = asyncio.create_task(
                self._run_accounting(
                    job_id,
                    delay_before_first=delay_before_first,
                ),
                name=f"x402-accounting:{job_id}",
            )
            self._accounting_tasks[job_id] = task
            self._active_tasks.add(task)
        task.add_done_callback(
            lambda done, key=job_id: self._accounting_task_done(key, done)
        )

    async def _run_accounting(
        self,
        job_id: str,
        *,
        delay_before_first: bool,
    ) -> None:
        delay_index = 0
        for attempt in range(self._accounting_retry_attempts):
            if delay_before_first or attempt > 0:
                delay = min(
                    float(2**delay_index),
                    _MAX_ACCOUNTING_BACKOFF_SECONDS,
                )
                delay_index += 1
                await self._accounting_sleep(delay)
                delay_before_first = False
            try:
                stored = await self._store.read(job_id)
                if stored is None or not self._accounting_is_pending(
                    stored.record
                ):
                    return
                marker = await self._store.read_accounting_marker(job_id)
                if marker is not None:
                    if not self._accounting_marker_matches(
                        stored.record,
                        marker,
                    ):
                        logger.warning("x402 competition accounting failed")
                    return
                if not await self._report_accounting_record(stored.record):
                    continue
                marker = {
                    "version": 1,
                    "eventId": str(
                        stored.record.get("competitionEventId") or ""
                    ),
                    "settledAt": int(stored.record.get("settledAt") or 0),
                }
                created = await self._store.create_accounting_marker(
                    job_id,
                    marker,
                )
                if not created:
                    existing = await self._store.read_accounting_marker(job_id)
                    if (
                        existing is None
                        or not self._accounting_marker_matches(
                            stored.record,
                            existing,
                        )
                    ):
                        logger.warning("x402 competition accounting failed")
                        return
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("x402 competition accounting failed")

    @staticmethod
    def _accounting_marker_matches(
        record: dict[str, Any],
        marker: dict[str, Any],
    ) -> bool:
        return (
            marker.get("version") == 1
            and marker.get("eventId") == record.get("competitionEventId")
            and marker.get("settledAt") == record.get("settledAt")
        )

    async def _report_accounting_record(
        self,
        record: dict[str, Any],
    ) -> bool:
        event_id = record.get("competitionEventId")
        address = record.get("address")
        settled_at = record.get("settledAt")
        if (
            not isinstance(event_id, str)
            or not event_id
            or not isinstance(address, str)
            or not address
            or not isinstance(settled_at, int)
            or settled_at <= 0
        ):
            logger.warning("x402 competition accounting failed")
            return False
        try:
            reported = await self._report(
                event_id=event_id,
                address=address,
                called_at=settled_at,
            )
        except Exception:
            logger.warning("x402 competition accounting failed")
            return False
        if not reported:
            logger.warning("x402 competition accounting failed")
            return False
        return True

    async def _run_job(self, job_id: str) -> None:
        try:
            claimed = await self._drive_execution_claim(job_id)
            if claimed is None:
                return
            running, lease_owner = claimed
            await self._redrive_accounting(running)
            holder = [running]
            lease_lost = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._heartbeat(holder, lease_owner, lease_lost),
                name=f"x402-heartbeat:{job_id}",
            )
            try:
                try:
                    markdown = await asyncio.wait_for(
                        self._consume_report(running.record),
                        timeout=self._analysis_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    await self._stop_heartbeat(heartbeat)
                    await self._fail_execution(
                        holder[0],
                        lease_owner,
                        lease_lost,
                        error_code="analysis_timeout",
                        retryable=True,
                    )
                    return
                except _MissingReport:
                    await self._stop_heartbeat(heartbeat)
                    await self._fail_execution(
                        holder[0],
                        lease_owner,
                        lease_lost,
                        error_code="analysis_failed",
                        retryable=False,
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._stop_heartbeat(heartbeat)
                    await self._fail_execution(
                        holder[0],
                        lease_owner,
                        lease_lost,
                        error_code="analysis_failed",
                        retryable=True,
                    )
                    return

                await self._stop_heartbeat(heartbeat)
                if lease_lost.is_set():
                    return
                fenced = await self._refresh_lease(
                    holder[0],
                    lease_owner,
                )
                if fenced is None:
                    return
                holder[0] = fenced
                try:
                    await self._store.put_report(
                        job_id,
                        markdown,
                        report_id=lease_owner,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._fail_execution(
                        holder[0],
                        lease_owner,
                        lease_lost,
                        error_code="analysis_failed",
                        retryable=True,
                    )
                    return
                succeeded = {
                    **holder[0].record,
                    "status": "succeeded",
                    "reportId": lease_owner,
                    "leaseOwner": None,
                    "leaseExpiresAt": None,
                    "updatedAt": int(self._clock()),
                    "errorCode": None,
                    "retryable": None,
                }
                try:
                    await self._store.replace(holder[0], succeeded)
                except JobConflict:
                    return
            finally:
                await self._stop_heartbeat(heartbeat)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("x402 background job execution failed")

    async def _drive_execution_claim(
        self,
        job_id: str,
    ) -> tuple[StoredJob, str] | None:
        lease_owner = secrets.token_hex(16)
        for drive in range(_CLAIM_DRIVE_ATTEMPTS):
            try:
                stored = await self._store.read(job_id)
                if stored is None:
                    return None
                return await self._claim_execution(
                    stored,
                    lease_owner=lease_owner,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if drive + 1 >= _CLAIM_DRIVE_ATTEMPTS:
                    raise
                await asyncio.sleep(0)
        raise AssertionError("unreachable")

    async def _claim_execution(
        self,
        stored: StoredJob,
        *,
        lease_owner: str | None = None,
    ) -> tuple[StoredJob, str] | None:
        record = stored.record
        now = int(self._clock())
        status = str(record.get("status"))
        selected_owner = lease_owner or secrets.token_hex(16)
        if (
            status == "running"
            and hmac.compare_digest(
                str(record.get("leaseOwner") or ""),
                selected_owner,
            )
        ):
            return stored, selected_owner
        stale = (
            status == "running"
            and int(record.get("updatedAt") or 0)
            <= now - _STALE_MILLISECONDS
        )
        retryable_failure = (
            status == "failed" and record.get("retryable") is True
        )
        if status != "queued" and not stale and not retryable_failure:
            return None
        attempt = int(record.get("attempt") or 0)
        if attempt >= _MAX_EXECUTION_ATTEMPTS:
            return None
        running = {
            **record,
            "status": "running",
            "attempt": attempt + 1,
            "leaseOwner": selected_owner,
            "leaseExpiresAt": now + _LEASE_MILLISECONDS,
            "updatedAt": now,
            "errorCode": None,
            "retryable": None,
        }
        return await self._store.replace(stored, running), selected_owner

    async def _consume_report(self, record: dict[str, Any]) -> str:
        request = record.get("request")
        if not isinstance(request, dict):
            raise _MissingReport
        symbols = request.get("symbols")
        if not isinstance(symbols, list) or not all(
            isinstance(symbol, str) for symbol in symbols
        ):
            raise _MissingReport
        task_json = json.dumps(
            {
                "task": f"Analyze {', '.join(symbols)}",
                "terms": {
                    "symbols": symbols,
                    "analysis_type": str(
                        request.get("analysisType") or "comprehensive"
                    ),
                },
            }
        )
        prompt, effective_symbols = _build_stock_analysis_prompt(
            task_json,
            portfolio=request.get("portfolio") or [],
            risk_profile=request.get("riskProfile") or {},
        )
        session_id = (
            f"{record['jobId']}-attempt-{int(record.get('attempt') or 0)}"
        )
        final_report = None
        async for event_name, data in self._stream_work(
            prompt,
            session_id,
            effective_symbols or symbols,
        ):
            if event_name != "report" or not isinstance(data, dict):
                continue
            content = data.get("content")
            if isinstance(content, str) and content:
                final_report = content
        if final_report is None:
            raise _MissingReport
        return final_report

    async def _heartbeat(
        self,
        holder: list[StoredJob],
        lease_owner: str,
        lease_lost: asyncio.Event,
    ) -> None:
        try:
            while True:
                await self._heartbeat_sleep(_HEARTBEAT_SECONDS)
                refresh_task = asyncio.create_task(
                    self._refresh_lease(holder[0], lease_owner),
                    name=(
                        "x402-heartbeat-write:"
                        f"{holder[0].record.get('jobId', '')}"
                    ),
                )
                try:
                    refreshed = await asyncio.shield(refresh_task)
                except asyncio.CancelledError:
                    refreshed = await refresh_task
                    if refreshed is None:
                        lease_lost.set()
                    else:
                        holder[0] = refreshed
                    raise
                if refreshed is None:
                    lease_lost.set()
                    return
                holder[0] = refreshed
        except asyncio.CancelledError:
            raise
        except Exception:
            lease_lost.set()
            logger.warning("x402 background job heartbeat failed")

    async def _refresh_lease(
        self,
        stored: StoredJob,
        lease_owner: str,
    ) -> StoredJob | None:
        record = stored.record
        if (
            record.get("status") != "running"
            or not hmac.compare_digest(
                str(record.get("leaseOwner") or ""),
                lease_owner,
            )
        ):
            return None
        now = int(self._clock())
        heartbeat = {
            **record,
            "updatedAt": now,
            "leaseExpiresAt": now + _LEASE_MILLISECONDS,
        }
        try:
            return await self._store.replace(stored, heartbeat)
        except JobConflict:
            return None

    @staticmethod
    async def _stop_heartbeat(task: asyncio.Task[None]) -> None:
        if task.done():
            await asyncio.gather(task, return_exceptions=True)
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _fail_execution(
        self,
        stored: StoredJob,
        lease_owner: str,
        lease_lost: asyncio.Event,
        *,
        error_code: str,
        retryable: bool,
    ) -> None:
        if lease_lost.is_set():
            return
        record = stored.record
        if (
            record.get("status") != "running"
            or not hmac.compare_digest(
                str(record.get("leaseOwner") or ""),
                lease_owner,
            )
        ):
            return
        can_retry = (
            retryable
            and int(record.get("attempt") or 0)
            < _MAX_EXECUTION_ATTEMPTS
        )
        failed = {
            **record,
            "status": "failed",
            "leaseOwner": None,
            "leaseExpiresAt": None,
            "updatedAt": int(self._clock()),
            "errorCode": error_code,
            "retryable": can_retry,
        }
        try:
            await self._store.replace(stored, failed)
        except JobConflict:
            return
