import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { TypedDataEncoder, Wallet, verifyTypedData } from "ethers";
import {
  BSC_MAINNET_CHAIN_ID,
  PAID_AMOUNT,
  PAYMENT_TOKENS,
  PERMIT2_ADDRESS,
  buildPaymentProof,
  type PaidPaymentChallenge,
  type PaymentTokenSymbol,
} from "./x402-payment.js";
import { buildPermit2PaymentProof } from "./x402-permit2.js";

const NOW = 1_785_484_800;
const TTL_SECONDS = 600;
const PRIVATE_KEY = `0x${"31".repeat(32)}`;
const SELLER = "0xd10bddc20e4dc42a1a19a9653e994991e25b8153";
const SIGNER = "0x1111111111111111111111111111111111111111";
const SPENDER = "0x2222222222222222222222222222222222222222";

const PERMIT2_TYPES = {
  PermitWitnessTransferFrom: [
    { name: "permitted", type: "TokenPermissions" },
    { name: "spender", type: "address" },
    { name: "nonce", type: "uint256" },
    { name: "deadline", type: "uint256" },
    { name: "witness", type: "Witness" },
  ],
  TokenPermissions: [
    { name: "token", type: "address" },
    { name: "amount", type: "uint256" },
  ],
  Witness: [
    { name: "to", type: "address" },
    { name: "validAfter", type: "uint256" },
  ],
};

interface Permit2WireProof {
  x402Version: 2;
  resource: PaidPaymentChallenge["resource"];
  accepted: PaidPaymentChallenge["accepted"];
  payload: {
    signature: string;
    permit2Authorization: {
      permitted: { token: string; amount: string };
      from: string;
      spender: string;
      nonce: string;
      deadline: string;
      witness: { to: string; validAfter: string };
    };
    domain?: unknown;
    types?: unknown;
    primaryType?: unknown;
  };
}

function permit2Challenge(
  token: Extract<PaymentTokenSymbol, "USDC" | "USDT"> = "USDC",
): PaidPaymentChallenge {
  const metadata = PAYMENT_TOKENS[token];
  return {
    x402Version: 2,
    resource: {
      url: "https://agent.example/x402/analyze/async",
      description: "Stock analysis for AAPL",
      mimeType: "application/json",
    },
    accepted: {
      scheme: "exact",
      network: "eip155:56",
      amount: PAID_AMOUNT,
      asset: metadata.asset,
      payTo: SELLER,
      maxTimeoutSeconds: TTL_SECONDS,
      extra: {
        name: metadata.name,
        version: metadata.version,
        assetTransferMethod: "permit2-exact",
        signerAddress: SIGNER,
        spenderAddress: SPENDER,
      },
    },
    promotional: false,
  };
}

function decodeProof(proof: string): Permit2WireProof {
  return JSON.parse(Buffer.from(proof, "base64").toString("utf8")) as Permit2WireProof;
}

function canonicalDecimal(value: string): boolean {
  return value === "0" || /^[1-9][0-9]*$/.test(value);
}

test("buildPermit2PaymentProof emits the exact local Permit2 wire proof", async () => {
  const wallet = new Wallet(PRIVATE_KEY);
  const challenge = permit2Challenge("USDC");
  const encoded = await buildPermit2PaymentProof(
    wallet,
    challenge,
    TTL_SECONDS,
    () => NOW,
  );
  const proof = decodeProof(encoded);
  const authorization = proof.payload.permit2Authorization;

  assert.deepEqual(proof.resource, challenge.resource);
  assert.deepEqual(proof.accepted, challenge.accepted);
  assert.deepEqual(Object.keys(proof.payload), ["signature", "permit2Authorization"]);
  assert.deepEqual(Object.keys(authorization), [
    "permitted",
    "from",
    "spender",
    "nonce",
    "deadline",
    "witness",
  ]);
  assert.deepEqual(authorization.permitted, {
    token: PAYMENT_TOKENS.USDC.asset,
    amount: PAID_AMOUNT,
  });
  assert.equal(authorization.from, wallet.address.toLowerCase());
  assert.equal(authorization.spender, challenge.accepted.extra.spenderAddress);
  assert.equal(authorization.witness.to, challenge.accepted.payTo);
  assert.equal(authorization.witness.validAfter, String(NOW));
  assert.equal(authorization.deadline, String(NOW + TTL_SECONDS));
  assert.equal(proof.payload.domain, undefined);
  assert.equal(proof.payload.types, undefined);
  assert.equal(proof.payload.primaryType, undefined);

  for (const value of [
    authorization.permitted.amount,
    authorization.nonce,
    authorization.deadline,
    authorization.witness.validAfter,
  ]) {
    assert.equal(canonicalDecimal(value), true, value);
    assert.ok(BigInt(value) < (1n << 256n), value);
  }

  const domain = {
    name: "Permit2",
    chainId: BSC_MAINNET_CHAIN_ID,
    verifyingContract: PERMIT2_ADDRESS,
  };
  assert.equal("version" in domain, false);
  assert.equal(domain.chainId, 56);
  assert.equal(domain.verifyingContract, "0x000000000022D473030F116dDEE9F6B43aC78BA3");
  assert.equal(
    TypedDataEncoder.from(PERMIT2_TYPES).primaryType,
    "PermitWitnessTransferFrom",
  );
  assert.equal(
    verifyTypedData(
      domain,
      PERMIT2_TYPES,
      {
        permitted: {
          token: authorization.permitted.token,
          amount: BigInt(authorization.permitted.amount),
        },
        spender: authorization.spender,
        nonce: BigInt(authorization.nonce),
        deadline: BigInt(authorization.deadline),
        witness: {
          to: authorization.witness.to,
          validAfter: BigInt(authorization.witness.validAfter),
        },
      },
      proof.payload.signature,
    ),
    wallet.address,
  );
});

