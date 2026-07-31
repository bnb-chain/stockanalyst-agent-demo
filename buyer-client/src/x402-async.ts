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
import { Wallet } from "ethers";
import { saveReport } from "./pdf-report.js";
import { GuardUserMemory, buildTaskFromMemory } from "./uomp.js";
import {
  createAsyncAnalysis,
  canonicalStatusPath,
  downloadAsyncReport,
  fetchPaymentChallenge,
  pollAsyncAnalysis,
  AsyncJobClientError,
  type AsyncAnalysisRequest,
  type AsyncJobReceipt,
  type AsyncJobStatus,
  type CreateRequestOptions,
  type FetchImpl,
  type PollOptions,
  type SleepImpl,
} from "./x402-async-client.js";
import {
  U_TOKEN_ADDRESS,
  buildPaymentProof,
  resolveX402SellerWallet,
} from "./x402-payment.js";

const MODULE_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const KEYSTORE_PATH = process.env["KEYSTORE_PATH"] ?? "";
const WALLET_PASSWORD = process.env["WALLET_PASSWORD"] ?? "";
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
    const authorization = payload["authorization"];
    if (!isRecord(authorization)) throw new Error("invalid proof");
    const validBefore = authorization["validBefore"];
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
  if (
    value["version"] !== 1
    || typeof paymentProof !== "string"
    || paymentProof.length === 0
    || paymentProof.length > 128 * 1024
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

async function main(): Promise<void> {
  const releaseLock = acquireExclusiveCliLock(CLI_LOCK_PATH);
  const removeSignalHandlers = installGracefulLockCleanup(releaseLock);
  try {
    cleanupPrivateStateTemps(dirname(RECEIPT_PATH), [
      basename(RECEIPT_PATH),
      basename(PENDING_CREATE_PATH),
    ]);
    const sellerWallet = resolveX402SellerWallet();
    let receipt: AsyncJobReceipt;
    let symbols: string[];

    if (existsSync(RECEIPT_PATH)) {
      receipt = loadAsyncJobReceipt(RECEIPT_PATH);
      if (existsSync(PENDING_CREATE_PATH)) {
        const pending = loadPendingCreate(PENDING_CREATE_PATH);
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
        console.log("Loading UOMP portfolio context for report metadata...");
        const context = await buildTaskFromMemory(new GuardUserMemory());
        symbols = context.symbols;
      }
      console.log(`Continuing asynchronous job ${receipt.jobId}`);
    } else {
      let pending: PendingCreateRecord;
      if (existsSync(PENDING_CREATE_PATH)) {
        pending = loadPendingCreate(PENDING_CREATE_PATH);
        symbols = [...pending.request.symbols];
        console.log("Continuing the previously signed pending payment request...");
      } else {
        if (!WALLET_PASSWORD) {
          throw new Error("WALLET_PASSWORD is required to create a new job");
        }
        if (!KEYSTORE_PATH) {
          throw new Error("KEYSTORE_PATH is required to create a new job");
        }
        console.log("Loading UOMP portfolio context...");
        const context = await buildTaskFromMemory(new GuardUserMemory());
        symbols = context.symbols;
        console.log(`  Symbols: ${symbols.join(", ")}`);
        const request: AsyncAnalysisRequest = {
          symbols,
          analysis_type: "comprehensive",
          portfolio: context.portfolio,
          risk_profile: context.riskProfile,
        };
        console.log("Fetching the current B402 payment requirement...");
        const challenge = await fetchPaymentChallenge(
          AGENT_ENDPOINT,
          request,
          sellerWallet,
        );
        const keystorePath = resolve(MODULE_DIRECTORY, "..", KEYSTORE_PATH);
        const wallet = await Wallet.fromEncryptedJson(
          readFileSync(keystorePath, "utf8"),
          WALLET_PASSWORD,
        ) as Wallet;
        console.log("Signing one x402 EIP-3009 payment authorization...");
        const proof = await buildPaymentProof(wallet, challenge);
        pending = createPendingRecord(
          proof,
          request,
        );
        // This durable record must exist before the first payment POST.
        persistPendingCreate(PENDING_CREATE_PATH, pending);
      }
      receipt = await createReceiptFromPending(
        AGENT_ENDPOINT,
        pending,
        PENDING_CREATE_PATH,
        RECEIPT_PATH,
      );
      console.log(`Created asynchronous job ${receipt.jobId}`);
      console.log(`Payment: 1.0 U → ${sellerWallet}`);
      console.log(`Token contract: ${U_TOKEN_ADDRESS}`);
    }

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

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : "";
if (invokedPath === fileURLToPath(import.meta.url)) {
  main().catch((error: unknown) => {
    const message = error instanceof Error ? error.message : "unknown error";
    console.error(`x402 asynchronous flow failed: ${message}`);
    console.error(
      pendingCreateFailureMessage(RECEIPT_PATH, PENDING_CREATE_PATH),
    );
    process.exitCode = 1;
  });
}
