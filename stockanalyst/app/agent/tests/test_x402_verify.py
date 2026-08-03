from __future__ import annotations

import base64
import json
import unittest

from eth_account import Account

from stockanalyst.app.agent import x402_verify as verify


NOW = 1_785_340_800
RESOURCE_URL = "https://api.example.test/testnet/x402/analyze/async"
SUPPORTED_EXTRA = {
    "name": "U",
    "version": "1",
    "assetTransferMethod": "eip3009",
    "signerAddress": "0x1111111111111111111111111111111111111111",
}


def signed_proof(
    value: object = str(verify.PRICE_WEI),
    *,
    valid_before: int = NOW + 600,
    accepted_overrides: dict[str, object] | None = None,
) -> str:
    account = Account.from_key("0x" + "11" * 32)
    nonce = bytes.fromhex("22" * 32)
    authorization = {
        "from": account.address.lower(),
        "to": verify.SELLER_WALLET.lower(),
        "value": value,
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
    )
    signature = Account._sign_hash(
        digest, private_key="0x" + "11" * 32
    ).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature
    accepted = verify.build_payment_requirement(SUPPORTED_EXTRA)
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


class VerifiedPaymentTests(unittest.TestCase):
    def setUp(self) -> None:
        verify._used_nonces.clear()

    def test_seller_wallet_comes_from_studio_wallet_config(self) -> None:
        active_seller = "0xd10BdDC20E4DC42A1a19a9653e994991e25b8153"

        resolved = verify._resolve_seller_wallet(
            {},
            lambda: {"wallet": {"address": active_seller}},
        )

        self.assertEqual(resolved, active_seller)

    def test_uses_b402_supported_six_decimal_u_token(self) -> None:
        self.assertEqual(
            verify.U_TOKEN_BSC_TESTNET,
            "0x330949Aed7d00FCe0558C64ED6FeC9792616cC39",
        )
        self.assertEqual(verify.PRICE_WEI, 1_000_000)

    def test_invalid_seller_wallet_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "seller wallet"):
            verify._resolve_seller_wallet(
                {},
                lambda: {"wallet": {"address": "not-an-address"}},
            )

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
            ("network", "eip155:56"),
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
                SUPPORTED_EXTRA
            ),
            now=NOW,
        )

        self.assertEqual(reason, "")
        assert payment is not None
        self.assertEqual(payment.proof["accepted"]["extra"], SUPPORTED_EXTRA)
        self.assertNotIn("scheme", payment.proof)

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
            SUPPORTED_EXTRA,
        )

        accept = challenge["accepts"][0]
        self.assertEqual(accept["amount"], str(verify.PRICE_WEI))
        self.assertNotIn("maxAmountRequired", accept)
        self.assertEqual(accept["extra"], SUPPORTED_EXTRA)
        self.assertEqual(challenge["resource"]["url"], RESOURCE_URL)
        self.assertEqual(challenge["resource"]["mimeType"], "application/json")

    def test_payment_requirement_defensively_copies_extra(self) -> None:
        extra = dict(SUPPORTED_EXTRA)

        requirement = verify.build_payment_requirement(extra)
        extra["signerAddress"] = "changed"

        self.assertEqual(requirement["extra"], SUPPORTED_EXTRA)
        self.assertEqual(requirement["amount"], str(verify.PRICE_WEI))

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
