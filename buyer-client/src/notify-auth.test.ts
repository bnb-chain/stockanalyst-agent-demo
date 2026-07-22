import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { Wallet } from "ethers";
import {
  buildNotifyContext,
  createNotifyAuthorization,
  recoverNotifySigner,
} from "./notify-auth.js";
import { notifyFunded } from "./negotiate.js";

interface NotifyAuthVector {
  test_key: string;
  expected_address: string;
  job_id: string;
  context: string;
  expires_at: number;
  nonce: string;
  signature: string;
}

const vector = JSON.parse(
  readFileSync(
    new URL("../../stockanalyst/app/agent/tests/fixtures/notify_auth_vector.json", import.meta.url),
    "utf8",
  ),
) as NotifyAuthVector;

test("signs exact context for a large job id", async () => {
  const wallet = new Wallet(vector.test_key);
  const context = buildNotifyContext({
    gatewayUrl: "https://buyer.trycloudflare.com",
    gatewayToken: "relay-token",
    portfolio: [{ symbol: "AAPL", shares: 10, avgCost: 190.25, currency: "USD" }],
    riskProfile: {
      tolerance: "moderate",
      horizonMonths: 12,
      preferredIndicators: ["RSI-14", "MACD"],
    },
  });

  assert.equal(context, vector.context);

  const authorization = await createNotifyAuthorization(wallet, BigInt(vector.job_id), context, {
    nowSeconds: vector.expires_at - 300,
    nonce: vector.nonce,
  });

  assert.equal(authorization.signature, vector.signature);
  assert.equal(recoverNotifySigner(BigInt(vector.job_id), authorization), vector.expected_address);
});

test("sends a signed notification envelope without duplicate context fields", async () => {
  const wallet = new Wallet(vector.test_key);
  const jobId = 2n ** 60n + 7n;
  const options = {
    gatewayUrl: "https://buyer.trycloudflare.com",
    gatewayToken: "relay-token",
    portfolio: [{ symbol: "AAPL", shares: 10, avgCost: 190.25, currency: "USD" }],
    riskProfile: {
      tolerance: "moderate",
      horizonMonths: 12,
      preferredIndicators: ["RSI-14", "MACD"],
    },
  };
  const expectedContext = buildNotifyContext(options);
  let requestBody = "";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (_input, init) => {
    requestBody = String(init?.body);
    return new Response(JSON.stringify({ result: { parts: [{ data: { status: "accepted" } }] } }));
  };

  try {
    const status = await notifyFunded(
      "https://seller.example/a2a",
      wallet,
      jobId,
      options,
    );
    assert.equal(status, "accepted");
  } finally {
    globalThis.fetch = originalFetch;
  }

  const payload = JSON.parse(requestBody) as {
    params: { message: { parts: Array<{ data: Record<string, unknown> }> } };
  };
  const data = payload.params.message.parts[0].data;
  const authorization = data["authorization"] as Record<string, unknown>;
  assert.equal(data["job_id"], jobId.toString());
  assert.deepEqual(Object.keys(data).sort(), ["authorization", "job_id", "skill"]);
  assert.equal(authorization["context"], expectedContext);
  assert.equal("portfolio" in data, false);
  assert.equal("delivery_gateway_token" in data, false);
});
