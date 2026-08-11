import ast
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[2]
ROOT_README = ROOT / "README.md"
BUYER_ENV = ROOT / "buyer-client" / ".env.example"
BUYER_README = ROOT / "buyer-client" / "README.md"
STOCKANALYST_README = ROOT / "stockanalyst" / "README.md"
AGENT_MAIN = ROOT / "stockanalyst" / "app" / "agent" / "main.py"
CUSTOM_DOMAIN_CUTOVER_STATE = (
    "The old execute-api endpoint remains enabled during certificate/DNS/custom-domain "
    "validation and is disabled only after successful final cutover verification."
)
STUDIO = ROOT / "stockanalyst" / "app" / "agent" / "studio.toml"
TOKENS = ROOT / "stockanalyst" / "app" / "agent" / "x402_tokens.py"
VERIFIER = ROOT / "stockanalyst" / "app" / "agent" / "x402_verify.py"
RUNTIME_X402_SOURCE_ROOT = ROOT / "stockanalyst" / "app" / "agent"
GATEWAY_X402_SOURCE_ROOT = ROOT / "gateway" / "x402_lambda" / "src"
BUYER_X402_SOURCE_ROOT = ROOT / "buyer-client" / "src"
RUNTIME_X402_HANDLER = RUNTIME_X402_SOURCE_ROOT / "x402_handler.py"
GATEWAY_X402_ENVELOPE = GATEWAY_X402_SOURCE_ROOT / "envelope.py"
GATEWAY_X402_CLIENT = GATEWAY_X402_SOURCE_ROOT / "agentcore_client.py"
BUYER_X402_ASYNC_CLIENT = BUYER_X402_SOURCE_ROOT / "x402-async-client.ts"
LIVE_PUBLIC_X402_DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "stockanalyst" / "README.md",
    ROOT / "buyer-client" / "README.md",
    ROOT / "stockanalyst" / "app" / "agent" / "README.md",
    ROOT / "buyer-client" / ".env.example",
    ROOT / "stockanalyst" / "app" / "agent" / "studio.toml",
)
X402_SOURCE_ROOTS = (
    RUNTIME_X402_SOURCE_ROOT,
    GATEWAY_X402_SOURCE_ROOT,
)
EXCLUDED_SOURCE_DIRECTORIES = {
    "__pycache__",
    "build",
    "dist",
    "generated",
    "site-packages",
    "tests",
    "venv",
}
LIVE_X402_SOURCE_EXCLUDED_SUFFIXES = (".test.ts", ".spec.ts")
LEGACY_X402_HEADER = re.compile(
    r"(?<![A-Za-z0-9-])x-payment(?:-required|-response)?(?![A-Za-z0-9-])",
    re.IGNORECASE,
)


def iter_x402_python_sources(
    roots: tuple[Path, ...] = X402_SOURCE_ROOTS,
):
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            relative_parts = path.relative_to(root).parts
            if any(
                part.startswith(".") or part in EXCLUDED_SOURCE_DIRECTORIES
                for part in relative_parts
            ):
                continue
            yield path


def iter_live_x402_production_sources():
    source_roots = (
        (RUNTIME_X402_SOURCE_ROOT, ".py"),
        (GATEWAY_X402_SOURCE_ROOT, ".py"),
        (BUYER_X402_SOURCE_ROOT, ".ts"),
    )
    for root, suffix in source_roots:
        for path in sorted(root.rglob(f"*{suffix}")):
            relative_parts = path.relative_to(root).parts
            if any(
                part.startswith(".") or part in EXCLUDED_SOURCE_DIRECTORIES
                for part in relative_parts
            ):
                continue
            if path.name.endswith(LIVE_X402_SOURCE_EXCLUDED_SUFFIXES):
                continue
            yield path


