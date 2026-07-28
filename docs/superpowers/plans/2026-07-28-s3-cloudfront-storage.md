# S3 + CloudFront Deliverable Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upload ERC-8183 deliverable manifests from AgentCore to the private `bnbagent-code-stock-analyst-agent` bucket and publish immutable CloudFront URLs on-chain.

**Architecture:** A custom `S3StorageProvider` canonicalizes each manifest, stores it under a content-addressed `deliverables/` key, and returns the CloudFront URL. `main.py` installs this provider after runtime secrets load; the existing per-job UOMP relay override remains higher priority. CloudFront reads the private S3 prefix through OAC, while the runtime role can only write objects.

**Tech Stack:** Python 3.14, `bnbagent.storage.StorageProvider`, boto3/botocore, unittest, AWS S3, CloudFront OAC, IAM, BSC Testnet.

## Global Constraints

- Run every AWS command only after `export AWS_PROFILE=dev`.
- Deploy only with `bag deploy`; never invoke raw `agentcore deploy`.
- Never print, copy, or commit wallet material, `.env.local`, API keys, or Cognito secrets.
- Keep `bnbagent-code-stock-analyst-agent` private with all S3 public-access blocks enabled.
- CloudFront may read only `deliverables/*`; the runtime role may write only `deliverables/*`.
- S3 and CloudFront configuration is active only when both `DELIVERABLE_S3_BUCKET` and `DELIVERABLE_PUBLIC_BASE` are present.
- A partial S3 configuration is a startup error; deployed delivery must never silently fall back to `file://`.
- Preserve the buyer-provided UOMP relay as an explicit per-job override.
- Do not change BSC Testnet, wallet, OAuth, poller, settlement, or mainnet configuration in this plan.
- A live E2E job locks 1 U and requires explicit user approval immediately before it is created.

---

### Task 1: Canonical S3 Upload Provider

**Files:**
- Create: `stockanalyst/app/agent/s3_storage.py`
- Create: `stockanalyst/app/agent/tests/test_s3_storage.py`

**Interfaces:**
- Consumes: `bnbagent.storage.StorageProvider`, a boto3-compatible S3 client, and `DELIVERABLE_S3_*` environment values.
- Produces: `S3StorageError`, `S3StorageConfig`, and `S3StorageProvider.from_env(env, s3_client=None)`.

- [ ] **Step 1: Write failing configuration and upload tests**

Create `test_s3_storage.py` with a fake client that records `put_object` calls:

```python
from __future__ import annotations

import hashlib
import json
import unittest

from s3_storage import S3StorageError, S3StorageProvider


class FakeBody:
    def __init__(self, value: bytes) -> None:
        self.value = value

    def read(self, amount: int = -1) -> bytes:
        return self.value if amount < 0 else self.value[:amount]


class FakeS3:
    def __init__(self) -> None:
        self.put_calls: list[dict] = []
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]
        return {"ETag": '"test"'}


class S3StorageUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = FakeS3()
        self.env = {
            "DELIVERABLE_S3_BUCKET": "bnbagent-code-stock-analyst-agent",
            "DELIVERABLE_PUBLIC_BASE": "https://d111111abcdef8.cloudfront.net",
            "DELIVERABLE_S3_PREFIX": "deliverables",
        }

    def provider(self) -> S3StorageProvider:
        return S3StorageProvider.from_env(self.env, s3_client=self.client)

    def test_requires_bucket_and_public_base_together(self) -> None:
        with self.assertRaisesRegex(S3StorageError, "must be set together"):
            S3StorageProvider.from_env(
                {"DELIVERABLE_S3_BUCKET": "bnbagent-code-stock-analyst-agent"},
                s3_client=self.client,
            )

    async def test_upload_uses_canonical_content_addressed_json(self) -> None:
        data = {"z": 1, "a": {"value": "ok"}}
        canonical = json.dumps(
            data, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()

        url = await self.provider().upload(data, "erc8183-job-397.json")

        self.assertEqual(
            url,
            "https://d111111abcdef8.cloudfront.net/"
            f"deliverables/erc8183-job-397-{digest}.json",
        )
        self.assertEqual(
            self.client.put_calls,
            [{
                "Bucket": "bnbagent-code-stock-analyst-agent",
                "Key": f"deliverables/erc8183-job-397-{digest}.json",
                "Body": canonical,
                "ContentType": "application/json",
                "CacheControl": "public,max-age=31536000,immutable",
            }],
        )

    async def test_upload_rejects_path_filename(self) -> None:
        with self.assertRaisesRegex(S3StorageError, "filename"):
            await self.provider().upload({"ok": True}, "../job.json")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
cd stockanalyst/app/agent
uv run python -m unittest tests.test_s3_storage.S3StorageUploadTests -v
```

