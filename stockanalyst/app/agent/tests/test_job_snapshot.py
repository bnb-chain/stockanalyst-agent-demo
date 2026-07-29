"""Single-read verification snapshot for notify and sweep delivery."""

from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, patch

from bnbagent.erc8183.types import JobStatus
from eth_account import Account
from eth_account.messages import encode_defunct
from stockanalyst.app.agent import signing
from web3 import Web3

PROVIDER = "0x1111111111111111111111111111111111111111"
CLIENT = "0x2222222222222222222222222222222222222222"
COMMERCE = "0x3333333333333333333333333333333333333333"


class VerifiedJobSnapshotTests(unittest.TestCase):
    def _verify_description(
        self,
        description: str,
        *,
        provider: str = PROVIDER,
    ) -> tuple[signing.VerifiedJobSnapshot | None, str, bool]:
        job = SimpleNamespace(
            status=JobStatus.FUNDED,
            provider=provider,
            client=CLIENT,
            expired_at=0,
            description=description,
            budget=10,
        )
        client = SimpleNamespace(
            get_job=lambda _job_id: job,
            network=SimpleNamespace(chain_id=97),
            commerce=SimpleNamespace(address=COMMERCE),
        )
        with (
            patch.object(signing, "get_8183_client", return_value=client),
            patch.object(
                signing,
                "get_wallet",
                return_value=SimpleNamespace(address=provider),
            ),
        ):
            return signing.verify_signed_job_snapshot(42)

    def _legacy_description(
        self,
        provider_sig: str,
        *,
        quote_expires_at: int = 1_785_224_900,
        chain_id: int = 97,
        verifying_contract: str = COMMERCE,
    ) -> str:
        content = {
            "version": 1,
            "negotiated_at": 1_785_224_000,
            "task": "analyse AAPL",
            "terms": {
                "deliverables": "report",
                "quality_standards": "cite sources",
                "success_criteria": "uomp_notify_context_required_v1",
            },
            "price": "5",
            "currency": "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565",
            "quote_expires_at": quote_expires_at,
            "chain_id": chain_id,
            "verifying_contract": verifying_contract,
        }
        signed_content = json.loads(json.dumps(content))
        signed_content["terms"]["success_criteria"] = list(
            "uomp_notify_context_required_v1"
        )
        canonical = json.dumps(
            signed_content,
            sort_keys=True,
            separators=(",", ":"),
        )
        content["negotiation_hash"] = f"0x{Web3.keccak(text=canonical).hex()}"
        content["provider_sig"] = provider_sig
        return json.dumps(content, sort_keys=True, separators=(",", ":"))

    def _signed_legacy_description(
        self,
        **overrides: object,
    ) -> tuple[str, str]:
        account = Account.create()
        unsigned = self._legacy_description("", **overrides)
        content = json.loads(unsigned)
        signature = Account.sign_message(
            encode_defunct(text=content["negotiation_hash"]),
            private_key=account.key,
        ).signature.hex()
        content["provider_sig"] = signature
        return (
            json.dumps(content, sort_keys=True, separators=(",", ":")),
            account.address,
        )

    def test_verifies_and_extracts_authorization_and_spec_from_one_chain_read(
        self,
    ) -> None:
        spec = SimpleNamespace(price="5", task="analyse", terms={"x": "y"})
        job = SimpleNamespace(
            status=JobStatus.FUNDED,
            provider=PROVIDER,
            client=CLIENT,
            expired_at=0,
            description="signed-description",
            budget=10,
        )
        client = SimpleNamespace(
            get_job=lambda job_id: job,
            network=SimpleNamespace(chain_id=97),
            commerce=SimpleNamespace(address=COMMERCE),
        )
        reads: list[int] = []

        def get_job(job_id: int):
            reads.append(job_id)
            return job

        client.get_job = get_job
        with (
            patch.object(signing, "get_8183_client", return_value=client),
            patch.object(
                signing,
                "get_wallet",
                return_value=SimpleNamespace(address=PROVIDER),
            ),
            patch(
                "bnbagent_studio_core.erc8183.verify.JobDescription.from_str",
                return_value=spec,
            ),
            patch.object(
                signing,
                "recover_bound_quote_signer_compat",
                return_value=PROVIDER,
            ) as recover,
        ):
            snapshot, reason, permanent = signing.verify_signed_job_snapshot(42)

        recover.assert_called_once_with(
            "signed-description",
            expected_chain_id=97,
            expected_verifying_contract=COMMERCE,
            now=ANY,
        )
        self.assertEqual(reads, [42])
        self.assertEqual(reason, "")
        self.assertIs(permanent, False)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.client, CLIENT)
        self.assertEqual(snapshot.chain_id, 97)
        self.assertEqual(snapshot.verifying_contract, COMMERCE)
        self.assertIs(snapshot.spec, spec)

    def test_non_funded_snapshot_failure_remains_transient(self) -> None:
        job = SimpleNamespace(status=JobStatus.SUBMITTED)
        client = SimpleNamespace(get_job=lambda _job_id: job)
        with (
            patch.object(signing, "get_8183_client", return_value=client),
            patch.object(
                signing,
                "get_wallet",
                return_value=SimpleNamespace(address=PROVIDER),
            ),
        ):
            snapshot, reason, permanent = signing.verify_signed_job_snapshot(42)

        self.assertIsNone(snapshot)
        self.assertIn("expected FUNDED", reason)
        self.assertIs(permanent, False)

    def test_rejects_a_compatibly_recovered_signature_from_another_signer(
        self,
    ) -> None:
        job = SimpleNamespace(
            status=JobStatus.FUNDED,
            provider=PROVIDER,
            client=CLIENT,
            expired_at=0,
            description="legacy-signed-description",
            budget=10,
        )
        client = SimpleNamespace(
            get_job=lambda _job_id: job,
            network=SimpleNamespace(chain_id=97),
            commerce=SimpleNamespace(address=COMMERCE),
        )
        spec = SimpleNamespace(price="5")
        with (
            patch.object(signing, "get_8183_client", return_value=client),
            patch.object(
                signing,
                "get_wallet",
                return_value=SimpleNamespace(address=PROVIDER),
            ),
            patch(
                "bnbagent_studio_core.erc8183.verify.JobDescription.from_str",
                return_value=spec,
            ),
            patch.object(
                signing,
                "recover_bound_quote_signer_compat",
                return_value=CLIENT,
            ),
        ):
            snapshot, reason, permanent = signing.verify_signed_job_snapshot(42)

        self.assertIsNone(snapshot)
        self.assertIn("quote signature does not match", reason)
        self.assertIs(permanent, True)

    def test_rejects_an_expired_legacy_compatible_quote(self) -> None:
        description, provider = self._signed_legacy_description(
            quote_expires_at=int(time.time()) - 1,
        )

        snapshot, reason, permanent = self._verify_description(
            description,
            provider=provider,
        )

        self.assertIsNone(snapshot)
        self.assertIn("quote signature does not match", reason)
        self.assertIs(permanent, True)

    def test_rejects_a_legacy_compatible_quote_from_another_chain(self) -> None:
        description, provider = self._signed_legacy_description(
            quote_expires_at=int(time.time()) + 300,
            chain_id=56,
        )

        snapshot, reason, permanent = self._verify_description(
            description,
            provider=provider,
        )

        self.assertIsNone(snapshot)
        self.assertIn("quote signature does not match", reason)
        self.assertIs(permanent, True)

    def test_rejects_a_legacy_compatible_quote_for_another_contract(self) -> None:
        description, provider = self._signed_legacy_description(
            quote_expires_at=int(time.time()) + 300,
            verifying_contract="0x4444444444444444444444444444444444444444",
        )

        snapshot, reason, permanent = self._verify_description(
            description,
            provider=provider,
        )

        self.assertIsNone(snapshot)
        self.assertIn("quote signature does not match", reason)
        self.assertIs(permanent, True)

    def test_accepts_an_unexpired_domain_bound_legacy_quote(self) -> None:
        description, provider = self._signed_legacy_description(
            quote_expires_at=int(time.time()) + 300,
        )

        snapshot, reason, permanent = self._verify_description(
            description,
            provider=provider,
        )

        self.assertIsNotNone(snapshot)
        self.assertEqual(reason, "")
        self.assertIs(permanent, False)

    def test_incomplete_versioned_description_is_a_permanent_rejection(self) -> None:
        for description in (
            '{"version":1}',
            '{"version":1,"negotiated_at":1785224000}',
        ):
            with self.subTest(description=description):
                self.assertEqual(
                    self._verify_description(description),
                    (
                        None,
                        "no signed quote anchored in job description",
                        True,
                    ),
                )

    def test_empty_and_short_legacy_signatures_are_permanent_rejections(self) -> None:
        for provider_sig in ("", "0x11"):
            with self.subTest(provider_sig=provider_sig):
                snapshot, reason, permanent = self._verify_description(
                    self._legacy_description(provider_sig)
                )
                self.assertIsNone(snapshot)
                self.assertEqual(
                    reason,
                    "quote signature does not match this provider "
                    "(or terms were tampered)",
                )
                self.assertIs(permanent, True)

    def test_invalid_legacy_signature_r_and_s_are_permanent_rejections(self) -> None:
        invalid_signatures = {
            "zero_r": f"0x{'00' * 32}{'01' * 32}1b",
            "zero_s": f"0x{'01' * 32}{'00' * 32}1b",
        }
        for field, provider_sig in invalid_signatures.items():
            with self.subTest(field=field):
                snapshot, reason, permanent = self._verify_description(
                    self._legacy_description(provider_sig)
                )
                self.assertIsNone(snapshot)
                self.assertEqual(
                    reason,
                    "quote signature does not match this provider "
                    "(or terms were tampered)",
                )
                self.assertIs(permanent, True)


if __name__ == "__main__":
    unittest.main()
