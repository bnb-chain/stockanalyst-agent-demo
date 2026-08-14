#!/usr/bin/env node
/**
 * Durable x402 buyer example.
 *
 * The payment request returns a private job receipt immediately. The receipt
 * remains on disk until the report has been downloaded and saved, so rerunning
 * this command after a network or process interruption continues the job
 * without signing or paying again.
 */

import {
  closeSync,
  constants,
  existsSync,
  fsyncSync,
  linkSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import {
  createHmac,
  randomBytes,
  timingSafeEqual,
} from "node:crypto";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Contract, JsonRpcProvider, Wallet } from "ethers";
import { saveReport } from "./pdf-report.js";
import { GuardUserMemory, buildTaskFromMemory } from "./uomp.js";
import {
  beginAsyncAnalysis,
  createAsyncAnalysis,
  canonicalStatusPath,
  downloadAsyncReport,
  pollAsyncAnalysis,
  AsyncJobClientError,
  PaymentTokenUnavailableError,
  type AsyncAnalysisRequest,
  type AsyncJobReceipt,
  type AsyncJobStatus,
  type CreateRequestOptions,
  type FetchImpl,
  type PollOptions,
  type SleepImpl,
} from "./x402-async-client.js";
import {
  PAID_AMOUNT,
  PAYMENT_TOKENS,
  buildPaymentProof,
  resolveX402SellerWallet,
  type PaidPaymentChallenge,
  type PaymentTokenSymbol,
} from "./x402-payment.js";
import {
  assertPermit2PaymentReady,
  readPermit2Allowance,
  type Permit2AllowanceContext,
  type Permit2TokenSymbol,
} from "./x402-permit2.js";

const MODULE_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const AGENT_ENDPOINT = process.env["X402_ENDPOINT"] ?? "http://localhost:9000";
const RECEIPT_PATH = resolve(
  MODULE_DIRECTORY,
  "..",
  ".agent-data",
  "x402-job-receipt.json",
);
const PENDING_CREATE_PATH = resolve(
  MODULE_DIRECTORY,
  "..",
  ".agent-data",
  "x402-pending-create.json",
);
const CLI_LOCK_PATH = resolve(
  MODULE_DIRECTORY,
  "..",
  ".agent-data",
  "x402-async.lock",
);
const MAX_RECEIPT_BYTES = 16 * 1024;
const MAX_PENDING_BYTES = 256 * 1024;
const PENDING_RECOVERY_MILLISECONDS = 7 * 24 * 60 * 60 * 1_000;
const MAX_CREATE_ATTEMPTS = 3;
const CREATE_RETRY_MILLISECONDS = 1_000;
const MAX_CREATE_RETRY_MILLISECONDS = 8_000;
const LEGACY_PAID_AMOUNT_FOR_RECOVERY = "210000000000000000";
const ERC20_ALLOWANCE_ABI = [
  "function allowance(address owner, address spender) view returns (uint256)",
] as const;
const SAFE_ASYNC_JOB_CLIENT_ERROR_CODES = new Set([
  "analysis_empty_response",
  "analysis_failed",
  "analysis_timeout",
  "async_jobs_paused",
  "attempts_exhausted",
  "download_network_error",
  "invalid_endpoint",
  "invalid_payment_challenge",
  "invalid_payment_response",
  "invalid_receipt",
  "invalid_request",
  "invalid_response",
  "invalid_timeout",
  "job_conflict",
  "job_expired",
  "job_not_found",
  "job_service_unavailable",
  "job_state_unavailable",
  "network_error",
  "payment_backend_unavailable",
  "payment_failed",
  "payment_rejected",
  "payment_unavailable",
  "pending_binding_invalid",
  "pending_cleanup_failed",
  "pending_create_expired",
  "pending_create_mismatch",
  "pending_job_mismatch",
  "poll_timeout",
  "promo_rate_limited",
  "report_not_ready",
  "report_too_large",
  "request_too_large",
  "settlement_pending",
]);
const SAFE_ASYNC_CLI_ERROR_MESSAGES = new Set([
  "X402_SELLER_WALLET is required",
  "X402_SELLER_WALLET must be a valid EVM address",
  "X402_PAYMENT_TOKEN must be U, USD1, USDC, or USDT",
  "WALLET_PASSWORD is required to create a paid job",
  "KEYSTORE_PATH is required to create a paid job",
  "Permit2 preflight failed; verify BSC_RPC_URL, BSC mainnet chain ID 56, and the USDC allowance",
  "Permit2 preflight failed; verify BSC_RPC_URL, BSC mainnet chain ID 56, and the USDT allowance",
  "Permit2 allowance is below 0.1; run npm run x402:approve -- USDC",
  "Permit2 allowance is below 0.1; run npm run x402:approve -- USDT",
  "Permit2 allowance exceeds 50; reset it to 50 or revoke it",
  "Pending x402 access metadata is invalid",
  "Payment access metadata is invalid",
  "Pending x402 binding is invalid",
  "Stored x402 receipt is invalid",
  "Private x402 state is too large",
  "Pending x402 payment proof is invalid",
  "Pending x402 request is invalid",
  "Pending x402 payment proof is expired",
  "Payment challenge token does not match X402_PAYMENT_TOKEN",
  "Stored pending x402 request is invalid",
  "Invalid pending-create retry configuration",
  "another x402 asynchronous client is active; if it crashed, verify no client is running before removing the lock file",
  "X402_POLL_TIMEOUT_MS must be a positive integer",
]);
const SAFE_ASYNC_RECOVERY_MESSAGES = new Set([
  "The private job receipt was retained for a safe retry.",
  "The pending payment request was retained; rerun to recover without paying again.",
  "No durable x402 retry state was created.",
]);

export function resolveX402PaymentToken(
  env: Readonly<Record<string, string | undefined>> = process.env,
): PaymentTokenSymbol {
  const value = env["X402_PAYMENT_TOKEN"] ?? "U";
  if (
    value !== "U"
    && value !== "USD1"
    && value !== "USDC"
    && value !== "USDT"
  ) {
    throw new Error("X402_PAYMENT_TOKEN must be U, USD1, USDC, or USDT");
  }
  return value;
}

