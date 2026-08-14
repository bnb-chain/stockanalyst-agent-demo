from __future__ import annotations

import base64
import copy
import json
from collections.abc import Callable
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from stockanalyst.app.agent import x402_verify as verify
from stockanalyst.app.agent.x402_permit2 import (
    UINT256_MAX,
    canonical_uint256,
    verify_permit2_exact,
)
from stockanalyst.app.agent.x402_tokens import USDC_TOKEN, USDT_TOKEN

NOW = 1_785_484_800
PRIVATE_KEY = "0x" + "31" * 32
OTHER_PRIVATE_KEY = "0x" + "32" * 32
PAYER = Account.from_key(PRIVATE_KEY).address.lower()
OTHER_PAYER = Account.from_key(OTHER_PRIVATE_KEY).address.lower()
SPENDER = "0x4444444444444444444444444444444444444444"
OTHER_ADDRESS = "0x9999999999999999999999999999999999999999"
PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
SAFE_PROOF_REJECTION = "Permit2 payment proof is invalid"


DOMAIN_FIELDS = [
    {"name": "name", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]
PERMISSION_FIELDS = [
    {"name": "token", "type": "address"},
    {"name": "amount", "type": "uint256"},
]
WITNESS_FIELDS = [
    {"name": "to", "type": "address"},
    {"name": "validAfter", "type": "uint256"},
]
PERMIT_FIELDS = [
    {"name": "permitted", "type": "TokenPermissions"},
    {"name": "spender", "type": "address"},
    {"name": "nonce", "type": "uint256"},
    {"name": "deadline", "type": "uint256"},
    {"name": "witness", "type": "Witness"},
]


def _typed_data(
    authorization: dict[str, Any],
    *,
    domain: dict[str, Any] | None = None,
    types: dict[str, list[dict[str, str]]] | None = None,
    primary_type: str = "PermitWitnessTransferFrom",
) -> dict[str, Any]:
    return {
        "types": copy.deepcopy(types) if types is not None else {
            "EIP712Domain": copy.deepcopy(DOMAIN_FIELDS),
            "TokenPermissions": copy.deepcopy(PERMISSION_FIELDS),
            "Witness": copy.deepcopy(WITNESS_FIELDS),
            "PermitWitnessTransferFrom": copy.deepcopy(PERMIT_FIELDS),
        },
        "primaryType": primary_type,
        "domain": copy.deepcopy(domain) if domain is not None else {
            "name": "Permit2",
            "chainId": 56,
            "verifyingContract": PERMIT2_ADDRESS,
        },
        "message": {
            key: copy.deepcopy(value)
            for key, value in authorization.items()
            if key != "from"
        },
    }


def permit2_proof(
    token=USDC_TOKEN,
    *,
    nonce: object = "12345678901234567890",
    deadline: object = str(NOW + 600),
    valid_after: object = str(NOW - 60),
    signing_key: str = PRIVATE_KEY,
    signed_domain: dict[str, Any] | None = None,
    signed_types: dict[str, list[dict[str, str]]] | None = None,
    signed_primary_type: str = "PermitWitnessTransferFrom",
    extra_fields: dict[str, Any] | None = None,
    accepted_spender: str = SPENDER,
    authorization_spender: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    extra = {
        "name": token.domain_name,
        "version": token.domain_version,
        "assetTransferMethod": "permit2-exact",
        "signerAddress": "0x3333333333333333333333333333333333333333",
        "spenderAddress": accepted_spender,
    }
    if extra_fields is not None:
        extra.update(copy.deepcopy(extra_fields))
    accepted = verify.build_payment_requirement(token, extra)
    authorization = {
        "permitted": {
            "token": token.address,
            "amount": accepted["amount"],
        },
        "from": PAYER,
        "spender": authorization_spender or accepted_spender,
        "nonce": nonce,
        "deadline": deadline,
        "witness": {
            "to": accepted["payTo"],
            "validAfter": valid_after,
        },
    }
    signable = encode_typed_data(
        full_message=_typed_data(
            authorization,
            domain=signed_domain,
            types=signed_types,
            primary_type=signed_primary_type,
        )
    )
    signature = Account.sign_message(
        signable,
        private_key=signing_key,
    ).signature
    proof = {
        "x402Version": 2,
        "resource": {
            "url": "https://api.example.test/x402/analyze/async",
            "description": "Stock analysis report",
            "mimeType": "application/json",
        },
        "accepted": accepted,
        "payload": {
            "signature": "0x" + bytes(signature).hex(),
            "permit2Authorization": authorization,
        },
    }
    return proof, accepted


def encoded_proof(proof: dict[str, Any]) -> str:
    return base64.b64encode(
        json.dumps(proof, separators=(",", ":")).encode()
    ).decode()


@pytest.mark.parametrize("token", [USDC_TOKEN, USDT_TOKEN])
def test_real_permit2_signatures_recover_both_registered_payers(token) -> None:
    proof, expected = permit2_proof(token)

    payment, reason = verify.validate_payment_proof(
        encoded_proof(proof),
        expected_requirement=expected,
        now=NOW,
    )

    assert reason == ""
    assert payment is not None
    assert payment.from_address == PAYER
    assert payment.to_address == verify.B402_PAY_TO_ADDRESS.lower()
    assert payment.asset == token.address.lower()
    assert payment.token_symbol == token.symbol
    assert payment.transfer_method == "permit2-exact"
    assert payment.value == verify.PRICE_WEI
    assert payment.nonce == "12345678901234567890"
    assert payment.nonce_bytes == (12345678901234567890).to_bytes(32, "big")
    assert payment.valid_after == NOW - 60
    assert payment.valid_before == NOW + 600
    assert not hasattr(payment, "promotional")


def test_nested_additive_extra_survives_challenge_proof_and_verification() -> None:
    additive = {
        "futureFlag": {
            "enabled": True,
            "metadata": {
                "revision": 3,
                "modes": ["exact", {"batch": False}],
            },
        },
    }
    proof, requirement = permit2_proof(extra_fields=additive)
    challenge = verify.build_payment_challenge(
        [],
        "https://api.example.test/x402/analyze/async",
        [requirement],
    )
    accepted = challenge["accepts"][0]
    proof["accepted"] = copy.deepcopy(accepted)

    payment, reason = verify.validate_payment_proof(
        encoded_proof(proof),
        expected_requirement=accepted,
        now=NOW,
    )

    assert reason == ""
    assert payment is not None
    assert accepted["extra"] == {
        "name": USDC_TOKEN.domain_name,
        "version": USDC_TOKEN.domain_version,
        "assetTransferMethod": "permit2-exact",
        "signerAddress": "0x3333333333333333333333333333333333333333",
        "spenderAddress": SPENDER,
        **additive,
    }
    assert payment.proof["accepted"] == accepted


def test_canonical_spender_authorization_preserves_mixed_case_accepted_extra(
) -> None:
    mixed_case_spender = "0x" + "Cd" * 20
    canonical_spender = mixed_case_spender.lower()
    proof, expected = permit2_proof(
        accepted_spender=mixed_case_spender,
        authorization_spender=canonical_spender,
    )

    payment, reason = verify.validate_payment_proof(
        encoded_proof(proof),
        expected_requirement=expected,
        now=NOW,
    )

    assert reason == ""
    assert payment is not None
    assert proof["accepted"]["extra"]["spenderAddress"] == mixed_case_spender
    assert (
        proof["payload"]["permit2Authorization"]["spender"]
        == canonical_spender
    )
    assert payment.proof["accepted"] == expected


def _mutate(path: tuple[str, ...], value: object) -> Callable[[dict[str, Any]], None]:
    def apply(proof: dict[str, Any]) -> None:
        current: dict[str, Any] = proof
        for key in path[:-1]:
            current = current[key]
        current[path[-1]] = value
    return apply


@pytest.mark.parametrize(
    ("label", "mutator", "expected_reason"),
    [
        (
            "asset",
            _mutate(("accepted", "asset"), USDT_TOKEN.address),
            "payment requirement mismatch",
        ),
        (
            "accepted amount",
            _mutate(("accepted", "amount"), str(verify.PRICE_WEI - 1)),
            "payment requirement mismatch",
        ),
        (
            "permitted token",
            _mutate(
                ("payload", "permit2Authorization", "permitted", "token"),
                OTHER_ADDRESS,
            ),
            SAFE_PROOF_REJECTION,
        ),
        (
            "permitted amount",
            _mutate(
                ("payload", "permit2Authorization", "permitted", "amount"),
                str(verify.PRICE_WEI - 1),
            ),
            SAFE_PROOF_REJECTION,
        ),
        (
            "payer",
            _mutate(
                ("payload", "permit2Authorization", "from"),
                OTHER_PAYER,
            ),
            SAFE_PROOF_REJECTION,
        ),
        (
            "spender",
            _mutate(
                ("payload", "permit2Authorization", "spender"),
                OTHER_ADDRESS,
            ),
            SAFE_PROOF_REJECTION,
        ),
        (
            "payTo",
            _mutate(("accepted", "payTo"), OTHER_ADDRESS),
            "payment requirement mismatch",
        ),
        (
            "witness recipient",
            _mutate(
                ("payload", "permit2Authorization", "witness", "to"),
                OTHER_ADDRESS,
            ),
            SAFE_PROOF_REJECTION,
        ),
        (
            "nonce",
            _mutate(
                ("payload", "permit2Authorization", "nonce"),
                "12345678901234567891",
            ),
            SAFE_PROOF_REJECTION,
        ),
        (
            "deadline",
            _mutate(
                ("payload", "permit2Authorization", "deadline"),
                str(NOW + 599),
            ),
            SAFE_PROOF_REJECTION,
        ),
        (
            "validAfter",
            _mutate(
                (
                    "payload",
                    "permit2Authorization",
                    "witness",
                    "validAfter",
                ),
                str(NOW - 59),
            ),
            SAFE_PROOF_REJECTION,
        ),
        (
            "short signature",
            _mutate(("payload", "signature"), "0x" + "12" * 64),
            SAFE_PROOF_REJECTION,
        ),
    ],
)
def test_tampered_wire_fields_fail_closed(
    label: str,
    mutator: Callable[[dict[str, Any]], None],
    expected_reason: str,
) -> None:
    proof, expected = permit2_proof()
    supplied_signature = proof["payload"]["signature"]
    mutator(proof)

    payment, reason = verify_permit2_exact(
        proof,
        token=USDC_TOKEN,
        expected_requirement=expected,
        now=NOW,
        allow_expired=False,
    )

    assert payment is None, label
    assert reason == expected_reason, label
    assert supplied_signature not in reason
    assert OTHER_ADDRESS not in reason
    assert OTHER_PAYER not in reason


def _default_types() -> dict[str, list[dict[str, str]]]:
    return {
        "EIP712Domain": copy.deepcopy(DOMAIN_FIELDS),
        "TokenPermissions": copy.deepcopy(PERMISSION_FIELDS),
        "Witness": copy.deepcopy(WITNESS_FIELDS),
        "PermitWitnessTransferFrom": copy.deepcopy(PERMIT_FIELDS),
    }


@pytest.mark.parametrize(
    ("label", "domain", "types", "primary_type"),
    [
        (
            "chain id",
            {
                "name": "Permit2",
                "chainId": 97,
                "verifyingContract": PERMIT2_ADDRESS,
            },
            None,
            "PermitWitnessTransferFrom",
        ),
        (
            "verifying contract",
            {
                "name": "Permit2",
                "chainId": 56,
                "verifyingContract": OTHER_ADDRESS,
            },
            None,
            "PermitWitnessTransferFrom",
        ),
        (
            "domain version presence",
            {
                "name": "Permit2",
                "version": "1",
                "chainId": 56,
                "verifyingContract": PERMIT2_ADDRESS,
            },
            {
                **_default_types(),
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
            },
            "PermitWitnessTransferFrom",
        ),
        (
            "primary type",
            None,
            {
                **{
                    name: fields
                    for name, fields in _default_types().items()
                    if name != "PermitWitnessTransferFrom"
                },
                "WrongPermitWitnessTransferFrom": copy.deepcopy(PERMIT_FIELDS),
            },
            "WrongPermitWitnessTransferFrom",
        ),
        (
            "nested type field order",
            None,
            {
                **_default_types(),
                "TokenPermissions": list(reversed(PERMISSION_FIELDS)),
            },
            "PermitWitnessTransferFrom",
        ),
    ],
)
def test_signatures_over_altered_typed_data_are_rejected(
    label: str,
    domain: dict[str, Any] | None,
    types: dict[str, list[dict[str, str]]] | None,
    primary_type: str,
) -> None:
    proof, expected = permit2_proof(
        signed_domain=domain,
        signed_types=types,
        signed_primary_type=primary_type,
    )

    payment, reason = verify_permit2_exact(
        proof,
        token=USDC_TOKEN,
        expected_requirement=expected,
        now=NOW,
        allow_expired=False,
    )

    assert payment is None, label
    assert reason == SAFE_PROOF_REJECTION, label
    assert proof["payload"]["signature"] not in reason


def test_signature_from_a_different_signer_is_rejected() -> None:
    proof, expected = permit2_proof(signing_key=OTHER_PRIVATE_KEY)

    payment, reason = verify_permit2_exact(
        proof,
        token=USDC_TOKEN,
        expected_requirement=expected,
        now=NOW,
        allow_expired=False,
    )

    assert payment is None
    assert reason == SAFE_PROOF_REJECTION
    assert OTHER_PAYER not in reason
    assert proof["payload"]["signature"] not in reason


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("permitted", "amount"), "-1"),
        (("permitted", "amount"), str(2**256)),
        (("permitted", "amount"), True),
        (("permitted", "amount"), float(verify.PRICE_WEI)),
        (("permitted", "amount"), "0210000000000000000"),
        (("nonce",), "-1"),
        (("nonce",), str(2**256)),
        (("nonce",), False),
        (("nonce",), 1.0),
        (("nonce",), "+1"),
        (("deadline",), "01"),
        (("witness", "validAfter"), "1_000"),
    ],
)
def test_invalid_uint256_wire_values_are_rejected(
    path: tuple[str, ...],
    value: object,
) -> None:
    proof, expected = permit2_proof()
    auth = proof["payload"]["permit2Authorization"]
    target = auth
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    payment, reason = verify_permit2_exact(
        proof,
        token=USDC_TOKEN,
        expected_requirement=expected,
        now=NOW,
        allow_expired=False,
    )

    assert payment is None
    assert reason == SAFE_PROOF_REJECTION
    assert str(value) not in reason


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (UINT256_MAX, UINT256_MAX),
        ("0", 0),
        (str(UINT256_MAX), UINT256_MAX),
        (-1, None),
        (2**256, None),
        (True, None),
        (False, None),
        (1.0, None),
        ("00", None),
        ("01", None),
        ("+1", None),
        ("1_000", None),
        ("\u0661", None),
    ],
)
def test_canonical_uint256_has_exact_bounds_and_shape(
    value: object,
    expected: int | None,
) -> None:
    assert canonical_uint256(value) == expected


