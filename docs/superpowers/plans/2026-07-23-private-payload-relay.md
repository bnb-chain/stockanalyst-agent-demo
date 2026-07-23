# Private Payload Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require off-chain authorization for every personalized report read, strengthen payload identifiers, and bound all relay memory retained by uploads.

**Architecture:** A per-relay request handler owns its payload Map and byte/slot accounting. The existing signed gateway token authenticates POST, GET, and HEAD without ever entering the on-chain URL. Buyer and Python clients attach that token only to the validated relay origin.

**Tech Stack:** TypeScript ES2022, Node.js 24 HTTP/crypto/test APIs, Python 3.10+, `urllib.request`, `unittest`.

## Global Constraints

- The on-chain `deliverable_url` is a locator, not an access capability.
- Relay credentials must never appear in URLs, query strings, fragments, logs, or on-chain metadata.
- POST, GET, and HEAD payload operations require the exact relay Bearer token.
- Authentication occurs before payload lookup.
- Payload IDs use `pay_${randomBytes(16).toString("hex")}`.
- `MAX_PAYLOAD_BYTES = 2 * 1024 * 1024`.
- `MAX_RELAY_BYTES = 16 * 1024 * 1024`.
- `MAX_RELAY_PAYLOADS = 32`.
- Stored plus in-flight bytes and stored plus active slots are bounded per relay.
- Rejected, aborted, and failed uploads release reservations exactly once.
- Existing payloads are never evicted.
- `413` means per-request overflow; `507` means aggregate bytes or slots exhausted.
- Tests may use ephemeral loopback servers but no Cloudflare, external network, live chain, or wallet.

---

### Task 1: Authenticate payload access and isolate relay stores

**Files:**
- Modify: `buyer-client/src/gateway.ts`
- Create: `buyer-client/src/gateway.test.ts`

**Interfaces:**
- Produces: `createGatewayHandler(token: string, options?: GatewayHandlerOptions): RequestListener`
- Produces: `GatewayHandlerOptions = { idFactory?: () => string; limits?: Partial<RelayLimits> }`
- Preserves: `startGatewayRelay(port?: number): Promise<GatewayRelay>`
- Guarantees: each handler has an independent payload store

- [ ] **Step 1: Write failing authentication, ID, HEAD, and isolation tests**

Use a real ephemeral loopback `http.Server` around the exported handler:

```ts
async function withRelay(
  token: string,
  run: (baseUrl: string) => Promise<void>,
): Promise<void> {
  const server = createServer(createGatewayHandler(token));
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  try {
    await run(`http://127.0.0.1:${address.port}`);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}
```

Add tests with these exact assertions:

```ts
test("requires bearer auth for upload, download, and existence checks", async () => {
  await withRelay("gw-secret", async (baseUrl) => {
    for (const [method, path] of [
      ["POST", "/v1/payload/upload"],
      ["GET", "/v1/payload/pay_missing"],
      ["HEAD", "/v1/payload/pay_missing"],
    ] as const) {
      const response = await fetch(`${baseUrl}${path}`, { method });
      assert.equal(response.status, 401);
    }
  });
});

test("stores under a 128-bit ID and serves authenticated GET and HEAD", async () => {
  await withRelay("gw-secret", async (baseUrl) => {
    const upload = await fetch(`${baseUrl}/v1/payload/upload`, {
      method: "POST",
      headers: { Authorization: "Bearer gw-secret" },
      body: "private report",
    });
    assert.equal(upload.status, 200);
    const result = await upload.json() as { payload_id: string; size: number };
    assert.match(result.payload_id, /^pay_[0-9a-f]{32}$/);
    assert.equal(result.size, 14);

    const path = `/v1/payload/${result.payload_id}`;
    const get = await fetch(`${baseUrl}${path}`, {
      headers: { Authorization: "Bearer gw-secret" },
    });
    assert.equal(await get.text(), "private report");

    const head = await fetch(`${baseUrl}${path}`, {
      method: "HEAD",
      headers: { Authorization: "Bearer gw-secret" },
    });
    assert.equal(head.status, 200);
    assert.equal(head.headers.get("content-length"), "14");
    assert.equal(await head.text(), "");
  });
});
```

Also start two handlers, upload to the first, and assert authenticated GET on
the second returns `404`. Assert `/v1/health` equals `{"status":"ok"}` and has
no payload-count field. With an injected ID sequence that repeats one existing
ID and then returns a fresh canonical ID, assert the first payload is not
overwritten and the second upload receives the fresh ID.

- [ ] **Step 2: Run Task 1 tests and verify RED**

Run:

```bash
cd buyer-client
/Users/zhaoyu/.nvm/versions/node/v24.10.0/bin/node \
  ../../apex-contracts/node_modules/tsx/dist/cli.mjs \
  --test src/gateway.test.ts