export function formatX402AccessSummary(
  access: X402PaymentAccess,
): string {
  const canonical = parseX402PaymentAccess(access);
  return `Payment: 0.1 ${canonical.token} (submitted)`;
}

export interface X402PaymentAccess {
  token: PaymentTokenSymbol;
  payTo: string;
}

function parseX402PaymentAccess(value: unknown): X402PaymentAccess {
  if (!isRecord(value)) throw new Error("Pending x402 access metadata is invalid");
  const token = value["token"];
  const payTo = value["payTo"];
  if (
    (
      token !== "U"
      && token !== "USD1"
      && token !== "USDC"
      && token !== "USDT"
    )
    || typeof payTo !== "string"
    || !/^0x[0-9a-f]{40}$/.test(payTo)
  ) {
    throw new Error("Pending x402 access metadata is invalid");
  }
  return { token, payTo };
}

export function paymentAccessFromChallenge(
  challenge: PaidPaymentChallenge,
): X402PaymentAccess {
  return paymentAccessFromAccepted(challenge.accepted);
}

function paymentAccessFromAccepted(
  accepted: unknown,
): X402PaymentAccess {
  if (!isRecord(accepted) || !isRecord(accepted["extra"])) {
    throw new Error("Payment access metadata is invalid");
  }
  const extra = accepted["extra"];
  const asset = accepted["asset"];
  const amount = accepted["amount"];
  const payTo = accepted["payTo"];
  if (typeof asset !== "string") {
    throw new Error("Payment access metadata is invalid");
  }
  const symbol = (Object.entries(PAYMENT_TOKENS).find(
    ([, token]) => token.asset.toLowerCase() === asset.toLowerCase(),
  )?.[0]) as PaymentTokenSymbol | undefined;
  if (symbol === undefined) throw new Error("Payment access metadata is invalid");
  const token = PAYMENT_TOKENS[symbol];
  const signerAddress = extra["signerAddress"];
  const spenderAddress = extra["spenderAddress"];
  const signerPresent = Object.prototype.hasOwnProperty.call(
    extra,
    "signerAddress",
  );
  const spenderPresent = Object.prototype.hasOwnProperty.call(
    extra,
    "spenderAddress",
  );
  const transferMethod = token.transferMethod;
  if (
    amount !== PAID_AMOUNT
    || typeof payTo !== "string"
    || !/^0x[0-9a-fA-F]{40}$/.test(payTo)
    || extra["name"] !== token.name
    || extra["version"] !== token.version
    || extra["assetTransferMethod"] !== transferMethod
    || !signerPresent
    || typeof signerAddress !== "string"
    || !/^0x[0-9a-fA-F]{40}$/.test(signerAddress)
    || (
      transferMethod === "permit2-exact"
      && (
        !spenderPresent
        || typeof spenderAddress !== "string"
        || !/^0x[0-9a-fA-F]{40}$/.test(spenderAddress)
      )
    )
  ) {
    throw new Error("Payment access metadata is invalid");
  }
  return {
    token: symbol,
    payTo: payTo.toLowerCase(),
  };
}

type SaveReportImpl = typeof saveReport;

interface StoredReceipt {
  jobId: string;
  jobToken: string;
  statusUrl: string;
  expiresAt: number;
}

export interface PendingCreateRecord {
  version: 1;
  paymentProof: string;
  request: AsyncAnalysisRequest;
  createdAt: number;
  proofExpiresAt: number;
  recoveryExpiresAt: number;
  jobId?: string;
  binding?: {
    version: 1;
    mac: string;
  };
}

export interface PendingCreateOptions extends CreateRequestOptions {
  maxAttempts?: number;
  baseRetryMilliseconds?: number;
  sleep?: SleepImpl;
  now?: () => number;
  expectedJobId?: string;
}

export interface PaidPendingCreateDependencies {
  createPermit2AllowanceReader(
    rpcUrl: string,
    walletAddress: string,
    token: Permit2TokenSymbol,
  ): () => Promise<bigint>;
  buildPaymentProof(
    wallet: Wallet,
    challenge: PaidPaymentChallenge,
  ): Promise<string>;
}

export interface AsyncStartupContext {
  symbols: string[];
  portfolio?: unknown[];
  riskProfile?: unknown;
}

export interface AsyncStartupDependencies extends PaidPendingCreateDependencies {
  fetch: FetchImpl;
  loadContext(): Promise<AsyncStartupContext>;
  loadWallet(
    env: Readonly<Record<string, string | undefined>>,
  ): Promise<Wallet>;
  log(message: string): void;
}

export interface AsyncStartupOptions {
  endpoint: string;
  receiptPath: string;
  pendingPath: string;
  env?: Readonly<Record<string, string | undefined>>;
  dependencies?: Partial<AsyncStartupDependencies>;
}

export interface AsyncStartupResult {
  receipt: AsyncJobReceipt;
  symbols: string[];
}

export interface AsyncCliProcess {
  writeError(message: string): void;
  setExitCode(code: number): void;
}

function createDefaultPermit2AllowanceReader(
  rpcUrl: string,
  walletAddress: string,
  token: Permit2TokenSymbol,
): () => Promise<bigint> {
  const provider = new JsonRpcProvider(rpcUrl);
  const contract = new Contract(
    PAYMENT_TOKENS[token].asset,
    ERC20_ALLOWANCE_ABI,
    provider,
  );
  const allowanceContract = {
    allowance: (owner: string, spender: string) => (
      contract.getFunction("allowance").staticCall(owner, spender)
    ),
  } as Permit2AllowanceContext["contract"];
  return () => readPermit2Allowance({
    token,
    walletAddress,
    rpcUrl,
    provider,
    contract: allowanceContract,
  });
}

const DEFAULT_PAID_PENDING_CREATE_DEPENDENCIES: PaidPendingCreateDependencies = {
  createPermit2AllowanceReader: createDefaultPermit2AllowanceReader,
  buildPaymentProof,
};