def test_oversized_decimal_is_rejected_without_raising_or_echoing() -> None:
    oversized = "9" * 10_000
    proof, expected = permit2_proof()
    proof["payload"]["permit2Authorization"]["nonce"] = oversized

    payment, reason = verify_permit2_exact(
        proof,
        token=USDC_TOKEN,
        expected_requirement=expected,
        now=NOW,
        allow_expired=False,
    )

    assert canonical_uint256(oversized) is None
    assert payment is None
    assert reason == SAFE_PROOF_REJECTION
    assert oversized not in reason


def test_deadline_and_valid_after_boundaries_are_strict() -> None:
    at_limit, expected = permit2_proof(
        deadline=str(NOW + 3600),
        valid_after=str(NOW),
    )
    payment, reason = verify_permit2_exact(
        at_limit,
        token=USDC_TOKEN,
        expected_requirement=expected,
        now=NOW,
        allow_expired=False,
    )
    assert reason == ""
    assert payment is not None

    too_long, expected = permit2_proof(deadline=str(NOW + 3601))
    payment, reason = verify_permit2_exact(
        too_long,
        token=USDC_TOKEN,
        expected_requirement=expected,
        now=NOW,
        allow_expired=False,
    )
    assert payment is None
    assert reason == "authorization valid for more than 1 hour from now"

    not_yet_valid, expected = permit2_proof(valid_after=str(NOW + 1))
    payment, reason = verify_permit2_exact(
        not_yet_valid,
        token=USDC_TOKEN,
        expected_requirement=expected,
        now=NOW,
        allow_expired=False,
    )
    assert payment is None
    assert reason == "authorization not yet valid"


