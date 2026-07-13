function nonnegative(value, fallback = 0) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : fallback;
}

function optionalNonnegative(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}

function count(value) {
  return Math.floor(nonnegative(value)).toLocaleString("en-US");
}

export function formatBudgetStatus(info = {}) {
  const used = nonnegative(info.used_usd);
  const maxUsd = optionalNonnegative(info.max_usd);
  const parts = [];
  if (maxUsd === null) {
    const label = info.enabled ? "不限费用" : "不限";
    parts.push(`${label} · 已用 $${used.toFixed(4)}`);
  } else {
    const percentage = optionalNonnegative(info.cost_percentage);
    const suffix = percentage === null ? "" : ` (${percentage.toFixed(1)}%)`;
    parts.push(`$${used.toFixed(4)}/$${maxUsd.toFixed(2)}${suffix}`);
  }
  if (info.max_input_tokens != null) {
    parts.push(`输入 ${count(info.input_tokens)}/${count(info.max_input_tokens)}`);
  }
  if (info.max_output_tokens != null) {
    parts.push(`输出 ${count(info.output_tokens)}/${count(info.max_output_tokens)}`);
  }
  return `预算: ${parts.join(" · ")}`.slice(0, 500);
}
