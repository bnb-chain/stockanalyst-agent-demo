import assert from "node:assert/strict";
import test from "node:test";

import { readFreeQuoteResponse } from "./x402-free-client.js";


test("reads a buffered JSON free quote", async () => {
  const response = new Response(JSON.stringify({
    content: "# AAPL report",
    format: "markdown",
  }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

  assert.equal(await readFreeQuoteResponse(response), "# AAPL report");
});


test("rejects malformed free quote responses", async () => {
  for (const body of (
    [
      {},
      { content: "", format: "markdown" },
      { content: "report", format: "html" },
    ]
  )) {
    const response = new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
    await assert.rejects(readFreeQuoteResponse(response), /Invalid free quote response/);
  }
});