Expected: import failure because `s3_storage.py` does not exist.

- [ ] **Step 3: Implement configuration, canonicalization, and upload**

Create `s3_storage.py` with these public types and methods:

```python
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import boto3
from bnbagent.storage import StorageProvider

_DEFAULT_PREFIX = "deliverables"
_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_OBJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,299}\Z")


class S3StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class S3StorageConfig:
    bucket: str
    public_base: str
    prefix: str = _DEFAULT_PREFIX

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "S3StorageConfig":
        bucket = env.get("DELIVERABLE_S3_BUCKET", "").strip()
        public_base = env.get("DELIVERABLE_PUBLIC_BASE", "").strip()
        if bool(bucket) != bool(public_base):
            raise S3StorageError(
                "DELIVERABLE_S3_BUCKET and DELIVERABLE_PUBLIC_BASE must be set together"
            )
        if not bucket:
            raise S3StorageError("S3 deliverable storage is not configured")
        if not _BUCKET_RE.fullmatch(bucket):
            raise S3StorageError("DELIVERABLE_S3_BUCKET is invalid")
        parsed = urlsplit(public_base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise S3StorageError(
                "DELIVERABLE_PUBLIC_BASE must be an HTTPS origin without a path"
            )
        prefix = env.get("DELIVERABLE_S3_PREFIX", _DEFAULT_PREFIX).strip().strip("/")
        segments = prefix.split("/")
        if not prefix or any(not _SEGMENT_RE.fullmatch(value) for value in segments):
            raise S3StorageError("DELIVERABLE_S3_PREFIX is invalid")
        return cls(
            bucket=bucket,
            public_base=public_base.rstrip("/"),
            prefix="/".join(segments),
        )


class S3StorageProvider(StorageProvider):
    uses_file_url = False

    def __init__(self, config: S3StorageConfig, s3_client: Any) -> None:
        self._config = config
        self._s3 = s3_client

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] = os.environ,
        *,
        s3_client: Any | None = None,
    ) -> "S3StorageProvider":
        return cls(
            S3StorageConfig.from_env(env),
            s3_client if s3_client is not None else boto3.client("s3"),
        )

    @staticmethod
    def _safe_stem(filename: str | None) -> str:
        value = filename or "manifest.json"
        if not _SEGMENT_RE.fullmatch(value):
            raise S3StorageError("filename must be one safe basename")
        return value[:-5] if value.endswith(".json") else value

    async def upload(self, data: dict, filename: str | None = None) -> str:
        if not isinstance(data, dict):
            raise S3StorageError("S3 deliverable root must be an object")
        try:
            body = json.dumps(
                data, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise S3StorageError("S3 deliverable is not valid JSON") from exc
        digest = hashlib.sha256(body).hexdigest()
        key = f"{self._config.prefix}/{self._safe_stem(filename)}-{digest}.json"
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self._config.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            CacheControl="public,max-age=31536000,immutable",
        )
        return f"{self._config.public_base}/{key}"
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```bash
cd stockanalyst/app/agent
uv run python -m unittest tests.test_s3_storage.S3StorageUploadTests -v
```

Expected: all `S3StorageUploadTests` pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add stockanalyst/app/agent/s3_storage.py \
  stockanalyst/app/agent/tests/test_s3_storage.py
git commit -m "feat: add canonical S3 deliverable uploads"
```

---

### Task 2: Safe Download and Existence Checks

