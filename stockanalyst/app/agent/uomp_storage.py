"""SSRF-safe storage provider for a buyer's UOMP payload relay.

The provider accepts a validated gateway origin, uploads deliverables to its
authenticated endpoint, and only reads payload URLs on that same origin.
"""
from __future__ import annotations

import json
import re
import ssl
import threading
from typing import Any, Callable
import urllib.request
from urllib.parse import urlsplit

from bnbagent.storage import StorageProvider
from notify_security import validate_gateway_url

_TIMEOUT_UPLOAD = 30
_TIMEOUT_DOWNLOAD = 30
_MAX_RESPONSE_BYTES = 65_536
_PAYLOAD_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_PAYLOAD_PATH_PATTERN = re.compile(r"/v1/payload/([A-Za-z0-9_-]{1,128})\Z")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into the opener's normal HTTP error path."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def _verified_no_redirect_opener():
    context = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        _NoRedirectHandler(),
    )


class UOMPGatewayStorageProvider(StorageProvider):
    """StorageProvider backed by one validated buyer UOMP relay origin."""

    uses_file_url = False

    def __init__(
        self,
        gateway_url: str,
        token: str,
        *,
        resolver: Callable[..., object] | None = None,
        opener: Any | None = None,
    ) -> None:
        self._base = validate_gateway_url(gateway_url, resolver=resolver)
        self._origin = urlsplit(self._base)
        self._token = token
        self._opener = opener if opener is not None else _verified_no_redirect_opener()

    async def upload(self, data: dict, filename: str | None = None) -> str:
        del filename
        body = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base}/v1/payload/upload",
            data=body,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with self._opener.open(request, timeout=_TIMEOUT_UPLOAD) as response:
            result = _read_bounded_json(response)
        if not isinstance(result, dict):
            raise ValueError("invalid gateway response")
        payload_id = result.get("payload_id")
        if not isinstance(payload_id, str) or _PAYLOAD_ID_PATTERN.fullmatch(payload_id) is None:
            raise ValueError("invalid gateway response")
        return f"{self._base}/v1/payload/{payload_id}"

    async def download(self, url: str) -> dict:
        target = self._validate_payload_url(url)
        request = urllib.request.Request(target, method="GET")
        with self._opener.open(request, timeout=_TIMEOUT_DOWNLOAD) as response:
            result = _read_bounded_json(response)
        if not isinstance(result, dict):
            raise ValueError("invalid gateway response")
        return result

    async def exists(self, url: str) -> bool:
        target = self._validate_payload_url(url)
        try:
            request = urllib.request.Request(target, method="HEAD")
            with self._opener.open(request, timeout=10):
                return True
        except Exception:
            return False

    def _validate_payload_url(self, url: str) -> str:
        if (
            not isinstance(url, str)
            or not url
            or "?" in url
            or "#" in url
            or "\\" in url
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
        ):
            raise ValueError("invalid payload URL")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (TypeError, ValueError, UnicodeError):
            raise ValueError("invalid payload URL") from None
        expected_port = self._origin.port or (443 if self._origin.scheme == "https" else 80)
        actual_port = port or (443 if parsed.scheme == "https" else 80)
        if (
            parsed.scheme.lower() != self._origin.scheme
            or parsed.hostname is None
            or parsed.hostname.lower() != self._origin.hostname
            or actual_port != expected_port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or _PAYLOAD_PATH_PATTERN.fullmatch(parsed.path) is None
        ):
            raise ValueError("invalid payload URL")
        return url


def _read_bounded_json(response: Any) -> object:
    body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise ValueError("gateway response too large")
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        raise ValueError("invalid gateway response") from None


# Thread-level lock so concurrent submit_result calls (rare but possible when
# two jobs run at the same time) never overlap their storage_provider_from_config
# monkey-patch.
submit_lock = threading.Lock()
