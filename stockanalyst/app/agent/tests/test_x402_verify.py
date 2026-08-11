from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import patch

import eth_abi
from eth_account import Account
from eth_utils import keccak, to_checksum_address
from stockanalyst.app.agent import x402_verify as verify
from stockanalyst.app.agent.x402_tokens import (
    U_TOKEN,
    USD1_TOKEN,
    PaymentToken,
)

NOW = 1_785_340_800
RESOURCE_URL = "https://api.example.test/testnet/x402/analyze/async"
MAINNET_U = "0xcE24439F2D9C6a2289F741120FE202248B666666"
B402_PAY_TO = "0x15958aad30b758dAbfbB9788Da69dfcd56e89078"
LEGACY_SELLER = "0xd10BdDC20E4DC42A1a19a9653e994991e25b8153"
PAID_PRICE_WEI = 210_000_000_000_000_000
U_EXTRA = {
    "name": U_TOKEN.domain_name,
    "version": "1",
    "assetTransferMethod": "eip3009",
    "signerAddress": "0x1111111111111111111111111111111111111111",
}
USD1_EXTRA = {
    "name": USD1_TOKEN.domain_name,
    "version": "1",
    "assetTransferMethod": "eip3009",
    "signerAddress": "0x1111111111111111111111111111111111111111",
}
SUPPORTED_EXTRA = U_EXTRA


class B402PayToConfigurationTests(unittest.TestCase):
    def test_dedicated_address_has_highest_precedence(self) -> None:
        resolved = verify._resolve_b402_pay_to_address(
            {
                "B402_PAY_TO_ADDRESS": B402_PAY_TO,
                "X402_SELLER_WALLET": LEGACY_SELLER,
            },
            lambda: {"wallet": {"address": "0x" + "33" * 20}},
        )
        self.assertEqual(resolved, B402_PAY_TO)

    def test_legacy_and_studio_fallbacks_remain_supported(self) -> None:
        self.assertEqual(
            verify._resolve_b402_pay_to_address(
                {"X402_SELLER_WALLET": LEGACY_SELLER},
                lambda: {"wallet": {"address": "0x" + "33" * 20}},
            ),
            LEGACY_SELLER,
        )
        studio = "0x" + "33" * 20
        self.assertEqual(
            verify._resolve_b402_pay_to_address(
                {}, lambda: {"wallet": {"address": studio}}
            ),
            studio,
        )

    def test_invalid_explicit_values_fail_without_value_leakage(self) -> None:
        for key in ("B402_PAY_TO_ADDRESS", "X402_SELLER_WALLET"):
            with self.subTest(key=key):
                invalid = "not-a-private-value"
                with self.assertRaises(RuntimeError) as raised:
                    verify._resolve_b402_pay_to_address({key: invalid})
                self.assertIn(key, str(raised.exception))
                self.assertNotIn(invalid, str(raised.exception))

    def test_paid_and_free_requirements_use_dedicated_address(self) -> None:
        with patch.object(verify, "B402_PAY_TO_ADDRESS", B402_PAY_TO):
            paid = verify.build_payment_requirement(U_TOKEN, SUPPORTED_EXTRA)
            free = verify.build_free_payment_challenge("AAPL")
        expected = B402_PAY_TO.lower()
        self.assertEqual(paid["payTo"], expected)
        self.assertEqual(free["accepts"][0]["payTo"], expected)