**Files:**
- Modify: `stockanalyst/app/agent/s3_storage.py`
- Modify: `stockanalyst/app/agent/tests/test_s3_storage.py`

**Interfaces:**
- Consumes: `S3StorageProvider` configuration and boto3-compatible `get_object`/`head_object`.
- Produces: strict `download(url) -> dict` and `exists(url) -> bool`.

- [ ] **Step 1: Add failing read-safety tests**

Extend `FakeS3` and the test class:

```python
from botocore.exceptions import ClientError


class FakeS3:
    # Keep Task 1 fields and methods.
    def get_object(self, *, Bucket: str, Key: str):
        value = self.objects[(Bucket, Key)]
        return {"ContentLength": len(value), "Body": FakeBody(value)}

    def head_object(self, *, Bucket: str, Key: str):
        if (Bucket, Key) not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}},
                "HeadObject",
            )
        return {"ContentLength": len(self.objects[(Bucket, Key)])}


class S3StorageReadTests(S3StorageUploadTests):
    async def test_download_reads_validated_cloudfront_url_from_s3(self) -> None:
        provider = self.provider()
        url = await provider.upload({"job_id": 397}, "job.json")
        self.assertEqual(await provider.download(url), {"job_id": 397})

    async def test_download_rejects_another_origin(self) -> None:
        with self.assertRaisesRegex(S3StorageError, "public base"):
            await self.provider().download(
                "https://attacker.example/deliverables/job.json"
            )

    async def test_download_rejects_path_outside_prefix(self) -> None:
        with self.assertRaisesRegex(S3StorageError, "prefix"):
            await self.provider().download(
                "https://d111111abcdef8.cloudfront.net/private/job.json"
            )

    async def test_download_rejects_oversized_body(self) -> None:
        provider = self.provider()
        self.client.objects[
            ("bnbagent-code-stock-analyst-agent", "deliverables/large.json")
        ] = b"x" * (2 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(S3StorageError, "2 MiB"):
            await provider.download(
                "https://d111111abcdef8.cloudfront.net/deliverables/large.json"
            )

    async def test_exists_returns_false_only_for_missing_key(self) -> None:
        self.assertFalse(
            await self.provider().exists(
                "https://d111111abcdef8.cloudfront.net/deliverables/missing.json"
            )
        )
```

- [ ] **Step 2: Run the read tests and verify RED**

Run:

```bash
cd stockanalyst/app/agent
uv run python -m unittest tests.test_s3_storage.S3StorageReadTests -v
```

Expected: failure because `download()` and `exists()` are abstract/unimplemented.

- [ ] **Step 3: Implement URL-to-key validation, bounded download, and exists**

Add to `S3StorageProvider`:

```python
    def _key_from_url(self, url: str) -> str:
        parsed = urlsplit(url)
        base = urlsplit(self._config.public_base)
        if (
            parsed.scheme != "https"
            or parsed.netloc != base.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise S3StorageError("deliverable URL must use the configured public base")
        path = parsed.path.lstrip("/")
        prefix = f"{self._config.prefix}/"
        if not path.startswith(prefix) or path == prefix or "%" in path:
            raise S3StorageError("deliverable URL must stay under the configured prefix")
        remainder = path[len(prefix):]
        if "/" in remainder or not _OBJECT_RE.fullmatch(remainder):
            raise S3StorageError("deliverable object key is invalid")
        return path

    async def download(self, url: str) -> dict:
        key = self._key_from_url(url)
        response = await asyncio.to_thread(
            self._s3.get_object,
            Bucket=self._config.bucket,
            Key=key,
        )
        declared = int(response.get("ContentLength", 0))
        if declared > _MAX_DOWNLOAD_BYTES:
            raise S3StorageError("S3 deliverable exceeds 2 MiB")
        body = await asyncio.to_thread(
            response["Body"].read,
            _MAX_DOWNLOAD_BYTES + 1,
        )
        if len(body) > _MAX_DOWNLOAD_BYTES:
            raise S3StorageError("S3 deliverable exceeds 2 MiB")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise S3StorageError("S3 deliverable is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise S3StorageError("S3 deliverable root must be an object")
        return value

    async def exists(self, url: str) -> bool:
        from botocore.exceptions import ClientError

        key = self._key_from_url(url)
        try:
            await asyncio.to_thread(
                self._s3.head_object,
                Bucket=self._config.bucket,
                Key=key,
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True
```

