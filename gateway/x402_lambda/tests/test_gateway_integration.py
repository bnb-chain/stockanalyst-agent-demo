"""In-process proof of the public Lambda to Agent x402 boundary."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import unittest
from typing import Any
from unittest.mock import patch

import handler
from agentcore_client import AgentCoreClient

# The address is public deployment configuration, never a credential.  Set it
# before importing the agent so this test is independent of its working directory.
os.environ.setdefault("X402_SELLER_WALLET", "0xd10BdDC20E4DC42A1a19a9653e994991e25b8153")

from stockanalyst.app.agent.x402_envelope import dispatch_x402_envelope
from stockanalyst.app.agent.x402_handler import X402Handler
from stockanalyst.app.agent.x402_job_service import CreateJobResult


API_DOMAIN = "a1b2c3d4e5.execute-api.us-east-1.amazonaws.com"
STAGE = "testnet"
PUBLIC_BASE = f"https://{API_DOMAIN}/{STAGE}"
JOB_ID = "x402_" + "a" * 32
CONTEXT = object()


def api_event(
    *,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create the relevant complete shape of an API Gateway REST proxy event."""
    return {
        "resource": path,
        "path": path,
        "httpMethod": method,
        "headers": headers or {"Accept": "application/json"},
        "multiValueHeaders": {},
        "queryStringParameters": None,
        "multiValueQueryStringParameters": None,
        "pathParameters": None,
        "stageVariables": None,
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "a1b2c3d4e5",
            "domainName": API_DOMAIN,
            "httpMethod": method,
            "identity": {"sourceIp": "203.0.113.10", "userAgent": "integration-test"},
            "path": f"/{STAGE}{path}",
            "protocol": "HTTP/1.1",
            "requestId": "gateway-request-id",
            "resourcePath": path,
            "stage": STAGE,
        },
        "body": body.decode("utf-8") if body is not None else None,
        "isBase64Encoded": False,
    }


class FakeOAuth:
    def authorization_header(self) -> str:
        return "Bearer fake-access-token"


class FakeJobService:
    def __init__(self) -> None:
        self.create_calls: list[tuple[str, dict[str, Any]]] = []

    async def create_job(self, payment_proof: str, request: dict[str, Any]) -> CreateJobResult:
        self.create_calls.append((payment_proof, request))
        return CreateJobResult(
            job_id=JOB_ID,
            job_token="fake-job-token",
            status="queued",
            expires_at=1_785_945_600_123,
        )


class FakeAgentCoreTransport:
    """Runs the agent's real envelope skill while preserving A2A wire shapes."""
    def __init__(self, job_service: FakeJobService) -> None:
        self._app = X402Handler(_unreached_inner_app, job_service=job_service)
        self.requests: list[dict[str, Any]] = []

    def __call__(
        self, *, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float
    ) -> dict[str, Any]:
        self.assert_internal_request(url, headers, timeout_seconds)
        request = json.loads(body)
        self.requests.append(request)
        envelope = self._extract_envelope(request)
        response_envelope = asyncio.run(
            dispatch_x402_envelope(
                self._app,
                envelope,
                expected_public_base_url=PUBLIC_BASE,
            )
        )
        return {
            "status": 200,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({
                "jsonrpc": "2.0",
                "id": envelope["requestId"],
                "result": {
                    "kind": "message",
                    "messageId": f"agent-{envelope['requestId']}",
                    "contextId": f"context-{envelope['requestId']}",
                    "taskId": f"task-{envelope['requestId']}",
                    "role": "agent",
                    "parts": [{"kind": "data", "data": response_envelope}],
                },
            }, separators=(",", ":")).encode("utf-8"),
        }

    @staticmethod
    def assert_internal_request(url: str, headers: dict[str, str], timeout_seconds: float) -> None:
        if url != "https://agentcore.example.test/runtime":
            raise AssertionError("unexpected AgentCore URL")
        if headers.get("Authorization") != "Bearer fake-access-token":
            raise AssertionError("missing fake OAuth authorization")
        if headers.get("Content-Type") != "application/json":
            raise AssertionError("unexpected AgentCore content type")
        if headers.get("Accept") != "application/json":
            raise AssertionError("unexpected AgentCore accept header")
        session_id = headers.get("X-Amzn-Bedrock-AgentCore-Runtime-Session-Id", "")
        if re.fullmatch(r"x402-gateway-session-[0-9a-f]{32}", session_id) is None:
            raise AssertionError("invalid AgentCore session ID")
        if timeout_seconds != 25.0:
            raise AssertionError("unexpected AgentCore timeout")

    @staticmethod
    def _extract_envelope(request: dict[str, Any]) -> dict[str, Any]:
        if request.get("jsonrpc") != "2.0" or request.get("method") != "message/send":
            raise AssertionError("invalid A2A JSON-RPC request")
        message = request.get("params", {}).get("message", {})
        parts = message.get("parts")
        if (
            request.get("id") != message.get("messageId")
            or message.get("kind") != "message"
            or message.get("role") != "user"
            or not isinstance(parts, list)
            or len(parts) != 1
            or not isinstance(parts[0], dict)
            or parts[0].get("kind") != "data"
        ):
            raise AssertionError("invalid A2A message shape")
        data = parts[0].get("data") if isinstance(parts[0], dict) else None
        if not isinstance(data, dict) or data.get("skill") != "x402_http_envelope":
            raise AssertionError("invalid A2A data part")
        envelope = data.get("envelope")
        if not isinstance(envelope, dict) or request["id"] != envelope.get("requestId"):
            raise AssertionError("A2A request ID is not correlated to the envelope")
        return envelope


