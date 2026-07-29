"""Deterministic signing — the Agent is the SOLE key-holder/signer.

Every on-chain WRITE the Agent performs lives here as FIXED code:

    sign_quote(...)    EIP-191 sign the (clamped) negotiated offer
    submit_result(...) build manifest → upload → on-chain ``submit``
    settle(...)        claim payment after the dispute window

These functions are NEVER registered as LLM-callable tools (``tools.py`` holds
only read-only tools). The price is a FIXED list price from studio.toml
(``list_price()``, clamped by ``main.py`` BEFORE it reaches here) — the LLM only
produces the work text and never moves money or sets a price.

The key is loaded by ``bnbagent_studio_core.wallet.get_wallet()`` (local keystore,
unlocked by ``WALLET_PASSWORD``). It is injected into the AgentCore runtime via
the secret store, never bundled into the code package.

You own this file — edit the pricing clamp source / manifest shape if your
domain needs it, but keep these ops OUT of the LLM tool list.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from bnbagent.erc8183 import NegotiationHandler
from bnbagent_studio_core import config
from bnbagent_studio_core.erc8183 import submit_workflow
from bnbagent_studio_core.erc8183.client import get_8183_client
from bnbagent_studio_core.erc8183.workflows import settle_workflow
from bnbagent_studio_core.wallet import get_wallet

_CONTEXT_REQUIRED_CRITERION = "uomp_notify_context_required_v1"


def _recover_quote_signer_compat_with_mode(
    description: str,
) -> tuple[str | None, bool]:
    """Return ``(signer, used_legacy_fallback)`` for a signed description."""
    from bnbagent_studio_core.erc8183.verify import recover_quote_signer

    try:
        recovered = recover_quote_signer(description)
    except (TypeError, ValueError):
        recovered = None
    if recovered is not None:
        return recovered, False

    import json

    try:
        parsed = json.loads(description)
        terms = parsed["terms"]
        if (
            not isinstance(parsed, dict)
            or not isinstance(terms, dict)
            or terms.get("success_criteria") != _CONTEXT_REQUIRED_CRITERION
        ):
            return None, True
        negotiation_hash = parsed["negotiation_hash"]
        provider_sig = parsed["provider_sig"]
        if not isinstance(negotiation_hash, str) or not isinstance(provider_sig, str):
            return None, True

        content = {
            key: value
            for key, value in parsed.items()
            if key not in ("negotiation_hash", "provider_sig")
        }
        content["terms"] = dict(terms)
        content["terms"]["success_criteria"] = list(_CONTEXT_REQUIRED_CRITERION)

        from web3 import Web3

        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
        recomputed = Web3.keccak(text=canonical).hex()
        recomputed = recomputed if recomputed.startswith("0x") else f"0x{recomputed}"
        if recomputed.lower() != negotiation_hash.lower():
            return None, True

        from eth_account import Account
        from eth_account.messages import encode_defunct

        return (
            Account.recover_message(
                encode_defunct(text=negotiation_hash),
                signature=provider_sig,
            ),
            True,
        )
    except (KeyError, TypeError, ValueError):
        return None, True


def recover_quote_signer_compat(description: str) -> str | None:
    """Recover a normal or legacy-compatible quote signer."""
    return _recover_quote_signer_compat_with_mode(description)[0]


def recover_bound_quote_signer_compat(
    description: str,
    *,
    expected_chain_id: int,
    expected_verifying_contract: str,
    now: int,
) -> str | None:
    """Recover a signer only when the quote is valid for the active domain.

    Normal SDK quotes retain the SDK's funded-job TTL semantics. The narrowly
    scoped legacy canonicalization fallback additionally requires an unexpired
    quote so old compatibility signatures cannot be replayed.
    """
    import json

    from web3 import Web3

    try:
        parsed = json.loads(description)
        if not isinstance(parsed, dict):
            return None

        signed_chain_id = parsed.get("chain_id")
        if (
            type(signed_chain_id) is not int
            or signed_chain_id != expected_chain_id
        ):
            return None

        signed_contract = parsed.get("verifying_contract")
        if (
            not isinstance(signed_contract, str)
            or not Web3.is_address(signed_contract)
            or not Web3.is_address(expected_verifying_contract)
            or Web3.to_checksum_address(signed_contract)
            != Web3.to_checksum_address(expected_verifying_contract)
        ):
            return None
    except (TypeError, ValueError):
        return None

    signer, used_legacy_fallback = _recover_quote_signer_compat_with_mode(description)
    if signer is None:
        return None

    if used_legacy_fallback:
        quote_expires_at = parsed.get("quote_expires_at")
        if (
            type(quote_expires_at) is not int
            or quote_expires_at <= now
        ):
            return None

    return signer


def _erc8183_cfg() -> dict:
    """Read ``[payments.erc8183]`` from studio.toml ({} when absent)."""
    try:
        cfg = config.load_studio_toml()
    except FileNotFoundError:
        cfg = {}
    return cfg.get("payments", {}).get("erc8183", {}) or {}


def price_bounds() -> tuple[int, int]:
    """Return ``(min_price, max_price)`` in raw wei from studio.toml.

    These are the clamp bounds applied to the configured list price BEFORE
    signing. ``min_price``/``max_price`` are raw uint256 strings in
    ``[payments.erc8183]``.
    """
    cfg = _erc8183_cfg()
    # TODO: if min/max are absent the bounds default to (0, +inf) — i.e. NO
    # clamp. Set [payments.erc8183].min_price / max_price in studio.toml to
    # enforce a real floor/ceiling (strongly recommended for production).
    # The scaffold ships max_price = "" (an empty string, not absent), so treat
    # empty/whitespace the same as missing → fall back to the default bound.
    def _raw(key: str, default: int) -> int:
        s = str(cfg.get(key, "")).strip()
        return int(s) if s else default

    min_price = _raw("min_price", 0)
    max_price = _raw("max_price", 2**256 - 1)
    return min_price, max_price


def list_price() -> int:
    """Return the seller's list price in raw wei from studio.toml.

    Reads ``[payments.erc8183].price`` — the deterministic asking price every
    quote uses (rule-based pricing; no LLM in the quote path). Empty/absent → 0.
    Edit ``price`` in studio.toml to change what you charge. The value is still
    clamped to ``[min_price, max_price]`` by :func:`clamp_price` before signing.
    """
    s = str(_erc8183_cfg().get("price", "")).strip()
    return int(s) if s else 0


def clamp_price(proposed_wei: int) -> int:
    """Clamp a proposed price into ``[min_price, max_price]``."""
    lo, hi = price_bounds()
    return max(lo, min(proposed_wei, hi))


_handler: NegotiationHandler | None = None


def _get_handler() -> NegotiationHandler:
    """Return the process-wide :class:`NegotiationHandler` (lazy, cached).

    The handler's chain_id + verifying_contract come from
    :func:`get_8183_client` and are stable per process, so we build it once.
    The per-request clamped price is passed via ``negotiate(..., price=...)``
    (see :func:`sign_quote`), so the construction-time ``service_price`` is a
    placeholder that is always overridden.
    """
    global _handler
    if _handler is None:
        cfg = _erc8183_cfg()
        currency = cfg.get("currency", "")  # the Agent owns the currency now
        ttl = int(cfg.get("quote_ttl_seconds", 900))
        est = int(cfg.get("default_estimated_completion_seconds", 600))
        # chain_id + verifying_contract bind provider_sig to this chain/contract
        # (prevents cross-chain replay). Read off the live ERC-8183 client.
        client = get_8183_client()
        _handler = NegotiationHandler(
            service_price="0",  # placeholder — overridden per call via price=
            currency=currency,
            estimated_completion_seconds=est,
            wallet_provider=get_wallet(),
            quote_ttl_seconds=ttl,
            chain_id=client.network.chain_id,
            verifying_contract=client.commerce.address,
        )
    return _handler


def sign_quote(request: dict, clamped_price_wei: int) -> dict:
    """Negotiate + EIP-191-sign a quote at ``clamped_price_wei``; return the SDK envelope.

    Reuses a process-wide :class:`NegotiationHandler` (cached — its chain_id +
    verifying_contract are stable per process) and overrides the price for this
    request via ``negotiate(..., price=str(clamped_price_wei))``.

    Returns the SDK's ``NegotiationResult.to_dict()`` envelope **verbatim** — the
    exact wire structure a buyer parses and feeds to ``build_job_description`` to
    anchor on-chain (see docs/design/erc8183-sdk-reference.md §2). On accept it
    carries ``response.terms.price``/``currency``, ``quote_expires_at``,
    ``negotiation_hash``, ``response_hash``, ``provider_sig``, ``chain_id``,
    ``verifying_contract``; on reject it carries ``response.reason_code`` /
    ``reason`` (empty hash + sig). We do NOT invent a custom shape.
    """
    cfg = _erc8183_cfg()
    est = int(cfg.get("default_estimated_completion_seconds", 600))

    handler = _get_handler()
    result = handler.negotiate(
        request, price=str(clamped_price_wei), estimated_completion_seconds=est
    )

    # NegotiationHandler signs non-fatally: if sign_message failed it returns an
    # accepted result WITHOUT provider_sig. Never relay an unsigned "accepted".
    if result.accepted and (not result.negotiation_hash or not result.provider_sig):
        raise RuntimeError(
            "quote accepted but provider_sig is missing (wallet sign failed); "
            "refusing to relay an unsigned offer"
        )

    return result.to_dict()


@dataclass(frozen=True)
class VerifiedJobSnapshot:
    """One verified chain read shared by authorization and delivery setup."""

    client: str
    chain_id: int
    verifying_contract: str
    spec: Any


def verify_signed_job_snapshot(
    job_id: int,
) -> tuple[VerifiedJobSnapshot | None, str, bool]:
    """Verify one fetched job and return its immutable authorization/work fields."""
    from bnbagent.erc8183.types import JobStatus
    from bnbagent_studio_core.erc8183.verify import JobDescription

    client = get_8183_client()
    try:
        job = client.get_job(job_id)
    except Exception as exc:
        return None, f"chain read failed: {exc}", False

    if job.status != JobStatus.FUNDED:
        return None, f"job status {job.status.name}, expected FUNDED", False

    expected_signer = str(get_wallet().address)
    if str(job.provider).lower() != expected_signer.lower():
        return None, "job is not assigned to this provider", True

    if job.expired_at and job.expired_at <= int(time.time()):
        return None, "job has expired", True

    try:
        spec = JobDescription.from_str(job.description)
    except Exception:
        return None, "no signed quote anchored in job description", True
    if spec is None:
        return None, "no signed quote anchored in job description", True

    active_chain_id = int(client.network.chain_id)
    active_verifying_contract = str(client.commerce.address)
    try:
        signer = recover_bound_quote_signer_compat(
            job.description,
            expected_chain_id=active_chain_id,
            expected_verifying_contract=active_verifying_contract,
            now=int(time.time()),
        )
    except Exception:
        signer = None
    if signer is None or signer.lower() != expected_signer.lower():
        return (
            None,
            "quote signature does not match this provider (or terms were tampered)",
            True,
        )

    try:
        if int(job.budget) < int(spec.price):
            return (
                None,
                f"funded budget {job.budget} is below the agreed price {spec.price}",
                True,
            )
    except (TypeError, ValueError):
        return None, f"unparseable agreed price {spec.price!r}", True

    return (
        VerifiedJobSnapshot(
            client=str(job.client),
            chain_id=active_chain_id,
            verifying_contract=active_verifying_contract,
            spec=spec,
        ),
        "",
        False,
    )


def verify_signed_job(job_id: int) -> tuple[bool, str, bool]:
    """Verify funded ``job_id`` carries the quote THIS agent signed.

    Compatibility wrapper over :func:`verify_signed_job_snapshot`.
    """
    snapshot, reason, permanent = verify_signed_job_snapshot(job_id)
    return snapshot is not None, reason, permanent


@dataclass(frozen=True)
class JobAuthorizationTarget:
    """On-chain identity and EIP-712 domain for a funded job's client."""

    client: str
    chain_id: int
    verifying_contract: str