- [ ] **Step 4: Run all S3 provider tests and verify GREEN**

Run:

```bash
cd stockanalyst/app/agent
uv run python -m unittest tests.test_s3_storage -v
```

Expected: all upload and read tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add stockanalyst/app/agent/s3_storage.py \
  stockanalyst/app/agent/tests/test_s3_storage.py
git commit -m "feat: validate S3 deliverable reads"
```

---

### Task 3: Runtime Installation and UOMP Precedence

**Files:**
- Modify: `stockanalyst/app/agent/s3_storage.py`
- Modify: `stockanalyst/app/agent/main.py`
- Modify: `stockanalyst/app/agent/studio.toml`
- Modify: `stockanalyst/app/agent/tests/test_s3_storage.py`
- Modify: `stockanalyst/app/agent/tests/test_signing_lock.py`

**Interfaces:**
- Consumes: runtime secrets loaded into `os.environ` and `bnbagent_studio_core.storage.storage_provider_from_config`.
- Produces: `install_s3_storage_from_env(env=os.environ, storage_module=None, s3_client=None) -> bool`.

- [ ] **Step 1: Write failing installer and precedence tests**

Add installer tests:

```python
from types import SimpleNamespace
from unittest.mock import patch

from s3_storage import install_s3_storage_from_env


class S3StorageInstallerTests(unittest.TestCase):
    def test_absent_configuration_leaves_factory_unchanged(self) -> None:
        original = lambda **kwargs: object()
        storage_module = SimpleNamespace(storage_provider_from_config=original)
        self.assertFalse(
            install_s3_storage_from_env(
                {},
                storage_module=storage_module,
                s3_client=FakeS3(),
            )
        )
        self.assertIs(storage_module.storage_provider_from_config, original)

    def test_complete_configuration_installs_one_provider(self) -> None:
        storage_module = SimpleNamespace(storage_provider_from_config=lambda **kwargs: None)
        self.assertTrue(
            install_s3_storage_from_env(
                {
                    "DELIVERABLE_S3_BUCKET": "bnbagent-code-stock-analyst-agent",
                    "DELIVERABLE_PUBLIC_BASE": "https://d111111abcdef8.cloudfront.net",
                },
                storage_module=storage_module,
                s3_client=FakeS3(),
            )
        )
        first = storage_module.storage_provider_from_config()
        second = storage_module.storage_provider_from_config()
        self.assertIs(first, second)
        self.assertIsInstance(first, S3StorageProvider)
```

Extend `test_signing_lock.py` so its default provider is the installed S3 object,
the gateway call captures the gateway provider, and the following default call
captures the exact same S3 object. Expected capture order:

```python
[(1, gateway_provider), (2, s3_provider)]
```

Add a source-order assertion that `main.py` calls `_load_runtime_secrets()` before
`install_s3_storage_from_env()`:

```python
main_text = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
self.assertLess(
    main_text.index("\n_load_runtime_secrets()\n"),
    main_text.index("\ninstall_s3_storage_from_env()\n"),
)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
cd stockanalyst/app/agent
uv run python -m unittest \
  tests.test_s3_storage.S3StorageInstallerTests \
  tests.test_signing_lock -v
```

Expected: failure because the installer and `main.py` call do not exist.

- [ ] **Step 3: Implement installer and call it after secrets load**

Add to `s3_storage.py`:

```python
def install_s3_storage_from_env(
    env: Mapping[str, str] = os.environ,
    *,
    storage_module: Any | None = None,
    s3_client: Any | None = None,
) -> bool:
    bucket_present = bool(env.get("DELIVERABLE_S3_BUCKET", "").strip())
    base_present = bool(env.get("DELIVERABLE_PUBLIC_BASE", "").strip())
    if not bucket_present and not base_present:
        return False
    provider = S3StorageProvider.from_env(env, s3_client=s3_client)
    if storage_module is None:
        import bnbagent_studio_core.storage as storage_module
    storage_module.storage_provider_from_config = lambda **_kwargs: provider
    return True
