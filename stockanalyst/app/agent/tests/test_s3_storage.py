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
