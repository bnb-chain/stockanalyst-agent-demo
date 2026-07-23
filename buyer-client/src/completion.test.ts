import assert from "node:assert/strict";
import test from "node:test";
import { keccak256, toUtf8Bytes } from "ethers";
import {
  completeSubmittedJob,
  SettlementAttemptError,
  SettlementBlockedError,
  type CompletionDependencies,
} from "./completion.js";
import { MAX_PAYLOAD_BYTES } from "./gateway.js";

const jobId = 42n;
const MAX_JSON_NESTING_DEPTH = 128;
const NESTING_DEPTH_ERROR =
  "DeliverableManifest exceeds the maximum JSON nesting depth";
const overDepthSecret = "over-depth-content-must-not-leak";
const contracts = {
  commerce: "0xa206c0517b6371c6638cd9e4a42cc9f02a33b0de",
  router: "0xd7d36d66d2f1b608a0f943f722d27e3744f66f25",
  policy: "0x4f4678d4439fec812ac7674bb3efb4c8f5fb78a6",
};
const manifest = `{"chain_id":97,"contracts":{"commerce":"${contracts.commerce}","policy":"${contracts.policy}","router":"${contracts.router}"},"job_id":42,"metadata":{},"response":{"content":"report","content_type":"text/plain"},"version":1}`;
const commitment = keccak256(toUtf8Bytes(manifest));
const rootPrototypeManifest = `{"\\u005f\\u005fproto\\u005f\\u005f":{"chain_id":97,"contracts":{"commerce":"${contracts.commerce}","policy":"${contracts.policy}","router":"${contracts.router}"},"job_id":42,"metadata":{},"response":{"content":"uncommitted root report","content_type":"text/plain"},"version":1}}`;
const nestedPrototypeManifest = `{"chain_id":97,"contracts":{"commerce":"${contracts.commerce}","policy":"${contracts.policy}","router":"${contracts.router}"},"job_id":42,"metadata":{},"response":{"__pro\\u0074o__":{"content":"uncommitted nested report","content_type":"text/plain"}},"version":1}`;
const nestedPrototypeCommitted = `{"chain_id":97,"contracts":{"commerce":"${contracts.commerce}","policy":"${contracts.policy}","router":"${contracts.router}"},"job_id":42,"metadata":{},"response":{},"version":1}`;
const duplicateVersionManifest = manifest.replace(
  '"version":1',
  '"version":1,"version":1',
);
const nestedDuplicateManifest = manifest.replace(
  '"metadata":{}',
  '"metadata":{"nested":{"same":"value","same":"value"}}',
);
const nestedDuplicateCommitted = manifest.replace(
  '"metadata":{}',
  '"metadata":{"nested":{"same":"value"}}',
);
const escapedDuplicateManifest = manifest.replace(
  '"metadata":{}',
  '"metadata":{"nested":{"x":1,"\\u0078":1}}',
);
const escapedDuplicateCommitted = manifest.replace(
  '"metadata":{}',
  '"metadata":{"nested":{"x":1}}',
);
const markerDuplicateManifest = manifest.replace(
  '"metadata":{}',
  '"metadata":{"value":{"isLosslessNumber":true,"value":"7"},"value":7}',
);
const markerDuplicateCommitted = manifest.replace(
  '"metadata":{}',
  '"metadata":{"value":7}',
);
const overDepthManifest = manifest.replace(
  '"metadata":{}',
  `"metadata":${nestedObjects(MAX_JSON_NESTING_DEPTH, JSON.stringify(overDepthSecret))}`,
);

function nestedObjects(depth: number, leaf: string): string {
  return `${'{"level":'.repeat(depth)}${leaf}${"}".repeat(depth)}`;
}

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

