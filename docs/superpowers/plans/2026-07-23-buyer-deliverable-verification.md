# Buyer Deliverable Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the buyer cryptographically verify the submitted SDK manifest before settlement and make Cloudflare Tunnel startup select an executable `cloudflared` candidate.

**Architecture:** Add a focused manifest-verification module that reproduces the Python SDK's canonical JSON commitment with lossless JSON numbers, then expose the on-chain commitment through `ERC8183Buyer`. Extract the post-submission fetch/verify/render/settle sequence behind an injected orchestration function so every failure path is testable and fail-closed. Replace the gateway's unconditional binary choice with an injectable executable resolver.

**Tech Stack:** TypeScript ES2022, Node.js test runner, ethers v6 Keccak utilities, `lossless-json`, ERC-8183 Commerce ABI, npm.

## Global Constraints

- Keep the successful E2E path automatic: a verified report is displayed and the buyer still attempts `settle`.
- Fetch, schema, context, or integrity failure must prevent every call to `settle`.
- Reproduce `keccak256(UTF-8(JSON(manifest, recursively sorted object keys, compact separators)))`.
- Preserve exact JSON numbers, including job IDs greater than `2^53`; never convert a job ID through JavaScript `number`.
- Validate manifest version `1`, current job ID, chain ID, Commerce/Router/Policy addresses, and string response fields.
- Retain all manifest fields, including metadata and extensions, in the commitment.
- PDF/HTML rendering failure after successful verification must not block settlement.
- Keep negotiation, signed `notify_funded`, relay authentication, manifest generation, contracts, and the settlement transaction unchanged.
- Do not introduce another runtime language or external service into the buyer.
- Reject unauthenticated raw-text delivery backends.
- Resolve `cloudflared` through PATH, `$HOME/.local/bin`, `/usr/local/bin`, then `/opt/homebrew/bin`, accepting only regular executable files.

---

### Task 1: Verify SDK manifests against the on-chain commitment

**Files:**
- Modify: `buyer-client/package.json`
- Modify: `buyer-client/package-lock.json`
- Create: `buyer-client/src/deliverable.ts`
- Create: `buyer-client/src/deliverable.test.ts`
- Modify: `buyer-client/src/erc8183.ts:175-183`
- Modify: `buyer-client/src/erc8183.ts:213-263`

**Interfaces:**
- Produces: `verifyDeliverableManifest(rawText: string, expected: DeliverableExpectation): string`
- Produces: `DeliverableExpectation` with `jobId: bigint`, `chainId: bigint`, `contracts: { commerce: string; router: string; policy: string }`, and `commitment: string`
- Produces: `ERC8183Buyer.getDeliverableCommitment(jobId: bigint): Promise<string>`
- Consumes: `MAX_PAYLOAD_BYTES` from `gateway.ts`, `CONTRACTS` from `erc8183.ts`, `keccak256`, `toUtf8Bytes`, and `getAddress` from ethers.

- [ ] **Step 1: Install the lossless JSON parser**

Run:

```bash
cd buyer-client
npm install lossless-json
```

Expected: `package.json` and `package-lock.json` add one runtime dependency and npm exits successfully.

- [ ] **Step 2: Write RED tests for protocol-compatible verification**

Create `buyer-client/src/deliverable.test.ts` with helpers that use the intended API:

```ts
import assert from "node:assert/strict";
import test from "node:test";
import { keccak256, toUtf8Bytes } from "ethers";
import { MAX_PAYLOAD_BYTES } from "./gateway.js";
import { verifyDeliverableManifest } from "./deliverable.js";

const JOB_ID = 2n ** 60n + 7n;
const CONTRACTS = {
  commerce: "0xa206c0517b6371c6638cd9e4a42cc9f02a33b0de",
  router: "0xd7d36d66d2f1b608a0f943f722d27e3744f66f25",
  policy: "0x4f4678d4439fec812ac7674bb3efb4c8f5fb78a6",
};

function canonicalManifest(content = "# verified"): string {
  return `{"chain_id":97,"contracts":{"commerce":"${CONTRACTS.commerce}","policy":"${CONTRACTS.policy}","router":"${CONTRACTS.router}"},"job_id":${JOB_ID},"metadata":{"job_id":${JOB_ID},"nested":[{"b":2,"a":1}]},"response":{"content":${JSON.stringify(content)},"content_type":"text/plain"},"version":1}`;
}

function expectation(raw: string) {
  return {
    jobId: JOB_ID,
    chainId: 97n,
    contracts: CONTRACTS,
    commitment: keccak256(toUtf8Bytes(raw)),
  };
}

test("verifies an SDK manifest with a job id above 2^53", () => {
  const raw = canonicalManifest();
  assert.equal(verifyDeliverableManifest(raw, expectation(raw)), "# verified");
});

test("canonicalizes key order and whitespace before comparing the commitment", () => {
  const canonical = canonicalManifest();
  const reordered = `{
    "version": 1,
    "response": {"content_type": "text/plain", "content": "# verified"},
    "metadata": {"nested": [{"a": 1, "b": 2}], "job_id": ${JOB_ID}},
    "job_id": ${JOB_ID},
    "contracts": {
      "router": "${CONTRACTS.router}",
      "policy": "${CONTRACTS.policy}",
      "commerce": "${CONTRACTS.commerce}"
    },
    "chain_id": 97
  }`;
  assert.equal(
    verifyDeliverableManifest(reordered, expectation(canonical)),
    "# verified",
  );
});

test("rejects a changed response even when the manifest context is valid", () => {
  const canonical = canonicalManifest();
  const changed = canonical.replace("# verified", "# replaced");
  assert.throws(
    () => verifyDeliverableManifest(changed, expectation(canonical)),
    /commitment does not match/i,
  );
});

for (const [name, mutate, error] of [
  ["job", (raw: string) => raw.replace(JOB_ID.toString(), (JOB_ID + 1n).toString()), /job_id/i],
  ["chain", (raw: string) => raw.replace('"chain_id":97', '"chain_id":56'), /chain_id/i],
  ["commerce", (raw: string) => raw.replace(CONTRACTS.commerce, "0x0000000000000000000000000000000000000001"), /commerce/i],
  ["router", (raw: string) => raw.replace(CONTRACTS.router, "0x0000000000000000000000000000000000000002"), /router/i],
  ["policy", (raw: string) => raw.replace(CONTRACTS.policy, "0x0000000000000000000000000000000000000003"), /policy/i],
] as const) {
  test(`rejects a manifest for the wrong ${name}`, () => {
    const canonical = canonicalManifest();
    assert.throws(
      () => verifyDeliverableManifest(mutate(canonical), expectation(canonical)),
      error,
    );
  });
}

test("rejects malformed, unsupported, incomplete, and oversized manifests", () => {
  const canonical = canonicalManifest();
  const expected = expectation(canonical);
  assert.throws(() => verifyDeliverableManifest("{", expected), /JSON/i);
  assert.throws(
    () => verifyDeliverableManifest(canonical.replace('"version":1', '"version":2'), expected),
    /version/i,
  );
  assert.throws(
    () => verifyDeliverableManifest(canonical.replace('"content_type":"text/plain"', '"content_type":7'), expected),
    /content_type/i,
  );
  assert.throws(
    () => verifyDeliverableManifest(`{"padding":"${"x".repeat(MAX_PAYLOAD_BYTES)}"}`, expected),
    /size/i,
  );
});
```

- [ ] **Step 3: Run the verifier tests and confirm RED**

Run:

```bash
cd buyer-client
npm run build
```

Expected: TypeScript fails because `./deliverable.js` does not exist.

- [ ] **Step 4: Implement lossless Python-compatible canonicalization and validation**

Create `buyer-client/src/deliverable.ts`. Use `lossless-json`'s `parse` and
`LosslessNumber` so numeric tokens are not rounded. Implement:

