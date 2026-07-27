"""Tests for strict, immutable signed notification contexts."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import socket
import unittest
from ipaddress import ip_address
from pathlib import Path
from unittest.mock import patch

from eth_account import Account
from eth_account.messages import encode_typed_data
from stockanalyst.app.agent.notify_security import (
    NotifySecurityError,
    _is_public_gateway_address,
    build_notify_typed_data,
    parse_signed_context,
    validate_gateway_url,
    verify_notify_authorization,
)


def _valid_context() -> dict[str, object]:
    return {
        "delivery_gateway_url": "https://buyer.trycloudflare.com",
        "delivery_gateway_token": "relay-token",
        "portfolio": [
            {
                "symbol": "AAPL",
                "shares": 10,
                "avgCost": 190.25,
                "currency": "USD",
            }
        ],
        "risk_profile": {
            "tolerance": "moderate",
            "horizonMonths": 12,
            "preferredIndicators": ["RSI-14", "MACD"],
        },
    }


def _raw(context: dict[str, object]) -> str:
    return json.dumps(context, separators=(",", ":"), allow_nan=True)


def _resolver(*addresses: str):
    def resolve(host: str, port: int, *args, **kwargs):
        del host, args, kwargs
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port, 0, 0) if ":" in address else (address, port),
            )
            for address in addresses
        ]

    return resolve


PUBLIC_RESOLVER = _resolver("104.16.132.229")


class GatewayPolicyTests(unittest.TestCase):
    def test_accepts_public_https_origin_on_default_suffix(self) -> None:
        self.assertEqual(
            validate_gateway_url(
                "https://Buyer.TryCloudflare.com/",
                resolver=PUBLIC_RESOLVER,
            ),
            "https://buyer.trycloudflare.com",
        )

    def test_rejects_non_origin_and_ambiguous_url_components(self) -> None:
        urls = [
            "http://buyer.trycloudflare.com",
            "https://user@buyer.trycloudflare.com",
            "https://user:pass@buyer.trycloudflare.com",
            "https://buyer.trycloudflare.com/path",
            "https://buyer.trycloudflare.com/%2fadmin",
            "https://buyer.trycloudflare.com?next=http://127.0.0.1",
            "https://buyer.trycloudflare.com?",
            "https://buyer.trycloudflare.com#fragment",
            "https://buyer.trycloudflare.com#",
            "https://buyer.trycloudflare.com:8443",
            "https://buyer.trycloudflare.com\\@evil.example",
        ]

        for url in urls:
            with self.subTest(url=url), self.assertRaises(NotifySecurityError):
                validate_gateway_url(url, resolver=PUBLIC_RESOLVER)

    def test_rejects_hosts_outside_exact_and_dot_boundary_allowlist(self) -> None:
        with patch.dict(
            os.environ,
            {"DELIVERY_GATEWAY_ALLOWED_HOSTS": "gateway.example,.approved.example"},
            clear=False,
        ):
            self.assertEqual(
                validate_gateway_url("https://gateway.example", resolver=PUBLIC_RESOLVER),
                "https://gateway.example",
            )
            self.assertEqual(
                validate_gateway_url("https://a.approved.example", resolver=PUBLIC_RESOLVER),
                "https://a.approved.example",
            )
            rejected = [
                "https://sub.gateway.example",
                "https://approved.example",
                "https://evilapproved.example",
                "https://buyer.trycloudflare.com",
            ]
            for url in rejected:
                with self.subTest(url=url), self.assertRaises(NotifySecurityError):
                    validate_gateway_url(url, resolver=PUBLIC_RESOLVER)

    def test_rejects_non_global_and_mixed_dns_answers_in_production(self) -> None:
        non_global = [
            "127.0.0.1",
            "::1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.0.1",
            "169.254.169.254",
            "fe80::1",
            "0.0.0.0",
            "::",
            "224.0.0.1",
            "ff02::1",
            "fec0::1",
            "240.0.0.1",
        ]
        for address in non_global:
            with self.subTest(address=address), self.assertRaises(NotifySecurityError):
                validate_gateway_url(
                    "https://buyer.trycloudflare.com",
                    resolver=_resolver(address),
                )

        with self.assertRaises(NotifySecurityError):
            validate_gateway_url(
                "https://buyer.trycloudflare.com",
                resolver=_resolver("104.16.132.229", "127.0.0.1"),
            )

    def test_rejects_version_independent_special_and_embedded_addresses(self) -> None:
        class LegacyGlobalAddress:
            """Simulate older Python tables that marked the outer IPv6 global."""

            is_global = True
            is_loopback = False
            is_private = False
            is_link_local = False
            is_multicast = False
            is_unspecified = False
            is_reserved = False
            is_site_local = False

            def __init__(self, value: str) -> None:
                self._address = ip_address(value)

            def __str__(self) -> str:
                return str(self._address)

        explicitly_denied = [
            "192.0.0.9",
            "64:ff9b:1::808:808",
            "2002:0808:0808::1",
        ]
        for address in explicitly_denied:
            with self.subTest(address=address):
                self.assertFalse(
                    _is_public_gateway_address(LegacyGlobalAddress(address))
                )

        embedded_non_global = [
            "::ffff:10.0.0.1",
            "::ffff:0:10.0.0.1",
            "::ffff:0:169.254.169.254",
            "64:ff9b::a00:1",
            "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
        ]
        for address in embedded_non_global:
            with self.subTest(address=address):
                self.assertFalse(
                    _is_public_gateway_address(LegacyGlobalAddress(address))
                )

    def test_rejects_localhost_ip_literals_and_failed_resolution(self) -> None:
        cases = [
            ("https://localhost", _resolver("127.0.0.1")),
            ("https://127.0.0.1", _resolver("127.0.0.1")),
            ("https://[::1]", _resolver("::1")),
            ("https://buyer.trycloudflare.com", _resolver()),
        ]
        for url, resolver in cases:
            with self.subTest(url=url), self.assertRaises(NotifySecurityError):
                validate_gateway_url(url, resolver=resolver)

    def test_rejects_resolution_errors(self) -> None:
        def failing_resolver(*args, **kwargs):
            del args, kwargs
            raise socket.gaierror("not found")

        with self.assertRaises(NotifySecurityError):
            validate_gateway_url(
                "https://buyer.trycloudflare.com",
                resolver=failing_resolver,
            )

    def test_rejects_malformed_allowlist_rules_as_a_whole(self) -> None:
        malformed = [
            "",
            ".",
            "gateway.example,",
            "gateway.example,,other.example",
            "gateway.example,.",
            "gateway.example,..example",
            "gateway.example,example.",
            "gateway.example,*.example",
            "gateway.example,https://other.example",
        ]
        for configured in malformed:
            with (
                self.subTest(configured=configured),
                patch.dict(
                    os.environ,
                    {"DELIVERY_GATEWAY_ALLOWED_HOSTS": configured},
                    clear=False,
                ),
                self.assertRaises(NotifySecurityError),
            ):
                validate_gateway_url("https://gateway.example", resolver=PUBLIC_RESOLVER)

    def test_rejects_non_ascii_hosts_and_allowlist_rules(self) -> None:
        for url in (
            "https://bücher.trycloudflare.com",
            "https://K.trycloudflare.com",
        ):
            with self.subTest(url=url), self.assertRaises(NotifySecurityError):
                validate_gateway_url(url, resolver=PUBLIC_RESOLVER)
        with (
            patch.dict(
                os.environ,
                {"DELIVERY_GATEWAY_ALLOWED_HOSTS": "gateway.example,bücher.example"},
                clear=False,
            ),
            self.assertRaises(NotifySecurityError),
        ):
            validate_gateway_url("https://gateway.example", resolver=PUBLIC_RESOLVER)
        with (
            patch.dict(
                os.environ,
                {"DELIVERY_GATEWAY_ALLOWED_HOSTS": "Kateway.example"},
                clear=False,
            ),
            self.assertRaises(NotifySecurityError),
        ):
            validate_gateway_url("https://kateway.example", resolver=PUBLIC_RESOLVER)

    def test_rejects_trailing_dot_host_and_wildcard_rule_bypasses(self) -> None:
        with self.assertRaises(NotifySecurityError):
            validate_gateway_url(
                "https://buyer.trycloudflare.com.",
                resolver=PUBLIC_RESOLVER,
            )
        with (
            patch.dict(
                os.environ,
                {"DELIVERY_GATEWAY_ALLOWED_HOSTS": "*.trycloudflare.com"},
                clear=False,
            ),
            self.assertRaises(NotifySecurityError),
        ):
            validate_gateway_url(
                "https://buyer.trycloudflare.com",
                resolver=PUBLIC_RESOLVER,
            )

    def test_development_flag_allows_only_http_loopback_origin(self) -> None:
        self.assertEqual(
            validate_gateway_url(
                "http://127.0.0.1:9444",
                allow_private=True,
                resolver=_resolver("127.0.0.1"),
            ),
            "http://127.0.0.1:9444",
        )
        self.assertEqual(
            validate_gateway_url(
                "http://[::1]:9444",
                allow_private=True,
                resolver=_resolver("::1"),
            ),
            "http://[::1]:9444",
        )
        for address in ("169.254.169.254", "10.0.0.1", "104.16.132.229"):
            with self.subTest(address=address), self.assertRaises(NotifySecurityError):
                validate_gateway_url(
                    "http://gateway.example:9444",
                    allow_private=True,
                    resolver=_resolver(address),
                )

    def test_explicit_private_setting_overrides_environment(self) -> None:
        with patch.dict(os.environ, {"ALLOW_PRIVATE_DELIVERY_GATEWAY": "true"}):
            self.assertEqual(
                validate_gateway_url(
                    "http://127.0.0.1:9444",
                    resolver=_resolver("127.0.0.1"),
                ),
                "http://127.0.0.1:9444",
            )
            with self.assertRaises(NotifySecurityError):
                validate_gateway_url(
                    "http://127.0.0.1:9444",
                    allow_private=False,
                    resolver=_resolver("127.0.0.1"),
                )


class ContextTests(unittest.TestCase):
    def test_parses_valid_context_into_immutable_values(self) -> None:
        raw = _raw(_valid_context())

        context = parse_signed_context(raw)

        self.assertEqual(context.gateway_url, "https://buyer.trycloudflare.com")
        self.assertEqual(context.gateway_token, "relay-token")
        self.assertEqual(context.portfolio[0].symbol, "AAPL")
        self.assertEqual(context.portfolio[0].shares, 10)
        self.assertEqual(context.risk_profile.tolerance, "moderate")
        self.assertEqual(context.digest, hashlib.sha256(raw.encode()).hexdigest())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            context.gateway_url = "https://attacker.example"  # type: ignore[misc]

    def test_prompt_values_are_fresh_caller_owned_dictionaries(self) -> None:
        context = parse_signed_context(_raw(_valid_context()))

        portfolio = context.portfolio_for_prompt()
        risk_profile = context.risk_profile_for_prompt()
        portfolio[0]["symbol"] = "EVIL"
        risk_profile["tolerance"] = "aggressive"

        self.assertEqual(context.portfolio[0].symbol, "AAPL")
        self.assertEqual(context.risk_profile.tolerance, "moderate")
        self.assertEqual(context.portfolio_for_prompt()[0]["symbol"], "AAPL")
        self.assertEqual(context.risk_profile_for_prompt()["tolerance"], "moderate")

    def test_rejects_invalid_context_shapes_and_values(self) -> None:
        cases: list[tuple[str, object]] = [
            ("unknown root field", {**_valid_context(), "extra": "no"}),
            (
                "unknown holding field",
                {
                    **_valid_context(),
                    "portfolio": [{**_valid_context()["portfolio"][0], "extra": "no"}],  # type: ignore[index]
                },
            ),
            (
                "unknown risk field",
                {**_valid_context(), "risk_profile": {**_valid_context()["risk_profile"], "extra": "no"}},  # type: ignore[arg-type]
            ),
            ("token contains carriage return", {**_valid_context(), "delivery_gateway_token": "bad\rtoken"}),
            ("token contains newline", {**_valid_context(), "delivery_gateway_token": "bad\ntoken"}),
            (
                "boolean shares",
                {**_valid_context(), "portfolio": [{**_valid_context()["portfolio"][0], "shares": True}]},  # type: ignore[index]
            ),
            (
                "boolean average cost",
                {**_valid_context(), "portfolio": [{**_valid_context()["portfolio"][0], "avgCost": False}]},  # type: ignore[index]
            ),
            (
                "non finite shares",
                {**_valid_context(), "portfolio": [{**_valid_context()["portfolio"][0], "shares": float("inf")}]},  # type: ignore[index]
            ),
            (
                "non finite average cost",
                {**_valid_context(), "portfolio": [{**_valid_context()["portfolio"][0], "avgCost": float("nan")}]},  # type: ignore[index]
            ),
            (
                "invalid ticker",
                {**_valid_context(), "portfolio": [{**_valid_context()["portfolio"][0], "symbol": "aapl"}]},  # type: ignore[index]
            ),
            (
                "unsupported indicator",
                {**_valid_context(), "risk_profile": {**_valid_context()["risk_profile"], "preferredIndicators": ["SMA-20"]}},  # type: ignore[arg-type]
            ),
            (
                "duplicate indicator",
                {**_valid_context(), "risk_profile": {**_valid_context()["risk_profile"], "preferredIndicators": ["MACD", "MACD"]}},  # type: ignore[arg-type]
            ),
            ("excessive holdings", {**_valid_context(), "portfolio": [_valid_context()["portfolio"][0]] * 51}),  # type: ignore[index]
            (
                "invalid risk tolerance",
                {**_valid_context(), "risk_profile": {**_valid_context()["risk_profile"], "tolerance": "reckless"}},  # type: ignore[arg-type]
            ),
        ]

        for name, value in cases:
            with self.subTest(name=name), self.assertRaises(NotifySecurityError) as raised:
                parse_signed_context(_raw(value))  # type: ignore[arg-type]
            self.assertEqual(raised.exception.code, "invalid_context")

    def test_enforces_documented_context_limits(self) -> None:
        valid = _valid_context()
        cases: list[tuple[str, dict[str, object]]] = [
            ("oversized context", {"delivery_gateway_token": "x" * 65_537}),
            ("oversized URL", {"delivery_gateway_url": "x" * 2_049}),
            ("empty token", {"delivery_gateway_token": ""}),
            ("oversized token", {"delivery_gateway_token": "x" * 2_049}),
            (
                "zero shares",
                {**valid, "portfolio": [{**valid["portfolio"][0], "shares": 0}]},  # type: ignore[index]
            ),
            (
                "too many shares",
                {**valid, "portfolio": [{**valid["portfolio"][0], "shares": 1e12 + 1}]},  # type: ignore[index]
            ),
            (
                "negative average cost",
                {**valid, "portfolio": [{**valid["portfolio"][0], "avgCost": -1}]},  # type: ignore[index]
            ),
            (
                "too high average cost",
                {**valid, "portfolio": [{**valid["portfolio"][0], "avgCost": 1e12 + 1}]},  # type: ignore[index]
            ),
            (
                "invalid currency",
                {**valid, "portfolio": [{**valid["portfolio"][0], "currency": "US"}]},  # type: ignore[index]
            ),
            (
                "invalid horizon",
                {**valid, "risk_profile": {**valid["risk_profile"], "horizonMonths": 0}},  # type: ignore[arg-type]
            ),
            (
                "too long horizon",
                {**valid, "risk_profile": {**valid["risk_profile"], "horizonMonths": 601}},  # type: ignore[arg-type]
            ),
        ]

        for name, value in cases:
            with self.subTest(name=name), self.assertRaises(NotifySecurityError) as raised:
                parse_signed_context(_raw(value))
            self.assertEqual(raised.exception.code, "invalid_context")

    def test_accepts_documented_schema_boundaries(self) -> None:
        valid = _valid_context()
        valid["delivery_gateway_url"] = "u" * 2_048
        valid["delivery_gateway_token"] = "t" * 2_048
        valid["portfolio"] = [
            {
                "symbol": "A123456789",
                "shares": 1_000_000_000_000,
                "avgCost": 1_000_000_000_000,
                "currency": "ABCDEFGH",
            }
            for _ in range(50)
        ]
        valid["risk_profile"] = {
            "tolerance": "moderate",
            "horizonMonths": 1,
            "preferredIndicators": ["RSI-14"],
        }

        context = parse_signed_context(_raw(valid))

        self.assertEqual(len(context.gateway_url), 2_048)
        self.assertEqual(len(context.gateway_token), 2_048)
        self.assertEqual(len(context.portfolio), 50)
        self.assertEqual(context.portfolio[0].symbol, "A123456789")
        self.assertEqual(context.portfolio[0].shares, 1_000_000_000_000)
        self.assertEqual(context.portfolio[0].avg_cost, 1_000_000_000_000)
        self.assertEqual(context.portfolio[0].currency, "ABCDEFGH")
        self.assertEqual(context.risk_profile.horizon_months, 1)

        valid["portfolio"] = [
            {
                "symbol": "A",
                "shares": 1,
                "avgCost": 0,
                "currency": "USD",
            }
        ]
        self.assertEqual(parse_signed_context(_raw(valid)).portfolio[0].symbol, "A")

        valid["risk_profile"] = {
            "tolerance": "moderate",
            "horizonMonths": 600,
            "preferredIndicators": ["RSI-14"],
        }
        self.assertEqual(parse_signed_context(_raw(valid)).risk_profile.horizon_months, 600)

    def test_enforces_exact_signed_context_byte_limit(self) -> None:
        raw = _raw(_valid_context())
        at_limit = raw + " " * (65_536 - len(raw.encode("utf-8")))
        over_limit = at_limit + " "

        self.assertEqual(len(at_limit.encode("utf-8")), 65_536)
        self.assertEqual(parse_signed_context(at_limit).digest, hashlib.sha256(at_limit.encode()).hexdigest())
        with self.assertRaises(NotifySecurityError) as raised:
            parse_signed_context(over_limit)
        self.assertEqual(raised.exception.code, "invalid_context")

    def test_rejects_huge_json_integer_without_leaking_an_overflow_error(self) -> None:
        raw = _raw(_valid_context()).replace('"shares":10', '"shares":1' + '0' * 4_000)

        with self.assertRaises(NotifySecurityError) as raised:
            parse_signed_context(raw)

        self.assertEqual(raised.exception.code, "invalid_context")

    def test_rejects_remaining_schema_boundaries_outside_the_allowlist(self) -> None:
        valid = _valid_context()
        cases = [
            ("symbol is too long", {**valid, "portfolio": [{**valid["portfolio"][0], "symbol": "A1234567890"}]}),  # type: ignore[index]
            ("currency is too long", {**valid, "portfolio": [{**valid["portfolio"][0], "currency": "ABCDEFGHI"}]}),  # type: ignore[index]
            ("boolean horizon", {**valid, "risk_profile": {**valid["risk_profile"], "horizonMonths": True}}),  # type: ignore[arg-type]
        ]

        for name, value in cases:
            with self.subTest(name=name), self.assertRaises(NotifySecurityError) as raised:
                parse_signed_context(_raw(value))
            self.assertEqual(raised.exception.code, "invalid_context")


TEST_NOW = 1_800_000_000


def _auth_vector() -> dict[str, object]:
    with (Path(__file__).parent / "fixtures" / "notify_auth_vector.json").open(encoding="utf-8") as vector_file:
        return json.load(vector_file)


class AuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vector = _auth_vector()
        self.account = Account.from_key(self.vector["test_key"])
        self.raw = self.vector["context"]
        self.job_id = int(self.vector["job_id"])
        self.chain_id = self.vector["domain"]["chainId"]
        self.contract = self.vector["domain"]["verifyingContract"]

    def _authorization(
        self,
        *,
        context: str | None = None,
        expires_at: int = TEST_NOW + 300,
        nonce: str | None = None,
        key: str | None = None,
    ) -> dict[str, object]:
        context = self.raw if context is None else context
        nonce = self.vector["nonce"] if nonce is None else nonce
        key = self.vector["test_key"] if key is None else key
        typed = build_notify_typed_data(
            job_id=self.job_id,
            context=context,
            expires_at=expires_at,
            nonce=nonce,
            chain_id=self.chain_id,
            verifying_contract=self.contract,
        )
        signature = Account.sign_message(encode_typed_data(full_message=typed), key).signature.hex()
        return {
            "context": context,
            "expires_at": expires_at,
            "nonce": nonce,
            "signature": signature,
        }

    def _verify(
        self,
        authorization: object,
        *,
        expected_client: str | None = None,
        chain_id: int | None = None,
        verifying_contract: str | None = None,
        now: int = TEST_NOW,
    ) -> object:
        return verify_notify_authorization(
            authorization,
            job_id=self.job_id,
            expected_client=self.account.address if expected_client is None else expected_client,
            chain_id=self.chain_id if chain_id is None else chain_id,
            verifying_contract=self.contract if verifying_contract is None else verifying_contract,
            now=now,
        )

    def test_recovers_the_expected_client_for_a_valid_authorization(self) -> None:
        result = self._verify(
            {
                "context": self.vector["context"],
                "expires_at": self.vector["expires_at"],
                "nonce": self.vector["nonce"],
                "signature": self.vector["signature"],
            }
        )

        self.assertEqual(result.digest, hashlib.sha256(self.raw.encode()).hexdigest())
        self.assertEqual(self.account.address, self.vector["expected_address"])

    def test_rejects_an_authorization_signed_by_a_different_wallet(self) -> None:
        with self.assertRaises(NotifySecurityError) as raised:
            self._verify(self._authorization(key="0x" + "22" * 32))

        self.assertEqual(raised.exception.code, "caller_not_job_client")

    def test_rejects_expired_authorization_after_the_clock_skew_window(self) -> None:
        with self.assertRaises(NotifySecurityError) as raised:
            self._verify(self._authorization(expires_at=TEST_NOW - 31))

        self.assertEqual(raised.exception.code, "authorization_expired")

    def test_rejects_an_expiry_more_than_ten_minutes_in_the_future(self) -> None:
        with self.assertRaises(NotifySecurityError) as raised:
            self._verify(self._authorization(expires_at=TEST_NOW + 601))

        self.assertEqual(raised.exception.code, "invalid_authorization")

    def test_rejects_malformed_nonce_and_signature(self) -> None:
        authorization = self._authorization()
        authorization["nonce"] = "0x11"
        with self.assertRaises(NotifySecurityError) as nonce_raised:
            self._verify(authorization)
        self.assertEqual(nonce_raised.exception.code, "invalid_authorization")

        authorization = self._authorization()
        authorization["signature"] = "0x11"
        with self.assertRaises(NotifySecurityError) as signature_raised:
            self._verify(authorization)
        self.assertEqual(signature_raised.exception.code, "invalid_authorization")

    def test_rejects_a_mutated_signed_context(self) -> None:
        authorization = self._authorization()
        authorization["context"] = self.raw.replace("relay-token", "relay-token-mutated")

        with self.assertRaises(NotifySecurityError) as raised:
            self._verify(authorization)

        self.assertEqual(raised.exception.code, "caller_not_job_client")

    def test_rejects_a_server_owned_domain_mutation(self) -> None:
        with self.assertRaises(NotifySecurityError) as raised:
            self._verify(self._authorization(), chain_id=56)

        self.assertEqual(raised.exception.code, "caller_not_job_client")

    def test_requires_an_authorization_envelope(self) -> None:
        with self.assertRaises(NotifySecurityError) as raised:
            self._verify(None)

        self.assertEqual(raised.exception.code, "authorization_required")

    def test_build_rejects_a_non_string_nonce_without_leaking_type_error(self) -> None:
        with self.assertRaises(NotifySecurityError) as raised:
            build_notify_typed_data(
                job_id=self.job_id,
                context=self.raw,
                expires_at=TEST_NOW + 300,
                nonce=object(),  # type: ignore[arg-type]
                chain_id=self.chain_id,
                verifying_contract=self.contract,
            )

        self.assertEqual(raised.exception.code, "invalid_authorization")


class GatewayDnsTimeoutTests(unittest.TestCase):
    """The default system resolver must fail closed under a hard deadline."""

    def test_slow_system_resolver_is_bounded_and_rejected(self) -> None:
        import threading
        import time

        release = threading.Event()
        started = threading.Event()

        def blocking_getaddrinfo(*args, **kwargs):
            del args, kwargs
            started.set()
            release.wait(timeout=5)
            return []

        try:
            with (
                patch.dict(os.environ, {"GATEWAY_DNS_TIMEOUT_SECONDS": "0.1"}),
                patch("stockanalyst.app.agent.notify_security.socket.getaddrinfo", blocking_getaddrinfo),
            ):
                began = time.monotonic()
                with self.assertRaises(NotifySecurityError) as raised:
                    # No custom resolver → exercises the bounded system-resolver path.
                    validate_gateway_url("https://buyer.trycloudflare.com")
                elapsed = time.monotonic() - began
            self.assertTrue(started.wait(timeout=1))
            self.assertEqual(raised.exception.code, "invalid_gateway_url")
            self.assertLess(elapsed, 2.0)
        finally:
            release.set()

    def test_custom_resolver_bypasses_the_deadline_thread(self) -> None:
        # A supplied resolver (as the seller core passes in production tests) is
        # invoked directly, so validation still succeeds for a public answer.
        origin = validate_gateway_url(
            "https://buyer.trycloudflare.com",
            resolver=PUBLIC_RESOLVER,
        )
        self.assertEqual(origin, "https://buyer.trycloudflare.com")


if __name__ == "__main__":
    unittest.main()
