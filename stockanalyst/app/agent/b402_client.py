"""Binance B402 V2 merchant client.

This module owns merchant authentication and the exact HTTP bytes covered by
the RSA signature. It never logs credentials, request signatures, or payment
payloads.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15


class B402Error(RuntimeError):
    """Base class for B402 integration failures."""


class B402ConfigurationError(B402Error):
    """Merchant configuration is incomplete or invalid."""


class B402IndeterminateError(B402Error):
    """The remote result cannot safely be classified as a rejection."""


class B402RejectedError(B402Error):
    """B402 explicitly rejected a well-formed operation."""


@dataclass(frozen=True)
class B402Config:
    client_id: str
    access_token: str
    base_url: str
    private_key: str

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] = os.environ,
    ) -> B402Config | None:
        names = (
            "B402_CLIENT_ID",
            "B402_ACCESS_TOKEN",
            "B402_BASE_URL",
            "B402_PRIVATE_KEY",
        )
        values = {name: str(env.get(name, "")).strip() for name in names}
        if not any(values.values()):
            return None
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise B402ConfigurationError(
                "incomplete B402 configuration; missing " + ", ".join(missing)
            )

        raw_base_url = values["B402_BASE_URL"].rstrip("/")
        parsed = urlsplit(raw_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise B402ConfigurationError(
                "B402_BASE_URL must be an absolute HTTPS URL"
            )

        try:
            private_key = RSA.import_key(
                base64.b64decode(
                    values["B402_PRIVATE_KEY"],
                    validate=True,
                )
            )
            if not private_key.has_private():
                raise ValueError("not private")
        except Exception as exc:
            raise B402ConfigurationError(
                "B402_PRIVATE_KEY must be a Base64 PKCS#8 RSA private key"
            ) from exc

        return cls(
            client_id=values["B402_CLIENT_ID"],
            access_token=values["B402_ACCESS_TOKEN"],
            base_url=raw_base_url,
            private_key=values["B402_PRIVATE_KEY"],
        )


class _Response(Protocol):
    status_code: int

    def json(self) -> Any: ...


class _HttpClient(Protocol):
    async def __aenter__(self) -> _HttpClient: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def post(
        self,
        url: str,
        *,
        content: bytes,
        headers: dict[str, str],
    ) -> _Response: ...


class B402Client:
    def __init__(
        self,
        config: B402Config,
        *,
        http_client_factory: Callable[[], _HttpClient] | None = None,
        now_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._private_key = RSA.import_key(
            base64.b64decode(config.private_key, validate=True)
        )
        self._http_client_factory = (
            http_client_factory
            if http_client_factory is not None
            else lambda: httpx.AsyncClient(timeout=20.0)
        )
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._monotonic = monotonic or time.monotonic
        self._supported_kinds: list[dict[str, Any]] | None = None
        self._supported_expires_at = 0.0
        self._supported_lock = asyncio.Lock()

    async def post(
        self,
        path: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(self._now_ms())
        signature = base64.b64encode(
            pkcs1_15.new(self._private_key).sign(
                SHA256.new(body + timestamp.encode("ascii"))
            )
        ).decode("ascii")
        headers = {
            "Content-Type": "application/json",
            "X-Tesla-ClientId": self._config.client_id,
            "X-Tesla-SignAccessToken": self._config.access_token,
            "X-Tesla-Timestamp": timestamp,
            "X-Tesla-Signature": signature,
        }
        try:
            async with self._http_client_factory() as client:
                response = await client.post(
                    f"{self._config.base_url}/{path.lstrip('/')}",
                    content=body,
                    headers=headers,
                )
                if response.status_code >= 500 or response.status_code in {
                    401,
                    403,
                }:
                    raise B402IndeterminateError(
                        f"B402 request failed with HTTP {response.status_code}"
                    )
                parsed = response.json()
        except B402IndeterminateError:
            raise
        except Exception as exc:
            raise B402IndeterminateError("B402 request outcome is unknown") from exc

        if not isinstance(parsed, dict):
            raise B402IndeterminateError("B402 returned a malformed response")
        return parsed

    async def payment_extra(
        self,
        network: str,
        name: str,
        version: str,
    ) -> dict[str, Any]:
        kinds = await self._get_supported_kinds()
        for kind in kinds:
            extra = kind.get("extra")
            if (
                kind.get("x402Version") == 2
                and kind.get("scheme") == "exact"
                and kind.get("network") == network
                and isinstance(extra, dict)
                and extra.get("name") == name
                and extra.get("version") == version
                and extra.get("assetTransferMethod") == "eip3009"
                and _is_evm_address(extra.get("signerAddress"))
            ):
                return copy.deepcopy(extra)
        raise B402RejectedError(
            f"B402 does not support exact eip3009 {name} {version} on {network}"
        )

    async def verify_and_settle(
        self,
        payment_payload: Mapping[str, Any],
    ) -> str:
        accepted = payment_payload.get("accepted")
        if not isinstance(accepted, dict):
            raise B402RejectedError("payment requirement is missing")
        extra = accepted.get("extra")
        if not isinstance(extra, dict):
            raise B402RejectedError("payment requirement extra is missing")

        current_extra = await self.payment_extra(
            str(accepted.get("network") or ""),
            str(extra.get("name") or ""),
            str(extra.get("version") or ""),
        )
        if current_extra != extra:
            raise B402RejectedError("payment requirement is no longer supported")

        envelope = {
            "x402Version": 2,
            "paymentPayload": copy.deepcopy(dict(payment_payload)),
            "paymentRequirements": copy.deepcopy(accepted),
        }
        verification = await self.post("/papi/v2/b402/verify", envelope)
        verification_data = _success_data(verification, operation="verify")
        is_valid = verification_data.get("isValid")
        if is_valid is False:
            reason = _reason(
                verification_data,
                "invalidReason",
                "payment verification rejected",
            )
            raise B402RejectedError(reason)
        if is_valid is not True:
            raise B402IndeterminateError(
                "B402 returned a malformed verification result"
            )

        settlement = await self.post("/papi/v2/b402/settle", envelope)
        settlement_data = _success_data(settlement, operation="settle")
        success = settlement_data.get("success")
        transaction = settlement_data.get("transaction")
        if success is True and isinstance(transaction, str) and transaction:
            return transaction
        if success is False and isinstance(transaction, str):
            if transaction:
                raise B402IndeterminateError(
                    "B402 settlement was broadcast but is not confirmed"
                )
            raise B402RejectedError(
                _reason(
                    settlement_data,
                    "errorReason",
                    "payment settlement rejected",
                )
            )
        raise B402IndeterminateError(
            "B402 returned a malformed settlement result"
        )

    async def _get_supported_kinds(self) -> list[dict[str, Any]]:
        now = self._monotonic()
        if (
            self._supported_kinds is not None
            and now < self._supported_expires_at
        ):
            return self._supported_kinds
        async with self._supported_lock:
            now = self._monotonic()
            if (
                self._supported_kinds is not None
                and now < self._supported_expires_at
            ):
                return self._supported_kinds
            response = await self.post("/papi/v2/b402/supported", {})
            data = response.get("data")
            kinds = data.get("kinds") if isinstance(data, dict) else None
            if response.get("code") != "000000" or not isinstance(kinds, list):
                raise B402IndeterminateError(
                    "B402 returned an invalid supported response"
                )
            if not all(isinstance(kind, dict) for kind in kinds):
                raise B402IndeterminateError(
                    "B402 returned malformed supported kinds"
                )
            self._supported_kinds = copy.deepcopy(kinds)
            self._supported_expires_at = now + 3_600.0
            return self._supported_kinds


def _is_evm_address(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 42:
        return False
    if not value.startswith("0x"):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def _success_data(
    response: Mapping[str, Any],
    *,
    operation: str,
) -> dict[str, Any]:
    data = response.get("data")
    if response.get("code") != "000000" or not isinstance(data, dict):
        raise B402IndeterminateError(
            f"B402 returned an invalid {operation} response"
        )
    return data


def _reason(
    data: Mapping[str, Any],
    field: str,
    fallback: str,
) -> str:
    value = data.get(field)
    return value if isinstance(value, str) and value else fallback