```ts
import { getAddress, keccak256, toUtf8Bytes } from "ethers";
import { isLosslessNumber, parse } from "lossless-json";
import { MAX_PAYLOAD_BYTES } from "./gateway.js";

type JsonValue =
  | null
  | boolean
  | string
  | { value: string; isLosslessNumber: true }
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface DeliverableExpectation {
  jobId: bigint;
  chainId: bigint;
  contracts: {
    commerce: string;
    router: string;
    policy: string;
  };
  commitment: string;
}

function jsonString(value: string): string {
  return JSON.stringify(value).replace(
    /[^\u0020-\u007e]/gu,
    (character) => [...character]
      .map((unit) => {
        const point = unit.codePointAt(0)!;
        if (point <= 0xffff) return `\\u${point.toString(16).padStart(4, "0")}`;
        const shifted = point - 0x10000;
        const high = 0xd800 + (shifted >> 10);
        const low = 0xdc00 + (shifted & 0x3ff);
        return `\\u${high.toString(16)}\\u${low.toString(16)}`;
      })
      .join(""),
  );
}

function canonicalJson(value: JsonValue): string {
  if (value === null || typeof value === "boolean") return String(value);
  if (typeof value === "string") return jsonString(value);
  if (isLosslessNumber(value)) return value.value;
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  return `{${Object.keys(value).sort().map(
    (key) => `${jsonString(key)}:${canonicalJson(value[key]!)}`,
  ).join(",")}}`;
}

function object(value: JsonValue, field: string): Record<string, JsonValue> {
  if (value === null || Array.isArray(value) || typeof value !== "object" || isLosslessNumber(value)) {
    throw new Error(`DeliverableManifest.${field} must be an object`);
  }
  return value;
}

function integer(value: JsonValue, field: string): bigint {
  if (!isLosslessNumber(value) || !/^(0|[1-9][0-9]*)$/.test(value.value)) {
    throw new Error(`DeliverableManifest.${field} must be an unsigned integer`);
  }
  return BigInt(value.value);
}

function string(value: JsonValue, field: string): string {
  if (typeof value !== "string") {
    throw new Error(`DeliverableManifest.${field} must be a string`);
  }
  return value;
}

function address(value: JsonValue, field: string): string {
  try {
    return getAddress(string(value, field));
  } catch {
    throw new Error(`DeliverableManifest.${field} must be an EVM address`);
  }
}

export function verifyDeliverableManifest(
  rawText: string,
  expected: DeliverableExpectation,
): string {
  if (Buffer.byteLength(rawText, "utf8") > MAX_PAYLOAD_BYTES) {
    throw new Error("DeliverableManifest size exceeds the 2 MiB limit");
  }

  let parsed: JsonValue;
  try {
    parsed = parse(rawText) as JsonValue;
  } catch {
    throw new Error("DeliverableManifest is not valid JSON");
  }

  const manifest = object(parsed, "root");
  if (integer(manifest["version"]!, "version") !== 1n) {
    throw new Error("Unsupported DeliverableManifest version");
  }
  if (integer(manifest["job_id"]!, "job_id") !== expected.jobId) {
    throw new Error("DeliverableManifest job_id does not match the current job");
  }
  if (integer(manifest["chain_id"]!, "chain_id") !== expected.chainId) {
    throw new Error("DeliverableManifest chain_id does not match the current chain");
  }

  const contracts = object(manifest["contracts"]!, "contracts");
  for (const key of ["commerce", "router", "policy"] as const) {
    if (address(contracts[key]!, `contracts.${key}`) !== getAddress(expected.contracts[key])) {
      throw new Error(`DeliverableManifest contracts.${key} does not match configuration`);
    }
  }

  const response = object(manifest["response"]!, "response");
  const content = string(response["content"]!, "response.content");
  string(response["content_type"]!, "response.content_type");

  const actual = keccak256(toUtf8Bytes(canonicalJson(parsed)));
  if (actual.toLowerCase() !== expected.commitment.toLowerCase()) {
    throw new Error("DeliverableManifest commitment does not match the on-chain deliverable");
  }
  return content;
}
```

If the installed `lossless-json` type exposes `LosslessNumber` differently,
adapt only the local `JsonValue` guard while preserving exact `.value` tokens
and the public interface above.

- [ ] **Step 5: Run verifier tests GREEN**

Run:

```bash
cd buyer-client
npm run build
node --test dist/deliverable.test.js
```

Expected: build succeeds and all deliverable tests pass.

- [ ] **Step 6: Add and test the on-chain commitment reader**

Add a focused `buyer-client/src/erc8183.test.ts` test using an object with the
class prototype and an injected `commerce.getJob` function:

```ts
import assert from "node:assert/strict";
import test from "node:test";
import { ERC8183Buyer } from "./erc8183.js";

test("reads and validates the job deliverable commitment", async () => {
  const buyer = Object.create(ERC8183Buyer.prototype) as ERC8183Buyer;
  Object.assign(buyer as object, {
    commerce: {
      getJob: async (jobId: bigint) => {
        assert.equal(jobId, 2n ** 60n + 7n);
        return { deliverable: `0x${"ab".repeat(32)}` };
      },
    },
  });
  assert.equal(
    await buyer.getDeliverableCommitment(2n ** 60n + 7n),
    `0x${"ab".repeat(32)}`,
  );
});
```