class NetworkConfigurationTests(unittest.TestCase):
    def test_defaults_to_bsc_mainnet(self) -> None:
        self.assertEqual(verify._resolve_x402_chain_id({}), 56)
        self.assertEqual(verify.CHAIN_ID, 56)
        self.assertEqual(
            verify.U_TOKEN_ADDRESS,
            MAINNET_U,
        )

    def test_accepts_only_canonical_mainnet_chain_id(self) -> None:
        self.assertEqual(
            verify._resolve_x402_chain_id({"X402_CHAIN_ID": "56"}),
            56,
        )

    def test_rejects_non_mainnet_chain_id(self) -> None:
        for value in (
            "",
            "0",
            "-1",
            "56.0",
            "056",
            " 56",
            "56 ",
            "97",
            "1",
            "bsc-mainnet-secret",
        ):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError) as raised:
                    verify._resolve_x402_chain_id({"X402_CHAIN_ID": value})
                self.assertIn("X402_CHAIN_ID", str(raised.exception))

    def test_chain_id_rejection_does_not_leak_value(self) -> None:
        invalid = "private-chain-value"
        with self.assertRaises(RuntimeError) as raised:
            verify._resolve_x402_chain_id({"X402_CHAIN_ID": invalid})
        self.assertNotIn(invalid, str(raised.exception))

    def test_rejects_invalid_explicit_token_address(self) -> None:
        for value in ("", "0x1234", "not-an-address", "0x" + "gg" * 20):
            with self.subTest(value=value), self.assertRaisesRegex(
                RuntimeError, "X402_TOKEN_ADDRESS"
            ):
                verify._resolve_x402_token_address({"X402_TOKEN_ADDRESS": value})

    def test_requirement_and_domain_use_the_same_mainnet_config(self) -> None:
        requirement = verify.build_payment_requirement(
            U_TOKEN,
            SUPPORTED_EXTRA,
        )
        domain = verify._domain_separator(U_TOKEN)

        expected_domain = keccak(
            eth_abi.encode(
                ["bytes32", "bytes32", "bytes32", "uint256", "address"],
                [
                    keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                    keccak(text="United Stables"),
                    keccak(text="1"),
                    56,
                    to_checksum_address(MAINNET_U),
                ],
            )
        )
        self.assertEqual(requirement["network"], "eip155:56")
        self.assertEqual(requirement["asset"], MAINNET_U)
        self.assertEqual(domain, expected_domain)


def signed_proof(
    value: object | None = None,
    *,
    token: PaymentToken = U_TOKEN,
    promotional: bool = False,
    valid_before: int = NOW + 600,
    accepted_overrides: dict[str, object] | None = None,
    to_address: str | None = None,
) -> str:
    account = Account.from_key("0x" + "11" * 32)
    nonce = bytes.fromhex("22" * 32)
    authorization_value = (
        str(0 if promotional else PAID_PRICE_WEI)
        if value is None
        else value
    )
    authorization = {
        "from": account.address.lower(),
        "to": (to_address or verify.B402_PAY_TO_ADDRESS).lower(),
        "value": authorization_value,
        "validAfter": "0",
        "validBefore": str(valid_before),
        "nonce": "0x" + nonce.hex(),
    }
    digest = verify._eip712_digest(
        authorization["from"],
        authorization["to"],
        int(authorization["value"]),
        int(authorization["validAfter"]),
        int(authorization["validBefore"]),
        nonce,
        token=token,
    )
    signature = Account._sign_hash(
        digest, private_key="0x" + "11" * 32
    ).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature
    extra = {
        "name": token.domain_name,
        "version": token.domain_version,
        "assetTransferMethod": token.transfer_method,
        "signerAddress": "0x1111111111111111111111111111111111111111",
    }
    accepted = verify.build_payment_requirement(
        token,
        extra,
        amount=0 if promotional else None,
        promotional=promotional,
    )
    accepted.update(accepted_overrides or {})
    proof = {
        "x402Version": 2,
        "resource": {
            "url": RESOURCE_URL,
            "description": "Stock analysis report",
            "mimeType": "application/json",
        },
        "accepted": accepted,
        "payload": {
            "signature": signature,
            "authorization": authorization,
        },
    }
    return base64.b64encode(json.dumps(proof).encode()).decode()


def signed_free_proof(to_address: str) -> str:
    account = Account.from_key("0x" + "11" * 32)
    nonce = bytes.fromhex("66" * 32)
    authorization = {
        "from": account.address.lower(),
        "to": to_address.lower(),
        "value": "0",
        "validAfter": "0",
        "validBefore": str(NOW + 600),
        "nonce": "0x" + nonce.hex(),
    }
    digest = verify._eip712_digest(
        authorization["from"],
        authorization["to"],
        0,
        0,
        NOW + 600,
        nonce,
        token=U_TOKEN,
    )
    signature = Account._sign_hash(
        digest, private_key="0x" + "11" * 32
    ).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature
    return base64.b64encode(json.dumps({
        "x402Version": 2,
        "scheme": "exact",
        "network": f"eip155:{verify.CHAIN_ID}",
        "payload": {
            "signature": signature,
            "authorization": authorization,
        },
    }).encode()).decode()


