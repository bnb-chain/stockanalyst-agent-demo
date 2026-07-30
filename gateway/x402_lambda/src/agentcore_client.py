"""Narrow JSON-RPC/A2A client for the internal x402 envelope skill."""
from __future__ import annotations

import base64
import json
import re
import secrets
import socket
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class AgentInvocationError(RuntimeError):
    pass


class AgentInvocationUnauthorized(AgentInvocationError):
    pass


class AgentInvocationRateLimited(AgentInvocationError):
    pass


class AgentInvocationTimeout(AgentInvocationError):
    pass


class AgentInvocationUnavailable(AgentInvocationError):
    pass


class InvalidAgentResponse(AgentInvocationError):
    pass


_RESPONSE_HEADERS = {
    "content-type", "location", "retry-after", "cache-control", "vary", "x-payment-required",
}
_HEADER_NAME = re.compile(r"[a-z0-9-]+\Z")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_AGENTCORE_RESPONSE_BYTES = 3 * 1024 * 1024


class AgentCoreClient:
    def __init__(
        self,
        runtime_url: str,
        authorization_header: Callable[[], str],
        transport: Callable[..., Mapping[str, Any]],
        *,
        timeout_seconds: float = 25.0,
    ) -> None:
        if not _https_url(runtime_url):
            raise ValueError("invalid_agentcore_runtime_url")
        self._runtime_url = runtime_url
        self._authorization_header = authorization_header
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    def invoke(
        self, envelope: Mapping[str, Any], *, authorization_header: str | None = None
    ) -> dict[str, Any]:
        request_id = envelope.get("requestId")
        if not isinstance(request_id, str):
            raise InvalidAgentResponse("invalid_request_id")
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "message/send",
            "params": {"message": {
                "kind": "message",
                "messageId": request_id,
                "role": "user",
                "parts": [{"kind": "data", "data": {"skill": "x402_http_envelope", "envelope": dict(envelope)}}],
            }},
        }, separators=(",", ":")).encode("utf-8")
        session_id = f"x402-gateway-session-{secrets.token_hex(16)}"
        try:
            response = self._transport(
                url=self._runtime_url,
                headers={
                    "Authorization": authorization_header or self._authorization_header(),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
                },
                body=body,
                timeout_seconds=self._timeout_seconds,
            )
        except (TimeoutError, socket.timeout) as exc:
            raise AgentInvocationTimeout("agent_invocation_timeout") from exc
        except AgentInvocationError:
            raise
        except Exception as exc:
            raise AgentInvocationUnavailable("agent_invocation_unavailable") from exc
        return self._extract_response(response, request_id)

    @staticmethod
    def _extract_response(response: Mapping[str, Any], request_id: str) -> dict[str, Any]:
        if not isinstance(response, Mapping):
            raise InvalidAgentResponse("invalid_agent_response")
        status = response.get("status")
        if type(status) is not int:
            raise InvalidAgentResponse("invalid_agent_response")
        if status in {401, 403}:
            raise AgentInvocationUnauthorized("agent_invocation_unauthorized")
        if status == 429:
            raise AgentInvocationRateLimited("agent_invocation_rate_limited")
        if 500 <= status <= 599:
            raise AgentInvocationUnavailable("agent_invocation_unavailable")
        if not 200 <= status <= 299:
            raise InvalidAgentResponse("invalid_agent_response")
        raw_body = response.get("body")
        if not isinstance(raw_body, bytes) or len(raw_body) > _MAX_AGENTCORE_RESPONSE_BYTES:
            raise InvalidAgentResponse("invalid_agent_response")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
            if (
                not isinstance(payload, Mapping) or payload.get("jsonrpc") != "2.0"
                or payload.get("id") != request_id
            ):
                raise ValueError("invalid_jsonrpc_binding")
            result = payload["result"]
            parts = result["parts"]
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise InvalidAgentResponse("invalid_agent_response") from exc
        if not isinstance(parts, list):
            raise InvalidAgentResponse("invalid_agent_response")
        data_parts = [part.get("data") for part in parts if isinstance(part, Mapping) and part.get("kind") == "data"]
        if len(data_parts) != 1 or not isinstance(data_parts[0], Mapping):
            raise InvalidAgentResponse("invalid_agent_response")
        return _validated_response_envelope(data_parts[0], request_id)


def _validated_response_envelope(value: Mapping[str, Any], request_id: str) -> dict[str, Any]:
    required = {"requestId", "status", "headers", "bodyBase64"}
    if set(value) != required or value.get("requestId") != request_id:
        raise InvalidAgentResponse("invalid_agent_response")
    status = value["status"]
    headers = value["headers"]
    body_base64 = value["bodyBase64"]
    if type(status) is not int or not 100 <= status <= 599 or not isinstance(headers, Mapping) or not isinstance(body_base64, str):
        raise InvalidAgentResponse("invalid_agent_response")
    clean_headers: dict[str, str] = {}
    for name, header_value in headers.items():
        if (
            not isinstance(name, str) or not isinstance(header_value, str)
            or name not in _RESPONSE_HEADERS or not _HEADER_NAME.fullmatch(name)
            or "\r" in header_value or "\n" in header_value or "\x00" in header_value
        ):
            raise InvalidAgentResponse("invalid_agent_response")
        clean_headers[name] = header_value
    try:
        decoded = base64.b64decode(body_base64, validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise InvalidAgentResponse("invalid_agent_response") from exc
    if len(decoded) > _MAX_RESPONSE_BYTES:
        raise InvalidAgentResponse("invalid_agent_response")
    return {"requestId": request_id, "status": status, "headers": clean_headers, "bodyBase64": body_base64}


def default_agentcore_transport(*, url: str, headers: Mapping[str, str], body: bytes, timeout_seconds: float) -> dict[str, Any]:
    request = Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310: constructor validates HTTPS URL
            return {
                "status": response.status,
                "headers": dict(response.headers.items()),
                "body": response.read(_MAX_AGENTCORE_RESPONSE_BYTES + 1),
            }
    except HTTPError as exc:
        # Deliberately discard remote body; it may contain internal details or credentials.
        return {"status": exc.code, "headers": dict(exc.headers.items()) if exc.headers else {}, "body": b""}
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise AgentInvocationTimeout("agent_invocation_timeout") from exc
        raise AgentInvocationUnavailable("agent_invocation_unavailable") from exc


def _https_url(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https" and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment
        )
    except ValueError:
        return False
