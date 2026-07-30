import { randomBytes } from "node:crypto";
import type { Wallet } from "ethers";

// These values must match stockanalyst/app/agent/x402_verify.py.
export const SELLER_WALLET = "0x1ff095e1c5cf4bc72a3dc54be17b6cf85043fb67";
export const U_TOKEN_ADDRESS = "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565";
export const U_TOKEN_DOMAIN_NAME = process.env["U_TOKEN_DOMAIN_NAME"] ?? "U";
export const U_TOKEN_DOMAIN_VERSION = process.env["U_TOKEN_DOMAIN_VERSION"] ?? "1";
export const BSC_TESTNET_CHAIN_ID = 97;

/**
 * Build and EIP-712 sign an x402 v2 TransferWithAuthorization proof.
 *
 * This module has no command-line side effects, so the asynchronous client can
 * build the payment wire format without importing a runnable CLI module.
 */
export async function buildPaymentProof(
  wallet: Wallet,
  priceWei: string = "1000000000000000000",
  ttlSeconds: number = 600,
): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  const nonce = `0x${randomBytes(32).toString("hex")}`;

  const authorization = {
    from: wallet.address.toLowerCase(),
    to: SELLER_WALLET,
    value: priceWei,
    validAfter: "0",
    validBefore: String(now + ttlSeconds),
    nonce,
  };
  const domain = {
    name: U_TOKEN_DOMAIN_NAME,
    version: U_TOKEN_DOMAIN_VERSION,
    chainId: BSC_TESTNET_CHAIN_ID,
    verifyingContract: U_TOKEN_ADDRESS,
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
    scheme: "exact",
    network: `eip155:${BSC_TESTNET_CHAIN_ID}`,
    payload: {
      signature,
      authorization,
    },
  };
  return Buffer.from(JSON.stringify(proof)).toString("base64");
}
