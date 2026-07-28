#!/usr/bin/env node
/**
 * ERC-8183 buyer client — uses UOMP user context to drive the full commerce flow.
 *
 * UOMP (User-Owned Memory Protocol) provides the user's portfolio preferences.
 * ERC-8183 (AgenticCommerce) handles the on-chain escrow and payment release.
 *
 * Flow:
 *   1. Load portfolio context from UOMP memory store
 *   2. A2A negotiate  — get signed price quote from agent
 *   3. On-chain buy   — createJob → registerJob → setBudget → approve → fund
 *   4. notify_funded  — tell agent to start LLM work
 *   5. Poll chain     — wait for SUBMITTED status
 *   6. Fetch report   — download deliverable from IPFS/storage
 *   7. Settle         — approve job, release escrow to seller
 *
 * Setup:
 *   cp .env.example .env
 *   # Edit .env: set KEYSTORE_PATH, WALLET_PASSWORD, AGENT_ENDPOINT
 *   npm install
 *   npm run dev
 */

import { readFileSync } from "fs";
import { resolve } from "path";
import { Wallet, type BaseWallet } from "ethers";
import { GuardUserMemory, buildTaskFromMemory } from "./uomp.js";
import {
  negotiate,
  buildJobDescription,
  notifyFunded,
  resolveMaxBudgetWei,
  assertQuoteWithinBudget,
} from "./negotiate.js";
import { CONTRACTS, ERC8183Buyer } from "./erc8183.js";
import {
  fetchDeliverable,
  shouldUseBuyerRelay,
  startGatewayRelay,
  type GatewayRelay,
} from "./gateway.js";
import { saveReport } from "./pdf-report.js";
import { completeSubmittedJob, SettlementAttemptError } from "./completion.js";

// ── Config from environment ──────────────────────────────────────────────────
const KEYSTORE_PATH  = process.env["KEYSTORE_PATH"]  ?? "../stockanalyst/.studio/wallets/0x1FF095E1C5Cf4bC72a3DC54be17B6cf85043Fb67.json";
const WALLET_PASSWORD = process.env["WALLET_PASSWORD"] ?? "";
const AGENT_ENDPOINT  = process.env["AGENT_ENDPOINT"]  ?? "http://localhost:9000";
const PROVIDER_ADDRESS = process.env["PROVIDER_ADDRESS"] ?? "0x1FF095E1C5Cf4bC72a3DC54be17B6cf85043Fb67";
const DELIVERY_MODE = process.env["DELIVERY_MODE"];

const POLL_INTERVAL_MS = 15_000;
const POLL_TIMEOUT_MS  = 1_800_000;

// Module-level relay handle so the process-exit handler can always close it.
let relay: GatewayRelay | undefined;

function hr(label: string): void {
  console.log(`\n${"─".repeat(60)}`);
  console.log(`  ${label}`);
  console.log("─".repeat(60));
}

function banner(lines: string[]): void {
  console.log("\n" + "═".repeat(60));
  for (const l of lines) console.log(`  ${l}`);
  console.log("═".repeat(60));
}

