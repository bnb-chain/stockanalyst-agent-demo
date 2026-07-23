import assert from "node:assert/strict";
import test from "node:test";
import { ERC8183Buyer } from "./erc8183.js";

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
