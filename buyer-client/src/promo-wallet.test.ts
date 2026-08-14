import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import test from "node:test";
import { Wallet, verifyTypedData } from "ethers";

import {
  buildPromoWalletSignature,
  parsePromoWalletRequirement,
  PROMO_WALLET_REQUIREMENT,
} from "./promo-wallet.js";


test("builds a real EIP-712 wallet authorization bound to exact body", async () => {
  const wallet = Wallet.createRandom();
  const body = JSON.stringify({ symbols: ["AAPL"] });
  const nonce = `0x${"12".repeat(32)}`;
  const result = await buildPromoWalletSignature(wallet, body, {
    nowSeconds: 1_780_000_000,
    nonce,
  });

  const envelope = JSON.parse(
    Buffer.from(result.header, "base64url").toString("utf8"),
  ) as Record<string, unknown>;
  assert.equal(envelope["version"], 1);
  assert.equal(envelope["address"], wallet.address);
  assert.equal(envelope["nonce"], nonce);
  assert.equal(envelope["expiresAt"], 1_780_000_600);
  assert.equal(result.expiresAt, 1_780_000_600);

  const recovered = verifyTypedData(
    PROMO_WALLET_REQUIREMENT.domain,
    PROMO_WALLET_REQUIREMENT.types,
    {
      address: wallet.address,
      method: "POST",
      path: "/x402/analyze/async",
      bodyHash: result.bodyHash,
      nonce,
      expiresAt: result.expiresAt,
    },
    String(envelope["signature"]),
  );
  assert.equal(recovered, wallet.address);
});

test("parses only the exact public promo wallet requirement", () => {
  assert.deepEqual(
    parsePromoWalletRequirement(PROMO_WALLET_REQUIREMENT.metadata),
    PROMO_WALLET_REQUIREMENT.metadata,
  );
  assert.throws(
    () => parsePromoWalletRequirement({
      ...PROMO_WALLET_REQUIREMENT.metadata,
      network: "eip155:97",
    }),
    /invalid_wallet_authorization/,
  );
});
