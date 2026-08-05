#!/usr/bin/env node
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

import { INPUT_KEYS } from "../src/input-buffer.js";
import {
  cancelTaskPanelItem,
  createInitialState,
  handleDoctorHealthKey,
  handlePermissionCenterKey,
  handleSubmitText,
  openSelectedTaskPanelItem,
  reduceServerEvent,
  selectTaskPanelOffset,
  setTaskPanelFocus,
  submitTaskMessage,
} from "../src/state.js";

const FINDING_CODES = new Set([
  "client_event_missing",
  "mapping_binding_mismatch",
  "mapping_shape_invalid",
  "probe_contract_mismatch",
  "probe_failed",
  "protocol_contract_stale",
  "server_event_missing",
  "state_module_stale",
  "target_path_missing",
  "target_test_missing",
  "ui_local_coverage_mismatch",
]);
const STALE_FINDINGS = new Set([
  "mapping_binding_mismatch",
  "protocol_contract_stale",
  "state_module_stale",
]);
const ROOT_KEYS = [
  "manifest_kind",
  "mappings",
  "protocol_contract_sha256",
  "schema_version",
  "scope",
  "semantic_mapping_sha256",
  "state_module_sha256",
];
const MAPPING_KEYS = [
  "cell_id",
  "client_events",
  "correlation",
  "probe_id",
  "semantic",
  "server_events",
  "state_paths",
  "target_tests",
  "trigger",
];
const TRIGGER_KEYS = ["kind", "value"];
const TEST_KEYS = ["path", "test_name"];

