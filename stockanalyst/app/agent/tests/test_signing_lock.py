"""Concurrency contract for the temporary UOMP storage-provider override."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


def _load_signing_with_stubs(submit_workflow, storage_module, uomp_module):
    bnbagent = ModuleType("bnbagent")
    bnbagent_erc8183 = ModuleType("bnbagent.erc8183")
    bnbagent_erc8183.NegotiationHandler = object
    bnbagent.erc8183 = bnbagent_erc8183

    studio = ModuleType("bnbagent_studio_core")
    config = ModuleType("bnbagent_studio_core.config")
    config.load_studio_toml = lambda: {}
    studio.config = config
    studio_erc8183 = ModuleType("bnbagent_studio_core.erc8183")
    studio_erc8183.submit_workflow = submit_workflow
    client = ModuleType("bnbagent_studio_core.erc8183.client")
    client.get_8183_client = lambda: None
    workflows = ModuleType("bnbagent_studio_core.erc8183.workflows")
    workflows.settle_workflow = lambda *args, **kwargs: None
    wallet = ModuleType("bnbagent_studio_core.wallet")
    wallet.get_wallet = lambda: None

    stubs = {
        "bnbagent": bnbagent,
        "bnbagent.erc8183": bnbagent_erc8183,
        "bnbagent_studio_core": studio,
        "bnbagent_studio_core.config": config,
        "bnbagent_studio_core.erc8183": studio_erc8183,
        "bnbagent_studio_core.erc8183.client": client,
        "bnbagent_studio_core.erc8183.workflows": workflows,
        "bnbagent_studio_core.storage": storage_module,
        "bnbagent_studio_core.wallet": wallet,
        "uomp_storage": uomp_module,
    }
    path = Path(__file__).parents[1] / "signing.py"
    spec = importlib.util.spec_from_file_location("signing_lock_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    stubs[spec.name] = module
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    module._test_stubs = stubs
    return module


class SubmissionLockTests(unittest.TestCase):
    def test_gateway_patch_excludes_concurrent_default_submission(self) -> None:
        default_provider = object()
        gateway_provider = object()
        storage_module = ModuleType("bnbagent_studio_core.storage")
        storage_module.storage_provider_from_config = lambda **kwargs: default_provider
        uomp_module = ModuleType("uomp_storage")
        uomp_module.submit_lock = threading.Lock()
        uomp_module.UOMPGatewayStorageProvider = (
            lambda gateway_url, gateway_token: gateway_provider
        )
        gateway_entered = threading.Event()
        release_gateway = threading.Event()
        default_started = threading.Event()
        default_entered = threading.Event()
        captured: list[tuple[int, object]] = []

        def submit_workflow(job_id, response_content, *, metadata=None):
            del response_content, metadata
            captured.append((job_id, storage_module.storage_provider_from_config()))
            if job_id == 1:
                gateway_entered.set()
                release_gateway.wait(timeout=5)
            else:
                default_entered.set()
            return SimpleNamespace(
                submit_tx=f"0x{job_id}",
                deliverable_url=f"https://result/{job_id}",
            )

        signing = _load_signing_with_stubs(
            submit_workflow,
            storage_module,
            uomp_module,
        )

        gateway_thread = threading.Thread(
            target=signing.submit_result,
            args=(1, "gateway report"),
            kwargs={"gateway_url": "https://buyer.example", "gateway_token": "token"},
        )

        def submit_default() -> None:
            default_started.set()
            signing.submit_result(2, "default report")

        default_thread = threading.Thread(target=submit_default)
        with patch.dict(sys.modules, signing._test_stubs):
            gateway_thread.start()
            self.assertTrue(gateway_entered.wait(timeout=2))
            default_thread.start()
            self.assertTrue(default_started.wait(timeout=2))
            try:
                self.assertFalse(default_entered.wait(timeout=0.1))
            finally:
                release_gateway.set()
            gateway_thread.join(timeout=2)
            default_thread.join(timeout=2)

        self.assertFalse(gateway_thread.is_alive())
        self.assertFalse(default_thread.is_alive())
        self.assertTrue(default_entered.is_set())
        self.assertEqual(captured, [(1, gateway_provider), (2, default_provider)])


if __name__ == "__main__":
    unittest.main()
