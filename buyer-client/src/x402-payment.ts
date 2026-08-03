import { randomBytes } from "node:crypto";
import { getAddress, type Wallet } from "ethers";

// These values must match stockanalyst/app/agent/x402_verify.py.
export const U_TOKEN_ADDRESS = "0x330949Aed7d00FCe0558C64ED6FeC9792616cC39";
export const U_TOKEN_DOMAIN_NAME = process.env["U_TOKEN_DOMAIN_NAME"] ?? "U";
export const U_TOKEN_DOMAIN_VERSION = process.env["U_TOKEN_DOMAIN_VERSION"] ?? "1";
export const BSC_TESTNET_CHAIN_ID = 97;

export interface B402PaymentExtra {
  name: string;
  version: string;
  assetTransferMethod: "eip3009";
  signerAddress: string;
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
 * Build and EIP-712 sign an x402 v2 TransferWithAuthorization proof.
 *
 * This module has no command-line side effects, so the asynchronous client can
 * build the payment wire format without importing a runnable CLI module.
 */
export async function buildPaymentProof(
  wallet: Wallet,
  challenge: PaidPaymentChallenge,
  ttlSeconds: number = 600,
): Promise<string> {
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
    chainId: BSC_TESTNET_CHAIN_ID,
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
