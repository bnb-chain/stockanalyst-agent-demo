import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { Wallet } from "ethers";
import {
  buildNotifyContext,
  createNotifyAuthorization,
  recoverNotifySigner,
  type NotifyOptions,
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

const missingRiskHorizon: NotifyOptions = {
  // @ts-expect-error risk profiles require horizonMonths.
  riskProfile: { tolerance: "moderate", preferredIndicators: [] },
};
void missingRiskHorizon;

const missingRiskIndicators: NotifyOptions = {
  // @ts-expect-error risk profiles require preferredIndicators.
  riskProfile: { tolerance: "moderate", horizonMonths: 12 },
};
void missingRiskIndicators;

const invalidRiskTolerance: NotifyOptions = {
  riskProfile: {
    // @ts-expect-error tolerance is a closed conservative/moderate/aggressive union.
    tolerance: "reckless",
    horizonMonths: 12,
    preferredIndicators: [],
  },
};
void invalidRiskTolerance;

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
  const options: NotifyOptions = {
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

test("throws when the seller rejects the notification instead of continuing", async () => {
  const wallet = Wallet.createRandom();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        result: {
          parts: [{ data: { status: "rejected", reason: "unsafe_gateway", job_id: "7" } }],
        },
      }),
    );

  try {
    await assert.rejects(
      notifyFunded("https://seller.example/a2a", wallet, 7n, {
        gatewayUrl: "https://buyer.trycloudflare.com",
        gatewayToken: "relay-token",
      }),
      /notify_funded not accepted: status=rejected \(unsafe_gateway\)/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("surfaces the retryable flag when verification is unavailable", async () => {
  const wallet = Wallet.createRandom();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        result: {
          parts: [{ data: { status: "rejected", reason: "verification_unavailable", retryable: true } }],
        },
      }),
    );

  try {
    await assert.rejects(
      notifyFunded("https://seller.example/a2a", wallet, 7n, {
        gatewayUrl: "https://buyer.trycloudflare.com",
        gatewayToken: "relay-token",
      }),
      /status=rejected \(verification_unavailable\) \[retryable\]/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("serializes every required risk-profile field into the signed context", () => {
  const context = JSON.parse(buildNotifyContext({
    riskProfile: {
      tolerance: "aggressive",
      horizonMonths: 24,
      preferredIndicators: ["MACD", "ATR"],
    },
  })) as { risk_profile: Record<string, unknown> };

  assert.deepEqual(context.risk_profile, {
    tolerance: "aggressive",
    horizonMonths: 24,
    preferredIndicators: ["MACD", "ATR"],
  });
});
