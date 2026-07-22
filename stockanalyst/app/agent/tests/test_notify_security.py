"""Tests for strict, immutable signed notification contexts."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
import unittest

from eth_account import Account
from eth_account.messages import encode_typed_data

from stockanalyst.app.agent.notify_security import (
    NotifySecurityError,
    build_notify_typed_data,
    parse_signed_context,
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

if __name__ == "__main__":
    unittest.main()
