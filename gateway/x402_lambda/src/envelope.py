"""Build the strictly limited x402 AgentCore envelope from REST proxy events."""
from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


_ALLOWED_HEADERS = {"accept", "content-type", "x-payment", "x-job-token"}
_JOB_PATH = re.compile(r"/x402/jobs/x402_[0-9a-f]{32}(?:/resume)?\Z")
_HEADER_NAME = re.compile(r"[a-z0-9-]+\Z")
_MAX_BODY_BYTES = 256 * 1024


class GatewayRequestError(ValueError):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def build_envelope(event: Mapping[str, Any], *, public_base_url: str) -> dict[str, Any]:
    """Validate an API Gateway REST proxy event without retaining its contents."""
    method, path = _validated_route(event)
    body = _decode_body(event)
    if method == "GET" and body:
        raise GatewayRequestError("get_request_body_not_allowed")
    headers = _allowed_headers(event.get("headers") or {}, event.get("multiValueHeaders"))
    digest = hashlib.sha256()
    for part in (
        b"x402-gateway-v1\0", method.encode("ascii"), b"\0", path.encode("ascii"),
        b"\0", headers.get("x-payment", "").encode("utf-8"), b"\0",
        headers.get("x-job-token", "").encode("utf-8"), b"\0", body,
    ):
        digest.update(part)
    return {
        "version": 1,
        "requestId": f"x402gw_{digest.hexdigest()}",
        "method": method,
        "path": path,
        "publicBaseUrl": validate_public_base(public_base_url),
        "headers": headers,
        "bodyBase64": base64.b64encode(body).decode("ascii"),
    }


def _validated_route(event: Mapping[str, Any]) -> tuple[str, str]:
    method = event.get("httpMethod")
    raw_path = event.get("path")
    if not isinstance(method, str) or not isinstance(raw_path, str):
        raise GatewayRequestError("route_not_allowed")
    if _has_query(event):
        raise GatewayRequestError("query_not_allowed")
    path = _without_stage_prefix(raw_path, event.get("requestContext"))
    allowed = ((method, path) in {("GET", "/x402/price"), ("POST", "/x402/analyze/async")})
    if not allowed and _JOB_PATH.fullmatch(path):
        allowed = (method == "GET" and not path.endswith("/resume")) or (
            method == "POST" and path.endswith("/resume")
        )
    if not allowed:
        raise GatewayRequestError("route_not_allowed")
    return method, path


def _has_query(event: Mapping[str, Any]) -> bool:
    for key in ("queryStringParameters", "multiValueQueryStringParameters"):
        value = event.get(key)
        if value not in (None, {}):
            if not isinstance(value, Mapping) or value:
                return True
    return False


def _without_stage_prefix(path: str, request_context: Any) -> str:
    stage = request_context.get("stage") if isinstance(request_context, Mapping) else None
    if isinstance(stage, str) and stage:
        prefix = "/" + stage
        if path == prefix:
            return "/"
        if path.startswith(prefix + "/"):
            return path[len(prefix):]
    return path


def _decode_body(event: Mapping[str, Any]) -> bytes:
    raw_body = event.get("body")
    encoded = event.get("isBase64Encoded", False)
    if type(encoded) is not bool:
        raise GatewayRequestError("invalid_body_base64")
    if raw_body is None:
        body = b""
    elif not isinstance(raw_body, str):
        raise GatewayRequestError("invalid_request_body")
    elif encoded:
        try:
            body = base64.b64decode(raw_body, validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise GatewayRequestError("invalid_body_base64") from exc
    else:
        try:
            body = raw_body.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise GatewayRequestError("invalid_request_body") from exc
    if len(body) > _MAX_BODY_BYTES:
        raise GatewayRequestError("request_too_large", 413)
    return body


def _allowed_headers(raw_headers: Any, multi_value_headers: Any) -> dict[str, str]:
    if not isinstance(raw_headers, Mapping):
        raise GatewayRequestError("invalid_request_headers")
    headers: dict[str, str] = {}
    for raw_name, value in raw_headers.items():
        _record_header(headers, raw_name, value)
    if multi_value_headers is None:
        return headers
    if not isinstance(multi_value_headers, Mapping):
        raise GatewayRequestError("invalid_request_headers")
    for raw_name, values in multi_value_headers.items():
        name = _header_name(raw_name)
        if name not in _ALLOWED_HEADERS:
            continue
        if not isinstance(values, list) or len(values) != 1:
            raise GatewayRequestError("request_header_not_allowed")
        _record_header(headers, raw_name, values[0])
    return headers


def _record_header(headers: dict[str, str], raw_name: Any, value: Any) -> None:
    name = _header_name(raw_name)
    if name not in _ALLOWED_HEADERS:
        return
    if not isinstance(value, str) or "\r" in value or "\n" in value or "\x00" in value:
        raise GatewayRequestError("request_header_not_allowed")
    try:
        value.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise GatewayRequestError("request_header_not_allowed") from exc
    if name in headers and headers[name] != value:
        raise GatewayRequestError("request_header_not_allowed")
    headers[name] = value


def _header_name(raw_name: Any) -> str:
    if not isinstance(raw_name, str):
        raise GatewayRequestError("request_header_not_allowed")
    name = raw_name.lower()
    if not _HEADER_NAME.fullmatch(name):
        raise GatewayRequestError("request_header_not_allowed")
    return name


def validate_public_base(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) <= 0x20 for char in value):
        raise GatewayRequestError("invalid_public_base_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise GatewayRequestError("invalid_public_base_url") from exc
    if (
        parsed.scheme != "https" or not parsed.netloc or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or (port is not None and not 1 <= port <= 65535)
    ):
        raise GatewayRequestError("invalid_public_base_url")
    return value.rstrip("/")
