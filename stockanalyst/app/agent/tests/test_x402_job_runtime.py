from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import sys
import unittest
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from eth_utils import to_checksum_address
from web3 import Web3

from report_renderer import render_report
from report_schema import StockReport
from tests.test_x402_job_service import (
    NOW as JOB_NOW,
)
from tests.test_x402_job_service import (
    MemoryJobStore,
    seed_settling_job,
)
from x402_job_service import (
    X402JobError,
    X402JobService,
    load_job_token_secret,
)
from x402_job_store import X402JobStore
from x402_promo import promo_free_mode
from x402_tokens import U_TOKEN, USD1_TOKEN, token_by_asset
from x402_verify import U_TOKEN_ADDRESS, VerifiedPayment

MAIN_PATH = Path(__file__).parents[1] / "main.py"
STUDIO_PATH = Path(__file__).parents[1] / "studio.toml"


class FakeS3:
    pass


def _load_runtime_functions(
    *,
    settle: AsyncMock | None = None,
    report: AsyncMock | None = None,
    get_client=None,
) -> dict[str, Any]:
    """Load only the pure runtime helpers without importing the real ADK runner."""
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    wanted = {"build_x402_job_service", "_runtime_is_busy"}
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in wanted
    ]
    namespace: dict[str, Any] = {
        "Mapping": Mapping,
        "Any": Any,
        "asyncio": asyncio,
        "X402JobError": X402JobError,
        "X402JobService": X402JobService,
        "X402JobStore": X402JobStore,
        "VerifiedPayment": VerifiedPayment,
        "U_TOKEN_ADDRESS": U_TOKEN_ADDRESS,
        "token_by_asset": token_by_asset,
        "load_job_token_secret": load_job_token_secret,
        "promo_free_mode": promo_free_mode,
        "_settle_via_facilitator": settle or AsyncMock(),
        "report_competition_call": report or AsyncMock(return_value=True),
        "get_8183_client": get_client or (lambda: None),
    }
    exec(  # noqa: S102 — isolate selected main.py helpers without importing main
        compile(ast.Module(body=functions, type_ignores=[]), MAIN_PATH, "exec"),
        namespace,
    )
    return namespace


def _load_runtime_secrets_function():
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_load_runtime_secrets"
    )
    namespace = {"json": json, "os": os}
    exec(  # noqa: S102 — isolate _load_runtime_secrets without importing main
        compile(ast.Module(body=[function], type_ignores=[]), MAIN_PATH, "exec"),
        namespace,
    )
    return namespace["_load_runtime_secrets"]


