import assert from "node:assert/strict";
import test from "node:test";
import { ERC8183Buyer, resolveRpcUrls } from "./erc8183.js";

test("requires an explicit archive RPC URL", () => {
  assert.throws(
    () => resolveRpcUrls({}),
    /BSC_LOG_RPC_URL is required/,
  );
});

test("rejects invalid RPC URLs without echoing credentials", () => {
  const secret = "must-not-appear";
  assert.throws(
    () => resolveRpcUrls({
      BSC_LOG_RPC_URL: `not-a-url-${secret}`,
    }),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.match(error.message, /BSC_LOG_RPC_URL must be a valid HTTP\(S\) URL/);
      assert.doesNotMatch(error.message, new RegExp(secret));
      return true;
    },
  );
});

test("resolves an explicit archive RPC and optional transaction RPC", () => {
  assert.deepEqual(
    resolveRpcUrls({
      BSC_RPC_URL: "https://rpc.example",
      BSC_LOG_RPC_URL: "https://logs.example/v1/private-key",
    }),
    {
      rpcUrl: "https://rpc.example/",
      logRpcUrl: "https://logs.example/v1/private-key",
    },
  );
});

function buyerWithDeliverableUrl(value: unknown): ERC8183Buyer {
  const optParams = `0x${Buffer.from(JSON.stringify({ deliverable_url: value })).toString("hex")}`;
  const buyer = Object.create(ERC8183Buyer.prototype) as ERC8183Buyer;
  Object.assign(buyer as object, {
    logProvider: {
      getBlockNumber: async () => 100,
    },
    findSubmitBlock: async () => 90,
    policyLog: {
      filters: {
        "JobInitialised(uint256,bytes32,uint64,bytes)": () => ({}),
      },
      queryFilter: async () => [{ args: { optParams } }],
    },
  });
  return buyer;
}

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

test("reads a string deliverable URL from policy optParams", async () => {
  const url = "https://relay.example/v1/payload/pay_0123456789abcdef0123456789abcdef";
  assert.equal(await buyerWithDeliverableUrl(url).getDeliverableUrl(42n), url);
});

for (const [name, value] of [
  ["array", ["https://relay.example/v1/payload/pay_0123456789abcdef0123456789abcdef"]],
  ["object", { url: "https://relay.example/v1/payload/pay_0123456789abcdef0123456789abcdef" }],
] as const) {
  test(`rejects an ${name}-valued deliverable URL in policy optParams`, async () => {
    assert.equal(await buyerWithDeliverableUrl(value).getDeliverableUrl(42n), null);
  });
}