Run:

```bash
cd buyer-client
npm run build
```

Expected: FAIL because `getDeliverableCommitment` does not exist.

Then add to `ERC8183Buyer`:

```ts
async getDeliverableCommitment(jobId: bigint): Promise<string> {
  const job = await this.commerce.getJob(jobId) as { deliverable?: unknown };
  if (typeof job.deliverable !== "string" || !/^0x[0-9a-fA-F]{64}$/.test(job.deliverable)) {
    throw new Error("On-chain job has no valid deliverable commitment");
  }
  return job.deliverable;
}
```

Run:

```bash
cd buyer-client
npm run build
node --test dist/erc8183.test.js
```

Expected: PASS.

- [ ] **Step 7: Commit the verifier**

```bash
git add buyer-client/package.json buyer-client/package-lock.json \
  buyer-client/src/deliverable.ts buyer-client/src/deliverable.test.ts \
  buyer-client/src/erc8183.ts buyer-client/src/erc8183.test.ts
git commit -m "feat: verify deliverable commitments"
```

---

### Task 2: Make verification a hard settlement prerequisite

**Files:**
- Create: `buyer-client/src/completion.ts`
- Create: `buyer-client/src/completion.test.ts`
- Modify: `buyer-client/src/index.ts:159-249`

**Interfaces:**
- Consumes: `verifyDeliverableManifest` and `DeliverableExpectation` from Task 1.
- Produces: `completeSubmittedJob(params: CompletionParams, dependencies: CompletionDependencies): Promise<CompletionResult>`
- `CompletionResult` contains `reportText: string`, `settleTx: string`, and `renderError?: string`.
- The dependency interface supplies URL lookup, commitment lookup, authenticated fetch, report rendering, and settlement so tests can prove settlement call counts.

- [ ] **Step 1: Write RED orchestration tests**

Create `buyer-client/src/completion.test.ts`:

```ts
import assert from "node:assert/strict";
import test from "node:test";
import { keccak256, toUtf8Bytes } from "ethers";
import { completeSubmittedJob, type CompletionDependencies } from "./completion.js";
import { MAX_PAYLOAD_BYTES } from "./gateway.js";

const jobId = 42n;
const contracts = {
  commerce: "0xa206c0517b6371c6638cd9e4a42cc9f02a33b0de",
  router: "0xd7d36d66d2f1b608a0f943f722d27e3744f66f25",
  policy: "0x4f4678d4439fec812ac7674bb3efb4c8f5fb78a6",
};
const manifest = `{"chain_id":97,"contracts":{"commerce":"${contracts.commerce}","policy":"${contracts.policy}","router":"${contracts.router}"},"job_id":42,"metadata":{},"response":{"content":"report","content_type":"text/plain"},"version":1}`;
const commitment = keccak256(toUtf8Bytes(manifest));

function dependencies(overrides: Partial<CompletionDependencies> = {}) {
  let settleCalls = 0;
  const deps: CompletionDependencies = {
    getDeliverableUrl: async () => "https://relay.example/v1/payload/pay_0123456789abcdef0123456789abcdef",
    getDeliverableCommitment: async () => commitment,
    fetchDeliverable: async () => new Response(manifest, { status: 200 }),
    renderReport: async () => undefined,
    settle: async () => {
      settleCalls += 1;
      return "0xsettled";
    },
    ...overrides,
  };
  return { deps, settleCalls: () => settleCalls };
}

test("settles once after fetch and manifest verification succeed", async () => {
  const fixture = dependencies();
  const result = await completeSubmittedJob(
    { jobId, fundTxBlock: 1, chainId: 97n, contracts },
    fixture.deps,
  );
  assert.equal(result.reportText, "report");
  assert.equal(result.settleTx, "0xsettled");
  assert.equal(fixture.settleCalls(), 1);
});

for (const [name, overrides] of [
  ["missing URL", { getDeliverableUrl: async () => null }],
  ["unsupported URL", { getDeliverableUrl: async () => "ipfs://payload" }],
  ["fetch rejection", { fetchDeliverable: async () => { throw new Error("offline"); } }],
  ["HTTP failure", { fetchDeliverable: async () => new Response("", { status: 503 }) }],
  ["oversized response", {
    fetchDeliverable: async () => new Response("x".repeat(MAX_PAYLOAD_BYTES + 1), { status: 200 }),
  }],
  ["invalid manifest", { fetchDeliverable: async () => new Response("{}", { status: 200 }) }],
  ["hash mismatch", { getDeliverableCommitment: async () => `0x${"00".repeat(32)}` }],
] as const) {
  test(`${name} blocks settlement`, async () => {
    const fixture = dependencies(overrides);
    await assert.rejects(
      completeSubmittedJob(
        { jobId, fundTxBlock: 1, chainId: 97n, contracts },
        fixture.deps,
      ),
      /settlement blocked/i,
    );
    assert.equal(fixture.settleCalls(), 0);
  });
}

test("rendering failure is reported but does not block a verified settlement", async () => {
  const fixture = dependencies({
    renderReport: async () => { throw new Error("Chrome unavailable"); },
  });
  const result = await completeSubmittedJob(
    { jobId, fundTxBlock: 1, chainId: 97n, contracts },
    fixture.deps,
  );
  assert.match(result.renderError ?? "", /Chrome unavailable/);
  assert.equal(fixture.settleCalls(), 1);
});
```

