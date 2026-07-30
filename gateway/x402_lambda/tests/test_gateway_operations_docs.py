"""Static safety checks for the x402 gateway operator runbook."""
from __future__ import annotations

from pathlib import Path
import unittest


RUNBOOK = Path(__file__).resolve().parents[3] / "docs" / "x402-lambda-gateway.md"


class GatewayOperationsDocumentationTests(unittest.TestCase):
    def test_cleanup_traps_use_portable_exit_code_variable(self) -> None:
        runbook = RUNBOOK.read_text(encoding="utf-8")

        self.assertNotIn("trap 'status=$?", runbook)
        self.assertEqual(runbook.count("trap 'exit_code=$?;"), 3)
        self.assertIn(
            "trap 'exit_code=$?; cleanup_gateway_oauth_provisioning \"$exit_code\"; exit \"$exit_code\"' EXIT",
            runbook,
        )
        self.assertIn(
            "trap 'exit_code=$?; cleanup_packaged_template \"$exit_code\"; exit \"$exit_code\"' EXIT",
            runbook,
        )
        self.assertIn(
            "trap 'exit_code=$?; cleanup_no_spend_files \"$exit_code\"; exit \"$exit_code\"' EXIT",
            runbook,
        )
