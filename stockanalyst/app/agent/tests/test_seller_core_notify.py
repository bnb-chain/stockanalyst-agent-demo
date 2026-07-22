"""Tests for authorization-bound ``notify_funded`` delivery state."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from eth_account import Account
from eth_account.messages import encode_typed_data

from stockanalyst.app.agent.notify_security import (
    JobContext,
    build_notify_typed_data,
    parse_signed_context,
)


# SellerCore's real behavior is under test; only its optional deployment and
# on-chain imports are stubbed so this unit suite does not install the full
# AgentCore/ADK runtime dependency graph.
signing_stub = ModuleType("signing")
signing_stub.clamp_price = lambda value: value
signing_stub.list_price = lambda: 0
signing_stub.sign_quote = lambda request, price: {}
signing_stub.verify_signed_job = lambda job_id: (True, "", False)
signing_stub.job_authorization_target = lambda job_id: None
signing_stub.job_spec = lambda job_id: None
signing_stub.submit_result = lambda *args, **kwargs: None
sys.modules.setdefault("signing", signing_stub)

studio_core_stub = ModuleType("bnbagent_studio_core")
erc8183_stub = ModuleType("bnbagent_studio_core.erc8183")
errors_stub = ModuleType("bnbagent_studio_core.erc8183.errors")


class SubmitPermanentlyUnsupportedError(Exception):
    pass


errors_stub.SubmitPermanentlyUnsupportedError = SubmitPermanentlyUnsupportedError
studio_core_stub.erc8183 = erc8183_stub
erc8183_stub.errors = errors_stub
sys.modules.setdefault("bnbagent_studio_core", studio_core_stub)
sys.modules.setdefault("bnbagent_studio_core.erc8183", erc8183_stub)
sys.modules.setdefault("bnbagent_studio_core.erc8183.errors", errors_stub)

from stockanalyst.app.agent import seller_core as seller_core_module  # noqa: E402
from stockanalyst.app.agent.seller_core import SellerCore  # noqa: E402


JOB_ID = 42
CHAIN_ID = 97
COMMERCE = "0x1111111111111111111111111111111111111111"
CLIENT_KEY = "0x" + "11" * 32
OTHER_CLIENT_KEY = "0x" + "22" * 32
EXPIRES_AT = int(time.time()) + 300
NONCE = "0x" + "33" * 32


class RecordingSellerCore(SellerCore):
    """Seller core whose background scheduling is observable and inert."""

    def __init__(self) -> None:
        async def unused_work(*_args, **_kwargs) -> str:
            raise AssertionError("work must not run in notification tests")

        super().__init__(run_work=unused_work, generator="test")
        self.spawned_jobs: list[tuple[int, bool]] = []

    def _spawn_job(self, job_id: int, *, verified: bool) -> None:
        if job_id in self._inflight:
            return
        self._inflight.add(job_id)
        self.spawned_jobs.append((job_id, verified))

    def _spawn_sweep(self) -> None:
        self._sweep().close()


class NotifyFundedAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = Account.from_key(CLIENT_KEY)
        self.other_client = Account.from_key(OTHER_CLIENT_KEY)
        self.core = RecordingSellerCore()
        logger_was_disabled = seller_core_module.logger.disabled
        seller_core_module.logger.disabled = True
        self.addCleanup(setattr, seller_core_module.logger, "disabled", logger_was_disabled)
        self.target = SimpleNamespace(
            client=self.client.address,
            chain_id=CHAIN_ID,
            verifying_contract=COMMERCE,
        )
        self.verify_patcher = patch.object(
            seller_core_module.signing,
            "verify_signed_job",
            return_value=(True, "", False),
        )
        self.target_patcher = patch.object(
            seller_core_module.signing,
            "job_authorization_target",
            return_value=self.target,
            create=True,
        )
        self.verify_signed_job = self.verify_patcher.start()
        self.job_authorization_target = self.target_patcher.start()
        self.addCleanup(self.verify_patcher.stop)
        self.addCleanup(self.target_patcher.stop)

    def _signed_request(
        self,
        *,
        context: dict[str, object] | None = None,
        key: str = CLIENT_KEY,
    ) -> dict[str, object]:
        raw_context = json.dumps(
            context
            or {
                "delivery_gateway_url": "https://buyer.trycloudflare.com",
                "delivery_gateway_token": "relay-token",
                "portfolio": [],
            },
            separators=(",", ":"),
        )
        typed_data = build_notify_typed_data(
            job_id=JOB_ID,
            context=raw_context,
            expires_at=EXPIRES_AT,
            nonce=NONCE,
            chain_id=CHAIN_ID,
            verifying_contract=COMMERCE,
        )
        signature = Account.sign_message(
            encode_typed_data(full_message=typed_data), key
        ).signature.hex()
        return {
            "job_id": JOB_ID,
            "authorization": {
                "context": raw_context,
                "expires_at": EXPIRES_AT,
                "nonce": NONCE,
                "signature": signature,
            },
        }

    @staticmethod
    def _context_from_request(request: dict[str, object]) -> JobContext:
        authorization = request["authorization"]
        assert isinstance(authorization, dict)
        context = authorization["context"]
        assert isinstance(context, str)
        return parse_signed_context(context)

    @staticmethod
    async def _drain_tasks(core: SellerCore) -> None:
        while core._tasks:
            await asyncio.gather(*tuple(core._tasks), return_exceptions=True)
            await asyncio.sleep(0)

    async def test_unsigned_named_notification_is_rejected_without_state(self) -> None:
        result = await self.core.notify_funded({"job_id": JOB_ID})

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "authorization_required")
        self.assertEqual(self.core._job_contexts, {})
        self.assertEqual(self.core.spawned_jobs, [])

    async def test_wrong_job_client_is_rejected_without_state(self) -> None:
        self.job_authorization_target.return_value = SimpleNamespace(
            client=self.other_client.address,
            chain_id=CHAIN_ID,
            verifying_contract=COMMERCE,
        )

        result = await self.core.notify_funded(self._signed_request())

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "caller_not_job_client")
        self.assertEqual(self.core._job_contexts, {})
        self.assertEqual(self.core.spawned_jobs, [])

    async def test_permanent_job_verification_failure_has_no_state(self) -> None:
        self.verify_signed_job.return_value = (False, "job_not_funded", True)

        result = await self.core.notify_funded(self._signed_request())

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "job_not_funded")
        self.assertEqual(self.core._job_contexts, {})
        self.assertEqual(self.core.spawned_jobs, [])

    async def test_transient_job_verification_failure_has_no_state(self) -> None:
        self.verify_signed_job.return_value = (False, "rpc_unavailable", False)

        result = await self.core.notify_funded(self._signed_request())

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "verification_unavailable")
        self.assertIs(result["retryable"], True)
        self.assertEqual(self.core._job_contexts, {})
        self.assertEqual(self.core.spawned_jobs, [])

    async def test_job_verification_timeout_has_no_state(self) -> None:
        self.verify_signed_job.side_effect = asyncio.TimeoutError

        result = await self.core.notify_funded(self._signed_request())

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "verification_unavailable")
        self.assertIs(result["retryable"], True)
        self.assertEqual(self.core._job_contexts, {})
        self.assertEqual(self.core.spawned_jobs, [])

    async def test_identical_signed_context_is_idempotent(self) -> None:
        request = self._signed_request()

        first = await self.core.notify_funded(request)
        original = self.core._job_contexts[JOB_ID]
        second = await self.core.notify_funded(request)

        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "accepted")
        self.assertIs(self.core._job_contexts[JOB_ID], original)
        self.assertEqual(self.core._job_contexts[JOB_ID].digest, original.digest)
        self.assertEqual(self.core.spawned_jobs, [(JOB_ID, True)])

    async def test_conflicting_signed_context_is_rejected(self) -> None:
        first_request = self._signed_request()
        second_request = self._signed_request(
            context={
                "delivery_gateway_url": "https://second.trycloudflare.com",
                "delivery_gateway_token": "second-token",
                "portfolio": [],
            }
        )

        first = await self.core.notify_funded(first_request)
        original_digest = self.core._job_contexts[JOB_ID].digest
        second = await self.core.notify_funded(second_request)

        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "rejected")
        self.assertEqual(second["reason"], "context_conflict")
        self.assertEqual(self.core._job_contexts[JOB_ID].digest, original_digest)
        self.assertEqual(self.core.spawned_jobs, [(JOB_ID, True)])

    async def test_background_worker_reads_context_without_popping_it(self) -> None:
        request = self._signed_request(
            context={
                "delivery_gateway_url": "https://buyer.trycloudflare.com",
                "delivery_gateway_token": "relay-token",
                "portfolio": [
                    {
                        "symbol": "AAPL",
                        "shares": 10,
                        "avgCost": 190,
                        "currency": "USD",
                    }
                ],
                "risk_profile": {
                    "tolerance": "moderate",
                    "horizonMonths": 12,
                    "preferredIndicators": ["RSI-14"],
                },
            }
        )
        context = self._context_from_request(request)
        self.core._job_contexts[JOB_ID] = context
        self.core._run_work = AsyncMock(return_value="report")
        submitted = SimpleNamespace(submit_tx="0xtx", deliverable_url="https://result")

        with (
            patch.object(seller_core_module.signing, "job_spec", return_value=None),
            patch.object(
                seller_core_module.signing,
                "submit_result",
                return_value=submitted,
            ) as submit,
        ):
            result = await self.core._do_work_and_submit(JOB_ID)

        self.assertIs(self.core._job_contexts[JOB_ID], context)
        self.assertEqual(self.core._job_contexts[JOB_ID].digest, context.digest)
        self.core._run_work.assert_awaited_once()
        submit.assert_called_once()
        self.assertEqual(submit.call_args.kwargs["gateway_url"], context.gateway_url)
        self.assertEqual(submit.call_args.kwargs["gateway_token"], context.gateway_token)
        self.assertIs(result["ok"], True)

    async def test_transient_delivery_keeps_context_for_retry(self) -> None:
        context = self._context_from_request(self._signed_request())
        self.core._job_contexts[JOB_ID] = context
        self.core._inflight.add(JOB_ID)
        self.core._do_work_and_submit = AsyncMock(side_effect=RuntimeError("rpc unavailable"))

        await self.core._run_job(JOB_ID, verified=True)

        self.assertEqual(self.core._job_contexts[JOB_ID].digest, context.digest)
        self.assertNotIn(JOB_ID, self.core._inflight)

    async def test_terminal_delivery_removes_context(self) -> None:
        context = self._context_from_request(self._signed_request())
        self.core._job_contexts[JOB_ID] = context
        self.core._inflight.add(JOB_ID)
        self.core._do_work_and_submit = AsyncMock(
            return_value={"ok": True, "job_id": JOB_ID, "tx_hash": "0xtx"}
        )

        await self.core._run_job(JOB_ID, verified=True)

        self.assertNotIn(JOB_ID, self.core._job_contexts)
        self.assertIn(JOB_ID, self.core._inflight)

    async def test_named_context_installed_before_sweep_work_is_used(self) -> None:
        run_work = AsyncMock(return_value="report")
        core = SellerCore(run_work=run_work, generator="test")
        core._sweep = AsyncMock(return_value=None)
        request = self._signed_request()
        expected_context = self._context_from_request(request)
        sweep_verify_started = threading.Event()
        allow_sweep_verify = threading.Event()
        verify_call_count = 0
        verify_lock = threading.Lock()

        def ordered_verify(_job_id: int) -> tuple[bool, str, bool]:
            nonlocal verify_call_count
            with verify_lock:
                verify_call_count += 1
                call_number = verify_call_count
            if call_number == 1:
                sweep_verify_started.set()
                allow_sweep_verify.wait(timeout=5)
            return True, "", False

        self.verify_signed_job.side_effect = ordered_verify
        submitted = SimpleNamespace(submit_tx="0xtx", deliverable_url="https://result")
        with (
            patch.object(seller_core_module.signing, "job_spec", return_value=None),
            patch.object(
                seller_core_module.signing,
                "submit_result",
                return_value=submitted,
            ) as submit,
        ):
            core._spawn_job(JOB_ID, verified=False)
            started = await asyncio.to_thread(sweep_verify_started.wait, 2)
            self.assertIs(started, True)
            try:
                result = await core.notify_funded(request)
            finally:
                allow_sweep_verify.set()
            await self._drain_tasks(core)

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(submit.call_args.kwargs["gateway_url"], expected_context.gateway_url)
        self.assertEqual(submit.call_args.kwargs["gateway_token"], expected_context.gateway_token)
        self.assertNotIn(JOB_ID, core._job_contexts)

    async def test_named_context_is_rejected_after_context_free_work_starts(self) -> None:
        run_work = AsyncMock(return_value="report")
        core = SellerCore(run_work=run_work, generator="test")
        core._sweep = AsyncMock(return_value=None)
        job_spec_started = threading.Event()
        allow_job_spec = threading.Event()

        def blocking_job_spec(_job_id: int):
            job_spec_started.set()
            allow_job_spec.wait(timeout=5)
            return None

        submitted = SimpleNamespace(submit_tx="0xtx", deliverable_url="https://result")
        with (
            patch.object(seller_core_module.signing, "job_spec", side_effect=blocking_job_spec),
            patch.object(
                seller_core_module.signing,
                "submit_result",
                return_value=submitted,
            ) as submit,
        ):
            core._spawn_job(JOB_ID, verified=False)
            started = await asyncio.to_thread(job_spec_started.wait, 2)
            self.assertIs(started, True)
            try:
                result = await core.notify_funded(self._signed_request())
            finally:
                allow_job_spec.set()
            await self._drain_tasks(core)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "delivery_already_started")
        self.assertNotIn(JOB_ID, core._job_contexts)
        self.assertIsNone(submit.call_args.kwargs["gateway_url"])
        self.assertIsNone(submit.call_args.kwargs["gateway_token"])

    async def test_identical_retries_share_one_active_sweep(self) -> None:
        core = SellerCore(run_work=AsyncMock(return_value="report"), generator="test")
        sweep_started = asyncio.Event()
        release_sweep = asyncio.Event()
        release_worker = asyncio.Event()

        async def slow_sweep() -> None:
            sweep_started.set()
            await release_sweep.wait()

        async def slow_worker(_job_id: int, *, verified: bool) -> None:
            self.assertIs(verified, True)
            await release_worker.wait()

        core._sweep = AsyncMock(side_effect=slow_sweep)
        core._run_job = AsyncMock(side_effect=slow_worker)
        request = self._signed_request()

        first = await core.notify_funded(request)
        await asyncio.wait_for(sweep_started.wait(), timeout=2)
        try:
            second = await core.notify_funded(request)
            await asyncio.sleep(0)
            self.assertEqual(core._sweep.await_count, 1)
        finally:
            release_sweep.set()
            release_worker.set()
        await self._drain_tasks(core)

        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(core._run_job.await_count, 1)


if __name__ == "__main__":
    unittest.main()