async function main(): Promise<void> {
  // ── Load wallet from encrypted keystore ─────────────────────────────────
  if (!WALLET_PASSWORD) {
    console.error("ERROR: WALLET_PASSWORD is required. Set it in .env");
    process.exit(1);
  }

  const keystorePath = resolve(import.meta.dirname, "..", KEYSTORE_PATH);
  const keystoreJson = readFileSync(keystorePath, "utf8");
  console.log("Decrypting keystore...");
  const wallet = await Wallet.fromEncryptedJson(keystoreJson, WALLET_PASSWORD) as BaseWallet;

  const buyer = new ERC8183Buyer(wallet);

  // ── Start UOMP payload relay (reverse gateway for report delivery) ────────
  // The seller uploads the report here; Cloudflare Tunnel exposes it publicly
  // so the seller can reach it even when the buyer has no public IP.
  if (shouldUseBuyerRelay(DELIVERY_MODE)) {
    hr("UOMP Gateway: starting payload relay + Cloudflare Tunnel");
    try {
      relay = await startGatewayRelay();
      console.log(`  publicUrl:   ${relay.publicUrl}`);
      console.log(`  localUrl:    ${relay.localUrl}`);
    } catch (err: unknown) {
      console.log(`  ⚠  Failed to start relay: ${err instanceof Error ? err.message : err}`);
      console.log("  Continuing without UOMP delivery (seller will use local storage).");
    }
  } else {
    hr("UOMP Gateway: external storage delivery");
    console.log("  External storage delivery is enabled; buyer relay is disabled.");
  }

  // ── Step 0: Pre-flight balance check ────────────────────────────────────
  const tBnb = await buyer.tBnbBalance();
  const uBal = await buyer.uBalance();

  banner([
    "Stock Analysis Agent — UOMP Buyer Client",
    `Agent:    ${AGENT_ENDPOINT}`,
    `Provider: ${PROVIDER_ADDRESS}`,
    `Buyer:    ${buyer.address}`,
    `Balance:  ${Number(tBnb).toFixed(4)} tBNB  |  ${Number(uBal).toFixed(4)} U`,
    "Network:  BSC Testnet (chain 97)",
  ]);

  if (Number(tBnb) < 0.001) {
    console.error("ERROR: Insufficient tBNB for gas. Faucet: https://testnet.bnbchain.org/faucet-smart");
    process.exit(1);
  }
  if (Number(uBal) < 1) {
    console.error("ERROR: Insufficient U balance (need ≥ 1 U). Faucet: https://united-coin-u.github.io/u-faucet/");
    process.exit(1);
  }

  // ── Step 1: Load UOMP portfolio context ─────────────────────────────────
  hr("Step 1: Load UOMP user context (portfolio + risk profile)");
  const guardUrl = process.env["UOMP_GUARD_URL"] ?? "http://127.0.0.1:9374";
  const memory = new GuardUserMemory();

  const { symbols, task, deliverables, quality, portfolio, riskProfile } = await buildTaskFromMemory(memory);
  console.log(`  ✓ Symbols:     ${symbols.join(", ")}`);
  console.log(`  ✓ Task:        ${task}`);
  console.log(`  ✓ Risk:        ${riskProfile.tolerance} / ${riskProfile.horizonMonths}mo`);
  console.log(`  ✓ Source:      UOMP Guard at ${guardUrl}`);

  // ── Step 2: Negotiate ────────────────────────────────────────────────────
  hr("Step 2: Negotiate — get signed price quote from agent");
  const envelope = await negotiate(AGENT_ENDPOINT, task, deliverables, quality);
  const priceRaw = BigInt(envelope.response.terms.price);
  // Refuse to fund a quote above the client-side spend ceiling BEFORE any
  // on-chain spend — the seller signs its own price and could quote anything.
  assertQuoteWithinBudget(priceRaw, resolveMaxBudgetWei());
  const priceU   = Number(priceRaw) / 1e18;
  console.log(`  ✓ Accepted     price=${priceU} U`);
  console.log(`  ✓ Estimated    completion=${envelope.response.estimated_completion_seconds}s`);
  const hash = (envelope.negotiation_hash ?? envelope.response.negotiation_hash ?? "").slice(0, 22);
  console.log(`  ✓ Quote hash   ${hash}...`);

  const description = buildJobDescription(envelope);

  // ── Step 3: On-chain buy ─────────────────────────────────────────────────
  hr("Step 3: On-chain — createJob → registerJob → setBudget → approve → fund");
  const buy = await buyer.buy({
    provider: PROVIDER_ADDRESS,
    description,
    budgetU: String(priceU),
  });

  console.log(`\n  ✓ Job ID       ${buy.jobId}`);
  console.log(`  ✓ create_tx    ${buy.createTx}`);
  console.log(`  ✓ fund_tx      ${buy.fundTx}`);
  console.log(`  ✓ budget       ${buy.budgetU} U`);

  // ── Step 4: notify_funded ────────────────────────────────────────────────
  hr(`Step 4: notify_funded — tell agent job #${buy.jobId} is funded`);
  const notifyStatus = await notifyFunded(AGENT_ENDPOINT, wallet, buy.jobId, {
    gatewayUrl:   relay?.publicUrl,
    gatewayToken: relay?.token,
    portfolio,
    riskProfile,
  });
  if (relay) {
    console.log(`  ✓ Gateway URL  ${relay.publicUrl}`);
  }
  console.log(`  ✓ Agent ACK    status=${notifyStatus}`);

  // ── Step 5: Poll for SUBMITTED ───────────────────────────────────────────
  hr(`Step 5: Poll on-chain until job #${buy.jobId} reaches SUBMITTED`);
  const finalStatus = await buyer.pollUntilSubmitted(buy.jobId, {
    intervalMs: POLL_INTERVAL_MS,
    timeoutMs:  POLL_TIMEOUT_MS,
  });
  console.log(`  ✓ Job reached  ${finalStatus}`);

  // ── Step 6: Verify deliverable and settle ─────────────────────────────────
  hr("Step 6: Verify deliverable, then settle escrow");
  try {
    const completion = await completeSubmittedJob(
      {
        jobId: buy.jobId,
        fundTxBlock: buy.fundTxBlock,
        chainId: BigInt(CONTRACTS.CHAIN_ID),
        contracts: {
          commerce: CONTRACTS.COMMERCE,
          router: CONTRACTS.ROUTER,
          policy: CONTRACTS.POLICY,
        },
      },
      {
        getDeliverableUrl: (jobId, block) => buyer.getDeliverableUrl(jobId, block),
        getDeliverableCommitment: (jobId) => buyer.getDeliverableCommitment(jobId),
        fetchDeliverable: (url) => fetchDeliverable(url, relay),
        renderReport: async (reportText) => {
          console.log("\n┌─ VERIFIED REPORT " + "─".repeat(41) + "┐");
          for (const line of reportText.split("\n")) console.log(`│ ${line}`);
          console.log("└" + "─".repeat(58) + "┘");
          const result = await saveReport(reportText, buy.jobId.toString(), symbols);
          console.log(`  ✓ HTML report  ${result.htmlPath}`);
          if (result.pdfPath) console.log(`  ✓ PDF report   ${result.pdfPath}`);
        },
        settle: (jobId) => buyer.settle(jobId),
      },
    );
    if (completion.renderError) {
      console.log(`  ⚠  Report save failed: ${completion.renderError}`);
    }
    console.log(`  ✓ settle_tx    ${completion.settleTx}`);
    banner([
      `✓ E2E PASSED — job #${buy.jobId} settled on BSC testnet`,
      `  Seller received: ${priceU} U`,
    ]);
  } catch (err: unknown) {
    // 0x17be5b7b = DisputeWindowActive — settle requires waiting 24h after submission
    if (err instanceof SettlementAttemptError) {
      const cause = err.cause;
      const msg = cause instanceof Error ? cause.message : String(cause);
      if (msg.includes("17be5b7b") || msg.includes("DisputeWindow")) {
        console.log("  ⚠  Dispute window still active (24h on BSC testnet).");
        console.log("     Run settle manually after the window elapses:");
        console.log(`     bag erc8183 settle ${buy.jobId}`);
        banner([
          `✓ E2E PASSED — job #${buy.jobId} SUBMITTED (settle pending dispute window)`,
          `  Run: bag erc8183 settle ${buy.jobId}  (available after ~24h)`,
        ]);
        return;
      }
    }
    throw err;
  }
}

main()
  .catch((err: Error) => {
    console.error("\n✗ FAILED:", err.message);
    process.exit(1);
  })
  .finally(() => {
    relay?.close();
  });
