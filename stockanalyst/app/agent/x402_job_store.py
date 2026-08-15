from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

_JOB_ID_RE = re.compile(r"x402_[0-9a-f]{32}\Z")
_REPORT_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_WALLET_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_PREFIX_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_MAX_JOB_BYTES = 256 * 1024
_MAX_REPORT_BYTES = 2 * 1024 * 1024
_MAX_ACCOUNTING_MARKER_BYTES = 1024
_MAX_ACCOUNTING_EVENT_ID_LENGTH = 512
_MAX_WALLET_RATE_LIMIT_BYTES = 16 * 1024
_MAX_WALLET_RATE_LIMIT_ENTRIES = 30
_MAX_TIMESTAMP_MILLISECONDS = 9_999_999_999_999


class X402JobStoreError(RuntimeError):
    pass


class JobConflict(X402JobStoreError):
    pass


@dataclass(frozen=True)
class X402JobStoreConfig:
    bucket: str
    prefix: str = "x402-jobs"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> X402JobStoreConfig:
        bucket = env.get("X402_JOB_S3_BUCKET", "").strip()
        prefix = env.get("X402_JOB_S3_PREFIX", "x402-jobs").strip().strip("/")
        if not bucket or not _BUCKET_RE.fullmatch(bucket):
            raise X402JobStoreError("X402_JOB_S3_BUCKET is required and must be valid")
        if not prefix or any(
            not _PREFIX_SEGMENT_RE.fullmatch(segment) for segment in prefix.split("/")
        ):
            raise X402JobStoreError("X402_JOB_S3_PREFIX is invalid")
        return cls(bucket=bucket, prefix=prefix)


@dataclass(frozen=True)
class StoredJob:
    record: dict[str, Any]
    etag: str


@dataclass(frozen=True)
class StoredWalletRateLimit:
    record: dict[str, Any]
    etag: str


