# Private Payload Relay Final Review Fix Report

**Date:** 2026-07-23

**Baseline:** `4d96a13` (`fix: reject empty relay URL delimiters`)

**Scope:** Complete remediation of the two Medium and four Low findings from
the final private-payload-relay review.

## Design and plan review

The approved design and implementation plan, current source and tests,
`.superpowers/sdd/progress.md`, and the complete final-review result were read
before implementation.

There is no protocol or security-invariant conflict. The review's requirement
to remove the successful-upload `Buffer.concat` supersedes the plan's stale
concatenation implementation detail. It does not change the protocol:

- one payload still accepts at most 2 MiB of logical body data;
- stored plus in-flight retained capacity is at most 16 MiB;
- stored plus active uploads is at most 32 slots;
- `413` remains per-payload overflow and `507` remains retained-capacity or
  slot exhaustion;
- no stored payload is evicted; and
- every failure path still releases its reservation exactly once.

Capacity rounding may conservatively return `507` before logical payload bytes
sum to 16 MiB. This is intentional: actual retained allocation, rather than
logical bytes alone, is the protected resource.

## TDD evidence

Only tests were changed before the production changes.

### RED — Node 24 gateway

Command:

```sh
cd buyer-client
/Users/zhaoyu/.nvm/versions/node/v24.10.0/bin/node \
  ../../apex-contracts/node_modules/tsx/dist/cli.mjs \
  --test src/gateway.test.ts
```

The sandbox initially denied tsx's local IPC socket with `listen EPERM`; the
same local-only command was rerun with the required permission. The valid RED
run produced 18 tests: 15 passed and these three failed for the expected
reasons:

1. `charges retained page capacity to the aggregate byte limit`
   - actual `200`, expected `507`;
   - proved that logical bytes, not retained allocation, controlled admission.
2. `coalesces raw one-byte HTTP chunks into bounded retained pages`
   - storage observation was absent;
   - the raw HTTP/1.1 request contained 10,000 transfer-encoded one-byte
     chunks.
3. `releases the active slot exactly once after an aborted upload`
   - observed `{ writeHead: 1, end: 1 }`, expected both zero;
   - proved the handler wrote to a request whose socket had died.

The new invalid-Bearer matrix and deterministic error-only cleanup test passed
on RED. Those two review items were missing deletion-sensitive coverage, not
known production defects.

### RED — Python UOMP

Command:

```sh
stockanalyst/app/agent/.venv/bin/python -m unittest \
  stockanalyst.app.agent.tests.test_uomp_storage -v
```

The RED run executed 26 tests and failed only the intended download-limit and
port-zero assertions:

- a syntactically valid JSON response of exactly 2,097,152 bytes raised
  `ValueError: gateway response too large`;
- the ordinary download still requested only 65,537 bytes instead of
  2,097,153; and
- an explicit `:0` locator was accepted instead of raising before opening an
  authenticated request.

### GREEN — focused suites

After the minimal production changes:

```text
Node 24 gateway: 18 tests, 18 passed
Python UOMP:      26 tests, 26 passed
```

The focused strict gateway typecheck also passed:

```sh
/Users/zhaoyu/.nvm/versions/node/v24.10.0/bin/node \
  ../../apex-contracts/node_modules/typescript/bin/tsc \
  src/gateway.ts src/gateway.test.ts \
  --target ES2022 --module NodeNext --moduleResolution NodeNext \
  --strict --noEmit --types node \
  --typeRoots ../../apex-contracts/node_modules/@types --skipLibCheck
```

## Fixes

### 1. Bounded relay memory

`buyer-client/src/gateway.ts` now copies incoming parser chunks into dedicated,
unpooled `Buffer.allocUnsafeSlow` pages. Page size is derived so each payload
retains at most 32 segments; the production 2 MiB limit yields 64 KiB pages.
Consequently all 32 stored/active slots retain at most 1,024 segment objects.

The accounting counters now track allocated page capacity for both stored and
in-flight payloads. Capacity is checked atomically before allocation. A failed
allocation or admission clears the partial page list and goes through the same
idempotent release closure.

Successful uploads move their page array directly into the payload Map. They
perform no final full-payload copy. GET writes the initialized portion of each
stored page and ends on the last one; HEAD returns only metadata.

The raw regression proved that 10,000 one-byte HTTP chunks caused 10,000
`data` events but retained only one 65,536-byte segment. A separate rounded
capacity test stores nine bytes as three four-byte pages under injected limits,
then proves another page is rejected with `507`.

### 2. Python download boundary

`_read_bounded_json` now requires a per-call `max_bytes` value:

- upload acknowledgement: 65,536 bytes; and
- downloaded payload JSON: 2,097,152 bytes.

