"""Single-read verification snapshot for notify and sweep delivery."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bnbagent.erc8183.types import JobStatus
from stockanalyst.app.agent import signing

PROVIDER = "0x1111111111111111111111111111111111111111"
CLIENT = "0x2222222222222222222222222222222222222222"
COMMERCE = "0x3333333333333333333333333333333333333333"


class VerifiedJobSnapshotTests(unittest.TestCase):
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
            patch(
                "bnbagent_studio_core.erc8183.verify.recover_quote_signer",
                return_value=PROVIDER,
            ),
        ):
            snapshot, reason, permanent = signing.verify_signed_job_snapshot(42)

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


if __name__ == "__main__":
    unittest.main()
