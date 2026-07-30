import json
import unittest
from unittest.mock import Mock

from oauth_client import OAuthClient, OAuthUnavailable


def secret_json(**overrides):
    values = {
        "client_id": "gateway-client",
        "client_secret": "gateway-secret",
        "token_url": "https://auth.example.test/oauth2/token",
        "scope": "api/x402.invoke",
    }
    values.update(overrides)
    return json.dumps(values)


class TokenTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, *, url, headers, body, timeout_seconds):
        self.calls.append({
            "url": url,
            "headers": dict(headers),
            "body": body,
            "timeout_seconds": timeout_seconds,
        })
        return self.responses.pop(0)


def token_response(token, expires_in=60, status=200):
    return {
        "status": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"access_token": token, "token_type": "Bearer", "expires_in": expires_in}).encode(),
    }


class OAuthClientTests(unittest.TestCase):
    def test_secret_is_read_only_once_per_warm_cache(self):
        secret_reader = Mock(return_value=secret_json())
        transport = TokenTransport([token_response("token-1")])
        client = OAuthClient(secret_reader, transport, clock=lambda: 0)
        self.assertEqual(client.authorization_header(), "Bearer token-1")
        self.assertEqual(client.authorization_header(), "Bearer token-1")
        secret_reader.assert_called_once()
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0]["url"], "https://auth.example.test/oauth2/token")
        self.assertEqual(transport.calls[0]["headers"]["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(transport.calls[0]["headers"]["authorization"], "Basic Z2F0ZXdheS1jbGllbnQ6Z2F0ZXdheS1zZWNyZXQ=")
        self.assertEqual(transport.calls[0]["body"], b"grant_type=client_credentials&scope=api%2Fx402.invoke")

    def test_refreshes_before_expiry(self):
        now = [0.0]
        secret_reader = Mock(return_value=secret_json())
        transport = TokenTransport([token_response("token-1"), token_response("token-2")])
        client = OAuthClient(secret_reader, transport, clock=lambda: now[0])
        self.assertEqual(client.authorization_header(), "Bearer token-1")
        now[0] = 31.0
        self.assertEqual(client.authorization_header(), "Bearer token-2")
        secret_reader.assert_called_once()
        self.assertEqual(len(transport.calls), 2)

    def test_rejects_secret_with_extra_or_empty_fields(self):
        for secret in (secret_json(extra="not permitted"), secret_json(scope=""), secret_json(client_id="   ")):
            with self.subTest(secret=secret), self.assertRaisesRegex(OAuthUnavailable, "oauth_secret_invalid"):
                OAuthClient(Mock(return_value=secret), TokenTransport([]), clock=lambda: 0).authorization_header()

    def test_hides_token_response_body_on_unavailable_error(self):
        client = OAuthClient(
            Mock(return_value=secret_json()),
            TokenTransport([{"status": 500, "headers": {"content-type": "text/plain"}, "body": b"token-secret-body"}]),
            clock=lambda: 0,
        )
        with self.assertRaises(OAuthUnavailable) as captured:
            client.authorization_header()
        self.assertEqual(str(captured.exception), "oauth_token_unavailable")
        self.assertNotIn("token-secret-body", str(captured.exception))