const PROBES = {
  "doctor.cancel": {
    probeId: "doctor_cancel_correlated_probe",
    statePaths: ["doctorHealth.probeCancelRequestId", "doctorHealth.probeLoading", "doctorHealth.probeNotice", "doctorHealth.probeRequestId"],
    trigger: { kind: "key", value: "c" },
    clientEvents: ["doctor/probe/cancel"],
    serverEvents: ["doctor/probe/result", "error"],
    correlation: "request_id",
    run() {
      const state = createInitialState();
      state.route = { name: "doctor_health", originAnchor: null };
      state.doctorHealth.probeLoading = true;
      state.doctorHealth.probeRequestId = "probe-request-1";
      const sent = captureSend("doctor-cancel");
      const handled = handleDoctorHealthKey(state, "c", sent.send);
      expect(handled, "c 必须由 Doctor 路由消费");
      expectEvent(sent.events, "doctor/probe/cancel", { target_request_id: "probe-request-1" });
      expect(state.doctorHealth.probeCancelRequestId === "doctor-cancel-1", "取消 request_id 未保存");
      expect(state.doctorHealth.probeLoading, "取消请求不得提前伪造探测终态");
      expect(state.doctorHealth.probeNotice.includes("等待终态"), "缺少终态等待提示");
    },
  },
  "doctor.empty": {
    probeId: "doctor_empty_is_not_loading_probe",
    statePaths: ["doctorHealth.error", "doctorHealth.loading", "doctorHealth.snapshot"],
    trigger: { kind: "initial_state", value: "createInitialState" },
    clientEvents: [], serverEvents: [], correlation: "none",
    run() {
      const state = createInitialState();
      expect(state.doctorHealth.snapshot === null, "初始 Doctor 不得伪造快照");
      expect(state.doctorHealth.loading === false, "初始空态不得等同 loading");
      expect(state.doctorHealth.error === "", "初始空态不得等同 error");
    },
  },
  "doctor.focus": {
    probeId: "doctor_focus_anchor_probe",
    statePaths: ["followTail", "route.name", "route.originAnchor", "scrollOffset"],
    trigger: { kind: "command", value: "/doctor" },
    clientEvents: ["doctor"], serverEvents: [], correlation: "none",
    run() {
      const state = createInitialState();
      state.scrollOffset = 7;
      state.followTail = false;
      const sent = captureSend("doctor-focus");
      handleSubmitText(state, "/doctor", sent.send);
      expect(state.route.name === "doctor_health", "Doctor 路由未打开");
      expect(state.route.originAnchor.scrollOffset === 7, "Doctor 未保存滚动锚点");
      handleDoctorHealthKey(state, INPUT_KEYS.escape, sent.send);
      expect(state.route.name === "conversation", "Escape 未返回会话");
      expect(state.scrollOffset === 7 && state.followTail === false, "会话锚点未恢复");
    },
  },
  "doctor.keyboard": {
    probeId: "doctor_refresh_key_probe",
    statePaths: ["doctorHealth.error", "doctorHealth.loading", "route.name"],
    trigger: { kind: "key", value: "r" },
    clientEvents: ["doctor"], serverEvents: [], correlation: "none",
    run() {
      const state = createInitialState();
      state.route = { name: "doctor_health", originAnchor: null };
      state.doctorHealth.error = "旧错误";
      const sent = captureSend("doctor-refresh");
      expect(handleDoctorHealthKey(state, "r", sent.send), "r 必须由 Doctor 路由消费");
      expectEvent(sent.events, "doctor", {});
      expect(state.doctorHealth.loading && state.doctorHealth.error === "", "刷新键未建立干净 loading 态");
    },
  },
  "doctor.loading": {
    probeId: "doctor_loading_terminal_probe",
    statePaths: ["doctorHealth.error", "doctorHealth.loading", "doctorHealth.snapshot"],
    trigger: { kind: "command", value: "/doctor" },
    clientEvents: ["doctor"], serverEvents: ["doctor/health"], correlation: "none",
    run() {
      const state = createInitialState();
      const sent = captureSend("doctor-load");
      handleSubmitText(state, "/doctor", sent.send);
      expect(state.doctorHealth.loading, "Doctor 请求后未进入 loading");
      const snapshot = doctorSnapshot();
      reduceServerEvent(state, { type: "doctor/health", payload: snapshot });
      expect(!state.doctorHealth.loading && state.doctorHealth.snapshot === snapshot, "Doctor 快照未结束 loading");
    },
  },
  "permission.focus": {
    probeId: "permission_focus_anchor_probe",
    statePaths: ["followTail", "route.name", "route.originAnchor", "scrollOffset"],
    trigger: { kind: "command", value: "/permissions" },
    clientEvents: ["permissions_panel"], serverEvents: [], correlation: "none",
    run() {
      const state = createInitialState();
      state.scrollOffset = 9;
      state.followTail = false;
      const sent = captureSend("permission-focus");
      handleSubmitText(state, "/permissions", sent.send);
      expect(state.route.name === "permissions" && state.route.originAnchor.scrollOffset === 9, "权限路由未保存锚点");
      handlePermissionCenterKey(state, INPUT_KEYS.escape, sent.send);
      expect(state.route.name === "conversation", "权限中心 Escape 未返回会话");
      expect(state.scrollOffset === 9 && state.followTail === false, "权限中心未恢复锚点");
    },
  },
  "permission.keyboard": {
    probeId: "permission_refresh_key_probe",
    statePaths: ["permissionCenter.error", "permissionCenter.limit", "permissionCenter.loading", "route.name"],
    trigger: { kind: "key", value: "r" },
    clientEvents: ["permissions_panel"], serverEvents: [], correlation: "none",
    run() {
      const state = createInitialState();
      state.route = { name: "permissions", originAnchor: null };
      state.permissionCenter.limit = 17;
      state.permissionCenter.error = "旧错误";
      const sent = captureSend("permission-refresh");
      expect(handlePermissionCenterKey(state, "r", sent.send), "r 必须由权限路由消费");
      expectEvent(sent.events, "permissions_panel", { limit: 17 });
      expect(state.permissionCenter.loading && state.permissionCenter.error === "", "权限刷新未建立干净 loading 态");
    },
  },
  "permission.loading": {
    probeId: "permission_loading_terminal_probe",
    statePaths: ["permissionCenter.error", "permissionCenter.loading", "permissionCenter.snapshot"],
    trigger: { kind: "command", value: "/permissions" },
    clientEvents: ["permissions_panel"], serverEvents: ["permissions/snapshot"], correlation: "none",
    run() {
      const state = createInitialState();
      const sent = captureSend("permission-load");
      handleSubmitText(state, "/permissions", sent.send);
      expect(state.permissionCenter.loading, "权限请求后未进入 loading");
      const snapshot = permissionSnapshot();
      reduceServerEvent(state, { type: "permissions/snapshot", payload: snapshot });
      expect(!state.permissionCenter.loading && state.permissionCenter.snapshot === snapshot, "权限快照未结束 loading");
    },
  },
  "task.cancel": {
    probeId: "task_cancel_selection_probe",
    statePaths: ["taskPanel.items", "taskPanel.selectedId", "taskPanel.selectedIndex"],
    trigger: { kind: "function", value: "cancelTaskPanelItem" },
    clientEvents: ["task_cancel"], serverEvents: [], correlation: "none",
    run() {
      const state = stateWithTaskSnapshot();
      const sent = captureSend("task-cancel");
      expect(cancelTaskPanelItem(state, sent.send), "选中任务必须可取消");
      expectEvent(sent.events, "task_cancel", { task_id: "job-1", source: "background", reason: "用户从任务面板取消。" });
    },
  },
  "task.detail": {
    probeId: "task_detail_correlated_probe",
    statePaths: ["taskPanel.detailId", "taskPanel.loading", "taskPanel.requestId", "taskPanel.selectedId"],
    trigger: { kind: "function", value: "openSelectedTaskPanelItem" },
    clientEvents: ["task_panel"], serverEvents: ["error", "tasks/snapshot"], correlation: "request_id",
    run() {
      const state = stateWithTaskSnapshot();
      const sent = captureSend("task-detail");
      expect(openSelectedTaskPanelItem(state, sent.send), "选中任务必须可打开详情");
      expect(state.taskPanel.detailId === "job-1", "详情身份未取自结构化 task_id");
      expect(state.taskPanel.loading && state.taskPanel.requestId === "task-detail-1", "详情请求未关联 loading");
      expectEvent(sent.events, "task_panel", { limit: 12, source: "all", status: "all", pinned: false, refresh: true, detail_id: "job-1" });
    },
  },
  "task.error": {
    probeId: "task_execution_error_probe",
    statePaths: ["activeTaskSubmission.requestId", "activeTaskSubmission.state", "messages[].taskStatus"],
    trigger: { kind: "server_event", value: "error" },
    clientEvents: ["task_submit"], serverEvents: ["error", "task/created"], correlation: "request_id",
    run() {
      const state = createInitialState();
      const sent = captureSend("task-submit", { preferExplicitId: true });
      const message = submitTaskMessage(state, "执行后失败", sent.send);
      reduceServerEvent(state, {
        type: "task/created",
        request_id: message.requestId,
        payload: { mission: { id: "mission-1" }, task: { id: "11", status: "in_progress" }, issue: { task_id: "11" } },
      });
      reduceServerEvent(state, {
        type: "error",
        request_id: message.requestId,
        payload: { code: "run_failed", message: "模型执行失败", intent: "task", task_id: "11", task_status: "blocked" },
      });
      expect(state.activeTaskSubmission.requestId === message.requestId, "任务终态丢失请求身份");
      expect(state.activeTaskSubmission.state === "blocked" && message.taskStatus === "blocked", "执行错误未阻断匹配任务");
      expect(message.deliveryStatus === "accepted" && !message.localOutbox, "已接纳任务被错误退回发送队列");
    },
  },
  "task.focus": {
    probeId: "task_focus_arbitration_probe",
    statePaths: ["inspector.focused", "taskPanel.focused", "taskPanel.items", "taskPanel.messageId"],
    trigger: { kind: "function", value: "setTaskPanelFocus" },
    clientEvents: [], serverEvents: [], correlation: "none",
    run() {
      const state = stateWithTaskSnapshot();
      state.taskPanel.focused = false;
      state.inspector.focused = true;
      expect(setTaskPanelFocus(state, true), "有结构化项目的任务面板必须可聚焦");
      expect(state.taskPanel.focused && !state.inspector.focused, "任务与 Inspector 焦点未互斥");
    },
  },
  "task.keyboard": {
    probeId: "task_keyboard_selection_probe",
    statePaths: ["taskPanel.items", "taskPanel.selectedId", "taskPanel.selectedIndex"],
    trigger: { kind: "function", value: "selectTaskPanelOffset" },
    clientEvents: [], serverEvents: [], correlation: "none",
    run() {
      const state = stateWithTaskSnapshot(true);
      expect(selectTaskPanelOffset(state, 1), "任务选择必须可向下移动");
      expect(state.taskPanel.selectedIndex === 1 && state.taskPanel.selectedId === "todo:todo-2", "任务键盘选择未使用稳定 view_id");
      expect(selectTaskPanelOffset(state, 1), "任务选择必须可循环");
      expect(state.taskPanel.selectedIndex === 0 && state.taskPanel.selectedId === "background:job-1", "任务选择未在边界循环");
    },
  },
  "task.loading": {
    probeId: "task_loading_correlated_terminal_probe",
    statePaths: ["taskPanel.error", "taskPanel.loading", "taskPanel.requestId", "taskPanel.snapshot"],
    trigger: { kind: "command", value: "/tasks" },
    clientEvents: ["task_panel"], serverEvents: ["error", "tasks/snapshot"], correlation: "request_id",
    run() {
      const state = createInitialState();
      const sent = captureSend("task-load");
      handleSubmitText(state, "/tasks", sent.send);
      expect(state.taskPanel.loading && state.taskPanel.requestId === "task-load-1", "任务请求未进入关联 loading");
      reduceServerEvent(state, { type: "error", request_id: "other", payload: { message: "无关错误" } });
      expect(state.taskPanel.loading, "无关错误提前结束任务 loading");
      const snapshot = taskSnapshot();
      reduceServerEvent(state, { type: "tasks/snapshot", request_id: "stale-task-load", payload: snapshot });
      expect(state.taskPanel.loading && state.taskPanel.snapshot === null, "过期任务快照提前结束当前 loading");
      reduceServerEvent(state, { type: "tasks/snapshot", request_id: "task-load-1", payload: snapshot });
      expect(!state.taskPanel.loading && state.taskPanel.snapshot === snapshot, "任务快照未结束 loading");
      handleSubmitText(state, "/tasks refresh", sent.send);
      reduceServerEvent(state, { type: "error", request_id: "task-load-2", payload: { message: "刷新失败" } });
      expect(!state.taskPanel.loading && state.taskPanel.error === "刷新失败", "匹配错误未形成任务失败终态");
      expect(state.taskPanel.snapshot === snapshot, "任务刷新失败未保留最后好快照");
    },
  },
};

