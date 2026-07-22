"""Strict validation and authorization primitives for ``notify_funded``."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from ipaddress import ip_address
import json
import math
import os
import re
import socket
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import to_checksum_address


_MAX_CONTEXT_BYTES = 65_536
_MAX_GATEWAY_URL_LENGTH = 2_048
_MAX_GATEWAY_TOKEN_LENGTH = 2_048
_MAX_HOLDINGS = 50
_MAX_VALUE = 10**12
_SYMBOL_PATTERN = re.compile(r"[A-Z][A-Z0-9.-]{0,9}\Z")
_CURRENCY_PATTERN = re.compile(r"[A-Z]{3,8}\Z")
_RISK_TOLERANCES = frozenset({"conservative", "moderate", "aggressive"})
_SUPPORTED_INDICATORS = frozenset(
    {"RSI-14", "MACD", "Bollinger Bands", "MA50/200", "ADX", "OBV", "ATR", "VaR"}
)
_ROOT_KEYS = frozenset(
    {"delivery_gateway_url", "delivery_gateway_token", "portfolio", "risk_profile"}
)
_HOLDING_KEYS = frozenset({"symbol", "shares", "avgCost", "currency"})
_RISK_PROFILE_KEYS = frozenset({"tolerance", "horizonMonths", "preferredIndicators"})
_AUTHORIZATION_KEYS = frozenset({"context", "expires_at", "nonce", "signature"})
_NONCE_PATTERN = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_SIGNATURE_PATTERN = re.compile(r"(?:0x)?[0-9a-fA-F]{130}\Z")
_MAX_UINT64 = 2**64 - 1
_MAX_UINT256 = 2**256 - 1
_DEFAULT_GATEWAY_HOST_RULES = (".trycloudflare.com",)
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


class NotifySecurityError(ValueError):
    """A caller-safe failure with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_gateway_url(
    url: str,
    *,
    allow_private: bool | None = None,
    resolver: Callable[..., object] | None = None,
) -> str:
    """Validate a gateway origin and return its canonical origin string.

    Production origins must be allowlisted HTTPS endpoints whose complete DNS
    answer set is globally routable.  Development mode adds one narrow
    exception: HTTP is permitted when every answer is loopback.
    """
    if not isinstance(url, str) or not url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise NotifySecurityError("invalid_gateway_url")
    # ``urlsplit`` discards empty query/fragment delimiters, so reject the
    # delimiters themselves rather than silently normalizing them away.
    if "?" in url or "#" in url or "\\" in url:
        raise NotifySecurityError("invalid_gateway_url")

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError, UnicodeError):
        raise NotifySecurityError("invalid_gateway_url") from None

    scheme = parsed.scheme.lower()
    if (
        hostname is None
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or scheme not in {"http", "https"}
    ):
        raise NotifySecurityError("invalid_gateway_url")

    hostname = hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise NotifySecurityError("invalid_gateway_url")

    if allow_private is None:
        allow_private = os.environ.get("ALLOW_PRIVATE_DELIVERY_GATEWAY", "").strip().lower() in _TRUE_ENV_VALUES
    elif not isinstance(allow_private, bool):
        raise NotifySecurityError("invalid_gateway_url")

    expected_port = 443 if scheme == "https" else 80
    if scheme == "https" and port not in (None, 443):
        raise NotifySecurityError("invalid_gateway_url")
    if scheme == "http" and not allow_private:
        raise NotifySecurityError("invalid_gateway_url")

    lookup = socket.getaddrinfo if resolver is None else resolver
    try:
        answers = lookup(hostname, port or expected_port, type=socket.SOCK_STREAM)
        addresses = [ip_address(answer[4][0]) for answer in answers]  # type: ignore[index]
    except (OSError, TypeError, ValueError, IndexError):
        raise NotifySecurityError("invalid_gateway_url") from None
    if not addresses:
        raise NotifySecurityError("invalid_gateway_url")

    loopback_http = scheme == "http" and allow_private and all(address.is_loopback for address in addresses)
    if scheme == "http" and not loopback_http:
        raise NotifySecurityError("invalid_gateway_url")
    if scheme == "https" and not all(_is_public_gateway_address(address) for address in addresses):
        raise NotifySecurityError("invalid_gateway_url")
    if not loopback_http and not _gateway_host_allowed(hostname):
        raise NotifySecurityError("invalid_gateway_url")

    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    rendered_port = "" if port in (None, expected_port) else f":{port}"
    return f"{scheme}://{rendered_host}{rendered_port}"


