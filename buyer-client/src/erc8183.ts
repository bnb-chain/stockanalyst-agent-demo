/**
 * ERC-8183 on-chain buyer operations (BSC testnet).
 *
 * Job lifecycle:
 *   createJob → registerJob (bind policy) → setBudget →
 *   ERC-20 approve → fund → [agent works] → settle
 */

import { Contract, JsonRpcProvider, Wallet, type BaseWallet, parseUnits, formatUnits } from "ethers";
import { COMMERCE_ABI } from "./abi/commerce.js";
import { ROUTER_ABI } from "./abi/router.js";
import { POLICY_ABI } from "./abi/policy.js";
import { ERC20_ABI } from "./abi/erc20.js";

const DEFAULT_RPC_URL = "https://data-seed-prebsc-2-s2.binance.org:8545";

function httpUrl(value: string | undefined, variable: string): string {
  if (!value) {
    throw new Error(`${variable} is required`);
  }
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new Error("unsupported protocol");
    }
    return parsed.toString();
  } catch {
    throw new Error(`${variable} must be a valid HTTP(S) URL`);
  }
}

export function resolveRpcUrls(
  env: Readonly<Record<string, string | undefined>> = process.env,
): { rpcUrl: string; logRpcUrl: string } {
  return {
    rpcUrl: httpUrl(env["BSC_RPC_URL"] ?? DEFAULT_RPC_URL, "BSC_RPC_URL"),
    logRpcUrl: httpUrl(env["BSC_LOG_RPC_URL"], "BSC_LOG_RPC_URL"),
  };
}

// ── BSC Testnet contract addresses ──────────────────────────────────────────
export const CONTRACTS = {
  CHAIN_ID: 97,
  COMMERCE: "0xa206c0517b6371c6638cd9e4a42cc9f02a33b0de",
  ROUTER:   "0xd7d36d66d2f1b608a0f943f722d27e3744f66f25",
  POLICY:   "0x4f4678d4439fec812ac7674bb3efb4c8f5fb78a6",
  U_TOKEN:  "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565",
} as const;

// JobStatus enum (matches IACP.JobStatus in Solidity)
export const JobStatus: Record<number, string> = {
  0: "OPEN",
  1: "FUNDED",
  2: "SUBMITTED",
  3: "COMPLETED",
  4: "REJECTED",
  5: "EXPIRED",
};

export interface BuyResult {
  jobId: bigint;
  createTx: string;
  registerTx: string;
  setBudgetTx: string;
  approveTx: string;
  fundTx: string;
  fundTxBlock: number;
  budgetU: string;
}

export class ERC8183Buyer {
  private provider: JsonRpcProvider;
  private logProvider: JsonRpcProvider;
  private wallet: BaseWallet;
  private commerce: Contract;
  private commerceLog: Contract;
  private router: Contract;
  private policy: Contract;
  private policyLog: Contract;
  private uToken: Contract;

  constructor(walletOrKey: BaseWallet | string) {
    const { rpcUrl, logRpcUrl } = resolveRpcUrls();
    this.provider = new JsonRpcProvider(rpcUrl, {
      chainId: CONTRACTS.CHAIN_ID,
      name: "bsc-testnet",
    });
    // data-seed nodes block eth_getLogs; use a separate archive endpoint for
    // event queries so deliverable URL lookups work.
    this.logProvider = new JsonRpcProvider(logRpcUrl, {
      chainId: CONTRACTS.CHAIN_ID,
      name: "bsc-testnet",
    });
    this.wallet =
      typeof walletOrKey === "string"
        ? new Wallet(walletOrKey, this.provider)
        : walletOrKey.connect(this.provider);

    this.commerce    = new Contract(CONTRACTS.COMMERCE, COMMERCE_ABI, this.wallet);
    this.commerceLog = new Contract(CONTRACTS.COMMERCE, COMMERCE_ABI, this.logProvider);
    this.router      = new Contract(CONTRACTS.ROUTER, ROUTER_ABI, this.wallet);
    this.policy      = new Contract(CONTRACTS.POLICY, POLICY_ABI, this.provider);
    this.policyLog   = new Contract(CONTRACTS.POLICY, POLICY_ABI, this.logProvider);
    this.uToken      = new Contract(CONTRACTS.U_TOKEN, ERC20_ABI, this.wallet);
  }

  get address(): string {
    return this.wallet.address;
  }

  async uBalance(): Promise<string> {
    const raw = await this.uToken.balanceOf(this.wallet.address) as bigint;
    return formatUnits(raw, 18);
  }