class VerifiedPaymentTests(unittest.TestCase):
    def setUp(self) -> None:
        verify._used_nonces.clear()

    def test_payment_signature_decoder_is_strict_and_returns_only_objects(
        self,
    ) -> None:
        malformed = {
            "array root": base64.b64encode(b"[]").decode(),
            "scalar root": base64.b64encode(b"1").decode(),
            "bad padding": "e30",
            "whitespace": " e30=",
            "redundant data": "e30===",
            "invalid utf8": base64.b64encode(b"\xff").decode(),
            "non-finite json": base64.b64encode(b'{"value":NaN}').decode(),
        }

        for name, encoded in malformed.items():
            with self.subTest(name=name):
                self.assertIsNone(verify.decode_payment_signature(encoded))

    def test_payment_signature_decoder_enforces_both_length_boundaries(
        self,
    ) -> None:
        prefix = b'{"padding":"'
        suffix = b'"}'
        raw = prefix + b"x" * (
            verify.MAX_PAYMENT_PROOF_BYTES - len(prefix) - len(suffix)
        ) + suffix
        encoded = base64.b64encode(raw).decode()
        self.assertEqual(len(raw), verify.MAX_PAYMENT_PROOF_BYTES)
        self.assertEqual(
            len(encoded),
            verify.MAX_PAYMENT_SIGNATURE_CHARACTERS,
        )
        self.assertIsNotNone(verify.decode_payment_signature(encoded))

        over_limit = base64.b64encode(raw + b" ").decode()
        self.assertGreater(
            len(over_limit),
            verify.MAX_PAYMENT_SIGNATURE_CHARACTERS,
        )
        self.assertIsNone(verify.decode_payment_signature(over_limit))

    def test_paid_and_free_paths_stably_reject_non_object_roots(self) -> None:
        marker = "private-proof-marker"
        for raw in (b"[]", json.dumps(marker).encode()):
            encoded = base64.b64encode(raw).decode()
            payment, reason = verify.validate_payment_proof(encoded, now=NOW)
            free_ok, free_reason, free_address = (
                verify.verify_free_payment_proof(encoded)
            )

            self.assertIsNone(payment)
            self.assertEqual(
                reason,
                "Payment-Signature is not valid base64 JSON",
            )
            self.assertFalse(free_ok)
            self.assertEqual(free_reason, reason)
            self.assertEqual(free_address, "")
            self.assertNotIn(marker, reason)

    def test_free_version_rejection_never_reflects_untrusted_input(
        self,
    ) -> None:
        marker = "free-attacker-version-marker"
        proof = json.loads(base64.b64decode(signed_free_proof(B402_PAY_TO)))
        proof["x402Version"] = marker
        encoded = base64.b64encode(json.dumps(proof).encode()).decode()

        ok, reason, from_address = verify.verify_free_payment_proof(encoded)

        self.assertFalse(ok)
        self.assertEqual(reason, "unsupported x402Version (expected 2)")
        self.assertEqual(from_address, "")
        self.assertNotIn(marker, reason)

    def test_paid_and_free_paths_stably_reject_malformed_nested_values(
        self,
    ) -> None:
        rejection = "Payment-Signature is not valid base64 JSON"

        paid_cases: list[tuple[str, object, object | None]] = [
            ("payload list", [], None),
            ("payload scalar", 1, None),
            ("authorization list", {}, []),
            ("authorization scalar", {}, "authorization"),
        ]
        for name, payload, authorization in paid_cases:
            proof = json.loads(base64.b64decode(signed_proof()))
            if authorization is None:
                proof["payload"] = payload
            else:
                proof["payload"] = {
                    **payload,
                    "authorization": authorization,
                }
            encoded = base64.b64encode(
                json.dumps(proof, separators=(",", ":")).encode()
            ).decode()
            try:
                payment, reason = verify.validate_payment_proof(
                    encoded,
                    now=NOW,
                )
            except Exception as exc:
                self.fail(f"{name} raised {type(exc).__name__}")
            self.assertIsNone(payment, name)
            self.assertEqual(reason, rejection, name)

        free_cases: list[tuple[str, object, object | None]] = [
            ("payload list", [], None),
            ("payload scalar", 1, None),
            ("authorization list", {}, []),
            ("authorization scalar", {}, "authorization"),
        ]
        for name, payload, authorization in free_cases:
            proof = json.loads(base64.b64decode(signed_free_proof(B402_PAY_TO)))
            if authorization is None:
                proof["payload"] = payload
            else:
                proof["payload"] = {
                    **payload,
                    "authorization": authorization,
                }
            encoded = base64.b64encode(
                json.dumps(proof, separators=(",", ":")).encode()
            ).decode()
            try:
                ok, reason, address = verify.verify_free_payment_proof(encoded)
            except Exception as exc:
                self.fail(f"free {name} raised {type(exc).__name__}")
            self.assertFalse(ok, name)
            self.assertEqual(reason, rejection, name)
            self.assertEqual(address, "", name)

        free_numeric = json.loads(
            base64.b64decode(signed_free_proof(B402_PAY_TO))
        )
        free_numeric["payload"]["authorization"]["validAfter"] = []
        encoded_numeric = base64.b64encode(
            json.dumps(free_numeric, separators=(",", ":")).encode()
        ).decode()
        try:
            numeric_result = verify.verify_free_payment_proof(encoded_numeric)
        except Exception as exc:
            self.fail(f"free numeric value raised {type(exc).__name__}")
        self.assertEqual(numeric_result, (False, rejection, ""))

    def test_free_path_stably_rejects_a_thousand_digit_value(self) -> None:
        proof = json.loads(base64.b64decode(signed_free_proof(B402_PAY_TO)))
        proof["payload"]["authorization"]["value"] = "9" * 1_000
        encoded = base64.b64encode(
            json.dumps(proof, separators=(",", ":")).encode()
        ).decode()
        rejection = (
            "free tier requires value=0; "
            "use /x402/analyze/async for paid analysis"
        )

        try:
            with patch.object(verify, "B402_PAY_TO_ADDRESS", B402_PAY_TO):
                result = verify.verify_free_payment_proof(encoded)
        except Exception as exc:
            self.fail(f"thousand-digit value raised {type(exc).__name__}")
        self.assertEqual(
            result,
            (
                False,
                rejection,
                Account.from_key("0x" + "11" * 32).address.lower(),
            ),
        )

    def test_payment_signature_decoder_rejects_recursive_json_stably(
        self,
    ) -> None:
        depth = 2_000
        raw = (
            b'{"payload":'
            + b"[" * depth
            + b"0"
            + b"]" * depth
            + b"}"
        )
        encoded = base64.b64encode(raw).decode()
        self.assertLess(len(raw), verify.MAX_PAYMENT_PROOF_BYTES)

        try:
            decoded = verify.decode_payment_signature(encoded)
        except Exception as exc:
            self.fail(f"recursive JSON raised {type(exc).__name__}")
        self.assertIsNone(decoded)

    def test_uses_mainnet_u_compatibility_aliases(self) -> None:
        self.assertEqual(
            verify.U_TOKEN_ADDRESS,
            U_TOKEN.address,
        )
        self.assertEqual(U_TOKEN.amount, 10**18)
        self.assertEqual(verify.PRICE_WEI, PAID_PRICE_WEI)

    def test_paid_requirements_use_eighteen_decimals_for_both_tokens(
        self,
    ) -> None:
        for token, extra in (
            (U_TOKEN, U_EXTRA),
            (USD1_TOKEN, USD1_EXTRA),
        ):
            with self.subTest(token=token.symbol):
                requirement = verify.build_payment_requirement(token, extra)
                self.assertEqual(requirement["asset"], token.address)
                self.assertEqual(requirement["amount"], str(PAID_PRICE_WEI))

    def test_promotional_requirement_is_zero_and_has_no_b402_signer(
        self,
    ) -> None:
        requirement = verify.build_payment_requirement(
            USD1_TOKEN,
            {
                "name": USD1_TOKEN.domain_name,
                "version": "1",
                "assetTransferMethod": "eip3009",
            },
            amount=0,
            promotional=True,
        )
        self.assertEqual(requirement["amount"], "0")
        self.assertNotIn("signerAddress", requirement["extra"])

    def test_usd1_signature_uses_usd1_verifying_contract(self) -> None:
        payment, reason = verify.validate_payment_proof(
            signed_proof(token=USD1_TOKEN),
            now=NOW,
        )
        self.assertEqual(reason, "")
        assert payment is not None
        self.assertEqual(payment.asset, USD1_TOKEN.address.lower())
        self.assertEqual(payment.token_symbol, "USD1")

    def test_paid_and_promotional_requirements_cannot_cross_modes(self) -> None:
        self.assertIsNone(
            verify.validate_payment_proof(
                signed_proof(token=U_TOKEN, promotional=True),
                now=NOW,
            )[0]
        )
        self.assertIsNone(
            verify.validate_payment_proof(
                signed_proof(token=U_TOKEN),
                now=NOW,
                promotional=True,
            )[0]
        )

    def test_rejects_invalid_token_requirements_and_authorizations(
        self,
    ) -> None:
        u_without_signer = {
            key: value
            for key, value in U_EXTRA.items()
            if key != "signerAddress"
        }
        promotional_with_signer = {
            "name": U_TOKEN.domain_name,
            "version": U_TOKEN.domain_version,
            "assetTransferMethod": U_TOKEN.transfer_method,
            "signerAddress": "0x" + "11" * 20,
        }
        cases = (
            (
                "unknown asset",
                signed_proof(
                    token=U_TOKEN,
                    accepted_overrides={"asset": "0x" + "11" * 20},
                ),
                False,
            ),
            (
                "U asset with USD1 domain",
                signed_proof(
                    token=U_TOKEN,
                    accepted_overrides={
                        "extra": {
                            **U_EXTRA,
                            "name": USD1_TOKEN.domain_name,
                        }
                    },
                ),
                False,
            ),
            (
                "USD1 asset with U domain",
                signed_proof(
                    token=USD1_TOKEN,
                    accepted_overrides={
                        "extra": {
                            **USD1_EXTRA,
                            "name": U_TOKEN.domain_name,
                        }
                    },
                ),
                False,
            ),
            (
                "wrong recipient",
                signed_proof(token=U_TOKEN, to_address="0x" + "44" * 20),
                False,
            ),
            (
                "one wei under exact price",
                signed_proof(token=U_TOKEN, value=PAID_PRICE_WEI - 1),
                False,
            ),
            (
                "one wei over exact price",
                signed_proof(token=USD1_TOKEN, value=PAID_PRICE_WEI + 1),
                False,
            ),
            (
                "old one-token price",
                signed_proof(token=U_TOKEN, value=10**18),
                False,
            ),
            (
                "legacy six-decimal amount",
                signed_proof(token=U_TOKEN, value=1_000_000),
                False,
            ),
            (
                "paid signer missing",
                signed_proof(
                    token=U_TOKEN,
                    accepted_overrides={"extra": u_without_signer},
                ),
                False,
            ),
            (
                "promotional signer present",
                signed_proof(
                    token=U_TOKEN,
                    promotional=True,
                    accepted_overrides={"extra": promotional_with_signer},
                ),
                True,
            ),
        )

        for label, proof, promotional in cases:
            with self.subTest(case=label):
                payment, reason = verify.validate_payment_proof(
                    proof,
                    now=NOW,
                    promotional=promotional,
                )
                self.assertIsNone(payment)
                self.assertTrue(reason)

    def test_pure_validation_does_not_consume_nonce(self) -> None:
        proof = signed_proof()
        first, reason = verify.validate_payment_proof(proof, now=NOW)
        second, second_reason = verify.validate_payment_proof(proof, now=NOW)

        self.assertEqual(reason, "")
        self.assertEqual(second_reason, "")
        self.assertEqual(first, second)
        assert first is not None
        self.assertEqual(
            first.from_address,
            Account.from_key("0x" + "11" * 32).address.lower(),
        )
        self.assertEqual(first.nonce, "0x" + "22" * 32)
        self.assertEqual(first.value, verify.PRICE_WEI)
        self.assertEqual(first.transfer_method, "eip3009")

    def test_payment_signature_decoder_enforces_exact_nesting_boundary(
        self,
    ) -> None:
        at_limit: object = 0
        for _ in range(62):
            at_limit = [at_limit]
        accepted = base64.b64encode(
            json.dumps({"value": at_limit}).encode()
        ).decode()

        over_limit: object = 0
        for _ in range(63):
            over_limit = [over_limit]
        rejected = base64.b64encode(
            json.dumps({"value": over_limit}).encode()
        ).decode()

        self.assertIsNotNone(verify.decode_payment_signature(accepted))
        self.assertIsNone(verify.decode_payment_signature(rejected))

    def test_pure_validation_rejects_json_float_value(self) -> None:
        proof = signed_proof(float(verify.PRICE_WEI))

        payment, reason = verify.validate_payment_proof(proof, now=NOW)

        self.assertIsNone(payment)
        self.assertTrue(reason)

    def test_paid_proof_requires_official_accepted_requirement(self) -> None:
        decoded = json.loads(base64.b64decode(signed_proof()))
        decoded.pop("accepted")
        proof = base64.b64encode(json.dumps(decoded).encode()).decode()

        payment, reason = verify.validate_payment_proof(proof, now=NOW)

        self.assertIsNone(payment)
        self.assertEqual(reason, "payment requirement is missing or invalid")

    def test_paid_proof_rejects_mismatched_requirement_fields(self) -> None:
        mismatches: list[tuple[str, object]] = [
            ("scheme", "upto"),
            ("network", "eip155:97"),
            ("amount", str(verify.PRICE_WEI - 1)),
            ("asset", "0x" + "33" * 20),
            ("payTo", "0x" + "44" * 20),
            ("maxTimeoutSeconds", 1),
            ("extra", {**SUPPORTED_EXTRA, "signerAddress": "0x" + "55" * 20}),
        ]

        for field, value in mismatches:
            with self.subTest(field=field):
                payment, reason = verify.validate_payment_proof(
                    signed_proof(accepted_overrides={field: value}),
                    expected_requirement=verify.build_payment_requirement(
                        U_TOKEN,
                        SUPPORTED_EXTRA
                    ),
                    now=NOW,
                )
                self.assertIsNone(payment)
                self.assertEqual(reason, "payment requirement mismatch")

    def test_paid_proof_exposes_official_payload_for_settlement(self) -> None:
        proof = signed_proof()

        payment, reason = verify.validate_payment_proof(
            proof,
            expected_requirement=verify.build_payment_requirement(
                U_TOKEN,
                SUPPORTED_EXTRA
            ),
            now=NOW,
        )

        self.assertEqual(reason, "")
        assert payment is not None
        self.assertEqual(payment.proof["accepted"]["extra"], SUPPORTED_EXTRA)
        self.assertNotIn("scheme", payment.proof)

    def test_paid_expected_requirement_cannot_override_canonical_amount(
        self,
    ) -> None:
        zero_requirement = verify.build_payment_requirement(
            U_TOKEN,
            U_EXTRA,
            amount=0,
        )

        payment, reason = verify.validate_payment_proof(
            signed_proof(value=0, accepted_overrides={"amount": "0"}),
            expected_requirement=zero_requirement,
            now=NOW,
        )

        self.assertIsNone(payment)
        self.assertEqual(reason, "payment requirement mismatch")

    def test_promotional_expected_requirement_cannot_override_zero_amount(
        self,
    ) -> None:
        promotional_extra = {
            "name": U_TOKEN.domain_name,
            "version": U_TOKEN.domain_version,
            "assetTransferMethod": U_TOKEN.transfer_method,
        }
        nonzero_requirement = verify.build_payment_requirement(
            U_TOKEN,
            promotional_extra,
            amount=U_TOKEN.amount,
            promotional=True,
        )

        payment, reason = verify.validate_payment_proof(
            signed_proof(
                value=U_TOKEN.amount,
                promotional=True,
                accepted_overrides={"amount": str(U_TOKEN.amount)},
            ),
            expected_requirement=nonzero_requirement,
            now=NOW,
            promotional=True,
        )

        self.assertIsNone(payment)
        self.assertEqual(reason, "payment requirement mismatch")

    def test_pure_validation_rejects_infinite_value_without_raising(self) -> None:
        encoded_proof = signed_proof()
        proof = json.loads(base64.b64decode(encoded_proof))
        proof["payload"]["authorization"]["value"] = float("inf")
        encoded_proof = base64.b64encode(json.dumps(proof).encode()).decode()

        payment, reason = verify.validate_payment_proof(encoded_proof, now=NOW)

        self.assertIsNone(payment)
        self.assertTrue(reason)

    def test_expired_proof_can_be_cryptographically_validated_for_recovery(
        self,
    ) -> None:
        proof = signed_proof(valid_before=NOW - 1)

        rejected, reason = verify.validate_payment_proof(proof, now=NOW)
        recovered, recovery_reason = verify.validate_payment_proof(
            proof,
            now=NOW,
            allow_expired=True,
        )

        self.assertIsNone(rejected)
        self.assertEqual(reason, "authorization expired")
        self.assertIsNotNone(recovered)
        self.assertEqual(recovery_reason, "")

    def test_valid_before_is_an_exclusive_boundary(self) -> None:
        proof = signed_proof(valid_before=NOW)

        payment, reason = verify.validate_payment_proof(proof, now=NOW)

        self.assertIsNone(payment)
        self.assertEqual(reason, "authorization expired")

    def test_payment_challenge_uses_official_v2_requirement(self) -> None:
        challenge = verify.build_payment_challenge(
            ["AAPL"],
            RESOURCE_URL,
            [verify.build_payment_requirement(U_TOKEN, SUPPORTED_EXTRA)],
        )

        accept = challenge["accepts"][0]
        self.assertEqual(accept["amount"], str(verify.PRICE_WEI))
        self.assertNotIn("maxAmountRequired", accept)
        self.assertEqual(accept["extra"], SUPPORTED_EXTRA)
        self.assertEqual(challenge["resource"]["url"], RESOURCE_URL)
        self.assertEqual(challenge["resource"]["mimeType"], "application/json")

    def test_payment_requirement_defensively_copies_extra(self) -> None:
        extra = dict(SUPPORTED_EXTRA)

        requirement = verify.build_payment_requirement(U_TOKEN, extra)
        extra["signerAddress"] = "changed"

        self.assertEqual(requirement["extra"], SUPPORTED_EXTRA)
        self.assertEqual(requirement["amount"], str(verify.PRICE_WEI))

    def test_paid_proof_enforces_dedicated_recipient(self) -> None:
        with patch.object(verify, "B402_PAY_TO_ADDRESS", B402_PAY_TO):
            accepted, accepted_reason = verify.validate_payment_proof(
                signed_proof(), now=NOW
            )
            rejected, rejected_reason = verify.validate_payment_proof(
                signed_proof(to_address="0x" + "44" * 20), now=NOW
            )
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted_reason, "")
        self.assertIsNone(rejected)
        self.assertIn("wrong recipient", rejected_reason)

    def test_free_proof_enforces_dedicated_recipient(self) -> None:
        with (
            patch.object(verify, "B402_PAY_TO_ADDRESS", B402_PAY_TO),
            patch.object(verify.time, "time", return_value=NOW),
            self.assertLogs(
                "seller-agent.x402.verify",
                level="INFO",
            ) as captured,
        ):
            accepted, accepted_reason, _ = verify.verify_free_payment_proof(
                signed_free_proof(B402_PAY_TO)
            )
            rejected, rejected_reason, _ = verify.verify_free_payment_proof(
                signed_free_proof("0x" + "44" * 20)
            )
        self.assertTrue(accepted)
        self.assertIn("uses remaining", accepted_reason)
        self.assertFalse(rejected)
        self.assertIn("wrong recipient", rejected_reason)
        rendered = "\n".join(captured.output)
        self.assertNotIn(
            Account.from_key("0x" + "11" * 32).address.lower(),
            rendered,
        )
        self.assertNotIn("0x" + "66" * 32, rendered)

    def test_expired_recovery_still_rejects_an_invalid_signature(self) -> None:
        proof = json.loads(
            base64.b64decode(signed_proof(valid_before=NOW - 1))
        )
        proof["payload"]["signature"] = "0x" + "00" * 65
        encoded = base64.b64encode(json.dumps(proof).encode()).decode()

        payment, reason = verify.validate_payment_proof(
            encoded,
            now=NOW,
            allow_expired=True,
        )

        self.assertIsNone(payment)
        self.assertTrue(reason)
