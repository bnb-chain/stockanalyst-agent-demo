# Buyer Deliverable Verification and Tunnel Discovery

## Objective

Prevent the buyer from approving escrow release unless it has fetched and
cryptographically verified the deliverable submitted for the current ERC-8183
job. Also make local `cloudflared` discovery honor every documented executable
location instead of always returning the first candidate.

The successful E2E flow remains automatic: a valid deliverable is displayed and
the buyer still attempts `settle`. Fetch or verification failures become hard
settlement blockers.

## Scope

This change covers:

- TypeScript-side validation of the SDK `DeliverableManifest`;
- comparison with the on-chain `Job.deliverable` commitment;
- fail-closed settlement orchestration in the buyer CLI;
- executable discovery for the bundled tunnel launcher; and
- focused and regression tests for those behaviors.

It does not attempt to judge whether financial opinions are correct or useful,
add a new dispute transaction, change the contract, or change the seller's
manifest format.

## Protocol Source of Truth

The seller SDK constructs a version 1 manifest with exactly these top-level
fields:

```text
version, job_id, chain_id, contracts, response, metadata
```

It computes the on-chain `bytes32 deliverable` as:

```text
keccak256(UTF-8(JSON(manifest, recursively sorted object keys, compact separators)))
```

The buyer must reproduce this algorithm. Hashing the downloaded response bytes
directly is incorrect because insignificant whitespace and object-key order are
not part of the SDK commitment.

## Components

### Deliverable verifier

Add a focused buyer module that accepts:

- the downloaded response body;
- the expected `jobId`;
- the expected chain ID;
- the configured Commerce, Router, and Policy addresses; and
- the on-chain `bytes32` deliverable commitment.

It returns the verified report content or throws a descriptive validation
error. It must:

1. enforce the buyer's existing bounded payload policy before parsing;
2. parse a JSON object and require manifest version `1`;
3. require an unsigned integer `job_id` that exactly equals the current
   `bigint` job ID without converting through JavaScript `number`;
4. require the configured chain ID;
5. require exact Commerce, Router, and Policy addresses using
   case-insensitive EVM-address comparison;
6. require `response.content` and `response.content_type` strings;
7. retain all manifest fields, including metadata and extension fields, when
   canonicalizing so any committed mutation is detected;
8. recursively sort object keys while preserving array order;
9. serialize compact JSON, hash the UTF-8 bytes with Keccak-256, and compare the
   result to the on-chain commitment; and
10. return only `response.content` after every check succeeds.

JSON numeric precision must not affect job identity. The verifier will preserve
the original JSON integer token for `job_id` or use another parser strategy
that proves exact equality before normal JavaScript parsing can round it.

### Chain reader

Extend the buyer's ERC-8183 wrapper with a read-only method that calls
`Commerce.getJob(jobId)` and returns the job's `deliverable` value. The value
must be a valid 32-byte hex string. The existing URL lookup remains unchanged.

### Settlement gate

Refactor the executable buyer flow so settlement is reachable only after a
verified report has been obtained.

These conditions block settlement:

- no on-chain deliverable URL;
- unsupported or malformed URL;
- download exception;
- non-success HTTP status;
- response body over the configured bound;
- malformed or unsupported manifest;
- job, chain, or contract context mismatch; or
- manifest hash mismatch.

A local HTML/PDF rendering failure does not block settlement because rendering
is not part of the submitted manifest. The original authenticated relay fetch
rules remain unchanged.

After successful verification, the client retains its current behavior:
display the report, attempt `settle`, and handle the active 24-hour dispute
window by printing the existing manual-settlement guidance.

### Cloudflared discovery

Replace the unconditional first-candidate return with a small executable
resolver:

1. resolve `cloudflared` through the supplied or process `PATH`;
2. check `$HOME/.local/bin/cloudflared` when `HOME` is present;
3. check `/usr/local/bin/cloudflared`;
4. check `/opt/homebrew/bin/cloudflared`; and
5. return the first regular executable file.

If no candidate is executable, fail before spawning with a clear installation
message. Discovery dependencies such as environment values and filesystem
checks must be injectable so tests do not depend on the host machine.

## Error Handling

Verification errors must identify the failed invariant without logging report
contents, tokens, or other private manifest data. The top-level CLI must emit a
clear “settlement blocked” error and exit unsuccessfully rather than continuing
to Step 7.

Tunnel discovery errors must list the supported discovery mechanism, not expose
unrelated environment values, and must occur before a child process is created.

## Compatibility

The externally visible successful flow and seller protocol remain unchanged:

- no changes to negotiation or `notify_funded`;
- no changes to gateway authentication;
- no changes to manifest generation;
- no changes to the contract or settlement call;
- no additional runtime language or service dependency; and
- verified jobs continue to attempt automatic settlement.

Previously tolerated raw-text storage backends are intentionally rejected
because their content cannot be authenticated against the SDK manifest
commitment.

## Test Strategy

Use test-driven development. Each production behavior must first be expressed
by a failing test.

Verifier tests cover:

- a valid SDK-compatible manifest;
- different whitespace and key order producing the same canonical hash;
- exact job IDs above `2^53`;
- changed report content;
- wrong job ID, chain ID, or any contract address;
- invalid version, missing fields, wrong field types, malformed JSON, and an
  oversized body; and
- nested metadata and arrays participating in the commitment.

Flow tests cover:

- absent URL, failed fetch, non-2xx response, malformed manifest, and hash
  mismatch never invoking settlement;
- a valid manifest invoking settlement once; and
- PDF generation failure after verification not blocking settlement.

Discovery tests cover:

- a PATH executable taking precedence;
- fallback to each supported absolute location;
- non-executable files being ignored; and
- a clear error when all candidates are absent.

Finally run the complete buyer test suite and TypeScript build. The existing
Python suite is also run as a repository regression check even though seller
code is not changed.

## Acceptance Criteria

1. No control-flow path can call buyer `settle` without a successfully verified
   manifest for that job.
2. The verifier reproduces the installed seller SDK's canonical manifest hash,
   including job IDs larger than JavaScript's safe integer range.
3. Every fetch, context, schema, and integrity failure blocks settlement.
4. Valid jobs preserve the current display, rendering, settlement, and dispute
   window behavior.
5. `findCloudflared` selects the first actually executable supported candidate
   and reports a clear error if none exists.
6. Focused tests, the full buyer suite, TypeScript compilation, and the Python
   regression suite pass.