def forbidden_x402_dependency_imports(source: str) -> list[str]:
    forbidden: set[str] = set()

    def inspect_name(name: str) -> None:
        parts = {part.casefold() for part in name.split(".")}
        if parts.intersection({"redis", "dynamodb"}):
            forbidden.add(name)

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                inspect_name(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                inspect_name(node.module)
            for alias in node.names:
                inspect_name(alias.name)
    return sorted(forbidden)


class MainnetInfrastructureContractTests(unittest.TestCase):
    def test_public_x402_contract_uses_only_v2_headers(self) -> None:
        live_paths = (*LIVE_PUBLIC_X402_DOCUMENTS, *iter_live_x402_production_sources())
        for path in live_paths:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIsNone(
                    LEGACY_X402_HEADER.search(path.read_text(encoding="utf-8")),
                    f"legacy x402 header in live surface: {path}",
                )

    def test_public_x402_components_expose_the_v2_header_contract(self) -> None:
        runtime_handler = RUNTIME_X402_HANDLER.read_text(encoding="utf-8")
        gateway_envelope = GATEWAY_X402_ENVELOPE.read_text(encoding="utf-8")
        gateway_client = GATEWAY_X402_CLIENT.read_text(encoding="utf-8")
        buyer_async_client = BUYER_X402_ASYNC_CLIENT.read_text(encoding="utf-8")

        for header in ("payment-required", "payment-signature", "payment-response"):
            self.assertRegex(runtime_handler, rf"(?i){re.escape(header)}")
            self.assertRegex(buyer_async_client, rf"(?i){re.escape(header)}")
        self.assertRegex(gateway_envelope, r"(?i)payment-signature")
        for header in ("payment-required", "payment-response"):
            self.assertRegex(gateway_client, rf"(?i){re.escape(header)}")

    def test_mainnet_paid_analysis_prices_are_exactly_point_21(self) -> None:
        config = tomllib.loads(STUDIO.read_text(encoding="utf-8"))
        erc8183 = config["payments"]["erc8183"]
        x402 = config["payments"]["x402"]["seller"]
        self.assertEqual(erc8183["price"], "210000000000000000")
        self.assertEqual(erc8183["min_price"], "210000000000000000")
        self.assertEqual(erc8183["max_price"], "5000000000000000000")
        self.assertEqual(x402["price_wei"], "210000000000000000")
        self.assertEqual(x402["min_price_wei"], "210000000000000000")

        buyer_readme = BUYER_README.read_text(encoding="utf-8")
        stock_readme = STOCKANALYST_README.read_text(encoding="utf-8")
        agent_readme = (ROOT / "stockanalyst" / "app" / "agent" / "README.md").read_text(
            encoding="utf-8"
        )
        main_source = AGENT_MAIN.read_text(encoding="utf-8")

        self.assertIn("0.21 U or 0.21 USD1", buyer_readme)
        self.assertIn("Payment: 0.21 USD1", buyer_readme)
        self.assertIn("0.21 U per analysis", agent_readme)
        self.assertIn("0.21 U（托管）", stock_readme)
        self.assertIn("0.21 U 或 0.21 USD1", stock_readme)
        self.assertIn("Paid tier (0.21 U)", main_source)

    def test_root_readme_describes_current_point_21_prices(self) -> None:
        root_readme = ROOT_README.read_text(encoding="utf-8")

        for current_guidance in (
            "0.21 U or 0.21 USD1",
            "sign 0.21-U/USD1 EIP-712 proof",
            "sign quote → 0.21 U",
            "| Cost | 0 U | 0.21 U or 0.21 USD1 | 0.21 U |",
            "signed quote 0.21 U",
            "+ ≥ 0.21 U (for ERC-8183)",
            "# paid: 0.21 U or 0.21 USD1",
            "# paid: 0.21 U, full analysis, ERC-8183 trustless",
        ):
            self.assertIn(current_guidance, root_readme)

    def test_mainnet_x402_examples_and_manual_promo_runbook_are_consistent(self) -> None:
        documents = (
            ROOT_README.read_text(encoding="utf-8"),
            BUYER_ENV.read_text(encoding="utf-8"),
            BUYER_README.read_text(encoding="utf-8"),
            STOCKANALYST_README.read_text(encoding="utf-8"),
        )
        for documentation in documents:
            self.assertIn(
                "X402_ENDPOINT=https://stock-agent.bnbchain.org", documentation
            )
            self.assertIn(CUSTOM_DOMAIN_CUTOVER_STATE, documentation)
            self.assertNotIn("xolw2dzbw2.execute-api", documentation)
            self.assertNotIn(
                "X402_ENDPOINT=https://<api-id>.execute-api.us-east-1.amazonaws.com/mainnet",
                documentation,
            )
        studio = STUDIO.read_text(encoding="utf-8")
        self.assertIn(
            "X402_GATEWAY_PUBLIC_BASE_URL=https://stock-agent.bnbchain.org",
            studio,
        )
        self.assertNotIn("xolw2dzbw2.execute-api", studio)
        self.assertIn("bag env set X402_PROMO_FREE_MODE 1", documents[2])
        self.assertIn("bag env set X402_PROMO_FREE_MODE 0", documents[2])

    def test_async_promotional_runbook_is_proofless_and_ip_limited(self) -> None:
        buyer = BUYER_README.read_text(encoding="utf-8")
        seller = STOCKANALYST_README.read_text(encoding="utf-8")
        expected = (
            "When `X402_PROMO_FREE_MODE=1`, callers POST directly without a "
            "wallet or `Payment-Signature`."
        )
        quota = (
            "Every accepted POST creates a new job and consumes one of the "
            "30 requests per trusted IP in the rolling 24-hour window, "
            "including an identical retry."
        )
        rollback = (
            "Setting `X402_PROMO_FREE_MODE=0` restores the paid U/USD1 "
            "HTTP 402 flow."
        )

        for document in (buyer, seller):
            normalized = " ".join(document.split())
            self.assertIn(expected, normalized)
            self.assertIn(quota, normalized)
            self.assertIn(rollback, normalized)

    def test_runtime_configuration_is_mainnet_only(self) -> None:
        studio = STUDIO.read_text(encoding="utf-8")

        self.assertIn('default = "bsc-mainnet"', studio)
        self.assertIn('kind = "s3"', studio)
        self.assertIn(
            'currency = "0xcE24439F2D9C6a2289F741120FE202248B666666"',
            studio,
        )
        self.assertIn(
            "bag env set U_TOKEN_DOMAIN_NAME    United Stables",
            studio,
        )
        self.assertIn("bag env set U_TOKEN_DOMAIN_VERSION 1", studio)
        self.assertIn("bag env set X402_CHAIN_ID      56", studio)
        self.assertIn(
            "bag env set X402_TOKEN_ADDRESS 0xcE24439F2D9C6a2289F741120FE202248B666666",
            studio,
        )
        self.assertIn(
            "bag env set B402_PAY_TO_ADDRESS 0x15958aad30b758dAbfbB9788Da69dfcd56e89078",
            studio,
        )
        self.assertNotIn("bsc-testnet", studio)
        self.assertNotIn("bnbagent-api.bnbchain.world", studio)
        self.assertIn(
            'runtime_arn = "arn:aws:bedrock-agentcore:us-east-1:201243086760:'
            'runtime/stockanalystmainnet_stockanalystmainnet-GVFwe5Etrj"',
            studio,
        )

    def test_mainnet_x402_registry_and_promotional_controls_are_explicit(self) -> None:
        studio = STUDIO.read_text(encoding="utf-8")
        tokens = TOKENS.read_text(encoding="utf-8")
        verifier = VERIFIER.read_text(encoding="utf-8")

        for value in (
            'address="0xcE24439F2D9C6a2289F741120FE202248B666666"',
            'domain_name="United Stables"',
            'address="0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d"',
            'domain_name="World Liberty Financial USD"',
        ):
            self.assertIn(value, tokens)
        self.assertEqual(tokens.count("decimals=18"), 2)
        self.assertIn("bag env set X402_PROMO_FREE_MODE 1", studio)
        self.assertIn("bag env set X402_PROMO_FREE_MODE 0", studio)
        self.assertIn(
            "Promotional access is proofless: paymentRequired=false, accepts=[],",
            studio,
        )
        self.assertIn("and no payment proof is required", studio)
        self.assertNotIn("still require an EIP-3009 wallet signature", studio)
        self.assertNotIn("MIN_PRICE_WEI", verifier)
        self.assertIn("U_TOKEN_ADDRESS = U_TOKEN.address", verifier)
        self.assertNotRegex(
            verifier,
            r"U_TOKEN_ADDRESS\s*=\s*_resolve_x402_token_address",
        )

    def test_x402_runtime_sources_do_not_import_shared_rate_limit_stores(self) -> None:
        for path in iter_x402_python_sources():
            self.assertEqual(
                forbidden_x402_dependency_imports(path.read_text(encoding="utf-8")),
                [],
                str(path),
            )

    def test_dependency_import_scan_rejects_redis_and_dynamodb_ast_forms(self) -> None:
        for source in (
            "import redis\n",
            "import os, redis\n",
            "from redis import Redis\n",
            "from . import redis\n",
            "import boto3.dynamodb.client\n",
            "from boto3.dynamodb import client\n",
            "from boto3 import dynamodb\n",
        ):
            self.assertTrue(forbidden_x402_dependency_imports(source), source)

    def test_dependency_import_scan_does_not_match_similar_module_names(self) -> None:
        source = "import predis\nimport rediscovery\nfrom tools import predis\n"

        self.assertEqual(forbidden_x402_dependency_imports(source), [])

    def test_dependency_source_scan_is_recursive_and_excludes_non_source_trees(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            included = (root / "nested" / "worker.py", root / "main.py")
            excluded = (
                root / ".venv" / "redis.py",
                root / ".cache" / "cached.py",
                root / ".hidden.py",
                root / "tests" / "test_worker.py",
                root / "generated" / "client.py",
                root / "__pycache__" / "cached.py",
            )
            for path in (*included, *excluded):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("pass\n", encoding="utf-8")

            actual = list(iter_x402_python_sources((root,)))

        self.assertEqual(actual, sorted(included))


if __name__ == "__main__":
    unittest.main()
