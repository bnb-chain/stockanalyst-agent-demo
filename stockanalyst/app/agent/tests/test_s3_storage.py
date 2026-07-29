from __future__ import annotations

import hashlib
import json
import unittest
from types import SimpleNamespace

from botocore.exceptions import ClientError

from s3_storage import (
    S3StorageError,
    S3StorageProvider,
    install_s3_storage_from_env,
)


class FakeBody:
    def __init__(self, value: bytes) -> None:
        self.value = value

    def read(self, amount: int = -1) -> bytes:
        return self.value if amount < 0 else self.value[:amount]


class FakeS3:
    def __init__(self) -> None:
        self.put_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.head_calls: list[dict] = []
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]
        return {"ETag": '"test"'}

    def get_object(self, *, Bucket: str, Key: str):
        self.get_calls.append({"Bucket": Bucket, "Key": Key})
        value = self.objects[(Bucket, Key)]
        return {"ContentLength": len(value), "Body": FakeBody(value)}

    def head_object(self, *, Bucket: str, Key: str):
        self.head_calls.append({"Bucket": Bucket, "Key": Key})
        if (Bucket, Key) not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}},
                "HeadObject",
            )
        return {"ContentLength": len(self.objects[(Bucket, Key)])}


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

    def test_rejects_public_base_with_out_of_range_port(self) -> None:
        env = {
            "DELIVERABLE_S3_BUCKET": "bnbagent-code-stock-analyst-agent",
            "DELIVERABLE_PUBLIC_BASE": "https://cdn.example:99999",
        }

        with self.assertRaisesRegex(S3StorageError, "DELIVERABLE_PUBLIC_BASE"):
            S3StorageProvider.from_env(env, s3_client=self.client)

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


class S3StorageReadTests(S3StorageUploadTests):
    def canonical_url(self, stem: str, body: bytes) -> str:
        digest = hashlib.sha256(body).hexdigest()
        return (
            "https://d111111abcdef8.cloudfront.net/"
            f"deliverables/{stem}-{digest}.json"
        )

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
        body = b"x" * (2 * 1024 * 1024 + 1)
        url = self.canonical_url("large", body)
        key = url.removeprefix("https://d111111abcdef8.cloudfront.net/")
        self.client.objects[("bnbagent-code-stock-analyst-agent", key)] = body
        with self.assertRaisesRegex(S3StorageError, "2 MiB"):
            await provider.download(url)

    async def test_exists_returns_false_only_for_missing_key(self) -> None:
        url = self.canonical_url("missing", b"{}")
        self.assertFalse(
            await self.provider().exists(url)
        )

    async def test_exists_propagates_non_missing_s3_errors(self) -> None:
        def fail_head_object(*, Bucket: str, Key: str) -> None:
            raise ClientError(
                {"Error": {"Code": "NotFound", "Message": "Unexpected error"}},
                "HeadObject",
            )

        self.client.head_object = fail_head_object
        url = self.canonical_url("missing", b"{}")

        with self.assertRaises(ClientError):
            await self.provider().exists(url)

    async def test_rejects_noncanonical_key_before_s3_calls(self) -> None:
        url = "https://d111111abcdef8.cloudfront.net/deliverables/job.json"

        with self.assertRaisesRegex(S3StorageError, "content-addressed"):
            await self.provider().download(url)
        with self.assertRaisesRegex(S3StorageError, "content-addressed"):
            await self.provider().exists(url)

        self.assertEqual(self.client.get_calls, [])
        self.assertEqual(self.client.head_calls, [])

    async def test_download_rejects_key_digest_mismatch(self) -> None:
        body = b'{"job_id":397}'
        url = (
            "https://d111111abcdef8.cloudfront.net/deliverables/"
            f"job-{'0' * 64}.json"
        )
        key = url.removeprefix("https://d111111abcdef8.cloudfront.net/")
        self.client.objects[
            ("bnbagent-code-stock-analyst-agent", key)
        ] = body

        with self.assertRaisesRegex(S3StorageError, "digest"):
            await self.provider().download(url)

    async def test_download_rejects_noncanonical_json_bytes(self) -> None:
        body = b'{"job_id": 397}'
        url = self.canonical_url("job", body)
        key = url.removeprefix("https://d111111abcdef8.cloudfront.net/")
        self.client.objects[
            ("bnbagent-code-stock-analyst-agent", key)
        ] = body

        with self.assertRaisesRegex(S3StorageError, "canonical"):
            await self.provider().download(url)

    async def test_download_rejects_nonfinite_json_constants(self) -> None:
        body = b'{"value":NaN}'
        url = self.canonical_url("job", body)
        key = url.removeprefix("https://d111111abcdef8.cloudfront.net/")
        self.client.objects[
            ("bnbagent-code-stock-analyst-agent", key)
        ] = body

        with self.assertRaisesRegex(S3StorageError, "valid UTF-8 JSON"):
            await self.provider().download(url)
