from __future__ import annotations

import base64
import hashlib
import json
import unittest

from eth_account import Account
from eth_account.messages import encode_typed_data

from promo_wallet_authorization import (
    PromoWalletAuthorizationError,
    promo_wallet_metadata,
    verify_promo_wallet_authorization,
)

NOW = 1_780_000_000
BODY = b'{"symbols":["AAPL"]}'
NONCE = "0x" + "12" * 32


def _typed_data(address: str, *, body: bytes = BODY, expires_at: int = NOW + 600):
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "PromoAuthorization": [
                {"name": "address", "type": "address"},
                {"name": "method", "type": "string"},
                {"name": "path", "type": "string"},
                {"name": "bodyHash", "type": "bytes32"},
                {"name": "nonce", "type": "bytes32"},
                {"name": "expiresAt", "type": "uint64"},
            ],
        },
        "primaryType": "PromoAuthorization",
        "domain": {
            "name": "Stock Analyst Promo",
            "version": "1",
            "chainId": 56,
        },
        "message": {
            "address": address,
            "method": "POST",
            "path": "/x402/analyze/async",
            "bodyHash": "0x" + hashlib.sha256(body).hexdigest(),
            "nonce": NONCE,
            "expiresAt": expires_at,
        },
    }


def _encode(value: object) -> str:
    raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _signed_header(*, body: bytes = BODY, expires_at: int = NOW + 600) -> tuple[str, str]:
    account = Account.create("promo-wallet-test")
    signature = Account.sign_message(
        encode_typed_data(
            full_message=_typed_data(
                account.address,
                body=body,
                expires_at=expires_at,
            )
        ),
        account.key,
    ).signature.hex()
    return _encode({
        "version": 1,
        "address": account.address,
        "nonce": NONCE,
        "expiresAt": expires_at,
        "signature": "0x" + signature.removeprefix("0x"),
    }), account.address.lower()


class PromoWalletAuthorizationTests(unittest.TestCase):
    def test_verifies_real_eip712_signature_bound_to_exact_body(self) -> None:
        header, address = _signed_header()

        authorization = verify_promo_wallet_authorization(
            header,
            BODY,
            now=NOW,
        )

        self.assertEqual(authorization.address, address)
        self.assertEqual(authorization.nonce, NONCE)
        self.assertEqual(authorization.expires_at, NOW + 600)
        self.assertEqual(
            authorization.request_digest,
            hashlib.sha256(BODY).hexdigest(),
        )

    def test_rejects_signature_for_different_body_with_fixed_error(self) -> None:
        header, _address = _signed_header()

        with self.assertRaisesRegex(
            PromoWalletAuthorizationError,
            "^wallet_signature_invalid$",
        ):
            verify_promo_wallet_authorization(
                header,
                b'{"symbols":["MSFT"]}',
                now=NOW,
            )

    def test_rejects_expired_and_overlong_authorizations(self) -> None:
        for expires_at in (NOW, NOW + 601):
            header, _address = _signed_header(expires_at=expires_at)
            with self.subTest(expires_at=expires_at), self.assertRaisesRegex(
                PromoWalletAuthorizationError,
                "^wallet_signature_invalid$",
            ):
                verify_promo_wallet_authorization(header, BODY, now=NOW)

    def test_expired_authorization_is_only_decoded_for_durable_recovery(self) -> None:
        header, address = _signed_header(expires_at=NOW - 1)

        authorization = verify_promo_wallet_authorization(
            header,
            BODY,
            now=NOW,
            allow_expired=True,
        )

        self.assertEqual(authorization.address, address)
        self.assertEqual(authorization.expires_at, NOW - 1)

        too_old, _address = _signed_header(expires_at=NOW - 7 * 24 * 60 * 60)
        with self.assertRaisesRegex(
            PromoWalletAuthorizationError,
            "^wallet_signature_invalid$",
        ):
            verify_promo_wallet_authorization(
                too_old,
                BODY,
                now=NOW,
                allow_expired=True,
            )

    def test_rejects_noncanonical_or_malformed_envelopes(self) -> None:
        header, _address = _signed_header()
        decoded = json.loads(
            base64.urlsafe_b64decode(header + "=" * (-len(header) % 4))
        )
        cases = [
            "",
            "not-base64url!",
            header + "=",
            _encode([decoded]),
            _encode({**decoded, "extra": True}),
            _encode({**decoded, "version": True}),
            _encode({**decoded, "nonce": "0x" + "AB" * 32}),
            _encode({**decoded, "expiresAt": True}),
            _encode({**decoded, "signature": "0x12"}),
            "a" * 4097,
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate[:30]), self.assertRaisesRegex(
                PromoWalletAuthorizationError,
                "^wallet_signature_invalid$",
            ):
                verify_promo_wallet_authorization(candidate, BODY, now=NOW)

    def test_metadata_freezes_public_wallet_contract(self) -> None:
        metadata = promo_wallet_metadata()

        self.assertEqual(metadata["scheme"], "eip712-wallet")
        self.assertEqual(metadata["network"], "eip155:56")
        self.assertEqual(metadata["header"], "Wallet-Signature")
        self.assertEqual(metadata["maxTimeoutSeconds"], 600)
        self.assertEqual(metadata["domain"], {
            "name": "Stock Analyst Promo",
            "version": "1",
            "chainId": 56,
        })
        self.assertEqual(metadata["primaryType"], "PromoAuthorization")


if __name__ == "__main__":
    unittest.main()