def _is_public_gateway_address(address: Any) -> bool:
    return bool(
        address.is_global
        and not address.is_loopback
        and not address.is_private
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
    )


def _gateway_host_allowed(hostname: str) -> bool:
    configured = os.environ.get("DELIVERY_GATEWAY_ALLOWED_HOSTS")
    rules = _DEFAULT_GATEWAY_HOST_RULES if configured is None else tuple(
        item.strip().lower() for item in configured.split(",") if item.strip()
    )
    for rule in rules:
        if rule.startswith("."):
            if hostname.endswith(rule) and hostname != rule[1:]:
                return True
        elif hostname == rule:
            return True
    return False


@dataclass(frozen=True)
class Holding:
    """A normalized portfolio holding from the signed context."""

    symbol: str
    shares: float | int
    avg_cost: float | int
    currency: str


@dataclass(frozen=True)
class RiskProfile:
    """A normalized risk profile from the signed context."""

    tolerance: str
    horizon_months: int
    preferred_indicators: tuple[str, ...]


@dataclass(frozen=True)
class JobContext:
    """Immutable, validated fields derived from the exact signed JSON string."""

    digest: str
    gateway_url: str | None
    gateway_token: str | None
    portfolio: tuple[Holding, ...]
    risk_profile: RiskProfile | None

    def portfolio_for_prompt(self) -> list[dict[str, object]]:
        """Return a fresh, prompt-safe representation of the holdings."""
        return [
            {
                "symbol": holding.symbol,
                "shares": holding.shares,
                "avgCost": holding.avg_cost,
                "currency": holding.currency,
            }
            for holding in self.portfolio
        ]

    def risk_profile_for_prompt(self) -> dict[str, object] | None:
        """Return a fresh, prompt-safe representation of the risk profile."""
        if self.risk_profile is None:
            return None
        return {
            "tolerance": self.risk_profile.tolerance,
            "horizonMonths": self.risk_profile.horizon_months,
            "preferredIndicators": list(self.risk_profile.preferred_indicators),
        }


def parse_signed_context(raw: str) -> JobContext:
    """Validate an exact signed JSON context and return immutable normalized data."""
    try:
        encoded = raw.encode("utf-8")
        if len(encoded) > _MAX_CONTEXT_BYTES:
            raise ValueError
        parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(parsed, dict) or set(parsed) - _ROOT_KEYS:
            raise ValueError

        gateway_url = _optional_string(
            parsed, "delivery_gateway_url", maximum_length=_MAX_GATEWAY_URL_LENGTH
        )
        gateway_token = _optional_string(
            parsed,
            "delivery_gateway_token",
            maximum_length=_MAX_GATEWAY_TOKEN_LENGTH,
            minimum_length=1,
        )
        if gateway_token is not None and ("\r" in gateway_token or "\n" in gateway_token):
            raise ValueError

        portfolio = _parse_portfolio(parsed.get("portfolio", []))
        risk_profile = _parse_risk_profile(parsed["risk_profile"]) if "risk_profile" in parsed else None
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise NotifySecurityError("invalid_context") from None

    return JobContext(
        digest=hashlib.sha256(encoded).hexdigest(),
        gateway_url=gateway_url,
        gateway_token=gateway_token,
        portfolio=portfolio,
        risk_profile=risk_profile,
    )


