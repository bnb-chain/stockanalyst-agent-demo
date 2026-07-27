#!/usr/bin/env node
/**
 * x402 / Binance Pay buyer — simpler alternative to the ERC-8183 flow.
 *
 * Instead of 5 on-chain transactions + polling, the buyer:
 *   1. Reads UOMP portfolio context (same as ERC-8183 client)
 *   2. Signs a payment authorization with their wallet (EIP-191 personal_sign)
 *   3. POSTs to /x402/analyze with X-Payment header
 *   4. Reads the SSE stream: progress events → final report
 *   5. Saves HTML + PDF report (same as ERC-8183 client)
 *
 * No on-chain transactions, no polling, no escrow.
 * Full report delivered in a single HTTP connection (40–120s SSE stream).
 *
 * Setup:  Same .env as index.ts — KEYSTORE_PATH, WALLET_PASSWORD, AGENT_ENDPOINT
 * Run:    npm run x402
 */

import { readFileSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import { Wallet } from "ethers";
import { randomBytes } from "crypto";
import { GuardUserMemory, buildTaskFromMemory, type Holding, type RiskProfile } from "./uomp.js";
import { saveReport } from "./pdf-report.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

// ── Config ────────────────────────────────────────────────────────────────────
const KEYSTORE_PATH   = process.env["KEYSTORE_PATH"]   ?? "../stockanalyst/.studio/wallets/0x1FF095E1C5Cf4bC72a3DC54be17B6cf85043Fb67.json";
const WALLET_PASSWORD = process.env["WALLET_PASSWORD"] ?? "";

// X402_ENDPOINT: base URL of the LOCAL agent (x402 routes are /x402/analyze, not /a2a).
// Defaults to localhost:9000. Do NOT use AGENT_ENDPOINT here — that points to the
// deployed platform's A2A path (which requires Cognito auth and has no x402 routes).
const AGENT_ENDPOINT = process.env["X402_ENDPOINT"] ?? "http://localhost:9000";

// Seller wallet address (must match x402_verify.py SELLER_WALLET)
const SELLER_WALLET = "0x1ff095e1c5cf4bc72a3dc54be17b6cf85043fb67";

// U token on BSC Testnet — EIP-712 domain for EIP-3009 TransferWithAuthorization
// Set env U_TOKEN_DOMAIN_NAME / U_TOKEN_DOMAIN_VERSION to override if the
// deployed contract uses different values (verify with token.name()).
const U_TOKEN_ADDRESS       = "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565";
const U_TOKEN_DOMAIN_NAME    = process.env["U_TOKEN_DOMAIN_NAME"]    ?? "U";
const U_TOKEN_DOMAIN_VERSION = process.env["U_TOKEN_DOMAIN_VERSION"] ?? "1";
const BSC_TESTNET_CHAIN_ID   = 97;

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

// ── x402 v2 payment proof (EIP-712 / EIP-3009) ───────────────────────────────

/**
 * Build and EIP-712 sign an x402 v2 TransferWithAuthorization proof.
 *
 * Uses EIP-3009 (transferWithAuthorization): the buyer signs typed data over
 * the U token's EIP-712 domain; the agent verifies locally then submits to the
 * Binance Pay x402 facilitator which calls token.transferWithAuthorization()
 * on-chain — no pre-approval required, no gas paid by the buyer.
 *
 * ethers v6 `signTypedData(domain, types, value)` produces the correct
 * EIP-712 signature that x402_verify.py's Account._recover_hash() expects.
 */
async function buildPaymentProof(
  wallet: Wallet,
  priceWei: string = "1000000000000000000",
  ttlSeconds: number = 600,
): Promise<string> {
  const now   = Math.floor(Date.now() / 1000);
  const nonce = "0x" + randomBytes(32).toString("hex");  // bytes32

  const authorization = {
    from:        wallet.address.toLowerCase(),
    to:          SELLER_WALLET,
    value:       priceWei,
    validAfter:  "0",
    validBefore: String(now + ttlSeconds),
    nonce,
  };

  // EIP-712 domain (must match x402_verify.py _domain_separator())
  const domain = {
    name:              U_TOKEN_DOMAIN_NAME,
    version:           U_TOKEN_DOMAIN_VERSION,
    chainId:           BSC_TESTNET_CHAIN_ID,
    verifyingContract: U_TOKEN_ADDRESS,
  };

  // EIP-712 types (TransferWithAuthorization — EIP-3009)
  const types = {
    TransferWithAuthorization: [
      { name: "from",        type: "address" },
      { name: "to",          type: "address" },
      { name: "value",       type: "uint256" },
      { name: "validAfter",  type: "uint256" },
      { name: "validBefore", type: "uint256" },
      { name: "nonce",       type: "bytes32" },
    ],
  };

  // signTypedData encodes uint256 fields as BigInt
  const sig = await wallet.signTypedData(domain, types, {
    from:        authorization.from,
    to:          authorization.to,
    value:       BigInt(authorization.value),
    validAfter:  BigInt(authorization.validAfter),
    validBefore: BigInt(authorization.validBefore),
    nonce:       authorization.nonce,   // bytes32 as "0x..." hex string
  });

  // x402 v2 wire format
  const proof = {
    x402Version: 2,
    scheme:      "exact",
    network:     `eip155:${BSC_TESTNET_CHAIN_ID}`,
    payload: {
      signature:     sig,
      authorization,
    },
  };

  return Buffer.from(JSON.stringify(proof)).toString("base64");
}

// ── SSE stream reader ─────────────────────────────────────────────────────────

interface SseEvent {
  event: string;
  data: string;
}

/** Parse a raw SSE text chunk into individual events. */
function parseSseChunk(chunk: string): SseEvent[] {
  const events: SseEvent[] = [];
  // SSE format: "event: <name>\ndata: <json>\n\n"
  const blocks = chunk.split("\n\n").filter((b) => b.trim());
  for (const block of blocks) {
    let event = "message";
    let data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) event = line.slice(7).trim();
      else if (line.startsWith("data: ")) data = line.slice(6).trim();
    }
    if (data) events.push({ event, data });
  }
  return events;
}