  async tBnbBalance(): Promise<string> {
    const raw = await this.provider.getBalance(this.wallet.address);
    return formatUnits(raw, 18);
  }

  async disputeWindow(): Promise<bigint> {
    return await this.policy.disputeWindow() as bigint;
  }

  /**
   * Full buyer flow: createJob → registerJob → setBudget → approve → fund.
   *
   * expiredAt = now + disputeWindow + deadlineSeconds
   * (seller's submission deadline is expiredAt - disputeWindow, so we need
   *  enough room for the seller to submit AND the dispute window to close).
   */
  async buy(params: {
    provider: string;
    description: string;
    budgetU: string;
    deadlineSeconds?: number;
  }): Promise<BuyResult> {
    const { provider, description, budgetU, deadlineSeconds = 7200 } = params;

    const rawBudget = parseUnits(budgetU, 18);
    const disputeWindowSec = await this.disputeWindow();
    const expiredAt = BigInt(Math.floor(Date.now() / 1000)) + disputeWindowSec + BigInt(deadlineSeconds);

    // 1. createJob — evaluator = Router (standard v1 pattern), hook = Router
    console.log("  [1/5] createJob...");
    const createTx = await this.commerce.createJob(
      provider,
      CONTRACTS.ROUTER,   // evaluator
      expiredAt,
      description,
      CONTRACTS.ROUTER,   // hook
    );
    const createReceipt = await createTx.wait();
    const createTxHash = createReceipt.hash as string;

    // Parse jobId from transaction logs
    const jobId = await this.parseJobId(createTxHash);
    console.log(`  [1/5] ✓ Job #${jobId} created  tx=${createTxHash.slice(0, 20)}...`);

    // 2. registerJob — bind OptimisticPolicy to this job on the Router
    console.log("  [2/5] registerJob (bind policy)...");
    const regTx = await this.router.registerJob(jobId, CONTRACTS.POLICY);
    const regReceipt = await regTx.wait();
    const registerTxHash = regReceipt.hash as string;
    console.log(`  [2/5] ✓ Registered  tx=${registerTxHash.slice(0, 20)}...`);

    // 3. setBudget
    console.log("  [3/5] setBudget...");
    const budgetTx = await this.commerce.setBudget(jobId, rawBudget, "0x");
    const budgetReceipt = await budgetTx.wait();
    const setBudgetTxHash = budgetReceipt.hash as string;
    console.log(`  [3/5] ✓ Budget set ${budgetU} U  tx=${setBudgetTxHash.slice(0, 20)}...`);

    // 4. ERC-20 approve (U token → Commerce contract)
    console.log("  [4/5] approve U token...");
    const approveTx = await this.uToken.approve(CONTRACTS.COMMERCE, rawBudget);
    const approveReceipt = await approveTx.wait();
    const approveTxHash = approveReceipt.hash as string;
    console.log(`  [4/5] ✓ Approved  tx=${approveTxHash.slice(0, 20)}...`);

    // 5. fund — deposits rawBudget into escrow
    console.log("  [5/5] fund (escrow deposit)...");
    const fundTx = await this.commerce.fund(jobId, rawBudget, "0x");
    const fundReceipt = await fundTx.wait();
    const fundTxHash = fundReceipt.hash as string;
    console.log(`  [5/5] ✓ Funded  tx=${fundTxHash.slice(0, 20)}...`);

    return {
      jobId,
      createTx: createTxHash,
      registerTx: registerTxHash,
      setBudgetTx: setBudgetTxHash,
      approveTx: approveTxHash,
      fundTx: fundTxHash,
      fundTxBlock: fundReceipt.blockNumber,
      budgetU,
    };
  }

  async getJobStatus(jobId: bigint): Promise<{ status: string; statusCode: number }> {
    const job = await this.commerce.getJob(jobId) as { status: number };
    const statusCode = Number(job.status);
    return { status: JobStatus[statusCode] ?? "UNKNOWN", statusCode };
  }

  async getDeliverableCommitment(jobId: bigint): Promise<string> {
    const job = await this.commerce.getJob(jobId) as { deliverable?: unknown };
    if (typeof job.deliverable !== "string" || !/^0x[0-9a-fA-F]{64}$/.test(job.deliverable)) {
      throw new Error("On-chain job has no valid deliverable commitment");
    }
    return job.deliverable;
  }