Boundary-valid JSON at exactly 2 MiB is accepted. Boundary-valid JSON at
2 MiB + 1 byte is rejected as too large. The upload acknowledgement test still
proves a 65,537-byte bounded read.

### 3. Explicit port zero

Python effective-port selection now distinguishes `None` from `0`. An explicit
`:0` therefore differs from the validated default HTTPS port and is rejected
by `_validate_payload_url` before request construction. Download and exists
both prove the fake opener receives no request.

TypeScript parity coverage includes the same `:0` locator and proves it receives
no relay Authorization header.

### 4. Dead request socket

Upload errors now skip `writeHead` and `end` when any of these are true:

```text
req.aborted
req.destroyed
req.socket.destroyed
res.headersSent
res.writableEnded
res.destroyed
```

The real aborted-upload test still proves slot recovery and exact-once release,
and now also proves zero response writes.

### 5. Error-only cleanup

A deterministic synthetic `IncomingMessage` emits `data` followed by `error`
without any `aborted` or `close` event. The test requires a `400`, then starts a
real server with the same handler and proves a full-capacity upload succeeds.
Removing the production `error` listener leaves the slot reserved and makes
this test fail.

### 6. Authentication and URL leakage coverage

After creating an existing payload, POST upload plus GET and HEAD for that
existing ID must return `401` for:

- an unrelated token;
- an equal-length wrong token;
- a strict token prefix;
- a value with the expected token as a prefix; and
- a value with the expected token as a suffix.

Responses are also checked not to disclose the expected token.

Buyer fake-fetch assertions prove that authenticated calls receive the exact
original locator string, the token appears only in the Authorization header,
and the URL has no userinfo, query, or fragment. All untrusted and normalized
locator cases also preserve the exact original URL while omitting
Authorization.

## Complete verification

All buyer-focused Node 24 tests passed with a temporary mapping to the already
installed sibling dependencies:

```sh
cd buyer-client
/Users/zhaoyu/.nvm/versions/node/v24.10.0/bin/node \
  ../../apex-contracts/node_modules/tsx/dist/cli.mjs \
  --tsconfig tsconfig.codex-check.json \
  --test src/gateway.test.ts src/notify-auth.test.ts \
  src/pdf-renderer.test.ts src/pdf-report.test.ts
```

```text
31 tests, 31 passed
```

The runtime map used `ethers/lib.esm/index.js`. The strict whole-project check
used `ethers/lib.esm/index.d.ts` and sibling Node type roots:

```sh
../apex-contracts/node_modules/.bin/tsc \
  -p buyer-client/tsconfig.codex-check.json
```

It passed with no output. The temporary configuration was removed.

Python verification:

```sh
stockanalyst/app/agent/.venv/bin/python -m unittest discover \
  -s stockanalyst/app/agent/tests -p 'test_*.py' -v
```

```text
Ran 80 tests in 0.618s
OK
```

```sh
stockanalyst/app/agent/.venv/bin/python -m py_compile \
  stockanalyst/app/agent/uomp_storage.py \
  stockanalyst/app/agent/tests/test_uomp_storage.py
```

`py_compile` and `git diff --check` both passed.

The required legacy scan found no relay use of `randomBytes(4)`, no
`payload_id is unguessable` claim, and no production gateway `Buffer.concat` or
per-event chunk retention. Its broad `no auth` alternative still reports only
two pre-existing, unrelated comments in `buyer-client/src/negotiate.ts` about
optional OAuth for local A2A development.

No Cloudflare tunnel, external network, live chain, wallet, or browser was
used.

## Self-review

The integrated diff was reviewed for:

- buffer backing-store size versus accounted capacity;
- constant segment and slot bounds;
- aggregate reservation races and underflow/double release;
- uninitialized trailing-page disclosure;
- rejected partial payload storage;
- GET/HEAD length and body behavior;
- error, abort, close, and allocation-failure overlap;
- exact Bearer comparison and authentication-before-lookup;
- URL credential leakage and raw locator preservation;
- Python response-bound and effective-port parity; and
- unrelated changes.

The implementation retains only initialized page prefixes, serves exactly the
logical `Content-Length`, uses one idempotent release path, and changes only
the four source/test files required by the findings plus this report.

Two independent read-only final reviews approved the integrated diff with no
findings. The gateway review confirmed dedicated page backing, full allocation
accounting, segment bounds, copy-free commit/segmented GET, status preservation,
and exact-once lifecycle cleanup. The client review confirmed the 2 MiB/+1
boundary, deadline-preserving bounded reads, pre-authentication `:0` rejection,
and mutation-sensitive authentication and URL-leakage coverage.
