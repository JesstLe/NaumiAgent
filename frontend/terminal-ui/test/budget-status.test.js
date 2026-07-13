import test from "node:test";
import assert from "node:assert/strict";
import { formatBudgetStatus } from "../src/components/budget-status.js";

test("formats unlimited and finite budget states", () => {
  assert.equal(
    formatBudgetStatus({ enabled: false, used_usd: 0.0123 }),
    "预算: 不限 · 已用 $0.0123",
  );
  assert.match(
    formatBudgetStatus({
      enabled: true,
      used_usd: 0.1,
      max_usd: 2,
      cost_percentage: 5,
    }),
    /预算: \$0\.1000\/\$2\.00 \(5\.0%\)/,
  );
});

test("formats token-only limits without inventing a money cap", () => {
  const value = formatBudgetStatus({
    enabled: true,
    used_usd: 0.02,
    max_usd: null,
    input_tokens: 42,
    max_input_tokens: 100,
  });

  assert.match(value, /不限费用 · 已用 \$0\.0200/);
  assert.match(value, /输入 42\/100/);
  assert.doesNotMatch(value, /NaN|Infinity/);
});