async function loadDefaultPaymentWallet(
  env: Readonly<Record<string, string | undefined>>,
): Promise<Wallet> {
  const password = env["WALLET_PASSWORD"] ?? "";
  if (!password) {
    throw new Error("WALLET_PASSWORD is required to create a paid job");
  }
  const configuredPath = env["KEYSTORE_PATH"] ?? "";
  if (!configuredPath) {
    throw new Error("KEYSTORE_PATH is required to create a paid job");
  }
  const keystorePath = resolve(MODULE_DIRECTORY, "..", configuredPath);
  return await Wallet.fromEncryptedJson(
    readFileSync(keystorePath, "utf8"),
    password,
  ) as Wallet;
}

const DEFAULT_ASYNC_STARTUP_DEPENDENCIES: AsyncStartupDependencies = {
  ...DEFAULT_PAID_PENDING_CREATE_DEPENDENCIES,
  fetch: globalThis.fetch,
  loadContext: () => buildTaskFromMemory(new GuardUserMemory()),
  loadWallet: loadDefaultPaymentWallet,
  log: console.log,
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function canonicalJson(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string" || typeof value === "boolean") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new Error("Pending x402 request is invalid");
    }
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    const items: string[] = [];
    for (let index = 0; index < value.length; index += 1) {
      if (!Object.prototype.hasOwnProperty.call(value, index)) {
        throw new Error("Pending x402 request is invalid");
      }
      items.push(canonicalJson(value[index]));
    }
    return `[${items.join(",")}]`;
  }
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`
    )).join(",")}}`;
  }
  throw new Error("Pending x402 request is invalid");
}

function pendingBindingMac(
  pending: PendingCreateRecord,
  jobId: string,
  jobToken: string,
): Buffer {
  if (
    !/^x402_[0-9a-f]{32}$/.test(jobId)
    || typeof jobToken !== "string"
    || jobToken.length === 0
    || jobToken.length > 4_096
    || /[\r\n]/.test(jobToken)
  ) {
    throw new Error("Pending x402 binding is invalid");
  }
  const parts = [
    "stockanalyst:x402-pending-binding:v1",
    jobId,
    pending.paymentProof,
    canonicalJson(pending.request),
  ].map((part) => Buffer.from(part, "utf8"));
  if (parts.some((part) => part.byteLength > MAX_PENDING_BYTES)) {
    throw new Error("Pending x402 binding is invalid");
  }
  const hmac = createHmac("sha256", Buffer.from(jobToken, "utf8"));
  for (const part of parts) {
    const length = Buffer.allocUnsafe(4);
    length.writeUInt32BE(part.byteLength);
    hmac.update(length);
    hmac.update(part);
  }
  return hmac.digest();
}

export function bindPendingToJob(
  pending: PendingCreateRecord,
  jobId: string,
  jobToken: string,
): PendingCreateRecord {
  const canonical = parsePendingCreate(pending);
  if (canonical.jobId !== undefined && canonical.jobId !== jobId) {
    throw new AsyncJobClientError("pending_job_mismatch");
  }
  const mac = pendingBindingMac(canonical, jobId, jobToken);
  return {
    ...canonical,
    jobId,
    binding: {
      version: 1,
      mac: mac.toString("base64url"),
    },
  };
}

function pendingBindingIsValid(
  pending: PendingCreateRecord,
  jobId: string,
  jobToken: string,
): boolean {
  if (
    pending.jobId !== jobId
    || pending.binding?.version !== 1
    || !/^[A-Za-z0-9_-]{43}$/.test(pending.binding.mac)
  ) {
    return false;
  }
  try {
    const supplied = Buffer.from(pending.binding.mac, "base64url");
    const expected = pendingBindingMac(pending, jobId, jobToken);
    return supplied.byteLength === expected.byteLength
      && timingSafeEqual(supplied, expected);
  } catch {
    return false;
  }
}

function parseStoredReceipt(value: unknown): AsyncJobReceipt {
  if (!isRecord(value)) throw new Error("Stored x402 receipt is invalid");
  const jobId = value["jobId"];
  const jobToken = value["jobToken"];
  const statusUrl = value["statusUrl"];
  const expiresAt = value["expiresAt"];
  if (
    typeof jobId !== "string"
    || !/^x402_[0-9a-f]{32}$/.test(jobId)
    || typeof jobToken !== "string"
    || jobToken.length === 0
    || jobToken.length > 4_096
    || /[\r\n]/.test(jobToken)
    || typeof statusUrl !== "string"
    || typeof expiresAt !== "number"
    || !Number.isSafeInteger(expiresAt)
    || expiresAt <= 0
  ) {
    throw new Error("Stored x402 receipt is invalid");
  }
  let canonicalStatusUrl: string;
  try {
    canonicalStatusUrl = canonicalStatusPath(undefined, jobId, statusUrl);
  } catch {
    throw new Error("Stored x402 receipt is invalid");
  }
  return {
    jobId,
    jobToken,
    status: "queued",
    statusUrl: canonicalStatusUrl,
    expiresAt,
  };
}

function durableDirectorySync(directory: string): void {
  const descriptor = openSync(directory, constants.O_RDONLY);
  try {
    fsyncSync(descriptor);
  } finally {
    closeSync(descriptor);
  }
}

function ensurePrivateDirectory(directory: string): void {
  const existed = existsSync(directory);
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  if (!existed) durableDirectorySync(dirname(directory));
}

function safeUnlink(path: string): void {
  try {
    unlinkSync(path);
  } catch (error) {
    if (
      !isRecord(error)
      || error["code"] !== "ENOENT"
    ) {
      throw error;
    }
  }
}

function durableUnlink(path: string): void {
  safeUnlink(path);
  durableDirectorySync(dirname(path));
}

