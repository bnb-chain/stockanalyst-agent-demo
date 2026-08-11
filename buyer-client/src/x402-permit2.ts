import { randomBytes } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createInterface } from "node:readline/promises";
import { fileURLToPath } from "node:url";
import {
  Contract,
  JsonRpcProvider,
  Wallet,
} from "ethers";
import {
  BSC_MAINNET_CHAIN_ID,
  PAYMENT_TIMEOUT_SECONDS,
  PAYMENT_TOKENS,
  PERMIT2_ADDRESS,
  type PaidPaymentChallenge,
} from "./x402-payment.js";

export const PERMIT2_ALLOWANCE_TARGET = 50n * 10n ** 18n;
export const PERMIT2_PAYMENT_MINIMUM = 210000000000000000n;

export type Permit2TokenSymbol = "USDC" | "USDT";

export interface Permit2Provider {
  getNetwork(): Promise<{ chainId: bigint | number }>;
}

export interface Permit2TransactionReceipt {
  status: bigint | number | null;
}

export interface Permit2TransactionResponse {
  wait(): Promise<Permit2TransactionReceipt>;
}

export interface Permit2TokenContract {
  allowance(owner: string, spender: string): Promise<unknown>;
  approve(spender: string, amount: bigint): Promise<Permit2TransactionResponse>;
}

export interface Permit2AllowanceContext {
  token: string;
  walletAddress: string;
  rpcUrl: string;
  provider: Permit2Provider;
  contract: Permit2TokenContract;
  yes?: boolean;
  confirm?: (question: string) => Promise<string>;
  log?: (message: string) => void;
}

export interface Permit2CliContextOptions {
  token: string;
  yes: boolean;
  env: Readonly<Record<string, string | undefined>>;
}

export interface Permit2CliDependencies {
  createContext(options: Permit2CliContextOptions): Promise<Permit2AllowanceContext>;
}

const ERC20_ALLOWANCE_ABI = [
  "function allowance(address owner, address spender) view returns (uint256)",
  "function approve(address spender, uint256 amount) returns (bool)",
] as const;

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

function requirePermit2Token(token: string): Permit2TokenSymbol {
  if (token !== "USDC" && token !== "USDT") {
    throw new Error("Permit2 allowance token must be exactly USDC or USDT");
  }
  return token;
}

function requireRpcUrl(raw: string): string {
  if (!raw) {
    throw new Error("BSC_RPC_URL is required for Permit2 allowance operations");
  }
  try {
    const parsed = new URL(raw);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new Error("unsupported protocol");
    }
    return parsed.toString();
  } catch {
    throw new Error("BSC_RPC_URL must be a valid HTTP(S) URL");
  }
}

async function requireBscMainnet(provider: Permit2Provider): Promise<void> {
  const { chainId } = await provider.getNetwork();
  if (chainId !== 56 && chainId !== 56n) {
    throw new Error("BSC_RPC_URL provider network must have chain ID 56");
  }
}

export function assertPermit2PaymentReady(allowance: bigint): void {
  if (allowance < PERMIT2_PAYMENT_MINIMUM) {
    throw new Error("Permit2 allowance is below 0.21; run npm run x402:approve -- <USDC|USDT>");
  }
  if (allowance > PERMIT2_ALLOWANCE_TARGET) {
    throw new Error("Permit2 allowance exceeds 50; reset it to 50 or revoke it");
  }
}

export async function readPermit2Allowance(
  context: Permit2AllowanceContext,
): Promise<bigint> {
  requirePermit2Token(context.token);
  requireRpcUrl(context.rpcUrl);
  await requireBscMainnet(context.provider);
  const allowance = await context.contract.allowance(
    context.walletAddress,
    PERMIT2_ADDRESS,
  );
  if (typeof allowance !== "bigint") {
    throw new Error("ERC-20 allowance read must return bigint");
  }
  return allowance;
}

function logAllowanceSummary(
  context: Permit2AllowanceContext,
  currentAllowance: bigint,
  transactionCount: number,
): void {
  const token = requirePermit2Token(context.token);
  const log = context.log ?? console.log;
  log(`Chain: ${BSC_MAINNET_CHAIN_ID}`);
  log(`Wallet: ${context.walletAddress}`);
  log(`Token: ${token}`);
  log(`Token contract: ${PAYMENT_TOKENS[token].asset}`);
  log(`Canonical Permit2: ${PERMIT2_ADDRESS}`);
  log(`Current allowance: ${currentAllowance}`);
  log(`Target allowance: ${PERMIT2_ALLOWANCE_TARGET}`);
  log(`Transaction count: ${transactionCount}`);
}

async function requireConfirmation(
  context: Permit2AllowanceContext,
  action: "approve" | "revoke",
): Promise<void> {
  if (context.yes) return;
  if (!context.confirm) {
    throw new Error(`Permit2 ${action} declined: exact answer "yes" is required`);
  }
  const answer = await context.confirm(`Type exactly "yes" to ${action}: `);
  if (answer !== "yes") {
    throw new Error(`Permit2 ${action} declined`);
  }
}