export function verifyUiStateMapping(manifest, semanticMapping, protocolContract, options) {
  const findings = new Set();
  const candidate = manifest && typeof manifest === "object" && !Array.isArray(manifest)
    ? manifest
    : {};
  const projectRoot = path.resolve(options.projectRoot);
  const stateModulePath = safeProjectPath(projectRoot, options.stateModulePath);
  if (!validManifestShape(candidate)) findings.add("mapping_shape_invalid");

  const semanticDigest = sha256Json(semanticMapping);
  const contractDigest = sha256Json(protocolContract);
  const stateDigest = sha256Bytes(readFileSync(stateModulePath));
  if (candidate.semantic_mapping_sha256 !== semanticDigest) findings.add("mapping_binding_mismatch");
  if (candidate.protocol_contract_sha256 !== contractDigest) findings.add("protocol_contract_stale");
  if (candidate.state_module_sha256 !== stateDigest) findings.add("state_module_stale");

  const uiLocalCells = (semanticMapping.responsibilities || [])
    .filter((item) => item.owner === "ui_local")
    .map((item) => item.cell_id)
    .sort();
  const mappings = Array.isArray(candidate.mappings) ? candidate.mappings : [];
  const declaredCells = mappings.map((item) => item?.cell_id).sort();
  if (!sameJson(uiLocalCells, declaredCells) || !sameJson(uiLocalCells, Object.keys(PROBES).sort())) {
    findings.add("ui_local_coverage_mismatch");
  }

  const clientEvents = new Set(protocolContract.client_events || []);
  const serverEvents = new Set(protocolContract.server_events || []);
  const clientRegistry = protocolContract.event_registry?.client || {};
  const serverRegistry = protocolContract.event_registry?.server || {};
  let verifiedProbes = 0;
  let verifiedTests = 0;
  for (const mapping of mappings) {
    if (!mapping || typeof mapping !== "object") continue;
    if ((mapping.client_events || []).some((event) => !clientEvents.has(event) || !clientRegistry[event])) findings.add("client_event_missing");
    if ((mapping.server_events || []).some((event) => !serverEvents.has(event) || !serverRegistry[event])) findings.add("server_event_missing");
    for (const target of mapping.target_tests || []) {
      let targetPath;
      try {
        targetPath = safeProjectPath(projectRoot, target.path);
      } catch {
        findings.add("target_path_missing");
        continue;
      }
      let source;
      try {
        source = readFileSync(targetPath, "utf8");
      } catch {
        findings.add("target_path_missing");
        continue;
      }
      if (!declaresNodeTest(source, target.test_name)) findings.add("target_test_missing");
      else verifiedTests += 1;
    }
    const probe = PROBES[mapping.cell_id];
    if (!probe || !matchesProbeContract(mapping, probe)) {
      findings.add("probe_contract_mismatch");
      continue;
    }
    try {
      const override = options.probeOverrides?.[mapping.cell_id];
      (override?.run || probe.run)();
      verifiedProbes += 1;
    } catch {
      findings.add("probe_failed");
    }
  }

  const sortedFindings = [...findings].filter((item) => FINDING_CODES.has(item)).sort();
  const invalid = sortedFindings.some((item) => !STALE_FINDINGS.has(item));
  const status = invalid ? "invalid" : sortedFindings.length ? "stale" : "valid";
  const base = {
    schema_version: 1,
    mapping_sha256: sha256Json(candidate),
    semantic_mapping_sha256: semanticDigest,
    protocol_contract_sha256: contractDigest,
    state_module_sha256: stateDigest,
    status,
    finding_codes: sortedFindings,
    ui_local_cell_count: uiLocalCells.length,
    mapping_count: mappings.length,
    verified_probe_count: verifiedProbes,
    verified_target_test_count: verifiedTests,
  };
  return { audit_id: sha256Json(base), ...base };
}

