import ast
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import tomllib
import unittest
import yaml


ROOT = Path(__file__).resolve().parents[2]
ROOT_README = ROOT / "README.md"
BUYER_ENV = ROOT / "buyer-client" / ".env.example"
BUYER_README = ROOT / "buyer-client" / "README.md"
STOCKANALYST_README = ROOT / "stockanalyst" / "README.md"
AGENT_README = ROOT / "stockanalyst" / "app" / "agent" / "README.md"
X402_API_USAGE = ROOT / "docs" / "x402-api-usage.md"
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
PUBLIC_FOUR_TOKEN_DOCUMENTS = (
    ROOT_README,
    STOCKANALYST_README,
    AGENT_README,
    BUYER_README,
    X402_API_USAGE,
)
TRACKED_X402_GUIDANCE = (
    *PUBLIC_FOUR_TOKEN_DOCUMENTS,
    BUYER_ENV,
    STUDIO,
)
PAID_TOKEN_TABLE = """| Token | BSC address | Method | Price |
| --- | --- | --- | --- |
| U | `0xcE24439F2D9C6a2289F741120FE202248B666666` | `eip3009` | 0.21 U |
| USD1 | `0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d` | `eip3009` | 0.21 USD1 |
| USDC | `0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d` | `permit2-exact` | 0.21 USDC |
| USDT | `0x55d398326f99059fF775485246999027B3197955` | `permit2-exact` | 0.21 USDT |"""
CANONICAL_PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
EXPECTED_RUNTIME_SECRET_NAMES = frozenset(
    {
        "ALPHA_VANTAGE_API_KEY",
        "B402_ACCESS_TOKEN",
        "B402_BASE_URL",
        "B402_CLIENT_ID",
        "B402_PAY_TO_ADDRESS",
        "B402_PRIVATE_KEY",
        "COMPETITION_AI_CALLS_URL",
        "COMPETITION_INTERNAL_TOKEN",
        "DELIVERABLE_PUBLIC_BASE",
        "DELIVERABLE_S3_BUCKET",
        "DELIVERABLE_S3_PREFIX",
        "GNEWS_API_KEY",
        "OAUTH_SCOPE",
        "OAUTH_TOKEN_URL",
        "OPENROUTER_API_KEY",
        "U_TOKEN_DOMAIN_NAME",
        "U_TOKEN_DOMAIN_VERSION",
        "WALLET_KEYSTORE_JSON",
        "WALLET_PASSWORD",
        "X402_ASYNC_ACCEPT_NEW_JOBS",
        "X402_CHAIN_ID",
        "X402_JOB_S3_BUCKET",
        "X402_JOB_S3_PREFIX",
        "X402_JOB_TOKEN_SECRET",
        "X402_PROMO_FREE_MODE",
        "X402_TOKEN_ADDRESS",
    }
)
INFRASTRUCTURE_RESOURCE_TYPES = {
    ROOT / "infra" / "agentcore-mainnet-fixed-egress.yaml": {
        "EgressElasticIp": "AWS::EC2::EIP",
        "PrivateSubnet": "AWS::EC2::Subnet",
        "NatGateway": "AWS::EC2::NatGateway",
        "PrivateRouteTable": "AWS::EC2::RouteTable",
        "PrivateDefaultRoute": "AWS::EC2::Route",
        "PrivateSubnetRouteTableAssociation": "AWS::EC2::SubnetRouteTableAssociation",
        "RuntimeSecurityGroup": "AWS::EC2::SecurityGroup",
        "S3GatewayEndpoint": "AWS::EC2::VPCEndpoint",
    },
    ROOT / "infra" / "stockanalyst-mainnet-prereqs.yaml": {
        "MainnetJobBucket": "AWS::S3::Bucket",
        "MainnetJobTokenSecret": "AWS::SecretsManager::Secret",
        "MainnetRuntimeRole": "AWS::IAM::Role",
        "MainnetGatewayLambdaRole": "AWS::IAM::Role",
    },
    ROOT / "infra" / "x402-lambda-gateway.yaml": {
        "X402Api": "AWS::Serverless::Api",
        "X402CustomDomain": "AWS::ApiGateway::DomainName",
        "X402CustomDomainMapping": "AWS::ApiGateway::BasePathMapping",
        "X402Adapter": "AWS::Serverless::Function",
        "X402ApiInvokePermission": "AWS::Lambda::Permission",
        "X402ApiAccessLogGroup": "AWS::Logs::LogGroup",
        "X402AdapterLogGroup": "AWS::Logs::LogGroup",
        "X402Gateway5xxMetric": "AWS::Logs::MetricFilter",
        "X402WebAcl": "AWS::WAFv2::WebACL",
        "X402WebAclAssociation": "AWS::WAFv2::WebACLAssociation",
        "X402LambdaErrors": "AWS::CloudWatch::Alarm",
        "X402LambdaThrottles": "AWS::CloudWatch::Alarm",
        "X402ApiServerErrors": "AWS::CloudWatch::Alarm",
    },
}
# SHA-256 of each parsed Resources mapping after canonical JSON serialization.
# This freezes every existing resource property/reference while ignoring YAML
# presentation and comments, so documentation-only payment changes cannot
# silently mutate the deployment graph.
INFRASTRUCTURE_RESOURCE_GRAPH_SHA256 = {
    ROOT / "infra" / "agentcore-mainnet-fixed-egress.yaml": (
        "5d05d012df86c8866563c59e26718f9b884d3d5cbb11595a46efd4bc7325119f"
    ),
    ROOT / "infra" / "stockanalyst-mainnet-prereqs.yaml": (
        "897c991179ebc4ac87391409f9deb284da6f01c77d323cbcdea812275dfb69ab"
    ),
    ROOT / "infra" / "x402-lambda-gateway.yaml": (
        "bfb2583f1768bc553d849d1ed0558913648da8b433139bee61f5911ea296da92"
    ),
}
X402_GATEWAY_ROUTE_METHODS = frozenset(
    {
        ("/x402/price", "get"),
        ("/x402/analyze/async", "post"),
        ("/x402/jobs/{jobId}", "get"),
        ("/x402/jobs/{jobId}/resume", "post"),
    }
)
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
UNSAFE_PAYMENT_GUIDANCE_PATTERNS = (
    r"(?i)\bapprove\s*\([^)]*(?:\b[A-Za-z_$][\w$]*\.)*MaxUint(?:256)?\b",
    r"(?i)\bset(?:\s+the)?\s+allowance\s+to\s+(?:[A-Za-z_$][\w$]*\.)*MaxUint(?:256)?\b",
    r"(?i)\buse\s+(?:an?\s+)?unlimited\s+allowance\b",
    r"(?i)\b(?:normal\s+)?x402(?::async|\s+(?:client|flow|run|CLI))?\s+automatically\s+approves\b",
)
GUIDANCE_NEGATION = re.compile(r"(?i)\b(?:never|no|not|without)\b")


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


