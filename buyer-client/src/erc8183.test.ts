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
