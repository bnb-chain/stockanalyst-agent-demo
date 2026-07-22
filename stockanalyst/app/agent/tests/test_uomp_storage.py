"""Tests for the SSRF-safe UOMP gateway transport."""

from __future__ import annotations

import socket
import ssl
import sys
from types import ModuleType
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch


# Exercise the real provider while stubbing only its optional SDK base class.
bnbagent_stub = ModuleType("bnbagent")
storage_stub = ModuleType("bnbagent.storage")


class StorageProvider:
    pass


storage_stub.StorageProvider = StorageProvider
bnbagent_stub.storage = storage_stub
sys.modules.setdefault("bnbagent", bnbagent_stub)
sys.modules.setdefault("bnbagent.storage", storage_stub)

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


class ResourceUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_accepts_only_single_payload_on_same_origin(self) -> None:
        opener = FakeOpener(b'{"response":{"content":"report"}}')
        instance = provider(opener)

        result = await instance.download(
            "https://buyer.trycloudflare.com:443/v1/payload/payload_123"
        )

        self.assertEqual(result, {"response": {"content": "report"}})
        request, timeout = opener.requests[0]
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
