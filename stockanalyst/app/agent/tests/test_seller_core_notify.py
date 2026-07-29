"""Tests for authorization-bound ``notify_funded`` delivery state."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import socket
import sys
import textwrap
import threading
import time
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

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


def _verified_job_snapshot_stub(job_id: int):
    ok, reason, permanent = signing_stub.verify_signed_job(job_id)
    if not ok:
        return None, reason, permanent
    target = signing_stub.job_authorization_target(job_id)
    return (
        SimpleNamespace(
            client=target.client,
            chain_id=target.chain_id,
            verifying_contract=target.verifying_contract,
            spec=signing_stub.job_spec(job_id),
        ),
        "",
        False,
    )


signing_stub.verify_signed_job_snapshot = _verified_job_snapshot_stub
signing_stub.submit_result = lambda *args, **kwargs: None
sys.modules["signing"] = signing_stub

studio_core_stub = ModuleType("bnbagent_studio_core")
erc8183_stub = ModuleType("bnbagent_studio_core.erc8183")
errors_stub = ModuleType("bnbagent_studio_core.erc8183.errors")


class SubmitPermanentlyUnsupportedError(Exception):
    pass


class SdkCallFailedError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        tx_hash: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        self.code = code
        self.tx_hash = tx_hash
        self.retryable = retryable
        super().__init__(message)


errors_stub.SubmitPermanentlyUnsupportedError = SubmitPermanentlyUnsupportedError
errors_stub.SdkCallFailedError = SdkCallFailedError
studio_core_stub.erc8183 = erc8183_stub
erc8183_stub.errors = errors_stub
sys.modules["bnbagent_studio_core"] = studio_core_stub
sys.modules["bnbagent_studio_core.erc8183"] = erc8183_stub
sys.modules["bnbagent_studio_core.erc8183.errors"] = errors_stub

from stockanalyst.app.agent import seller_core as seller_core_module
from stockanalyst.app.agent.seller_core import SellerCore

JOB_ID = 42
CHAIN_ID = 97
COMMERCE = "0x1111111111111111111111111111111111111111"
CLIENT_KEY = "0x" + "11" * 32
OTHER_CLIENT_KEY = "0x" + "22" * 32
EXPIRES_AT = int(time.time()) + 300
NONCE = "0x" + "33" * 32


def _gateway_resolver(address: str):
    def resolve(host: str, port: int, *args, **kwargs):
        del host, args, kwargs
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port),
            )
        ]

    return resolve


def _job_spec(*, required: bool):
    criteria = (
        "uomp_notify_context_required_v1"
        if required
        else "legacy_optional_delivery"
    )
    return SimpleNamespace(
        task="analyse portfolio",
        terms={"success_criteria": criteria},
    )


class RecordingSellerCore(SellerCore):
    """Seller core whose background scheduling is observable and inert."""

    def __init__(self) -> None:
        async def unused_work(*_args, **_kwargs) -> str:
            raise AssertionError("work must not run in notification tests")

        super().__init__(run_work=unused_work, generator="test")
        self.spawned_jobs: list[tuple[int, bool]] = []

    def _spawn_job(self, job_id: int, *, verified: bool) -> None:
        if job_id in self._inflight or job_id in self._handled:
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
        self.real_validate_gateway_url = seller_core_module.validate_gateway_url
        self.gateway_patcher = patch.object(
            seller_core_module,
            "validate_gateway_url",
            side_effect=lambda url: self.real_validate_gateway_url(
                url,
                resolver=_gateway_resolver("104.16.132.229"),
            ),
        )
        self.validate_gateway_url = self.gateway_patcher.start()
        self.addCleanup(self.verify_patcher.stop)
        self.addCleanup(self.target_patcher.stop)
        self.addCleanup(self.gateway_patcher.stop)

    def _signed_request(
        self,
        *,
        context: dict[str, object] | None = None,
        key: str = CLIENT_KEY,
        job_id: int = JOB_ID,
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
            job_id=job_id,
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
            "job_id": job_id,
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

    def assert_optional_contextless_transition_is_atomic(
        self,
        source: str,
    ) -> None:
        function = ast.parse(textwrap.dedent(source)).body[0]
        self.assertIsInstance(function, ast.AsyncFunctionDef)
        assert isinstance(function, ast.AsyncFunctionDef)

        marker_indices = [
            index
            for index, statement in enumerate(function.body)
            if any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_contextless_started"
                for node in ast.walk(statement)
            )
        ]
        self.assertEqual(len(marker_indices), 1)
        marker_index = marker_indices[0]
        context_lookup_indices = [
            index
            for index, statement in enumerate(function.body[:marker_index])
            if isinstance(statement, ast.If)
            and any(
                isinstance(node, ast.Attribute)
                and node.attr == "_job_contexts"
                for node in ast.walk(statement.test)
            )
        ]
        self.assertGreaterEqual(len(context_lookup_indices), 2)
        final_lookup_index = context_lookup_indices[-1]
        final_lookup = function.body[final_lookup_index]
        assert isinstance(final_lookup, ast.If)
        self.assertEqual(
            final_lookup.orelse,
            [],
            "final context decision must not have an else branch",
        )

        guarded_statements = function.body[
            final_lookup_index : marker_index + 1
        ]
        suspension_types = (ast.Await, ast.AsyncWith, ast.AsyncFor)
        suspensions = [
            type(node).__name__
            for statement in guarded_statements
            for node in ast.walk(statement)
            if isinstance(node, suspension_types)
        ]
        self.assertEqual(
            suspensions,
            [],
            "optional contextless marker transition must not suspend",
        )

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

    async def test_invalid_job_ids_are_rejected_before_chain_rpc(self) -> None:
        for raw_job_id in (True, -1, 2**256):
            with self.subTest(job_id=raw_job_id):
                request = self._signed_request()
                request["job_id"] = raw_job_id
                self.verify_signed_job.reset_mock()
                self.job_authorization_target.reset_mock()

                result = await self.core.notify_funded(request)

                self.assertEqual(result["status"], "rejected")
                self.verify_signed_job.assert_not_called()
                self.job_authorization_target.assert_not_called()

    async def test_malformed_authorization_is_rejected_before_chain_rpc(self) -> None:
        request = self._signed_request()
        authorization = request["authorization"]
        assert isinstance(authorization, dict)
        malformed_authorizations = [
            {key: value for key, value in authorization.items() if key != "context"},
            {**authorization, "unexpected": "value"},
            {**authorization, "context": 42},
            {**authorization, "context": "x" * 65_537},
            {**authorization, "expires_at": True},
            {**authorization, "expires_at": int(time.time()) - 31},
            {**authorization, "expires_at": int(time.time()) + 601},
            {**authorization, "nonce": "0x01"},
            {**authorization, "signature": "0x01"},
        ]

        for malformed in malformed_authorizations:
            with self.subTest(keys=tuple(malformed), expires_at=malformed.get("expires_at")):
                self.verify_signed_job.reset_mock()
                self.job_authorization_target.reset_mock()
                self.validate_gateway_url.reset_mock()

                result = await self.core.notify_funded(
                    {"job_id": JOB_ID, "authorization": malformed}
                )

                self.assertEqual(result["status"], "rejected")
                self.verify_signed_job.assert_not_called()
                self.job_authorization_target.assert_not_called()
                self.validate_gateway_url.assert_not_called()

    async def test_unsafe_signed_gateway_is_rejected_before_state_or_spawn(self) -> None:
        self.validate_gateway_url.side_effect = lambda url: self.real_validate_gateway_url(
            url,
            resolver=_gateway_resolver("127.0.0.1"),
        )

        result = await self.core.notify_funded(self._signed_request())

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "unsafe_gateway")
        self.assertEqual(self.core._job_contexts, {})
        self.assertEqual(self.core._inflight, set())
        self.assertEqual(self.core.spawned_jobs, [])

    async def test_signer_verification_precedes_gateway_dns(self) -> None:
        request = self._signed_request()
        context = self._context_from_request(request)
        order: list[str] = []

        def verify_signer(*args, **kwargs):
            del args, kwargs
            order.append("signer")
            return context

        def validate_dns(url: str) -> str:
            order.append("dns")
            return url

        with (
            patch.object(
                seller_core_module,
                "verify_notify_authorization",
                side_effect=verify_signer,
            ),
            patch.object(
                seller_core_module,
                "validate_gateway_url",
                side_effect=validate_dns,
            ),
        ):
            result = await self.core.notify_funded(request)

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(order, ["signer", "dns"])

    async def test_slow_gateway_validation_does_not_block_event_loop(self) -> None:
        started = threading.Event()
        release = threading.Event()
        loop_thread = threading.get_ident()
        validator_threads: list[int] = []

        def blocking_validate(url: str) -> str:
            validator_threads.append(threading.get_ident())
            started.set()
            release.wait(timeout=2)
            return url

        with patch.object(
            seller_core_module,
            "validate_gateway_url",
            side_effect=blocking_validate,
        ):
            notification = asyncio.create_task(
                self.core.notify_funded(self._signed_request())
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            heartbeat = asyncio.create_task(asyncio.sleep(0))
            await asyncio.wait_for(heartbeat, timeout=0.1)
            self.assertFalse(notification.done())
            release.set()
            result = await notification

        self.assertEqual(len(validator_threads), 1)
        self.assertTrue(
            all(thread_id != loop_thread for thread_id in validator_threads)
        )
        self.assertEqual(result["status"], "accepted")

    async def test_signature_recovery_runs_off_event_loop(self) -> None:
        loop_thread = threading.get_ident()
        verifier_threads: list[int] = []
        real_verify = seller_core_module.verify_notify_authorization

        def record_verifier_thread(*args, **kwargs) -> JobContext:
            verifier_threads.append(threading.get_ident())
            return real_verify(*args, **kwargs)

        with patch.object(
            seller_core_module,
            "verify_notify_authorization",
            side_effect=record_verifier_thread,
        ):
            result = await self.core.notify_funded(self._signed_request())

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(verifier_threads), 1)
        self.assertNotEqual(verifier_threads[0], loop_thread)

    async def test_validation_timeout_has_no_state_or_delivery_side_effect(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocking_validate(url: str) -> str:
            started.set()
            release.wait(timeout=2)
            return url

        try:
            with (
                patch.object(
                    seller_core_module,
                    "validate_gateway_url",
                    side_effect=blocking_validate,
                ),
                patch.object(seller_core_module, "_PREVERIFY_TIMEOUT_SECONDS", 0.05),
                patch.object(self.core, "_spawn_sweep") as spawn_sweep,
            ):
                notification = asyncio.create_task(
                    self.core.notify_funded(self._signed_request())
                )
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                result = await notification

            self.assertEqual(
                result,
                {
                    "status": "rejected",
                    "job_id": JOB_ID,
                    "reason": "verification_unavailable",
                    "retryable": True,
                },
            )
            self.assertEqual(self.core._job_contexts, {})
            self.assertEqual(self.core.spawned_jobs, [])
            self.assertNotIn(JOB_ID, self.core._handled)
            self.assertNotIn(JOB_ID, self.core._contextless_started)
            spawn_sweep.assert_not_called()
        finally:
            release.set()

    async def test_validation_worker_limit_covers_lingering_workers(self) -> None:
        core = RecordingSellerCore()
        requests = [
            self._signed_request(job_id=JOB_ID + offset)
            for offset in range(5)
        ]
        real_verify = seller_core_module.verify_notify_authorization
        active = 0
        maximum_active = 0
        started_calls = 0
        lock = threading.Lock()
        four_started = threading.Event()
        release = threading.Event()

        def blocking_verify(*args, **kwargs) -> JobContext:
            nonlocal active, maximum_active, started_calls
            with lock:
                active += 1
                started_calls += 1
                maximum_active = max(maximum_active, active)
                if started_calls == 4:
                    four_started.set()
            try:
                release.wait(timeout=2)
                return real_verify(*args, **kwargs)
            finally:
                with lock:
                    active -= 1

        first_four: list[asyncio.Task[dict[str, object]]] = []
        try:
            with (
                patch.object(
                    seller_core_module,
                    "verify_notify_authorization",
                    side_effect=blocking_verify,
                ),
                patch.object(seller_core_module, "_PREVERIFY_TIMEOUT_SECONDS", 5),
            ):
                first_four = [
                    asyncio.create_task(core.notify_funded(request))
                    for request in requests[:4]
                ]
                self.assertTrue(await asyncio.to_thread(four_started.wait, 2))

                with patch.object(
                    seller_core_module,
                    "_PREVERIFY_TIMEOUT_SECONDS",
                    0.05,
                ):
                    fifth = await core.notify_funded(requests[4])

                self.assertEqual(fifth["reason"], "verification_unavailable")
                self.assertEqual(started_calls, 4)
                self.assertEqual(maximum_active, 4)
                await asyncio.sleep(0.1)
                self.assertEqual(started_calls, 4)
        finally:
            release.set()
            if first_four:
                await asyncio.gather(*first_four, return_exceptions=True)

        for _ in range(100):
            if core._notify_validation_slots._value == 4:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(core._notify_validation_slots._value, 4)

    async def test_unexpected_validation_failure_is_retryable_and_opaque(self) -> None:
        with patch.object(
            seller_core_module,
            "verify_notify_authorization",
            side_effect=RuntimeError("sensitive worker failure"),
        ):
            result = await self.core.notify_funded(self._signed_request())

        self.assertEqual(
            result,
            {
                "status": "rejected",
                "job_id": JOB_ID,
                "reason": "verification_unavailable",
                "retryable": True,
            },
        )
        self.assertNotIn("sensitive", repr(result))
        self.assertEqual(self.core._job_contexts, {})
        self.assertEqual(self.core.spawned_jobs, [])

    async def test_late_validation_failure_is_retrieved_without_loop_warning(self) -> None:
        started = threading.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop_errors: list[dict[str, object]] = []

        def blocking_failure(url: str) -> str:
            started.set()
            release.wait(timeout=2)
            raise RuntimeError(f"sensitive late failure for {url}")

        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        try:
            with (
                patch.object(
                    seller_core_module,
                    "validate_gateway_url",
                    side_effect=blocking_failure,
                ),
                patch.object(seller_core_module, "_PREVERIFY_TIMEOUT_SECONDS", 0.05),
            ):
                notification = asyncio.create_task(
                    self.core.notify_funded(self._signed_request())
                )
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                result = await notification

            self.assertEqual(result["reason"], "verification_unavailable")
            release.set()
            for _ in range(100):
                if self.core._notify_validation_slots._value == 4:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0)
            self.assertEqual(loop_errors, [])
        finally:
            release.set()
            loop.set_exception_handler(previous_handler)

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

    async def test_named_notification_uses_one_verified_job_snapshot(self) -> None:
        snapshot = SimpleNamespace(
            client=self.client.address,
            chain_id=CHAIN_ID,
            verifying_contract=COMMERCE,
            spec=_job_spec(required=True),
        )
        report = AsyncMock(return_value=True)
        with (
            patch.object(
                seller_core_module.signing,
                "verify_signed_job_snapshot",
                return_value=(snapshot, "", False),
                create=True,
            ) as verify_snapshot,
            patch.object(
                seller_core_module.signing,
                "verify_signed_job",
                side_effect=AssertionError("legacy verification read"),
            ),
            patch.object(
                seller_core_module.signing,
                "job_authorization_target",
                side_effect=AssertionError("second authorization read"),
            ),
            patch.object(
                seller_core_module,
                "report_competition_call",
                report,
                create=True,
            ),
        ):
            result = await self.core.notify_funded(self._signed_request())

        self.assertEqual(result["status"], "accepted")
        verify_snapshot.assert_called_once_with(JOB_ID)
        report.assert_awaited_once_with(
            event_id=f"erc8183:{CHAIN_ID}:{COMMERCE.lower()}:{JOB_ID}",
            address=self.client.address,
            called_at=ANY,
        )

    async def test_sweep_reuses_spec_from_one_verified_job_snapshot(self) -> None:
        run_work = AsyncMock(return_value="report")
        core = SellerCore(run_work=run_work, generator="test")
        context = self._context_from_request(self._signed_request())
        core._job_contexts[JOB_ID] = context
        snapshot = SimpleNamespace(
            client=self.client.address,
            chain_id=CHAIN_ID,
            verifying_contract=COMMERCE,
            spec=_job_spec(required=True),
        )
        submitted = SimpleNamespace(
            submit_tx="0xtx",
            deliverable_url="https://result",
        )
        report = AsyncMock(return_value=True)
        with (
            patch.object(
                seller_core_module.signing,
                "verify_signed_job_snapshot",
                return_value=(snapshot, "", False),
                create=True,
            ) as verify_snapshot,
            patch.object(
                seller_core_module.signing,
                "verify_signed_job",
                side_effect=AssertionError("legacy verification read"),
            ),
            patch.object(
                seller_core_module.signing,
                "job_spec",
                side_effect=AssertionError("second spec read"),
            ),
            patch.object(
                seller_core_module.signing,
                "submit_result",
                return_value=submitted,
            ),
            patch.object(
                seller_core_module,
                "report_competition_call",
                report,
                create=True,
            ),
        ):
            result = await core._fulfill_job(JOB_ID)

        self.assertIs(result["ok"], True)
        verify_snapshot.assert_called_once_with(JOB_ID)
        report.assert_awaited_once_with(
            event_id=f"erc8183:{CHAIN_ID}:{COMMERCE.lower()}:{JOB_ID}",
            address=self.client.address,
            called_at=ANY,
        )
        run_work.assert_awaited_once()

    async def test_transient_delivery_keeps_context_for_retry(self) -> None:
        context = self._context_from_request(self._signed_request())
        event = asyncio.Event()
        self.core._job_contexts[JOB_ID] = context
        self.core._context_events[JOB_ID] = event
        self.core._context_deadlines[JOB_ID] = 123.0
        self.core._inflight.add(JOB_ID)
        self.core._do_work_and_submit = AsyncMock(side_effect=RuntimeError("rpc unavailable"))

        await self.core._run_job(JOB_ID, verified=True)

        self.assertEqual(self.core._job_contexts[JOB_ID].digest, context.digest)
        self.assertIs(self.core._context_events[JOB_ID], event)
        self.assertEqual(self.core._context_deadlines[JOB_ID], 123.0)
        self.assertNotIn(JOB_ID, self.core._inflight)

    async def test_terminal_delivery_removes_context(self) -> None:
        context = self._context_from_request(self._signed_request())
        self.core._job_contexts[JOB_ID] = context
        self.core._context_events[JOB_ID] = asyncio.Event()
        self.core._context_deadlines[JOB_ID] = 123.0
        self.core._contextless_started.add(JOB_ID)
        self.core._inflight.add(JOB_ID)
        self.core._do_work_and_submit = AsyncMock(
            return_value={"ok": True, "job_id": JOB_ID, "tx_hash": "0xtx"}
        )

        await self.core._run_job(JOB_ID, verified=True)

        self.assertNotIn(JOB_ID, self.core._job_contexts)
        self.assertNotIn(JOB_ID, self.core._context_events)
        self.assertNotIn(JOB_ID, self.core._context_deadlines)
        self.assertNotIn(JOB_ID, self.core._contextless_started)
        self.assertNotIn(JOB_ID, self.core._inflight)
        self.assertIn(JOB_ID, self.core._handled)

    async def test_permanent_sweep_skip_clears_grace_state(self) -> None:
        run_work = AsyncMock()
        core = SellerCore(run_work=run_work, generator="test")
        core._job_contexts[JOB_ID] = self._context_from_request(
            self._signed_request()
        )
        core._inflight.add(JOB_ID)
        core._context_events[JOB_ID] = asyncio.Event()
        core._context_deadlines[JOB_ID] = 123.0
        core._contextless_started.add(JOB_ID)
        with (
            patch.object(
                seller_core_module.signing,
                "verify_signed_job",
                return_value=(False, "invalid_provider_signature", True),
            ),
            patch.object(seller_core_module.signing, "submit_result") as submit,
        ):
            await core._run_job(JOB_ID, verified=False)

        self.assertIn(JOB_ID, core._handled)
        self.assertNotIn(JOB_ID, core._job_contexts)
        self.assertNotIn(JOB_ID, core._context_events)
        self.assertNotIn(JOB_ID, core._context_deadlines)
        self.assertNotIn(JOB_ID, core._contextless_started)
        self.assertNotIn(JOB_ID, core._inflight)
        run_work.assert_not_awaited()
        submit.assert_not_called()

    async def test_terminal_delivery_log_does_not_include_gateway_result_url(self) -> None:
        self.core._inflight.add(JOB_ID)
        self.core._do_work_and_submit = AsyncMock(
            return_value={
                "ok": True,
                "job_id": JOB_ID,
                "tx_hash": "0xtx",
                "deliverable_url": "https://buyer.trycloudflare.com/v1/payload/private",
            }
        )

        with patch.object(seller_core_module.logger, "info") as log_info:
            await self.core._run_job(JOB_ID, verified=True)

        rendered = log_info.call_args.args[0] % log_info.call_args.args[1:]
        self.assertIn(str(JOB_ID), rendered)
        self.assertNotIn("trycloudflare.com", rendered)
        self.assertNotIn("private", rendered)

    async def test_terminal_sdk_submission_failures_do_not_retry(self) -> None:
        failures = [
            SdkCallFailedError("expired", code="job_expired"),
            SdkCallFailedError("wrong status", code="wrong_status"),
            SdkCallFailedError(
                "deadline passed",
                code="submit_deadline_passed",
            ),
            SdkCallFailedError(
                "network timeout text must not override the verdict",
                code="future_terminal_code",
                retryable=False,
            ),
            SdkCallFailedError(
                "unclassified SDK failure is not an authorized retry verdict",
                code=None,
                retryable=None,
            ),
        ]
        for failure in failures:
            with self.subTest(code=failure.code, retryable=failure.retryable):
                core = RecordingSellerCore()
                context = self._context_from_request(self._signed_request())
                event = asyncio.Event()
                core._job_contexts[JOB_ID] = context
                core._context_events[JOB_ID] = event
                core._context_deadlines[JOB_ID] = 123.0
                core._contextless_started.add(JOB_ID)
                core._inflight.add(JOB_ID)
                core._run_work = AsyncMock(return_value="report")
                with (
                    patch.object(seller_core_module.signing, "job_spec", return_value=None),
                    patch.object(
                        seller_core_module.signing,
                        "submit_result",
                        side_effect=failure,
                    ),
                ):
                    await core._run_job(JOB_ID, verified=True)

                self.assertNotIn(JOB_ID, core._job_contexts)
                self.assertNotIn(JOB_ID, core._context_events)
                self.assertNotIn(JOB_ID, core._context_deadlines)
                self.assertNotIn(JOB_ID, core._contextless_started)
                self.assertNotIn(JOB_ID, core._inflight)
                self.assertIn(JOB_ID, core._handled)

    async def test_retryable_sdk_submission_failure_keeps_context(self) -> None:
        core = RecordingSellerCore()
        context = self._context_from_request(self._signed_request())
        event = asyncio.Event()
        core._job_contexts[JOB_ID] = context
        core._context_events[JOB_ID] = event
        core._context_deadlines[JOB_ID] = 123.0
        core._inflight.add(JOB_ID)
        core._run_work = AsyncMock(return_value="report")
        failure = SdkCallFailedError(
            "job_expired appears only in arbitrary message text",
            code="chain_unavailable",
            retryable=True,
        )
        with (
            patch.object(seller_core_module.signing, "job_spec", return_value=None),
            patch.object(
                seller_core_module.signing,
                "submit_result",
                side_effect=failure,
            ),
        ):
            await core._run_job(JOB_ID, verified=True)

        self.assertIs(core._job_contexts[JOB_ID], context)
        self.assertIs(core._context_events[JOB_ID], event)
        self.assertEqual(core._context_deadlines[JOB_ID], 123.0)
        self.assertNotIn(JOB_ID, core._inflight)
        self.assertNotIn(JOB_ID, core._handled)

    async def test_stale_verified_notification_cannot_install_after_terminal(self) -> None:
        core = RecordingSellerCore()
        first_request = self._signed_request()
        conflicting_request = self._signed_request(
            context={
                "delivery_gateway_url": "https://second.trycloudflare.com",
                "delivery_gateway_token": "second-token",
                "portfolio": [],
            }
        )
        target_lookup_started = threading.Event()
        release_target_lookup = threading.Event()
        target_calls = 0
        target_calls_lock = threading.Lock()

        def ordered_target(_job_id: int):
            nonlocal target_calls
            with target_calls_lock:
                target_calls += 1
                call_number = target_calls
            if call_number == 2:
                target_lookup_started.set()
                release_target_lookup.wait(timeout=5)
            return self.target

        self.job_authorization_target.side_effect = ordered_target
        first = await core.notify_funded(first_request)
        core._do_work_and_submit = AsyncMock(
            return_value={"ok": True, "job_id": JOB_ID, "tx_hash": "0xtx"}
        )
        stale = asyncio.create_task(core.notify_funded(conflicting_request))
        started = await asyncio.to_thread(target_lookup_started.wait, 2)
        self.assertIs(started, True)
        try:
            await core._run_job(JOB_ID, verified=True)
        finally:
            release_target_lookup.set()
        second = await stale

        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "rejected")
        self.assertEqual(second["reason"], "delivery_already_started")
        self.assertNotIn(JOB_ID, core._job_contexts)
        self.assertIn(JOB_ID, core._handled)
        self.assertEqual(core.spawned_jobs, [(JOB_ID, True)])

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

    async def test_named_notify_during_grace_wakes_same_sweep_worker(self) -> None:
        run_work = AsyncMock(return_value="report")
        core = SellerCore(run_work=run_work, generator="test")
        core._sweep = AsyncMock(return_value=None)
        submitted = SimpleNamespace(submit_tx="0xtx", deliverable_url="https://result")

        with (
            patch.object(seller_core_module, "_SWEEP_CONTEXT_GRACE_SECONDS", 1.0),
            patch.object(
                seller_core_module.signing,
                "job_spec",
                return_value=_job_spec(required=True),
            ),
            patch.object(
                seller_core_module.signing,
                "submit_result",
                return_value=submitted,
            ) as submit,
        ):
            core._spawn_job(JOB_ID, verified=False)
            while JOB_ID not in core._context_events:
                await asyncio.sleep(0)
            original_task_count = len(core._tasks)
            result = await core.notify_funded(self._signed_request())
            await self._drain_tasks(core)

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(original_task_count, 1)
        run_work.assert_awaited_once()
        submit.assert_called_once()
        self.assertIsNotNone(submit.call_args.kwargs["gateway_url"])

    async def test_repeated_waits_do_not_reset_or_shorten_deadline(self) -> None:
        core = SellerCore(run_work=AsyncMock(), generator="test")
        loop_times = iter((100.0, 100.0, 100.25, 100.25))
        fake_loop = SimpleNamespace(time=lambda: next(loop_times))
        observed_timeouts: list[float] = []

        async def capture_timeout(awaitable, *, timeout: float):
            observed_timeouts.append(timeout)
            awaitable.close()
            raise TimeoutError

        with (
            patch.object(
                seller_core_module,
                "_SWEEP_CONTEXT_GRACE_SECONDS",
                60.0,
            ),
            patch.object(
                seller_core_module.asyncio,
                "get_running_loop",
                return_value=fake_loop,
            ),
            patch.object(
                seller_core_module.asyncio,
                "wait_for",
                side_effect=capture_timeout,
            ),
        ):
            self.assertIs(
                await core._await_sweep_context(JOB_ID, required=True),
                False,
            )
            original_deadline = core._context_deadlines[JOB_ID]
            self.assertIs(
                await core._await_sweep_context(JOB_ID, required=True),
                False,
            )

        self.assertEqual(observed_timeouts, [60.0, 59.75])
        self.assertEqual(original_deadline, 160.0)
        self.assertEqual(core._context_deadlines[JOB_ID], original_deadline)

    def test_optional_contextless_transition_has_no_await_gap(self) -> None:
        self.assert_optional_contextless_transition_is_atomic(
            inspect.getsource(SellerCore._await_sweep_context)
        )

    def test_optional_transition_ast_guard_rejects_all_suspensions(self) -> None:
        mutants = {
            "else-await": """
                async def sample(self, job_id, required):
                    if job_id in self._job_contexts:
                        return True
                    if job_id in self._job_contexts:
                        return True
                    else:
                        await asyncio.sleep(0)
                    if required:
                        return False
                    self._contextless_started.add(job_id)
                    return True
            """,
            "async-with": """
                async def sample(self, job_id, required):
                    if job_id in self._job_contexts:
                        return True
                    if job_id in self._job_contexts:
                        return True
                    async with context_manager:
                        pass
                    if required:
                        return False
                    self._contextless_started.add(job_id)
                    return True
            """,
            "async-for": """
                async def sample(self, job_id, required):
                    if job_id in self._job_contexts:
                        return True
                    if job_id in self._job_contexts:
                        return True
                    async for item in async_items:
                        consume(item)
                    if required:
                        return False
                    self._contextless_started.add(job_id)
                    return True
            """,
        }
        expected_messages = {
            "else-await": "final context decision must not have an else branch",
            "async-with": "optional contextless marker transition must not suspend",
            "async-for": "optional contextless marker transition must not suspend",
        }

        for name, source in mutants.items():
            with (
                self.subTest(mutant=name),
                self.assertRaisesRegex(
                    AssertionError,
                    expected_messages[name],
                ),
            ):
                self.assert_optional_contextless_transition_is_atomic(source)

    async def test_repeated_sweep_discovery_keeps_one_grace_worker(self) -> None:
        core = SellerCore(run_work=AsyncMock(), generator="test")
        with (
            patch.object(seller_core_module, "_SWEEP_CONTEXT_GRACE_SECONDS", 1.0),
            patch.object(
                seller_core_module.signing,
                "job_spec",
                return_value=_job_spec(required=True),
            ),
        ):
            core._spawn_job(JOB_ID, verified=False)
            core._spawn_job(JOB_ID, verified=False)
            while JOB_ID not in core._context_events:
                await asyncio.sleep(0)
            self.assertEqual(len(core._tasks), 1)
            core._job_contexts[JOB_ID] = self._context_from_request(
                self._signed_request()
            )
            core._context_events[JOB_ID].set()
            core._run_work = AsyncMock(return_value="report")
            with patch.object(
                seller_core_module.signing,
                "submit_result",
                return_value=SimpleNamespace(
                    submit_tx="0xtx",
                    deliverable_url="https://result",
                ),
            ):
                await self._drain_tasks(core)

    async def test_unmarked_job_falls_back_contextless_after_grace(self) -> None:
        run_work = AsyncMock(return_value="report")
        core = SellerCore(run_work=run_work, generator="test")
        core._inflight.add(JOB_ID)
        submitted = SimpleNamespace(
            submit_tx="0xtx",
            deliverable_url="https://result",
        )
        with (
            patch.object(seller_core_module, "_SWEEP_CONTEXT_GRACE_SECONDS", 0.001),
            patch.object(
                seller_core_module.signing,
                "job_spec",
                return_value=_job_spec(required=False),
            ),
            patch.object(
                seller_core_module.signing,
                "submit_result",
                return_value=submitted,
            ) as submit,
        ):
            await core._run_job(JOB_ID, verified=False)

        run_work.assert_awaited_once()
        submit.assert_called_once()
        self.assertIsNone(submit.call_args.kwargs["gateway_url"])
        self.assertIn(JOB_ID, core._handled)

    async def test_context_arriving_before_optional_transition_wins(self) -> None:
        core = SellerCore(run_work=AsyncMock(), generator="test")
        context = self._context_from_request(self._signed_request())
        event = asyncio.Event()
        core._context_events[JOB_ID] = event
        core._context_deadlines[JOB_ID] = asyncio.get_running_loop().time() + 1.0
        waiter = asyncio.create_task(
            core._await_sweep_context(JOB_ID, required=False)
        )
        await asyncio.sleep(0)
        core._job_contexts[JOB_ID] = context
        event.set()

        ready = await waiter

        self.assertIs(ready, True)
        self.assertIs(core._job_contexts[JOB_ID], context)
        self.assertNotIn(JOB_ID, core._contextless_started)

    async def test_named_context_is_rejected_after_context_free_work_starts(
        self,
    ) -> None:
        work_started = asyncio.Event()
        release_work = asyncio.Event()

        async def blocking_work(*_args, **_kwargs) -> str:
            work_started.set()
            await release_work.wait()
            return "report"

        core = SellerCore(run_work=blocking_work, generator="test")
        core._sweep = AsyncMock(return_value=None)
        submitted = SimpleNamespace(
            submit_tx="0xtx",
            deliverable_url="https://result",
        )
        with (
            patch.object(seller_core_module, "_SWEEP_CONTEXT_GRACE_SECONDS", 0.001),
            patch.object(
                seller_core_module.signing,
                "job_spec",
                return_value=_job_spec(required=False),
            ),
            patch.object(
                seller_core_module.signing,
                "submit_result",
                return_value=submitted,
            ) as submit,
        ):
            core._spawn_job(JOB_ID, verified=False)
            await asyncio.wait_for(work_started.wait(), timeout=2)
            try:
                result = await core.notify_funded(self._signed_request())
            finally:
                release_work.set()
            await self._drain_tasks(core)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "delivery_already_started")
        self.assertIsNone(submit.call_args.kwargs["gateway_url"])

    async def test_contextless_transient_accepts_later_named_notification(
        self,
    ) -> None:
        original_event = asyncio.Event()
        original_deadline = asyncio.get_running_loop().time() - 1.0

        async def fail_contextless_work(*_args, **_kwargs) -> str:
            self.assertIn(JOB_ID, core._contextless_started)
            self.assertNotIn(JOB_ID, core._job_contexts)
            raise RuntimeError("transient work failure")

        first_run_work = AsyncMock(side_effect=fail_contextless_work)
        core = SellerCore(run_work=first_run_work, generator="test")
        core._sweep = AsyncMock(return_value=None)
        core._inflight.add(JOB_ID)
        core._context_events[JOB_ID] = original_event
        core._context_deadlines[JOB_ID] = original_deadline
        submitted = SimpleNamespace(
            submit_tx="0xtx",
            deliverable_url="https://result",
        )

        with (
            patch.object(
                seller_core_module.signing,
                "job_spec",
                return_value=_job_spec(required=False),
            ),
            patch.object(
                seller_core_module.signing,
                "submit_result",
                return_value=submitted,
            ) as submit,
        ):
            await core._run_job(JOB_ID, verified=False)

            first_run_work.assert_awaited_once()
            submit.assert_not_called()
            self.assertNotIn(JOB_ID, core._contextless_started)
            self.assertIs(core._context_events[JOB_ID], original_event)
            self.assertEqual(
                core._context_deadlines[JOB_ID],
                original_deadline,
            )
            self.assertNotIn(JOB_ID, core._inflight)
            self.assertNotIn(JOB_ID, core._handled)

            second_run_work = AsyncMock(return_value="report")
            core._run_work = second_run_work
            result = await core.notify_funded(self._signed_request())
            await self._drain_tasks(core)

        self.assertEqual(result["status"], "accepted")
        second_run_work.assert_awaited_once()
        submit.assert_called_once()
        self.assertIsNotNone(submit.call_args.kwargs["gateway_url"])
        self.assertIn(JOB_ID, core._handled)

    async def test_marked_sweep_without_context_never_starts_work(self) -> None:
        run_work = AsyncMock(return_value="must not run")
        core = SellerCore(run_work=run_work, generator="test")

        async def sweep_victim() -> None:
            core._spawn_job(JOB_ID, verified=False)

        core._sweep = AsyncMock(side_effect=sweep_victim)

        with (
            patch.object(seller_core_module, "_SWEEP_CONTEXT_GRACE_SECONDS", 0.001),
            patch.object(
                seller_core_module.signing,
                "job_spec",
                return_value=_job_spec(required=True),
            ),
            patch.object(seller_core_module.signing, "submit_result") as submit,
        ):
            response = await core.notify_funded({})
            await self._drain_tasks(core)

        self.assertEqual(response["status"], "accepted")
        run_work.assert_not_awaited()
        submit.assert_not_called()
        self.assertNotIn(JOB_ID, core._contextless_started)
        self.assertNotIn(JOB_ID, core._inflight)
        self.assertNotIn(JOB_ID, core._handled)

    async def test_required_sweep_cleanup_recovers_context_committed_while_inflight(
        self,
    ) -> None:
        core = SellerCore(run_work=AsyncMock(return_value="report"), generator="test")
        context = self._context_from_request(self._signed_request())
        core._fulfill_job = AsyncMock(
            return_value={
                "ok": False,
                "job_id": JOB_ID,
                "skip": False,
                "reason": "notify_context_required",
            }
        )
        core._do_work_and_submit = AsyncMock(
            return_value={"ok": True, "job_id": JOB_ID, "tx_hash": "0xtx"}
        )
        core._inflight.add(JOB_ID)
        named_spawn_task_counts: list[tuple[int, int]] = []
        context_installed = False

        def install_context_before_cleanup(*_args) -> None:
            nonlocal context_installed
            if context_installed:
                return
            context_installed = True
            self.assertIn(JOB_ID, core._inflight)
            core._job_contexts[JOB_ID] = context
            before = len(core._tasks)
            core._spawn_job(JOB_ID, verified=True)
            named_spawn_task_counts.append((before, len(core._tasks)))

        with (
            patch.object(
                seller_core_module.logger,
                "info",
                side_effect=install_context_before_cleanup,
            ),
            patch.object(core, "_spawn_job", wraps=core._spawn_job) as spawn_job,
        ):
            await core._run_job(JOB_ID, verified=False)
            await self._drain_tasks(core)

        self.assertEqual(named_spawn_task_counts, [(0, 0)])
        self.assertEqual(spawn_job.call_count, 2)
        core._do_work_and_submit.assert_awaited_once_with(JOB_ID)
        self.assertIn(JOB_ID, core._handled)
        self.assertNotIn(JOB_ID, core._inflight)
        self.assertNotIn(JOB_ID, core._job_contexts)

    async def test_named_context_handoffs_after_transient_sweep_verification(
        self,
    ) -> None:
        core = SellerCore(run_work=AsyncMock(return_value="report"), generator="test")
        core._sweep = AsyncMock(return_value=None)
        sweep_verify_started = threading.Event()
        release_sweep_verify = threading.Event()
        verify_calls = 0
        verify_lock = threading.Lock()

        def ordered_verify(_job_id: int) -> tuple[bool, str, bool]:
            nonlocal verify_calls
            with verify_lock:
                verify_calls += 1
                call_number = verify_calls
            if call_number == 1:
                sweep_verify_started.set()
                release_sweep_verify.wait(timeout=5)
                return False, "rpc_unavailable", False
            return True, "", False

        core._do_work_and_submit = AsyncMock(
            return_value={"ok": True, "job_id": JOB_ID, "tx_hash": "0xtx"}
        )
        self.verify_signed_job.side_effect = ordered_verify

        core._spawn_job(JOB_ID, verified=False)
        self.assertTrue(await asyncio.to_thread(sweep_verify_started.wait, 2))
        try:
            response = await core.notify_funded(self._signed_request())
        finally:
            release_sweep_verify.set()
        await self._drain_tasks(core)

        self.assertEqual(response["status"], "accepted")
        core._do_work_and_submit.assert_awaited_once_with(JOB_ID)
        self.assertIn(JOB_ID, core._handled)
        self.assertNotIn(JOB_ID, core._inflight)
        self.assertNotIn(JOB_ID, core._job_contexts)

    async def test_named_context_handoffs_after_transient_sweep_job_spec_failure(
        self,
    ) -> None:
        core = SellerCore(run_work=AsyncMock(return_value="report"), generator="test")
        core._sweep = AsyncMock(return_value=None)
        job_spec_started = threading.Event()
        release_job_spec = threading.Event()
        job_spec_calls = 0

        def failing_job_spec(_job_id: int):
            nonlocal job_spec_calls
            job_spec_calls += 1
            if job_spec_calls == 1:
                job_spec_started.set()
                release_job_spec.wait(timeout=5)
                raise RuntimeError("job spec unavailable")
            return _job_spec(required=True)

        core._do_work_and_submit = AsyncMock(
            return_value={"ok": True, "job_id": JOB_ID, "tx_hash": "0xtx"}
        )
        with patch.object(
            seller_core_module.signing,
            "job_spec",
            side_effect=failing_job_spec,
        ):
            core._spawn_job(JOB_ID, verified=False)
            self.assertTrue(await asyncio.to_thread(job_spec_started.wait, 2))
            try:
                response = await core.notify_funded(self._signed_request())
            finally:
                release_job_spec.set()
            await self._drain_tasks(core)

        self.assertEqual(response["status"], "accepted")
        core._do_work_and_submit.assert_awaited_once_with(
            JOB_ID,
            spec=_job_spec(required=True),
        )
        self.assertIn(JOB_ID, core._handled)
        self.assertNotIn(JOB_ID, core._inflight)
        self.assertNotIn(JOB_ID, core._job_contexts)

    async def test_submit_thread_retains_contextless_ownership_after_timeout(
        self,
    ) -> None:
        run_work = AsyncMock(return_value="report")
        core = SellerCore(run_work=run_work, generator="test")
        core._sweep = AsyncMock(return_value=None)
        core._context_deadlines[JOB_ID] = asyncio.get_running_loop().time() - 1.0
        submit_started = threading.Event()
        release_submit = threading.Event()
        timeout_fired = asyncio.Event()
        delivery_timeout = 0.01
        real_wait_for = asyncio.wait_for

        def blocking_submit(*_args, **_kwargs):
            submit_started.set()
            release_submit.wait(timeout=5)
            raise RuntimeError("late submit failure")

        async def controlled_wait_for(awaitable, *, timeout: float):
            coroutine_code = getattr(awaitable, "cr_code", None)
            coroutine_name = getattr(coroutine_code, "co_name", "")
            if (
                timeout == delivery_timeout
                and coroutine_name in {"_fulfill_job", "_do_work_and_submit"}
            ):
                worker = asyncio.create_task(awaitable)
                if not await asyncio.to_thread(submit_started.wait, 2):
                    raise AssertionError("submit_result did not start")
                worker.cancel()
                timeout_fired.set()
                try:
                    await worker
                except asyncio.CancelledError as cancelled:
                    raise TimeoutError from cancelled
                raise AssertionError("delivery ignored timeout cancellation")
            return await real_wait_for(awaitable, timeout=timeout)

        loop = asyncio.get_running_loop()
        loop_errors: list[dict[str, object]] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )
        try:
            with (
                patch.object(
                    seller_core_module,
                    "_JOB_DELIVERY_TIMEOUT_SECONDS",
                    delivery_timeout,
                ),
                patch.object(
                    seller_core_module.asyncio,
                    "wait_for",
                    new=controlled_wait_for,
                ),
                patch.object(
                    seller_core_module.signing,
                    "job_spec",
                    return_value=_job_spec(required=False),
                ),
                patch.object(
                    seller_core_module.signing,
                    "submit_result",
                    side_effect=blocking_submit,
                ) as submit,
            ):
                core._spawn_job(JOB_ID, verified=False)
                await real_wait_for(timeout_fired.wait(), timeout=2)

                self.assertTrue(core.is_busy())
                self.assertIn(JOB_ID, core._inflight)
                self.assertIn(JOB_ID, core._contextless_started)

                response = await core.notify_funded(self._signed_request())

                self.assertEqual(response["status"], "rejected")
                self.assertEqual(response["reason"], "delivery_already_started")
                run_work.assert_awaited_once()
                self.assertEqual(submit.call_count, 1)
        finally:
            release_submit.set()
            await self._drain_tasks(core)
            await asyncio.sleep(0)
            loop.set_exception_handler(previous_handler)

        self.assertFalse(core.is_busy())
        self.assertNotIn(JOB_ID, core._inflight)
        self.assertNotIn(JOB_ID, core._contextless_started)
        self.assertEqual(loop_errors, [])

    async def test_late_success_satisfies_pending_named_handoff_without_redelivery(
        self,
    ) -> None:
        run_work = AsyncMock(return_value="report")
        core = SellerCore(run_work=run_work, generator="test")
        core._sweep = AsyncMock(return_value=None)
        core._context_deadlines[JOB_ID] = (
            asyncio.get_running_loop().time() + 60.0
        )
        core._context_events[JOB_ID] = asyncio.Event()
        job_spec_started = threading.Event()
        release_job_spec = threading.Event()
        submit_started = threading.Event()
        release_submit = threading.Event()
        timeout_fired = asyncio.Event()
        delivery_timeout = 0.01
        real_wait_for = asyncio.wait_for
        timeout_applied = False
        spec_calls = 0
        submit_calls = 0
        call_lock = threading.Lock()
        submitted = SimpleNamespace(
            submit_tx="0xtx",
            deliverable_url="https://result",
        )

        def ordered_job_spec(_job_id: int):
            nonlocal spec_calls
            with call_lock:
                spec_calls += 1
                call_number = spec_calls
            if call_number == 1:
                job_spec_started.set()
                release_job_spec.wait(timeout=5)
            return _job_spec(required=True)

        def ordered_submit(*_args, **_kwargs):
            nonlocal submit_calls
            with call_lock:
                submit_calls += 1
                call_number = submit_calls
            if call_number == 1:
                submit_started.set()
                release_submit.wait(timeout=5)
            return submitted

        async def controlled_wait_for(awaitable, *, timeout: float):
            nonlocal timeout_applied
            coroutine_code = getattr(awaitable, "cr_code", None)
            coroutine_name = getattr(coroutine_code, "co_name", "")
            if (
                not timeout_applied
                and timeout == delivery_timeout
                and coroutine_name in {"_fulfill_job", "_do_work_and_submit"}
            ):
                timeout_applied = True
                worker = asyncio.create_task(awaitable)
                if not await asyncio.to_thread(submit_started.wait, 2):
                    raise AssertionError("submit_result did not start")
                worker.cancel()
                timeout_fired.set()
                try:
                    await worker
                except asyncio.CancelledError as cancelled:
                    raise TimeoutError from cancelled
                raise AssertionError("delivery ignored timeout cancellation")
            return await real_wait_for(awaitable, timeout=timeout)

        with (
            patch.object(
                seller_core_module,
                "_JOB_DELIVERY_TIMEOUT_SECONDS",
                delivery_timeout,
            ),
            patch.object(
                seller_core_module.asyncio,
                "wait_for",
                new=controlled_wait_for,
            ),
            patch.object(
                seller_core_module.signing,
                "job_spec",
                side_effect=ordered_job_spec,
            ),
            patch.object(
                seller_core_module.signing,
                "submit_result",
                side_effect=ordered_submit,
            ) as submit,
        ):
            core._spawn_job(JOB_ID, verified=False)
            self.assertTrue(await asyncio.to_thread(job_spec_started.wait, 2))
            try:
                response = await core.notify_funded(self._signed_request())
            finally:
                release_job_spec.set()

            await real_wait_for(timeout_fired.wait(), timeout=2)
            self.assertTrue(core.is_busy())
            self.assertIn(JOB_ID, core._inflight)
            release_submit.set()
            await self._drain_tasks(core)

        self.assertEqual(response["status"], "accepted")
        run_work.assert_awaited_once()
        self.assertEqual(submit.call_count, 1)
        self.assertIsNotNone(submit.call_args.kwargs["gateway_url"])
        self.assertIn(JOB_ID, core._handled)
        self.assertNotIn(JOB_ID, core._job_contexts)
        self.assertNotIn(JOB_ID, core._context_deadlines)
        self.assertNotIn(JOB_ID, core._context_events)
        self.assertNotIn(JOB_ID, core._contextless_started)
        self.assertNotIn(JOB_ID, core._inflight)
        self.assertNotIn(JOB_ID, core._inflight_verified)
        self.assertNotIn(JOB_ID, core._pending_verified_handoffs)
        self.assertNotIn(JOB_ID, core._late_submit_successes)

        core._spawn_job(JOB_ID, verified=True)
        await asyncio.sleep(0)
        run_work.assert_awaited_once()
        self.assertEqual(submit.call_count, 1)

    async def test_stale_named_validation_cannot_cross_late_success_cleanup(
        self,
    ) -> None:
        run_work = AsyncMock(return_value="report")
        core = SellerCore(run_work=run_work, generator="test")
        core._sweep = AsyncMock(return_value=None)
        core._context_deadlines[JOB_ID] = asyncio.get_running_loop().time() - 1.0
        submit_started = threading.Event()
        release_submit = threading.Event()
        validation_started = threading.Event()
        release_validation = threading.Event()
        timeout_fired = asyncio.Event()
        delivery_timeout = 0.01
        real_wait_for = asyncio.wait_for
        timeout_applied = False
        submit_calls = 0
        submit_lock = threading.Lock()
        submitted = SimpleNamespace(
            submit_tx="0xtx",
            deliverable_url="https://result",
        )

        def ordered_submit(*_args, **_kwargs):
            nonlocal submit_calls
            with submit_lock:
                submit_calls += 1
                call_number = submit_calls
            if call_number == 1:
                submit_started.set()
                release_submit.wait(timeout=5)
            return submitted

        def blocking_gateway_validation(url: str) -> str:
            validation_started.set()
            release_validation.wait(timeout=5)
            return url

        async def controlled_wait_for(awaitable, *, timeout: float):
            nonlocal timeout_applied
            coroutine_code = getattr(awaitable, "cr_code", None)
            coroutine_name = getattr(coroutine_code, "co_name", "")
            if (
                not timeout_applied
                and timeout == delivery_timeout
                and coroutine_name in {"_fulfill_job", "_do_work_and_submit"}
            ):
                timeout_applied = True
                worker = asyncio.create_task(awaitable)
                if not await asyncio.to_thread(submit_started.wait, 2):
                    raise AssertionError("submit_result did not start")
                worker.cancel()
                timeout_fired.set()
                try:
                    await worker
                except asyncio.CancelledError as cancelled:
                    raise TimeoutError from cancelled
                raise AssertionError("delivery ignored timeout cancellation")
            return await real_wait_for(awaitable, timeout=timeout)

        notification: asyncio.Task[dict[str, object]] | None = None
        try:
            with (
                patch.object(
                    seller_core_module,
                    "_JOB_DELIVERY_TIMEOUT_SECONDS",
                    delivery_timeout,
                ),
                patch.object(
                    seller_core_module.asyncio,
                    "wait_for",
                    new=controlled_wait_for,
                ),
                patch.object(
                    seller_core_module.signing,
                    "job_spec",
                    return_value=_job_spec(required=False),
                ),
                patch.object(
                    seller_core_module.signing,
                    "submit_result",
                    side_effect=ordered_submit,
                ) as submit,
                patch.object(
                    seller_core_module,
                    "validate_gateway_url",
                    side_effect=blocking_gateway_validation,
                ),
            ):
                core._spawn_job(JOB_ID, verified=False)
                await real_wait_for(timeout_fired.wait(), timeout=2)
                notification = asyncio.create_task(
                    core.notify_funded(self._signed_request())
                )
                self.assertTrue(
                    await asyncio.to_thread(validation_started.wait, 2)
                )

                release_submit.set()
                await self._drain_tasks(core)
                self.assertNotIn(JOB_ID, core._inflight)
                self.assertNotIn(JOB_ID, core._contextless_started)

                release_validation.set()
                response = await real_wait_for(notification, timeout=2)
                await self._drain_tasks(core)
        finally:
            release_submit.set()
            release_validation.set()
            if notification is not None and not notification.done():
                notification.cancel()
                await asyncio.gather(notification, return_exceptions=True)

        self.assertEqual(response["status"], "rejected")
        self.assertEqual(response["reason"], "delivery_already_started")
        run_work.assert_awaited_once()
        self.assertEqual(submit.call_count, 1)
        self.assertIsNone(submit.call_args.kwargs["gateway_url"])
        self.assertIn(JOB_ID, core._handled)
        self.assertNotIn(JOB_ID, core._late_submit_successes)
        self.assertNotIn(JOB_ID, core._job_contexts)
        self.assertNotIn(JOB_ID, core._context_deadlines)
        self.assertNotIn(JOB_ID, core._context_events)
        self.assertNotIn(JOB_ID, core._contextless_started)
        self.assertNotIn(JOB_ID, core._inflight)
        self.assertNotIn(JOB_ID, core._inflight_verified)
        self.assertNotIn(JOB_ID, core._pending_verified_handoffs)

        core._spawn_job(JOB_ID, verified=True)
        await asyncio.sleep(0)
        run_work.assert_awaited_once()
        self.assertEqual(submit.call_count, 1)

    def test_only_exact_signed_marker_requires_context(self) -> None:
        required = SimpleNamespace(
            terms={"success_criteria": "uomp_notify_context_required_v1"}
        )
        wrong = SimpleNamespace(
            terms={"success_criteria": "UOMP_NOTIFY_CONTEXT_REQUIRED_V1"}
        )
        malformed = SimpleNamespace(terms="not-a-dict")

        self.assertIs(seller_core_module._requires_notify_context(required), True)
        self.assertIs(seller_core_module._requires_notify_context(wrong), False)
        self.assertIs(seller_core_module._requires_notify_context(malformed), False)
        self.assertIs(seller_core_module._requires_notify_context(None), False)

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

    async def test_chain_read_concurrency_is_bounded_before_validation(self) -> None:
        core = RecordingSellerCore()
        requests = [self._signed_request(job_id=JOB_ID + offset) for offset in range(5)]
        active = 0
        maximum_active = 0
        started_reads = 0
        lock = threading.Lock()
        four_started = threading.Event()
        release = threading.Event()

        def blocking_snapshot(job_id: int):
            nonlocal active, maximum_active, started_reads
            with lock:
                active += 1
                started_reads += 1
                maximum_active = max(maximum_active, active)
                if started_reads == seller_core_module._MAX_CHAIN_READ_WORKERS:
                    four_started.set()
            try:
                release.wait(timeout=2)
                return (
                    SimpleNamespace(
                        client=self.client.address,
                        chain_id=CHAIN_ID,
                        verifying_contract=COMMERCE,
                        spec=_job_spec(required=False),
                    ),
                    "",
                    False,
                )
            finally:
                with lock:
                    active -= 1

        cap = seller_core_module._MAX_CHAIN_READ_WORKERS
        first_batch: list[asyncio.Task[dict[str, object]]] = []
        try:
            with (
                patch.object(
                    seller_core_module.signing,
                    "verify_signed_job_snapshot",
                    side_effect=blocking_snapshot,
                    create=True,
                ),
                patch.object(seller_core_module, "_PREVERIFY_TIMEOUT_SECONDS", 5),
            ):
                first_batch = [
                    asyncio.create_task(core.notify_funded(request))
                    for request in requests[:cap]
                ]
                self.assertTrue(await asyncio.to_thread(four_started.wait, 2))

                with patch.object(
                    seller_core_module,
                    "_PREVERIFY_TIMEOUT_SECONDS",
                    0.05,
                ):
                    overflow = await core.notify_funded(requests[cap])

                self.assertEqual(overflow["reason"], "verification_unavailable")
                self.assertIs(overflow["retryable"], True)
                self.assertEqual(started_reads, cap)
                self.assertEqual(maximum_active, cap)
                await asyncio.sleep(0.1)
                self.assertEqual(started_reads, cap)
        finally:
            release.set()
            if first_batch:
                await asyncio.gather(*first_batch, return_exceptions=True)
                await self._drain_tasks(core)

        for _ in range(100):
            if core._chain_read_slots._value == cap:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(core._chain_read_slots._value, cap)

    def test_prune_evicts_the_stalest_idle_jobs_over_cap(self) -> None:
        core = RecordingSellerCore()
        with patch.object(seller_core_module, "_MAX_TRACKED_JOBS", 2):
            for job in (1, 2, 3):
                core._job_contexts[job] = SimpleNamespace(digest=str(job))
                core._job_specs[job] = SimpleNamespace()
                core._context_deadlines[job] = float(job)
                core._context_events[job] = asyncio.Event()
            core._prune_tracked_state()

        # overflow of 1 → evict the single stalest (earliest-deadline) idle job.
        self.assertNotIn(1, core._job_contexts)
        self.assertNotIn(1, core._job_specs)
        self.assertNotIn(1, core._context_deadlines)
        self.assertNotIn(1, core._context_events)
        self.assertEqual(set(core._job_contexts), {2, 3})

    def test_prune_never_evicts_inflight_or_targeted_jobs(self) -> None:
        core = RecordingSellerCore()
        with patch.object(seller_core_module, "_MAX_TRACKED_JOBS", 1):
            core._job_contexts[1] = SimpleNamespace(digest="1")
            core._context_deadlines[1] = 1.0
            core._inflight.add(1)
            core._job_contexts[2] = SimpleNamespace(digest="2")
            core._context_deadlines[2] = 2.0
            core._prune_tracked_state(protect=2)

        # Both are protected (one in-flight, one the current target), so neither
        # is dropped even though the set is over the cap.
        self.assertEqual(set(core._job_contexts), {1, 2})


if __name__ == "__main__":
    unittest.main()
