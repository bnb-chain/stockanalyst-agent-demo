#!/usr/bin/env node
/**
 * x402 free tier buyer — 0 U EIP-712 identity proof → quick quote.
 *
 * Signs a TransferWithAuthorization for 0 U (proving wallet identity without
 * any token transfer).  Rate-limited to 10 requests per wallet per 24 hours.
 *
 * Usage:
 *   npm run x402:free              # uses SYMBOL env var or prompts error
 *   SYMBOL=NVDA npm run x402:free  # override symbol
 *   SYMBOL=AAPL,TSLA npm run x402:free  # first symbol used
 *
 * Setup: same .env as x402.ts — KEYSTORE_PATH, WALLET_PASSWORD, X402_ENDPOINT
 */

import { readFileSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import { Wallet } from "ethers";
import { randomBytes } from "crypto";

const __dirname = dirname(fileURLToPath(import.meta.url));

// ── Config ────────────────────────────────────────────────────────────────────
const KEYSTORE_PATH   = process.env["KEYSTORE_PATH"]   ?? "../stockanalyst/.studio/wallets/0x1FF095E1C5Cf4bC72a3DC54be17B6cf85043Fb67.json";
const WALLET_PASSWORD = process.env["WALLET_PASSWORD"] ?? "";
const AGENT_ENDPOINT  = process.env["X402_ENDPOINT"]   ?? "http://localhost:9000";

const SELLER_WALLET          = "0x1ff095e1c5cf4bc72a3dc54be17b6cf85043fb67";
const U_TOKEN_ADDRESS        = "0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565";
const U_TOKEN_DOMAIN_NAME    = process.env["U_TOKEN_DOMAIN_NAME"]    ?? "U";
const U_TOKEN_DOMAIN_VERSION = process.env["U_TOKEN_DOMAIN_VERSION"] ?? "1";
const BSC_TESTNET_CHAIN_ID   = 97;

// Symbol: first CLI argument, or SYMBOL env var
const rawSymbol = process.argv[2] || process.env["SYMBOL"] || "";
const SYMBOL    = rawSymbol.split(",")[0].trim().toUpperCase();

function hr(label: string): void {
  console.log(`\n${"─".repeat(60)}`);
  console.log(`  ${label}`);
  console.log("─".repeat(60));
}

// ── 0-U EIP-712 proof ─────────────────────────────────────────────────────────

async function buildFreeProof(wallet: Wallet, ttlSeconds = 600): Promise<string> {
  const now   = Math.floor(Date.now() / 1000);
  const nonce = "0x" + randomBytes(32).toString("hex");

  const authorization = {
    from:        wallet.address.toLowerCase(),
    to:          SELLER_WALLET,
    value:       "0",                           // ← 0 U (identity proof only)
    validAfter:  "0",
    validBefore: String(now + ttlSeconds),
    nonce,
  };

  const domain = {
    name:              U_TOKEN_DOMAIN_NAME,
    version:           U_TOKEN_DOMAIN_VERSION,
    chainId:           BSC_TESTNET_CHAIN_ID,
    verifyingContract: U_TOKEN_ADDRESS,
  };

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

  const sig = await wallet.signTypedData(domain, types, {
    from:        authorization.from,
    to:          authorization.to,
    value:       BigInt(0),
    validAfter:  BigInt(0),
    validBefore: BigInt(authorization.validBefore),
    nonce:       authorization.nonce,
  });

  const proof = {
    x402Version: 2,
    scheme:      "exact",
    network:     `eip155:${BSC_TESTNET_CHAIN_ID}`,
    payload: { signature: sig, authorization },
  };

  return Buffer.from(JSON.stringify(proof)).toString("base64");
}

// ── SSE stream reader ─────────────────────────────────────────────────────────

interface SseEvent { event: string; data: string }

function parseSseChunk(chunk: string): SseEvent[] {
  const events: SseEvent[] = [];
  for (const block of chunk.split("\n\n").filter((b) => b.trim())) {
    let event = "message", data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) event = line.slice(7).trim();
      else if (line.startsWith("data: ")) data = line.slice(6).trim();
    }
    if (data) events.push({ event, data });
  }
  return events;
}

