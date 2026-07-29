from __future__ import annotations

import copy
import importlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPOSITORY_ROOT))

signing = importlib.import_module("stockanalyst.app.agent.signing")

MARKER = "uomp_notify_context_required_v1"


def _description(
    *,
    stored_marker: str = MARKER,
    signed_marker: object | None = None,
    prefix_negotiation_hash: bool = True,
) -> tuple[str, str]:
    account = Account.create()
    content = {
        "version": 1,
        "negotiated_at": 1_785_224_000,
        "task": "analyse AAPL and NVDA",
        "terms": {
            "deliverables": "report",
            "quality_standards": "cite sources",
            "success_criteria": stored_marker,
        },
        "price": "1000000000000000000",
        "currency": "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565",
        "quote_expires_at": 1_785_224_900,
        "chain_id": 97,
        "verifying_contract": "0xa206c0517B6371C6638CD9e4a42Cc9f02A33B0DE",
    }
    signed = copy.deepcopy(content)
    if signed_marker is not None:
        signed["terms"]["success_criteria"] = signed_marker
    canonical = json.dumps(signed, sort_keys=True, separators=(",", ":"))
    negotiation_hash = Web3.keccak(text=canonical).hex()
    if prefix_negotiation_hash:
        negotiation_hash = f"0x{negotiation_hash}"
    signature = Account.sign_message(
        encode_defunct(text=negotiation_hash),
        private_key=account.key,
    ).signature.hex()
    content["negotiation_hash"] = negotiation_hash
    content["provider_sig"] = signature
    return json.dumps(content, sort_keys=True, separators=(",", ":")), account.address


class QuoteSignatureCompatibilityTests(unittest.TestCase):
    def test_normal_sdk_recovery_is_preferred(self) -> None:
        with patch(
            "bnbagent_studio_core.erc8183.verify.recover_quote_signer",
            return_value="0x1111111111111111111111111111111111111111",
        ) as normal:
            recovered = signing.recover_quote_signer_compat("{}")
        self.assertEqual(recovered, "0x1111111111111111111111111111111111111111")
        normal.assert_called_once_with("{}")

    def test_returns_none_when_sdk_recovery_rejects_malformed_input(self) -> None:
        with patch(
            "bnbagent_studio_core.erc8183.verify.recover_quote_signer",
            side_effect=ValueError("malformed description"),
        ):
            self.assertIsNone(signing.recover_quote_signer_compat("not-json"))

    def test_recovers_legacy_marker_character_list_signature(self) -> None:
        description, expected = _description(signed_marker=list(MARKER))
        self.assertEqual(
            signing.recover_quote_signer_compat(description).lower(),
            expected.lower(),
        )

    def test_recovers_legacy_signature_with_sdk_prefixed_negotiation_hash(self) -> None:
        description, expected = _description(
            signed_marker=list(MARKER),
            prefix_negotiation_hash=True,
        )
        self.assertEqual(
            signing.recover_quote_signer_compat(description).lower(),
            expected.lower(),
        )

    def test_rejects_tampered_signed_field(self) -> None:
        description, _ = _description(signed_marker=list(MARKER))
        parsed = json.loads(description)
        parsed["price"] = "2000000000000000000"
        self.assertIsNone(
            signing.recover_quote_signer_compat(
                json.dumps(parsed, sort_keys=True, separators=(",", ":"))
            )
        )

    def test_rejects_other_success_criterion(self) -> None:
        other = "other_criterion"
        description, _ = _description(
            stored_marker=other,
            signed_marker=list(other),
        )
        self.assertIsNone(signing.recover_quote_signer_compat(description))

    def test_rejects_malformed_description(self) -> None:
        self.assertIsNone(signing.recover_quote_signer_compat("not-json"))


if __name__ == "__main__":
    unittest.main()
