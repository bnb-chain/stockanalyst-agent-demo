"""API Gateway REST proxy Lambda entrypoint for the internal x402 bridge."""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
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
from envelope import GatewayRequestError, build_envelope
from oauth_client import OAuthClient, OAuthUnavailable, default_token_transport


logger = logging.getLogger(__name__)
_application: "GatewayApplication | None" = None


class GatewayConfigurationError(RuntimeError):
    pass


class GatewayApplication:
    """Stateless request coordinator; only its OAuth client may warm-cache safely."""
    def __init__(self, public_base_url: str, oauth: Any, agentcore: Any) -> None:
        self._public_base_url = public_base_url
        self._oauth = oauth
        self._agentcore = agentcore

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        request_id: str | None = None
        route: str | None = None
        try:
            envelope = build_envelope(event, public_base_url=self._public_base_url)
            request_id = envelope["requestId"]
            route = f"{envelope['method']} {envelope['path']}"
            authorization_header = self._oauth.authorization_header()
            response = self._agentcore.invoke(envelope, authorization_header=authorization_header)
            result = {
                "statusCode": response["status"],
                "headers": response["headers"],
                "isBase64Encoded": True,
                "body": response["bodyBase64"],
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
        logger.info(json.dumps({
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
        logger.info(json.dumps({
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
    public_base_url = environment.get("X402_GATEWAY_PUBLIC_BASE_URL", "")
    runtime_url = environment.get("X402_AGENTCORE_RUNTIME_URL", environment.get("AGENTCORE_RUNTIME_URL", ""))
    secret_arn = environment.get("X402_OAUTH_SECRET_ARN", "")
    if not public_base_url or not runtime_url or not secret_arn:
        raise GatewayConfigurationError("gateway_configuration_missing")
    oauth = OAuthClient(secret_reader_factory(secret_arn), default_token_transport)
    try:
        agentcore = AgentCoreClient(runtime_url, oauth.authorization_header, default_agentcore_transport)
    except ValueError as exc:
        raise GatewayConfigurationError("gateway_configuration_invalid") from exc
    return GatewayApplication(public_base_url, oauth, agentcore)


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