function atomicPrivateJson(
  path: string,
  value: unknown,
  maximumBytes: number,
): void {
  const serialized = `${JSON.stringify(value)}\n`;
  if (Buffer.byteLength(serialized, "utf8") > maximumBytes) {
    throw new Error("Private x402 state is too large");
  }
  const directory = dirname(path);
  ensurePrivateDirectory(directory);
  const temporaryPath = join(
    directory,
    `.${basename(path)}.${process.pid}.${randomBytes(8).toString("hex")}.tmp`,
  );
  let descriptor: number | undefined;
  try {
    descriptor = openSync(
      temporaryPath,
      constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY,
      0o600,
    );
    writeFileSync(descriptor, serialized, "utf8");
    fsyncSync(descriptor);
    closeSync(descriptor);
    descriptor = undefined;
    renameSync(temporaryPath, path);
    durableDirectorySync(directory);
  } finally {
    if (descriptor !== undefined) closeSync(descriptor);
    safeUnlink(temporaryPath);
  }
}

export function cleanupPrivateStateTemps(
  directory: string,
  fileNames: string[],
): void {
  if (!existsSync(directory)) return;
  const patterns = fileNames.map((fileName) => {
    const escaped = fileName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return new RegExp(`^\\.${escaped}\\.\\d+\\.[0-9a-f]{16}\\.tmp$`);
  });
  let removed = false;
  for (const entry of readdirSync(directory)) {
    if (!patterns.some((pattern) => pattern.test(entry))) continue;
    safeUnlink(join(directory, entry));
    removed = true;
  }
  if (removed) durableDirectorySync(directory);
}

export function persistAsyncJobReceipt(
  receiptPath: string,
  receipt: AsyncJobReceipt,
): void {
  const candidate: StoredReceipt = {
    jobId: receipt.jobId,
    jobToken: receipt.jobToken,
    statusUrl: receipt.statusUrl,
    expiresAt: receipt.expiresAt,
  };
  const validated = parseStoredReceipt(candidate);
  const stored: StoredReceipt = {
    jobId: validated.jobId,
    jobToken: validated.jobToken,
    statusUrl: validated.statusUrl,
    expiresAt: validated.expiresAt,
  };
  atomicPrivateJson(receiptPath, stored, MAX_RECEIPT_BYTES);
}

export function loadAsyncJobReceipt(receiptPath: string): AsyncJobReceipt {
  if (statSync(receiptPath).size > MAX_RECEIPT_BYTES) {
    throw new Error("Stored x402 receipt is invalid");
  }
  try {
    return parseStoredReceipt(JSON.parse(readFileSync(receiptPath, "utf8")));
  } catch {
    throw new Error("Stored x402 receipt is invalid");
  }
}

function paymentProofExpiresAt(paymentProof: string): number {
  try {
    const proof = JSON.parse(
      Buffer.from(paymentProof, "base64").toString("utf8"),
    ) as unknown;
    if (!isRecord(proof)) throw new Error("invalid proof");
    const payload = proof["payload"];
    if (!isRecord(payload)) throw new Error("invalid proof");
    const accepted = proof["accepted"];
    const acceptedExtra = isRecord(accepted) ? accepted["extra"] : undefined;
    const permit2 = isRecord(acceptedExtra)
      && acceptedExtra["assetTransferMethod"] === "permit2-exact";
    const authorization = permit2
      ? payload["permit2Authorization"]
      : payload["authorization"];
    if (!isRecord(authorization)) throw new Error("invalid proof");
    const validBefore = permit2
      ? authorization["deadline"]
      : authorization["validBefore"];
    if (
      typeof validBefore !== "string"
      || !/^[1-9]\d{0,10}$/.test(validBefore)
    ) {
      throw new Error("invalid proof");
    }
    const milliseconds = Number(validBefore) * 1_000;
    if (!Number.isSafeInteger(milliseconds) || milliseconds <= 0) {
      throw new Error("invalid proof");
    }
    return milliseconds;
  } catch {
    throw new Error("Pending x402 payment proof is invalid");
  }
}

export function createPendingRecord(
  paymentProof: string,
  request: AsyncAnalysisRequest,
  now = Date.now(),
): PendingCreateRecord {
  if (!Number.isSafeInteger(now) || now <= 0 || !isRecord(request)) {
    throw new Error("Pending x402 request is invalid");
  }
  const proofExpiresAt = paymentProofExpiresAt(paymentProof);
  if (proofExpiresAt <= now) {
    throw new Error("Pending x402 payment proof is expired");
  }
  try {
    JSON.stringify(request);
  } catch {
    throw new Error("Pending x402 request is invalid");
  }
  return {
    version: 1,
    paymentProof,
    request,
    createdAt: now,
    proofExpiresAt,
    recoveryExpiresAt: now + PENDING_RECOVERY_MILLISECONDS,
  };
}

export async function preparePaidPendingCreate(
  wallet: Wallet,
  challenge: PaidPaymentChallenge,
  paymentToken: PaymentTokenSymbol,
  request: AsyncAnalysisRequest,
  env: Readonly<Record<string, string | undefined>> = process.env,
  dependencies: PaidPendingCreateDependencies = (
    DEFAULT_PAID_PENDING_CREATE_DEPENDENCIES
  ),
): Promise<PendingCreateRecord> {
  const access = paymentAccessFromChallenge(challenge);
  if (access.token !== paymentToken) {
    throw new Error("Payment challenge token does not match X402_PAYMENT_TOKEN");
  }
  await preflightPermit2Payment(wallet, paymentToken, env, dependencies);
  return buildPaidPendingCreate(
    wallet,
    challenge,
    paymentToken,
    request,
    dependencies,
  );
}

async function preflightPermit2Payment(
  wallet: Wallet,
  paymentToken: PaymentTokenSymbol,
  env: Readonly<Record<string, string | undefined>>,
  dependencies: PaidPendingCreateDependencies,
): Promise<void> {
  if (paymentToken !== "USDC" && paymentToken !== "USDT") return;
  let allowance: bigint;
  try {
    const readAllowance = dependencies.createPermit2AllowanceReader(
      env["BSC_RPC_URL"] ?? "",
      wallet.address,
      paymentToken,
    );
    allowance = await readAllowance();
    if (typeof allowance !== "bigint") throw new Error("invalid allowance");
  } catch {
    throw new Error(
      "Permit2 preflight failed; verify BSC_RPC_URL, BSC mainnet chain ID 56, "
        + `and the ${paymentToken} allowance`,
    );
  }
  assertPermit2PaymentReady(allowance, paymentToken);
}