test("Permit2 proofs use fresh independent 256-bit nonce values", async () => {
  const wallet = new Wallet(PRIVATE_KEY);
  const challenge = permit2Challenge("USDT");
  const first = decodeProof(await buildPermit2PaymentProof(
    wallet,
    challenge,
    TTL_SECONDS,
    () => NOW,
  ));
  const second = decodeProof(await buildPermit2PaymentProof(
    wallet,
    challenge,
    TTL_SECONDS,
    () => NOW,
  ));
  const firstNonce = first.payload.permit2Authorization.nonce;
  const secondNonce = second.payload.permit2Authorization.nonce;

  assert.notEqual(firstNonce, secondNonce);
  assert.equal(canonicalDecimal(firstNonce), true);
  assert.equal(canonicalDecimal(secondNonce), true);
  assert.ok(BigInt(firstNonce) < (1n << 256n));
  assert.ok(BigInt(secondNonce) < (1n << 256n));
});

test("buildPermit2PaymentProof independently recovers the USDT signer", async () => {
  const wallet = new Wallet(PRIVATE_KEY);
  const challenge = permit2Challenge("USDT");
  const proof = decodeProof(await buildPermit2PaymentProof(
    wallet,
    challenge,
    TTL_SECONDS,
    () => NOW,
  ));
  const authorization = proof.payload.permit2Authorization;
  const domain = {
    name: "Permit2",
    chainId: 56,
    verifyingContract: PERMIT2_ADDRESS,
  };

  assert.equal(proof.accepted.asset, PAYMENT_TOKENS.USDT.asset);
  assert.equal(proof.accepted.extra.name, PAYMENT_TOKENS.USDT.name);
  assert.equal(proof.accepted.extra.version, PAYMENT_TOKENS.USDT.version);
  assert.equal(proof.accepted.extra.assetTransferMethod, "permit2-exact");
  assert.equal(authorization.permitted.token, PAYMENT_TOKENS.USDT.asset);
  assert.deepEqual(domain, {
    name: "Permit2",
    chainId: BSC_MAINNET_CHAIN_ID,
    verifyingContract: "0x000000000022D473030F116dDEE9F6B43aC78BA3",
  });
  assert.equal(
    verifyTypedData(
      domain,
      PERMIT2_TYPES,
      {
        permitted: {
          token: authorization.permitted.token,
          amount: BigInt(authorization.permitted.amount),
        },
        spender: authorization.spender,
        nonce: BigInt(authorization.nonce),
        deadline: BigInt(authorization.deadline),
        witness: {
          to: authorization.witness.to,
          validAfter: BigInt(authorization.witness.validAfter),
        },
      },
      proof.payload.signature,
    ),
    wallet.address,
  );
});

test("buildPaymentProof dispatches Permit2 without provider or typed-data metadata on wire", async () => {
  const wallet = new Wallet(PRIVATE_KEY);
  assert.equal(wallet.provider, null);
  const proof = decodeProof(await buildPaymentProof(
    wallet,
    permit2Challenge("USDT"),
    TTL_SECONDS,
  ));

  assert.equal("authorization" in proof.payload, false);
  assert.equal("domain" in proof.payload, false);
  assert.equal("types" in proof.payload, false);
  assert.equal("primaryType" in proof.payload, false);
  assert.equal(
    proof.payload.permit2Authorization.permitted.token,
    PAYMENT_TOKENS.USDT.asset,
  );

  const permit2Module = readFileSync(
    new URL("./x402-permit2.js", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(permit2Module, /JsonRpcProvider|BrowserProvider|new Provider/);
});
