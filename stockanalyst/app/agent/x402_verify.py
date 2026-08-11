"""Fixed-code x402 v2 EIP-3009 proof verification.

Paid proofs select U or USD1 from the immutable token registry using the
accepted asset address. The selected token supplies the 18-decimal units,
EIP-712 domain name/version, and verifying contract; the seller price is
fixed separately. Promotional
proofs use the same token-aware signature verification with an exact zero
amount and no B402 ``signerAddress`` metadata.

This module is never LLM-callable. It verifies signatures locally; on-chain
settlement remains the responsibility of the B402 facilitator integration in
``x402_handler.py``. The legacy ``/x402/free`` helpers remain separate.
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import math
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from eth_account import Account

try:
    from .x402_tokens import PaymentToken, U_TOKEN, token_by_asset
except ImportError:  # Direct imports from stockanalyst/app/agent.
    from x402_tokens import PaymentToken, U_TOKEN, token_by_asset

_log = logging.getLogger("seller-agent.x402.verify")

# Startup assertion — fail hard if eth_account is present but broken.
# The module-level import above already fails closed if eth_account is missing
# entirely; this catches a partially-working install (e.g. bad C extension).
_recover_hash = getattr(Account, "_recover_hash", None)
if not callable(_recover_hash):
    raise RuntimeError(
        "eth_account._recover_hash unavailable — signature verification is broken. "
        "Run: pip install 'eth-account>=0.8' 'eth-abi>=4' 'eth-utils>=2'"
    )
try:
    # Bad signature is expected; we only need the callable to run without
    # ImportError / AttributeError from a broken eth_account install.
    _recover_hash(b"\x00" * 32, signature=b"\x00" * 65)
except Exception:
    _log.debug("eth_account smoke recover failed as expected", exc_info=True)

_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
MAX_PAYMENT_SIGNATURE_CHARACTERS = 32 * 1024
MAX_PAYMENT_PROOF_BYTES = 24 * 1024
MAX_PAYMENT_PROOF_NESTING = 64
_PAYMENT_SIGNATURE_REJECTION = (
    "Payment-Signature is not valid base64 JSON"
)
_FREE_VALUE_REJECTION = (
    "free tier requires value=0; "
    "use /x402/analyze/async for paid analysis"
)


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _has_bounded_json_nesting(value: object) -> bool:
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > MAX_PAYMENT_PROOF_NESTING:
            return False
        if isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)
    return True


def decode_payment_signature(value: str) -> dict[str, Any] | None:
    """Strictly decode a bounded canonical Base64 JSON proof object."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_PAYMENT_SIGNATURE_CHARACTERS
    ):
        return None
    try:
        encoded = value.encode("ascii")
        raw = base64.b64decode(encoded, validate=True)
        if (
            len(raw) > MAX_PAYMENT_PROOF_BYTES
            or base64.b64encode(raw) != encoded
        ):
            return None
        decoded = raw.decode("utf-8", errors="strict")
        proof = json.loads(
            decoded,
            parse_float=_finite_json_float,
            parse_constant=_reject_json_constant,
        )
        if not _has_bounded_json_nesting(proof):
            return None
    except (RecursionError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return proof if isinstance(proof, dict) else None


def _payment_payload_objects(
    proof: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    payload = proof.get("payload")
    if not isinstance(payload, dict):
        return None
    authorization = payload.get("authorization")
    if not isinstance(authorization, dict):
        return None
    return payload, authorization


def _resolve_b402_pay_to_address(
    env: Mapping[str, str] = os.environ,
    studio_loader: Callable[[], Mapping[str, Any] | None] | None = None,
) -> str:
    source = "B402_PAY_TO_ADDRESS"
    raw = str(env.get(source, "")).strip()
    if not raw:
        source = "X402_SELLER_WALLET"
        raw = str(env.get(source, "")).strip()
    if not raw:
        source = "studio wallet address"
        try:
            if studio_loader is None:
                from bnbagent_studio_core import config
                studio_loader = config.load_studio_toml
            studio = studio_loader() or {}
            raw = str((studio.get("wallet") or {}).get("address") or "").strip()
        except Exception as exc:
            raise RuntimeError("B402 pay-to configuration unavailable") from exc
    if not _EVM_ADDRESS.fullmatch(raw):
        raise RuntimeError(f"{source} must be a 0x-prefixed EVM address")
    return raw


_DEFAULT_X402_CHAIN_ID = 56
_DEFAULT_X402_TOKEN_ADDRESS = (
    "0x330949Aed7d00FCe0558C64ED6FeC9792616cC39"
)


def _resolve_x402_chain_id(env: Mapping[str, str] = os.environ) -> int:
    if "X402_CHAIN_ID" not in env:
        return _DEFAULT_X402_CHAIN_ID
    if str(env.get("X402_CHAIN_ID", "")) != "56":
        raise RuntimeError("X402_CHAIN_ID must be exactly 56")
    return 56


def _resolve_x402_token_address(
    env: Mapping[str, str] = os.environ,
) -> str:
    if "X402_TOKEN_ADDRESS" not in env:
        return _DEFAULT_X402_TOKEN_ADDRESS
    raw = str(env.get("X402_TOKEN_ADDRESS", "")).strip()
    if _EVM_ADDRESS.fullmatch(raw) is None:
        raise RuntimeError(
            "X402_TOKEN_ADDRESS must be a 0x-prefixed EVM address"
        )
    return raw


B402_PAY_TO_ADDRESS = _resolve_b402_pay_to_address()
SELLER_WALLET = B402_PAY_TO_ADDRESS  # compatibility alias
U_TOKEN_ADDRESS = U_TOKEN.address
PRICE_WEI = 210_000_000_000_000_000
CHAIN_ID = _resolve_x402_chain_id()


@dataclass(frozen=True)
class VerifiedPayment:
    proof: dict[str, Any]
    from_address: str
    to_address: str
    value: int
    valid_after: int
    valid_before: int
    nonce: str
    nonce_bytes: bytes
    asset: str = U_TOKEN.address.lower()
    token_symbol: str = U_TOKEN.symbol
    promotional: bool = False

# Compatibility aliases for the legacy free-tier verifier.
_TOKEN_DOMAIN_NAME = U_TOKEN.domain_name
_TOKEN_DOMAIN_VERSION = U_TOKEN.domain_version

# ── Free-tier replay protection ────────────────────────────────────────────────
# Zero-value free-tier nonces are stored in-memory: they are lost on restart and
# NOT shared across replicas. Paid async jobs use durable job identity plus the
# on-chain EIP-3009 authorization state instead of this set.
#
# For production without a facilitator, replace with an atomic Redis set:
#   redis.set(nonce_key, 1, ex=3600, nx=True)  →  False means already used
# or rely on the on-chain EIP-3009 nullifier check via eth_call before delivery.
_used_nonces: set[str] = set()

if True:  # always — warn operators about the in-memory limitation at startup
    _log.warning(
        "x402 free-tier replay protection is IN-MEMORY only — free-tier nonces "
        "are lost on restart and not shared across replicas. Use Redis for "
        "durable free-tier replay protection in production."
    )


# ── EIP-712 hashing ────────────────────────────────────────────────────────────

def _keccak(data: bytes) -> bytes:
    from eth_utils import keccak as _k
    return _k(data)


def _ktext(text: str) -> bytes:
    from eth_utils import keccak as _k
    return _k(text=text)


_DOMAIN_TYPE_HASH = _ktext(
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
_TRANSFER_TYPE_HASH = _ktext(
    "TransferWithAuthorization(address from,address to,uint256 value,"
    "uint256 validAfter,uint256 validBefore,bytes32 nonce)"
)


def _domain_separator(token: PaymentToken) -> bytes:
    import eth_abi
    from eth_utils import to_checksum_address
    return _keccak(eth_abi.encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [
            _DOMAIN_TYPE_HASH,
            _ktext(token.domain_name),
            _ktext(token.domain_version),
            CHAIN_ID,
            to_checksum_address(token.address),
        ],
    ))


def _eip712_digest(
    from_: str, to: str, value: int,
    valid_after: int, valid_before: int, nonce: bytes,
    *,
    token: PaymentToken = U_TOKEN,
) -> bytes:
    """keccak256(\\x19\\x01 || domain_separator || struct_hash)."""
    import eth_abi
    from eth_utils import to_checksum_address
    struct_hash = _keccak(eth_abi.encode(
        ["bytes32", "address", "address", "uint256", "uint256", "uint256", "bytes32"],
        [
            _TRANSFER_TYPE_HASH,
            to_checksum_address(from_),
            to_checksum_address(to),
            value,
            valid_after,
            valid_before,
            nonce,
        ],
    ))
    return _keccak(
        b"\x19\x01"
        + _domain_separator(token)
        + struct_hash
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def build_payment_challenge(
    symbols: list[str],
    resource_url: str,
    requirements: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return x402 v2 standard payment challenge (HTTP 402 body / Payment-Required header)."""
    if isinstance(requirements, Mapping):
        # Compatibility for the paid-U handler until it supplies a list of
        # already-built requirements.
        accepts = [build_payment_requirement(U_TOKEN, requirements)]
    else:
        accepts = copy.deepcopy(list(requirements))
    description = (
        f"Stock analysis for {', '.join(s.upper() for s in symbols)}"
        if symbols else "Stock analysis report"
    )
    return {
        "x402Version": 2,
        "accepts": accepts,
        "error":    "Payment Required",
        "resource": {
            "url": resource_url,
            "description": description,
            "mimeType": "application/json",
        },
    }


def build_payment_requirement(
    token: PaymentToken,
    extra: Mapping[str, Any],
    *,
    amount: int | None = None,
    promotional: bool = False,
) -> dict[str, Any]:
    """Build an exact token requirement for a paid or promotional proof."""
    clean_extra = copy.deepcopy(dict(extra))
    if promotional:
        clean_extra.pop("signerAddress", None)
    return {
        "scheme": "exact",
        "network": f"eip155:{CHAIN_ID}",
        "amount": str(PRICE_WEI if amount is None else amount),
        "asset": token.address,
        "payTo": B402_PAY_TO_ADDRESS.lower(),
        "maxTimeoutSeconds": 600,
        "extra": clean_extra,
    }


def validate_payment_proof(
    proof_header: str,
    *,
    expected_requirement: Mapping[str, Any] | None = None,
    now: int | None = None,
    allow_expired: bool = False,
    promotional: bool = False,
) -> tuple[VerifiedPayment | None, str]:
    """Validate a proof without consuming its nonce.

    ``allow_expired`` is only for locating an existing recovery record. It
    bypasses the wall-clock expiry rejection but preserves every other
    semantic, domain, and cryptographic check.
    """
    proof = decode_payment_signature(proof_header)
    if proof is None:
        return None, _PAYMENT_SIGNATURE_REJECTION

    if proof.get("x402Version") != 2:
        return None, (
            f"unsupported x402Version: {proof.get('x402Version')!r} "
            "(expected 2)"
        )

    resource = proof.get("resource")
    if (
        not isinstance(resource, dict)
        or not isinstance(resource.get("url"), str)
        or not resource["url"]
    ):
        return None, "payment resource is missing or invalid"

    accepted = proof.get("accepted")
    if not isinstance(accepted, dict):
        return None, "payment requirement is missing or invalid"
    token = token_by_asset(accepted.get("asset"))
    if token is None:
        return None, (
            "payment requirement mismatch"
            if expected_requirement is not None
            else "payment requirement is missing or invalid"
        )
    extra = accepted.get("extra")
    if (
        not isinstance(extra, dict)
        or extra.get("name") != token.domain_name
        or extra.get("version") != token.domain_version
        or extra.get("assetTransferMethod") != token.transfer_method
    ):
        return None, "payment requirement is missing or invalid"
    signer_address = extra.get("signerAddress")
    # EIP-3009 does not bind B402 facilitator metadata. With no trusted
    # expected requirement, this layer checks only signerAddress syntax;
    # B402Client.verify_and_settle refreshes the current supported metadata
    # and exact-compares it before verification, settlement, or job execution.
    if promotional:
        if "signerAddress" in extra:
            return None, "payment requirement is missing or invalid"
    elif not _EVM_ADDRESS.fullmatch(str(signer_address or "")):
        return None, "payment requirement is missing or invalid"
    canonical_extra = (
        expected_requirement.get("extra")
        if expected_requirement is not None
        else extra
    )
    if not isinstance(canonical_extra, Mapping):
        return None, "payment requirement mismatch"
    if (
        canonical_extra.get("name") != token.domain_name
        or canonical_extra.get("version") != token.domain_version
        or canonical_extra.get("assetTransferMethod") != token.transfer_method
    ):
        return None, "payment requirement mismatch"
    if promotional:
        if "signerAddress" in canonical_extra:
            return None, "payment requirement mismatch"
    elif not _EVM_ADDRESS.fullmatch(
        str(canonical_extra.get("signerAddress") or "")
    ):
        return None, "payment requirement mismatch"
    canonical = build_payment_requirement(
        token,
        canonical_extra,
        amount=0 if promotional else PRICE_WEI,
        promotional=promotional,
    )
    if (
        expected_requirement is not None
        and dict(expected_requirement) != canonical
    ):
        return None, "payment requirement mismatch"
    if accepted != canonical:
        return None, "payment requirement mismatch"

    payload_objects = _payment_payload_objects(proof)
    if payload_objects is None:
        return None, _PAYMENT_SIGNATURE_REJECTION
    payload, auth = payload_objects
    signature = str(payload.get("signature") or "")
    from_address = str(auth.get("from", "")).lower()
    to_address = str(auth.get("to", "")).lower()
    value_raw = str(auth.get("value", "0"))
    try:
        value = int(value_raw)
        valid_after = int(auth.get("validAfter", 0))
        valid_before = int(auth.get("validBefore", 0))
        nonce_bytes = bytes.fromhex(
            str(auth.get("nonce", "")).removeprefix("0x").zfill(64)
        )
    except (TypeError, ValueError, OverflowError):
        return None, "authorization contains invalid numeric or nonce fields"

    nonce = "0x" + nonce_bytes.hex()
    current_time = int(time.time()) if now is None else int(now)
    if not from_address.startswith("0x") or len(from_address) != 42:
        return None, f"invalid from address: {from_address!r}"
    if to_address != B402_PAY_TO_ADDRESS.lower():
        return None, f"wrong recipient: {to_address!r}"
    if to_address != str(accepted["payTo"]).lower():
        return None, f"wrong recipient: {to_address!r}"
    if value != int(accepted["amount"]):
        return None, "authorization value does not match payment requirement"
    if len(nonce_bytes) != 32:
        return None, "nonce must be bytes32"
    if current_time < valid_after:
        return None, "authorization not yet valid"
    if current_time >= valid_before and not allow_expired:
        return None, "authorization expired"
    if valid_before - current_time > 3600:
        return None, "authorization valid for more than 1 hour from now"
    if not signature.startswith("0x"):
        return None, "signature must be a 0x-prefixed hex string"

    try:
        digest = _eip712_digest(
            from_address,
            to_address,
            value,
            valid_after,
            valid_before,
            nonce_bytes,
            token=token,
        )
        recovered = Account._recover_hash(digest, signature=signature)
    except Exception as exc:
        return None, f"EIP-712 verification error: {exc}"

    if recovered.lower() != from_address:
        return None, "signature mismatch"

    return (
        VerifiedPayment(
            proof=proof,
            from_address=from_address,
            to_address=to_address,
            value=value,
            valid_after=valid_after,
            valid_before=valid_before,
            nonce=nonce,
            nonce_bytes=nonce_bytes,
            asset=token.address.lower(),
            token_symbol=token.symbol,
            promotional=promotional,
        ),
        "",
    )


# ── Free tier ──────────────────────────────────────────────────────────────────

FREE_TIER_LIMIT  = 10     # calls per wallet per FREE_TIER_WINDOW
FREE_TIER_WINDOW = 86400  # seconds (24 h)

# Sliding-window call log per wallet.  In-memory; resets on restart.
# Replace with Redis ZRANGEBYSCORE / ZADD / ZREMRANGEBYSCORE for persistence.
_free_tier_calls: dict[str, list[float]] = {}


def _check_free_rate_limit(from_addr: str) -> tuple[bool, str]:
    now   = time.time()
    calls = [t for t in _free_tier_calls.get(from_addr, []) if now - t < FREE_TIER_WINDOW]
    if len(calls) >= FREE_TIER_LIMIT:
        secs = int(min(calls) + FREE_TIER_WINDOW - now)
        h, m = divmod(secs // 60, 60)
        return False, f"free tier rate limit: {FREE_TIER_LIMIT}/24h exceeded; resets in {h}h {m:02d}m"
    calls.append(now)
    _free_tier_calls[from_addr] = calls
    return True, f"{FREE_TIER_LIMIT - len(calls)} uses remaining today"


def build_free_payment_challenge(
    symbol: str,
    resource_url: str = "http://localhost:9000/x402/free",
) -> dict:
    """Return x402 v2 free tier challenge (maxAmountRequired=0, wallet identity proof only)."""
    desc = f"Free quick quote for {symbol.upper()}" if symbol else "Free quick quote"
    return {
        "x402Version": 2,
        "accepts": [
            {
                "scheme":            "exact",
                "network":           f"eip155:{CHAIN_ID}",
                "maxAmountRequired": "0",
                "asset":             U_TOKEN_ADDRESS,
                "payTo":             B402_PAY_TO_ADDRESS.lower(),
                "maxTimeoutSeconds": 600,
                "extra": {
                    "assetTransferMethod": "eip3009",
                    "name":      _TOKEN_DOMAIN_NAME,
                    "version":   _TOKEN_DOMAIN_VERSION,
                    "tier":      "free",
                    "rateLimit": f"{FREE_TIER_LIMIT}/24h",
                    "description": desc,
                },
            }
        ],
        "error":    "Payment Required",
        "resource": resource_url,
    }


def verify_free_payment_proof(proof_header: str) -> tuple[bool, str, str]:
    """Verify x402 v2 EIP-712 free tier proof (value must be 0).

    Returns (ok, message, from_addr).
      ok=True  → message = "<N> uses remaining today";  from_addr = signer
      ok=False → message = rejection reason;            from_addr = detected addr or ""
    """
    proof = decode_payment_signature(proof_header)
    if proof is None:
        return False, _PAYMENT_SIGNATURE_REJECTION, ""

    if proof.get("x402Version") != 2:
        return False, f"unsupported x402Version: {proof.get('x402Version')!r} (expected 2)", ""
    if proof.get("scheme", "exact") != "exact":
        return False, f"unsupported scheme: {proof.get('scheme')!r}", ""
    network = proof.get("network", f"eip155:{CHAIN_ID}")
    if network != f"eip155:{CHAIN_ID}":
        return False, f"wrong network: {network!r} (expected eip155:{CHAIN_ID})", ""

    payload_objects = _payment_payload_objects(proof)
    if payload_objects is None:
        return False, _PAYMENT_SIGNATURE_REJECTION, ""
    payload, auth = payload_objects
    sig     = str(payload.get("signature") or "")

    try:
        from_addr = str(auth.get("from", "")).lower()
        to_addr = str(auth.get("to", "")).lower()
        value_raw = auth.get("value")
        valid_after = int(auth.get("validAfter", 0))
        valid_before = int(auth.get("validBefore", 0))
        nonce_hex = str(auth.get("nonce", "0x" + "00" * 32))
    except (OverflowError, TypeError, ValueError):
        return False, _PAYMENT_SIGNATURE_REJECTION, ""

    if not from_addr.startswith("0x") or len(from_addr) != 42:
        return False, f"invalid from address: {from_addr!r}", ""
    if to_addr != B402_PAY_TO_ADDRESS.lower():
        return False, f"wrong recipient: {to_addr!r} (expected {B402_PAY_TO_ADDRESS.lower()!r})", from_addr
    if not (
        (type(value_raw) is str and value_raw == "0")
        or (type(value_raw) is int and value_raw == 0)
    ):
        return False, _FREE_VALUE_REJECTION, from_addr
    value = 0

    now = int(time.time())
    if now < valid_after:
        return False, "authorization not yet valid", from_addr
    if now > valid_before:
        return False, "authorization expired", from_addr
    if valid_before - now > 3600:
        return False, "authorization valid for more than 1 hour from now", from_addr
    if not sig.startswith("0x"):
        return False, "signature must be a 0x-prefixed hex string", from_addr

    nonce_key = f"{from_addr}:{nonce_hex}"
    if nonce_key in _used_nonces:
        return False, "nonce already used (replay blocked)", from_addr

    try:
        nonce_bytes = bytes.fromhex(nonce_hex.removeprefix("0x").zfill(64))
        digest      = _eip712_digest(
            from_addr, to_addr, value, valid_after, valid_before, nonce_bytes,
        )
        recovered = Account._recover_hash(digest, signature=sig)
    except Exception as exc:
        return False, f"EIP-712 verification error: {exc}", from_addr

    if recovered.lower() != from_addr:
        return False, (
            f"signature mismatch: recovered {recovered.lower()!r} ≠ from {from_addr!r}"
        ), from_addr

    ok, msg = _check_free_rate_limit(from_addr)
    if not ok:
        return False, msg, from_addr

    _used_nonces.add(nonce_key)
    _log.info("x402 free tier outcome=accepted")
    return True, msg, from_addr
