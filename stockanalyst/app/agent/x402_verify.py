"""Fixed-code x402 v2 EIP-3009 and Permit2 paid-proof verification.

Proofs select U, USD1, USDC, or USDT from the immutable token registry using
the accepted asset address. The selected token supplies the transfer method
and 18-decimal units; the seller price is fixed separately.

This module is never LLM-callable. It verifies signatures locally; on-chain
settlement remains the responsibility of the B402 facilitator integration in
``x402_handler.py``.
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
    from .x402_tokens import U_TOKEN, PaymentToken, token_by_asset
except ImportError:  # Direct imports from stockanalyst/app/agent.
    from x402_tokens import U_TOKEN, PaymentToken, token_by_asset

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
_X402_VERSION_REJECTION = "unsupported x402Version (expected 2)"


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
PRICE_WEI = 100_000_000_000_000_000
LEGACY_PAID_AMOUNT_FOR_RECOVERY = 210_000_000_000_000_000
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
    transfer_method: str = U_TOKEN.transfer_method

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
    amount: int = PRICE_WEI,
) -> dict[str, Any]:
    """Build an exact paid-token requirement."""
    clean_extra = copy.deepcopy(dict(extra))
    return {
        "scheme": "exact",
        "network": f"eip155:{CHAIN_ID}",
        "amount": str(amount),
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
) -> tuple[VerifiedPayment | None, str]:
    """Validate a proof without consuming its nonce.

    ``allow_expired`` is only for locating an existing recovery record. It
    bypasses the wall-clock expiry rejection but preserves every other
    semantic, domain, and cryptographic check.
    """
    return _validate_payment_proof_amount(
        proof_header,
        expected_requirement=expected_requirement,
        now=now,
        allow_expired=allow_expired,
        required_amount=PRICE_WEI,
    )


def validate_legacy_paid_payment_proof(
    proof_header: str,
    *,
    now: int | None = None,
    allow_expired: bool = True,
) -> tuple[VerifiedPayment | None, str]:
    """Locally authenticate one former-0.21 proof for durable lookup only."""
    return _validate_payment_proof_amount(
        proof_header,
        expected_requirement=None,
        now=now,
        allow_expired=allow_expired,
        required_amount=LEGACY_PAID_AMOUNT_FOR_RECOVERY,
    )


def _validate_payment_proof_amount(
    proof_header: str,
    *,
    expected_requirement: Mapping[str, Any] | None,
    now: int | None,
    allow_expired: bool,
    required_amount: int,
) -> tuple[VerifiedPayment | None, str]:
    proof = decode_payment_signature(proof_header)
    if proof is None:
        return None, _PAYMENT_SIGNATURE_REJECTION

    if proof.get("x402Version") != 2:
        return None, _X402_VERSION_REJECTION

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
    if (
        required_amount == LEGACY_PAID_AMOUNT_FOR_RECOVERY
        and token.symbol == "USDC"
    ):
        return None, "payment requirement is missing or invalid"
    if token.transfer_method == "permit2-exact":
        try:
            from .x402_permit2 import verify_permit2_exact
        except ImportError:  # Direct imports from stockanalyst/app/agent.
            from x402_permit2 import verify_permit2_exact
        return verify_permit2_exact(
            proof,
            token=token,
            expected_requirement=expected_requirement,
            now=int(time.time()) if now is None else int(now),
            allow_expired=allow_expired,
            required_amount=required_amount,
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
    if not _EVM_ADDRESS.fullmatch(str(signer_address or "")):
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
    if not _EVM_ADDRESS.fullmatch(
        str(canonical_extra.get("signerAddress") or "")
    ):
        return None, "payment requirement mismatch"
    canonical = build_payment_requirement(
        token,
        canonical_extra,
        amount=required_amount,
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
            transfer_method=token.transfer_method,
        ),
        "",
    )
