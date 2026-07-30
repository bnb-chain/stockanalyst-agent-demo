from __future__ import annotations

import io
import json
import unittest
from typing import Any

from botocore.exceptions import ClientError

from x402_job_store import (
    JobConflict,
    X402JobStore,
    X402JobStoreConfig,
    X402JobStoreError,
)


def job_record() -> dict[str, Any]:
    return {
        "jobId": "x402_" + "a" * 32,
        "status": "queued",
        "ticker": "BNB",
    }


def s3_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "PutObject")


class FakeConditionalS3:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.presign_calls: list[dict[str, Any]] = []
        self.put_errors: list[ClientError] = []
        self._next_etag = 1

    def put_object(self, **kwargs: Any) -> dict[str, str]:
        self.put_calls.append(kwargs)
        if self.put_errors:
            raise self.put_errors.pop(0)
        key = kwargs["Key"]
        existing = self.objects.get(key)
        if kwargs.get("IfNoneMatch") == "*" and existing is not None:
            raise s3_error("PreconditionFailed")
        if "IfMatch" in kwargs and (existing is None or kwargs["IfMatch"] != existing[1]):
            raise s3_error("412")
        etag = f'"etag-{self._next_etag}"'
        self._next_etag += 1
        self.objects[key] = (kwargs["Body"], etag)
        return {"ETag": etag}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        try:
            body, etag = self.objects[kwargs["Key"]]
        except KeyError as exc:
            raise s3_error("NoSuchKey") from exc
        return {"Body": io.BytesIO(body), "ETag": etag}

    def generate_presigned_url(self, operation: str, **kwargs: Any) -> str:
        self.presign_calls.append({"operation": operation, **kwargs})
        return "https://signed.example/report"