async def _unreached_inner_app(scope: Any, receive: Any, send: Any) -> None:
    raise AssertionError("x402 handler did not intercept the x402 route")


class GatewayIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.job_service = FakeJobService()
        self.transport = FakeAgentCoreTransport(self.job_service)
        agentcore = AgentCoreClient(
            "https://agentcore.example.test/runtime",
            FakeOAuth().authorization_header,
            self.transport,
        )
        self.application = handler.GatewayApplication(FakeOAuth(), agentcore)

    def invoke(self, event: dict[str, Any]) -> dict[str, Any]:
        with patch.object(handler, "_application", self.application):
            return handler.lambda_handler(event, CONTEXT)

    def test_api_event_matches_explicit_rest_route_shape(self) -> None:
        event = api_event(
            method="POST",
            path="/x402/analyze/async",
            body=b'{"symbols":["AAPL"]}',
        )

        self.assertEqual(event["resource"], "/x402/analyze/async")
        self.assertEqual(event["path"], "/x402/analyze/async")
        self.assertIsNone(event["pathParameters"])
        self.assertEqual(
            event["requestContext"]["resourcePath"],
            "/x402/analyze/async",
        )
        self.assertEqual(
            event["requestContext"]["path"],
            "/testnet/x402/analyze/async",
        )

    def test_missing_payment_round_trip_returns_real_402(self) -> None:
        response = self.invoke(api_event(
            method="POST",
            path="/x402/analyze/async",
            body=b'{"symbols":["AAPL"]}',
        ))

        self.assertEqual(response["statusCode"], 402)
        self.assertFalse(response["isBase64Encoded"])
        body = json.loads(response["body"])
        self.assertEqual(body["error"], "Payment Required")
        self.assertEqual(
            body["paymentRequired"]["resource"],
            "https://a1b2c3d4e5.execute-api.us-east-1.amazonaws.com/testnet/x402/analyze/async",
        )
        self.assertEqual(self.job_service.create_calls, [])

    def test_valid_create_round_trip_returns_202(self) -> None:
        response = self.invoke(api_event(
            method="POST",
            path="/x402/analyze/async",
            headers={"X-Payment": "test-proof"},
            body=b'{"symbols":["AAPL"]}',
        ))

        self.assertEqual(response["statusCode"], 202)
        self.assertFalse(response["isBase64Encoded"])
        body = json.loads(response["body"])
        self.assertEqual(body["jobId"], JOB_ID)
        self.assertEqual(body["status"], "queued")
        self.assertEqual(self.job_service.create_calls, [("test-proof", {"symbols": ["AAPL"]})])
        request_id = self.transport.requests[0]["id"]
        self.assertEqual(request_id, self.transport.requests[0]["params"]["message"]["messageId"])