- [ ] **Step 2: Run orchestration tests and confirm RED**

Run:

```bash
cd buyer-client
npm run build
```

Expected: FAIL because `./completion.js` does not exist.

- [ ] **Step 3: Implement the minimal fail-closed orchestration**

Create `buyer-client/src/completion.ts`:

```ts
import {
  verifyDeliverableManifest,
  type DeliverableExpectation,
} from "./deliverable.js";
import { MAX_PAYLOAD_BYTES } from "./gateway.js";

export interface CompletionParams {
  jobId: bigint;
  fundTxBlock?: number;
  chainId: bigint;
  contracts: DeliverableExpectation["contracts"];
}

export interface CompletionDependencies {
  getDeliverableUrl(jobId: bigint, fundTxBlock?: number): Promise<string | null>;
  getDeliverableCommitment(jobId: bigint): Promise<string>;
  fetchDeliverable(url: string): Promise<Response>;
  renderReport(reportText: string): Promise<void>;
  settle(jobId: bigint): Promise<string>;
}

export interface CompletionResult {
  reportText: string;
  settleTx: string;
  renderError?: string;
}

function blocked(reason: string): Error {
  return new Error(`Settlement blocked: ${reason}`);
}

async function readBoundedBody(response: Response): Promise<string> {
  const declared = response.headers.get("content-length");
  if (declared !== null && (!/^[0-9]+$/.test(declared) || BigInt(declared) > BigInt(MAX_PAYLOAD_BYTES))) {
    throw blocked("deliverable exceeds the 2 MiB limit");
  }
  if (!response.body) return "";

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_PAYLOAD_BYTES) {
        await reader.cancel();
        throw blocked("deliverable exceeds the 2 MiB limit");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)), size).toString("utf8");
}

export async function completeSubmittedJob(
  params: CompletionParams,
  dependencies: CompletionDependencies,
): Promise<CompletionResult> {
  const url = await dependencies.getDeliverableUrl(params.jobId, params.fundTxBlock);
  if (!url) throw blocked("deliverable URL is missing");

  let parsedUrl: URL;
  try {
    parsedUrl = new URL(url);
  } catch {
    throw blocked("deliverable URL is malformed");
  }
  if (parsedUrl.protocol !== "http:" && parsedUrl.protocol !== "https:") {
    throw blocked("deliverable URL must use HTTP or HTTPS");
  }

  let response: Response;
  try {
    response = await dependencies.fetchDeliverable(url);
  } catch {
    throw blocked("deliverable download failed");
  }
  if (!response.ok) {
    throw blocked(`deliverable download returned HTTP ${response.status}`);
  }

  let rawText: string;
  let commitment: string;
  try {
    [rawText, commitment] = await Promise.all([
      readBoundedBody(response),
      dependencies.getDeliverableCommitment(params.jobId),
    ]);
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("Settlement blocked:")) throw error;
    throw blocked("deliverable body or on-chain commitment could not be read");
  }

  let reportText: string;
  try {
    reportText = verifyDeliverableManifest(rawText, {
      jobId: params.jobId,
      chainId: params.chainId,
      contracts: params.contracts,
      commitment,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "verification failed";
    throw blocked(message);
  }

  let renderError: string | undefined;
  try {
    await dependencies.renderReport(reportText);
  } catch (error) {
    renderError = error instanceof Error ? error.message : String(error);
  }

  const settleTx = await dependencies.settle(params.jobId);
  return { reportText, settleTx, ...(renderError ? { renderError } : {}) };
}
```