async function buildPaidPendingCreate(
  wallet: Wallet,
  challenge: PaidPaymentChallenge,
  paymentToken: PaymentTokenSymbol,
  request: AsyncAnalysisRequest,
  dependencies: PaidPendingCreateDependencies,
): Promise<PendingCreateRecord> {
  const access = paymentAccessFromChallenge(challenge);
  if (access.token !== paymentToken) {
    throw new Error("Payment challenge token does not match X402_PAYMENT_TOKEN");
  }
  const proof = await dependencies.buildPaymentProof(wallet, challenge);
  return createPendingRecord(proof, request);
}

function parsePendingCreate(value: unknown): PendingCreateRecord {
  if (!isRecord(value)) throw new Error("Stored pending x402 request is invalid");
  const paymentProof = value["paymentProof"];
  const request = value["request"];
  const symbols = isRecord(request) ? request["symbols"] : undefined;
  const createdAt = value["createdAt"];
  const proofExpiresAt = value["proofExpiresAt"];
  const recoveryExpiresAt = value["recoveryExpiresAt"];
  const jobId = value["jobId"];
  const binding = value["binding"];
  const legacyPromotional = value["promotional"];
  if (
    value["version"] !== 1
    || typeof paymentProof !== "string"
    || paymentProof.length === 0
    || paymentProof.length > 128 * 1024
    || (legacyPromotional !== undefined && legacyPromotional !== false)
    || paymentProofContainsPromotionalAmount(paymentProof)
    || !isRecord(request)
    || !Array.isArray(symbols)
    || symbols.length === 0
    || !symbols.every((symbol) => typeof symbol === "string" && symbol.length > 0)
    || typeof createdAt !== "number"
    || !Number.isSafeInteger(createdAt)
    || createdAt <= 0
    || typeof proofExpiresAt !== "number"
    || !Number.isSafeInteger(proofExpiresAt)
    || proofExpiresAt !== paymentProofExpiresAt(paymentProof)
    || typeof recoveryExpiresAt !== "number"
    || !Number.isSafeInteger(recoveryExpiresAt)
    || recoveryExpiresAt !== createdAt + PENDING_RECOVERY_MILLISECONDS
    || (
      jobId !== undefined
      && (
        typeof jobId !== "string"
        || !/^x402_[0-9a-f]{32}$/.test(jobId)
      )
    )
    || (
      binding !== undefined
      && (
        !isRecord(binding)
        || binding["version"] !== 1
        || typeof binding["mac"] !== "string"
        || !/^[A-Za-z0-9_-]{43}$/.test(binding["mac"])
      )
    )
  ) {
    throw new Error("Stored pending x402 request is invalid");
  }
  return {
    version: 1,
    paymentProof,
    request: request as unknown as AsyncAnalysisRequest,
    createdAt,
    proofExpiresAt,
    recoveryExpiresAt,
    ...(jobId === undefined ? {} : { jobId }),
    ...(binding === undefined
      ? {}
      : {
        binding: {
          version: 1,
          mac: binding["mac"] as string,
        },
      }),
  };
}

function paymentProofContainsPromotionalAmount(paymentProof: string): boolean {
  try {
    const proof = JSON.parse(Buffer.from(paymentProof, "base64").toString("utf8"));
    return isRecord(proof)
      && isRecord(proof["accepted"])
      && proof["accepted"]["amount"] === "0";
  } catch {
    return false;
  }
}

function paymentAccessFromProof(paymentProof: string): X402PaymentAccess {
  try {
    const proof = JSON.parse(
      Buffer.from(paymentProof, "base64").toString("utf8"),
    ) as unknown;
    if (!isRecord(proof)) throw new Error("invalid proof");
    return paymentAccessFromAccepted(proof["accepted"]);
  } catch {
    throw new Error("Pending x402 access metadata is invalid");
  }
}

export function pendingAccessSummary(pending: PendingCreateRecord): string {
  const canonical = parsePendingCreate(pending);
  try {
    return formatX402AccessSummary(paymentAccessFromProof(canonical.paymentProof));
  } catch {
    if (isLegacyPaidProofForRecovery(canonical.paymentProof)) {
      return "Payment: legacy paid request (submitted)";
    }
    throw new Error("Pending x402 access metadata is invalid");
  }
}

function isLegacyPaidProofForRecovery(paymentProof: string): boolean {
  try {
    const proof = JSON.parse(
      Buffer.from(paymentProof, "base64").toString("utf8"),
    ) as unknown;
    if (!isRecord(proof) || !isRecord(proof["accepted"])) return false;
    const accepted = proof["accepted"];
    if (accepted["amount"] !== LEGACY_PAID_AMOUNT_FOR_RECOVERY) return false;
    paymentAccessFromAccepted({ ...accepted, amount: PAID_AMOUNT });
    return true;
  } catch {
    return false;
  }
}

export function persistPendingCreate(
  pendingPath: string,
  pending: PendingCreateRecord,
): void {
  atomicPrivateJson(
    pendingPath,
    parsePendingCreate(pending),
    MAX_PENDING_BYTES,
  );
}

export function loadPendingCreate(
  pendingPath: string,
): PendingCreateRecord {
  try {
    if (statSync(pendingPath).size > MAX_PENDING_BYTES) {
      throw new Error("oversized");
    }
    return parsePendingCreate(
      JSON.parse(readFileSync(pendingPath, "utf8")),
    );
  } catch {
    throw new Error("Stored pending x402 request is invalid");
  }
}