async function fetchFreeQuote(endpoint: string, symbol: string, proof: string): Promise<string> {
  const url  = `${endpoint}/x402/free`;
  const body = JSON.stringify({ symbol });

  const resp = await fetch(url, {
    method:  "POST",
    headers: { "Content-Type": "application/json", "X-Payment": proof },
    body,
  });

  if (resp.status === 402) {
    const j = await resp.json() as { error?: string; detail?: string };
    throw new Error(`Access denied: ${j.error ?? ""} — ${j.detail ?? "check X-Payment header"}`);
  }
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`HTTP ${resp.status}: ${text.slice(0, 200)}`);
  }
  if (!resp.body) throw new Error("No response body");

  const reader  = resp.body.getReader();
  const decoder = new TextDecoder();
  let report = "", buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = parseSseChunk(buffer);
    const lastSep = buffer.lastIndexOf("\n\n");
    buffer = lastSep >= 0 ? buffer.slice(lastSep + 2) : buffer;

    for (const { event, data } of events) {
      try {
        const payload = JSON.parse(data) as Record<string, unknown>;
        if (event === "progress") {
          const tool = String(payload["tool"] ?? "");
          const msg  = String(payload["message"] ?? "");
          if (tool) console.log(`  ⟳  ${tool}`);
          else if (msg) console.log(`  →  ${msg}`);
        } else if (event === "report") {
          report = String(payload["content"] ?? "");
          console.log(`\n  ✓ Report received (${report.length} chars)`);
        } else if (event === "error") {
          throw new Error(`Agent error: ${payload["message"] ?? data}`);
        } else if (event === "done") {
          return report;
        }
      } catch (e) {
        if (e instanceof SyntaxError) continue;
        throw e;
      }
    }
  }
  return report;
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  if (!WALLET_PASSWORD) {
    console.error("ERROR: WALLET_PASSWORD is required. Set it in .env");
    process.exit(1);
  }
  if (!SYMBOL) {
    console.error("ERROR: provide a symbol — e.g.  SYMBOL=AAPL npm run x402:free");
    console.error("       or pass it as an argument: npm run x402:free AAPL");
    process.exit(1);
  }

  const keystorePath = resolve(__dirname, "..", KEYSTORE_PATH);
  const keystoreJson = readFileSync(keystorePath, "utf8");
  console.log("Decrypting keystore...");
  const wallet = await Wallet.fromEncryptedJson(keystoreJson, WALLET_PASSWORD) as Wallet;

  console.log("\n" + "═".repeat(60));
  console.log("  x402 Free Tier — Quick Quote");
  console.log(`  Endpoint: ${AGENT_ENDPOINT}`);
  console.log(`  Wallet:   ${wallet.address}`);
  console.log(`  Symbol:   ${SYMBOL}`);
  console.log(`  Payment:  0 U (wallet identity proof only)`);
  console.log(`  Limit:    10 requests / 24 h per wallet`);
  console.log("═".repeat(60));

  hr("Step 1: Sign 0-U EIP-712 identity proof");
  const proof = await buildFreeProof(wallet);
  console.log("  ✓ EIP-712 proof signed (value = 0 U)");

  hr(`Step 2: POST /x402/free → ${SYMBOL} quick quote`);
  const report = await fetchFreeQuote(AGENT_ENDPOINT, SYMBOL, proof);

  if (!report) throw new Error("No report received");

  console.log("\n┌─ REPORT " + "─".repeat(50) + "┐");
  for (const line of report.split("\n")) {
    console.log("│ " + line);
  }
  console.log("└" + "─".repeat(52) + "┘");

  console.log("\n" + "═".repeat(60));
  console.log("  ✓ FREE TIER COMPLETE — 0 U · 1 signature · ~1s");
  console.log("  Full analysis (paid): npm run x402");
  console.log("═".repeat(60) + "\n");
}

main().catch((err: Error) => {
  console.error("\n✗ FAILED:", err.message);
  process.exit(1);
});
