"""Strict in-process bridge from the internal A2A x402 envelope to ASGI."""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit


class EnvelopeError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_ROUTES = {
    ("GET", "/x402/price"),
    ("GET", "/x402/free"),
    ("POST", "/x402/free"),
    ("POST", "/x402/analyze/async"),
}
_JOB_ROUTE = re.compile(r"/x402/jobs/x402_[0-9a-f]{32}(?:/resume)?\Z")
_FREE_SYMBOL = re.compile(r"[A-Za-z0-9.^_-]{1,32}\Z")
_HEADER_NAME = re.compile(r"[a-z0-9-]+\Z")
_REQUEST_ID = re.compile(r"x402gw_[0-9a-f]{64}\Z")
_REQUEST_HEADERS = {
    "accept",
    "content-type",
    "payment-signature",
    "wallet-signature",
    "x-job-token",
}
_RESPONSE_HEADERS = {
    "content-type",
    "location",
    "retry-after",
    "cache-control",
    "vary",
    "payment-required",
    "payment-response",
}
_MAX_REQUEST_BYTES = 256 * 1024
_MAX_WALLET_SIGNATURE_CHARACTERS = 4096
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_BASE_ENVELOPE_FIELDS = {
    "version",
    "requestId",
    "method",
    "path",
    "queryString",
    "headers",
    "bodyBase64",
    "publicBaseUrl",
}
_SOURCE_ENVELOPE_FIELDS = _BASE_ENVELOPE_FIELDS | {"sourceIp"}


@dataclass(frozen=True)
class _Request:
    request_id: str
    method: str
    path: str
    query_string: bytes
    headers: list[tuple[bytes, bytes]]
    body: bytes
    public_base_url: str
    source_ip: str | None


async def dispatch_x402_envelope(
    app,
    envelope: dict[str, Any],
    *,
    expected_public_base_url: str,
) -> dict[str, Any]:
    """Dispatch one validated, non-streaming x402 request without network I/O."""
    request = _validate_request(envelope, expected_public_base_url)
    sent: list[dict[str, Any]] = []
    pending = [{
        "type": "http.request",
        "body": request.body,
        "more_body": False,
    }]

    async def receive() -> dict[str, Any]:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "http_version": "1.1",
            "scheme": "https",
            "method": request.method,
            "path": request.path,
            "raw_path": request.path.encode(),
            "query_string": request.query_string,
            "headers": request.headers,
            "x402_public_base_url": request.public_base_url,
            "x402_source_ip": request.source_ip,
        },
        receive,
        send,
    )
    return _validate_and_encode_response(request.request_id, sent)


def _validate_request(
    envelope: dict[str, Any], expected_public_base_url: str
) -> _Request:
    if not isinstance(envelope, dict) or set(envelope) not in (
        _BASE_ENVELOPE_FIELDS,
        _SOURCE_ENVELOPE_FIELDS,
    ):
        raise EnvelopeError("invalid_envelope")
    if type(envelope["version"]) is not int or envelope["version"] != 1:
        raise EnvelopeError("invalid_envelope_version")

    request_id = envelope["requestId"]
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise EnvelopeError("invalid_request_id")

    expected_base_url = _validate_public_base_url(expected_public_base_url)
    public_base_url = _validate_public_base_url(envelope["publicBaseUrl"])
    if public_base_url != expected_base_url:
        raise EnvelopeError("public_base_mismatch")

    method = envelope["method"]
    path = envelope["path"]
    if not isinstance(method, str) or not isinstance(path, str):
        raise EnvelopeError("route_not_allowed")
    if not _is_allowed_route(method, path):
        raise EnvelopeError("route_not_allowed")
    query_string = _validate_query_string(envelope["queryString"], method, path)

    body = _decode_request_body(envelope["bodyBase64"])
    if method == "GET" and body:
        raise EnvelopeError("get_request_body_not_allowed")

    return _Request(
        request_id=request_id,
        method=method,
        path=path,
        query_string=query_string,
        headers=_validate_request_headers(envelope["headers"]),
        body=body,
        public_base_url=public_base_url,
        source_ip=_validate_source_ip(envelope.get("sourceIp")) if "sourceIp" in envelope else None,
    )


def _validate_source_ip(value: Any) -> str:
    if not isinstance(value, str):
        raise EnvelopeError("invalid_source_ip")
    try:
        return ip_address(value).compressed
    except ValueError:
        raise EnvelopeError("invalid_source_ip") from None