export function acquireExclusiveCliLock(lockPath: string): () => void {
  const directory = dirname(lockPath);
  ensurePrivateDirectory(directory);
  const token = randomBytes(32).toString("hex");
  const contents = `${JSON.stringify({
    version: 1,
    pid: process.pid,
    token,
    createdAt: Date.now(),
  })}\n`;
  let descriptor: number;
  try {
    descriptor = openSync(
      lockPath,
      constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY,
      0o600,
    );
  } catch (error) {
    if (isRecord(error) && error["code"] === "EEXIST") {
      throw new Error(
        "another x402 asynchronous client is active; "
        + "if it crashed, verify no client is running before removing the lock file",
      );
    }
    throw error;
  }
  let initializationError: unknown;
  try {
    writeFileSync(descriptor, contents, "utf8");
    fsyncSync(descriptor);
  } catch (error) {
    initializationError = error;
  } finally {
    closeSync(descriptor);
  }
  if (initializationError !== undefined) {
    safeUnlink(lockPath);
    durableDirectorySync(directory);
    throw initializationError;
  }
  durableDirectorySync(directory);

  let released = false;
  return () => {
    if (released) return;
    released = true;
    try {
      if (readFileSync(lockPath, "utf8") === contents) {
        durableUnlink(lockPath);
      }
    } catch (error) {
      if (!isRecord(error) || error["code"] !== "ENOENT") throw error;
    }
  };
}

export interface SignalCleanupHost {
  readonly pid: number;
  once(
    signal: "SIGINT" | "SIGTERM",
    listener: () => void,
  ): unknown;
  removeListener(
    signal: "SIGINT" | "SIGTERM",
    listener: () => void,
  ): unknown;
  kill(pid: number, signal: "SIGINT" | "SIGTERM"): unknown;
}

export function installGracefulLockCleanup(
  releaseLock: () => void,
  host: SignalCleanupHost = process,
): () => void {
  let listening = true;
  const removeHandlers = (): void => {
    if (!listening) return;
    listening = false;
    host.removeListener("SIGINT", onSigint);
    host.removeListener("SIGTERM", onSigterm);
  };
  const handleSignal = (signal: "SIGINT" | "SIGTERM"): void => {
    removeHandlers();
    try {
      releaseLock();
    } finally {
      // Re-raise after cleanup so the process retains normal signal semantics.
      host.kill(host.pid, signal);
    }
  };
  const onSigint = (): void => handleSignal("SIGINT");
  const onSigterm = (): void => handleSignal("SIGTERM");
  host.once("SIGINT", onSigint);
  host.once("SIGTERM", onSigterm);
  return removeHandlers;
}

export async function createReceiptFromPending(
  endpoint: string,
  pending: PendingCreateRecord,
  pendingPath: string,
  receiptPath: string,
  fetchImpl: FetchImpl = globalThis.fetch,
  options: PendingCreateOptions = {},
): Promise<AsyncJobReceipt> {
  const candidate = parsePendingCreate(pending);
  const validated = loadPendingCreate(pendingPath);
  if (JSON.stringify(candidate) !== JSON.stringify(validated)) {
    throw new AsyncJobClientError("pending_create_mismatch");
  }
  const now = options.now ?? Date.now;
  const sleep = options.sleep ?? (
    (milliseconds: number) => new Promise<void>(
      (resolveDelay) => setTimeout(resolveDelay, milliseconds),
    )
  );
  const requestedAttempts = options.maxAttempts ?? MAX_CREATE_ATTEMPTS;
  const baseRetryMilliseconds = (
    options.baseRetryMilliseconds ?? CREATE_RETRY_MILLISECONDS
  );
  if (
    !Number.isSafeInteger(requestedAttempts)
    || requestedAttempts < 1
    || requestedAttempts > 10
    || !Number.isSafeInteger(baseRetryMilliseconds)
    || baseRetryMilliseconds < 1
    || baseRetryMilliseconds > MAX_CREATE_RETRY_MILLISECONDS
  ) {
    throw new Error("Invalid pending-create retry configuration");
  }
  if (now() >= validated.recoveryExpiresAt) {
    throw new AsyncJobClientError("pending_create_expired");
  }
  const attempts = now() >= validated.proofExpiresAt
    ? 1
    : requestedAttempts;

  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const receipt = await createAsyncAnalysis(
        endpoint,
        validated.paymentProof,
        validated.request,
        fetchImpl,
        options,
      );
      if (
        options.expectedJobId !== undefined
        && receipt.jobId !== options.expectedJobId
      ) {
        throw new AsyncJobClientError("invalid_response");
      }
      if (
        validated.jobId !== undefined
        && validated.jobId !== receipt.jobId
      ) {
        throw new AsyncJobClientError("pending_job_mismatch");
      }
      if (
        validated.binding !== undefined
        && !pendingBindingIsValid(
          validated,
          receipt.jobId,
          receipt.jobToken,
        )
      ) {
        throw new AsyncJobClientError("pending_binding_invalid");
      }
      const boundPending = bindPendingToJob(
        validated,
        receipt.jobId,
        receipt.jobToken,
      );
      // The recovery proof must be durably bound before this receipt can
      // become the authoritative restart state.
      persistPendingCreate(pendingPath, boundPending);
      persistAsyncJobReceipt(receiptPath, receipt);
      return receipt;
    } catch (error) {
      if (
        !(error instanceof AsyncJobClientError)
        || !error.retryable
        || attempt + 1 >= attempts
      ) {
        throw error;
      }
      const current = now();
      const remaining = Math.min(
        validated.proofExpiresAt,
        validated.recoveryExpiresAt,
      ) - current;
      const backoff = Math.min(
        MAX_CREATE_RETRY_MILLISECONDS,
        baseRetryMilliseconds * 2 ** attempt,
      );
      if (remaining < backoff) throw error;
      await sleep(backoff);
    }
  }
  throw new AsyncJobClientError("network_error", { retryable: true });
}