function validManifestShape(manifest) {
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) return false;
  if (!sameJson(Object.keys(manifest).sort(), ROOT_KEYS)) return false;
  if (manifest.schema_version !== 1 || manifest.manifest_kind !== "claude_code_ui_state_mapping") return false;
  if (manifest.scope !== "cc_03_2b_ui_local_semantics" || !Array.isArray(manifest.mappings)) return false;
  if (!isSha(manifest.semantic_mapping_sha256) || !isSha(manifest.protocol_contract_sha256) || !isSha(manifest.state_module_sha256)) return false;
  if (manifest.mappings.length !== 14) return false;
  if (!sameJson(manifest.mappings.map((item) => item.cell_id), manifest.mappings.map((item) => item.cell_id).slice().sort())) return false;
  return manifest.mappings.every((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return false;
    if (!sameJson(Object.keys(item).sort(), MAPPING_KEYS)) return false;
    if (!item.cell_id || !item.probe_id || !visibleLine(item.semantic)) return false;
    if (!item.trigger || !sameJson(Object.keys(item.trigger).sort(), TRIGGER_KEYS)) return false;
    if (!visibleLine(item.trigger.kind) || !visibleLine(item.trigger.value)) return false;
    if (!["none", "request_id"].includes(item.correlation)) return false;
    if (![item.state_paths, item.client_events, item.server_events, item.target_tests].every(Array.isArray)) return false;
    if (!sortedUnique(item.state_paths) || !sortedUnique(item.client_events) || !sortedUnique(item.server_events)) return false;
    const sortedTargets = item.target_tests.slice().sort((left, right) => (
      `${left?.path || ""}\0${left?.test_name || ""}`.localeCompare(
        `${right?.path || ""}\0${right?.test_name || ""}`,
      )
    ));
    return item.target_tests.length >= 1
      && sameJson(item.target_tests, sortedTargets)
      && new Set(item.target_tests.map((target) => `${target?.path || ""}\0${target?.test_name || ""}`)).size === item.target_tests.length
      && item.target_tests.every((target) => (
      target && sameJson(Object.keys(target).sort(), TEST_KEYS)
      && visibleLine(target.path) && visibleLine(target.test_name)
      ));
  });
}