class CloudFormationLoader(yaml.SafeLoader):
    pass


def _construct_cloudformation_tag(
    loader: CloudFormationLoader,
    tag_suffix: str,
    node: yaml.Node,
) -> dict[str, object]:
    if isinstance(node, yaml.ScalarNode):
        value: object = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    key = "Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}"
    return {key: value}


CloudFormationLoader.add_multi_constructor("!", _construct_cloudformation_tag)


def load_cloudformation(path: Path) -> dict[str, object]:
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=CloudFormationLoader)
    if not isinstance(document, dict):
        raise AssertionError(f"CloudFormation document is not a mapping: {path}")
    return document


def declared_runtime_secret_names(studio: str) -> list[str]:
    block = re.search(
        r"(?ms)^# BEGIN RUNTIME SECRET NAME CONTRACT$\n"
        r"(?P<names>(?:# runtime-secret-name: [A-Z0-9_]+\n)+)"
        r"^# END RUNTIME SECRET NAME CONTRACT$",
        studio,
    )
    if block is None:
        return []
    return re.findall(
        r"(?m)^# runtime-secret-name: ([A-Z0-9_]+)$",
        block.group("names"),
    )


def affirmative_payment_guidance_violations(documentation: str) -> list[str]:
    violations: list[str] = []
    for pattern in UNSAFE_PAYMENT_GUIDANCE_PATTERNS:
        for match in re.finditer(pattern, documentation):
            prefix = documentation[: match.start()]
            clause_start = max(prefix.rfind(boundary) for boundary in ".!?;,:\n") + 1
            if GUIDANCE_NEGATION.search(prefix[clause_start:]):
                continue
            violations.append(match.group(0))
    return violations