```

In `main.py`, immediately after `_load_runtime_secrets()`:

```python
from s3_storage import install_s3_storage_from_env

install_s3_storage_from_env()
```

Update `[storage]` comments in `studio.toml` to state that `kind = "local"` is
only the local-development fallback and deployed S3 is installed from the three
`DELIVERABLE_S3_*` environment variables.

- [ ] **Step 4: Run focused and full agent tests**

Run:

```bash
cd stockanalyst/app/agent
uv run python -m unittest tests.test_s3_storage tests.test_signing_lock -v
uv run python -m unittest discover -s tests -v
uv run ruff check s3_storage.py main.py tests/test_s3_storage.py tests/test_signing_lock.py
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 5: Commit Task 3**

```bash
git add stockanalyst/app/agent/s3_storage.py \
  stockanalyst/app/agent/main.py \
  stockanalyst/app/agent/studio.toml \
  stockanalyst/app/agent/tests/test_s3_storage.py \
  stockanalyst/app/agent/tests/test_signing_lock.py
git commit -m "feat: install S3 storage in AgentCore"
```

---

### Task 4: Provision CloudFront OAC and Least-Privilege AWS Policies

**Files:**
- No committed source files.
- Record returned OAC ID, distribution ID, distribution ARN, and domain in the execution notes.

**Interfaces:**
- Consumes: existing private bucket and the `dev` AWS profile.
- Produces: a deployed CloudFront distribution, scoped bucket policy, and scoped runtime-role write policy.

- [ ] **Step 1: Recheck bucket safety and effective permissions**

Run:

```bash
export AWS_PROFILE=dev
aws s3api get-bucket-location \
  --bucket bnbagent-code-stock-analyst-agent
aws s3api get-public-access-block \
  --bucket bnbagent-code-stock-analyst-agent
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::201243086760:user/BNBAgentStudioTestnet \
  --action-names \
    cloudfront:CreateOriginAccessControl \
    cloudfront:GetOriginAccessControl \
    cloudfront:CreateDistribution \
    cloudfront:GetDistribution \
    cloudfront:GetDistributionConfig \
    s3:GetBucketPolicy \
    s3:PutBucketPolicy \
    iam:PutRolePolicy \
  --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision}' \
  --output table
```

Expected: bucket region is `us-east-1` (`LocationConstraint: null`), all four
public-access-block values are `true`, and every simulated action is `allowed`.

- [ ] **Step 2: Create the OAC**

Run:

```bash
export AWS_PROFILE=dev
stock_oac_config=$(jq -n '{
  Name:"stockanalyst-deliverables-oac",
  Description:"Private S3 access for StockAnalyst deliverables",
  SigningProtocol:"sigv4",
  SigningBehavior:"always",
  OriginAccessControlOriginType:"s3"
}')
stock_oac_result=$(aws cloudfront create-origin-access-control \
  --origin-access-control-config "$stock_oac_config")
stock_oac_id=$(jq -r '.OriginAccessControl.Id' <<<"$stock_oac_result")
test -n "$stock_oac_id"
```

Expected: `stock_oac_id` contains the created OAC ID.

- [ ] **Step 3: Create the CloudFront distribution without List permissions**

Run:

