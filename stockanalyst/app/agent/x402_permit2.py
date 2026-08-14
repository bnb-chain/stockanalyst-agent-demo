"""Strict local verification for Binance Permit2 exact payment proofs.

The Binance wire payload carries only the signature and authorization.  The
EIP-712 domain and types are fixed here rather than accepted from the caller.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import eth_abi
from eth_account import Account
from eth_utils import keccak, to_checksum_address

try:
    from .x402_tokens import PaymentToken
except ImportError:  # Direct imports from stockanalyst/app/agent.
    from x402_tokens import PaymentToken


PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
UINT256_MAX = 2**256 - 1
PERMIT2_DOMAIN_TYPE = (
    "EIP712Domain(string name,uint256 chainId,address verifyingContract)"
)
TOKEN_PERMISSIONS_TYPE = "TokenPermissions(address token,uint256 amount)"
WITNESS_TYPE = "Witness(address to,uint256 validAfter)"
PERMIT_WITNESS_TYPE = (
    "PermitWitnessTransferFrom(TokenPermissions permitted,address spender,"
    "uint256 nonce,uint256 deadline,Witness witness)"
    + TOKEN_PERMISSIONS_TYPE
    + WITNESS_TYPE
)

_CHAIN_ID = 56
_UINT256_DECIMAL_DIGITS = len(str(UINT256_MAX))
_PROOF_REJECTION = "Permit2 payment proof is invalid"
_REQUIREMENT_REJECTION = "payment requirement mismatch"
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z", flags=re.ASCII)
_SIGNATURE = re.compile(r"0x[0-9a-fA-F]{130}\Z", flags=re.ASCII)
_EXTRA_KEYS = frozenset(
    {
        "name",
        "version",
        "assetTransferMethod",
        "signerAddress",
        "spenderAddress",
    }
)
_PAYLOAD_KEYS = frozenset({"signature", "permit2Authorization"})
_AUTHORIZATION_KEYS = frozenset(
    {"permitted", "from", "spender", "nonce", "deadline", "witness"}
)
_PERMITTED_KEYS = frozenset({"token", "amount"})
_WITNESS_KEYS = frozenset({"to", "validAfter"})

_DOMAIN_TYPE_HASH = keccak(text=PERMIT2_DOMAIN_TYPE)
_TOKEN_PERMISSIONS_TYPE_HASH = keccak(text=TOKEN_PERMISSIONS_TYPE)
_WITNESS_TYPE_HASH = keccak(text=WITNESS_TYPE)
_PERMIT_WITNESS_TYPE_HASH = keccak(text=PERMIT_WITNESS_TYPE)


def canonical_uint256(value: object) -> int | None:
    if type(value) is int:
        parsed = value
    elif isinstance(value, str) and (
        value == "0"
        or (
            bool(value)
            and len(value) <= _UINT256_DECIMAL_DIGITS
            and value[0] in "123456789"
            and value.isascii()
            and value.isdecimal()
        )
    ):
        parsed = int(value)
    else:
        return None
    return parsed if 0 <= parsed <= UINT256_MAX else None


def _exact_object(
    value: object,
    keys: frozenset[str],
) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.keys() != keys:
        return None
    return value


def _address(value: object) -> str | None:
    if not isinstance(value, str) or _ADDRESS.fullmatch(value) is None:
        return None
    return value.lower()


def _wire_uint256(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    return canonical_uint256(value)


def _valid_extra(extra: object, token: PaymentToken) -> bool:
    return bool(
        isinstance(extra, Mapping)
        and _EXTRA_KEYS.issubset(extra.keys())
        and extra.get("name") == token.domain_name
        and extra.get("version") == token.domain_version
        and extra.get("assetTransferMethod") == token.transfer_method
        and _address(extra.get("signerAddress")) is not None
        and _address(extra.get("spenderAddress")) is not None
    )


def _domain_separator() -> bytes:
    return keccak(
        eth_abi.encode(
            ["bytes32", "bytes32", "uint256", "address"],
            [
                _DOMAIN_TYPE_HASH,
                keccak(text="Permit2"),
                _CHAIN_ID,
                to_checksum_address(PERMIT2_ADDRESS),
            ],
        )
    )


def _permit2_digest(
    *,
    token: str,
    amount: int,
    spender: str,
    nonce: int,
    deadline: int,
    to: str,
    valid_after: int,
) -> bytes:
    permissions_hash = keccak(
        eth_abi.encode(
            ["bytes32", "address", "uint256"],
            [
                _TOKEN_PERMISSIONS_TYPE_HASH,
                to_checksum_address(token),
                amount,
            ],
        )
    )
    witness_hash = keccak(
        eth_abi.encode(
            ["bytes32", "address", "uint256"],
            [
                _WITNESS_TYPE_HASH,
                to_checksum_address(to),
                valid_after,
            ],
        )
    )
    permit_hash = keccak(
        eth_abi.encode(
            [
                "bytes32",
                "bytes32",
                "address",
                "uint256",
                "uint256",
                "bytes32",
            ],
            [
                _PERMIT_WITNESS_TYPE_HASH,
                permissions_hash,
                to_checksum_address(spender),
                nonce,
                deadline,
                witness_hash,
            ],
        )
    )
    return keccak(b"\x19\x01" + _domain_separator() + permit_hash)


def verify_permit2_exact(
    proof: dict[str, Any],
    token: PaymentToken,
    expected_requirement: Mapping[str, Any] | None,
    now: int,
    allow_expired: bool,
) -> tuple[Any | None, str]:
    """Verify one exact Permit2 proof without consuming its nonce."""
    try:
        from .x402_verify import (
            PRICE_WEI,
            VerifiedPayment,
            build_payment_requirement,
        )
    except ImportError:  # Direct imports from stockanalyst/app/agent.
        from x402_verify import (  # type: ignore[no-redef]
            PRICE_WEI,
            VerifiedPayment,
            build_payment_requirement,
        )

    accepted = proof.get("accepted")
    if not isinstance(accepted, dict):
        return None, _REQUIREMENT_REJECTION
    accepted_extra = accepted.get("extra")
    if not _valid_extra(accepted_extra, token):
        return None, _REQUIREMENT_REJECTION

    expected_extra: object = accepted_extra
    if expected_requirement is not None:
        if not isinstance(expected_requirement, Mapping):
            return None, _REQUIREMENT_REJECTION
        expected_extra = expected_requirement.get("extra")
    if not _valid_extra(expected_extra, token):
        return None, _REQUIREMENT_REJECTION

    canonical = build_payment_requirement(token, expected_extra)
    if (
        expected_requirement is not None
        and dict(expected_requirement) != canonical
    ):
        return None, _REQUIREMENT_REJECTION
    if accepted != canonical:
        return None, _REQUIREMENT_REJECTION

    payload = _exact_object(proof.get("payload"), _PAYLOAD_KEYS)
    if payload is None:
        return None, _PROOF_REJECTION
    authorization = _exact_object(
        payload.get("permit2Authorization"),
        _AUTHORIZATION_KEYS,
    )
    if authorization is None:
        return None, _PROOF_REJECTION
    permitted = _exact_object(
        authorization.get("permitted"),
        _PERMITTED_KEYS,
    )
    witness = _exact_object(
        authorization.get("witness"),
        _WITNESS_KEYS,
    )
    if permitted is None or witness is None:
        return None, _PROOF_REJECTION

    signature = payload.get("signature")
    payer = _address(authorization.get("from"))
    spender = _address(authorization.get("spender"))
    permitted_token = _address(permitted.get("token"))
    recipient = _address(witness.get("to"))
    amount = _wire_uint256(permitted.get("amount"))
    nonce = _wire_uint256(authorization.get("nonce"))
    deadline = _wire_uint256(authorization.get("deadline"))
    valid_after = _wire_uint256(witness.get("validAfter"))
    if (
        not isinstance(signature, str)
        or _SIGNATURE.fullmatch(signature) is None
        or payer is None
        or spender is None
        or permitted_token is None
        or recipient is None
        or amount is None
        or nonce is None
        or deadline is None
        or valid_after is None
    ):
        return None, _PROOF_REJECTION

    accepted_extra_dict = dict(accepted_extra)
    if (
        permitted_token != token.address.lower()
        or amount != PRICE_WEI
        or amount != canonical_uint256(accepted.get("amount"))
        or spender != str(accepted_extra_dict["spenderAddress"]).lower()
        or recipient != str(accepted["payTo"]).lower()
        or valid_after >= deadline
    ):
        return None, _PROOF_REJECTION

    if now < valid_after:
        return None, "authorization not yet valid"
    if now >= deadline and not allow_expired:
        return None, "authorization expired"
    if deadline - now > 3600:
        return None, "authorization valid for more than 1 hour from now"

    try:
        digest = _permit2_digest(
            token=permitted_token,
            amount=amount,
            spender=spender,
            nonce=nonce,
            deadline=deadline,
            to=recipient,
            valid_after=valid_after,
        )
        recovered = Account._recover_hash(digest, signature=signature)
    except Exception:
        return None, _PROOF_REJECTION
    if recovered.lower() != payer:
        return None, _PROOF_REJECTION

    nonce_bytes = nonce.to_bytes(32, "big")
    return (
        VerifiedPayment(
            proof=proof,
            from_address=payer,
            to_address=recipient,
            value=amount,
            valid_after=valid_after,
            valid_before=deadline,
            nonce=str(nonce),
            nonce_bytes=nonce_bytes,
            asset=token.address.lower(),
            token_symbol=token.symbol,
            transfer_method=token.transfer_method,
            promotional=False,
        ),
        "",
    )
