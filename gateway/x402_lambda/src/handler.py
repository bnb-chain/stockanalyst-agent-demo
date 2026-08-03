"""API Gateway REST proxy Lambda entrypoint for the internal x402 bridge."""
from __future__ import annotations

import base64
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from agentcore_client import (
    AgentCoreClient,
    AgentInvocationRateLimited,
    AgentInvocationTimeout,
    AgentInvocationUnauthorized,
    AgentInvocationUnavailable,
    InvalidAgentResponse,
    default_agentcore_transport,
)
from envelope import GatewayRequestError, build_envelope, validate_public_base
from oauth_client import OAuthClient, OAuthUnavailable, default_token_transport

_application: "GatewayApplication | None" = None
_EXECUTE_API_DOMAIN = re.compile(
    r"[a-z0-9]{10}\.execute-api\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?\Z"
)
_STAGE_NAME = re.compile(r"[a-z0-9-]+\Z")


class GatewayConfigurationError(RuntimeError):
    pass


class GatewayApplication:
    """Stateless request coordinator; only its OAuth client may warm-cache safely."""
    def __init__(self, oauth: Any, agentcore: Any) -> None:
        self._oauth = oauth
        self._agentcore = agentcore

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        request_id: str | None = None
        route: str | None = None
        try:
            envelope = build_envelope(event, public_base_url=_trusted_public_base(event))
            request_id = envelope["requestId"]
            route = f"{envelope['method']} {envelope['path']}"
            authorization_header = self._oauth.authorization_header()
            response = self._agentcore.invoke(envelope, authorization_header=authorization_header)
            try:
                body = base64.b64decode(response["bodyBase64"], validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise InvalidAgentResponse("invalid_utf8_response_body") from exc
            result = {
                "statusCode": response["status"],
                "headers": response["headers"],
                "isBase64Encoded": False,
                "body": body,
            }
            self._log(request_id, route, result["statusCode"], "success", started)
            return result
        except GatewayRequestError as exc:
            result = _safe_error(exc.status, exc.code)
            self._log(request_id, route, result["statusCode"], exc.code, started)
            return result
        except AgentInvocationTimeout:
            result = _safe_error(503, "settlement_status_unknown", retryable=True)
            self._log(request_id, route, result["statusCode"], "timeout", started)
            return result
        except (OAuthUnavailable, AgentInvocationUnauthorized, AgentInvocationRateLimited, AgentInvocationUnavailable, GatewayConfigurationError):
            result = _safe_error(503, "service_unavailable", retryable=True)
            self._log(request_id, route, result["statusCode"], "unavailable", started)
            return result
        except InvalidAgentResponse:
            result = _safe_error(502, "invalid_upstream_response")
            self._log(request_id, route, result["statusCode"], "invalid_response", started)
            return result
        except Exception:
            result = _safe_error(500, "internal_error")
            self._log(request_id, route, result["statusCode"], "internal_error", started)
            return result

    @staticmethod
    def _log(request_id: str | None, route: str | None, status: int, outcome: str, started: float) -> None:
        print(json.dumps({
            "requestId": request_id,
            "route": route,
            "status": status,
            "outcome": outcome,
            "durationMilliseconds": int((time.monotonic() - started) * 1000),
        }, separators=(",", ":")))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda-compatible entrypoint. Context is intentionally not retained."""
    started = time.monotonic()
    try:
        return _get_application().handle(event)
    except GatewayConfigurationError:
        result = _safe_error(503, "service_unavailable", retryable=True)
        print(json.dumps({
            "requestId": None,
            "route": None,
            "status": result["statusCode"],
            "outcome": "unavailable",
            "durationMilliseconds": int((time.monotonic() - started) * 1000),
        }, separators=(",", ":")))
        return result


def _get_application() -> GatewayApplication:
    global _application
    if _application is None:
        _application = _application_from_environment(os.environ, _secrets_manager_reader)
    return _application


def _application_from_environment(
    environment: dict[str, str], secret_reader_factory: Callable[[str], Callable[[], str]],
) -> GatewayApplication:
    runtime_url = environment.get("AGENTCORE_INVOKE_URL", "")
    secret_arn = environment.get("OAUTH_SECRET_ARN", "")
    if not runtime_url or not secret_arn:
        raise GatewayConfigurationError("gateway_configuration_missing")
    try:
        oauth = OAuthClient(secret_reader_factory(secret_arn), default_token_transport)
        agentcore = AgentCoreClient(runtime_url, oauth.authorization_header, default_agentcore_transport)
    except ValueError as exc:
        raise GatewayConfigurationError("gateway_configuration_invalid") from exc
    return GatewayApplication(oauth, agentcore)


def _trusted_public_base(event: Mapping[str, Any]) -> str:
    """Build the public origin from API Gateway's REST context, never caller headers."""
    request_context = event.get("requestContext")
    if not isinstance(request_context, Mapping):
        raise GatewayConfigurationError("gateway_request_context_missing")
    domain_name = request_context.get("domainName")
    stage = request_context.get("stage")
    if (
        not isinstance(domain_name, str)
        or not isinstance(stage, str)
        or not _EXECUTE_API_DOMAIN.fullmatch(domain_name)
        or not _STAGE_NAME.fullmatch(stage)
    ):
        raise GatewayConfigurationError("gateway_request_context_invalid")
    try:
        return validate_public_base(f"https://{domain_name}/{stage}")
    except GatewayRequestError as exc:
        raise GatewayConfigurationError("gateway_request_context_invalid") from exc


def _secrets_manager_reader(secret_arn: str) -> Callable[[], str]:
    def read_secret() -> str:
        try:
            import boto3  # Lambda-provided dependency; delayed so local tests need no AWS SDK.

            result = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
            secret = result.get("SecretString")
            if not isinstance(secret, str):
                raise ValueError("secret_string_missing")
            return secret
        except Exception as exc:
            raise OAuthUnavailable("oauth_secret_unavailable") from exc
    return read_secret


def _safe_error(status: int, error_code: str, *, retryable: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {"errorCode": error_code}
    if retryable:
        body["retryable"] = True
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "isBase64Encoded": False,
        "body": json.dumps(body, separators=(",", ":")),
    }
