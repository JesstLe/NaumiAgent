import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { stripAnsi, visibleWidth } from "../src/ansi.js";
import { renderCompletionReceiptCard } from "../src/components/completion-receipt-card.js";
import { renderHarnessDetailPage } from "../src/components/harness-detail-page.js";
import { normalizeServerRecord } from "../src/protocol.js";

const golden = JSON.parse(readFileSync(new URL(
  "../../../tests/fixtures/har07/terminal-parity-golden.json",
  import.meta.url,
), "utf8"));

test("New UI receipt and detail cover shared HAR-07.6 golden fields", () => {
  const harnessReceipt = normalizeServerRecord({
    type: "harness/receipt",
    payload: golden.harness_receipt,
  }).payload;
  const explain = normalizeServerRecord({
    type: "harness/explain",
    payload: golden.explain,
  }).payload;
  const replay = normalizeServerRecord({
    type: "harness/replay",
    payload: golden.replay,
  }).payload;
  const receiptLines = renderCompletionReceiptCard(
    golden.completion_receipt,
    { width: 120 },
    harnessReceipt,
  );
  const receipt = receiptLines.map(stripAnsi).join("\n");
  for (const fragment of golden.expected_receipt_fragments) {
    assert.match(receipt, new RegExp(escapeRegExp(fragment)));
  }
  assert(receiptLines.every((line) => visibleWidth(line) <= 120));

  const detailLines = renderHarnessDetailPage({
    runId: explain.run_id,
    explainLoading: false,
    replayLoading: false,
    explain,
    replay,
    scrollOffset: 0,
  }, 200, 100);
  const detail = detailLines.map(stripAnsi).join("\n");
  for (const fragment of golden.expected_detail_fragments) {
    assert.match(detail, new RegExp(escapeRegExp(fragment)));
  }
  assert(detailLines.every((line) => visibleWidth(line) <= 200));
  assert.doesNotMatch(`${receipt}\n${detail}`, /private/);
});

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