for (const [name, maliciousManifest, committedManifest] of [
  ["root", rootPrototypeManifest, "{}"],
  ["nested", nestedPrototypeManifest, nestedPrototypeCommitted],
] as const) {
  test(`${name} __proto__ fields cannot supply uncommitted report data`, async () => {
    const fixture = dependencies({
      fetchDeliverable: async () => new Response(maliciousManifest, { status: 200 }),
      getDeliverableCommitment: async () => keccak256(toUtf8Bytes(committedManifest)),
    });
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

for (const [name, maliciousManifest, committedManifest] of [
  ["root required-key", duplicateVersionManifest, manifest],
  ["nested identical-value", nestedDuplicateManifest, nestedDuplicateCommitted],
  ["escaped-name", escapedDuplicateManifest, escapedDuplicateCommitted],
  ["marker-object-versus-number", markerDuplicateManifest, markerDuplicateCommitted],
] as const) {
  test(`${name} duplicate keys cannot reach settlement`, async () => {
    const fixture = dependencies({
      fetchDeliverable: async () => new Response(maliciousManifest, { status: 200 }),
      getDeliverableCommitment: async () => keccak256(toUtf8Bytes(committedManifest)),
    });
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

test("over-depth deliverables are typed blockers and cannot reach settlement", async () => {
  const fixture = dependencies({
    fetchDeliverable: async () => new Response(overDepthManifest, { status: 200 }),
    getDeliverableCommitment: async () => keccak256(toUtf8Bytes(overDepthManifest)),
  });
  await assert.rejects(
    completeSubmittedJob(
      { jobId, fundTxBlock: 1, chainId: 97n, contracts },
      fixture.deps,
    ),
    (error: unknown) => {
      if (!(error instanceof SettlementBlockedError)) return false;
      assert.equal(
        error.message,
        `Settlement blocked: ${NESTING_DEPTH_ERROR}`,
      );
      assert.doesNotMatch(error.message, new RegExp(overDepthSecret));
      assert.equal(error instanceof SettlementAttemptError, false);
      return true;
    },
  );
  assert.equal(fixture.settleCalls(), 0);
});

test("URL lookup failure blocks settlement without leaking dependency errors", async () => {
  const secret = "rpc-token-must-not-leak";
  const fixture = dependencies({
    getDeliverableUrl: async () => {
      throw new Error(`RPC request failed at https://gateway.example/${secret}`);
    },
  });
  await assert.rejects(
    completeSubmittedJob(
      { jobId, fundTxBlock: 1, chainId: 97n, contracts },
      fixture.deps,
    ),
    (error: unknown) => {
      if (!(error instanceof Error)) return false;
      assert.match(error.message, /settlement blocked/i);
      assert.doesNotMatch(error.message, new RegExp(secret));
      return true;
    },
  );
  assert.equal(fixture.settleCalls(), 0);
});

test("a dependency cannot spoof the internal settlement-blocked prefix", async () => {
  const secret = "commitment-secret-must-not-leak";
  const fixture = dependencies({
    getDeliverableCommitment: async () => {
      throw new Error(`Settlement blocked: ${secret}`);
    },
  });
  await assert.rejects(
    completeSubmittedJob(
      { jobId, fundTxBlock: 1, chainId: 97n, contracts },
      fixture.deps,
    ),
    (error: unknown) => {
      if (!(error instanceof SettlementBlockedError)) return false;
      assert.equal(
        error.message,
        "Settlement blocked: deliverable body or on-chain commitment could not be read",
      );
      assert.doesNotMatch(error.message, new RegExp(secret));
      assert.equal(error instanceof SettlementAttemptError, false);
      return true;
    },
  );
  assert.equal(fixture.settleCalls(), 0);
});

for (const [name, overrides] of [
  ["missing URL", { getDeliverableUrl: async () => null }],
  ["array URL", {
    getDeliverableUrl: async () => [
      "https://relay.example/v1/payload/pay_0123456789abcdef0123456789abcdef",
    ],
  }],
  ["object URL", {
    getDeliverableUrl: async () => ({
      toString: () => "https://relay.example/v1/payload/pay_0123456789abcdef0123456789abcdef",
    }),
  }],
  ["unsupported URL", { getDeliverableUrl: async () => "ipfs://payload" }],
  ["fetch rejection", { fetchDeliverable: async () => { throw new Error("offline"); } }],
  ["HTTP failure", { fetchDeliverable: async () => new Response("", { status: 503 }) }],
  ["oversized response", {
    fetchDeliverable: async () => new Response("x".repeat(MAX_PAYLOAD_BYTES + 1), { status: 200 }),
  }],
  ["plain-text response", {
    fetchDeliverable: async () => new Response("legacy raw report", { status: 200 }),
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

test("a pre-verification DisputeWindow error stays blocked and never settles", async () => {
  const fixture = dependencies({
    fetchDeliverable: async () => { throw new Error("DisputeWindowActive"); },
  });
  await assert.rejects(
    completeSubmittedJob(
      { jobId, fundTxBlock: 1, chainId: 97n, contracts },
      fixture.deps,
    ),
    (error: unknown) => {
      if (!(error instanceof Error)) return false;
      assert.match(error.message, /settlement blocked/i);
      assert.equal(error instanceof SettlementAttemptError, false);
      return true;
    },
  );
  assert.equal(fixture.settleCalls(), 0);
});

test("a settle-stage DisputeWindow error is distinguishable for CLI guidance", async () => {
  let settleCalls = 0;
  const fixture = dependencies({
    settle: async () => {
      settleCalls += 1;
      throw new Error("DisputeWindowActive");
    },
  });
  await assert.rejects(
    completeSubmittedJob(
      { jobId, fundTxBlock: 1, chainId: 97n, contracts },
      fixture.deps,
    ),
    (error: unknown) => {
      if (!(error instanceof Error)) return false;
      if (!(error instanceof SettlementAttemptError)) return false;
      const cause = (error as Error & { cause?: unknown }).cause;
      if (!(cause instanceof Error)) return false;
      assert.match(cause.message, /DisputeWindowActive/);
      return true;
    },
  );
  assert.equal(settleCalls, 1);
});
