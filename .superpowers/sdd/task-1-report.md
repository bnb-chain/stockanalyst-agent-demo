# Task 1 report: Verify SDK manifests against the on-chain commitment

## Implementation

- Added `lossless-json` as a production dependency.
- Added `verifyDeliverableManifest(rawText, expected)`, which enforces the 2 MiB byte limit, losslessly parses JSON numeric tokens, recursively produces the SDK-compatible sorted compact JSON, checks version/job/chain/contracts/response fields, and compares the Keccak-256 commitment before returning response content.
- Added `ERC8183Buyer.getDeliverableCommitment(jobId)`, which reads and shape-validates the on-chain 32-byte deliverable commitment.
- Added focused verifier and commitment-reader tests, including a job ID above `2^53`.

## Files

- `buyer-client/package.json`
- `buyer-client/package-lock.json`
- `buyer-client/src/deliverable.ts`
- `buyer-client/src/deliverable.test.ts`
- `buyer-client/src/erc8183.ts`
- `buyer-client/src/erc8183.test.ts`

## TDD evidence

### RED: verifier

Command:

```sh
cd buyer-client && npm run build
```

Result: expected failure, before `deliverable.ts` existed:

```text
src/deliverable.test.ts(5,43): error TS2307: Cannot find module './deliverable.js'
```

### GREEN: verifier

Command:

```sh
cd buyer-client && npm run build && /Users/zhaoyu/.nvm/versions/node/v20.9.0/bin/node --test dist/deliverable.test.js
```

Result: build succeeded; 9 tests passed.

### RED: commitment reader

Command:

```sh
cd buyer-client && npm run build
```

Result: expected failure, before the method existed:

```text
src/erc8183.test.ts(16,17): error TS2339: Property 'getDeliverableCommitment' does not exist on type 'ERC8183Buyer'.
```

### GREEN: commitment reader

Command:

```sh
cd buyer-client && npm run build && /Users/zhaoyu/.nvm/versions/node/v20.9.0/bin/node --test dist/erc8183.test.js
```

Result: build succeeded; 1 test passed.

### Full buyer suite

Command:

```sh
cd buyer-client && npm run build && /Users/zhaoyu/.nvm/versions/node/v20.9.0/bin/node --test dist/*.test.js
```

Result: build succeeded; 41 tests passed, 0 failed. The run was repeated outside the filesystem sandbox solely because existing gateway tests bind local `127.0.0.1` servers, which the sandbox rejects with `EPERM`.

## Self-review

- Confirmed numbers remain `LosslessNumber` tokens until integer validation; no `Number` conversion is used for manifest values.
- Confirmed canonicalization recursively sorts object keys, emits compact JSON, preserves numeric token text, and escapes non-ASCII text in Python-compatible UTF-16 escape units.
- Confirmed the commitment is checked only after context validation and comparison is case-insensitive for an EVM hex hash.
- Confirmed EVM contract addresses are checksum-normalized before comparison.
- Ran `git diff --check`; no whitespace errors.

## Concerns

- The workspace default Node is v16.16.0, which does not support `node --test`. Node v20.9.0 is installed locally and was used for all test executions.
- The supplied test fixture labeled `canonicalManifest` ordered a nested object as `b,a`; SDK-compatible recursive sorting requires `a,b`. The fixture was corrected so its expected commitment is genuinely canonical rather than weakening the required protocol.