def _is_allowed_route(method: str, path: str) -> bool:
    if (method, path) in _ROUTES:
        return True
    if not _JOB_ROUTE.fullmatch(path):
        return False
    return (method == "GET" and not path.endswith("/resume")) or (
        method == "POST" and path.endswith("/resume")
    )


def _validate_query_string(value: Any, method: str, path: str) -> bytes:
    if not isinstance(value, str):
        raise EnvelopeError("query_not_allowed")
    if not value:
        return b""
    if method != "GET" or path != "/x402/free":
        raise EnvelopeError("query_not_allowed")
    try:
        pairs = parse_qsl(
            value,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError):
        raise EnvelopeError("query_not_allowed") from None
    if len(pairs) != 1 or pairs[0][0] != "symbol":
        raise EnvelopeError("query_not_allowed")
    symbol = pairs[0][1]
    if not _FREE_SYMBOL.fullmatch(symbol) or urlencode({"symbol": symbol}) != value:
        raise EnvelopeError("query_not_allowed")
    return value.encode("ascii")


def _validate_public_base_url(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise EnvelopeError("invalid_public_base_url")
    if any(ord(char) <= 0x20 for char in value):
        raise EnvelopeError("invalid_public_base_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise EnvelopeError("invalid_public_base_url") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise EnvelopeError("invalid_public_base_url")
    return value.rstrip("/")


def _validate_request_headers(headers: Any) -> list[tuple[bytes, bytes]]:
    if not isinstance(headers, dict):
        raise EnvelopeError("invalid_request_headers")
    result: list[tuple[bytes, bytes]] = []
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or not _HEADER_NAME.fullmatch(name)
            or name not in _REQUEST_HEADERS
            or not isinstance(value, str)
            or "\r" in value
            or "\n" in value
            or "\x00" in value
            or (
                name == "wallet-signature"
                and len(value) > _MAX_WALLET_SIGNATURE_CHARACTERS
            )
        ):
            raise EnvelopeError("request_header_not_allowed")
        try:
            result.append((name.encode("ascii"), value.encode("latin-1")))
        except UnicodeEncodeError as exc:
            raise EnvelopeError("request_header_not_allowed") from exc
    return result


def _decode_request_body(body_base64: Any) -> bytes:
    if not isinstance(body_base64, str):
        raise EnvelopeError("invalid_body_base64")
    try:
        body = base64.b64decode(body_base64, validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise EnvelopeError("invalid_body_base64") from exc
    if len(body) > _MAX_REQUEST_BYTES:
        raise EnvelopeError("request_too_large")
    return body


def _validate_and_encode_response(
    request_id: str, sent: list[dict[str, Any]]
) -> dict[str, Any]:
    starts = [message for message in sent if message.get("type") == "http.response.start"]
    if not starts:
        raise EnvelopeError("missing_response_start")
    if len(starts) != 1:
        raise EnvelopeError("duplicate_response_start")

    start_index = sent.index(starts[0])
    body_messages = [
        message
        for message in sent[start_index + 1 :]
        if message.get("type") == "http.response.body"
    ]
    if not body_messages:
        raise EnvelopeError("missing_response_body")
    if any(message.get("more_body", False) for message in body_messages):
        raise EnvelopeError("streaming_not_supported")

    start = starts[0]
    status = start.get("status")
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise EnvelopeError("invalid_response_status")

    body_chunks: list[bytes] = []
    for message in body_messages:
        body = message.get("body", b"")
        if not isinstance(body, bytes):
            raise EnvelopeError("invalid_response_body")
        body_chunks.append(body)
    body = b"".join(body_chunks)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise EnvelopeError("response_too_large")

    return {
        "requestId": request_id,
        "status": status,
        "headers": _response_headers(start.get("headers", [])),
        "bodyBase64": base64.b64encode(body).decode("ascii"),
    }


def _response_headers(headers: Any) -> dict[str, str]:
    if not isinstance(headers, list):
        raise EnvelopeError("invalid_response_headers")
    result: dict[str, str] = {}
    for header in headers:
        if not (
            isinstance(header, tuple)
            and len(header) == 2
            and isinstance(header[0], bytes)
            and isinstance(header[1], bytes)
        ):
            raise EnvelopeError("invalid_response_headers")
        try:
            name = header[0].decode("ascii")
            value = header[1].decode("latin-1")
        except UnicodeDecodeError as exc:
            raise EnvelopeError("invalid_response_headers") from exc
        if not _HEADER_NAME.fullmatch(name) or name != name.lower():
            raise EnvelopeError("invalid_response_headers")
        if name in _RESPONSE_HEADERS:
            result[name] = value
    return result
