/**
 * PDF report generator — converts the Markdown stock analysis report to a
 * professionally styled PDF with the ChainAnalyst logo.
 *
 * Uses puppeteer (headless Chrome) to render HTML → PDF.
 * Falls back to saving an HTML file if Chrome is unavailable.
 */

import { writeFileSync } from "fs";
import { resolve } from "path";

// ── Logo SVG (inline, no external deps) ──────────────────────────────────────
const LOGO_SVG = `<svg width="54" height="54" viewBox="0 0 54 54" xmlns="http://www.w3.org/2000/svg">
  <polygon points="27,2 52,15.5 52,38.5 27,52 2,38.5 2,15.5" fill="#c4a046"/>
  <polygon points="27,8 46,18.5 46,35.5 27,46 8,35.5 8,18.5" fill="#1a2744"/>
  <polyline points="13,38 22,26 31,30 41,14" stroke="#c4a046" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="22" cy="26" r="2.2" fill="#c4a046"/>
  <circle cx="31" cy="30" r="2.2" fill="#c4a046"/>
  <circle cx="41" cy="14" r="3" fill="white"/>
  <circle cx="13" cy="38" r="2.2" fill="#aab8d4"/>
</svg>`;

export interface ReportMeta {
  jobId: string;
  date: string;
  symbols: string;
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char] ?? char);
}