function matchesProbeContract(mapping, probe) {
  return mapping.probe_id === probe.probeId
    && sameJson(mapping.state_paths, probe.statePaths)
    && sameJson(mapping.trigger, probe.trigger)
    && sameJson(mapping.client_events, probe.clientEvents)
    && sameJson(mapping.server_events, probe.serverEvents)
    && mapping.correlation === probe.correlation;
}

function captureSend(prefix, options = {}) {
  const events = [];
  return {
    events,
    send(type, payload, sendOptions = {}) {
      events.push({ type, payload, options: sendOptions });
      if (options.preferExplicitId && sendOptions.id) return sendOptions.id;
      return `${prefix}-${events.length}`;
    },
  };
}

function expect(value, message) {
  if (!value) throw new Error(message);
}

function expectEvent(events, type, payload) {
  const event = events.find((item) => item.type === type);
  expect(Boolean(event), `缺少 ${type} 事件`);
  expect(sameJson(event.payload, payload), `${type} payload 不匹配`);
}

function doctorSnapshot() {
  return { schema_version: 1, status: "ok", generated_at: "fixture", live_probe: false, snapshot_sha256: "a".repeat(64), items: [] };
}

function permissionSnapshot() {
  return { schema_version: 1, runtime_mode: "bypass", permission_mode: "bypass", pending: [], grants: [], history: [], warnings: [] };
}

