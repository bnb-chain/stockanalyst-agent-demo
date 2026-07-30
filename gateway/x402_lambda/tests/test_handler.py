import base64
import json
import os
import unittest
from unittest.mock import patch

import handler
from agentcore_client import AgentInvocationTimeout, InvalidAgentResponse
from oauth_client import OAuthUnavailable


PUBLIC_BASE = "https://gateway.example.test/stages/testnet"


EVENT = {
    "resource": "/{proxy+}",
    "path": "/testnet/x402/price",
    "httpMethod": "GET",
    "headers": {"Accept": "application/json"},
    "multiValueHeaders": {},
    "queryStringParameters": None,
    "multiValueQueryStringParameters": None,
    "pathParameters": {"proxy": "x402/price"},
    "stageVariables": None,
    "requestContext": {"stage": "testnet", "requestId": "gateway-request"},
    "body": None,
    "isBase64Encoded": False,
}
CONTEXT = object()


def response_envelope(*, status=402, headers=None, body=b'{"error":"Payment Required"}'):
    return {
        "requestId": "ignored-by-test",
        "status": status,
        "headers": headers or {"content-type": "application/json"},
        "bodyBase64": base64.b64encode(body).decode("ascii"),
    }


class FakeOAuth:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = 0

    def authorization_header(self):
        self.calls += 1
        if self.failure:
            raise self.failure
        return "Bearer test-token"


class FakeAgentCore:
    def __init__(self, result=None, failure=None):
        self.result = result
        self.failure = failure
        self.calls = []

    def invoke(self, envelope, *, authorization_header=None):
        self.calls.append({"envelope": envelope, "authorization_header": authorization_header})
        if self.failure:
            raise self.failure
        return {**self.result, "requestId": envelope["requestId"]}


class HandlerTests(unittest.TestCase):
    def invoke(self, oauth, agentcore):
        app = handler.GatewayApplication(PUBLIC_BASE, oauth, agentcore)
        with patch.object(handler, "_application", app):
            return handler.lambda_handler(EVENT, CONTEXT)

    def test_reconstructs_application_402(self):
        result = self.invoke(
            FakeOAuth(),
            FakeAgentCore(response_envelope(headers={
                "content-type": "application/json",
                "x-payment-required": '{"x402Version":2}',
            })),
        )
        self.assertEqual(result["statusCode"], 402)
        self.assertEqual(result["headers"]["x-payment-required"], '{"x402Version":2}')
        self.assertTrue(result["isBase64Encoded"])

    def test_oauth_failure_returns_safe_503(self):
        result = self.invoke(FakeOAuth(OAuthUnavailable()), FakeAgentCore(response_envelope()))
        self.assertEqual(result["statusCode"], 503)
        self.assertEqual(json.loads(result["body"])["errorCode"], "service_unavailable")

    def test_malformed_agent_response_returns_safe_502(self):
        result = self.invoke(FakeOAuth(), FakeAgentCore(failure=InvalidAgentResponse()))
        self.assertEqual(result["statusCode"], 502)
        self.assertNotIn("AgentCore", result["body"])

    def test_upstream_timeout_returns_indeterminate_503(self):
        result = self.invoke(FakeOAuth(), FakeAgentCore(failure=AgentInvocationTimeout()))
        self.assertEqual(result["statusCode"], 503)
        body = json.loads(result["body"])
        self.assertEqual(body["errorCode"], "settlement_status_unknown")
        self.assertTrue(body["retryable"])
        self.assertNotIn("proof", result["body"].lower())

    def test_invalid_gateway_request_uses_its_safe_status(self):
        event = {**EVENT, "path": "/testnet/x402/free"}
        app = handler.GatewayApplication(PUBLIC_BASE, FakeOAuth(), FakeAgentCore(response_envelope()))
        with patch.object(handler, "_application", app):
            result = handler.lambda_handler(event, CONTEXT)
        self.assertEqual(result["statusCode"], 400)
        self.assertEqual(json.loads(result["body"])["errorCode"], "route_not_allowed")

    def test_missing_configuration_returns_safe_503(self):
        with patch.object(handler, "_application", None), patch.dict(os.environ, {}, clear=True):
            result = handler.lambda_handler(EVENT, CONTEXT)
        self.assertEqual(result["statusCode"], 503)
        self.assertEqual(json.loads(result["body"])["errorCode"], "service_unavailable")