export function quarantineRemoveOwnedPending(
  pendingPath: string,
  expected: PendingCreateRecord,
  jobId: string,
  jobToken: string,
  afterQuarantineRename?: () => void,
): void {
  let canonicalExpected: PendingCreateRecord;
  try {
    canonicalExpected = parsePendingCreate(expected);
    if (!pendingBindingIsValid(canonicalExpected, jobId, jobToken)) {
      throw new Error("pending job mismatch");
    }
  } catch {
    throw new AsyncJobClientError("pending_cleanup_failed");
  }

  const directory = dirname(pendingPath);
  const quarantinePath = join(
    directory,
    `.${basename(pendingPath)}.${process.pid}.`
      + `${randomBytes(8).toString("hex")}.quarantine`,
  );
  try {
    renameSync(pendingPath, quarantinePath);
    durableDirectorySync(directory);
  } catch (error) {
    if (isRecord(error) && error["code"] === "ENOENT") return;
    throw new AsyncJobClientError("pending_cleanup_failed");
  }

  try {
    afterQuarantineRename?.();
    const quarantined = loadPendingCreate(quarantinePath);
    if (
      !pendingBindingIsValid(quarantined, jobId, jobToken)
      || JSON.stringify(quarantined) !== JSON.stringify(canonicalExpected)
    ) {
      throw new Error("quarantined pending owner mismatch");
    }
    durableUnlink(quarantinePath);
    return;
  } catch {
    try {
      // A hard link is the portable no-replace primitive: it restores only
      // when the original path is absent and cannot overwrite a replacement.
      linkSync(quarantinePath, pendingPath);
      durableDirectorySync(directory);
    } catch {
      // If the destination exists or restoration otherwise fails, preserve
      // the quarantine for manual recovery and never touch the destination.
    }
    throw new AsyncJobClientError("pending_cleanup_failed");
  }
}

export async function pollAsyncAnalysisWithPendingRecovery(
  endpoint: string,
  receipt: AsyncJobReceipt,
  pendingPath: string,
  receiptPath: string,
  fetchImpl: FetchImpl = globalThis.fetch,
  pollOptions: PollOptions = {},
  createOptions: PendingCreateOptions = {},
): Promise<AsyncJobStatus> {
  let pending: PendingCreateRecord | undefined;
  try {
    pending = existsSync(pendingPath)
      ? loadPendingCreate(pendingPath)
      : undefined;
  } catch {
    throw new AsyncJobClientError("pending_binding_invalid");
  }
  if (pending !== undefined && pending.jobId !== receipt.jobId) {
    throw new AsyncJobClientError("pending_job_mismatch");
  }
  if (
    pending !== undefined
    && !pendingBindingIsValid(pending, receipt.jobId, receipt.jobToken)
  ) {
    throw new AsyncJobClientError("pending_binding_invalid");
  }
  return pollAsyncAnalysis(
    endpoint,
    receipt,
    fetchImpl,
    {
      ...pollOptions,
      recoverSettling: pending === undefined
        ? undefined
        : async (remainingMilliseconds) => createReceiptFromPending(
          endpoint,
          pending,
          pendingPath,
          receiptPath,
          fetchImpl,
          {
            ...createOptions,
            maxAttempts: 1,
            expectedJobId: receipt.jobId,
            requestTimeoutMilliseconds: Math.min(
              createOptions.requestTimeoutMilliseconds ?? 30_000,
              remainingMilliseconds,
            ),
          },
        ),
      onAuthoritativeNonSettling: pending === undefined
        ? undefined
        : () => quarantineRemoveOwnedPending(
          pendingPath,
          pending,
          receipt.jobId,
          receipt.jobToken,
        ),
    },
  );
}

export function pendingCreateFailureMessage(
  receiptPath: string,
  pendingPath: string,
): string {
  if (existsSync(receiptPath)) {
    return "The private job receipt was retained for a safe retry.";
  }
  if (existsSync(pendingPath)) {
    return "The pending payment request was retained; rerun to recover without paying again.";
  }
  return "No durable x402 retry state was created.";
}

function reportLabel(jobId: string): string {
  const suffix = jobId.slice("x402_".length);
  if (!/^[0-9a-f]{32}$/.test(suffix)) {
    throw new Error("Stored x402 receipt is invalid");
  }
  return BigInt(`0x${suffix}`).toString(10);
}

export async function finalizeAsyncReport(
  receiptPath: string,
  receipt: AsyncJobReceipt,
  reportMarkdown: string,
  symbols: string[],
  saveReportImpl: SaveReportImpl = saveReport,
): ReturnType<SaveReportImpl> {
  const saved = await saveReportImpl(
    reportMarkdown,
    reportLabel(receipt.jobId),
    symbols,
  );
  durableUnlink(receiptPath);
  return saved;
}

function pollingTimeout(): number {
  const raw = process.env["X402_POLL_TIMEOUT_MS"] ?? "1800000";
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error("X402_POLL_TIMEOUT_MS must be a positive integer");
  }
  return value;
}