function taskSnapshot(includeSecond = false) {
  const items = [{
    view_id: "background:job-1", source: "background", task_id: "job-1", status: "running",
    raw_status: "running", title: "索引工作区", owner: "main", age_seconds: 1,
    dependency_ids: [], child_ids: [], detail: "phase=running", artifact_refs: [],
  }];
  if (includeSecond) items.push({
    view_id: "todo:todo-2", source: "todo", task_id: "todo-2", status: "pending",
    raw_status: "pending", title: "运行定向验证", owner: "main", age_seconds: 0,
    dependency_ids: ["job-1"], child_ids: [], detail: "phase=pending", artifact_refs: [],
  });
  return { schema_version: 1, filters: { source: "all", status: "all", detail_id: "", history: false }, items, timeline: [], warnings: [] };
}

function stateWithTaskSnapshot(includeSecond = false) {
  const state = createInitialState();
  reduceServerEvent(state, { type: "tasks/snapshot", payload: taskSnapshot(includeSecond) });
  return state;
}

function declaresNodeTest(source, testName) {
  const escaped = String(testName).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`\\btest\\(\\s*[\"']${escaped}[\"']`).test(source);
}

function safeProjectPath(projectRoot, candidate) {
  const resolved = path.resolve(projectRoot, candidate);
  if (resolved !== projectRoot && !resolved.startsWith(`${projectRoot}${path.sep}`)) {
    throw new Error("路径越出项目根目录");
  }
  return resolved;
}

function sortedUnique(values) {
  return values.every(visibleLine) && sameJson(values, [...new Set(values)].sort());
}

function visibleLine(value) {
  return typeof value === "string" && value.length > 0 && value === value.trim() && !/[\u0000-\u001f]/.test(value);
}

function isSha(value) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function sameJson(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function sha256Bytes(value) {
  return createHash("sha256").update(value).digest("hex");
}

function sha256Json(value) {
  return sha256Bytes(Buffer.from(canonicalJson(value), "utf8"));
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function parseArgs(argv) {
  const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../../..");
  const defaults = {
    mapping: "frontend/terminal-ui/cc-ui-state-mapping.v1.json",
    semanticMapping: "frontend/terminal-ui/cc-semantic-mapping.v1.json",
    protocolContract: "frontend/terminal-ui/protocol-contract.json",
    stateModule: "frontend/terminal-ui/src/state.js",
    projectRoot: root,
  };
  const names = new Map([
    ["--mapping", "mapping"],
    ["--semantic-mapping", "semanticMapping"],
    ["--protocol-contract", "protocolContract"],
    ["--state-module", "stateModule"],
    ["--project-root", "projectRoot"],
  ]);
  for (let index = 0; index < argv.length; index += 2) {
    const key = names.get(argv[index]);
    if (!key || argv[index + 1] === undefined) throw new Error(`未知或不完整参数: ${argv[index] || "-"}`);
    defaults[key] = argv[index + 1];
  }
  defaults.projectRoot = path.resolve(defaults.projectRoot);
  return defaults;
}

function runCli() {
  const args = parseArgs(process.argv.slice(2));
  const mapping = JSON.parse(readFileSync(safeProjectPath(args.projectRoot, args.mapping), "utf8"));
  const semantic = JSON.parse(readFileSync(safeProjectPath(args.projectRoot, args.semanticMapping), "utf8"));
  const contract = JSON.parse(readFileSync(safeProjectPath(args.projectRoot, args.protocolContract), "utf8"));
  const audit = verifyUiStateMapping(mapping, semantic, contract, {
    projectRoot: args.projectRoot,
    stateModulePath: args.stateModule,
  });
  process.stdout.write(`${JSON.stringify(audit)}\n`);
  process.exitCode = audit.status === "valid" ? 0 : 1;
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) runCli();
