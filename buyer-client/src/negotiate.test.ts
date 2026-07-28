import assert from "node:assert/strict";
import test from "node:test";
import {
  getBytes,
  keccak256,
  toUtf8Bytes,
  verifyMessage,
  Wallet,
} from "ethers";
import { parseUnits } from "ethers";
import {
  assertQuoteWithinBudget,
  buildJobDescription,
  DEFAULT_MAX_PRICE_U,
  negotiate,
  NOTIFY_CONTEXT_REQUIRED,
  resolveMaxBudgetWei,
  type NegotiationEnvelope,
} from "./negotiate.js";

test("resolveMaxBudgetWei falls back to the default cap when unset", () => {
  assert.equal(resolveMaxBudgetWei({}), parseUnits(String(DEFAULT_MAX_PRICE_U), 18));
  assert.equal(resolveMaxBudgetWei({ MAX_PRICE_U: "  " }), parseUnits(String(DEFAULT_MAX_PRICE_U), 18));
});

test("resolveMaxBudgetWei honours a configured cap", () => {
  assert.equal(resolveMaxBudgetWei({ MAX_PRICE_U: "2.5" }), parseUnits("2.5", 18));
});

test("resolveMaxBudgetWei rejects a non-positive or non-numeric cap", () => {
  for (const bad of ["0", "-1", "abc", "NaN", "Infinity"]) {
    assert.throws(() => resolveMaxBudgetWei({ MAX_PRICE_U: bad }), /MAX_PRICE_U must be a positive number/);
  }
});

test("assertQuoteWithinBudget accepts a quote at or below the cap", () => {
  const cap = parseUnits("100", 18);
  assert.doesNotThrow(() => assertQuoteWithinBudget(1n, cap));
  assert.doesNotThrow(() => assertQuoteWithinBudget(cap, cap));
});

test("assertQuoteWithinBudget rejects a quote above the cap", () => {
  const cap = parseUnits("100", 18);
  assert.throws(() => assertQuoteWithinBudget(cap + 1n, cap), /exceeds the MAX_PRICE_U cap/);
});

test("assertQuoteWithinBudget rejects a non-positive quote", () => {
  const cap = parseUnits("100", 18);
  assert.throws(() => assertQuoteWithinBudget(0n, cap), /non-positive price/);
  assert.throws(() => assertQuoteWithinBudget(-5n, cap), /non-positive price/);
});

test("requests the exact notify-context requirement", async () => {
  let requestBody = "";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (_input, init) => {
    requestBody = String(init?.body);
    return new Response(JSON.stringify({
      result: {
        parts: [{
          data: {
            request: {
              task_description: "analyse portfolio",
              terms: {
                deliverables: "report",
                quality_standards: "cited",
                success_criteria: NOTIFY_CONTEXT_REQUIRED,
              },
            },
            response: {
              accepted: true,
              terms: {
                price: "1",
                currency: "USDT",
                deliverables: "report",
                quality_standards: "cited",
                success_criteria: NOTIFY_CONTEXT_REQUIRED,
              },
              provider_sig: "0xsigned",
              negotiation_hash: "0xhash",
            },
          },
        }],
      },
    }));
  };

  try {
    await negotiate("https://seller.example/a2a", "analyse portfolio", "report", "cited");
  } finally {
    globalThis.fetch = originalFetch;
  }

  const payload = JSON.parse(requestBody);
  assert.equal(
    payload.params.message.parts[0].data.terms.success_criteria,
    NOTIFY_CONTEXT_REQUIRED,
  );
});

