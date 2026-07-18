import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi, visibleWidth } from "../src/ansi.js";
import { renderPermissionCenterPage } from "../src/components/permission-center-page.js";

function snapshot() {
  return {
    schema_version: 1,
    runtime_mode: "bypass",
    permission_mode: "bypass",
    pending: [{
      request_id: "perm-1",
      agent_name: "main",
      tool_name: "bash_run",
      reason: "需要运行检查",
      status: "needs_confirmation",
      policy: { source: "TOOL_PERMISSIONS:bash_run", risk: "medium", confirmation: "需要确认", bypass: "bypass 全权限放行" },
    }],
    grants: [{ grant_id: "grant-1", tool_family: "shell", expires_at: "" }],
    history: [{ request_id: "hist-1", agent_name: "main", tool_name: "file_write", status: "allow_once", reason: "用户已允许", receipt_id: "receipt-1", actor: "user", source: "user_confirmation", decided_at: "2026-07-19T08:00:00+00:00", policy: { source: "TOOL_PERMISSIONS:file_write", risk: "low" } }],
    warnings: [],
  };
}

test("permission center renders authoritative sections at common widths", () => {
  for (const width of [80, 120, 200]) {
    const lines = renderPermissionCenterPage({ snapshot: snapshot(), loading: false, error: "", scrollOffset: 0 }, width, 22);
    const plain = lines.map(stripAnsi).join("\n");
    assert.equal(lines.length, 22);
    assert(lines.every((line) => visibleWidth(line) <= width));
    for (const expected of ["权限策略中心", "bypass", "待确认", "有效授权", "最近决定", "TOOL_PERMISSIONS:bash_run", "操作者 user", "决策源 user_confirmation"]) {
      assert(plain.includes(expected));
    }
  }
});

test("permission center distinguishes loading and unavailable state", () => {
  const loading = renderPermissionCenterPage({ snapshot: null, loading: true, error: "", scrollOffset: 0 }, 90, 12).map(stripAnsi).join("\n");
  const failed = renderPermissionCenterPage({ snapshot: null, loading: false, error: "权限快照暂不可用", scrollOffset: 0 }, 90, 12).map(stripAnsi).join("\n");
  assert(loading.includes("正在加载"));
  assert(failed.includes("权限快照暂不可用"));
  assert(!failed.includes("暂无待确认"));
});
