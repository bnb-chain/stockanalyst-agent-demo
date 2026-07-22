import {
  hexlify,
  randomBytes,
  verifyTypedData,
  type Signer,
  type TypedDataField,
} from "ethers";
import { CONTRACTS } from "./erc8183.js";

export interface NotifyOptions {
  gatewayUrl?: string;
  gatewayToken?: string;
  portfolio?: Array<{ symbol: string; shares: number; avgCost: number; currency: string }>;
  riskProfile?: {
    tolerance: "conservative" | "moderate" | "aggressive";
    horizonMonths: number;
    preferredIndicators: string[];
  };
}

export interface NotifyAuthorization {
  context: string;
  expires_at: number;
  nonce: string;
  signature: string;
}

const NOTIFY_DOMAIN = {
  name: "stockanalyst-notify-funded",
  version: "1",
  chainId: CONTRACTS.CHAIN_ID,
  verifyingContract: CONTRACTS.COMMERCE,
} as const;

const NOTIFY_TYPES: Record<string, TypedDataField[]> = {
  NotifyFunded: [
    { name: "jobId", type: "uint256" },
    { name: "context", type: "string" },
    { name: "expiresAt", type: "uint64" },
    { name: "nonce", type: "bytes32" },
  ],
};

/** Serialize the complete buyer-supplied notification context exactly once. */
export function buildNotifyContext(options: NotifyOptions): string {
  const context: Record<string, unknown> = {};
  if (options.gatewayUrl) context["delivery_gateway_url"] = options.gatewayUrl;
  if (options.gatewayToken) context["delivery_gateway_token"] = options.gatewayToken;
  if (options.portfolio?.length) context["portfolio"] = options.portfolio;
  if (options.riskProfile) context["risk_profile"] = options.riskProfile;
  return JSON.stringify(context);
}

export async function createNotifyAuthorization(
  signer: Signer,
  jobId: bigint,
  context: string,
  options: { nowSeconds?: number; nonce?: string } = {},
): Promise<NotifyAuthorization> {
  const nowSeconds = options.nowSeconds ?? Math.floor(Date.now() / 1000);
  const expires_at = nowSeconds + 300;
  const nonce = options.nonce ?? hexlify(randomBytes(32));
  const signature = await signer.signTypedData(NOTIFY_DOMAIN, NOTIFY_TYPES, {
    jobId,
    context,
    expiresAt: expires_at,
    nonce,
  });

  return { context, expires_at, nonce, signature: signature.slice(2) };
}

export function recoverNotifySigner(jobId: bigint, authorization: NotifyAuthorization): string {
  return verifyTypedData(NOTIFY_DOMAIN, NOTIFY_TYPES, {
    jobId,
    context: authorization.context,
    expiresAt: authorization.expires_at,
    nonce: authorization.nonce,
  }, authorization.signature.startsWith("0x") ? authorization.signature : `0x${authorization.signature}`);
}