def test_expired_proof_can_only_be_recovered_when_expiry_is_allowed() -> None:
    proof, expected = permit2_proof(deadline=str(NOW))

    rejected, reason = verify_permit2_exact(
        proof,
        token=USDC_TOKEN,
        expected_requirement=expected,
        now=NOW,
        allow_expired=False,
    )
    recovered, recovery_reason = verify_permit2_exact(
        proof,
        token=USDC_TOKEN,
        expected_requirement=expected,
        now=NOW,
        allow_expired=True,
    )

    assert rejected is None
    assert reason == "authorization expired"
    assert recovery_reason == ""
    assert recovered is not None
    assert recovered.valid_before == NOW


def test_recovery_rejects_an_inverted_validity_window() -> None:
    proof, expected = permit2_proof(
        deadline=str(NOW - 10),
        valid_after=str(NOW - 5),
    )

    payment, reason = verify_permit2_exact(
        proof,
        token=USDC_TOKEN,
        expected_requirement=expected,
        now=NOW,
        allow_expired=True,
    )

    assert payment is None
    assert reason == SAFE_PROOF_REJECTION


@pytest.mark.parametrize(
    ("path", "extra_key"),
    [
        (("payload",), "domain"),
        (("payload",), "types"),
        (("payload",), "primaryType"),
        (("payload",), "unknown"),
        (("payload", "permit2Authorization"), "unknown"),
        (("payload", "permit2Authorization", "permitted"), "unknown"),
        (("payload", "permit2Authorization", "witness"), "unknown"),
    ],
)
def test_unknown_nested_wire_fields_are_rejected(
    path: tuple[str, ...],
    extra_key: str,
) -> None:
    proof, expected = permit2_proof()
    current = proof
    for key in path:
        current = current[key]
    current[extra_key] = "untrusted-marker"

    payment, reason = verify_permit2_exact(
        proof,
        token=USDC_TOKEN,
        expected_requirement=expected,
        now=NOW,
        allow_expired=False,
    )

    assert payment is None
    assert reason == SAFE_PROOF_REJECTION
    assert "untrusted-marker" not in reason


@pytest.mark.parametrize(
    "path",
    [
        ("payload", "signature"),
        ("payload", "permit2Authorization", "from"),
        ("payload", "permit2Authorization", "permitted", "amount"),
        ("payload", "permit2Authorization", "witness", "validAfter"),
    ],
)
def test_missing_required_wire_fields_are_rejected(path: tuple[str, ...]) -> None:
    proof, expected = permit2_proof()
    current = proof
    for key in path[:-1]:
        current = current[key]
    del current[path[-1]]

    payment, reason = verify_permit2_exact(
        proof,
        token=USDC_TOKEN,
        expected_requirement=expected,
        now=NOW,
        allow_expired=False,
    )

    assert payment is None
    assert reason == SAFE_PROOF_REJECTION


def test_permit2_version_rejection_never_reflects_untrusted_input() -> None:
    marker = "attacker-controlled-version-marker"
    proof, expected = permit2_proof()
    proof["x402Version"] = marker

    payment, reason = verify.validate_payment_proof(
        encoded_proof(proof),
        expected_requirement=expected,
        now=NOW,
    )

    assert payment is None
    assert reason == "unsupported x402Version (expected 2)"
    assert marker not in reason
