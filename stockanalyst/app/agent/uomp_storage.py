"""SSRF-safe storage provider for a buyer's UOMP payload relay.

The provider accepts a validated gateway origin, uploads deliverables to its
authenticated endpoint, and only reads payload URLs on that same origin.
"""
from __future__ import annotations

import http.client
import io
import json
import re
import socket
import ssl
import threading
import time
import urllib.request
from collections.abc import Callable
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

from bnbagent.storage import StorageProvider

from notify_security import _validate_gateway_origin, _ValidatedGatewayOrigin

_TIMEOUT_UPLOAD = 30
_TIMEOUT_DOWNLOAD = 30
_TIMEOUT_EXISTS = 10
_MAX_UPLOAD_RESPONSE_BYTES = 65_536
_MAX_DOWNLOAD_RESPONSE_BYTES = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 8_192
_PAYLOAD_ID_PATTERN = re.compile(r"pay_[0-9a-f]{32}\Z")
_PAYLOAD_PATH_PATTERN = re.compile(r"/v1/payload/(pay_[0-9a-f]{32})\Z")
_monotonic = time.monotonic
_request_deadline = threading.local()
_SOCKET_TYPE = socket.socket


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into the opener's normal HTTP error path."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl


class _DeadlineSocketIO(io.RawIOBase):
    """Raw stream whose socket timeout shrinks with one absolute deadline."""

    def __init__(self, deadline_socket: _DeadlineSocket) -> None:
        super().__init__()
        self._deadline_socket = deadline_socket
        self._released = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        return self._deadline_socket.recv_into(buffer)

    def fileno(self) -> int:
        return self._deadline_socket.fileno()

    def close(self) -> None:
        if not self.closed:
            try:
                if not self._released:
                    self._released = True
                    self._deadline_socket._release_makefile()
            finally:
                super().close()


