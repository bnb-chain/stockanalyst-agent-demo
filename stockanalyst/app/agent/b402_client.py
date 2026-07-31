"""Binance B402 V2 merchant client.

This module owns merchant authentication and the exact HTTP bytes covered by
the RSA signature. It never logs credentials, request signatures, or payment
payloads.
"""
from __future__ import annotations

import base64
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