async function sendApproval(
  contract: Permit2TokenContract,
  amount: bigint,
): Promise<void> {
  const transaction = await contract.approve(PERMIT2_ADDRESS, amount);
  const receipt = await transaction.wait();
  if (receipt.status !== 1 && receipt.status !== 1n) {
    throw new Error("Permit2 approval transaction must have receipt status 1");
  }
}

export async function approvePermit2Allowance(
  context: Permit2AllowanceContext,
): Promise<void> {
  const currentAllowance = await readPermit2Allowance(context);
  const transactionCount = currentAllowance === PERMIT2_ALLOWANCE_TARGET
    ? 0
    : currentAllowance === 0n ? 1 : 2;
  logAllowanceSummary(context, currentAllowance, transactionCount);
  if (transactionCount === 0) return;
  await requireConfirmation(context, "approve");
  if (currentAllowance !== 0n) {
    await sendApproval(context.contract, 0n);
  }
  await sendApproval(context.contract, PERMIT2_ALLOWANCE_TARGET);
}

export async function revokePermit2Allowance(
  context: Permit2AllowanceContext,
): Promise<void> {
  const currentAllowance = await readPermit2Allowance(context);
  logAllowanceSummary(context, currentAllowance, 1);
  await requireConfirmation(context, "revoke");
  await sendApproval(context.contract, 0n);
}

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

async function promptForConfirmation(question: string): Promise<string> {
  const prompt = createInterface({ input: process.stdin, output: process.stdout });
  try {
    return await prompt.question(question);
  } finally {
    prompt.close();
  }
}

async function createDefaultCliContext(
  options: Permit2CliContextOptions,
): Promise<Permit2AllowanceContext> {
  const token = requirePermit2Token(options.token);
  const rpcUrl = requireRpcUrl(options.env["BSC_RPC_URL"] ?? "");
  const keystorePath = options.env["KEYSTORE_PATH"]?.trim();
  if (!keystorePath) {
    throw new Error("KEYSTORE_PATH is required for Permit2 allowance operations");
  }
  const password = options.env["WALLET_PASSWORD"];
  if (!password) {
    throw new Error("WALLET_PASSWORD is required for Permit2 allowance operations");
  }

  const provider = new JsonRpcProvider(rpcUrl);
  const absoluteKeystorePath = resolve(
    fileURLToPath(new URL("..", import.meta.url)),
    keystorePath,
  );
  const encryptedWallet = await readFile(absoluteKeystorePath, "utf8");
  const wallet = (await Wallet.fromEncryptedJson(encryptedWallet, password)).connect(provider);
  const ethersContract = new Contract(
    PAYMENT_TOKENS[token].asset,
    ERC20_ALLOWANCE_ABI,
    wallet,
  );
  const contract: Permit2TokenContract = {
    allowance: (owner, spender) => ethersContract.allowance(owner, spender),
    approve: async (spender, amount) => {
      const transaction = await ethersContract.approve(spender, amount);
      return {
        wait: async () => {
          const receipt = await transaction.wait();
          return { status: receipt?.status ?? null };
        },
      };
    },
  };

  return {
    token,
    walletAddress: wallet.address,
    rpcUrl,
    provider,
    contract,
    yes: options.yes,
    confirm: promptForConfirmation,
    log: console.log,
  };
}

const DEFAULT_CLI_DEPENDENCIES: Permit2CliDependencies = {
  createContext: createDefaultCliContext,
};

export async function runPermit2AllowanceCli(
  args: readonly string[],
  env: Readonly<Record<string, string | undefined>> = process.env,
  dependencies: Permit2CliDependencies = DEFAULT_CLI_DEPENDENCIES,
): Promise<void> {
  const [action, token, ...flags] = args;
  if (action !== "allowance" && action !== "approve" && action !== "revoke") {
    throw new Error("Usage: x402-permit2.ts <allowance|approve|revoke> <USDC|USDT> [--yes]");
  }
  const symbol = requirePermit2Token(token ?? "");
  if (flags.some((flag) => flag !== "--yes") || flags.filter((flag) => flag === "--yes").length > 1) {
    throw new Error("Only the optional --yes flag is supported");
  }
  const yes = flags.includes("--yes");
  if (action === "allowance" && yes) {
    throw new Error("--yes is only valid for approve or revoke");
  }
  const context = await dependencies.createContext({ token: symbol, yes, env });
  if (action === "approve") {
    await approvePermit2Allowance(context);
    return;
  }
  if (action === "revoke") {
    await revokePermit2Allowance(context);
    return;
  }
  const currentAllowance = await readPermit2Allowance(context);
  logAllowanceSummary(context, currentAllowance, 0);
}

function isDirectExecution(): boolean {
  const entryPoint = process.argv[1];
  return entryPoint !== undefined
    && resolve(entryPoint) === fileURLToPath(import.meta.url);
}

if (isDirectExecution()) {
  void runPermit2AllowanceCli(process.argv.slice(2)).catch((error: unknown) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