class MainnetInfrastructureContractTests(unittest.TestCase):
    def test_public_docs_describe_the_complete_four_token_contract(self) -> None:
        for path in PUBLIC_FOUR_TOKEN_DOCUMENTS:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertTrue(path.is_file(), f"missing public guide: {path}")
                documentation = path.read_text(encoding="utf-8")
                normalized = " ".join(documentation.split())
                self.assertIn(PAID_TOKEN_TABLE, documentation)
                for required in (
                    f"canonical Permit2 `{CANONICAL_PERMIT2}`",
                    "A 50-token allowance covers 238 complete 0.21 payments and leaves 0.02 token.",
                    "`npm run x402:allowance`",
                    "`npm run x402:approve`",
                    "`npm run x402:revoke`",
                    "`BSC_RPC_URL` is used only for USDC/USDT",
                    "`npm run x402:async` never approves or revokes",
                    "B402 capabilities may be partial",
                    "`extra.signerAddress` is facilitator EOA metadata; it is not the Permit2 spender and is not part of `permit2-exact` typed data.",
                    f"`extra.spenderAddress` is the live B402 proxy and the `permit2-exact` typed-data spender; the ERC-20 approval target remains canonical Permit2 `{CANONICAL_PERMIT2}`.",
                    "When `pendingSettlementReference` has been durably recorded, Permit2 recovery is settle-only with the identical persisted proof and does not call `/verify` again.",
                    "Without a durable `pendingSettlementReference`, other retryable create/recovery cases reuse the identical proof but may repeat verify-and-settle under B402 idempotency with the same nonce; they create no new signature, nonce, or approval.",
                    "Both approve and revoke require confirmation; `--yes` is an explicit noninteractive bypass.",
                    "In promotional mode, `paymentRequired=false` and the active `accepts=[]`; `supportedAssets` may still list all four tokens as registry metadata and is not an active payment requirement.",
                    "There is no USDC/USDT promotional proof, B402 verify/settle, or automatic approval.",
                ):
                    self.assertIn(required, normalized)

    def test_tracked_guidance_rejects_stale_or_unsafe_payment_advice(self) -> None:
        for path in TRACKED_X402_GUIDANCE:
            with self.subTest(path=path.relative_to(ROOT)):
                source = path.read_text(encoding="utf-8")
                documentation = " ".join(source.split())
                for forbidden in (
                    r"(?i)USDC(?:/| and )USDT.{0,80}(?:defer|暂缓)",
                    r"(?i)(?:only|strict).{0,24}U.{0,16}(?:or|/).{0,16}USD1",
                    r"(?i)fixed U/USD1(?:\s+mainnet)? registry",
                    r"买家可以用\s+`X402_PAYMENT_TOKEN=U`（默认）\s+或\s+`X402_PAYMENT_TOKEN=USD1`\s+严格选择",
                ):
                    self.assertNotRegex(documentation, forbidden)
                self.assertEqual(affirmative_payment_guidance_violations(source), [])

    def test_payment_guidance_safety_patterns_are_affirmative_only(self) -> None:
        safe_guidance = (
            "The client must never approve unlimited allowances.",
            "There is no automatic approval.",
            "Normal x402 never automatically approves Permit2.",
            "Do not use an unlimited allowance.",
            "x402:async does not automatically approve Permit2.",
            "Never call approve(token, MaxUint256).",
            "Do not set allowance to MaxUint256.",
        )
        prohibited_guidance = (
            "Call approve(token, MaxUint256) before paying.",
            "Set allowance to MaxUint256 for convenience.",
            "Use unlimited allowance to avoid another transaction.",
            "Normal x402 automatically approves Permit2.",
        )

        for guidance in safe_guidance:
            with self.subTest(safe=guidance):
                self.assertEqual(affirmative_payment_guidance_violations(guidance), [])
        for guidance in prohibited_guidance:
            with self.subTest(prohibited=guidance):
                self.assertNotEqual(
                    affirmative_payment_guidance_violations(guidance), []
                )

    def test_runtime_secret_name_contract_remains_the_existing_26_names(self) -> None:
        declared = declared_runtime_secret_names(STUDIO.read_text(encoding="utf-8"))

        self.assertEqual(len(declared), 26)
        self.assertEqual(len(declared), len(set(declared)))
        self.assertEqual(frozenset(declared), EXPECTED_RUNTIME_SECRET_NAMES)
        self.assertNotIn("BSC_RPC_URL", declared)
        self.assertFalse(
            EXPECTED_RUNTIME_SECRET_NAMES.intersection(
                {
                    "PERMIT2_ADDRESS",
                    "PERMIT2_SPENDER_ADDRESS",
                    "REDIS_URL",
                    "DATABASE_URL",
                }
            )
        )

    def test_permit2_adds_no_infrastructure_resources_or_routes(self) -> None:
        templates: dict[Path, dict[str, object]] = {}
        for path, expected_types in INFRASTRUCTURE_RESOURCE_TYPES.items():
            with self.subTest(path=path.relative_to(ROOT)):
                template = load_cloudformation(path)
                templates[path] = template
                resources = template["Resources"]
                self.assertIsInstance(resources, dict)
                self.assertEqual(
                    {name: resource["Type"] for name, resource in resources.items()},
                    expected_types,
                )
                canonical_resources = json.dumps(
                    resources,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
                self.assertEqual(
                    hashlib.sha256(canonical_resources).hexdigest(),
                    INFRASTRUCTURE_RESOURCE_GRAPH_SHA256[path],
                )

        gateway = templates[ROOT / "infra" / "x402-lambda-gateway.yaml"]
        gateway_resources = gateway["Resources"]
        api = gateway_resources["X402Api"]["Properties"]
        paths = api["DefinitionBody"]["paths"]
        self.assertEqual(
            frozenset(
                (path, method)
                for path, methods in paths.items()
                for method in methods
            ),
            X402_GATEWAY_ROUTE_METHODS,
        )
        adapter = gateway_resources["X402Adapter"]["Properties"]
        self.assertEqual(
            {
                "Runtime": adapter["Runtime"],
                "Handler": adapter["Handler"],
                "CodeUri": adapter["CodeUri"],
                "AutoPublishAlias": adapter["AutoPublishAlias"],
                "Role": adapter["Role"],
                "Environment": adapter["Environment"],
            },
            {
                "Runtime": "python3.13",
                "Handler": "handler.lambda_handler",
                "CodeUri": "../gateway/x402_lambda/src",
                "AutoPublishAlias": "live",
                "Role": {"Ref": "LambdaExecutionRoleArn"},
                "Environment": {
                    "Variables": {
                        "AGENTCORE_INVOKE_URL": {"Ref": "AgentCoreInvokeUrl"},
                        "OAUTH_SECRET_ARN": {"Ref": "OAuthSecretArn"},
                        "X402_CUSTOM_DOMAIN_NAME": {"Ref": "CustomDomainName"},
                    }
                },
            },
        )
        self.assertNotIn("Policies", adapter)
        self.assertNotIn("VpcConfig", adapter)

        prereqs = templates[ROOT / "infra" / "stockanalyst-mainnet-prereqs.yaml"]
        prereq_resources = prereqs["Resources"]
        bucket = prereq_resources["MainnetJobBucket"]
        self.assertEqual(bucket["DeletionPolicy"], "Retain")
        self.assertEqual(bucket["UpdateReplacePolicy"], "Retain")
        self.assertEqual(bucket["Properties"]["BucketName"], {"Ref": "JobBucketName"})
        self.assertEqual(
            bucket["Properties"]["PublicAccessBlockConfiguration"],
            {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        )
        runtime_role = prereq_resources["MainnetRuntimeRole"]["Properties"]
        self.assertEqual(runtime_role["RoleName"], "bnbagent-stockanalyst-mainnet-runtime")
        self.assertEqual(
            [policy["PolicyName"] for policy in runtime_role["Policies"]],
            [
                "stockanalyst-mainnet-runtime-telemetry",
                "stockanalyst-mainnet-runtime-secrets",
                "stockanalyst-mainnet-runtime-storage",
            ],
        )

        network = templates[ROOT / "infra" / "agentcore-mainnet-fixed-egress.yaml"]
        network_resources = network["Resources"]
        self.assertEqual(
            network_resources["NatGateway"]["Properties"],
            {
                "AllocationId": {"Fn::GetAtt": "EgressElasticIp.AllocationId"},
                "ConnectivityType": "public",
                "SubnetId": {"Ref": "PublicSubnetId"},
                "Tags": [
                    {"Key": "Name", "Value": "stockanalyst-mainnet-fixed-egress-nat"},
                    {"Key": "Project", "Value": "stockanalyst-agent"},
                    {"Key": "Purpose", "Value": "fixed-egress"},
                    {"Key": "ManagedBy", "Value": "cloudformation"},
                    {"Key": "Environment", "Value": "mainnet"},
                ],
            },
        )
        self.assertEqual(
            network_resources["PrivateDefaultRoute"]["Properties"],
            {
                "RouteTableId": {"Ref": "PrivateRouteTable"},
                "DestinationCidrBlock": "0.0.0.0/0",
                "NatGatewayId": {"Ref": "NatGateway"},
            },
        )
        self.assertEqual(
            network_resources["RuntimeSecurityGroup"]["Properties"]["VpcId"],
            {"Ref": "VpcId"},
        )
        self.assertEqual(
            network_resources["S3GatewayEndpoint"]["Properties"]["RouteTableIds"],
            [{"Ref": "PrivateRouteTable"}],
        )

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

        for documentation in (buyer_readme, stock_readme, agent_readme):
            self.assertIn(PAID_TOKEN_TABLE, documentation)
        self.assertIn("Paid tier (0.21 U)", main_source)

    def test_root_readme_describes_current_point_21_prices(self) -> None:
        root_readme = ROOT_README.read_text(encoding="utf-8")

        for current_guidance in (
            PAID_TOKEN_TABLE,
            "sign exact 0.21-token proof",
            "sign quote → 0.21 U",
            "| **Paid** full analysis via x402 | `npm run x402:async` | 0.21 U, USD1, USDC, or USDT |",
            "signed quote 0.21 U",
            "+ ≥ 0.21 U (for ERC-8183)",
            "# paid: exact 0.21 selected token",
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
            "Setting `X402_PROMO_FREE_MODE=0` restores the four-token paid "
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
        for value in (
            'address="0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"',
            'domain_name="USD Coin"',
            'address="0x55d398326f99059fF775485246999027B3197955"',
            'domain_name="Tether USD"',
            'transfer_method="permit2-exact"',
        ):
            self.assertIn(value, tokens)
        self.assertEqual(tokens.count("decimals=18"), 4)
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
