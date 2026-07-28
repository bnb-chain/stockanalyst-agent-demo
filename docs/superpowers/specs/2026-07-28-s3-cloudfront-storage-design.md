# S3 + CloudFront Deliverable Storage Design

## Goal

Replace the deployed seller's default Pinata/local delivery path with a
production-capable S3 backend that publishes immutable, permanently readable
CloudFront URLs on BSC Testnet before the later mainnet cutover.

The existing buyer-provided UOMP relay remains an explicit per-job override.

## Architecture

The AgentCore runtime uploads each ERC-8183 `DeliverableManifest` to the private
S3 bucket `bnbagent-code-stock-analyst-agent`. A CloudFront distribution reads
only the `deliverables/*` prefix through Origin Access Control (OAC). The S3
bucket keeps all four public-access-block settings enabled.

The storage provider returns:

```text
https://<cloudfront-domain>/deliverables/<content-addressed-object>.json
```

The SDK writes that URL into the ERC-8183 submission `optParams`. Buyers fetch
the manifest through CloudFront and verify its canonical JSON commitment against
the on-chain `deliverable` value.

## Components

### `s3_storage.py`

Add `S3StorageProvider(StorageProvider)` with:

- `uses_file_url = False`
- `from_env()` for validated runtime configuration
- `upload(data, filename=None) -> str`
- `download(url) -> dict`
- `exists(url) -> bool`

Required environment variables:

```dotenv
DELIVERABLE_S3_BUCKET=bnbagent-code-stock-analyst-agent
DELIVERABLE_PUBLIC_BASE=https://<distribution-domain>
```

Optional:

```dotenv
DELIVERABLE_S3_PREFIX=deliverables
```

The prefix defaults to `deliverables`. The bucket and public base must either
both be present or both be absent. Partial S3 configuration is a startup error.

### Runtime installation

After `_load_runtime_secrets()` in `main.py`, install the provider by replacing
`bnbagent_studio_core.storage.storage_provider_from_config` with a factory that
returns `S3StorageProvider.from_env()`.

If neither required S3 variable is present, local development retains the
existing `[storage].kind` behavior. A deployed runtime is configured with both
variables and therefore cannot silently fall back to `file://`.

The existing `signing.submit_result()` lock and UOMP relay override remain
unchanged: a signed buyer gateway temporarily replaces the S3 factory for that
job and restores S3 afterward.

## Object format and naming

The upload body is canonical JSON:

```python
json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
```

This is byte-for-byte compatible with `StorageProvider.compute_hash()` and the
ERC-8183 manifest commitment.

Object keys are content-addressed. The provider validates the optional filename
as a single safe basename, then combines its stem with the canonical body's
SHA-256 digest:

```text
deliverables/erc8183-job-397-<sha256>.json
```

Retries producing identical manifests reuse the same immutable object. A
different manifest gets a different key, preventing CloudFront from serving a
stale object for an overwritten job filename.

Uploads set:

```text
Content-Type: application/json
Cache-Control: public,max-age=31536000,immutable
```

No ACL is supplied; bucket ownership and public access remain controlled by S3
and CloudFront OAC.

## Read safety and errors

`download()` and `exists()` accept only HTTPS URLs whose origin equals
`DELIVERABLE_PUBLIC_BASE` and whose path is under the configured prefix. They
never fetch arbitrary HTTP URLs.

After URL validation, reads use the S3 API directly:

- `download()` calls `get_object`, applies a 2 MiB response ceiling, decodes
  UTF-8 JSON, and requires the root value to be an object.
- `exists()` calls `head_object`; S3 404/NoSuchKey returns `False`.
- AWS authorization, throttling, timeout, malformed JSON, and oversized payload
  errors propagate as explicit storage errors.

`upload()` rejects unsafe filenames, non-dictionary input, serialization
failure, and configuration errors before calling S3.

## AWS resources and permissions

Bucket:

```text
bnbagent-code-stock-analyst-agent
region: us-east-1
public access: fully blocked
```

CloudFront:

- S3 REST origin (not S3 website hosting)
- OAC with SigV4, always sign
- HTTPS redirect for viewers
- GET/HEAD only
- compression enabled
- default root object unset

Bucket policy grants `s3:GetObject` on
`arn:aws:s3:::bnbagent-code-stock-analyst-agent/deliverables/*` only to
`cloudfront.amazonaws.com`, conditioned on the created distribution ARN.

The AgentCore execution role `bnbagent-stockanalyst-runtime` receives only:

```json
{
  "Effect": "Allow",
  "Action": "s3:PutObject",
  "Resource": "arn:aws:s3:::bnbagent-code-stock-analyst-agent/deliverables/*"
}
```

The deployment operator may read and write test objects with the existing
`bnbagent-code-*/*` permissions. No runtime `GetObject`, bucket-list, bucket
policy, or CloudFront management permission is required.

## Testing

Unit tests use a fake S3 client and cover:

- required and partial environment configuration
- canonical JSON bytes and content-addressed keys
- upload headers and absence of ACLs
- public CloudFront URL construction
- unsafe filename and prefix rejection
- same-origin/prefix enforcement for reads
- 2 MiB download limit and JSON-object validation
- `exists()` 404 versus non-404 behavior
- installation only when both environment variables are present
- restoration of S3 after the existing UOMP per-job override

After unit and full agent tests pass:

1. Create OAC and CloudFront distribution.
2. Apply the scoped bucket policy.
3. Add the scoped runtime-role write policy.
4. Store `DELIVERABLE_S3_BUCKET`, `DELIVERABLE_PUBLIC_BASE`, and prefix in the
   runtime secret bundle through `bag deploy`.
5. Run one BSC Testnet job in default-delivery mode.
6. Verify the on-chain URL uses CloudFront, anonymous GET succeeds, the manifest
   job/chain/contracts match, and its canonical commitment equals the chain.

## Non-goals

- No public S3 bucket or presigned URLs.
- No custom CloudFront domain in this phase.
- No CloudFront WAF or signed URLs; ERC-8183 deliverables are intentionally
  publicly readable.
- No mainnet configuration change.
- No change to UOMP relay behavior, OAuth gateway, poller, or settlement.
