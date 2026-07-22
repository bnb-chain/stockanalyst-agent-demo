"""SSRF-safe storage provider for a buyer's UOMP payload relay.

The provider accepts a validated gateway origin, uploads deliverables to its
authenticated endpoint, and only reads payload URLs on that same origin.
"""
from __future__ import annotations

import http.client
from ipaddress import ip_address
import json
import re
import ssl
import socket
import threading
from typing import Any, Callable
import urllib.request
from urllib.parse import urlsplit

from bnbagent.storage import StorageProvider
from notify_security import _ValidatedGatewayOrigin, _validate_gateway_origin

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


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connect to a prevalidated address while retaining the HTTP hostname."""

    def __init__(
        self,
        host: str,
        *,
        pinned_addresses: tuple[str, ...],
        pinned_port: int,
        **kwargs,
    ) -> None:
        super().__init__(host, **kwargs)
        self._pinned_addresses = pinned_addresses
        self._pinned_port = pinned_port

    def connect(self) -> None:
        self.sock = _open_pinned_socket(
            self._pinned_addresses,
            self._pinned_port,
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Pin the TCP peer while preserving the hostname for certificate SNI."""

    def __init__(
        self,
        host: str,
        *,
        pinned_addresses: tuple[str, ...],
        pinned_port: int,
        **kwargs,
    ) -> None:
        super().__init__(host, **kwargs)
        self._pinned_addresses = pinned_addresses
        self._pinned_port = pinned_port

    def connect(self) -> None:
        self.sock = _open_pinned_socket(
            self._pinned_addresses,
            self._pinned_port,
            self.timeout,
            self.source_address,
            wrap_socket=lambda connection: self._context.wrap_socket(
                connection,
                server_hostname=self.host,
            ),
        )


def _open_pinned_socket(
    addresses: tuple[str, ...],
    port: int,
    timeout: object,
    source_address: tuple[str, int] | None,
    wrap_socket: Callable[[Any], Any] | None = None,
):
    first_error: Exception | None = None
    for address in addresses:
        connection = None
        try:
            family = socket.AF_INET6 if ip_address(address).version == 6 else socket.AF_INET
            connection = socket.socket(family, socket.SOCK_STREAM)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect((address, port))
            return wrap_socket(connection) if wrap_socket is not None else connection
        except Exception as error:
            if connection is not None:
                connection.close()
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error
    raise OSError("gateway resolved without an address")


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, validated: _ValidatedGatewayOrigin, connection_factory=None) -> None:
        super().__init__()
        self._validated = validated
        self._connection_factory = connection_factory

    def http_open(self, request):
        return self.do_open(self._connection, request)

    def _connection(self, host: str, *, timeout: float, **kwargs):
        del kwargs
        if self._connection_factory is not None:
            return self._connection_factory(
                scheme="http",
                host=host,
                address=self._validated.addresses[0],
                addresses=self._validated.addresses,
                port=self._validated.port,
                context=None,
                timeout=timeout,
            )
        return _PinnedHTTPConnection(
            host,
            pinned_addresses=self._validated.addresses,
            pinned_port=self._validated.port,
            timeout=timeout,
        )


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(
        self,
        validated: _ValidatedGatewayOrigin,
        context: ssl.SSLContext,
        connection_factory=None,
    ) -> None:
        super().__init__(context=context)
        self._validated = validated
        self._connection_factory = connection_factory

    def https_open(self, request):
        return self.do_open(self._connection, request)

    def _connection(self, host: str, *, timeout: float, **kwargs):
        del kwargs
        if self._connection_factory is not None:
            return self._connection_factory(
                scheme="https",
                host=host,
                address=self._validated.addresses[0],
                addresses=self._validated.addresses,
                port=self._validated.port,
                context=self._context,
                timeout=timeout,
            )
        return _PinnedHTTPSConnection(
            host,
            pinned_addresses=self._validated.addresses,
            pinned_port=self._validated.port,
            context=self._context,
            timeout=timeout,
        )


def _verified_no_redirect_opener(
    validated: _ValidatedGatewayOrigin,
    *,
    connection_factory=None,
):
    if validated.scheme == "https":
        transport_handler = _PinnedHTTPSHandler(
            validated,
            ssl.create_default_context(),
            connection_factory,
        )
    else:
        transport_handler = _PinnedHTTPHandler(validated, connection_factory)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        transport_handler,
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
        connection_factory: Any | None = None,
    ) -> None:
        validated = _validate_gateway_origin(gateway_url, resolver=resolver)
        self._base = validated.origin
        self._origin = urlsplit(self._base)
        self._token = token
        self._opener = (
            opener
            if opener is not None
            else _verified_no_redirect_opener(
                validated,
                connection_factory=connection_factory,
            )
        )

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
        scheme = parsed.scheme.lower()
        expected_port = self._origin.port or (443 if self._origin.scheme == "https" else 80)
        actual_port = port or (443 if scheme == "https" else 80)
        if (
            scheme != self._origin.scheme
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
        return f"{self._base}{parsed.path}"


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
