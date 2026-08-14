import { Buffer } from "node:buffer";
import {
  getAddress,
  hexlify,
  randomBytes,
  sha256,
  toUtf8Bytes,
  type TypedDataDomain,
  type TypedDataField,
} from "ethers";


const MAX_TIMEOUT_SECONDS = 600;
const BODY_PATH = "/x402/analyze/async";
const NONCE_PATTERN = /^0x[0-9a-f]{64}$/;
const SIGNATURE_PATTERN = /^0x[0-9a-fA-F]{130}$/;

const domain: TypedDataDomain = {
  name: "Stock Analyst Promo",
  version: "1",
  chainId: 56,
};
const types: Record<string, TypedDataField[]> = {
  PromoAuthorization: [
    { name: "address", type: "address" },
    { name: "method", type: "string" },
    { name: "path", type: "string" },
    { name: "bodyHash", type: "bytes32" },
    { name: "nonce", type: "bytes32" },
    { name: "expiresAt", type: "uint64" },
  ],
};
const metadata = {
  scheme: "eip712-wallet",
  network: "eip155:56",
  header: "Wallet-Signature",
  maxTimeoutSeconds: MAX_TIMEOUT_SECONDS,
  domain: {
    name: "Stock Analyst Promo",
    version: "1",
    chainId: 56,
  },
  primaryType: "PromoAuthorization",
  types: {
    PromoAuthorization: types["PromoAuthorization"].map((field) => ({
      name: field.name,
      type: field.type,
    })),
  },
  message: {
    method: "POST",
    path: BODY_PATH,
    bodyHash: "sha256",
  },
} as const;

export const PROMO_WALLET_REQUIREMENT = {
  domain,
  types,
  metadata,
} as const;

export interface PromoWalletSigner {
  readonly address: string;
  signTypedData(
    domain: TypedDataDomain,
    types: Record<string, TypedDataField[]>,
    value: Record<string, unknown>,
  ): Promise<string>;
}

export interface PromoWalletSignatureOptions {
  nowSeconds?: number;
  nonce?: string;
}

export interface PromoWalletSignature {
  header: string;
  expiresAt: number;
  bodyHash: string;
}

export interface ParsedPromoWalletSignature {
  address: string;
  nonce: string;
  expiresAt: number;
}

function canonicalJson(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string" || typeof value === "boolean") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("invalid_wallet_authorization");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(object[key])}`
    )).join(",")}}`;
  }
  throw new Error("invalid_wallet_authorization");
}

export function parsePromoWalletRequirement(
  value: unknown,
): typeof metadata {
  let actual: string;
  let expected: string;
  try {
    actual = canonicalJson(value);
    expected = canonicalJson(metadata);
  } catch {
    throw new Error("invalid_wallet_authorization");
  }
  if (actual !== expected) throw new Error("invalid_wallet_authorization");
  return metadata;
}

export async function buildPromoWalletSignature(
  signer: PromoWalletSigner,
  body: string,
  options: PromoWalletSignatureOptions = {},
): Promise<PromoWalletSignature> {
  if (typeof body !== "string") throw new Error("invalid_wallet_authorization");
  const bytes = toUtf8Bytes(body);
  if (bytes.length > 256 * 1024) throw new Error("invalid_wallet_authorization");
  const nowSeconds = options.nowSeconds ?? Math.floor(Date.now() / 1000);
  const nonce = options.nonce ?? hexlify(randomBytes(32));
  if (
    !Number.isSafeInteger(nowSeconds)
    || nowSeconds < 0
    || !NONCE_PATTERN.test(nonce)
  ) {
    throw new Error("invalid_wallet_authorization");
  }
  let address: string;
  try {
    address = getAddress(signer.address);
  } catch {
    throw new Error("invalid_wallet_authorization");
  }
  const expiresAt = nowSeconds + MAX_TIMEOUT_SECONDS;
  const bodyHash = sha256(bytes);
  let signature: string;
  try {
    signature = await signer.signTypedData(domain, types, {
      address,
      method: "POST",
      path: BODY_PATH,
      bodyHash,
      nonce,
      expiresAt,
    });
  } catch {
    throw new Error("invalid_wallet_authorization");
  }
  if (!SIGNATURE_PATTERN.test(signature)) {
    throw new Error("invalid_wallet_authorization");
  }
  const header = Buffer.from(JSON.stringify({
    version: 1,
    address,
    nonce,
    expiresAt,
    signature,
  }), "utf8").toString("base64url");
  return { header, expiresAt, bodyHash };
}

export function parsePromoWalletSignature(
  header: string,
): ParsedPromoWalletSignature {
  if (
    typeof header !== "string"
    || !/^[A-Za-z0-9_-]{1,4096}$/.test(header)
  ) {
    throw new Error("invalid_wallet_authorization");
  }
  try {
    const bytes = Buffer.from(header, "base64url");
    if (bytes.toString("base64url") !== header) throw new Error("invalid");
    const value = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(bytes),
    ) as unknown;
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      throw new Error("invalid");
    }
    const object = value as Record<string, unknown>;
    if (
      Object.keys(object).sort().join(",")
        !== "address,expiresAt,nonce,signature,version"
      || object["version"] !== 1
      || typeof object["address"] !== "string"
      || getAddress(object["address"]) !== object["address"]
      || typeof object["nonce"] !== "string"
      || !NONCE_PATTERN.test(object["nonce"])
      || typeof object["expiresAt"] !== "number"
      || !Number.isSafeInteger(object["expiresAt"])
      || object["expiresAt"] <= 0
      || typeof object["signature"] !== "string"
      || !SIGNATURE_PATTERN.test(object["signature"])
    ) {
      throw new Error("invalid");
    }
    return {
      address: object["address"],
      nonce: object["nonce"],
      expiresAt: object["expiresAt"],
    };
  } catch {
    throw new Error("invalid_wallet_authorization");
  }
}