// ── HTML template ─────────────────────────────────────────────────────────────
function buildHtml(markdownHtml: string, meta: ReportMeta): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
:root {
  --navy:  #1a2744;
  --navy2: #243460;
  --gold:  #c4a046;
  --light: #f4f6fb;
  --border:#dde2ee;
  --green: #155f34;
  --amber: #8a4f00;
  --red:   #991b1b;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Georgia,serif;font-size:10pt;color:#1e2535;line-height:1.55;background:#fff}

/* ── Header ── */
.hdr{background:var(--navy);padding:18px 36px;display:flex;justify-content:space-between;align-items:center}
.logo{display:flex;align-items:center;gap:14px}
.logo-text .firm{font-family:Arial,sans-serif;font-size:22pt;font-weight:700;letter-spacing:.5px;color:var(--gold)}
.logo-text .tag{font-family:Arial,sans-serif;font-size:7.5pt;color:#7a96bf;letter-spacing:2px;text-transform:uppercase;margin-top:2px}
.hdr-meta{text-align:right;font-family:Arial,sans-serif;font-size:8pt;color:#7a96bf;line-height:1.8}
.hdr-meta .conf{color:#e8be56;font-weight:bold;letter-spacing:1px;font-size:7pt;text-transform:uppercase}

/* ── Title bar ── */
.title-bar{background:var(--light);border-left:5px solid var(--gold);border-bottom:1px solid var(--border);padding:14px 36px}
.title-bar h1{font-family:Arial,sans-serif;font-size:15pt;color:var(--navy);font-weight:700}
.title-bar .symbols{font-family:Arial,sans-serif;font-size:9pt;color:#4a5e80;margin-top:3px}

/* ── Body content ── */
.body{padding:20px 36px 80px}

/* Headings */
h1{font-family:Arial,sans-serif;font-size:16pt;color:var(--navy);font-weight:700;margin:24px 0 10px;border-bottom:2px solid var(--gold);padding-bottom:6px}
h2{font-family:Arial,sans-serif;font-size:13pt;color:var(--navy);font-weight:700;margin:20px 0 8px;border-bottom:2px solid var(--gold);padding-bottom:5px}
h3{font-family:Arial,sans-serif;font-size:11pt;color:var(--navy2);font-weight:700;margin:16px 0 6px}
h4{font-family:Arial,sans-serif;font-size:10pt;color:#3a4f6e;font-weight:700;margin:12px 0 5px}
hr{border:none;border-top:1px solid var(--border);margin:18px 0}

/* Tables */
table{width:100%;border-collapse:collapse;margin:10px 0 16px;font-size:9pt;font-family:Arial,sans-serif}
th{background:var(--navy);color:#fff;padding:7px 10px;text-align:left;font-weight:600;font-size:8.5pt}
td{border:1px solid var(--border);padding:6px 10px;vertical-align:top}
tr:nth-child(even) td{background:var(--light)}

/* Blockquotes (verdict banners) */
blockquote{background:#eef1f9;border-left:5px solid var(--navy);padding:10px 16px;margin:10px 0 16px;border-radius:0 4px 4px 0}
blockquote p{margin:0;font-family:Arial,sans-serif;font-size:10.5pt;font-weight:700;color:var(--navy)}

/* Lists */
ul,ol{padding-left:22px;margin:6px 0 10px}
li{margin:4px 0}
p{margin:5px 0 10px}
strong{color:var(--navy)}
em{color:#5a6f8a}

/* Code (used for tickers) */
code{background:#e8ecf4;border-radius:3px;padding:1px 5px;font-family:monospace;font-size:9pt;color:var(--navy)}

/* BUY / HOLD / SELL inline badges — applied by trusted Markdown conversion */
.badge{display:inline-block;padding:2px 9px;border-radius:3px;font-family:Arial,sans-serif;font-weight:700;font-size:9pt;color:#fff;letter-spacing:.5px}
.badge-buy{background:var(--green)}
.badge-hold{background:var(--amber)}
.badge-sell{background:var(--red)}

/* Positive / negative colouring */
.pos{color:var(--green);font-weight:bold}
.neg{color:var(--red);font-weight:bold}

/* ── Footer ── */
.ftr{position:fixed;bottom:0;left:0;right:0;background:var(--navy);color:#7a96bf;font-family:Arial,sans-serif;font-size:7pt;padding:7px 36px;display:flex;justify-content:space-between;align-items:center}
.ftr .left{letter-spacing:.3px}
.ftr .right{color:#4a6088}

/* ── Page numbers ── */
@page{size:A4;margin:0}
@media print{
  .ftr{position:fixed;bottom:0}
  .no-break{page-break-inside:avoid}
}
</style>
</head>
<body>

<!-- Header -->
<div class="hdr">
  <div class="logo">
    ${LOGO_SVG}
    <div class="logo-text">
      <div class="firm">ChainAnalyst</div>
      <div class="tag">Blockchain-Settled Research</div>
    </div>
  </div>
  <div class="hdr-meta">
    <div class="conf">Confidential — Client Use Only</div>
    <div>Date: ${meta.date}</div>
    <div>Report ID: #${meta.jobId}</div>
    <div>Network: BSC Testnet</div>
  </div>
</div>

<!-- Title bar -->
<div class="title-bar">
  <h1>Personalised Stock Analysis Report</h1>
  <div class="symbols">Coverage: ${meta.symbols}</div>
</div>

<!-- Body -->
<div class="body" id="report-body">
${markdownHtml}
</div>

<!-- Footer -->
<div class="ftr">
  <span class="left">ChainAnalyst &nbsp;|&nbsp; Blockchain-Settled AI Research &nbsp;|&nbsp; Powered by BNB Chain ERC-8183</span>
  <span class="right">This report is for informational purposes only and does not constitute investment advice.</span>
</div>

</body>
</html>`;
}

// ── Inline markdown processor (reusable for cell content and prose) ──────────
function inlineToHtml(s: string): string {
  return s
    .replace(/\*\*\*(.+?)\*\*\*/g, "<strong><em>$1</em></strong>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\b(BUY)\b/g, '<span class="badge badge-buy">BUY</span>')
    .replace(/\b(HOLD)\b/g, '<span class="badge badge-hold">HOLD</span>')
    .replace(/\b(SELL)\b/g, '<span class="badge badge-sell">SELL</span>')
    .replace(/(\+\d+(?:\.\d+)?%)/g, '<span class="pos">$1</span>')
    .replace(/(−|-)\d+(?:\.\d+)?%/g, (value) => `<span class="neg">${value}</span>`);
}

// ── Markdown → HTML ───────────────────────────────────────────────────────────
function mdToHtml(md: string): string {
  // Block-level element tag names — paragraph catch-all skips these
  const BLOCK = /^<(h[1-6]|table|thead|tbody|tr|ul|ol|li|blockquote|hr|div|p)\b/;

  return escapeHtml(md)
    // Fenced code blocks (strip — reports shouldn't have code)
    .replace(/```[\s\S]*?```/g, "")
    // HR
    .replace(/^---+$/gm, "<hr>")
    // H1-H4 (run before blockquotes so "## heading" inside prose isn't swallowed)
    .replace(/^#### (.+)$/gm, (_, t) => `<h4>${inlineToHtml(t)}</h4>`)
    .replace(/^### (.+)$/gm,  (_, t) => `<h3>${inlineToHtml(t)}</h3>`)
    .replace(/^## (.+)$/gm,   (_, t) => `<h2>${inlineToHtml(t)}</h2>`)
    .replace(/^# (.+)$/gm,    (_, t) => `<h1>${inlineToHtml(t)}</h1>`)
    // Blockquotes — process inline markup inside them
    .replace(/^> (.+)$/gm, (_, content) => {
      // Strip any stray heading markers left inside a blockquote
      const clean = content.replace(/^#{1,4}\s+/, "");
      return `<blockquote><p>${inlineToHtml(clean)}</p></blockquote>`;
    })
    // Unordered lists
    .replace(/^(\s*[-*] .+(\n\s*[-*] .+)*)/gm, (block) => {
      const items = block
        .trim()
        .split(/\n\s*[-*] /)
        .filter(Boolean)
        .map((s) => `<li>${inlineToHtml(s.replace(/^[-*] /, "").trim())}</li>`)
        .join("");
      return `<ul>${items}</ul>`;
    })
    // Ordered lists
    .replace(/^(\s*\d+\. .+(\n\s*\d+\. .+)*)/gm, (block) => {
      const items = block
        .trim()
        .split(/\n\s*\d+\. /)
        .filter(Boolean)
        .map((s) => `<li>${inlineToHtml(s.replace(/^\d+\. /, "").trim())}</li>`)
        .join("");
      return `<ol>${items}</ol>`;
    })
    // Tables — apply inline markup inside cells
    .replace(/^\|(.+)\|$/gm, (_, row) => {
      const cells = row.split("|").map((c: string) => c.trim());
      return `<tr-row>${cells.map((c: string) => `<cell>${inlineToHtml(c)}</cell>`).join("")}</tr-row>`;
    })
    // Table assembler
    .replace(/(<tr-row>.*?<\/tr-row>\n?)+/gs, (block) => {
      const rows = [...block.matchAll(/<tr-row>(.*?)<\/tr-row>/gs)];
      if (rows.length === 0) return block;
      let table = "<table>";
      rows.forEach((m, i) => {
        const cells = [...(m[1] ?? "").matchAll(/<cell>(.*?)<\/cell>/gs)].map((c) => c[1]);
        if (i === 0) {
          table += "<thead><tr>" + cells.map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>";
        } else if (i === 1 && cells.every((c) => /^[-|: ]+$/.test(c ?? ""))) {
          // separator row — skip
        } else {
          table += "<tr>" + cells.map((c) => `<td>${c}</td>`).join("") + "</tr>";
        }
      });
      table += "</tbody></table>";
      return table;
    })
    // Paragraphs — wrap any non-empty line that is not already a block element.
    // Inline elements (<strong>, <em>, <code>) ARE wrapped; block elements are NOT.
    .replace(/^.+$/gm, (line) => {
      if (!line.trim()) return "";
      if (BLOCK.test(line)) return line;
      // Already wrapped by a previous rule (e.g. heading lines that were converted inline)
      if (/^<(tr-row|cell)/.test(line)) return line;
      return `<p>${inlineToHtml(line)}</p>`;
    })
    // Cleanup
    .replace(/<p>\s*<\/p>/g, "");
}

export function renderReportHtml(reportText: string, meta: ReportMeta): string {
  return buildHtml(mdToHtml(reportText), {
    jobId: escapeHtml(meta.jobId),
    date: escapeHtml(meta.date),
    symbols: escapeHtml(meta.symbols),
  });
}

// ── Main export ───────────────────────────────────────────────────────────────
export async function saveReport(
  reportText: string,
  jobId: string,
  symbols: string[],
): Promise<{ pdfPath: string | null; htmlPath: string }> {
  const date = new Date().toLocaleDateString("en-GB", {
    year: "numeric", month: "long", day: "numeric",
  });
  const meta = { jobId, date, symbols: symbols.join(", ") };
  const html = renderReportHtml(reportText, meta);

  const base = `stock-analysis-${jobId}`;
  const htmlPath = resolve(process.cwd(), `${base}.html`);
  writeFileSync(htmlPath, html, "utf8");

  // Try puppeteer PDF generation
  let pdfPath: string | null = null;
  try {
    // Dynamic import so a missing puppeteer doesn't crash the whole process
    const puppeteer = await import("puppeteer" as string);
    // Prefer system Chrome on macOS if puppeteer's bundled Chrome failed to extract
    const systemChrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
    const { existsSync } = await import("fs");
    const launchOpts: Record<string, unknown> = {
      headless: true,
      args: ["--no-sandbox", "--disable-setuid-sandbox"],
    };
    if (existsSync(systemChrome)) launchOpts["executablePath"] = systemChrome;
    const browser = await (puppeteer as unknown as {
      launch: (opts: object) => Promise<{
        newPage: () => Promise<{
          setContent: (html: string, opts: object) => Promise<void>;
          pdf: (opts: object) => Promise<Buffer>;
          close: () => Promise<void>;
        }>;
        close: () => Promise<void>;
      }>;
    }).launch(launchOpts);

    const page = await browser.newPage();
    await page.setContent(html, { waitUntil: "networkidle0" });
    pdfPath = resolve(process.cwd(), `${base}.pdf`);
    await page.pdf({
      path: pdfPath,
      format: "A4",
      printBackground: true,
      margin: { top: "0", right: "0", bottom: "0", left: "0" },
    });
    await browser.close();
  } catch {
    // puppeteer not installed or Chrome unavailable — HTML is the fallback
  }

  return { pdfPath, htmlPath };
}
