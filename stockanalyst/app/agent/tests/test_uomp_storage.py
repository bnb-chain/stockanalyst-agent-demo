"""Tests for the SSRF-safe UOMP gateway transport."""

from __future__ import annotations

from email.message import Message
import socket
import ssl
import sys
from types import ModuleType
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch


# Exercise the real provider while stubbing only its optional SDK base class.
bnbagent_stub = ModuleType("bnbagent")
storage_stub = ModuleType("bnbagent.storage")


class StorageProvider:
    pass


storage_stub.StorageProvider = StorageProvider
bnbagent_stub.storage = storage_stub
sys.modules.setdefault("bnbagent", bnbagent_stub)
sys.modules.setdefault("bnbagent.storage", storage_stub)

from stockanalyst.app.agent import uomp_storage as storage_module  # noqa: E402
from stockanalyst.app.agent.uomp_storage import (  # noqa: E402
    UOMPGatewayStorageProvider,
)


def public_resolver(host: str, port: int, *args, **kwargs):
    del host, args, kwargs
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("104.16.132.229", port),
        )
    ]


def loopback_resolver(host: str, port: int, *args, **kwargs):
    del host, args, kwargs
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("127.0.0.1", port),
        )
    ]


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.read_sizes: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        del args

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.body if size < 0 else self.body[:size]


class FakeOpener:
    def __init__(self, body: bytes = b'{"payload_id":"payload_123"}', error=None) -> None:
        self.response = FakeResponse(body)
        self.error = error
        self.requests: list[tuple[urllib.request.Request, int]] = []

    def open(self, request: urllib.request.Request, *, timeout: int):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class FakeTransportResponse(FakeResponse):
    def __init__(
        self,
        body: bytes,
        *,
        code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(body)
        self.code = code
        self.status = code
        self.reason = "Found" if 300 <= code < 400 else "OK"
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.msg = self.reason
        self.url = ""

    def info(self):
        return self.headers

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self, response: FakeTransportResponse, requests: list[dict]) -> None:
        self.response = response
        self.requests = requests
        self.sock = None

    def set_debuglevel(self, level: int) -> None:
        del level

    def set_tunnel(self, *args, **kwargs) -> None:
        del args, kwargs

    def request(self, method, path, body, headers, *, encode_chunked) -> None:
        self.requests.append(
            {
                "method": method,
                "path": path,
                "body": body,
                "headers": headers,
                "encode_chunked": encode_chunked,
            }
        )

    def getresponse(self) -> FakeTransportResponse:
        return self.response

    def close(self) -> None:
        pass