```

Expected: FAIL because `createGatewayHandler` is not exported and current GET
does not require authentication.

- [ ] **Step 3: Implement the private per-relay handler**

Add these production constants and types:

```ts
import {
  randomBytes,
  timingSafeEqual,
} from "crypto";
import type { RequestListener } from "http";

export const MAX_PAYLOAD_BYTES = 2 * 1024 * 1024;
export const MAX_RELAY_BYTES = 16 * 1024 * 1024;
export const MAX_RELAY_PAYLOADS = 32;

export interface RelayLimits {
  maxPayloadBytes: number;
  maxRelayBytes: number;
  maxPayloads: number;
}

export interface GatewayHandlerOptions {
  idFactory?: () => string;
  limits?: Partial<RelayLimits>;
}
```

Authenticate exact Bearer bytes:

```ts
function hasBearer(req: IncomingMessage, token: string): boolean {
  const header = req.headers.authorization;
  if (typeof header !== "string" || !header.startsWith("Bearer ")) return false;
  const supplied = Buffer.from(header.slice(7), "utf8");
  const expected = Buffer.from(token, "utf8");
  return supplied.length === expected.length && timingSafeEqual(supplied, expected);
}
```

Create one Map inside `createGatewayHandler`, not at module scope. Generate IDs
with an injected factory in tests or `pay_${randomBytes(16).toString("hex")}` in
production, retrying when the candidate already exists. Authenticate
POST/GET/HEAD before reading the body or looking up an ID. Match only
`^/v1/payload/(pay_[0-9a-f]{32})$`. GET returns bytes; HEAD sends the same
content headers without bytes. Unknown authenticated IDs return a generic
`404`.

Use `createGatewayHandler(token)` in `createRelayServer`; do not create a new
handler per request.

- [ ] **Step 4: Run Task 1 tests and verify GREEN**

Run the same focused command. Expected: authentication, ID, GET/HEAD, health,
and isolated-store tests all pass.

- [ ] **Step 5: Commit**

```bash
git add buyer-client/src/gateway.ts buyer-client/src/gateway.test.ts
git commit -m "fix: authenticate payload relay reads"
```

---

### Task 2: Bound per-request and aggregate relay memory

**Files:**
- Modify: `buyer-client/src/gateway.ts`
- Modify: `buyer-client/src/gateway.test.ts`

**Interfaces:**
- Consumes: `createGatewayHandler` and `GatewayHandlerOptions` from Task 1
- Uses test limits: `{ maxPayloadBytes: 8, maxRelayBytes: 12, maxPayloads: 2 }`
- Guarantees: bytes/slot reservations are released exactly once

- [ ] **Step 1: Write failing quota and cleanup tests**

Add an HTTP request helper that can omit Content-Length and write controlled
chunks:

```ts
async function postChunks(
  baseUrl: string,
  token: string,
  chunks: Buffer[],
  headers: Record<string, string> = {},
): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const target = new URL("/v1/payload/upload", baseUrl);
    const request = httpRequest(target, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, ...headers },
    }, (response) => {
      const parts: Buffer[] = [];
      response.on("data", (part: Buffer) => parts.push(part));
      response.on("end", () => resolve({
        status: response.statusCode ?? 0,
        body: Buffer.concat(parts).toString("utf8"),
      }));
    });
    request.on("error", reject);
    for (const chunk of chunks) request.write(chunk);
    request.end();
  });
}
```

Use an ephemeral handler with the small limits above and assert:

```ts
assert.equal(
  (await postChunks(baseUrl, token, [Buffer.alloc(9)])).status,
  413,
);
assert.equal(
  (await postChunks(baseUrl, token, [], { "Content-Length": "9" })).status,
  413,
);
assert.equal(
  (await postChunks(baseUrl, token, [], { "Content-Length": "-1" })).status,
  400,
);
```

Store 8 bytes, then assert a 5-byte upload returns `507` while the original
payload remains readable. With `maxPayloads: 1`, keep one authenticated request
open after writing a byte and assert a concurrent upload returns `507`; destroy
the first request, wait for its close, then assert a fresh upload succeeds.
Repeat the cleanup assertion for a request emitting an error after partial
data. Verify rejected uploads never return a payload ID.

- [ ] **Step 2: Run Task 2 tests and verify RED**

Run the focused gateway test. Expected: oversize and quota cases currently
return `200` or retain state instead of the required `413`/`507`.

- [ ] **Step 3: Implement reservation accounting**

Resolve limits once per handler:

```ts
const limits: RelayLimits = {
  maxPayloadBytes: options.limits?.maxPayloadBytes ?? MAX_PAYLOAD_BYTES,
  maxRelayBytes: options.limits?.maxRelayBytes ?? MAX_RELAY_BYTES,
  maxPayloads: options.limits?.maxPayloads ?? MAX_RELAY_PAYLOADS,
};
let storedBytes = 0;
let inFlightBytes = 0;
let activeUploads = 0;
```

For each authenticated POST:

1. Validate a single decimal Content-Length, rejecting malformed/negative with
   `400`, values over `maxPayloadBytes` with `413`, and reservations over
   aggregate capacity with `507`.
2. Reserve one slot and declared bytes before attaching data listeners.
3. For chunked/under-declared bodies, reserve each additional byte before
   retaining its chunk.
4. If request bytes cross `maxPayloadBytes`, return `413`; if aggregate bytes
   cross `maxRelayBytes`, return `507`.
5. Use one idempotent `release()` closure for `end`, `aborted`, `error`, and
   premature `close`.
6. On success, concatenate only the accepted chunks, transfer actual bytes from
   in-flight to stored accounting, convert the active slot to a stored payload,
   then send the response.
7. On failure, clear chunks, release every reserved byte and active slot, send
   at most one response, and drain or destroy the remaining request body.

Do not evict existing Map entries.

- [ ] **Step 4: Run Task 2 tests and verify GREEN**

Expected: all focused gateway tests pass, including abort/error recovery and
the original payload remaining readable after `507`.

- [ ] **Step 5: Commit**

```bash
git add buyer-client/src/gateway.ts buyer-client/src/gateway.test.ts
git commit -m "fix: bound payload relay memory"
```

---

### Task 3: Send read credentials from buyer and Python clients

**Files:**
- Modify: `buyer-client/src/gateway.ts`
- Modify: `buyer-client/src/gateway.test.ts`
- Modify: `buyer-client/src/index.ts`
- Modify: `stockanalyst/app/agent/uomp_storage.py`
- Modify: `stockanalyst/app/agent/tests/test_uomp_storage.py`
- Modify: `buyer-client/README.md`

**Interfaces:**
- Produces: `fetchDeliverable(url: string, relay: GatewayRelay | undefined, fetchImpl?: typeof fetch): Promise<Response>`
- Consumes: existing `GatewayRelay.localUrl`, `.publicUrl`, and `.token`
- Preserves: returned/on-chain payload URLs without credentials

- [ ] **Step 1: Write failing buyer credential-scoping tests**

Add a fake fetch recorder:

```ts
test("sends the relay token only to the relay origin", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fakeFetch = async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return new Response("ok");
  };
  const relay: GatewayRelay = {
    localUrl: "http://127.0.0.1:9444",
    publicUrl: "https://buyer.trycloudflare.com",
    token: "gw-secret",
    close() {},
  };

  await fetchDeliverable(
    "https://buyer.trycloudflare.com/v1/payload/pay_0123456789abcdef0123456789abcdef",
    relay,
    fakeFetch,
  );
  assert.equal(
    new Headers(calls[0]?.init?.headers).get("Authorization"),
    "Bearer gw-secret",
  );
  assert.equal(calls[0]?.init?.redirect, "error");

  await fetchDeliverable("https://evil.example/report", relay, fakeFetch);
  assert.equal(
    new Headers(calls[1]?.init?.headers).has("Authorization"),
    false,
  );
});
```

Also assert token attachment rejects userinfo, query strings, fragments,
non-payload paths, lookalike suffixes, and non-default ports. Assert
`relay === undefined` performs the existing credential-free fetch.

- [ ] **Step 2: Write failing Python header tests**

Extend existing download and exists tests:

```python
self.assertEqual(request.get_header("Authorization"), "Bearer token")
```

The upload test must continue to assert the same header, and returned payload
URLs must contain neither the token nor a query string. Replace existing valid
`payload_123` fixtures with
`pay_0123456789abcdef0123456789abcdef`, and add rejection cases for uppercase,
short, non-hex, and legacy payload IDs.

- [ ] **Step 3: Run both focused suites and verify RED**

Run:

```bash
cd buyer-client
/Users/zhaoyu/.nvm/versions/node/v24.10.0/bin/node \
  ../../apex-contracts/node_modules/tsx/dist/cli.mjs \
  --test src/gateway.test.ts