export async function runAsyncStartup(
  options: AsyncStartupOptions,
): Promise<AsyncStartupResult> {
  const env = options.env ?? process.env;
  const dependencies: AsyncStartupDependencies = {
    ...DEFAULT_ASYNC_STARTUP_DEPENDENCIES,
    ...options.dependencies,
  };
  let receipt: AsyncJobReceipt | undefined;
  let symbols: string[];

  if (existsSync(options.receiptPath)) {
    receipt = loadAsyncJobReceipt(options.receiptPath);
    if (existsSync(options.pendingPath)) {
      const pending = loadPendingCreate(options.pendingPath);
      if (pending.jobId !== receipt.jobId) {
        throw new AsyncJobClientError("pending_job_mismatch");
      }
      if (!pendingBindingIsValid(
        pending,
        receipt.jobId,
        receipt.jobToken,
      )) {
        throw new AsyncJobClientError("pending_binding_invalid");
      }
      symbols = [...pending.request.symbols];
    } else {
      dependencies.log("Loading UOMP portfolio context for report metadata...");
      const context = await dependencies.loadContext();
      symbols = context.symbols;
    }
    dependencies.log(`Continuing asynchronous job ${receipt.jobId}`);
    return { receipt, symbols };
  }

  let pending: PendingCreateRecord | undefined;
  if (existsSync(options.pendingPath)) {
    pending = loadPendingCreate(options.pendingPath);
    symbols = [...pending.request.symbols];
    dependencies.log("Continuing the previously signed pending payment request...");
  } else {
    const sellerWallet = resolveX402SellerWallet(env);
    const paymentToken = resolveX402PaymentToken(env);
    dependencies.log("Loading UOMP portfolio context...");
    const context = await dependencies.loadContext();
    symbols = context.symbols;
    dependencies.log(`  Symbols: ${symbols.join(", ")}`);
    const request: AsyncAnalysisRequest = {
      symbols,
      analysis_type: "comprehensive",
      portfolio: context.portfolio,
      risk_profile: context.riskProfile,
    };
    let wallet: Wallet | undefined;
    if (paymentToken === "USDC" || paymentToken === "USDT") {
      wallet = await dependencies.loadWallet(env);
      dependencies.log("Checking Permit2 allowance before requesting analysis access...");
      await preflightPermit2Payment(
        wallet,
        paymentToken,
        env,
        dependencies,
      );
    }

    dependencies.log("Requesting asynchronous analysis access...");
    const initial = await beginAsyncAnalysis(
      options.endpoint,
      request,
      sellerWallet,
      dependencies.fetch,
      paymentToken,
    );
    wallet ??= await dependencies.loadWallet(env);
    dependencies.log(
      initial.challenge.accepted.extra.assetTransferMethod === "permit2-exact"
        ? "Signing one exact Permit2 payment authorization..."
        : "Signing one x402 EIP-3009 payment authorization...",
    );
    pending = await buildPaidPendingCreate(
      wallet,
      initial.challenge,
      paymentToken,
      request,
      dependencies,
    );
    // The proof must be durable before any POST that carries it.
    persistPendingCreate(options.pendingPath, pending);
  }

  if (pending !== undefined) {
    receipt = await createReceiptFromPending(
      options.endpoint,
      pending,
      options.pendingPath,
      options.receiptPath,
      dependencies.fetch,
    );
    dependencies.log(`Created asynchronous job ${receipt.jobId}`);
    dependencies.log(pendingAccessSummary(pending));
  }
  if (receipt === undefined) {
    throw new AsyncJobClientError("invalid_response");
  }
  return { receipt, symbols };
}

async function main(): Promise<void> {
  const releaseLock = acquireExclusiveCliLock(CLI_LOCK_PATH);
  const removeSignalHandlers = installGracefulLockCleanup(releaseLock);
  try {
    cleanupPrivateStateTemps(dirname(RECEIPT_PATH), [
      basename(RECEIPT_PATH),
      basename(PENDING_CREATE_PATH),
    ]);
    const { receipt, symbols } = await runAsyncStartup({
      endpoint: AGENT_ENDPOINT,
      receiptPath: RECEIPT_PATH,
      pendingPath: PENDING_CREATE_PATH,
    });

    console.log("Waiting for the private report; interrupted runs can be restarted safely...");
    const completed = await pollAsyncAnalysisWithPendingRecovery(
      AGENT_ENDPOINT,
      receipt,
      PENDING_CREATE_PATH,
      RECEIPT_PATH,
      globalThis.fetch,
      { timeoutMs: pollingTimeout() },
    );
    const reportMarkdown = await downloadAsyncReport(
      AGENT_ENDPOINT,
      receipt,
      completed,
    );
    const { htmlPath, pdfPath } = await finalizeAsyncReport(
      RECEIPT_PATH,
      receipt,
      reportMarkdown,
      symbols,
    );
    console.log(`Saved HTML report: ${htmlPath}`);
    if (pdfPath) console.log(`Saved PDF report: ${pdfPath}`);
    console.log("Asynchronous x402 flow complete.");
  } finally {
    removeSignalHandlers();
    releaseLock();
  }
}

const DEFAULT_ASYNC_CLI_PROCESS: AsyncCliProcess = {
  writeError: (message) => console.error(message),
  setExitCode: (code) => {
    process.exitCode = code;
  },
};

function formatAsyncCliError(error: unknown): string {
  try {
    if (error instanceof PaymentTokenUnavailableError) {
      const supplied = new Set(error.availableTokens);
      const available = (
        Object.keys(PAYMENT_TOKENS) as PaymentTokenSymbol[]
      ).filter((symbol) => supplied.has(symbol));
      if (available.length > 0) {
        return "x402 asynchronous job failed: payment_token_unavailable; "
          + `available tokens: ${available.join(", ")}`;
      }
      return "unexpected client failure";
    }
    if (error instanceof AsyncJobClientError) {
      const code = error.code;
      if (
        SAFE_ASYNC_JOB_CLIENT_ERROR_CODES.has(code)
        || /^(?:download_)?http_[1-5][0-9]{2}$/.test(code)
      ) {
        return `x402 asynchronous job failed: ${code}`;
      }
      return "unexpected client failure";
    }
    if (error instanceof Error) {
      const message = error.message;
      if (SAFE_ASYNC_CLI_ERROR_MESSAGES.has(message)) return message;
    }
  } catch {
    // A hostile error getter must not alter or inject output.
  }
  return "unexpected client failure";
}

function formatAsyncRecoveryMessage(
  recoveryMessage: () => string,
): string {
  try {
    const message = recoveryMessage();
    if (SAFE_ASYNC_RECOVERY_MESSAGES.has(message)) return message;
  } catch {
    // Recovery classification is advisory; keep output fixed on failure.
  }
  return "Private x402 retry state could not be safely classified.";
}

export async function runAsyncCliMain(
  operation: () => Promise<void> = main,
  cliProcess: AsyncCliProcess = DEFAULT_ASYNC_CLI_PROCESS,
  recoveryMessage: () => string = () => pendingCreateFailureMessage(
    RECEIPT_PATH,
    PENDING_CREATE_PATH,
  ),
): Promise<void> {
  try {
    await operation();
  } catch (error: unknown) {
    cliProcess.writeError(
      `x402 asynchronous flow failed: ${formatAsyncCliError(error)}`,
    );
    cliProcess.writeError(formatAsyncRecoveryMessage(recoveryMessage));
    cliProcess.setExitCode(1);
  }
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : "";
if (invokedPath === fileURLToPath(import.meta.url)) {
  void runAsyncCliMain();
}
