import { randomBytes } from "node:crypto";
import { getAddress, type Wallet } from "ethers";

// These values must match stockanalyst/app/agent/x402_tokens.py.
export type PaymentTokenSymbol = "U" | "USD1" | "USDC" | "USDT";
export type PaymentTransferMethod = "eip3009" | "permit2-exact";
export const PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3";
export const PAYMENT_TOKENS = {
  U: {
    asset: "0xcE24439F2D9C6a2289F741120FE202248B666666",
    name: "United Stables",
    version: "1",
    transferMethod: "eip3009",
  },
  USD1: {
    asset: "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d",
    name: "World Liberty Financial USD",
    version: "1",
    transferMethod: "eip3009",
  },
  USDC: {
    asset: "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
    name: "USD Coin",
    version: "2",
    transferMethod: "permit2-exact",
  },
  USDT: {
    asset: "0x55d398326f99059fF775485246999027B3197955",
    name: "Tether USD",
    version: "1",
    transferMethod: "permit2-exact",
  },
} as const;
export const BSC_MAINNET_CHAIN_ID = 56;
export const PAID_AMOUNT = "100000000000000000";
export const PAYMENT_TIMEOUT_SECONDS = 600;

export interface B402PaymentExtra {
  name: string;
  version: string;
  assetTransferMethod: PaymentTransferMethod;
  signerAddress?: string;
  spenderAddress?: string;
  [key: string]: unknown;
}

export interface B402PaymentRequirement {
  scheme: "exact";
  network: `eip155:${number}`;
  amount: string;
  asset: string;
  payTo: string;
  maxTimeoutSeconds: number;
  extra: B402PaymentExtra;
}

export interface B402PaymentResource {
  url: string;
  description: string;
  mimeType: "application/json";
}

export interface PaidPaymentChallenge {
  x402Version: 2;
  resource: B402PaymentResource;
  accepted: B402PaymentRequirement;
}

function assertExactPaidAmount(challenge: PaidPaymentChallenge): void {
  if (challenge.accepted.amount !== PAID_AMOUNT) {
    throw new Error("Payment challenge must use the exact paid amount");
  }
}

export function resolveX402SellerWallet(
  env: Readonly<Record<string, string | undefined>> = process.env,
): string {
  const raw = env["X402_SELLER_WALLET"]?.trim();
  if (!raw) throw new Error("X402_SELLER_WALLET is required");
  try {
    return getAddress(raw).toLowerCase();
  } catch {
    throw new Error("X402_SELLER_WALLET must be a valid EVM address");
  }
}

/**
 * Build and locally sign an x402 v2 payment proof for the selected method.
 *
 * This module has no command-line side effects, so the asynchronous client can
 * build the payment wire format without importing a runnable CLI module.
 */
export async function buildPaymentProof(
  wallet: Wallet,
  challenge: PaidPaymentChallenge,
  ttlSeconds: number = PAYMENT_TIMEOUT_SECONDS,
): Promise<string> {
  assertExactPaidAmount(challenge);
  if (challenge.accepted.extra.assetTransferMethod === "permit2-exact") {
    const { buildPermit2PaymentProof } = await import("./x402-permit2.js");
    return buildPermit2PaymentProof(wallet, challenge, ttlSeconds);
  }

  const { accepted, resource } = challenge;
  const now = Math.floor(Date.now() / 1000);
  const nonce = `0x${randomBytes(32).toString("hex")}`;
  const recipient = getAddress(accepted.payTo).toLowerCase();

  const authorization = {
    from: wallet.address.toLowerCase(),
    to: recipient,
    value: accepted.amount,
    validAfter: "0",
    validBefore: String(now + ttlSeconds),
    nonce,
  };
  const domain = {
    name: accepted.extra.name,
    version: accepted.extra.version,
    chainId: BSC_MAINNET_CHAIN_ID,
    verifyingContract: accepted.asset,
  };
  const types = {
    TransferWithAuthorization: [
      { name: "from", type: "address" },
      { name: "to", type: "address" },
      { name: "value", type: "uint256" },
      { name: "validAfter", type: "uint256" },
      { name: "validBefore", type: "uint256" },
      { name: "nonce", type: "bytes32" },
    ],
  };

  const signature = await wallet.signTypedData(domain, types, {
    from: authorization.from,
    to: authorization.to,
    value: BigInt(authorization.value),
    validAfter: BigInt(authorization.validAfter),
    validBefore: BigInt(authorization.validBefore),
    nonce: authorization.nonce,
  });
  const proof = {
    x402Version: 2,
    resource: { ...resource },
    accepted: {
      ...accepted,
      extra: { ...accepted.extra },
    },
    payload: {
      signature,
      authorization,
    },
  };
  return Buffer.from(JSON.stringify(proof)).toString("base64");
}