class _DeadlineSocket:
    """Socket facade enforcing a monotonic deadline across all response reads."""

    def __init__(
        self,
        raw_socket: Any,
        *,
        deadline: float,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._raw_socket = raw_socket
        self._deadline = deadline
        self._monotonic = _monotonic if monotonic is None else monotonic
        self._closed = False
        self._raw_closed = False
        self._makefile_refs = 0

    def _close_raw(self) -> None:
        if not self._raw_closed:
            self._raw_closed = True
            self._raw_socket.close()

    def _abort(self) -> None:
        """Force-close on a deadline breach, including active response streams."""
        self._closed = True
        self._close_raw()

    def _acquire_makefile(self) -> None:
        if self._closed:
            raise OSError("socket is closed")
        self._makefile_refs += 1

    def _release_makefile(self) -> None:
        if self._makefile_refs > 0:
            self._makefile_refs -= 1
        if self._closed and self._makefile_refs == 0:
            self._close_raw()

    def _prepare_operation(self) -> None:
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            self._abort()
            raise TimeoutError("gateway response deadline exceeded")
        self._raw_socket.settimeout(remaining)

    def recv(self, *args):
        self._prepare_operation()
        return self._raw_socket.recv(*args)

    def recv_into(self, *args):
        self._prepare_operation()
        return self._raw_socket.recv_into(*args)

    def send(self, *args):
        self._prepare_operation()
        return self._raw_socket.send(*args)

    def sendall(self, *args):
        self._prepare_operation()
        return self._raw_socket.sendall(*args)

    def makefile(
        self,
        mode: str = "r",
        buffering: int | None = None,
        *,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ):
        if mode not in {"r", "rb"}:
            return self._raw_socket.makefile(
                mode,
                buffering,
                encoding=encoding,
                errors=errors,
                newline=newline,
            )
        self._acquire_makefile()
        raw = _DeadlineSocketIO(self)
        if buffering == 0:
            stream = raw
        else:
            stream = io.BufferedReader(
                raw,
                io.DEFAULT_BUFFER_SIZE if buffering is None or buffering < 0 else buffering,
            )
        if "b" in mode:
            return stream
        return io.TextIOWrapper(
            stream,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._makefile_refs == 0:
                self._close_raw()

    def __getattr__(self, name: str):
        return getattr(self._raw_socket, name)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connect to a prevalidated address while retaining the HTTP hostname."""

    def __init__(
        self,
        host: str,
        *,
        pinned_addresses: tuple[str, ...],
        pinned_port: int,
        response_deadline: float | None = None,
        **kwargs,
    ) -> None:
        super().__init__(host, **kwargs)
        self._pinned_addresses = pinned_addresses
        self._pinned_port = pinned_port
        self._response_deadline = response_deadline

    def connect(self) -> None:
        self.sock = _open_pinned_socket(
            self._pinned_addresses,
            self._pinned_port,
            self.timeout,
            self.source_address,
            deadline=self._response_deadline,
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
        response_deadline: float | None = None,
        **kwargs,
    ) -> None:
        super().__init__(host, **kwargs)
        self._pinned_addresses = pinned_addresses
        self._pinned_port = pinned_port
        self._response_deadline = response_deadline

    def connect(self) -> None:
        self.sock = _open_pinned_socket(
            self._pinned_addresses,
            self._pinned_port,
            self.timeout,
            self.source_address,
            deadline=self._response_deadline,
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
    *,
    deadline: float | None = None,
    wrap_socket: Callable[[Any], Any] | None = None,
):
    def remaining_timeout() -> float | None:
        if deadline is not None:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise TimeoutError("gateway response deadline exceeded")
            return remaining
        if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
            return None
        return float(timeout)

    first_error: Exception | None = None
    if deadline is None and timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
        deadline = _monotonic() + float(timeout)
    for address in addresses:
        connection = None
        connected = None
        try:
            family = socket.AF_INET6 if ip_address(address).version == 6 else socket.AF_INET
            connection = socket.socket(family, socket.SOCK_STREAM)
            operation_timeout = remaining_timeout()
            if operation_timeout is not None:
                connection.settimeout(operation_timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect((address, port))
            if wrap_socket is not None:
                operation_timeout = remaining_timeout()
                if operation_timeout is not None:
                    connection.settimeout(operation_timeout)
                connected = wrap_socket(connection)
            else:
                connected = connection
            operation_timeout = remaining_timeout()
            if operation_timeout is not None and isinstance(connected, _SOCKET_TYPE):
                connected.settimeout(operation_timeout)
            if deadline is not None and isinstance(connected, _SOCKET_TYPE):
                return _DeadlineSocket(connected, deadline=deadline)
            return connected
        except Exception as error:
            if connected is not None and connected is not connection:
                close_connected = getattr(connected, "close", None)
                if callable(close_connected):
                    close_connected()
                else:
                    connection.close()
            elif connection is not None:
                connection.close()
            if isinstance(error, TimeoutError):
                raise
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
        deadline = getattr(_request_deadline, "value", _monotonic() + timeout)
        if self._connection_factory is not None:
            return self._connection_factory(
                scheme="http",
                host=host,
                address=self._validated.addresses[0],
                addresses=self._validated.addresses,
                port=self._validated.port,
                context=None,
                timeout=timeout,
                deadline=deadline,
            )
        return _PinnedHTTPConnection(
            host,
            pinned_addresses=self._validated.addresses,
            pinned_port=self._validated.port,
            timeout=timeout,
            response_deadline=deadline,
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
        deadline = getattr(_request_deadline, "value", _monotonic() + timeout)
        if self._connection_factory is not None:
            return self._connection_factory(
                scheme="https",
                host=host,
                address=self._validated.addresses[0],
                addresses=self._validated.addresses,
                port=self._validated.port,
                context=self._context,
                timeout=timeout,
                deadline=deadline,
            )
        return _PinnedHTTPSConnection(
            host,
            pinned_addresses=self._validated.addresses,
            pinned_port=self._validated.port,
            context=self._context,
            timeout=timeout,
            response_deadline=deadline,
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
        response, deadline = self._open_response(request, timeout=_TIMEOUT_UPLOAD)
        with response:
            result = _read_bounded_json(
                response,
                deadline=deadline,
                max_bytes=_MAX_UPLOAD_RESPONSE_BYTES,
            )
        if not isinstance(result, dict):
            raise ValueError("invalid gateway response")
        payload_id = result.get("payload_id")
        if not isinstance(payload_id, str) or _PAYLOAD_ID_PATTERN.fullmatch(payload_id) is None:
            raise ValueError("invalid gateway response")
        return f"{self._base}/v1/payload/{payload_id}"

    async def download(self, url: str) -> dict:
        target = self._validate_payload_url(url)
        request = urllib.request.Request(
            target,
            headers={"Authorization": f"Bearer {self._token}"},
            method="GET",
        )
        response, deadline = self._open_response(request, timeout=_TIMEOUT_DOWNLOAD)
        with response:
            result = _read_bounded_json(
                response,
                deadline=deadline,
                max_bytes=_MAX_DOWNLOAD_RESPONSE_BYTES,
            )
        if not isinstance(result, dict):
            raise ValueError("invalid gateway response")
        return result

    async def exists(self, url: str) -> bool:
        target = self._validate_payload_url(url)
        try:
            request = urllib.request.Request(
                target,
                headers={"Authorization": f"Bearer {self._token}"},
                method="HEAD",
            )
            response, deadline = self._open_response(request, timeout=_TIMEOUT_EXISTS)
            with response:
                _ensure_before_deadline(response, deadline)
                return True
        except Exception:
            return False

    def _open_response(self, request: urllib.request.Request, *, timeout: int):
        deadline = _monotonic() + timeout
        previous = getattr(_request_deadline, "value", None)
        _request_deadline.value = deadline
        try:
            response = self._opener.open(request, timeout=timeout)
        finally:
            if previous is None:
                try:
                    del _request_deadline.value
                except AttributeError:
                    pass
            else:
                _request_deadline.value = previous
        _ensure_before_deadline(response, deadline)
        return response, deadline

    def _validate_payload_url(self, url: str) -> str:
        if (
            not isinstance(url, str)
            or not url
            or not url.isascii()
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
        expected_port = (
            self._origin.port
            if self._origin.port is not None
            else (443 if self._origin.scheme == "https" else 80)
        )
        actual_port = port if port is not None else (443 if scheme == "https" else 80)
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


def _read_bounded_json(
    response: Any,
    *,
    deadline: float,
    max_bytes: int,
) -> object:
    read1 = getattr(response, "read1", None)
    if callable(read1):
        chunks: list[bytes] = []
        size = 0
        while True:
            _ensure_before_deadline(response, deadline)
            chunk = read1(min(_READ_CHUNK_BYTES, max_bytes + 1 - size))
            _ensure_before_deadline(response, deadline)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                _close_response(response)
                raise ValueError("invalid gateway response")
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                break
        body = b"".join(chunks)
    else:
        _ensure_before_deadline(response, deadline)
        body = response.read(max_bytes + 1)
        _ensure_before_deadline(response, deadline)
    if len(body) > max_bytes:
        raise ValueError("gateway response too large")
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        raise ValueError("invalid gateway response") from None


def _ensure_before_deadline(response: Any, deadline: float) -> None:
    if _monotonic() >= deadline:
        _close_response(response)
        raise TimeoutError("gateway response deadline exceeded")


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


# Thread-level lock so concurrent submit_result calls (rare but possible when
# two jobs run at the same time) never overlap their storage_provider_from_config
# monkey-patch.
submit_lock = threading.Lock()