/**
 * POST to /x402/analyze with X-Payment header and consume the SSE stream.
 * Returns the final report markdown when the "done" event arrives.
 */
async function analyzeViaX402(
  endpoint: string,
  symbols: string[],
  paymentProof: string,
  portfolio: Holding[],
  riskProfile: RiskProfile,
): Promise<string> {
  const url = `${endpoint}/x402/analyze`;

  const body = JSON.stringify({
    symbols,
    analysis_type: "comprehensive",
    portfolio,        // passed in body for UOMP context (seller_core reads from portfolio key in ERC-8183 notify_funded)
    risk_profile: riskProfile,
  });

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type":  "application/json",
      "X-Payment":     paymentProof,
    },
    body,
  });

  if (resp.status === 402) {
    const j = await resp.json() as { error?: string; detail?: string };
    throw new Error(`Payment rejected: ${j.error ?? ""} — ${j.detail ?? "check X-Payment header"}`);
  }
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`HTTP ${resp.status}: ${text.slice(0, 200)}`);
  }
  if (!resp.body) {
    throw new Error("No response body — SSE stream unavailable");
  }

  // Read SSE stream
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let reportMarkdown = "";
  let buffer = "";
  let thinkingShown = false;  // collapse repeated heartbeat lines

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const events = parseSseChunk(buffer);

    // Keep any incomplete block in the buffer (no trailing \n\n yet)
    const lastSep = buffer.lastIndexOf("\n\n");
    buffer = lastSep >= 0 ? buffer.slice(lastSep + 2) : buffer;

    for (const { event, data } of events) {
      try {
        const payload = JSON.parse(data) as Record<string, unknown>;

        if (event === "progress") {
          const stage   = String(payload["stage"]   ?? "");
          const tool    = String(payload["tool"]    ?? "");
          const message = String(payload["message"] ?? "");
          if (tool) {
            thinkingShown = false;
            process.stdout.write(`  ⟳  ${tool}\n`);
          } else if (stage === "thinking") {
            // Heartbeat keepalive — show a single "..." line, then update in-place
            if (!thinkingShown) {
              process.stdout.write(`  ·  ${message}`);
              thinkingShown = true;
            } else {
              process.stdout.write(".");
            }
          } else if (message) {
            if (thinkingShown) process.stdout.write("\n");
            thinkingShown = false;
            console.log(`  ${stage === "generating" ? "✎" : "→"}  ${message}`);
          }
        } else if (event === "report") {
          reportMarkdown = String(payload["content"] ?? "");
          console.log(`\n  ✓ Report received (${reportMarkdown.length} chars)`);
        } else if (event === "error") {
          throw new Error(`Agent error: ${payload["message"] ?? data}`);
        } else if (event === "done") {
          return reportMarkdown;
        }
      } catch (parseErr) {
        if (parseErr instanceof SyntaxError) continue; // partial JSON, skip
        throw parseErr;
      }
    }
  }

  return reportMarkdown; // stream ended without "done" — return what we have
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  if (!WALLET_PASSWORD) {
    console.error("ERROR: WALLET_PASSWORD is required. Set it in .env");
    process.exit(1);
  }

  // ── Load wallet ─────────────────────────────────────────────────────────
  const keystorePath = resolve(__dirname, "..", KEYSTORE_PATH);
  const keystoreJson = readFileSync(keystorePath, "utf8");
  console.log("Decrypting keystore...");
  const wallet = await Wallet.fromEncryptedJson(keystoreJson, WALLET_PASSWORD) as Wallet;

  // ── Load UOMP context ────────────────────────────────────────────────────
  hr("Step 1: Load UOMP user context (portfolio + risk profile)");
  const memory = new GuardUserMemory();
  const { symbols, portfolio, riskProfile } = await buildTaskFromMemory(memory);
  console.log(`  ✓ Symbols:  ${symbols.join(", ")}`);
  console.log(`  ✓ Holdings: ${portfolio.length} positions`);
  console.log(`  ✓ Risk:     ${riskProfile.tolerance} / ${riskProfile.horizonMonths}mo`);

  banner([
    "Stock Analysis Agent — x402 Buyer",
    `x402 endpoint: ${AGENT_ENDPOINT}`,
    `Buyer:         ${wallet.address}`,
    `Symbols:       ${symbols.join(", ")}`,
    "Payment:       x402 v2 / EIP-712 EIP-3009 (Binance Pay facilitator)",
    "Delivery:      SSE stream (no polling needed)",
  ]);

  // ── Sign payment proof ───────────────────────────────────────────────────
  hr("Step 2: Sign x402 v2 payment authorization (EIP-712 EIP-3009)");
  const proof = await buildPaymentProof(wallet);
  console.log("  ✓ EIP-712 proof signed (TransferWithAuthorization)");
  console.log(`  ✓ Paying:   1.0 U → ${SELLER_WALLET}`);
  console.log(`  ✓ Token:    ${U_TOKEN_ADDRESS} (BSC Testnet)`);
  console.log(`  ✓ Valid for: 10 minutes`);

  // ── Stream analysis ──────────────────────────────────────────────────────
  hr("Step 3: POST /x402/analyze → streaming SSE report");
  console.log("  (progress events below — full analysis takes 40–120s)\n");

  const reportMarkdown = await analyzeViaX402(
    AGENT_ENDPOINT,
    symbols,
    proof,
    portfolio,
    riskProfile,
  );

  if (!reportMarkdown) {
    throw new Error("No report content received from agent");
  }

  // ── Print report ─────────────────────────────────────────────────────────
  console.log("\n┌─ REPORT " + "─".repeat(50) + "┐");
  for (const line of reportMarkdown.split("\n").slice(0, 30)) {
    console.log("│ " + line);
  }
  if (reportMarkdown.split("\n").length > 30) {
    console.log("│ ... (truncated — see full report in saved file)");
  }
  console.log("└" + "─".repeat(52) + "┘");

  // ── Save HTML + PDF ──────────────────────────────────────────────────────
  hr("Step 4: Save report as HTML + PDF");
  try {
    const jobLabel = "x402-" + Date.now().toString().slice(-6);
    const { pdfPath, htmlPath } = await saveReport(reportMarkdown, jobLabel, symbols);
    console.log(`  ✓ HTML  ${htmlPath}`);
    if (pdfPath) {
      console.log(`  ✓ PDF   ${pdfPath}`);
    } else {
      console.log("  ℹ  PDF skipped (Chrome unavailable) — HTML saved.");
    }
  } catch (err) {
    console.log(`  ⚠  Save failed: ${err}`);
  }

  banner([
    "✓ x402 FLOW COMPLETE",
    "  1 signature  ·  1 HTTP call  ·  SSE stream",
    "  (vs ERC-8183: 5 on-chain txs + polling + settle)",
  ]);
}

main().catch((err: Error) => {
  console.error("\n✗ FAILED:", err.message);
  process.exit(1);
});
