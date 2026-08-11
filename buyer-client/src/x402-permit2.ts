import { randomBytes } from "node:crypto";
import type { Wallet } from "ethers";
import {
  BSC_MAINNET_CHAIN_ID,
  PAYMENT_TIMEOUT_SECONDS,
  PERMIT2_ADDRESS,
  type PaidPaymentChallenge,
} from "./x402-payment.js";

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

export async function buildPermit2PaymentProof(
  wallet: Wallet,
  challenge: PaidPaymentChallenge,
  ttlSeconds: number = PAYMENT_TIMEOUT_SECONDS,
  nowSeconds: () => number = () => Math.floor(Date.now() / 1_000),
): Promise<string> {
  const { accepted, resource } = challenge;
  const spender = accepted.extra.spenderAddress;
  const now = nowSeconds();
  if (
    accepted.extra.assetTransferMethod !== "permit2-exact"
    || typeof spender !== "string"
    || !/^0x[0-9a-fA-F]{40}$/.test(spender)
    || !Number.isSafeInteger(now)
    || now < 0
    || !Number.isSafeInteger(ttlSeconds)
    || ttlSeconds <= 0
    || ttlSeconds > 3_600
  ) {
    throw new Error("Permit2 payment challenge is invalid");
  }

  const authorization = {
    permitted: {
      token: accepted.asset,
      amount: accepted.amount,
    },
    from: wallet.address.toLowerCase(),
    spender,
    nonce: BigInt(`0x${randomBytes(32).toString("hex")}`).toString(10),
    deadline: String(now + ttlSeconds),
    witness: {
      to: accepted.payTo,
      validAfter: String(now),
    },
  };
  const signature = await wallet.signTypedData(
    {
      name: "Permit2",
      chainId: BSC_MAINNET_CHAIN_ID,
      verifyingContract: PERMIT2_ADDRESS,
    },
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
  );
  return Buffer.from(JSON.stringify({
    x402Version: 2,
    resource: { ...resource },
    accepted: {
      ...accepted,
      extra: { ...accepted.extra },
    },
    payload: {
      signature,
      permit2Authorization: authorization,
    },
  })).toString("base64");
}