```bash
export AWS_PROFILE=dev
stock_distribution_config=$(jq -n \
  --arg oac "$stock_oac_id" \
  '{
    CallerReference:"stockanalyst-deliverables-20260728",
    Comment:"StockAnalyst immutable ERC-8183 deliverables",
    Enabled:true,
    HttpVersion:"http2",
    IsIPV6Enabled:true,
    PriceClass:"PriceClass_100",
    Origins:{
      Quantity:1,
      Items:[{
        Id:"stockanalyst-s3-origin",
        DomainName:"bnbagent-code-stock-analyst-agent.s3.us-east-1.amazonaws.com",
        OriginAccessControlId:$oac,
        S3OriginConfig:{OriginAccessIdentity:""}
      }]
    },
    DefaultCacheBehavior:{
      TargetOriginId:"stockanalyst-s3-origin",
      ViewerProtocolPolicy:"redirect-to-https",
      AllowedMethods:{
        Quantity:2,
        Items:["GET","HEAD"],
        CachedMethods:{Quantity:2,Items:["GET","HEAD"]}
      },
      Compress:true,
      CachePolicyId:"658327ea-f89d-4fab-a63d-7e88639e58f6"
    }
  }')
stock_distribution_result=$(aws cloudfront create-distribution \
  --distribution-config "$stock_distribution_config")
stock_distribution_id=$(jq -r '.Distribution.Id' <<<"$stock_distribution_result")
stock_distribution_arn=$(jq -r '.Distribution.ARN' <<<"$stock_distribution_result")
stock_distribution_domain=$(jq -r '.Distribution.DomainName' <<<"$stock_distribution_result")
test -n "$stock_distribution_id"
test -n "$stock_distribution_arn"
test -n "$stock_distribution_domain"
```

Expected: all three distribution values are non-empty.

- [ ] **Step 4: Reconfirm the empty policy, then apply CloudFront-only access**

Run:

```bash
export AWS_PROFILE=dev
stock_policy_check_dir=$(mktemp -d)
if aws s3api get-bucket-policy \
  --bucket bnbagent-code-stock-analyst-agent \
  --query Policy \
  --output text \
  >"$stock_policy_check_dir/policy.txt" \
  2>"$stock_policy_check_dir/error.txt"; then
  echo "Bucket policy appeared after design review; stop before overwriting it."
  exit 1
fi
grep -q 'NoSuchBucketPolicy' "$stock_policy_check_dir/error.txt"
stock_bucket_policy=$(jq -n \
  --arg distribution_arn "$stock_distribution_arn" \
  '{
    Version:"2012-10-17",
    Statement:[{
      Sid:"AllowCloudFrontReadDeliverables",
      Effect:"Allow",
      Principal:{Service:"cloudfront.amazonaws.com"},
      Action:"s3:GetObject",
      Resource:"arn:aws:s3:::bnbagent-code-stock-analyst-agent/deliverables/*",
      Condition:{StringEquals:{"AWS:SourceArn":$distribution_arn}}
    }]
  }')
aws s3api put-bucket-policy \
  --bucket bnbagent-code-stock-analyst-agent \
  --policy "$stock_bucket_policy"
```

Expected: the check confirms `NoSuchBucketPolicy`, the write succeeds, and S3
public-access-block remains fully enabled. If a policy appears before execution,
stop and review it instead of overwriting it.

- [ ] **Step 5: Add runtime-only write permission**

Run:

```bash
export AWS_PROFILE=dev
stock_runtime_policy=$(jq -n '{
  Version:"2012-10-17",
  Statement:[{
    Sid:"WriteStockAnalystDeliverables",
    Effect:"Allow",
    Action:"s3:PutObject",
    Resource:"arn:aws:s3:::bnbagent-code-stock-analyst-agent/deliverables/*"
  }]
}')
aws iam put-role-policy \
  --role-name bnbagent-stockanalyst-runtime \
  --policy-name bnbagent-write-deliverables \
  --policy-document "$stock_runtime_policy"
```

Expected: command exits successfully.

- [ ] **Step 6: Wait for the exact distribution to become deployed**

Run repeatedly, without `ListDistributions`:

```bash
export AWS_PROFILE=dev
aws cloudfront get-distribution \
  --id "$stock_distribution_id" \
  --query 'Distribution.{Status:Status,DomainName:DomainName}' \
  --output table
```

Expected: status becomes `Deployed` and the domain matches
`stock_distribution_domain`.

---

### Task 5: Deploy and Verify a BSC Testnet S3 Delivery

**Files:**
- Generated report under `buyer-client/`; do not commit.
- No secret files are committed.

**Interfaces:**
- Consumes: CloudFront domain, runtime secret bundle, buyer wallet, Cognito proxy, and BSC Testnet.
- Produces: one CloudFront deliverable URL, transaction hashes, matching manifest commitment, report artifact, and CloudWatch correlation.

