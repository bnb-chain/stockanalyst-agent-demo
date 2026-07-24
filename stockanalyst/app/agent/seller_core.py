"""Seller core — the a2a-free seller logic + background delivery machinery.

This is the protocol-neutral heart of the ERC-8183 seller: the two fixed-code
operations (``negotiate`` → signed quote; ``notify_funded`` → verify → ACK →
deliver in the background) plus the background-delivery bookkeeping (``is_busy``,
the spawn/run/sweep helpers). It imports NOTHING from ``a2a`` so it can back any
transport — the A2A executor (``executor.py``) inherits it and wraps it with the
a2a wire, and a non-A2A HTTP entrypoint can call it directly without dragging in
``a2a-sdk``.

    negotiate     → ``signing.sign_quote`` (rule-based price clamp + EIP-191 sign)
    notify_funded → ``signing.verify_signed_job`` (fast on-chain gate) → ACK at
                    once, then in the BACKGROUND: LLM work → ``signing.submit_result``

``notify_funded`` is the buyer's "I funded job X — please deliver" notification.
Because the work takes time, it does NOT block the caller: it verifies the funded
job synchronously (a couple of eth_calls) to ACK accepted/rejected, then runs the
slow LLM work + on-chain ``submit`` in a background asyncio task and returns
immediately. The buyer reads the deliverable back from the CHAIN (SUBMITTED /
``get_deliverable_url``) — the chain is the source of truth. While any background
delivery is in flight :meth:`is_busy` reports busy, which the transport feeds to
AgentCore's ``/ping`` as ``HEALTHY_BUSY`` so the scale-to-zero runtime stays warm
until the work lands (within the session max-lifetime).

ALL signing is FIXED code in ``signing.py`` — NEVER an LLM-callable tool (money
is never in the LLM; the LLM only produces the work text, via the ``run_work``
hook). On each notification the core also opportunistically sweeps OTHER funded
jobs assigned to this provider — the buyer-push fallback for jobs whose buyer
funded on-chain but never sent ``notify_funded`` (deduped against in-flight jobs).
Negotiate stays sweep-free so quotes are fast. A periodic Lambda poller — which
also covers the scale-to-zero cold window when no one is invoking — is the v2
robust path.

You own this file — specialise the work hook / dispatch, but keep signing OUT of
the LLM tool list.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
import os
from typing import Any

import signing
from bnbagent_studio_core.erc8183.errors import (
    SdkCallFailedError,
    SubmitPermanentlyUnsupportedError,
)
from notify_security import (
    JobContext,
    NotifySecurityError,
    preflight_notify_authorization,
    validate_gateway_url,
    verify_notify_authorization,
)

try:
    from .prompt_builder import _build_stock_analysis_prompt
except ImportError:
    from prompt_builder import _build_stock_analysis_prompt

logger = logging.getLogger("seller-agent.core")


def _env_seconds(name: str, default: int) -> float:
    """Read a positive timeout (seconds) from the env, falling back to ``default``."""
    try:
        v = float(os.environ.get(name, "") or default)
        return v if v > 0 else float(default)
    except ValueError:
        return float(default)


# Background-task ceilings. notify_funded ACKs immediately and delivers in a
# BACKGROUND task; AgentCore keeps the scale-to-zero microVM warm (HEALTHY_BUSY)
# while is_busy() is True. A delivery (LLM text + on-chain submit + IPFS pin)
# normally finishes in ~1-2 min, so these caps sit far above real work and only
# fire on a HANG (e.g. an unresponsive RPC) — without them a hung task keeps the
# VM pinned to its 8h max-lifetime, billing memory the whole time. A timed-out
# job is treated as TRANSIENT (not dropped): the funded job stays on-chain and a
# later sweep re-delivers it idempotently.
_JOB_DELIVERY_TIMEOUT_SECONDS = _env_seconds("NOTIFY_DELIVERY_TIMEOUT_SECONDS", 1800)
_SWEEP_TIMEOUT_SECONDS = _env_seconds("NOTIFY_SWEEP_TIMEOUT_SECONDS", 60)
_PREVERIFY_TIMEOUT_SECONDS = _env_seconds("NOTIFY_PREVERIFY_TIMEOUT_SECONDS", 30)
_MAX_NOTIFY_VALIDATION_WORKERS = 4
_MAX_JOB_ID = 2**256 - 1
_CONTEXT_REQUIRED_CRITERION = "uomp_notify_context_required_v1"
_SWEEP_CONTEXT_GRACE_SECONDS = 60.0
_JOB_SPEC_UNSET = object()


def _requires_notify_context(spec: object) -> bool:
    terms = getattr(spec, "terms", None)
    return (
        isinstance(terms, dict)
        and terms.get("success_criteria") == _CONTEXT_REQUIRED_CRITERION
    )


def _verify_and_validate_notify_context(
    authorization: object,
    *,
    job_id: int,
    expected_client: str,
    chain_id: int,
    verifying_contract: str,
) -> JobContext:
    context = verify_notify_authorization(
        authorization,
        job_id=job_id,
        expected_client=expected_client,
        chain_id=chain_id,
        verifying_contract=verifying_contract,
    )
    if context.gateway_url is not None:
        context = replace(
            context,
            gateway_url=validate_gateway_url(context.gateway_url),
        )
    return context


class SellerCore:
    """ERC-8183 seller core: negotiate + notify_funded, backed by signing.py.

    ``run_work(prompt, *, session_id) -> str`` is the LLM work hook (built in
    ``main.py`` from the ADK runner); it is called inside the background delivery
    (``notify_funded`` → ``_do_work_and_submit``) to produce the deliverable text.

    The core exposes ONLY the two paid, structured operations — there is no
    free-form chat operation. The transport is responsible for routing a request
    to :meth:`negotiate` / :meth:`notify_funded`; a request that names no
    structured operation must never trigger an LLM call or a paid action.
    """

    def __init__(self, *, run_work, generator: str, network: str | None = None) -> None:
        self._run_work = run_work
        self._generator = generator
        self._network = network or "bsc-testnet"
        # Background delivery bookkeeping (see notify_funded / is_busy):
        #  _tasks       — live background asyncio tasks (busy-status source).
        #  _inflight    — job ids with active background work.
        #  _handled     — terminal jobs retained for process-lifetime dedup so a
        #                 stale notify/sweep cannot reinstall context or redeliver.
        self._tasks: set[asyncio.Task] = set()
        self._inflight: set[int] = set()
        # Terminal jobs are distinct from live work. This marker is checked in
        # the same no-await section as context compare-and-set, so a stale
        # verification can never reinstall context after terminal cleanup.
        self._handled: set[int] = set()
        # Context-free sweep work records when it has irreversibly consumed the
        # absence of notification context. Kept for terminal jobs alongside
        # _handled; cleared for transient outcomes so a named retry can win.
        self._contextless_started: set[int] = set()
        self._sweep_active = False
        # Immutable, authorization-bound off-chain context. This is installed
        # only after all on-chain and wallet checks succeed.
        self._job_contexts: dict[int, JobContext] = {}
        self._context_deadlines: dict[int, float] = {}
        self._context_events: dict[int, asyncio.Event] = {}
        self._notify_validation_slots = asyncio.Semaphore(
            _MAX_NOTIFY_VALIDATION_WORKERS
        )

    def is_busy(self) -> bool:
        """True while any background delivery is in flight.

        The transport feeds this to AgentCore's ``/ping`` (``HEALTHY_BUSY`` when
        busy) so the scale-to-zero runtime is not reaped on idle while work runs.
        """
        return bool(self._tasks)

    async def _run_notify_validation(
        self,
        authorization: object,
        *,
        job_id: int,
        expected_client: str,
        chain_id: int,
        verifying_contract: str,
    ) -> JobContext:
        await self._notify_validation_slots.acquire()
        release_in_finally = True
        worker: asyncio.Task[JobContext] | None = None
        try:
            worker = asyncio.create_task(
                asyncio.to_thread(
                    _verify_and_validate_notify_context,
                    authorization,
                    job_id=job_id,
                    expected_client=expected_client,
                    chain_id=chain_id,
                    verifying_contract=verifying_contract,
                )
            )
            try:
                # ``wait`` observes the task without cancelling it with this
                # caller and, unlike Python 3.14 ``shield``, does not log a
                # late worker exception before our completion callback can
                # retrieve it.
                await asyncio.wait({worker})
                return worker.result()
            except asyncio.CancelledError:
                release_in_finally = False

                def release_when_finished(done: asyncio.Task[JobContext]) -> None:
                    self._notify_validation_slots.release()
                    try:
                        done.exception()
                    except asyncio.CancelledError:
                        pass

                worker.add_done_callback(release_when_finished)
                raise
        finally:
            if release_in_finally:
                self._notify_validation_slots.release()

    # -- skills ----------------------------------------------------------------
    async def negotiate(self, data: dict[str, Any]) -> dict[str, Any]:
        """Rule-based quote → SDK ``NegotiationResult`` envelope (no LLM).

        The price is the FIXED list price from studio.toml, clamped to
        ``[min,max]`` BEFORE signing — a misconfigured or hostile request can
        never sign out of bounds. The buyer parses this envelope verbatim and
        anchors it on-chain via ``createJob`` + ``fund``.
        """
        request = data.get("request")
        if not isinstance(request, dict):
            request = {k: data[k] for k in ("task_description", "terms") if k in data}
        clamped = signing.clamp_price(signing.list_price())
        return signing.sign_quote(request, clamped)

    @staticmethod
    def _skills() -> list[str]:
        """The seller's two advertised skills."""
        return ["negotiate", "notify_funded"]

    async def notify_funded(self, data: dict[str, Any]) -> dict[str, Any]:
        """Buyer notification: "I funded job X — please deliver."

        Verify the funded job synchronously (a couple of eth_calls) to ACK
        accepted/rejected at once, then run the slow LLM work + on-chain
        ``submit`` in a BACKGROUND task and return IMMEDIATELY. The buyer reads the
        deliverable back from the CHAIN (SUBMITTED / ``get_deliverable_url``) —
        the chain is the source of truth (see buyer-push-protocol.md).

        An accepted notification also kicks a background sweep (deduped against
        in-flight jobs), so a buyer that funded but forgot to notify is still
        served while we're warm. A rejected / malformed notification spawns
        nothing.
        """
        raw = data.get("job_id")
        if raw is None or str(raw) == "":
            self._spawn_sweep()  # bare notify → just scan stragglers
            return {"status": "accepted", "note": "no job_id — scanning funded jobs in the background; poll the chain for results"}
        try:
            job_id = _parse_job_id(raw)
        except (TypeError, ValueError):
            return {"status": "rejected", "error": f"invalid job_id: {raw!r}"}

        legacy_context_keys = {
            "delivery_gateway_url",
            "delivery_gateway_token",
            "portfolio",
            "risk_profile",
        }
        if legacy_context_keys.intersection(data):
            reason = (
                "invalid_authorization" if "authorization" in data else "authorization_required"
            )
            return {"status": "rejected", "job_id": job_id, "reason": reason}
        authorization = data.get("authorization")
        if not isinstance(authorization, dict):
            return {
                "status": "rejected",
                "job_id": job_id,
                "reason": "authorization_required",
            }
        try:
            preflight_notify_authorization(authorization)
        except NotifySecurityError as error:
            logger.warning("job %s: notification rejected — %s", job_id, error.code)
            return {"status": "rejected", "job_id": job_id, "reason": error.code}

        try:
            ok, reason, permanent = await asyncio.wait_for(
                asyncio.to_thread(signing.verify_signed_job, job_id),
                timeout=_PREVERIFY_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 — includes RPC and timeout failures
            logger.warning("job %s: verification unavailable", job_id)
            return {
                "status": "rejected",
                "job_id": job_id,
                "reason": "verification_unavailable",
                "retryable": True,
            }
        if not ok:
            if permanent:
                logger.warning("job %s: verify rejected permanently — %s", job_id, reason)
                return {"status": "rejected", "job_id": job_id, "reason": reason}
            logger.warning("job %s: verification unavailable", job_id)
            return {
                "status": "rejected",
                "job_id": job_id,
                "reason": "verification_unavailable",
                "retryable": True,
            }

        try:
            target = await asyncio.wait_for(
                asyncio.to_thread(signing.job_authorization_target, job_id),
                timeout=_PREVERIFY_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 — includes RPC and timeout failures
            logger.warning("job %s: authorization target unavailable", job_id)
            return {
                "status": "rejected",
                "job_id": job_id,
                "reason": "verification_unavailable",
                "retryable": True,
            }

        try:
            context = await asyncio.wait_for(
                self._run_notify_validation(
                    authorization,
                    job_id=job_id,
                    expected_client=target.client,
                    chain_id=target.chain_id,
                    verifying_contract=target.verifying_contract,
                ),
                timeout=_PREVERIFY_TIMEOUT_SECONDS,
            )
        except NotifySecurityError as error:
            reason = "unsafe_gateway" if error.code == "invalid_gateway_url" else error.code
            logger.warning("job %s: notification rejected — %s", job_id, reason)
            return {"status": "rejected", "job_id": job_id, "reason": reason}
        except Exception:  # noqa: BLE001 — timeout, scheduling, or worker failure
            logger.warning("job %s: notification validation unavailable", job_id)
            return {
                "status": "rejected",
                "job_id": job_id,
                "reason": "verification_unavailable",
                "retryable": True,
            }

        # Atomic with the worker's context lookup/marker update: neither path
        # awaits between observing the marker/context and committing its state.
        if job_id in self._handled or job_id in self._contextless_started:
            logger.warning("job %s: notification rejected — delivery_already_started", job_id)
            return {
                "status": "rejected",
                "job_id": job_id,
                "reason": "delivery_already_started",
            }
        existing_context = self._job_contexts.get(job_id)
        if existing_context is None:
            self._job_contexts[job_id] = context
        elif existing_context.digest != context.digest:
            logger.warning("job %s: notification rejected — context_conflict", job_id)
            return {"status": "rejected", "job_id": job_id, "reason": "context_conflict"}
        context_event = self._context_events.get(job_id)
        if context_event is not None:
            context_event.set()
        self._spawn_job(job_id, verified=True)
        self._spawn_sweep()  # straggler fallback alongside the named job
        return {
            "status": "accepted",
            "job_id": job_id,
            "note": "delivery started; poll the chain (SUBMITTED / get_deliverable_url) for the result",
        }

    # -- background delivery ---------------------------------------------------
    def _spawn(self, coro: Any) -> None:
        """Run ``coro`` in a tracked background task (keeps :meth:`is_busy` True)."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _spawn_sweep(self) -> None:
        """Start at most one pending-job scan at a time."""
        if self._sweep_active:
            return
        self._sweep_active = True

        async def run_sweep() -> None:
            try:
                await self._sweep()
            finally:
                self._sweep_active = False

        self._spawn(run_sweep())

    def _spawn_job(self, job_id: int, *, verified: bool) -> None:
        """Background-deliver ``job_id`` once, deduped against in-flight jobs.

        ``_inflight`` is updated SYNCHRONOUSLY here (before scheduling) so a
        concurrent notify + sweep can never double-deliver the same job.
        """
        if job_id in self._inflight or job_id in self._handled:
            return
        self._inflight.add(job_id)
        self._spawn(self._run_job(job_id, verified=verified))

    async def _run_job(self, job_id: int, *, verified: bool) -> None:
        """Background runner: deliver one job, log the outcome, free the slot.

        ``verified`` jobs (pre-verified in ``notify_funded``) skip straight to the
        work; unverified ones (the sweep) run the full verify gate first.
        """
        terminal = False
        try:
            # Hard ceiling so a hung delivery (e.g. unresponsive RPC) cannot keep
            # is_busy() True — which would pin the microVM to its 8h max-lifetime.
            # A timeout is TRANSIENT: terminal stays False, the slot is freed, and
            # the funded job is re-delivered idempotently by a later sweep.
            result = await asyncio.wait_for(
                self._do_work_and_submit(job_id) if verified else self._fulfill_job(job_id),
                timeout=_JOB_DELIVERY_TIMEOUT_SECONDS,
            )
            logger.info(
                "notify_funded job %s completed (ok=%s, skip=%s)",
                job_id,
                bool(result.get("ok")),
                bool(result.get("skip")),
            )
            # A terminal outcome (delivered, or a permanent skip) moves to the
            # distinct handled marker. The _spawn_job gate then rejects a slower
            # concurrent sweep that still sees this job as FUNDED, while the
            # notify CAS gate rejects stale verified requests after context cleanup.
            # Only transient failures clear all markers for a later retry.
            terminal = bool(result.get("ok") or result.get("skip"))
        except (asyncio.TimeoutError, TimeoutError):
            # Transient by design — leave terminal False so a later sweep retries.
            logger.warning(
                "background delivery of job %s timed out after %ss; will retry",
                job_id,
                _JOB_DELIVERY_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 — a background job must never crash the loop
            logger.exception("background delivery of job %s failed", job_id)
        finally:
            if terminal:
                self._handled.add(job_id)
                self._job_contexts.pop(job_id, None)
                self._inflight.discard(job_id)
            else:
                self._inflight.discard(job_id)
                self._contextless_started.discard(job_id)

    # -- internals -------------------------------------------------------------
    async def _await_sweep_context(self, job_id: int, *, required: bool) -> bool:
        if job_id in self._job_contexts:
            return True

        loop = asyncio.get_running_loop()
        deadline = self._context_deadlines.setdefault(
            job_id,
            loop.time() + _SWEEP_CONTEXT_GRACE_SECONDS,
        )
        event = self._context_events.setdefault(job_id, asyncio.Event())
        remaining = max(0.0, deadline - loop.time())
        if remaining > 0 and not event.is_set():
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass

        if job_id in self._job_contexts:
            return True
        if required:
            return False
        self._contextless_started.add(job_id)
        return True

    async def _fulfill_job(self, job_id: int) -> dict[str, Any]:
        """Verify the signed deal on-chain, then deliver (the sweep's per-job worker).

        VERIFY before working: confirm the funded job carries the exact quote
        THIS agent signed (ecrecover + budget ≥ price). A permanent failure
        (not our signature, tampered terms, underfunded, expired) returns
        ``skip: True``; a transient one returns ``ok: False`` to retry.
        """
        ok, reason, permanent = await asyncio.to_thread(
            signing.verify_signed_job,
            job_id,
        )
        if not ok:
            return {
                "ok": False,
                "job_id": job_id,
                "skip": permanent,
                "reason": reason,
            }

        spec = await asyncio.to_thread(signing.job_spec, job_id)
        ready = await self._await_sweep_context(
            job_id,
            required=_requires_notify_context(spec),
        )
        if not ready:
            return {
                "ok": False,
                "job_id": job_id,
                "skip": False,
                "reason": "notify_context_required",
            }
        return await self._do_work_and_submit(job_id, spec=spec)

    async def _do_work_and_submit(
        self,
        job_id: int,
        *,
        spec: object = _JOB_SPEC_UNSET,
    ) -> dict[str, Any]:
        """LLM work → sign + submit. Assumes ``job_id`` is already verified.

        DEVELOPER HOOK: the LLM block produces the deliverable text — specialise
        it for your seller. ``signing.submit_result`` re-runs the SDK ``verify_job``
        (defense in depth) and RAISES on a failed submit, so an ``ok: True`` result
        always carries a landed tx hash.
        """
        # Read without consuming: a transient work/submit failure must retry with
        # the exact same authorization-bound values. Swept jobs have no context.
        context = self._job_contexts.get(job_id)
        if context is None:
            self._contextless_started.add(job_id)
        gateway_url = context.gateway_url if context is not None else None
        gateway_token = context.gateway_token if context is not None else None
        portfolio = context.portfolio_for_prompt() if context is not None else []
        risk_profile = context.risk_profile_for_prompt() if context is not None else None

        if spec is _JOB_SPEC_UNSET:
            spec = await asyncio.to_thread(signing.job_spec, job_id)
        if spec is not None:
            task = json.dumps({"task": spec.task, "terms": spec.terms}, ensure_ascii=False)
        else:
            task = f"job {job_id}"
        prompt, symbols = _build_stock_analysis_prompt(
            task,
            portfolio=portfolio,
            risk_profile=risk_profile,
        )
        work = await self._run_work(prompt, session_id=str(job_id), symbols=symbols)

        try:
            res = await asyncio.to_thread(
                signing.submit_result,
                job_id,
                response_content=work,
                metadata={
                    "job_id": job_id,
                    "generator": self._generator,
                    "built_with": "https://github.com/bnb-chain/bnbagent-studio",
                },
                gateway_url=gateway_url,
                gateway_token=gateway_token,
            )
        except SubmitPermanentlyUnsupportedError as e:
            # Deterministic for this wallet kind: submit can NEVER succeed →
            # permanent skip (a transient error would burn one LLM call / retry).
            return {"ok": False, "job_id": job_id, "skip": True, "reason": str(e)}
        except SdkCallFailedError as error:
            # bnbagent's structured contract marks only genuine transient
            # chain/internal failures with ``retryable=True``. Missing/false
            # retryability is terminal; retrying an unclassified SDK failure
            # would repeat costly LLM work forever.
            if error.retryable is True:
                raise
            result = {
                "ok": False,
                "job_id": job_id,
                "skip": True,
                "reason": error.code or "submission_failed",
            }
            if error.tx_hash is not None:
                result["tx_hash"] = error.tx_hash
            return result
        return {
            "ok": True,
            "job_id": job_id,
            "tx_hash": res.submit_tx,
            "deliverable_url": res.deliverable_url,
        }

    async def _sweep(self) -> None:
        """Best-effort background fallback: deliver any FUNDED jobs for this provider.

        Catches jobs whose buyer funded on-chain but never sent ``notify_funded``.
        Each job is handed to ``_spawn_job`` (deduped against in-flight jobs, so a
        concurrent notify never double-delivers); ``verify_signed_job`` returns
        non-OK for an already-SUBMITTED job (idempotent, no state file). Errors
        here are logged and never surface to the caller.
        """
        try:
            from bnbagent.erc8183 import ERC8183JobOps

            from bnbagent_studio_core.wallet import get_wallet

            ops = ERC8183JobOps(wallet_provider=get_wallet(), network=self._network)
            # Time-bounded: a hung scan would otherwise keep is_busy() True (it runs
            # on every notify) and pin the microVM to its 8h max-lifetime.
            pending = await asyncio.wait_for(ops.get_pending_jobs(), timeout=_SWEEP_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001 — the sweep is best-effort (incl. TimeoutError)
            logger.warning("funded-job sweep failed: %s", e)
            return
        for job in (pending or {}).get("jobs", []):
            jid = job.get("jobId") if isinstance(job, dict) else None
            if jid is None:
                continue
            try:
                self._spawn_job(int(jid), verified=False)
            except (TypeError, ValueError):
                continue




def _parse_job_id(raw: Any) -> int:
    """Normalise an envelope ``job_id`` (``0x..`` / decimal string / int) to int."""
    if isinstance(raw, bool):
        raise TypeError("job_id must be an unsigned integer")
    if isinstance(raw, int):
        job_id = raw
    elif isinstance(raw, str):
        s = raw.strip()
        job_id = int(s, 16) if s.lower().startswith("0x") else int(s)
    else:
        raise TypeError("job_id must be an integer or string")
    if not 0 <= job_id <= _MAX_JOB_ID:
        raise ValueError("job_id is outside uint256")
    return job_id