class X402JobStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.s3 = FakeConditionalS3()
        self.store = X402JobStore.from_env(
            {
                "X402_JOB_S3_BUCKET": "private-jobs",
                "X402_JOB_S3_PREFIX": "x402-jobs",
            },
            s3_client=self.s3,
        )

    async def test_create_is_atomic_and_never_overwrites(self) -> None:
        first = await self.store.create(job_record())
        second = await self.store.create(job_record())
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.s3.put_calls[0]["IfNoneMatch"], "*")

    async def test_replace_requires_the_observed_etag(self) -> None:
        stored = await self.store.create(job_record())
        assert stored is not None
        changed = {**stored.record, "status": "running"}
        updated = await self.store.replace(stored, changed)
        self.assertEqual(updated.record["status"], "running")
        self.assertEqual(self.s3.put_calls[-1]["IfMatch"], stored.etag)

        with self.assertRaises(JobConflict):
            await self.store.replace(stored, {**changed, "status": "failed"})

    async def test_report_is_private_and_presigned_for_thirty_minutes(self) -> None:
        await self.store.put_report("x402_" + "a" * 32, "# report")
        url = await self.store.presign_report("x402_" + "a" * 32)
        self.assertEqual(url, "https://signed.example/report")
        self.assertEqual(self.s3.presign_calls[-1]["ExpiresIn"], 1800)
        self.assertNotIn("ACL", self.s3.put_calls[-1])
        self.assertEqual(
            self.s3.put_calls[-1]["ContentType"], "text/markdown; charset=utf-8"
        )

    async def test_accounting_marker_is_private_immutable_and_conditional(
        self,
    ) -> None:
        job_id = "x402_" + "a" * 32
        marker = {
            "version": 1,
            "eventId": "b402:97:0xabc:0xdef",
            "settledAt": 2_000_000_000_000,
        }

        self.assertIsNone(await self.store.read_accounting_marker(job_id))
        self.assertTrue(
            await self.store.create_accounting_marker(job_id, marker)
        )
        self.assertFalse(
            await self.store.create_accounting_marker(job_id, marker)
        )
        self.assertEqual(
            await self.store.read_accounting_marker(job_id),
            marker,
        )

        put = self.s3.put_calls[-2]
        self.assertEqual(put["IfNoneMatch"], "*")
        self.assertNotIn("IfMatch", put)
        self.assertNotIn("ACL", put)
        self.assertEqual(
            put["Key"],
            f"x402-jobs/{job_id}/competition-reported.json",
        )

    async def test_accounting_marker_rejects_unbounded_or_malformed_data(
        self,
    ) -> None:
        job_id = "x402_" + "a" * 32
        for marker in (
            {"version": 2, "eventId": "event", "settledAt": 1},
            {"version": 1, "eventId": "", "settledAt": 1},
            {"version": 1, "eventId": "x" * 513, "settledAt": 1},
            {"version": 1, "eventId": "event", "settledAt": True},
            {
                "version": 1,
                "eventId": "event",
                "settledAt": 1,
                "unexpected": "field",
            },
        ):
            with self.subTest(marker=marker):
                with self.assertRaises(X402JobStoreError):
                    await self.store.create_accounting_marker(job_id, marker)

    async def test_attempt_reports_use_distinct_presigned_keys(self) -> None:
        job_id = "x402_" + "a" * 32
        first_report_id = "b" * 32
        second_report_id = "c" * 32

        await self.store.put_report(
            job_id,
            "# first",
            report_id=first_report_id,
        )
        await self.store.put_report(
            job_id,
            "# second",
            report_id=second_report_id,
        )
        await self.store.presign_report(
            job_id,
            report_id=second_report_id,
        )

        self.assertIn(
            f"x402-jobs/{job_id}/reports/{first_report_id}.md",
            self.s3.objects,
        )
        self.assertIn(
            f"x402-jobs/{job_id}/reports/{second_report_id}.md",
            self.s3.objects,
        )
        self.assertEqual(
            self.s3.presign_calls[-1]["Params"]["Key"],
            f"x402-jobs/{job_id}/reports/{second_report_id}.md",
        )

    async def test_read_returns_none_for_a_missing_job(self) -> None:
        self.assertIsNone(await self.store.read("x402_" + "a" * 32))

    async def test_read_rejects_non_object_json(self) -> None:
        key = "x402-jobs/x402_" + "a" * 32 + "/job.json"
        self.s3.objects[key] = (b"[]", '"etag-1"')
        with self.assertRaisesRegex(X402JobStoreError, "must be an object"):
            await self.store.read("x402_" + "a" * 32)

    async def test_read_rejects_job_json_over_256_kib(self) -> None:
        key = "x402-jobs/x402_" + "a" * 32 + "/job.json"
        self.s3.objects[key] = (b"x" * (256 * 1024 + 1), '"etag-1"')
        with self.assertRaisesRegex(X402JobStoreError, "exceeds 256 KiB"):
            await self.store.read("x402_" + "a" * 32)

    async def test_create_rejects_non_object_and_oversized_json(self) -> None:
        with self.assertRaisesRegex(X402JobStoreError, "must be an object"):
            await self.store.create([])  # type: ignore[arg-type]
        with self.assertRaisesRegex(X402JobStoreError, "exceeds 256 KiB"):
            await self.store.create({"jobId": "x402_" + "a" * 32, "data": "x" * (256 * 1024)})

    async def test_put_report_rejects_content_over_2_mib(self) -> None:
        with self.assertRaisesRegex(X402JobStoreError, "exceeds 2 MiB"):
            await self.store.put_report("x402_" + "a" * 32, "x" * (2 * 1024 * 1024 + 1))

    async def test_malformed_job_ids_are_rejected(self) -> None:
        with self.assertRaisesRegex(X402JobStoreError, "invalid x402 job id"):
            await self.store.read("x402_not-a-valid-id")
        with self.assertRaisesRegex(X402JobStoreError, "invalid x402 job id"):
            await self.store.put_report("x402_" + "A" * 32, "report")

    async def test_412_on_create_returns_none_and_replace_becomes_conflict(self) -> None:
        stored = await self.store.create(job_record())
        assert stored is not None
        self.s3.objects["x402-jobs/x402_" + "a" * 32 + "/job.json"] = (
            b"{}",
            '"another-etag"',
        )
        with self.assertRaises(JobConflict):
            await self.store.replace(stored, {**stored.record, "status": "failed"})

    async def test_create_retries_409_then_returns_none_for_an_existing_job(self) -> None:
        existing = await self.store.create(job_record())
        assert existing is not None
        self.s3.put_errors = [s3_error("409")]

        self.assertIsNone(await self.store.create(job_record()))
        self.assertEqual(len(self.s3.put_calls), 3)

    async def test_create_retries_409_then_returns_the_created_job(self) -> None:
        self.s3.put_errors = [s3_error("409")]

        stored = await self.store.create(job_record())

        self.assertIsNotNone(stored)
        self.assertEqual(len(self.s3.put_calls), 2)

    async def test_replace_retries_409_then_updates_the_job(self) -> None:
        stored = await self.store.create(job_record())
        assert stored is not None
        self.s3.put_errors = [s3_error("409")]

        updated = await self.store.replace(stored, {**stored.record, "status": "running"})

        self.assertEqual(updated.record["status"], "running")
        self.assertEqual(len(self.s3.put_calls), 3)

    async def test_replace_retries_409_then_maps_412_to_a_conflict(self) -> None:
        stored = await self.store.create(job_record())
        assert stored is not None
        self.s3.put_errors = [s3_error("409"), s3_error("412")]

        with self.assertRaises(JobConflict):
            await self.store.replace(stored, {**stored.record, "status": "failed"})

    async def test_repeated_409_conditional_writes_raise_job_conflict(self) -> None:
        self.s3.put_errors = [s3_error("409"), s3_error("409")]
        with self.assertRaises(JobConflict):
            await self.store.create(job_record())

        stored = await self.store.create(job_record())
        assert stored is not None
        self.s3.put_errors = [s3_error("409"), s3_error("409")]
        with self.assertRaises(JobConflict):
            await self.store.replace(stored, {**stored.record, "status": "failed"})


class X402JobStoreConfigTests(unittest.TestCase):
    def test_rejects_invalid_bucket_and_prefix(self) -> None:
        with self.assertRaisesRegex(X402JobStoreError, "X402_JOB_S3_BUCKET"):
            X402JobStoreConfig.from_env({"X402_JOB_S3_BUCKET": "invalid_bucket"})
        with self.assertRaisesRegex(X402JobStoreError, "X402_JOB_S3_PREFIX"):
            X402JobStoreConfig.from_env(
                {"X402_JOB_S3_BUCKET": "private-jobs", "X402_JOB_S3_PREFIX": "bad//prefix"}
            )

    def test_normalizes_a_valid_prefix(self) -> None:
        config = X402JobStoreConfig.from_env(
            {"X402_JOB_S3_BUCKET": "private-jobs", "X402_JOB_S3_PREFIX": "/x402-jobs/reports/"}
        )
        self.assertEqual(config.prefix, "x402-jobs/reports")


if __name__ == "__main__":
    unittest.main()
