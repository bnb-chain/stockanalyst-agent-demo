"""Tests for the SSRF-safe UOMP gateway transport."""

from __future__ import annotations

import socket
import ssl
import sys
import unittest
import urllib.error
import urllib.request
from email.message import Message
from types import ModuleType
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

from stockanalyst.app.agent import uomp_storage as storage_module
from stockanalyst.app.agent.uomp_storage import (
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
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        del args
        self.close()

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.body if size < 0 else self.body[:size]

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SlowDripResponse(FakeResponse):
    def __init__(self, chunks: list[bytes], clock: FakeClock) -> None:
        super().__init__(b"".join(chunks))
        self._chunks = list(chunks)
        self._clock = clock

    def read(self, size: int = -1) -> bytes:
        self._clock.advance(0.4)
        return super().read(size)

    def read1(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        self._clock.advance(0.4)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class FakeOpener:
    def __init__(
        self,
        body: bytes = b'{"payload_id":"pay_0123456789abcdef0123456789abcdef"}',
        error=None,
    ) -> None:
        self.response = FakeResponse(body)
        self.error = error
        self.requests: list[tuple[urllib.request.Request, int]] = []

    def open(self, request: urllib.request.Request, *, timeout: int):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class DelayedOpener(FakeOpener):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__(b"")
        self._clock = clock

    def open(self, request: urllib.request.Request, *, timeout: int):
        self._clock.advance(1.1)
        return super().open(request, timeout=timeout)


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


def valid_manifest(total_bytes: int) -> bytes:
    prefix = b'{"response":{"content":"'
    suffix = b'"}}'
    return prefix + b"x" * (total_bytes - len(prefix) - len(suffix)) + suffix


class ProviderConstructionTests(unittest.TestCase):
    def test_deadline_socket_recomputes_remaining_timeout_and_closes_on_expiry(self) -> None:
        clock = FakeClock()
        raw_socket = Mock()
        raw_socket.recv_into.return_value = 1
        deadline_socket = getattr(storage_module, "_DeadlineSocket", None)
        self.assertIsNotNone(deadline_socket)
        wrapped = deadline_socket(
            raw_socket,
            deadline=1.0,
            monotonic=clock,
        )
        clock.advance(0.75)

        self.assertEqual(wrapped.recv_into(bytearray(1)), 1)
        raw_socket.settimeout.assert_called_once_with(0.25)

        clock.advance(0.30)
        with self.assertRaises(TimeoutError):
            wrapped.recv_into(bytearray(1))
        raw_socket.close.assert_called_once_with()

    def test_deadline_socket_defers_raw_close_while_makefile_is_readable(self) -> None:
        client, server = socket.socketpair()
        reader = None
        try:
            wrapped = storage_module._DeadlineSocket(
                client,
                deadline=storage_module._monotonic() + 1,
            )
            reader = wrapped.makefile("rb")
            server.sendall(b"body")

            # urllib closes HTTPConnection.sock after parsing headers while the
            # HTTPResponse makefile must remain able to consume the body.
            wrapped.close()

            self.assertEqual(reader.read(4), b"body")
            self.assertNotEqual(client.fileno(), -1)
            reader.close()
            self.assertEqual(client.fileno(), -1)
        finally:
            if reader is not None:
                reader.close()
            client.close()
            server.close()

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
                    pinned_addresses=(address,),
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
                connect_timeout, tls_timeout = [
                    call.args[0] for call in raw_socket.settimeout.call_args_list
                ]
                self.assertGreater(connect_timeout, 0)
                self.assertLessEqual(connect_timeout, connection.timeout)
                self.assertGreater(tls_timeout, 0)
                self.assertLessEqual(tls_timeout, connect_timeout)
                raw_socket.connect.assert_called_once_with((address, 443))
                context.wrap_socket.assert_called_once_with(
                    raw_socket,
                    server_hostname="buyer.trycloudflare.com",
                )
                self.assertIs(connection.sock, wrapped_socket)

    def test_pinned_connection_tries_all_validated_addresses_without_resolution(self) -> None:
        resolver_calls = 0

        def two_address_resolver(host: str, port: int, *args, **kwargs):
            nonlocal resolver_calls
            del host, args, kwargs
            resolver_calls += 1
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (address, port),
                )
                for address in ("104.16.132.229", "104.16.133.229")
            ]

        captured_handlers: list[object] = []

        def capture_opener(*handlers):
            captured_handlers.extend(handlers)
            return FakeOpener()

        with patch.object(urllib.request, "build_opener", side_effect=capture_opener):
            UOMPGatewayStorageProvider(
                "https://buyer.trycloudflare.com",
                "token",
                resolver=two_address_resolver,
            )
        handler = next(
            item for item in captured_handlers if isinstance(item, urllib.request.HTTPSHandler)
        )
        connection = handler._connection("buyer.trycloudflare.com", timeout=30)
        first_socket = Mock()
        first_socket.connect.side_effect = OSError("first address unreachable")
        second_socket = Mock()
        wrapped_socket = object()
        handler._context.wrap_socket = Mock(return_value=wrapped_socket)

        with (
            patch.object(
                storage_module.socket,
                "getaddrinfo",
                side_effect=AssertionError("must not resolve while connecting"),
            ) as resolve,
            patch.object(
                storage_module.socket,
                "socket",
                side_effect=[first_socket, second_socket],
            ),
        ):
            connection.connect()

        self.assertEqual(resolver_calls, 1)
        resolve.assert_not_called()
        first_socket.connect.assert_called_once_with(("104.16.132.229", 443))
        first_socket.close.assert_called_once_with()
        second_socket.connect.assert_called_once_with(("104.16.133.229", 443))
        second_socket.close.assert_not_called()
        handler._context.wrap_socket.assert_called_once_with(
            second_socket,
            server_hostname="buyer.trycloudflare.com",
        )
        self.assertIs(connection.sock, wrapped_socket)

    def test_pinned_connection_preserves_first_error_when_all_addresses_fail(self) -> None:
        first_error = OSError("first address unreachable")
        second_error = OSError("second address unreachable")
        first_socket = Mock()
        first_socket.connect.side_effect = first_error
        second_socket = Mock()
        second_socket.connect.side_effect = second_error
        connection = storage_module._PinnedHTTPConnection(
            "buyer.trycloudflare.com",
            pinned_addresses=("104.16.132.229", "104.16.133.229"),
            pinned_port=80,
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
                side_effect=[first_socket, second_socket],
            ),
            self.assertRaises(OSError) as raised,
        ):
            connection.connect()

        self.assertIs(raised.exception, first_error)
        resolve.assert_not_called()
        first_socket.close.assert_called_once_with()
        second_socket.close.assert_called_once_with()

    def test_https_failover_closes_failed_tls_connection(self) -> None:
        first_socket = Mock()
        second_socket = Mock()
        wrapped_socket = object()
        context = Mock()
        context.wrap_socket.side_effect = [ssl.SSLError("TLS failed"), wrapped_socket]
        connection = storage_module._PinnedHTTPSConnection(
            "buyer.trycloudflare.com",
            pinned_addresses=("104.16.132.229", "104.16.133.229"),
            pinned_port=443,
            context=context,
            timeout=30,
        )

        with patch.object(
            storage_module.socket,
            "socket",
            side_effect=[first_socket, second_socket],
        ):
            connection.connect()

        first_socket.close.assert_called_once_with()
        second_socket.close.assert_not_called()
        self.assertEqual(context.wrap_socket.call_count, 2)
        self.assertIs(connection.sock, wrapped_socket)

    def test_connect_attempts_and_tls_handshake_share_one_deadline(self) -> None:
        clock = FakeClock()
        first_socket = Mock()
        second_socket = Mock()
        context = Mock()

        def fail_first(*args) -> None:
            del args
            clock.advance(0.6)
            raise OSError("first address unreachable")

        def connect_second(*args) -> None:
            del args
            clock.advance(0.25)

        def slow_tls(*args, **kwargs):
            del args, kwargs
            clock.advance(0.2)
            return object()

        first_socket.connect.side_effect = fail_first
        second_socket.connect.side_effect = connect_second
        context.wrap_socket.side_effect = slow_tls
        connection = storage_module._PinnedHTTPSConnection(
            "buyer.trycloudflare.com",
            pinned_addresses=("104.16.132.229", "104.16.133.229"),
            pinned_port=443,
            context=context,
            timeout=1,
            response_deadline=1,
        )

        with (
            patch.object(storage_module, "_monotonic", clock),
            patch.object(
                storage_module.socket,
                "socket",
                side_effect=[first_socket, second_socket],
            ),
            self.assertRaises(TimeoutError),
        ):
            connection.connect()

        self.assertAlmostEqual(first_socket.settimeout.call_args_list[0].args[0], 1)
        self.assertAlmostEqual(second_socket.settimeout.call_args_list[0].args[0], 0.4)
        self.assertAlmostEqual(second_socket.settimeout.call_args_list[1].args[0], 0.15)
        first_socket.close.assert_called_once_with()
        second_socket.close.assert_called_once_with()


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_uses_validated_origin_and_bounded_json_response(self) -> None:
        opener = FakeOpener(
            b'{"payload_id":"pay_0123456789abcdef0123456789abcdef"}'
        )
        instance = provider(opener)

        result = await instance.upload({"response": {"content": "report"}})

        self.assertEqual(
            result,
            "https://buyer.trycloudflare.com/v1/payload/"
            "pay_0123456789abcdef0123456789abcdef",
        )
        self.assertNotIn("token", result)
        self.assertNotIn("?", result)
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
        invalid_ids = [
            "",
            "pay_0123456789ABCDEF0123456789abcdef",
            "pay_0123456789abcdef",
            "pay_0123456789abcdef0123456789abcdeg",
            "payload_123",
            "../../admin",
            "with/slash",
            "percent%2Fescape",
            "a" * 129,
            42,
        ]
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

    async def test_upload_enforces_one_total_slow_drip_deadline(self) -> None:
        clock = FakeClock()
        opener = FakeOpener()
        opener.response = SlowDripResponse(
            [
                b'{"payload_id":',
                b'"pay_0123456789abcdef0123456789abcdef"',
                b"}",
            ],
            clock,
        )
        instance = provider(opener)

        with (
            patch.object(storage_module, "_monotonic", clock, create=True),
            patch.object(storage_module, "_TIMEOUT_UPLOAD", 1),
            self.assertRaises(TimeoutError),
        ):
            await instance.upload({"response": {"content": "report"}})

        self.assertTrue(opener.response.closed)


class ResourceUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_accepts_only_single_payload_on_same_origin(self) -> None:
        opener = FakeOpener(b'{"response":{"content":"report"}}')
        instance = provider(opener)

        result = await instance.download(
            "HTTPS://BUYER.TRYCLOUDFLARE.COM:443/v1/payload/"
            "pay_0123456789abcdef0123456789abcdef"
        )

        self.assertEqual(result, {"response": {"content": "report"}})
        request, timeout = opener.requests[0]
        self.assertEqual(
            request.full_url,
            "https://buyer.trycloudflare.com/v1/payload/"
            "pay_0123456789abcdef0123456789abcdef",
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer token")
        self.assertEqual(timeout, 30)
        self.assertEqual(opener.response.read_sizes, [2_097_153])

    async def test_download_accepts_exactly_two_mib_and_rejects_above(self) -> None:
        limit = 2 * 1024 * 1024
        url = (
            "https://buyer.trycloudflare.com/v1/payload/"
            "pay_0123456789abcdef0123456789abcdef"
        )
        exact_opener = FakeOpener(valid_manifest(limit))

        result = await provider(exact_opener).download(url)

        self.assertEqual(
            len(result["response"]["content"]),
            limit - len(b'{"response":{"content":"') - len(b'"}}'),
        )
        self.assertEqual(exact_opener.response.read_sizes, [limit + 1])

        oversized_opener = FakeOpener(valid_manifest(limit + 1))
        with self.assertRaisesRegex(ValueError, "gateway response too large"):
            await provider(oversized_opener).download(url)
        self.assertEqual(oversized_opener.response.read_sizes, [limit + 1])

    async def test_download_normalizes_uppercase_https_with_implicit_default_port(self) -> None:
        opener = FakeOpener(b'{"response":{"content":"report"}}')
        instance = provider(opener)

        await instance.download(
            "HTTPS://BUYER.TRYCLOUDFLARE.COM/v1/payload/"
            "pay_0123456789abcdef0123456789abcdef"
        )

        request, _ = opener.requests[0]
        self.assertEqual(
            request.full_url,
            "https://buyer.trycloudflare.com/v1/payload/"
            "pay_0123456789abcdef0123456789abcdef",
        )

    async def test_download_rejects_non_ascii_host_before_normalization(self) -> None:
        instance = UOMPGatewayStorageProvider(
            "https://k.trycloudflare.com",
            "token",
            resolver=public_resolver,
            opener=FakeOpener(b'{}'),
        )

        with self.assertRaises(ValueError):
            await instance.download(
                "https://K.trycloudflare.com/v1/payload/"
                "pay_0123456789abcdef0123456789abcdef"
            )

    async def test_rejects_explicit_zero_port_before_authenticated_request(self) -> None:
        opener = FakeOpener(b"{}")
        instance = provider(opener)
        url = (
            "https://buyer.trycloudflare.com:0/v1/payload/"
            "pay_0123456789abcdef0123456789abcdef"
        )

        with self.assertRaises(ValueError):
            await instance.download(url)
        with self.assertRaises(ValueError):
            await instance.exists(url)

        self.assertEqual(opener.requests, [])

    async def test_download_enforces_one_total_slow_drip_deadline(self) -> None:
        clock = FakeClock()
        opener = FakeOpener()
        opener.response = SlowDripResponse(
            [b'{"response":', b'{"content":"report"}', b"}"],
            clock,
        )
        instance = provider(opener)

        with (
            patch.object(storage_module, "_monotonic", clock, create=True),
            patch.object(storage_module, "_TIMEOUT_DOWNLOAD", 1),
            self.assertRaises(TimeoutError),
        ):
            await instance.download(
                "https://buyer.trycloudflare.com/v1/payload/"
                "pay_0123456789abcdef0123456789abcdef"
            )

        self.assertTrue(opener.response.closed)

    async def test_download_rejects_cross_origin_and_path_smuggling(self) -> None:
        urls = [
            "https://evil.trycloudflare.com/v1/payload/pay_0123456789abcdef0123456789abcdef",
            "http://buyer.trycloudflare.com/v1/payload/pay_0123456789abcdef0123456789abcdef",
            "https://buyer.trycloudflare.com:8443/v1/payload/pay_0123456789abcdef0123456789abcdef",
            "https://user@buyer.trycloudflare.com/v1/payload/pay_0123456789abcdef0123456789abcdef",
            "https://buyer.trycloudflare.com/v1/payload/pay_0123456789abcdef0123456789abcdef/extra",
            "https://buyer.trycloudflare.com/v1/payload/pay_0123456789ABCDEF0123456789abcdef",
            "https://buyer.trycloudflare.com/v1/payload/pay_0123456789abcdef",
            "https://buyer.trycloudflare.com/v1/payload/pay_0123456789abcdef0123456789abcdeg",
            "https://buyer.trycloudflare.com/v1/payload/payload_123",
            "https://buyer.trycloudflare.com/v1/payload/../admin",
            "https://buyer.trycloudflare.com/v1/payload/%2Fadmin",
            "https://buyer.trycloudflare.com/v1/payload/pay_0123456789abcdef0123456789abcdef?next=/admin",
            "https://buyer.trycloudflare.com/v1/payload/pay_0123456789abcdef0123456789abcdef#fragment",
        ]
        instance = provider()
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                await instance.download(url)

    async def test_exists_validates_url_and_uses_same_no_redirect_opener(self) -> None:
        opener = FakeOpener()
        instance = provider(opener)

        self.assertTrue(
            await instance.exists(
                "https://buyer.trycloudflare.com/v1/payload/"
                "pay_0123456789abcdef0123456789abcdef"
            )
        )
        request, timeout = opener.requests[0]
        self.assertEqual(request.get_method(), "HEAD")
        self.assertEqual(request.get_header("Authorization"), "Bearer token")
        self.assertEqual(timeout, 10)
        with self.assertRaises(ValueError):
            await instance.exists(
                "https://evil.example/v1/payload/"
                "pay_0123456789abcdef0123456789abcdef"
            )

    async def test_exists_returns_false_on_transport_error(self) -> None:
        instance = provider(FakeOpener(error=OSError("offline")))

        self.assertFalse(
            await instance.exists(
                "https://buyer.trycloudflare.com/v1/payload/"
                "pay_0123456789abcdef0123456789abcdef"
            )
        )

    async def test_exists_rejects_a_response_opened_after_total_deadline(self) -> None:
        clock = FakeClock()
        opener = DelayedOpener(clock)
        instance = provider(opener)

        with (
            patch.object(storage_module, "_monotonic", clock, create=True),
            patch.object(storage_module, "_TIMEOUT_EXISTS", 1, create=True),
        ):
            exists = await instance.exists(
                "https://buyer.trycloudflare.com/v1/payload/"
                "pay_0123456789abcdef0123456789abcdef"
            )

        self.assertFalse(exists)
        self.assertTrue(opener.response.closed)

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
            FakeTransportResponse(
                b'{"payload_id":"pay_0123456789abcdef0123456789abcdef"}'
            ),
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
            "HTTPS://BUYER.TRYCLOUDFLARE.COM:443/v1/payload/"
            "pay_0123456789abcdef0123456789abcdef"
        )
        self.assertTrue(
            await instance.exists(
                "HTTPS://BUYER.TRYCLOUDFLARE.COM:443/v1/payload/"
                "pay_0123456789abcdef0123456789abcdef"
            )
        )

        self.assertEqual(resolver_calls, 1)
        self.assertEqual(len(factory.calls), 3)
        for call in factory.calls:
            self.assertEqual(call["host"], "buyer.trycloudflare.com")
            self.assertEqual(call["address"], "104.16.132.229")
            self.assertEqual(call["port"], 443)