def _load_stream_functions(final_text: str) -> dict[str, Any]:
    """Load the real stream path with only the external ADK runner replaced."""

    class SessionService:
        def __init__(self) -> None:
            self.session = None

        async def get_session(self, **_kwargs: Any) -> object | None:
            return self.session

        async def create_session(self, **_kwargs: Any) -> object:
            self.session = object()
            return self.session

    class Runner:
        app_name = "test-agent"

        def __init__(self) -> None:
            self.session_service = SessionService()

        async def run_async(self, **_kwargs: Any) -> AsyncGenerator[Any, None]:
            part = SimpleNamespace(
                text=final_text,
                thought=False,
                function_call=None,
                function_response=None,
            )
            yield SimpleNamespace(
                content=SimpleNamespace(parts=[part]),
                is_final_response=lambda: True,
            )

    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    wanted = {
        "_extract_json",
        "_raise_for_provider_rate_limit",
        "_try_parse_report",
        "_stream_runner",
    }
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in wanted
    ]
    gtypes = SimpleNamespace(
        Content=lambda **kwargs: SimpleNamespace(**kwargs),
        Part=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    namespace: dict[str, Any] = {
        "Any": Any,
        "AsyncGenerator": AsyncGenerator,
        "Mapping": Mapping,
        "StockReport": StockReport,
        "X402JobError": X402JobError,
        "_log": logging.getLogger("test.x402.stream"),
        "asyncio": asyncio,
        "gtypes": gtypes,
        "json": json,
        "re": re,
        "render_report": render_report,
        "runner": Runner(),
    }
    exec(  # noqa: S102 — exercise selected real main.py stream functions
        compile(ast.Module(body=functions, type_ignores=[]), MAIN_PATH, "exec"),
        namespace,
    )
    return namespace


class X402JobRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_final_text_emits_no_report_event(self) -> None:
        stream_runner = _load_stream_functions("")["_stream_runner"]

        events = [
            event
            async for event in stream_runner(
                "analyze",
                "session-empty",
                ["BNB"],
            )
        ]

        self.assertNotIn("report", [name for name, _data in events])
        self.assertEqual(events[-1], ("done", {}))

    async def test_non_json_final_text_is_delivered_unchanged(self) -> None:
        raw = "plain text report; not JSON"
        stream_runner = _load_stream_functions(raw)["_stream_runner"]

        events = [
            event
            async for event in stream_runner(
                "analyze",
                "session-raw",
                ["BNB"],
            )
        ]

        reports = [data for name, data in events if name == "report"]
        self.assertEqual(reports, [{"content": raw, "format": "text"}])

    def test_parse_failure_log_never_contains_model_output(self) -> None:
        sensitive = "sensitive-model-output-fragment"
        try_parse_report = _load_stream_functions("")["_try_parse_report"]

        with self.assertLogs("test.x402.stream", level="WARNING") as captured:
            parsed = try_parse_report(
                json.dumps({"executive_summary": sensitive})
            )

        self.assertIsNone(parsed)
        logged = "\n".join(captured.output)
        self.assertNotIn(sensitive, logged)
        self.assertNotIn("model-output-fragment", logged)
        self.assertNotIn("executive_summary", logged)
        self.assertNotIn("input_value", logged)
        self.assertEqual(
            captured.output,
            [
                (
                    "WARNING:test.x402.stream:report parse/validation failed "
                    "(ValidationError)"
                )
            ],
        )

    def test_runtime_loads_job_token_from_dedicated_secret(self) -> None:
        calls: list[str] = []

        class FakeSecretsManager:
            def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
                calls.append(SecretId)
                if SecretId == "job-token-secret":
                    return {"SecretString": "j" * 64}
                raise AssertionError(SecretId)

        fake_boto3 = SimpleNamespace(client=lambda service: FakeSecretsManager())
        load_runtime_secrets = _load_runtime_secrets_function()
        environment = {"X402_JOB_TOKEN_SECRET_ID": "job-token-secret"}

        with (
            patch.dict(os.environ, environment, clear=True),
            patch.dict(sys.modules, {"boto3": fake_boto3}),
        ):
            load_runtime_secrets()

            self.assertEqual(os.environ["X402_JOB_TOKEN_SECRET"], "j" * 64)

        self.assertEqual(calls, ["job-token-secret"])

    def test_runtime_does_not_override_existing_job_token(self) -> None:
        class FakeSecretsManager:
            def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
                if SecretId != "job-token-secret":
                    raise AssertionError(SecretId)
                return {"SecretString": "new-token"}

        fake_boto3 = SimpleNamespace(client=lambda service: FakeSecretsManager())
        load_runtime_secrets = _load_runtime_secrets_function()
        environment = {
            "X402_JOB_TOKEN_SECRET_ID": "job-token-secret",
            "X402_JOB_TOKEN_SECRET": "existing-token",
        }

        with (
            patch.dict(os.environ, environment, clear=True),
            patch.dict(sys.modules, {"boto3": fake_boto3}),
        ):
            load_runtime_secrets()

            self.assertEqual(
                os.environ["X402_JOB_TOKEN_SECRET"],
                "existing-token",
            )

    def test_partial_configuration_fails_startup(self) -> None:
        build = _load_runtime_functions()["build_x402_job_service"]

        with self.assertRaisesRegex(X402JobError, "must be set together"):
            build(
                {"X402_JOB_S3_BUCKET": "private-jobs"},
                stream_work=AsyncMock(),
            )
        with self.assertRaisesRegex(X402JobError, "must be set together"):
            build(
                {"X402_JOB_TOKEN_SECRET": "x" * 32},
                stream_work=AsyncMock(),
            )

    def test_absent_configuration_disables_only_async_routes(self) -> None:
        build = _load_runtime_functions()["build_x402_job_service"]

        self.assertIsNone(build({}, stream_work=AsyncMock()))

    def test_configured_factory_builds_service_with_pause_switch(self) -> None:
        settle = AsyncMock()
        report = AsyncMock(return_value=True)
        build = _load_runtime_functions(
            settle=settle,
            report=report,
        )["build_x402_job_service"]
        stream_work = AsyncMock()

        service = build(
            {
                "X402_JOB_S3_BUCKET": "private-jobs",
                "X402_JOB_S3_PREFIX": "private/x402",
                "X402_JOB_TOKEN_SECRET": "x" * 32,
                "X402_ASYNC_ACCEPT_NEW_JOBS": "0",
            },
            stream_work=stream_work,
            s3_client=FakeS3(),
        )

        self.assertIsInstance(service, X402JobService)
        assert service is not None
        self.assertFalse(service.accept_new_jobs)
        self.assertEqual(service._store.config.bucket, "private-jobs")
        self.assertEqual(service._store.config.prefix, "private/x402")
        self.assertIs(service._settle, settle)
        self.assertIs(service._report, report)
        self.assertIs(service._stream_work, stream_work)

    def test_configured_factory_enables_only_exact_promotional_flag(self) -> None:
        build = _load_runtime_functions()["build_x402_job_service"]

        service = build(
            {
                "X402_JOB_S3_BUCKET": "private-jobs",
                "X402_JOB_TOKEN_SECRET": "x" * 32,
                "X402_PROMO_FREE_MODE": "1",
            },
            stream_work=AsyncMock(),
            s3_client=FakeS3(),
        )

        assert service is not None
        self.assertTrue(service.promo_free)

        with self.assertRaisesRegex(RuntimeError, "X402_PROMO_FREE_MODE"):
            build(
                {
                    "X402_JOB_S3_BUCKET": "private-jobs",
                    "X402_JOB_TOKEN_SECRET": "x" * 32,
                    "X402_PROMO_FREE_MODE": "true",
                },
                stream_work=AsyncMock(),
                s3_client=FakeS3(),
            )

    async def test_stale_recovery_queries_the_payment_token_contract(self) -> None:
        for selected_token in (U_TOKEN, USD1_TOKEN):
            with self.subTest(token=selected_token.symbol):
                real_web3 = Web3()
                observed_contracts: list[str] = []
                observed_calls: list[tuple[str, bytes]] = []
                states = {
                    U_TOKEN.address.lower(): selected_token is U_TOKEN,
                    USD1_TOKEN.address.lower(): selected_token is USD1_TOKEN,
                }

                class AuthorizationState:
                    def __init__(self, used: bool) -> None:
                        self._used = used

                    def call(self) -> bool:
                        return self._used

                class Functions:
                    def __init__(self, used: bool) -> None:
                        self._used = used

                    def authorizationState(
                        self,
                        address: str,
                        nonce: bytes,
                        observed_calls: list[tuple[str, bytes]] = observed_calls,
                    ) -> AuthorizationState:
                        observed_calls.append((address, nonce))
                        return AuthorizationState(self._used)

                def contract(
                    *,
                    address: str,
                    abi: list[dict],
                    real_web3: Web3 = real_web3,
                    observed_contracts: list[str] = observed_contracts,
                    states: dict[str, bool] = states,
                ) -> Any:
                    self.assertTrue(abi)
                    validated = real_web3.eth.contract(
                        address=address,
                        abi=abi,
                    )
                    self.assertEqual(validated.address, address)
                    observed_contracts.append(address)
                    used = states[address.lower()]
                    return SimpleNamespace(functions=Functions(used))

                client = SimpleNamespace(
                    w3=SimpleNamespace(
                        eth=SimpleNamespace(contract=contract),
                        to_checksum_address=to_checksum_address,
                    )
                )
                settle = AsyncMock(
                    side_effect=AssertionError(
                        "used authorization must not be settled again"
                    )
                )
                build = _load_runtime_functions(
                    settle=settle,
                    get_client=lambda client=client: client,
                )["build_x402_job_service"]
                service = build(
                    {
                        "X402_JOB_S3_BUCKET": "private-jobs",
                        "X402_JOB_TOKEN_SECRET": "x" * 32,
                    },
                    stream_work=AsyncMock(),
                    s3_client=FakeS3(),
                )
                assert service is not None
                store = MemoryJobStore()
                service._store = store
                service._clock = lambda: JOB_NOW
                payment = VerifiedPayment(
                    proof={},
                    from_address="0xabcdef1234567890abcdef1234567890abcdef12",
                    to_address="0x" + "34" * 20,
                    value=210_000_000_000_000_000,
                    valid_after=0,
                    valid_before=JOB_NOW // 1000 + 600,
                    nonce="0x" + "56" * 32,
                    nonce_bytes=bytes.fromhex("56" * 32),
                    asset=selected_token.address.lower(),
                    token_symbol=selected_token.symbol,
                )
                stored = await seed_settling_job(
                    service,
                    store,
                    lease_expires_at=JOB_NOW - 1,
                    payment=payment,
                )

                reconciled = await service._reconcile_settling(
                    stored,
                    payment,
                    "proof",
                )

                self.assertEqual(reconciled.record["status"], "queued")
                self.assertEqual(
                    [address.lower() for address in observed_contracts],
                    [payment.asset.lower()],
                )
                self.assertEqual(observed_contracts, [selected_token.address])
                self.assertEqual(
                    observed_calls,
                    [
                        (
                            to_checksum_address(payment.from_address),
                            payment.nonce_bytes,
                        )
                    ],
                )
                settle.assert_not_awaited()

    async def test_authorization_reader_rejects_unknown_asset(self) -> None:
        build = _load_runtime_functions(
            get_client=lambda: SimpleNamespace(),
        )["build_x402_job_service"]
        service = build(
            {
                "X402_JOB_S3_BUCKET": "private-jobs",
                "X402_JOB_TOKEN_SECRET": "x" * 32,
            },
            stream_work=AsyncMock(),
            s3_client=FakeS3(),
        )
        assert service is not None
        payment = VerifiedPayment(
            proof={},
            from_address="0xabcdef1234567890abcdef1234567890abcdef12",
            to_address="0x" + "34" * 20,
            value=1,
            valid_after=0,
            valid_before=1,
            nonce="0x" + "56" * 32,
            nonce_bytes=bytes.fromhex("56" * 32),
            asset="0x" + "99" * 20,
            token_symbol="UNKNOWN",
        )

        with self.assertRaisesRegex(X402JobError, "invalid_payment_asset"):
            await service._authorization_used(payment)

    def test_runtime_busy_combines_erc8183_and_x402_activity(self) -> None:
        runtime_is_busy = _load_runtime_functions()["_runtime_is_busy"]

        self.assertFalse(
            runtime_is_busy(
                SimpleNamespace(is_busy=lambda: False),
                None,
            )
        )
        self.assertTrue(
            runtime_is_busy(
                SimpleNamespace(is_busy=lambda: True),
                SimpleNamespace(is_busy=lambda: False),
            )
        )
        self.assertTrue(
            runtime_is_busy(
                SimpleNamespace(is_busy=lambda: False),
                SimpleNamespace(is_busy=lambda: True),
            )
        )

    def test_main_constructs_one_service_and_injects_one_x402_handler(
        self,
    ) -> None:
        main_text = MAIN_PATH.read_text(encoding="utf-8")
        self.assertLess(
            main_text.index("\n_load_runtime_secrets()\n"),
            main_text.index("\nx402_jobs = build_x402_job_service(\n"),
        )

        tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
        service_builds = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "x402_jobs"
                for target in node.targets
            )
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "build_x402_job_service"
        ]
        handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "X402Handler"
        ]

        self.assertEqual(len(service_builds), 1)
        self.assertEqual(len(handlers), 1)
        for handler in handlers:
            job_service = next(
                keyword.value
                for keyword in handler.keywords
                if keyword.arg == "job_service"
            )
            self.assertIsInstance(job_service, ast.Name)
            self.assertEqual(job_service.id, "x402_jobs")

    def test_factory_is_not_exposed_as_an_llm_tool(self) -> None:
        tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
        agent_call = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Agent"
        )
        tools = next(
            keyword.value
            for keyword in agent_call.keywords
            if keyword.arg == "tools"
        )
        tool_names = {
            node.id for node in ast.walk(tools) if isinstance(node, ast.Name)
        }

        self.assertNotIn("build_x402_job_service", tool_names)

    def test_studio_documents_async_configuration_without_values(self) -> None:
        lines = STUDIO_PATH.read_text(encoding="utf-8").splitlines()
        expected = {
            "X402_JOB_S3_BUCKET",
            "X402_JOB_S3_PREFIX",
            "X402_JOB_TOKEN_SECRET",
            "X402_ASYNC_ACCEPT_NEW_JOBS",
        }

        for name in expected:
            matches = [line for line in lines if name in line]
            self.assertTrue(matches, name)
            self.assertTrue(all(line.lstrip().startswith("#") for line in matches))


if __name__ == "__main__":
    unittest.main()