class X402JobStore:
    def __init__(self, config: X402JobStoreConfig, s3_client: Any) -> None:
        self.config = config
        self._s3 = s3_client

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] = os.environ,
        *,
        s3_client: Any | None = None,
    ) -> X402JobStore:
        return cls(
            X402JobStoreConfig.from_env(env),
            s3_client if s3_client is not None else boto3.client("s3"),
        )

    def _job_key(self, job_id: str) -> str:
        if not _JOB_ID_RE.fullmatch(job_id):
            raise X402JobStoreError("invalid x402 job id")
        return f"{self.config.prefix}/{job_id}/job.json"

    def _report_key(
        self,
        job_id: str,
        report_id: str | None = None,
    ) -> str:
        self._job_key(job_id)
        if report_id is not None:
            if not _REPORT_ID_RE.fullmatch(report_id):
                raise X402JobStoreError("invalid x402 report id")
            return (
                f"{self.config.prefix}/{job_id}/reports/{report_id}.md"
            )
        return f"{self.config.prefix}/{job_id}/report.md"

    def _accounting_marker_key(self, job_id: str) -> str:
        self._job_key(job_id)
        return (
            f"{self.config.prefix}/{job_id}/"
            "competition-reported.json"
        )

    def _wallet_rate_limit_key(self, wallet_digest: str) -> str:
        if not _WALLET_DIGEST_RE.fullmatch(wallet_digest):
            raise X402JobStoreError("invalid wallet digest")
        return (
            f"{self.config.prefix}/rate-limits/v1/"
            f"{wallet_digest}.json"
        )

    @staticmethod
    def _encode(record: dict[str, Any]) -> bytes:
        if not isinstance(record, dict):
            raise X402JobStoreError("job record must be an object")
        try:
            body = json.dumps(
                record, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        except (TypeError, ValueError) as exc:
            raise X402JobStoreError("job record must be finite JSON") from exc
        if len(body) > _MAX_JOB_BYTES:
            raise X402JobStoreError("job record exceeds 256 KiB")
        return body

    @staticmethod
    def _encode_wallet_rate_limit(record: dict[str, Any]) -> bytes:
        if (
            not isinstance(record, dict)
            or set(record) != {"version", "entries"}
            or type(record.get("version")) is not int
            or record.get("version") != 1
        ):
            raise X402JobStoreError("invalid wallet rate-limit record")
        entries = record.get("entries")
        if (
            not isinstance(entries, list)
            or len(entries) > _MAX_WALLET_RATE_LIMIT_ENTRIES
        ):
            raise X402JobStoreError("invalid wallet rate-limit entries")
        reservation_ids: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {
                "reservationId",
                "reservedAt",
                "state",
            }:
                raise X402JobStoreError("invalid wallet rate-limit entry")
            reservation_id = entry.get("reservationId")
            reserved_at = entry.get("reservedAt")
            if (
                not isinstance(reservation_id, str)
                or not _JOB_ID_RE.fullmatch(reservation_id)
                or reservation_id in reservation_ids
                or type(reserved_at) is not int
                or not 1 <= reserved_at <= _MAX_TIMESTAMP_MILLISECONDS
                or entry.get("state") not in {"reserved", "committed"}
            ):
                raise X402JobStoreError("invalid wallet rate-limit entry")
            reservation_ids.add(reservation_id)
        try:
            body = json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        except (TypeError, ValueError) as exc:
            raise X402JobStoreError(
                "wallet rate-limit record must be finite JSON"
            ) from exc
        if len(body) > _MAX_WALLET_RATE_LIMIT_BYTES:
            raise X402JobStoreError(
                "wallet rate-limit record exceeds 16 KiB"
            )
        return body

    @staticmethod
    def _copy_wallet_rate_limit_record(
        record: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "entries": [dict(entry) for entry in record["entries"]],
        }

    async def _conditional_put(self, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(2):
            try:
                return await asyncio.to_thread(self._s3.put_object, **kwargs)
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code"))
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if code in {"409", "ConditionalRequestConflict"} or status == 409:
                    if attempt == 0:
                        continue
                    raise JobConflict("job changed concurrently") from exc
                raise
        raise AssertionError("unreachable")

    async def create(self, record: dict[str, Any]) -> StoredJob | None:
        body = self._encode(record)
        try:
            response = await self._conditional_put(
                Bucket=self.config.bucket,
                Key=self._job_key(str(record.get("jobId", ""))),
                Body=body,
                ContentType="application/json",
                CacheControl="no-store",
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in {"412", "PreconditionFailed"}:
                return None
            raise
        return StoredJob(record=dict(record), etag=str(response["ETag"]))

    async def create_wallet_rate_limit(
        self,
        wallet_digest: str,
        record: dict[str, Any],
    ) -> StoredWalletRateLimit | None:
        body = self._encode_wallet_rate_limit(record)
        try:
            response = await self._conditional_put(
                Bucket=self.config.bucket,
                Key=self._wallet_rate_limit_key(wallet_digest),
                Body=body,
                ContentType="application/json",
                CacheControl="private,no-store",
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in {
                "412",
                "PreconditionFailed",
            }:
                return None
            raise
        return StoredWalletRateLimit(
            record=self._copy_wallet_rate_limit_record(record),
            etag=str(response["ETag"]),
        )

    async def read_wallet_rate_limit(
        self,
        wallet_digest: str,
    ) -> StoredWalletRateLimit | None:
        try:
            response = await asyncio.to_thread(
                self._s3.get_object,
                Bucket=self.config.bucket,
                Key=self._wallet_rate_limit_key(wallet_digest),
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in {
                "404",
                "NoSuchKey",
            }:
                return None
            raise
        body = await asyncio.to_thread(
            response["Body"].read,
            _MAX_WALLET_RATE_LIMIT_BYTES + 1,
        )
        if len(body) > _MAX_WALLET_RATE_LIMIT_BYTES:
            raise X402JobStoreError(
                "wallet rate-limit record exceeds 16 KiB"
            )
        try:
            record = json.loads(
                body.decode(),
                parse_constant=lambda value: (
                    (_ for _ in ()).throw(ValueError(value))
                ),
            )
        except (AttributeError, TypeError, UnicodeDecodeError, ValueError) as exc:
            raise X402JobStoreError("invalid wallet rate-limit record") from exc
        self._encode_wallet_rate_limit(record)
        return StoredWalletRateLimit(
            record=self._copy_wallet_rate_limit_record(record),
            etag=str(response["ETag"]),
        )

    async def replace_wallet_rate_limit(
        self,
        wallet_digest: str,
        stored: StoredWalletRateLimit,
        record: dict[str, Any],
    ) -> StoredWalletRateLimit:
        if not isinstance(stored, StoredWalletRateLimit):
            raise X402JobStoreError("invalid stored wallet rate-limit record")
        body = self._encode_wallet_rate_limit(record)
        try:
            response = await self._conditional_put(
                Bucket=self.config.bucket,
                Key=self._wallet_rate_limit_key(wallet_digest),
                Body=body,
                ContentType="application/json",
                CacheControl="private,no-store",
                IfMatch=stored.etag,
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in {
                "412",
                "PreconditionFailed",
            }:
                raise JobConflict("wallet rate limit changed concurrently") from exc
            raise
        return StoredWalletRateLimit(
            record=self._copy_wallet_rate_limit_record(record),
            etag=str(response["ETag"]),
        )

    async def read(self, job_id: str) -> StoredJob | None:
        try:
            response = await asyncio.to_thread(
                self._s3.get_object,
                Bucket=self.config.bucket,
                Key=self._job_key(job_id),
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in {"404", "NoSuchKey"}:
                return None
            raise
        body = await asyncio.to_thread(response["Body"].read, _MAX_JOB_BYTES + 1)
        if len(body) > _MAX_JOB_BYTES:
            raise X402JobStoreError("job record exceeds 256 KiB")
        value = json.loads(
            body.decode(),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        if not isinstance(value, dict):
            raise X402JobStoreError("job record must be an object")
        return StoredJob(record=value, etag=str(response["ETag"]))

    async def replace(self, stored: StoredJob, record: dict[str, Any]) -> StoredJob:
        body = self._encode(record)
        try:
            response = await self._conditional_put(
                Bucket=self.config.bucket,
                Key=self._job_key(str(record.get("jobId", ""))),
                Body=body,
                ContentType="application/json",
                CacheControl="no-store",
                IfMatch=stored.etag,
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in {"412", "PreconditionFailed"}:
                raise JobConflict("job changed concurrently") from exc
            raise
        return StoredJob(record=dict(record), etag=str(response["ETag"]))

    @staticmethod
    def _encode_accounting_marker(marker: dict[str, Any]) -> bytes:
        if not isinstance(marker, dict) or set(marker) != {
            "version",
            "eventId",
            "settledAt",
        }:
            raise X402JobStoreError("invalid accounting marker")
        event_id = marker.get("eventId")
        settled_at = marker.get("settledAt")
        if (
            marker.get("version") != 1
            or not isinstance(event_id, str)
            or not 1 <= len(event_id) <= _MAX_ACCOUNTING_EVENT_ID_LENGTH
            or isinstance(settled_at, bool)
            or not isinstance(settled_at, int)
            or settled_at <= 0
        ):
            raise X402JobStoreError("invalid accounting marker")
        body = json.dumps(
            marker,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if len(body) > _MAX_ACCOUNTING_MARKER_BYTES:
            raise X402JobStoreError("accounting marker exceeds 1 KiB")
        return body

    async def read_accounting_marker(
        self,
        job_id: str,
    ) -> dict[str, Any] | None:
        try:
            response = await asyncio.to_thread(
                self._s3.get_object,
                Bucket=self.config.bucket,
                Key=self._accounting_marker_key(job_id),
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in {
                "404",
                "NoSuchKey",
            }:
                return None
            raise
        body = await asyncio.to_thread(
            response["Body"].read,
            _MAX_ACCOUNTING_MARKER_BYTES + 1,
        )
        if len(body) > _MAX_ACCOUNTING_MARKER_BYTES:
            raise X402JobStoreError("accounting marker exceeds 1 KiB")
        try:
            marker = json.loads(
                body.decode(),
                parse_constant=lambda value: (
                    (_ for _ in ()).throw(ValueError(value))
                ),
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise X402JobStoreError("invalid accounting marker") from exc
        self._encode_accounting_marker(marker)
        return dict(marker)

    async def create_accounting_marker(
        self,
        job_id: str,
        marker: dict[str, Any],
    ) -> bool:
        body = self._encode_accounting_marker(marker)
        try:
            await self._conditional_put(
                Bucket=self.config.bucket,
                Key=self._accounting_marker_key(job_id),
                Body=body,
                ContentType="application/json",
                CacheControl="private,no-store",
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in {
                "412",
                "PreconditionFailed",
            }:
                return False
            raise
        return True

    async def put_report(
        self,
        job_id: str,
        markdown: str,
        *,
        report_id: str | None = None,
    ) -> None:
        body = markdown.encode("utf-8")
        if len(body) > _MAX_REPORT_BYTES:
            raise X402JobStoreError("report exceeds 2 MiB")
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self.config.bucket,
            Key=self._report_key(job_id, report_id),
            Body=body,
            ContentType="text/markdown; charset=utf-8",
            CacheControl="private,no-store",
        )

    async def presign_report(
        self,
        job_id: str,
        *,
        report_id: str | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self._s3.generate_presigned_url,
            "get_object",
            Params={
                "Bucket": self.config.bucket,
                "Key": self._report_key(job_id, report_id),
                "ResponseContentType": "text/markdown; charset=utf-8",
            },
            ExpiresIn=1800,
        )
