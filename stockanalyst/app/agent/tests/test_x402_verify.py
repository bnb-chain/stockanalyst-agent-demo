from __future__ import annotations

import base64
import json
import unittest

from eth_account import Account

from stockanalyst.app.agent import x402_verify as verify


NOW = 1_785_340_800


def signed_proof(
    value: object = str(verify.PRICE_WEI),
    *,
    valid_before: int = NOW + 600,
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
    proof = {
        "x402Version": 2,
        "scheme": "exact",
        "network": f"eip155:{verify.CHAIN_ID}",
        "payload": {
            "signature": signature,
            "authorization": authorization,
        },
    }
    return base64.b64encode(json.dumps(proof).encode()).decode()


class VerifiedPaymentTests(unittest.TestCase):
    def setUp(self) -> None:
        verify._used_nonces.clear()

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

    def test_payment_challenge_targets_async_route(self) -> None:
        challenge = verify.build_payment_challenge(
            ["AAPL"],
            "agent.example",
        )

        self.assertEqual(
            challenge["resource"],
            "http://agent.example/x402/analyze/async",
        )

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