def job_authorization_target(job_id: int) -> JobAuthorizationTarget:
    """Return the chain-owned client and EIP-712 domain for ``job_id``."""
    client = get_8183_client()
    job = client.get_job(job_id)
    return JobAuthorizationTarget(
        client=str(job.client),
        chain_id=int(client.network.chain_id),
        verifying_contract=str(client.commerce.address),
    )


def job_spec(job_id: int):
    """Return the on-chain :class:`JobDescription` for ``job_id`` (``None`` if unstructured).

    The task + terms the buyer ANCHORED ON-CHAIN — and that this agent's
    ``provider_sig`` covers — are the authoritative work spec. The work hook
    reads the task from HERE (the on-chain job description), so the Agent
    delivers exactly the deal it signed.
    Returns ``None`` for legacy/plain-text descriptions (caller falls back).
    """
    from bnbagent.erc8183.schema import JobDescription

    job = get_8183_client().get_job(job_id)
    return JobDescription.from_str(job.description)


def submit_result(
    job_id: int,
    response_content: str,
    metadata: dict | None = None,
    *,
    gateway_url: str | None = None,
    gateway_token: str | None = None,
):
    """Sign + broadcast the on-chain ``submit`` for ``job_id``.

    When ``gateway_url`` and ``gateway_token`` are provided (set by the buyer in
    notify_funded), the deliverable is uploaded to the buyer's UOMP payload relay
    (a local HTTP server exposed via Cloudflare Tunnel). The public tunnel URL is
    then stored on-chain as the ``deliverable_url``. This is the UOMP remote
    delivery path.

    Without gateway params the function falls back to the default storage backend
    configured in studio.toml (typically LocalStorageProvider for local dev).
    """
    # The gateway path temporarily replaces a process-global SDK factory. Every
    # submission, including default-storage sweeps, must share this lock or a
    # concurrent default call can capture another buyer's gateway provider.
    # The current SDK has no explicit provider-injection seam, so the lock
    # cannot safely be shortened to provider construction alone.
    from uomp_storage import submit_lock

    with submit_lock:
        if gateway_url and gateway_token:
            import bnbagent_studio_core.storage as _storage_mod

            from uomp_storage import UOMPGatewayStorageProvider

            _orig = _storage_mod.storage_provider_from_config
            _storage_mod.storage_provider_from_config = (
                lambda **_kw: UOMPGatewayStorageProvider(gateway_url, gateway_token)
            )
            try:
                return submit_workflow(job_id, response_content, metadata=metadata)
            finally:
                _storage_mod.storage_provider_from_config = _orig

        return submit_workflow(job_id, response_content, metadata=metadata)


def settle(job_id: int) -> str:
    """Sign + broadcast ``settle`` (claim payment) for ``job_id``.

    Delegates to :func:`bnbagent_studio_core.erc8183.workflows.settle_workflow` with the
    default ``approve`` action → SDK ``router.settle(job_id)``, ``audited_op``-
    wrapped. Returns the settle tx hash.
    """
    return settle_workflow(job_id, action="approve")
