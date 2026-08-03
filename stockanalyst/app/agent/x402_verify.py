"""x402 v2 payment proof verification — FIXED CODE, never LLM-callable.

Signing scheme: EIP-712 TransferWithAuthorization (EIP-3009).
The buyer uses their Web3 wallet (Binance Web3 Wallet / MetaMask / ethers.js
signTypedData) to sign a typed-data authorization; the seller verifies the
EIP-712 signature locally in this module. On-chain settlement is handled
separately by the Binance Pay x402 facilitator (called from x402_handler.py).

Wire format — X-Payment header = base64(JSON):
  {
    "x402Version": 2,
    "resource": {"url": "https://<agent>/x402/analyze/async", ...},
    "accepted": {
      "scheme": "exact", "network": "eip155:97",
      "amount": "1000000000000000000",
      "asset": "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565",
      "payTo": "0x<seller>", "maxTimeoutSeconds": 600,
      "extra": {
        "name": "U", "version": "1",
        "assetTransferMethod": "eip3009",
        "signerAddress": "0x<facilitator>"
      }
    },
    "payload": {
      "signature":     "0x<65-byte EIP-712 sig>",
      "authorization": {
        "from":        "0x<buyer>",
        "to":          "0x<seller>",       // must equal SELLER_WALLET
        "value":       "1000000000000000000",  // 1.0 U in wei (≥ MIN_PRICE_WEI)
        "validAfter":  "0",
        "validBefore": "<unix_ts>",        // +10 min TTL recommended
        "nonce":       "0x<32 random bytes>"  // bytes32
      }
    }
  }

EIP-712 domain for U token (BSC Testnet):
  name:              env U_TOKEN_DOMAIN_NAME    (default "U")
  version:           env U_TOKEN_DOMAIN_VERSION (default "1")
  chainId:           97
  verifyingContract: 0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565

EIP-712 primary type: TransferWithAuthorization(
    address from, address to, uint256 value,
    uint256 validAfter, uint256 validBefore, bytes32 nonce)

To generate a test proof (Python):
  python - <<'EOF'
  import json, base64, time, os
  from eth_account import Account
  from eth_utils import keccak, to_checksum_address
  import eth_abi, secrets

  PRIV = "0x<private-key>"
  SELLER = "0x<seller-wallet>"
  TOKEN  = "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565"
  acct = Account.from_key(PRIV)

  domain_type = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
  domain_sep  = keccak(eth_abi.encode(["bytes32","bytes32","bytes32","uint256","address"],
    [domain_type, keccak(text="U"), keccak(text="1"), 97, to_checksum_address(TOKEN)]))
  type_hash = keccak(text="TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)")
  nonce = secrets.token_bytes(32)
  auth = {"from": acct.address.lower(), "to": SELLER,
          "value": "1000000000000000000", "validAfter": "0",
          "validBefore": str(int(time.time())+600), "nonce": "0x"+nonce.hex()}
  struct_hash = keccak(eth_abi.encode(
    ["bytes32","address","address","uint256","uint256","uint256","bytes32"],
    [type_hash, to_checksum_address(auth["from"]), to_checksum_address(auth["to"]),
     int(auth["value"]), int(auth["validAfter"]), int(auth["validBefore"]), nonce]))
  digest = keccak(b"\\x19\\x01" + domain_sep + struct_hash)
  sig = Account._sign_hash(digest, PRIV).signature.hex()
  requirement = {"scheme":"exact","network":"eip155:97",
    "amount":"1000000000000000000","asset":TOKEN,"payTo":SELLER.lower(),
    "maxTimeoutSeconds":600,"extra":{"name":"U","version":"1",
      "assetTransferMethod":"eip3009","signerAddress":"0x<from-/supported>"}}
  proof = {"x402Version":2,
           "resource":{"url":"https://<agent>/x402/analyze/async"},
           "accepted":requirement,
           "payload":{"signature":"0x"+sig,"authorization":auth}}
  print(base64.b64encode(json.dumps(proof).encode()).decode())
  EOF
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from eth_account import Account

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


def _resolve_seller_wallet(
    env: Mapping[str, str] = os.environ,
    studio_loader: Callable[[], Mapping[str, Any] | None] | None = None,
) -> str:
    explicit = env.get("X402_SELLER_WALLET", "").strip()
    if explicit:
        raw = explicit
    else:
        try:
            if studio_loader is None:
                from bnbagent_studio_core import config
                studio_loader = config.load_studio_toml
            studio = studio_loader() or {}
            raw = str((studio.get("wallet") or {}).get("address") or "").strip()
        except Exception as exc:
            raise RuntimeError("x402 seller wallet configuration unavailable") from exc
    if not _EVM_ADDRESS.fullmatch(raw):
        raise RuntimeError("x402 seller wallet must be a 0x-prefixed EVM address")
    return raw


SELLER_WALLET       = _resolve_seller_wallet()
U_TOKEN_BSC_TESTNET = "0x330949Aed7d00FCe0558C64ED6FeC9792616cC39"
PRICE_WEI           = 10**6         # 1.0 U (6 decimals)
MIN_PRICE_WEI       = 5 * 10**5     # 0.5 U (6 decimals)
CHAIN_ID            = 97            # BSC Testnet


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

# U token EIP-712 domain — set env vars if the deployed contract differs.
# Verify via: cast call <U_TOKEN> "name()" --rpc-url $BSC_TESTNET_RPC
_TOKEN_DOMAIN_NAME    = os.environ.get("U_TOKEN_DOMAIN_NAME",    "U")
_TOKEN_DOMAIN_VERSION = os.environ.get("U_TOKEN_DOMAIN_VERSION", "1")

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


def _domain_separator(
    name: str = _TOKEN_DOMAIN_NAME,
    version: str = _TOKEN_DOMAIN_VERSION,
) -> bytes:
    import eth_abi
    from eth_utils import to_checksum_address
    return _keccak(eth_abi.encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [
            _DOMAIN_TYPE_HASH,
            _ktext(name),
            _ktext(version),
            CHAIN_ID,
            to_checksum_address(U_TOKEN_BSC_TESTNET),
        ],
    ))


def _eip712_digest(
    from_: str, to: str, value: int,
    valid_after: int, valid_before: int, nonce: bytes,
    *,
    domain_name: str = _TOKEN_DOMAIN_NAME,
    domain_version: str = _TOKEN_DOMAIN_VERSION,
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
        + _domain_separator(domain_name, domain_version)
        + struct_hash
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def build_payment_challenge(
    symbols: list[str],
    resource_url: str,
    extra: Mapping[str, Any],
) -> dict:
    """Return x402 v2 standard payment challenge (HTTP 402 body / X-Payment-Required header)."""
    description = (
        f"Stock analysis for {', '.join(s.upper() for s in symbols)}"
        if symbols else "Stock analysis report"
    )
    return {
        "x402Version": 2,
        "accepts": [build_payment_requirement(extra)],
        "error":    "Payment Required",
        "resource": {
            "url": resource_url,
            "description": description,
            "mimeType": "application/json",
        },
    }


def build_payment_requirement(extra: Mapping[str, Any]) -> dict[str, Any]:
    """Build the paid requirement shared by the challenge and B402 calls."""
    return {
        "scheme": "exact",
        "network": f"eip155:{CHAIN_ID}",
        "amount": str(PRICE_WEI),
        "asset": U_TOKEN_BSC_TESTNET,
        "payTo": SELLER_WALLET.lower(),
        "maxTimeoutSeconds": 600,
        "extra": copy.deepcopy(dict(extra)),
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
    try:
        proof = json.loads(base64.b64decode(proof_header.strip()))
    except Exception:
        return None, "X-Payment is not valid base64 JSON"

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
    extra = accepted.get("extra")
    if (
        not isinstance(extra, dict)
        or extra.get("name") != _TOKEN_DOMAIN_NAME
        or extra.get("version") != _TOKEN_DOMAIN_VERSION
        or extra.get("assetTransferMethod") != "eip3009"
        or not _EVM_ADDRESS.fullmatch(str(extra.get("signerAddress") or ""))
    ):
        return None, "payment requirement is missing or invalid"
    required = (
        dict(expected_requirement)
        if expected_requirement is not None
        else build_payment_requirement(extra)
    )
    if accepted != required:
        return None, "payment requirement mismatch"

    payload = proof.get("payload") or {}
    auth = payload.get("authorization") or {}
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
    if to_address != SELLER_WALLET.lower():
        return None, f"wrong recipient: {to_address!r}"
    if to_address != str(accepted["payTo"]).lower():
        return None, f"wrong recipient: {to_address!r}"
    if value < MIN_PRICE_WEI:
        return None, (
            f"value {value / 1e18:.3f} U < minimum "
            f"{MIN_PRICE_WEI / 1e18:.3f} U"
        )
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
            domain_name=str(extra["name"]),
            domain_version=str(extra["version"]),
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


def build_free_payment_challenge(symbol: str, host: str = "localhost:9000") -> dict:
    """Return x402 v2 free tier challenge (maxAmountRequired=0, wallet identity proof only)."""
    desc = f"Free quick quote for {symbol.upper()}" if symbol else "Free quick quote"
    return {
        "x402Version": 2,
        "accepts": [
            {
                "scheme":            "exact",
                "network":           f"eip155:{CHAIN_ID}",
                "maxAmountRequired": "0",
                "asset":             U_TOKEN_BSC_TESTNET,
                "payTo":             SELLER_WALLET.lower(),
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
        "resource": f"http://{host}/x402/free",
    }


def verify_free_payment_proof(proof_header: str) -> tuple[bool, str, str]:
    """Verify x402 v2 EIP-712 free tier proof (value must be 0).

    Returns (ok, message, from_addr).
      ok=True  → message = "<N> uses remaining today";  from_addr = signer
      ok=False → message = rejection reason;            from_addr = detected addr or ""
    """
    try:
        raw   = base64.b64decode(proof_header.strip())
        proof = json.loads(raw)
    except Exception:
        return False, "X-Payment is not valid base64 JSON", ""

    if proof.get("x402Version") != 2:
        return False, f"unsupported x402Version: {proof.get('x402Version')!r} (expected 2)", ""
    if proof.get("scheme", "exact") != "exact":
        return False, f"unsupported scheme: {proof.get('scheme')!r}", ""
    network = proof.get("network", f"eip155:{CHAIN_ID}")
    if network != f"eip155:{CHAIN_ID}":
        return False, f"wrong network: {network!r} (expected eip155:{CHAIN_ID})", ""

    payload = proof.get("payload") or {}
    auth    = payload.get("authorization") or {}
    sig     = str(payload.get("signature") or "")

    from_addr    = str(auth.get("from",        "")).lower()
    to_addr      = str(auth.get("to",          "")).lower()
    value_raw    = str(auth.get("value",       "0"))
    valid_after  = int(auth.get("validAfter",  0))
    valid_before = int(auth.get("validBefore", 0))
    nonce_hex    = str(auth.get("nonce", "0x" + "00" * 32))

    if not from_addr.startswith("0x") or len(from_addr) != 42:
        return False, f"invalid from address: {from_addr!r}", ""
    if to_addr != SELLER_WALLET.lower():
        return False, f"wrong recipient: {to_addr!r} (expected {SELLER_WALLET.lower()!r})", from_addr
    try:
        value = int(value_raw)
    except (ValueError, TypeError):
        return False, f"invalid value: {value_raw!r}", from_addr
    if value != 0:
        return False, (
            f"free tier requires value=0 (got {value / 1e18:.3f} U); "
            "use /x402/analyze/async for paid analysis"
        ), from_addr

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
    _log.info("x402 free tier accepted: from=%s nonce=%s (%s)", from_addr, nonce_hex, msg)
    return True, msg, from_addr