class FakeConnectionFactory:
    def __init__(self, *responses: FakeTransportResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.requests: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return FakeConnection(self.responses.pop(0), self.requests)


def provider(opener: FakeOpener | None = None) -> UOMPGatewayStorageProvider:
    return UOMPGatewayStorageProvider(
        "https://buyer.trycloudflare.com",
        "token",
        resolver=public_resolver,
        opener=opener or FakeOpener(),
    )


class ProviderConstructionTests(unittest.TestCase):
    def test_constructor_rechecks_and_normalizes_gateway_origin(self) -> None:
        instance = UOMPGatewayStorageProvider(
            "https://Buyer.TryCloudflare.com/",
            "token",
            resolver=public_resolver,
            opener=FakeOpener(),
        )

        self.assertEqual(instance._base, "https://buyer.trycloudflare.com")
        with self.assertRaises(ValueError):
            UOMPGatewayStorageProvider(
                "http://169.254.169.254",
                "token",
                resolver=loopback_resolver,
                opener=FakeOpener(),
            )

    def test_default_opener_verifies_tls_and_refuses_redirects(self) -> None:
        captured_handlers: list[object] = []

        def capture_opener(*handlers):
            captured_handlers.extend(handlers)
            return FakeOpener()

        with patch.object(urllib.request, "build_opener", side_effect=capture_opener):
            UOMPGatewayStorageProvider(
                "https://buyer.trycloudflare.com",
                "token",
                resolver=public_resolver,
            )

        https_handler = next(
            handler for handler in captured_handlers if isinstance(handler, urllib.request.HTTPSHandler)
        )
        redirect_handler = next(
            handler
            for handler in captured_handlers
            if isinstance(handler, urllib.request.HTTPRedirectHandler)
        )
        self.assertTrue(https_handler._context.check_hostname)
        self.assertEqual(https_handler._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIsNone(
            redirect_handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example")
        )

    def test_pinned_https_connection_uses_ip_but_hostname_for_sni(self) -> None:
        cases = [
            ("104.16.132.229", socket.AF_INET),
            ("2606:4700::6810:84e5", socket.AF_INET6),
        ]
        for address, family in cases:
            with self.subTest(address=address):
                raw_socket = Mock()
                wrapped_socket = object()
                context = Mock()
                context.wrap_socket.return_value = wrapped_socket
                connection = storage_module._PinnedHTTPSConnection(
                    "buyer.trycloudflare.com",
                    pinned_address=address,
                    pinned_port=443,
                    context=context,
                    timeout=30,
                )

                with (
                    patch.object(
                        storage_module.socket,
                        "getaddrinfo",
                        side_effect=AssertionError("must not resolve while connecting"),
                    ) as resolve,
                    patch.object(
                        storage_module.socket,
                        "socket",
                        return_value=raw_socket,
                    ) as make_socket,
                ):
                    connection.connect()

                resolve.assert_not_called()
                make_socket.assert_called_once_with(family, socket.SOCK_STREAM)
                raw_socket.settimeout.assert_called_once_with(connection.timeout)
                raw_socket.connect.assert_called_once_with((address, 443))
                context.wrap_socket.assert_called_once_with(
                    raw_socket,
                    server_hostname="buyer.trycloudflare.com",
                )
                self.assertIs(connection.sock, wrapped_socket)


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_uses_validated_origin_and_bounded_json_response(self) -> None:
        opener = FakeOpener(b'{"payload_id":"payload_123"}')
        instance = provider(opener)

        result = await instance.upload({"response": {"content": "report"}})

        self.assertEqual(result, "https://buyer.trycloudflare.com/v1/payload/payload_123")
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "https://buyer.trycloudflare.com/v1/payload/upload")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer token")
        self.assertEqual(timeout, 30)
        self.assertEqual(opener.response.read_sizes, [65_537])

    async def test_upload_rejects_oversized_or_non_json_response(self) -> None:
        bodies = [b"x" * 65_537, b"not json", b"[]", b"{}"]
        for body in bodies:
            with self.subTest(body_length=len(body)), self.assertRaises(ValueError):
                await provider(FakeOpener(body)).upload({"response": {"content": "report"}})

    async def test_upload_rejects_invalid_payload_id(self) -> None:
        invalid_ids = ["", "../../admin", "with/slash", "percent%2Fescape", "a" * 129, 42]
        for payload_id in invalid_ids:
            body = ("{\"payload_id\":" + repr(payload_id).replace("'", '"') + "}").encode()
            with self.subTest(payload_id=payload_id), self.assertRaises(ValueError):
                await provider(FakeOpener(body)).upload({"response": {"content": "report"}})

    async def test_upload_does_not_recover_from_redirect_response(self) -> None:
        redirect = urllib.error.HTTPError(
            "https://buyer.trycloudflare.com/v1/payload/upload",
            302,
            "Found",
            {"Location": "https://evil.example"},
            None,
        )
        try:
            with self.assertRaises(urllib.error.HTTPError):
                await provider(FakeOpener(error=redirect)).upload(
                    {"response": {"content": "report"}}
                )
        finally:
            redirect.close()

    async def test_constructed_transport_rejects_redirect_without_second_connection(self) -> None:
        factory = FakeConnectionFactory(
            FakeTransportResponse(
                b"redirect",
                code=302,
                headers={"Location": "https://evil.example/v1/payload/upload"},
            )
        )
        instance = UOMPGatewayStorageProvider(
            "https://buyer.trycloudflare.com",
            "token",
            resolver=public_resolver,
            connection_factory=factory,
        )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            await instance.upload({"response": {"content": "report"}})

        self.assertEqual(raised.exception.code, 302)
        raised.exception.close()
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(factory.calls[0]["host"], "buyer.trycloudflare.com")


class ResourceUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_accepts_only_single_payload_on_same_origin(self) -> None:
        opener = FakeOpener(b'{"response":{"content":"report"}}')
        instance = provider(opener)

        result = await instance.download(
            "HTTPS://BUYER.TRYCLOUDFLARE.COM:443/v1/payload/payload_123"
        )

        self.assertEqual(result, {"response": {"content": "report"}})
        request, timeout = opener.requests[0]
        self.assertEqual(
            request.full_url,
            "https://buyer.trycloudflare.com/v1/payload/payload_123",
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 30)
        self.assertEqual(opener.response.read_sizes, [65_537])

    async def test_download_rejects_cross_origin_and_path_smuggling(self) -> None:
        urls = [
            "https://evil.trycloudflare.com/v1/payload/payload_123",
            "http://buyer.trycloudflare.com/v1/payload/payload_123",
            "https://buyer.trycloudflare.com:8443/v1/payload/payload_123",
            "https://user@buyer.trycloudflare.com/v1/payload/payload_123",
            "https://buyer.trycloudflare.com/v1/payload/payload_123/extra",
            "https://buyer.trycloudflare.com/v1/payload/../admin",
            "https://buyer.trycloudflare.com/v1/payload/%2Fadmin",
            "https://buyer.trycloudflare.com/v1/payload/payload_123?next=/admin",
            "https://buyer.trycloudflare.com/v1/payload/payload_123#fragment",
        ]
        instance = provider()
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                await instance.download(url)

    async def test_exists_validates_url_and_uses_same_no_redirect_opener(self) -> None:
        opener = FakeOpener()
        instance = provider(opener)

        self.assertTrue(
            await instance.exists("https://buyer.trycloudflare.com/v1/payload/payload_123")
        )
        request, timeout = opener.requests[0]
        self.assertEqual(request.get_method(), "HEAD")
        self.assertEqual(timeout, 10)
        with self.assertRaises(ValueError):
            await instance.exists("https://evil.example/v1/payload/payload_123")

    async def test_exists_returns_false_on_transport_error(self) -> None:
        instance = provider(FakeOpener(error=OSError("offline")))

        self.assertFalse(
            await instance.exists("https://buyer.trycloudflare.com/v1/payload/payload_123")
        )

    async def test_all_requests_use_validated_address_without_dns_rebinding(self) -> None:
        resolver_calls = 0

        def rebinding_resolver(host: str, port: int, *args, **kwargs):
            nonlocal resolver_calls
            del host, args, kwargs
            resolver_calls += 1
            address = "104.16.132.229" if resolver_calls == 1 else "127.0.0.1"
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (address, port),
                )
            ]

        factory = FakeConnectionFactory(
            FakeTransportResponse(b'{"payload_id":"payload_123"}'),
            FakeTransportResponse(b'{"response":{"content":"report"}}'),
            FakeTransportResponse(b""),
        )
        instance = UOMPGatewayStorageProvider(
            "HTTPS://BUYER.TRYCLOUDFLARE.COM:443/",
            "token",
            resolver=rebinding_resolver,
            connection_factory=factory,
        )

        await instance.upload({"response": {"content": "report"}})
        await instance.download(
            "HTTPS://BUYER.TRYCLOUDFLARE.COM:443/v1/payload/payload_123"
        )
        self.assertTrue(
            await instance.exists(
                "HTTPS://BUYER.TRYCLOUDFLARE.COM:443/v1/payload/payload_123"
            )
        )

        self.assertEqual(resolver_calls, 1)
        self.assertEqual(len(factory.calls), 3)
        for call in factory.calls:
            self.assertEqual(call["host"], "buyer.trycloudflare.com")
            self.assertEqual(call["address"], "104.16.132.229")
            self.assertEqual(call["port"], 443)