  /**
   * Poll until job reaches SUBMITTED (or a terminal error status).
   * Returns the final status string.
   */
  async pollUntilSubmitted(
    jobId: bigint,
    options: { intervalMs?: number; timeoutMs?: number } = {}
  ): Promise<string> {
    const { intervalMs = 15_000, timeoutMs = 600_000 } = options;
    const deadline = Date.now() + timeoutMs;
    let elapsed = 0;

    while (Date.now() < deadline) {
      const { status, statusCode } = await this.getJobStatus(jobId);
      console.log(`  [${String(elapsed).padStart(4)}s] status=${status}`);

      if (statusCode === 2 /* SUBMITTED */ || statusCode === 3 /* COMPLETED */) {
        return status;
      }
      if (statusCode === 4 /* REJECTED */ || statusCode === 5 /* EXPIRED */) {
        throw new Error(`Job ended with terminal status: ${status}`);
      }

      await sleep(intervalMs);
      elapsed += Math.round(intervalMs / 1000);
    }

    throw new Error(`Timed out after ${timeoutMs / 1000}s waiting for SUBMITTED`);
  }

  /**
   * Fetch the deliverable URL from the OptimisticPolicy's JobInitialised event.
   * Scans Commerce's JobSubmitted event to find the submit block (using the
   * archive log provider), then reads Policy's JobInitialised event nearby.
   *
   * fundTxBlock: the block where the fund tx was mined (from BuyResult). Used
   * to anchor the scan so we only look from fund time forward — keeps the
   * window small and avoids hitting RPC range limits.
   */
  async getDeliverableUrl(jobId: bigint, fundTxBlock?: number): Promise<string | null> {
    const currentBlock = await this.logProvider.getBlockNumber();

    // Scan Commerce for JobSubmitted using the archive RPC.
    const submitBlock = await this.findSubmitBlock(jobId, currentBlock, fundTxBlock);
    if (submitBlock === null) return null;

    // Scan Policy's JobInitialised event around the submit block (±20 blocks).
    const fromBlock = Math.max(0, submitBlock - 20);
    const toBlock = submitBlock + 20;

    const filter = this.policyLog.filters["JobInitialised(uint256,bytes32,uint64,bytes)"](jobId);
    let logs;
    try {
      logs = await this.policyLog.queryFilter(filter, fromBlock, toBlock);
    } catch {
      return null;
    }
    if (logs.length === 0) return null;

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const optParamsHex: string = (logs[0] as any).args?.optParams ?? "";
    if (!optParamsHex) return null;

    try {
      // optParams is returned as a hex string by ethers
      const hex = optParamsHex.startsWith("0x") ? optParamsHex.slice(2) : optParamsHex;
      const bytes = Buffer.from(hex, "hex");
      const json = bytes.toString("utf8");
      const parsed: unknown = JSON.parse(json);
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        return null;
      }
      const deliverableUrl = (parsed as Record<string, unknown>)["deliverable_url"];
      return typeof deliverableUrl === "string" ? deliverableUrl : null;
    } catch {
      return null;
    }
  }

  /** Approve escrow release to the seller. Callable permissionlessly once SUBMITTED. */
  async settle(jobId: bigint): Promise<string> {
    const tx = await this.router.settle(jobId, "0x");
    const receipt = await tx.wait();
    return receipt.hash as string;
  }

  private async findSubmitBlock(
    jobId: bigint,
    currentBlock: number,
    fromBlockHint?: number,
  ): Promise<number | null> {
    const filter = this.commerceLog.filters["JobSubmitted(uint256,address,bytes32)"](jobId);
    // If we have the fund block we can search forward from it (tight window).
    // Without a hint, fall back to scanning 5000 blocks back from current head.
    const fromBlock = fromBlockHint !== undefined
      ? fromBlockHint
      : Math.max(0, currentBlock - 5000);
    try {
      const logs = await this.commerceLog.queryFilter(filter, fromBlock, currentBlock);
      if (logs.length > 0) return logs[0].blockNumber;
    } catch {
      // archive RPC unreachable — caller gets null
    }
    return null;
  }

  private async parseJobId(txHash: string): Promise<bigint> {
    const receipt = await this.provider.getTransactionReceipt(txHash);
    if (!receipt) throw new Error(`No receipt for tx ${txHash}`);

    // Decode JobCreated event directly from the receipt's raw logs — no eth_getLogs call.
    for (const log of receipt.logs) {
      if (log.address.toLowerCase() !== CONTRACTS.COMMERCE.toLowerCase()) continue;
      try {
        const parsed = this.commerce.interface.parseLog({
          topics: log.topics as string[],
          data: log.data,
        });
        if (parsed && parsed.name === "JobCreated") {
          return parsed.args.jobId as bigint;
        }
      } catch {
        // not this event, continue
      }
    }

    throw new Error("JobCreated event not found in transaction receipt logs");
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