```

and:

```bash
stockanalyst/app/agent/.venv/bin/python -m unittest \
  stockanalyst.app.agent.tests.test_uomp_storage -v
```

Expected: buyer helper/export is missing and Python GET/HEAD requests lack the
Authorization header.

- [ ] **Step 4: Implement origin-scoped buyer fetching**

Add:

```ts
export async function fetchDeliverable(
  url: string,
  relay: GatewayRelay | undefined,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  if (!relay) return fetchImpl(url);
  const target = new URL(url);
  const relayOrigins = new Set([
    new URL(relay.publicUrl).origin,
    new URL(relay.localUrl).origin,
  ]);
  const isCanonicalPayload =
    !target.username &&
    !target.password &&
    !target.search &&
    !target.hash &&
    /^\/v1\/payload\/pay_[0-9a-f]{32}$/.test(target.pathname);
  if (!relayOrigins.has(target.origin) || !isCanonicalPayload) {
    return fetchImpl(url);
  }
  return fetchImpl(url, {
    headers: { Authorization: `Bearer ${relay.token}` },
    redirect: "error",
  });
}
```

Replace `fetch(deliverableUrl)` in `index.ts` with
`fetchDeliverable(deliverableUrl, relay)`. Never send the token to a different
origin.

- [ ] **Step 5: Add Python download and HEAD authorization**

In `UOMPGatewayStorageProvider.download` and `.exists`, construct requests with:

```python
headers={"Authorization": f"Bearer {self._token}"}
```

Keep `_validate_payload_url` before request construction so credentials are
only sent to the prevalidated same origin.

Tighten both Python protocol patterns:

```python
_PAYLOAD_ID_PATTERN = re.compile(r"pay_[0-9a-f]{32}\Z")
_PAYLOAD_PATH_PATTERN = re.compile(r"/v1/payload/(pay_[0-9a-f]{32})\Z")
```

- [ ] **Step 6: Update protocol documentation**

Document that the on-chain URL is a public locator but GET/HEAD require the
off-chain signed gateway token. Remove statements claiming payload IDs alone
authorize reads. State the 2 MiB/16 MiB/32 limits and `413`/`507` behavior.

- [ ] **Step 7: Run complete verification**

Run focused Node 24 gateway tests, all buyer focused tests, strict
sibling-mapped TypeScript typecheck, Python UOMP tests, full lightweight Python
discovery, `git diff --check`, and:

```bash
rg -n "randomBytes\\(4\\)|no auth|payload_id is unguessable" \
  buyer-client/src buyer-client/README.md
```

Expected: every test passes and the prohibited legacy assumptions have no
matches. If official buyer `npm test`/`npm run build` remain blocked by the
empty local dependency installation, record their exact errors separately.

- [ ] **Step 8: Commit**

```bash
git add buyer-client/src/gateway.ts buyer-client/src/gateway.test.ts \
  buyer-client/src/index.ts buyer-client/README.md \
  stockanalyst/app/agent/uomp_storage.py \
  stockanalyst/app/agent/tests/test_uomp_storage.py
git commit -m "fix: authenticate deliverable downloads"
```

- [ ] **Step 9: Final review**

Review the complete range from `f181250` to `HEAD` for credential leakage,
origin confusion, unauthenticated reads, reservation underflow/double release,
concurrent quota bypass, partial payload storage, and unrelated changes. Re-run
focused tests after any review fix. The final working tree must be clean.
