import base64
import json
import unittest

from agentcore_client import (
    AgentCoreClient,
    AgentInvocationRateLimited,
    AgentInvocationTimeout,
    AgentInvocationUnavailable,
    AgentInvocationUnauthorized,
    InvalidAgentResponse,
)


REQUEST_ID = "x402gw_" + "b" * 64
ENVELOPE = {
    "version": 1,
    "requestId": REQUEST_ID,
    "method": "GET",
    "path": "/x402/price",
    "publicBaseUrl": "https://gateway.example.test/stages/testnet",
    "headers": {"accept": "application/json"},
    "bodyBase64": "",
}


def response_envelope(*, request_id=REQUEST_ID, status=402, headers=None, body=b'{"error":"Payment Required"}'):
    return {
        "requestId": request_id,
        "status": status,
        "headers": headers or {"content-type": "application/json"},
        "bodyBase64": base64.b64encode(body).decode("ascii"),
    }


def a2a_response(envelope, *, status=200):
    return {
        "status": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({
            "jsonrpc": "2.0",
            "id": REQUEST_ID,
            "result": {
                "kind": "message",
                "messageId": "agent-message-1",
                "contextId": "context-1",
                "taskId": "task-1",
                "role": "agent",
                "parts": [{"kind": "data", "data": envelope}],
            },
        }).encode("utf-8"),
    }


class AgentTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.last_url = None
        self.last_headers = None
        self.last_body = None

    def __call__(self, *, url, headers, body, timeout_seconds):
        self.last_url = url
        self.last_headers = dict(headers)
        self.last_body = body
        if self.error:
            raise self.error
        return self.response


class AgentCoreClientTests(unittest.TestCase):
    def test_invocation_is_valid_a2a_data_part(self):
        transport = AgentTransport(a2a_response(response_envelope()))
        client = AgentCoreClient(
            "https://agentcore.example.test/runtime", lambda: "Bearer access-token", transport
        )
        response = client.invoke(ENVELOPE)
        request = json.loads(transport.last_body)
        data = request["params"]["message"]["parts"][0]["data"]
        self.assertEqual(data, {"skill": "x402_http_envelope", "envelope": ENVELOPE})
        self.assertEqual(transport.last_headers["Authorization"], "Bearer access-token")
        self.assertEqual(transport.last_headers["Content-Type"], "application/json")
        self.assertEqual(
            transport.last_headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"],
            f"x402-gateway-session-{REQUEST_ID}",
        )
        self.assertGreaterEqual(
            len(transport.last_headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]), 33
        )
        self.assertEqual(response, response_envelope())

    def test_rejects_mismatched_response_request_id(self):
        client = AgentCoreClient(
            "https://agentcore.example.test/runtime", lambda: "Bearer access-token",
            AgentTransport(a2a_response(response_envelope(request_id="x402gw_" + "a" * 64))),
        )
        with self.assertRaises(InvalidAgentResponse):
            client.invoke(ENVELOPE)

    def test_maps_documented_upstream_statuses_without_body(self):
        expected = {401: AgentInvocationUnauthorized, 403: AgentInvocationUnauthorized, 429: AgentInvocationRateLimited, 500: AgentInvocationUnavailable}
        for status, error_type in expected.items():
            with self.subTest(status=status), self.assertRaises(error_type) as captured:
                AgentCoreClient(
                    "https://agentcore.example.test/runtime", lambda: "Bearer access-token",
                    AgentTransport({"status": status, "headers": {"content-type": "text/plain"}, "body": b"private upstream response"}),
                ).invoke(ENVELOPE)
            self.assertNotIn("private upstream response", str(captured.exception))

    def test_maps_transport_timeout_to_indeterminate_error(self):
        with self.assertRaises(AgentInvocationTimeout):
            AgentCoreClient(
                "https://agentcore.example.test/runtime", lambda: "Bearer access-token",
                AgentTransport(error=TimeoutError()),
            ).invoke(ENVELOPE)
