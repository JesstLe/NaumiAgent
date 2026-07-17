import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi, visibleWidth } from "../src/ansi.js";
import { renderDoctorHealthPage } from "../src/components/doctor-health-page.js";

function snapshot() {
  return {
    schema_version: 1,
    status: "degraded",
    generated_at: "2026-07-18T10:00:00+00:00",
    live_probe: false,
    snapshot_sha256: "a".repeat(64),
    items: [
      {
        id: "node-1", domain: "node", label: "Node.js", severity: "ok",
        responsibility: "unknown", detail: "v22.0.0", suggestion: "",
      },
      {
        id: "provider-2", domain: "provider", label: "API key", severity: "error",
        responsibility: "user_config", detail: "未检测到凭据",
        suggestion: "运行 naumi configure。",
      },
    ],
  };
}

test("doctor health page renders typed local evidence at common widths", () => {
  for (const width of [80, 120, 200]) {
    const lines = renderDoctorHealthPage({
      snapshot: snapshot(),
      heartbeat: { status: "healthy", rttMs: 12, ageMs: 0 },
      scrollOffset: 0,
    }, width, 16);
    const plain = lines.map(stripAnsi).join("\n");
    assert.equal(lines.length, 16);
    assert(lines.every((line) => visibleWidth(line) <= width));
    for (const expected of ["环境健康诊断", "本地只读", "Bridge 心跳", "Node.js", "API key", "用户配置", "下一步"]) {
      assert(plain.includes(expected));
    }
  }
});

test("doctor health page distinguishes stale and unknown heartbeat", () => {
  const stale = renderDoctorHealthPage({
    snapshot: snapshot(), heartbeat: { status: "stale", ageMs: 7000 },
  }, 100, 12).map(stripAnsi).join("\n");
  const starting = renderDoctorHealthPage({
    snapshot: snapshot(), heartbeat: { status: "starting" },
  }, 100, 12).map(stripAnsi).join("\n");
  assert(stale.includes("后端控制面已 7000ms 无响应"));
  assert(stale.includes("不要自动重复提交"));
  assert(starting.includes("等待首次心跳证据"));
});
