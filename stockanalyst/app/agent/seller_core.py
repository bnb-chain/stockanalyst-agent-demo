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
_MAX_JOB_ID = 2**256 - 1


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

    def is_busy(self) -> bool:
        """True while any background delivery is in flight.

        The transport feeds this to AgentCore's ``/ping`` (``HEALTHY_BUSY`` when
        busy) so the scale-to-zero runtime is not reaped on idle while work runs.
        """
        return bool(self._tasks)

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
            context = verify_notify_authorization(
                authorization,
                job_id=job_id,
                expected_client=target.client,
                chain_id=target.chain_id,
                verifying_contract=target.verifying_contract,
            )
            if context.gateway_url is not None:
                context = replace(
                    context,
                    gateway_url=validate_gateway_url(context.gateway_url),
                )
        except NotifySecurityError as error:
            reason = "unsafe_gateway" if error.code == "invalid_gateway_url" else error.code
            logger.warning("job %s: notification rejected — %s", job_id, reason)
            return {"status": "rejected", "job_id": job_id, "reason": reason}

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
    async def _fulfill_job(self, job_id: int) -> dict[str, Any]:
        """Verify the signed deal on-chain, then deliver (the sweep's per-job worker).

        VERIFY before working: confirm the funded job carries the exact quote
        THIS agent signed (ecrecover + budget ≥ price). A permanent failure
        (not our signature, tampered terms, underfunded, expired) returns
        ``skip: True``; a transient one returns ``ok: False`` to retry.
        """
        ok, reason, permanent = await asyncio.to_thread(signing.verify_signed_job, job_id)
        if not ok:
            return {"ok": False, "job_id": job_id, "skip": permanent, "reason": reason}
        return await self._do_work_and_submit(job_id)

    async def _do_work_and_submit(self, job_id: int) -> dict[str, Any]:
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


