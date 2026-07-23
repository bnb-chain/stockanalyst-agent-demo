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
  return `{"chain_id":97,"contracts":{"commerce":"${CONTRACTS.commerce}","policy":"${CONTRACTS.policy}","router":"${CONTRACTS.router}"},"job_id":${JOB_ID},"metadata":{"job_id":${JOB_ID},"nested":[{"a":1,"b":2}]},"response":{"content":${JSON.stringify(content)},"content_type":"text/plain"},"version":1}`;
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

test("matches SDK key ordering for BMP and astral metadata keys", () => {
  const sdkCanonical = `{"chain_id":97,"contracts":{"commerce":"${CONTRACTS.commerce}","policy":"${CONTRACTS.policy}","router":"${CONTRACTS.router}"},"job_id":${JOB_ID},"metadata":{"\\ue000":"bmp","\\ud800\\udc00":"astral"},"response":{"content":"# verified","content_type":"text/plain"},"version":1}`;
  assert.equal(
    verifyDeliverableManifest(sdkCanonical, expectation(sdkCanonical)),
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
