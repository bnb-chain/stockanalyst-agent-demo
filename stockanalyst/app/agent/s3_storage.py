from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import boto3
from bnbagent.storage import StorageProvider

_DEFAULT_PREFIX = "deliverables"
_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_OBJECT_RE = re.compile(
    r"(?P<stem>[A-Za-z0-9][A-Za-z0-9._-]{0,199})-"
    r"(?P<digest>[0-9a-f]{64})\.json\Z"
)


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


class S3StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class S3StorageConfig:
    bucket: str
    public_base: str
    prefix: str = _DEFAULT_PREFIX

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> S3StorageConfig:
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
        try:
            _ = parsed.port
        except ValueError as exc:
            raise S3StorageError("DELIVERABLE_PUBLIC_BASE has an invalid port") from exc
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
    ) -> S3StorageProvider:
        return cls(
            S3StorageConfig.from_env(env),
            s3_client if s3_client is not None else boto3.client("s3"),
        )

    @staticmethod
    def _safe_stem(filename: str | None) -> str:
        value = filename or "manifest.json"
        if not _SEGMENT_RE.fullmatch(value):
            raise S3StorageError("filename must be one safe basename")
        return value.removesuffix(".json")

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

    def _key_from_url(self, url: str) -> tuple[str, str]:
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
        remainder = path[len(prefix) :]
        match = _OBJECT_RE.fullmatch(remainder)
        if "/" in remainder or match is None:
            raise S3StorageError("deliverable object key must be content-addressed")
        return path, match["digest"]

    async def download(self, url: str) -> dict:
        key, expected_digest = self._key_from_url(url)
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
            value = json.loads(
                body.decode("utf-8"),
                parse_constant=_reject_nonfinite_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise S3StorageError("S3 deliverable is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise S3StorageError("S3 deliverable root must be an object")
        try:
            canonical = json.dumps(
                value, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise S3StorageError("S3 deliverable is not valid canonical JSON") from exc
        if canonical != body:
            raise S3StorageError("S3 deliverable JSON is not canonical")
        if hashlib.sha256(canonical).hexdigest() != expected_digest:
            raise S3StorageError("S3 deliverable digest does not match its key")
        return value

    async def exists(self, url: str) -> bool:
        from botocore.exceptions import ClientError

        key, _ = self._key_from_url(url)
        try:
            await asyncio.to_thread(
                self._s3.head_object,
                Bucket=self._config.bucket,
                Key=key,
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey"}:
                return False
            raise
        return True


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
