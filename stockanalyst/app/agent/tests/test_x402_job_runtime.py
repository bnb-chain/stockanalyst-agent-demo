from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from eth_utils import to_checksum_address

from x402_job_service import (
    X402JobError,
    X402JobService,
    load_job_token_secret,
)
from x402_job_store import X402JobStore
from x402_verify import U_TOKEN_BSC_TESTNET, VerifiedPayment


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
        "U_TOKEN_BSC_TESTNET": U_TOKEN_BSC_TESTNET,
        "load_job_token_secret": load_job_token_secret,
        "_settle_via_facilitator": settle or AsyncMock(),
        "report_competition_call": report or AsyncMock(return_value=True),
        "get_8183_client": get_client or (lambda: None),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), MAIN_PATH, "exec"), namespace)
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
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), MAIN_PATH, "exec"),
        namespace,
    )
    return namespace["_load_runtime_secrets"]


class X402JobRuntimeTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_authorization_reader_uses_active_client_and_token(self) -> None:
        observed: list[tuple[str, bytes]] = []

        class AuthorizationState:
            def call(self) -> bool:
                return True

        class Functions:
            def authorizationState(
                self,
                address: str,
                nonce: bytes,
            ) -> AuthorizationState:
                observed.append((address, nonce))
                return AuthorizationState()

        contract = SimpleNamespace(functions=Functions())
        eth = SimpleNamespace(
            contract=lambda *, address, abi: (
                contract
                if address == U_TOKEN_BSC_TESTNET and abi
                else (_ for _ in ()).throw(AssertionError("wrong token contract"))
            )
        )
        client = SimpleNamespace(
            w3=SimpleNamespace(
                eth=eth,
                to_checksum_address=to_checksum_address,
            )
        )
        build = _load_runtime_functions(
            get_client=lambda: client,
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
        )

        self.assertTrue(await service._authorization_used(payment))
        self.assertEqual(
            observed,
            [
                (
                    to_checksum_address(payment.from_address),
                    payment.nonce_bytes,
                )
            ],
        )

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