def _build_stock_analysis_prompt(
    task_json: str,
    portfolio: list | None = None,
    risk_profile: dict | None = None,
) -> tuple[str, list[str]]:
    """Build the analysis prompt and return (prompt, symbols).

    Stage 1 drives tool-call data collection per symbol.
    Stage 2 instructs the LLM to output a single raw JSON object matching the
    StockReport schema — the JSON is parsed + validated in _run_llm and rendered
    to Markdown by report_renderer.render_report().
    """
    import re as _re

    try:
        data = json.loads(task_json)
        task_desc = data.get("task", "")
        terms = data.get("terms", {})
        if isinstance(terms, str):
            try:
                terms = json.loads(terms)
            except Exception:
                terms = {}
    except Exception:
        task_desc = task_json
        terms = {}

    symbols: list[str] = terms.get("symbols") or []
    analysis_type = terms.get("analysis_type", "comprehensive")

    if not symbols and task_desc:
        found = _re.findall(r'\b[A-Z]{1,5}(?:\.[A-Z]{1,2})?\b', task_desc)
        symbols = list(dict.fromkeys(found))[:10]

    symbol_list = ", ".join(symbols) if symbols else "the requested stocks"
    n_symbols = len(symbols) if symbols else 1

    # ── Portfolio context ──────────────────────────────────────────────────
    portfolio_block = ""
    if portfolio:
        lines = ["CLIENT PORTFOLIO (use for personalised P&L in client_position fields):"]
        for h in portfolio:
            sym = str(h.get("symbol", "")).upper()
            avg_cost = h.get("avgCost")
            shares = h.get("shares")
            currency = h.get("currency", "USD")
            if sym and avg_cost is not None and shares is not None:
                lines.append(f"  {sym}: {shares} shares @ {currency} {avg_cost:.2f} avg cost")
        if len(lines) > 1:
            portfolio_block = "\n".join(lines)

    risk_block = ""
    if risk_profile:
        tolerance = risk_profile.get("tolerance", "moderate")
        horizon = risk_profile.get("horizonMonths", 12)
        indicators = risk_profile.get("preferredIndicators", [])
        parts = [f"CLIENT RISK PROFILE: {tolerance} tolerance, {horizon}mo horizon"]
        if indicators:
            parts.append(f"  Preferred indicators: {', '.join(indicators)}")
        risk_block = "\n".join(parts)

    context_section = "\n".join(filter(None, [portfolio_block, risk_block]))
    if context_section:
        context_section = f"\n{context_section}\n"

    # ── Stage 1 checklist ──────────────────────────────────────────────────
    symbol_checklist = "\n".join(
        f"  {i+1}. {sym}: get_stock_quote, get_technical_signals, get_options_sentiment, "
        f"get_insider_activity, get_news_sentiment"
        for i, sym in enumerate(symbols)
    ) if symbols else f"  1. {symbol_list}: all five tools"

    # ── Stage 2 JSON schema (compact reference) ────────────────────────────
    held_symbols = []
    if portfolio:
        held_symbols = [
            str(h.get("symbol", "")).upper()
            for h in portfolio
            if h.get("symbol") and h.get("avgCost") is not None and h.get("shares") is not None
        ]

    client_position_note = (
        f"  Populate client_position for held symbols ({', '.join(held_symbols)}); "
        f"set to null for non-held symbols."
        if held_symbols else
        "  Set client_position to null for all symbols (no holdings provided)."
    )

    json_schema = f"""{{
  "executive_summary": "(string, 3-5 sentences: macro backdrop + one-line verdict per stock + top action)",
  "macro_snapshot": {{
    "vix": "(string)", "vix_signal": "(string)",
    "fed_rate": "(string)", "fed_rate_signal": "(string)",
    "treasury_10y": "(string)", "treasury_10y_signal": "(string)",
    "cpi_yoy": "(string or '—')", "unemployment": "(string or '—')",
    "macro_posture": "(string, 1-2 sentences)"
  }},
  "analyses": [
    {{
      "symbol": "(string, e.g. 'AAPL')",
      "company_name": "(string)",
      "rating": "Buy|Hold|Sell",
      "price_target": (number),
      "implied_return_pct": (number, e.g. 18.5 means +18.5%),
      "horizon_months": (integer),
      "risk_level": "Low|Moderate|High|Very High",
      "rating_rationale": "(string, 2-3 institutional sentences)",
      "current_price": (number|null), "week_52_low": (number|null), "week_52_high": (number|null),
      "market_cap": "(string|null, e.g. '2.85T')",
      "pe_trailing": (number|null), "pe_forward": (number|null), "peg": (number|null),
      "analyst_target": (number|null), "analyst_upside_pct": (number|null),
      "revenue_growth_pct": (number|null), "gross_margin_pct": (number|null),
      "beta": (number|null), "short_float_pct": (number|null),
      "fundamentals_commentary": "(string, 2-3 sentences on valuation vs sector/history)",
      "rsi_14": (number|null), "rsi_14_signal": "(string|null)",
      "rsi_weekly": (number|null), "rsi_weekly_signal": "(string|null)",
      "macd_signal": "(string|null)",
      "bollinger_position": (number|null, 0.0=lower band 1.0=upper band),
      "bollinger_signal": "(string|null)",
      "ma_50": (number|null), "ma_200": (number|null),
      "ma_cross": "(string|null: 'Golden Cross'|'Death Cross'|'None')",
      "adx": (number|null), "adx_signal": "(string|null)",
      "obv_trend": "(string|null)", "atr_pct": (number|null), "var_95_pct": (number|null),
      "technicals_commentary": "(string, 2-3 sentences on overall technical picture)",
      "upside_catalysts": ["(string, numbered prose, mechanism + timeframe)", "(string)", "(string)"],
      "principal_risks": ["(string, numbered prose, trigger + impact)", "(string)", "(string)"],
      "insider_activity": "(string, e.g. '3 buy transactions by CEO (90 days)')",
      "options_pcr": (number|null), "implied_vol_pct": (number|null),
      "news_sentiment_score": (number|null, -1.0 to +1.0),
      "top_headline": "(string|null)",
      "sentiment_summary": "(string, 2-3 sentences synthesising all sentiment signals)",
      "client_position": {{
        "shares": (number), "avg_cost": (number), "unrealised_pnl_pct": (number),
        "stop_loss": (number), "stop_loss_basis": "(string, e.g. 'MA-200 at $175.80')",
        "action_summary": "(string, one sentence recommendation for this position)"
      }} or null
    }}
  ],
  "portfolio_actions": [
    {{
      "priority": (integer, 1=highest), "action": "Trim|Add|New Buy|Hold",
      "symbol": "(string)", "quantity": "(string, e.g. '20 shares' or 'Reduce by 15%')",
      "price_level": "(string, e.g. 'Current ~$185' or 'On pullback to $170')",
      "capital_impact": "(string, e.g. 'Free ~$3,600')", "rationale": "(string, one sentence)"
    }}
  ],
  "stop_losses": [
    {{
      "symbol": "(string)", "avg_cost": (number), "stop_loss_level": (number),
      "risk_per_share": (number), "position_size": "(string)",
      "max_loss_at_stop": "(string, e.g. '$1,000 (10.8%)')",
      "technical_basis": "(string, e.g. 'MA-200 at $175.80')"
    }}
  ],
  "watchlist": [
    {{
      "ticker": "(string)", "company": "(string)",
      "strategic_rationale": "(string, one sentence)",
      "key_catalyst": "(string)", "entry_zone": "(string)", "risk": "(string, brief)",
      "thesis": "(string, exactly 2 sentences)"
    }}
  ],
  "risk_factors": [
    {{
      "factor": "(string, e.g. 'Sector Concentration')",
      "assessment": "Low|Moderate|High",
      "supporting_observation": "(string, specific data point)",
      "threshold_to_act": "(string, trigger level or event)"
    }}
  ]
}}"""

    prompt = f"""You are a senior equity analyst at a top-tier investment bank. \
A client has paid for a professional, actionable research report.

STOCKS TO ANALYZE: {symbol_list}
NUMBER OF STOCKS: {n_symbols} — you must produce a complete analyses entry for EACH one.
ANALYSIS TYPE: {analysis_type}
{context_section}
════════════════════════════════════════════════════════
STAGE 1 — COLLECT ALL DATA (complete every call before writing)
════════════════════════════════════════════════════════
Call all five tools for EACH symbol, then call get_macro_context() once:

{symbol_checklist}
  + get_macro_context()  (once only)

Do not begin writing until every tool call above has returned a result.
NEVER fabricate a number — use only values returned by the tools.

════════════════════════════════════════════════════════
STAGE 2 — OUTPUT JSON
════════════════════════════════════════════════════════
Your ENTIRE final response must be a single raw JSON object.
- Do NOT output any text before or after the JSON.
- Do NOT wrap it in markdown code fences (no ```json).
- Do NOT add comments inside the JSON.

The JSON must match this schema exactly:

{json_schema}

FIELD RULES:
1. analyses array must contain EXACTLY {n_symbols} entries, one per symbol in STOCKS TO ANALYZE.
   Symbols (in order): {symbol_list}
2. Use null for any field where the tool returned no data — never omit a field.
3. upside_catalysts and principal_risks must each have EXACTLY 3 items.
4. rating must be exactly "Buy", "Hold", or "Sell" (capital first letter, no other values).
5. risk_level must be exactly "Low", "Moderate", "High", or "Very High".
6. All prices and numbers must come verbatim from tool call results.
7. watchlist must have 3–5 entries of stocks NOT in the client's current portfolio.
8. risk_factors must have exactly 5 entries covering: Sector Concentration, Rate Sensitivity,
   Inter-Holding Correlation, Portfolio VaR (95%), Liquidity Risk.
{client_position_note}
"""
    return prompt, symbols


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
