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
        diagnostic_code: "provider_credentials_missing",
      },
      {
        id: "runtime-heartbeat-retention", domain: "runtime",
        label: "运行时心跳清理", severity: "degraded",
        responsibility: "product_runtime", detail: "策略已启用；本轮失败；历史失败 1。",
        suggestion: "检查 Harness Store；清理失败不会中断模型执行。",
      },
      {
        id: "runtime-worker-authority", domain: "runtime",
        label: "Worker authority", severity: "ok",
        responsibility: "unknown",
        detail: "tool-worker-a tool epoch 3 linux/x86_64 容量占用 1/4、可用 3 心跳健康/3.0s",
        suggestion: "",
      },
      {
        id: "runtime-worker-capacity-queue", domain: "runtime",
        label: "Worker 容量队列", severity: "degraded",
        responsibility: "product_runtime",
        detail: "已配置队列 Worker 1 个。 tool-worker-a 等待 1/8、领取 1、最久 2.0s",
        suggestion: "等待容量释放，避免无界重试。",
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
    }, width, 30);
    const plain = lines.map(stripAnsi).join("\n");
    assert.equal(lines.length, 30);
    assert(lines.every((line) => visibleWidth(line) <= width));
    for (const expected of [
      "环境健康诊断", "本地只读", "Bridge 心跳", "Node.js", "API key", "用户配置",
      "诊断码", "provider_credentials_missing",
      "运行时心跳清理", "产品运行时", "清理失败不会中断模型执行", "下一步",
      "Worker authority", "容量占用 1/4", "可用 3", "Worker 容量队列",
      "等待 1/8", "领取 1", "最久 2.0s", "避免无界重试",
    ]) {
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

test("doctor health page renders exact live probe budget and terminal receipt", () => {
  const lines = renderDoctorHealthPage({
    snapshot: { ...snapshot(), live_probe: true },
    heartbeat: { status: "healthy", rttMs: 12 },
    probeLoading: false,
    probeTimeoutMs: 12_000,
    probeResult: {
      schema_version: 1,
      status: "failed",
      diagnostic_code: "provider_timeout",
      message: "连接超时",
      suggestion: "检查网络、代理和 API Base。",
      request_count: 1,
      request_limit: 1,
      max_output_tokens: 8,
      duration_ms: 12_001,
      timeout_ms: 12_000,
      snapshot_sha256: "f".repeat(64),
    },
  }, 100, 40);
  const plain = lines.map(stripAnsi).join("\n");

  assert.match(plain, /p 在线/);
  assert.match(plain, /最多 1 请求/);
  assert.match(plain, /最多 8 输出 token/);
  assert.match(plain, /不自动重试/);
  assert.match(plain, /包含显式在线探测证据/);
  assert.match(plain, /在线探测回执 · 失败/);
  assert.match(plain, /请求 1\/1/);
  assert.match(plain, /provider_timeout/);
});

test("doctor health page renders export preview and written receipt", () => {
  const preview = {
    schema_version: 1,
    status: "written",
    bundle_format: "zip",
    source_snapshot_sha256: "a".repeat(64),
    manifest_sha256: "b".repeat(64),
    bundle_sha256: "c".repeat(64),
    total_bytes: 2048,
    privacy_notice: "不包含聊天、reasoning、原始 trace、凭据或源码。",
    files: [
      { path: "health.json", size_bytes: 1000, description: "脱敏 Health" },
      { path: "README.txt", size_bytes: 200, description: "说明" },
      { path: "manifest.json", size_bytes: 500, description: "清单" },
    ],
  };
  const lines = renderDoctorHealthPage({
    snapshot: snapshot(),
    heartbeat: { status: "healthy", rttMs: 12 },
    exportPreview: preview,
    exportReceipt: {
      output_path: "/state/diagnostics/report.zip",
      reused_existing: false,
    },
  }, 100, 40);
  const plain = lines.map(stripAnsi).join("\n");

  assert.match(plain, /诊断包预览/);
  assert.match(plain, /3 个文件 · 2.0 KiB/);
  assert.match(plain, /health.json/);
  assert.match(plain, /不包含聊天/);
  assert.match(plain, /已导出 · .*report.zip/);
  assert(lines.every((line) => visibleWidth(line) <= 100));
});

test("doctor health page distinguishes compatibility downgrade from export failure", () => {
  const lines = renderDoctorHealthPage({
    snapshot: snapshot(),
    heartbeat: { status: "healthy", rttMs: 12 },
    exportNotice: "当前 Bridge 不支持脱敏诊断包导出，未发送写入请求。",
    exportError: "状态目录不可写。",
  }, 100, 30);
  const plain = lines.map(stripAnsi).join("\n");

  assert.match(plain, /兼容模式 · 当前 Bridge 不支持/);
  assert.match(plain, /导出失败 · 状态目录不可写/);
  assert(lines.every((line) => visibleWidth(line) <= 100));
});