def build_notify_typed_data(
    *,
    job_id: int,
    context: str,
    expires_at: int,
    nonce: str,
    chain_id: int,
    verifying_contract: str,
) -> dict[str, object]:
    """Build the fixed EIP-712 message for a notify authorization."""
    _require_uint(job_id, maximum=_MAX_UINT256)
    _require_uint(expires_at, maximum=_MAX_UINT64)
    _require_uint(chain_id, maximum=_MAX_UINT256)
    if (
        not isinstance(context, str)
        or not isinstance(nonce, str)
        or _NONCE_PATTERN.fullmatch(nonce) is None
    ):
        raise NotifySecurityError("invalid_authorization")
    try:
        contract = to_checksum_address(verifying_contract)
    except (TypeError, ValueError):
        raise NotifySecurityError("invalid_authorization") from None

    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "NotifyFunded": [
                {"name": "jobId", "type": "uint256"},
                {"name": "context", "type": "string"},
                {"name": "expiresAt", "type": "uint64"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "NotifyFunded",
        "domain": {
            "name": "stockanalyst-notify-funded",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": contract,
        },
        "message": {
            "jobId": job_id,
            "context": context,
            "expiresAt": expires_at,
            "nonce": nonce,
        },
    }


def verify_notify_authorization(
    authorization: object,
    *,
    job_id: int,
    expected_client: str,
    chain_id: int,
    verifying_contract: str,
    now: int | None = None,
) -> JobContext:
    """Recover and verify the job client's EIP-712 authorization envelope."""
    if not isinstance(authorization, dict):
        raise NotifySecurityError("authorization_required")
    if not _AUTHORIZATION_KEYS <= set(authorization):
        raise NotifySecurityError("authorization_required")
    if set(authorization) != _AUTHORIZATION_KEYS:
        raise NotifySecurityError("invalid_authorization")

    context = authorization["context"]
    expires_at = authorization["expires_at"]
    nonce = authorization["nonce"]
    signature = authorization["signature"]
    if (
        not isinstance(context, str)
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or not 0 <= expires_at <= _MAX_UINT64
        or not isinstance(nonce, str)
        or _NONCE_PATTERN.fullmatch(nonce) is None
        or not isinstance(signature, str)
        or _SIGNATURE_PATTERN.fullmatch(signature) is None
    ):
        raise NotifySecurityError("invalid_authorization")

    current_time = int(time.time()) if now is None else now
    if isinstance(current_time, bool) or not isinstance(current_time, int):
        raise NotifySecurityError("invalid_authorization")
    if expires_at < current_time - 30:
        raise NotifySecurityError("authorization_expired")
    if expires_at > current_time + 600:
        raise NotifySecurityError("invalid_authorization")

    try:
        expected = to_checksum_address(expected_client)
        typed_data = build_notify_typed_data(
            job_id=job_id,
            context=context,
            expires_at=expires_at,
            nonce=nonce,
            chain_id=chain_id,
            verifying_contract=verifying_contract,
        )
        recovered = Account.recover_message(encode_typed_data(full_message=typed_data), signature=signature)
    except (TypeError, ValueError):
        raise NotifySecurityError("invalid_authorization") from None

    if to_checksum_address(recovered) != expected:
        raise NotifySecurityError("caller_not_job_client")
    return parse_signed_context(context)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _optional_string(
    values: dict[str, Any],
    key: str,
    *,
    maximum_length: int,
    minimum_length: int = 0,
) -> str | None:
    if key not in values:
        return None
    value = values[key]
    if not isinstance(value, str) or not minimum_length <= len(value) <= maximum_length:
        raise ValueError
    return value


def _parse_portfolio(value: Any) -> tuple[Holding, ...]:
    if not isinstance(value, list) or len(value) > _MAX_HOLDINGS:
        raise ValueError

    holdings: list[Holding] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _HOLDING_KEYS:
            raise ValueError
        symbol = item["symbol"]
        currency = item["currency"]
        shares = _finite_number(item["shares"])
        avg_cost = _finite_number(item["avgCost"])
        if (
            not isinstance(symbol, str)
            or _SYMBOL_PATTERN.fullmatch(symbol) is None
            or not isinstance(currency, str)
            or _CURRENCY_PATTERN.fullmatch(currency) is None
            or not 0 < shares <= _MAX_VALUE
            or not 0 <= avg_cost <= _MAX_VALUE
        ):
            raise ValueError
        holdings.append(Holding(symbol, shares, avg_cost, currency))
    return tuple(holdings)


def _finite_number(value: Any) -> float | int:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError


def _parse_risk_profile(value: Any) -> RiskProfile | None:
    if not isinstance(value, dict) or set(value) != _RISK_PROFILE_KEYS:
        raise ValueError

    tolerance = value["tolerance"]
    horizon_months = value["horizonMonths"]
    indicators = value["preferredIndicators"]
    if (
        not isinstance(tolerance, str)
        or tolerance not in _RISK_TOLERANCES
        or isinstance(horizon_months, bool)
        or not isinstance(horizon_months, int)
        or not 1 <= horizon_months <= 600
        or not isinstance(indicators, list)
        or any(not isinstance(indicator, str) or indicator not in _SUPPORTED_INDICATORS for indicator in indicators)
        or len(indicators) != len(set(indicators))
    ):
        raise ValueError
    return RiskProfile(tolerance, horizon_months, tuple(indicators))


def _require_uint(value: object, *, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise NotifySecurityError("invalid_authorization")
