import assert from "node:assert/strict";
import test from "node:test";

import { readFreeQuoteResponse } from "./x402-free-client.js";


test("reads a JSON free quote object", async () => {
  const quote = {
    symbol: "AAPL",
    name: "Apple Inc.",
    price: 333.43,
    price_now: 333.43,
    analyst_target: 340,
    twelve_month_target: 340,
    analyst_upside_pct: 2.0,
    room_to_target: 2.0,
  };
  const response = new Response(JSON.stringify(quote), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

  assert.deepEqual(await readFreeQuoteResponse(response), quote);
});


test("rejects malformed free quote responses", async () => {
  for (const body of (
    [
      {},
      [],
      { content: "# AAPL report", format: "markdown" },
      { symbol: "" },
    ]
  )) {
    const response = new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
    await assert.rejects(readFreeQuoteResponse(response), /Invalid free quote response/);
  }
});
