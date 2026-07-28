/**
 * PDF report generator — converts the Markdown stock analysis report to a
 * professionally styled PDF with the ChainAnalyst logo.
 *
 * Uses puppeteer (headless Chrome) to render HTML → PDF.
 * Falls back to saving an HTML file if Chrome is unavailable.
 */

import { rmSync, writeFileSync } from "fs";
import { resolve } from "path";
import { renderPdf, type PuppeteerLike } from "./pdf-renderer.js";

const MAX_UINT256_DECIMAL = "115792089237316195423570985008687907853269984665640564039457584007913129639935";

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
  const lines = escapeHtml(md)
    // Reports should not contain code blocks. This non-nested scan is linear.
    .replace(/```[\s\S]*?```/g, "")
    .split("\n");
  const html: string[] = [];

  for (let index = 0; index < lines.length;) {
    const line = lines[index]!;
    if (!line.trim()) {
      index += 1;
      continue;
    }

    if (/^---+$/.test(line)) {
      html.push("<hr>");
      index += 1;
      continue;
    }

    const heading = /^(#{1,4}) (.+)$/.exec(line);
    if (heading) {
      html.push(`<h${heading[1]!.length}>${inlineToHtml(heading[2]!)}</h${heading[1]!.length}>`);
      index += 1;
      continue;
    }

    const quote = /^&gt; (.+)$/.exec(line);
    if (quote) {
      const content = quote[1]!.replace(/^#{1,4}\s+/, "");
      html.push(`<blockquote><p>${inlineToHtml(content)}</p></blockquote>`);
      index += 1;
      continue;
    }

    const unordered = /^[ \t]*[-*] (.+)$/.exec(line);
    if (unordered) {
      const items: string[] = [];
      while (index < lines.length) {
        const item = /^[ \t]*[-*] (.+)$/.exec(lines[index]!);
        if (!item) break;
        items.push(`<li>${inlineToHtml(item[1]!.trim())}</li>`);
        index += 1;
      }
      html.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    const ordered = /^[ \t]*\d+\. (.+)$/.exec(line);
    if (ordered) {
      const items: string[] = [];
      while (index < lines.length) {
        const item = /^[ \t]*\d+\. (.+)$/.exec(lines[index]!);
        if (!item) break;
        items.push(`<li>${inlineToHtml(item[1]!.trim())}</li>`);
        index += 1;
      }
      html.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    const tableRow = /^\|(.+)\|$/.exec(line);
    if (tableRow) {
      const rows: string[][] = [];
      while (index < lines.length) {
        const row = /^\|(.+)\|$/.exec(lines[index]!);
        if (!row) break;
        rows.push(row[1]!.split("|").map((cell) => inlineToHtml(cell.trim())));
        index += 1;
      }
      let table = "<table>";
      rows.forEach((cells, rowIndex) => {
        if (rowIndex === 0) {
          table += `<thead><tr>${cells.map((cell) => `<th>${cell}</th>`).join("")}</tr></thead><tbody>`;
        } else if (rowIndex !== 1 || !cells.every((cell) => /^[-|: ]+$/.test(cell))) {
          table += `<tr>${cells.map((cell) => `<td>${cell}</td>`).join("")}</tr>`;
        }
      });
      html.push(`${table}</tbody></table>`);
      continue;
    }

    html.push(`<p>${inlineToHtml(line)}</p>`);
    index += 1;
  }

  return html.join("\n");
}

export function renderReportHtml(reportText: string, meta: ReportMeta): string {
  return buildHtml(mdToHtml(reportText), {
    jobId: escapeHtml(meta.jobId),
    date: escapeHtml(meta.date),
    symbols: escapeHtml(meta.symbols),
  });
}

export type PdfRenderer = (html: string, pdfPath: string) => Promise<void>;

function assertCanonicalUint256(jobId: string): void {
  if (
    typeof jobId !== "string"
    || !/^(0|[1-9]\d{0,77})$/.test(jobId)
    || (jobId.length === MAX_UINT256_DECIMAL.length && jobId > MAX_UINT256_DECIMAL)
  ) {
    throw new TypeError("jobId must be a canonical decimal uint256");
  }
}

export async function saveReportWithRenderer(
  reportText: string,
  jobId: string,
  symbols: string[],
  renderer: PdfRenderer,
): Promise<{ pdfPath: string | null; htmlPath: string }> {
  assertCanonicalUint256(jobId);
  const date = new Date().toLocaleDateString("en-GB", {
    year: "numeric", month: "long", day: "numeric",
  });
  const html = renderReportHtml(reportText, { jobId, date, symbols: symbols.join(", ") });

  const base = `stock-analysis-${jobId}`;
  const htmlPath = resolve(process.cwd(), `${base}.html`);
  const candidatePdfPath = resolve(process.cwd(), `${base}.pdf`);
  writeFileSync(htmlPath, html, "utf8");

  try {
    await renderer(html, candidatePdfPath);
    return { pdfPath: candidatePdfPath, htmlPath };
  } catch {
    try {
      rmSync(candidatePdfPath, { force: true });
    } catch {
      // Preserve the inert HTML fallback if cleanup fails.
    }
    return { pdfPath: null, htmlPath };
  }
}

// ── Main export ───────────────────────────────────────────────────────────────
export async function saveReport(
  reportText: string,
  jobId: string,
  symbols: string[],
): Promise<{ pdfPath: string | null; htmlPath: string }> {
  return saveReportWithRenderer(reportText, jobId, symbols, async (html, pdfPath) => {
    const puppeteer = await import("puppeteer" as string);
    await renderPdf(puppeteer as unknown as PuppeteerLike, html, pdfPath);
  });
}