- [ ] **Step 4: Run orchestration tests GREEN**

Run:

```bash
cd buyer-client
npm run build
node --test dist/completion.test.js
```

Expected: all completion tests pass.

- [ ] **Step 5: Wire the verified completion into the CLI**

Replace `index.ts` Steps 6 and 7 with one call to `completeSubmittedJob`. Supply:

```ts
const completion = await completeSubmittedJob(
  {
    jobId: buy.jobId,
    fundTxBlock: buy.fundTxBlock,
    chainId: BigInt(CONTRACTS.CHAIN_ID),
    contracts: {
      commerce: CONTRACTS.COMMERCE,
      router: CONTRACTS.ROUTER,
      policy: CONTRACTS.POLICY,
    },
  },
  {
    getDeliverableUrl: (jobId, block) => buyer.getDeliverableUrl(jobId, block),
    getDeliverableCommitment: (jobId) => buyer.getDeliverableCommitment(jobId),
    fetchDeliverable: (url) => fetchDeliverable(url, relay),
    renderReport: async (reportText) => {
      console.log("\n┌─ VERIFIED REPORT " + "─".repeat(41) + "┐");
      for (const line of reportText.split("\n")) console.log(`│ ${line}`);
      console.log("└" + "─".repeat(58) + "┘");
      const result = await saveReport(reportText, buy.jobId.toString(), symbols);
      console.log(`  ✓ HTML report  ${result.htmlPath}`);
      if (result.pdfPath) console.log(`  ✓ PDF report   ${result.pdfPath}`);
    },
    settle: (jobId) => buyer.settle(jobId),
  },
);
```

Import `CONTRACTS` and `completeSubmittedJob`. Print `completion.renderError` as
the existing non-fatal report-save warning and print `completion.settleTx` in
the success banner. Keep the existing `DisputeWindowActive` catch around this
call so a verified job still receives the current 24-hour guidance. Do not
catch any `Settlement blocked:` error before the top-level failure handler.

- [ ] **Step 6: Run focused and full buyer tests**

Run:

```bash
cd buyer-client
npm test
```

Expected: TypeScript compiles and every buyer test passes with no warnings.

- [ ] **Step 7: Commit the settlement gate**

```bash
git add buyer-client/src/completion.ts buyer-client/src/completion.test.ts \
  buyer-client/src/index.ts
git commit -m "fix: require verified delivery before settlement"
```

---

### Task 3: Resolve the first executable cloudflared candidate

**Files:**
- Modify: `buyer-client/src/gateway.ts:20-25`
- Modify: `buyer-client/src/gateway.ts:421-436`
- Modify: `buyer-client/src/gateway.test.ts`

**Interfaces:**
- Produces: `findCloudflared(options?: CloudflaredDiscoveryOptions): string`
- `CloudflaredDiscoveryOptions` contains injectable `env?: NodeJS.ProcessEnv` and `isExecutable?: (path: string) => boolean`.
- Consumes: Node `accessSync`, `constants.X_OK`, `statSync`, `delimiter`, and `join`.

- [ ] **Step 1: Write RED resolver tests**

Append focused tests to `buyer-client/src/gateway.test.ts` and import
`findCloudflared`:

```ts
test("findCloudflared prefers an executable found through PATH", () => {
  const executable = new Set(["/custom/bin/cloudflared", "/home/me/.local/bin/cloudflared"]);
  assert.equal(findCloudflared({
    env: { PATH: "/missing:/custom/bin", HOME: "/home/me" },
    isExecutable: (path) => executable.has(path),
  }), "/custom/bin/cloudflared");
});

test("findCloudflared falls back through every documented absolute location", () => {
  for (const expected of [
    "/home/me/.local/bin/cloudflared",
    "/usr/local/bin/cloudflared",
    "/opt/homebrew/bin/cloudflared",
  ]) {
    assert.equal(findCloudflared({
      env: { PATH: "/missing", HOME: "/home/me" },
      isExecutable: (path) => path === expected,
    }), expected);
  }
});

test("findCloudflared rejects non-executable candidates with a clear error", () => {
  assert.throws(
    () => findCloudflared({
      env: { PATH: "/bin", HOME: "/home/me" },
      isExecutable: () => false,
    }),
    /cloudflared executable not found/i,
  );
});
```

- [ ] **Step 2: Run gateway tests and confirm RED**

