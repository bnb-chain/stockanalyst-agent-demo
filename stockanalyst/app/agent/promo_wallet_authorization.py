"""Local EIP-712 wallet identity verification for promotional jobs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import is_address

_CHAIN_ID = 56
_METHOD = "POST"
_PATH = "/x402/analyze/async"
_MAX_TIMEOUT_SECONDS = 600
_MAX_RECOVERY_AGE_SECONDS = 7 * 24 * 60 * 60
_MAX_HEADER_CHARACTERS = 4096
_BASE64URL_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_NONCE_RE = re.compile(r"0x[0-9a-f]{64}\Z")
_SIGNATURE_RE = re.compile(r"0x[0-9a-fA-F]{130}\Z")
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_ENVELOPE_KEYS = frozenset(
    {"version", "address", "nonce", "expiresAt", "signature"}
)

_DOMAIN = {
    "name": "Stock Analyst Promo",
    "version": "1",
    "chainId": _CHAIN_ID,
}
_DOMAIN_TYPES = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
]
_AUTHORIZATION_TYPES = [
    {"name": "address", "type": "address"},
    {"name": "method", "type": "string"},
    {"name": "path", "type": "string"},
    {"name": "bodyHash", "type": "bytes32"},
    {"name": "nonce", "type": "bytes32"},
    {"name": "expiresAt", "type": "uint64"},
]


class PromoWalletAuthorizationError(ValueError):
    """A promotional wallet authorization failed closed."""

    def __init__(self) -> None:
        super().__init__("wallet_signature_invalid")


@dataclass(frozen=True)
class PromoWalletAuthorization:
    address: str
    nonce: str
    expires_at: int
    request_digest: str


def _reject() -> PromoWalletAuthorizationError:
    return PromoWalletAuthorizationError()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _reject()
        result[key] = value
    return result


def _decode_envelope(header: str) -> dict[str, Any]:
    if (
        type(header) is not str
        or not 1 <= len(header) <= _MAX_HEADER_CHARACTERS
        or _BASE64URL_RE.fullmatch(header) is None
    ):
        raise _reject()
    try:
        raw = base64.b64decode(
            header + "=" * (-len(header) % 4),
            altchars=b"-_",
            validate=True,
        )
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != header:
            raise _reject()
        decoded = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
        )
    except (
        binascii.Error,
        PromoWalletAuthorizationError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise _reject() from exc
    if type(decoded) is not dict or frozenset(decoded) != _ENVELOPE_KEYS:
        raise _reject()
    return decoded


def _typed_data(
    *,
    address: str,
    nonce: str,
    expires_at: int,
    request_digest: str,
) -> dict[str, Any]:
    return {
        "types": {
            "EIP712Domain": _DOMAIN_TYPES,
            "PromoAuthorization": _AUTHORIZATION_TYPES,
        },
        "primaryType": "PromoAuthorization",
        "domain": _DOMAIN,
        "message": {
            "address": address,
            "method": _METHOD,
            "path": _PATH,
            "bodyHash": "0x" + request_digest,
            "nonce": nonce,
            "expiresAt": expires_at,
        },
    }


def verify_promo_wallet_authorization(
    header: str,
    body: bytes,
    *,
    now: int,
    allow_expired: bool = False,
) -> PromoWalletAuthorization:
    """Verify a bounded promotional identity proof without RPC or settlement."""
    if type(body) is not bytes or type(now) is not int or now < 0:
        raise _reject()
    envelope = _decode_envelope(header)
    version = envelope.get("version")
    address = envelope.get("address")
    nonce = envelope.get("nonce")
    expires_at = envelope.get("expiresAt")
    signature = envelope.get("signature")
    if (
        type(version) is not int
        or version != 1
        or type(address) is not str
        or _ADDRESS_RE.fullmatch(address) is None
        or not is_address(address)
        or type(nonce) is not str
        or _NONCE_RE.fullmatch(nonce) is None
        or type(expires_at) is not int
        or (
            not now < expires_at <= now + _MAX_TIMEOUT_SECONDS
            and not (
                allow_expired is True
                and now - _MAX_RECOVERY_AGE_SECONDS < expires_at <= now
            )
        )
        or type(signature) is not str
        or _SIGNATURE_RE.fullmatch(signature) is None
    ):
        raise _reject()

    request_digest = hashlib.sha256(body).hexdigest()
    try:
        recovered = Account.recover_message(
            encode_typed_data(
                full_message=_typed_data(
                    address=address,
                    nonce=nonce,
                    expires_at=expires_at,
                    request_digest=request_digest,
                )
            ),
            signature=signature,
        )
    except Exception as exc:
        raise _reject() from exc
    if recovered.lower() != address.lower():
        raise _reject()
    return PromoWalletAuthorization(
        address=address.lower(),
        nonce=nonce,
        expires_at=expires_at,
        request_digest=request_digest,
    )


def promo_wallet_metadata() -> dict[str, object]:
    """Return the fixed public identity-signing contract for promo callers."""
    return {
        "scheme": "eip712-wallet",
        "network": f"eip155:{_CHAIN_ID}",
        "header": "Wallet-Signature",
        "maxTimeoutSeconds": _MAX_TIMEOUT_SECONDS,
        "domain": dict(_DOMAIN),
        "primaryType": "PromoAuthorization",
        "types": {
            "PromoAuthorization": [dict(field) for field in _AUTHORIZATION_TYPES]
        },
        "message": {
            "method": _METHOD,
            "path": _PATH,
            "bodyHash": "sha256",
        },
    }