test("rejects an accepted negotiation response missing the notify-context marker", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    result: {
      parts: [{
        data: {
          request: {
            task_description: "analyse portfolio",
            terms: {
              deliverables: "report",
              quality_standards: "cited",
              success_criteria: NOTIFY_CONTEXT_REQUIRED,
            },
          },
          response: {
            accepted: true,
            terms: {
              price: "1",
              currency: "USDT",
              deliverables: "report",
              quality_standards: "cited",
            },
            provider_sig: "0xsigned",
            negotiation_hash: "0xhash",
          },
        },
      }],
    },
  }));

  try {
    await assert.rejects(
      negotiate(
        "https://seller.example/a2a",
        "analyse portfolio",
        "report",
        "cited",
      ),
      new Error("Invalid negotiation: required notify-context marker missing or altered"),
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("rejects an accepted negotiation response with an altered notify-context marker", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    result: {
      parts: [{
        data: {
          request: {
            task_description: "analyse portfolio",
            terms: {
              deliverables: "report",
              quality_standards: "cited",
              success_criteria: NOTIFY_CONTEXT_REQUIRED,
            },
          },
          response: {
            accepted: true,
            terms: {
              price: "1",
              currency: "USDT",
              deliverables: "report",
              quality_standards: "cited",
              success_criteria: "legacy_optional_delivery",
            },
            provider_sig: "0xsigned",
            negotiation_hash: "0xhash",
          },
        },
      }],
    },
  }));

  try {
    await assert.rejects(
      negotiate(
        "https://seller.example/a2a",
        "analyse portfolio",
        "report",
        "cited",
      ),
      new Error("Invalid negotiation: required notify-context marker missing or altered"),
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("copies the seller-signed requirement into the on-chain description", () => {
  const envelope = {
    request: {
      task_description: "analyse portfolio",
      terms: {
        deliverables: "report",
        quality_standards: "cited",
        success_criteria: NOTIFY_CONTEXT_REQUIRED,
      },
    },
    response: {
      accepted: true,
      terms: {
        price: "1",
        currency: "USDT",
        deliverables: "report",
        quality_standards: "cited",
        success_criteria: NOTIFY_CONTEXT_REQUIRED,
      },
      negotiated_at: 1,
      negotiation_hash: "0xhash",
      provider_sig: "0xsigned",
    },
  } satisfies NegotiationEnvelope;

  const description = JSON.parse(buildJobDescription(envelope));
  assert.equal(description.terms.success_criteria, NOTIFY_CONTEXT_REQUIRED);
  assert.equal(description.negotiation_hash, "0xhash");
  assert.equal(description.provider_sig, "0xsigned");
});

test("rejects a job description whose response is missing the notify-context marker", () => {
  const envelope = {
    request: {
      task_description: "analyse portfolio",
      terms: {
        deliverables: "report",
        quality_standards: "cited",
        success_criteria: NOTIFY_CONTEXT_REQUIRED,
      },
    },
    response: {
      accepted: true,
      terms: {
        price: "1",
        currency: "USDT",
        deliverables: "report",
        quality_standards: "cited",
      },
      negotiated_at: 1,
      negotiation_hash: "0xhash",
      provider_sig: "0xsigned",
    },
  } satisfies NegotiationEnvelope;

  assert.throws(
    () => buildJobDescription(envelope),
    new Error("Invalid negotiation: required notify-context marker missing or altered"),
  );
});

test("rejects a job description whose response has an altered notify-context marker", () => {
  const envelope = {
    request: {
      task_description: "analyse portfolio",
      terms: {
        deliverables: "report",
        quality_standards: "cited",
        success_criteria: NOTIFY_CONTEXT_REQUIRED,
      },
    },
    response: {
      accepted: true,
      terms: {
        price: "1",
        currency: "USDT",
        deliverables: "report",
        quality_standards: "cited",
        success_criteria: "legacy_optional_delivery",
      },
      negotiated_at: 1,
      negotiation_hash: "0xhash",
      provider_sig: "0xsigned",
    },
  } satisfies NegotiationEnvelope;

  assert.throws(
    () => buildJobDescription(envelope),
    new Error("Invalid negotiation: required notify-context marker missing or altered"),
  );
});

test("rejects a job description whose response has a non-string notify-context marker", () => {
  const envelope = {
    request: {
      task_description: "analyse portfolio",
      terms: {
        deliverables: "report",
        quality_standards: "cited",
        success_criteria: NOTIFY_CONTEXT_REQUIRED,
      },
    },
    response: {
      accepted: true,
      terms: {
        price: "1",
        currency: "USDT",
        deliverables: "report",
        quality_standards: "cited",
        success_criteria: 42,
      },
      negotiated_at: 1,
      negotiation_hash: "0xhash",
      provider_sig: "0xsigned",
    },
  } as unknown as NegotiationEnvelope;

  assert.throws(
    () => buildJobDescription(envelope),
    new Error("Invalid negotiation: required notify-context marker missing or altered"),
  );
});

function canonicalize(value: unknown): unknown {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, child]) => [key, canonicalize(child)]),
  );
}

test("preserves a verifiable provider signature over the marked description", async () => {
  const provider = Wallet.createRandom();
  const signedContent = {
    version: 1,
    negotiated_at: 1,
    task: "analyse portfolio",
    terms: {
      deliverables: "report",
      quality_standards: "cited",
      success_criteria: NOTIFY_CONTEXT_REQUIRED,
    },
    price: "1",
    currency: "0x1111111111111111111111111111111111111111",
    chain_id: 97,
    verifying_contract: "0x2222222222222222222222222222222222222222",
  };
  const negotiationHash = keccak256(
    toUtf8Bytes(JSON.stringify(canonicalize(signedContent))),
  );
  const providerSig = await provider.signMessage(getBytes(negotiationHash));
  const envelope = {
    request: {
      task_description: signedContent.task,
      terms: signedContent.terms,
    },
    response: {
      accepted: true,
      terms: {
        ...signedContent.terms,
        price: signedContent.price,
        currency: signedContent.currency,
      },
      negotiated_at: signedContent.negotiated_at,
    },
    negotiation_hash: negotiationHash,
    provider_sig: providerSig,
    chain_id: signedContent.chain_id,
    verifying_contract: signedContent.verifying_contract,
  } satisfies NegotiationEnvelope;

  const description = JSON.parse(buildJobDescription(envelope));
  const { negotiation_hash, provider_sig, ...rehydratedContent } = description;
  const rebuiltHash = keccak256(
    toUtf8Bytes(JSON.stringify(canonicalize(rehydratedContent))),
  );
  assert.equal(rebuiltHash, negotiation_hash);
  assert.equal(
    verifyMessage(getBytes(negotiation_hash), provider_sig),
    provider.address,
  );
});