Run:

```bash
cd buyer-client
npm run build
```

Expected: FAIL because `findCloudflared` is not exported and does not accept
injected options.

- [ ] **Step 3: Implement executable discovery**

In `gateway.ts`, add the Node filesystem/path imports and implement:

```ts
import { accessSync, constants, statSync } from "fs";
import { delimiter, join } from "path";

export interface CloudflaredDiscoveryOptions {
  env?: NodeJS.ProcessEnv;
  isExecutable?: (path: string) => boolean;
}

function defaultIsExecutable(path: string): boolean {
  try {
    if (!statSync(path).isFile()) return false;
    accessSync(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

export function findCloudflared(
  options: CloudflaredDiscoveryOptions = {},
): string {
  const env = options.env ?? process.env;
  const isExecutable = options.isExecutable ?? defaultIsExecutable;
  const pathCandidates = (env.PATH ?? "")
    .split(delimiter)
    .filter(Boolean)
    .map((directory) => join(directory, "cloudflared"));
  const candidates = [
    ...pathCandidates,
    ...(env.HOME ? [join(env.HOME, ".local", "bin", "cloudflared")] : []),
    "/usr/local/bin/cloudflared",
    "/opt/homebrew/bin/cloudflared",
  ];
  const match = candidates.find(isExecutable);
  if (!match) {
    throw new Error(
      "cloudflared executable not found in PATH, ~/.local/bin, /usr/local/bin, or /opt/homebrew/bin",
    );
  }
  return match;
}
```

Keep `startCloudflaredTunnel` calling `findCloudflared()` before `spawn`, so
absence fails before child-process creation.

- [ ] **Step 4: Run focused gateway tests GREEN**

Run:

```bash
cd buyer-client
npm run build
node --test dist/gateway.test.js
```

Expected: all gateway tests pass.

- [ ] **Step 5: Run complete repository regression checks**

Run:

```bash
cd buyer-client
npm test
cd ../stockanalyst/app/agent
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Expected: every buyer and Python test passes.

Then run:

```bash
cd ../../..
git diff --check
git status --short
```

Expected: no whitespace errors and only intended Task 3 files are uncommitted.

- [ ] **Step 6: Commit tunnel discovery**

```bash
git add buyer-client/src/gateway.ts buyer-client/src/gateway.test.ts
git commit -m "fix: discover installed cloudflared executable"
```

---

### Task 4: Final security and compatibility review

**Files:**
- Review: `buyer-client/src/deliverable.ts`
- Review: `buyer-client/src/completion.ts`
- Review: `buyer-client/src/index.ts`
- Review: `buyer-client/src/erc8183.ts`
- Review: `buyer-client/src/gateway.ts`
- Review: all new and modified buyer tests

**Interfaces:**
- Consumes: every interface produced by Tasks 1-3.
- Produces: no new public API; only corrections required by review.

- [ ] **Step 1: Run deletion-sensitive targeted tests**

Run:

```bash
cd buyer-client
node --test dist/deliverable.test.js dist/completion.test.js dist/gateway.test.js
```

Expected: every integrity, settlement-blocking, and executable-discovery test
passes.

- [ ] **Step 2: Audit settlement call sites and unsafe job conversions**

Run:

```bash
cd ..
rg -n "settle\\(" buyer-client/src
rg -n "Number\\(.*job|parseInt\\(.*job|parseFloat\\(.*job" buyer-client/src
```

Expected: the executable flow reaches `buyer.settle` only through
`completeSubmittedJob`; no job ID is converted through an unsafe number.
The wrapper method and test doubles may contain their own `settle` definitions.

- [ ] **Step 3: Audit failure handling and secret safety**

Read the modified flow and verify:

- every URL, fetch, HTTP, schema, context, and hash failure throws
  `Settlement blocked`;
- no caught verification error falls through to settlement;
- errors contain no report content, gateway token, or complete manifest;
- render errors are the only errors intentionally converted to a non-fatal
  result; and
- the authenticated relay fetch function remains unchanged.

If a violation exists, first add a failing test that exposes it, run that test
RED, make the smallest correction, and rerun it GREEN.

- [ ] **Step 4: Run final verification**

Run:

```bash
cd buyer-client
npm test
cd ../stockanalyst/app/agent
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
cd ../../..
git diff --check
git status --short
```

Expected: all tests pass, `git diff --check` is silent, and the worktree has no
uncommitted implementation changes.