- [ ] **Step 1: Configure non-secret S3 variables through `bag env`**

Run from `stockanalyst/app/agent`:

```bash
bag env set DELIVERABLE_S3_BUCKET bnbagent-code-stock-analyst-agent
bag env set DELIVERABLE_PUBLIC_BASE "https://$stock_distribution_domain"
bag env set DELIVERABLE_S3_PREFIX deliverables
```

Expected: variables are stored in the gitignored runtime environment without
printing existing secrets.

- [ ] **Step 2: Deploy only through bnbagent-studio**

Run:

```bash
export AWS_PROFILE=dev
export PATH=/Users/zhaoyu/.nvm/versions/node/v20.9.0/bin:/Users/zhaoyu/.nvm/versions/node/v24.10.0/bin:$PATH
/Users/zhaoyu/corp/bnbchain/chain_middleware/bnbchain-studio/.venv/bin/bag \
  deploy agent --accept-risk --force-deploy-broken-storage
```

Expected: deployment succeeds and runtime
`stockanalyst_stockanalyst-hrXlh1BUtQ` returns to `READY`.

- [ ] **Step 3: Verify runtime startup before spending U**

Run:

```bash
export AWS_PROFILE=dev
aws bedrock-agentcore-control get-agent-runtime \
  --region us-east-1 \
  --agent-runtime-id stockanalyst_stockanalyst-hrXlh1BUtQ \
  --query '{status:status,lastUpdatedAt:lastUpdatedAt}' \
  --output json
aws logs filter-log-events \
  --region us-east-1 \
  --log-group-name \
    '/aws/bedrock-agentcore/runtimes/stockanalyst_stockanalyst-hrXlh1BUtQ-DEFAULT' \
  --filter-pattern '"S3 deliverable"' \
  --limit 20
```

Expected: runtime status is `READY` and there is no partial-configuration or
startup traceback.

- [ ] **Step 4: Ask for explicit approval to lock 1 U**

Stop and ask:

```text
S3/CloudFront deployment is healthy. May I create and fund one new BSC Testnet
job, which will lock 1 U, to verify the CloudFront deliverable end to end?
```

Expected: continue only after explicit user approval.

- [ ] **Step 5: Run the existing buyer with external storage delivery**

Start the existing UOMP guard and Cognito OAuth proxy, then run from
`buyer-client` with:

```bash
export DELIVERY_MODE=ipfs
export MAX_PRICE_U=1
export BSC_RPC_URL=https://bsc-testnet-dataseed.bnbchain.org
export BSC_LOG_RPC_URL=https://bsc-prebsc-dataseed.bnbchain.org
npx tsx src/index.ts
```

Use the existing gitignored keystore/password and OAuth variables without
printing them. `DELIVERY_MODE=ipfs` here means “do not start the buyer relay”;
HTTP(S) CloudFront deliverables pass through unchanged.

Expected: negotiate, create/fund, notify, OpenRouter analysis, S3 upload, and
on-chain submission complete.

- [ ] **Step 6: Verify CloudFront, chain, report, and CloudWatch**

For the new job:

- Assert on-chain status is `SUBMITTED`.
- Assert `deliverable_url` begins with
  `https://$stock_distribution_domain/deliverables/`.
- Fetch the URL anonymously and require HTTP 200.
- Verify manifest `job_id`, `chain_id`, and all contract addresses.
- Canonicalize the raw manifest object and require its Keccak-256 to equal the
  on-chain deliverable commitment.
- Save and inspect the HTML/PDF report.
- Query CloudWatch for the job ID and require the logged upload URL and submit
  transaction to match the chain.

- [ ] **Step 7: Stop support processes and run final verification**

Run:

```bash
cd stockanalyst/app/agent
uv run python -m unittest discover -s tests -v
uv run ruff check s3_storage.py main.py tests/test_s3_storage.py tests/test_signing_lock.py
cd ../../..
git status --short
```

Expected: tests and lint pass; only generated reports or pre-existing user files
remain untracked/modified.
