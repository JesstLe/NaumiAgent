import fs from "node:fs";
import { StringDecoder } from "node:string_decoder";

export const PROTOCOL_CONTRACT = loadProtocolContract();
export const PROTOCOL_VERSION = Number(PROTOCOL_CONTRACT.version);

const CLIENT_EVENT_TYPES = new Set(PROTOCOL_CONTRACT.client_events ?? []);
const SERVER_EVENT_TYPES = new Set(PROTOCOL_CONTRACT.server_events ?? []);
const INSPECTOR_TAB_NAMES = ["plan", "tools", "context", "changes", "tests"];
const INSPECTOR_STATES = new Set(["ready", "empty", "loading", "stale", "error"]);
const AGENT_CONTROL_SECTIONS = ["summary", "agents", "executions", "team_messages", "blackboard", "warnings"];
const AGENT_KINDS = new Set(["preset", "dynamic"]);
const AGENT_STATES = new Set(["uninitialized", "spawned", "ready", "running", "idle", "destroyed"]);
const EXECUTION_STATUSES = new Set(["running", "stopping", "completed", "error", "failed", "timeout", "max_turns", "cancelled"]);
const EXECUTION_PHASES = new Set(["starting", "running", "preparing_tool", "running_tool", "stopping", "finished"]);
const TEAM_PRIORITIES = new Set(["low", "normal", "high", "critical"]);

export function parseArgs(argv) {
  const parsed = { config: "config.yaml", bridgeCommand: "", bridgeCommandJson: "" };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if ((arg === "--config" || arg === "-c") && argv[i + 1]) {
      parsed.config = argv[i + 1];
      i += 1;
    } else if (arg === "--bridge-command" && argv[i + 1]) {
      parsed.bridgeCommand = argv[i + 1];
      i += 1;
    } else if (arg === "--bridge-command-json" && argv[i + 1]) {
      parsed.bridgeCommandJson = argv[i + 1];
      i += 1;
    }
  }
  return parsed;
}

export function parseBridgeCommandJson(value) {
  if (!value) return [];
  const parsed = JSON.parse(value);
  if (!Array.isArray(parsed) || parsed.some((item) => typeof item !== "string" || item.length === 0)) {
    throw new Error("--bridge-command-json 必须是非空字符串数组");
  }
  return parsed;
}

export function splitShellLike(command) {
  return command.match(/(?:[^\s"]+|"[^"]*")+/g)?.map((part) => part.replace(/^"|"$/g, "")) ?? [];
}

export function createEventSender(writable, { debugLog = null } = {}) {
  let nextClientId = 1;
  return function send(type, payload, options = {}) {
    if (!CLIENT_EVENT_TYPES.has(type)) {
      throw new Error(`未知客户端事件: ${type}`);
    }
    const id = options.id ? String(options.id) : `ui-${nextClientId++}`;
    const record = {
      id,
      type,
      version: PROTOCOL_VERSION,
      payload,
    };
    const line = `${JSON.stringify(record)}\n`;
    debugLog?.log("protocol.send", { record, line });
    writable.write(line);
    return record.id;
  };
}

function loadProtocolContract() {
  const contractUrl = new URL("../protocol-contract.json", import.meta.url);
  const contract = JSON.parse(fs.readFileSync(contractUrl, "utf8"));
  if (!contract || typeof contract !== "object" || Array.isArray(contract)) {
    throw new Error("protocol-contract.json 必须是对象");
  }
  if (!Number.isInteger(Number(contract.version)) || Number(contract.version) <= 0) {
    throw new Error("protocol-contract.json 缺少有效 version");
  }
  for (const key of ["client_events", "server_events"]) {
    if (!Array.isArray(contract[key]) || contract[key].some((item) => typeof item !== "string" || !item)) {
      throw new Error(`protocol-contract.json ${key} 必须是非空字符串数组`);
    }
  }
  return contract;
}

export function normalizeServerRecord(record) {
  if (!record || typeof record !== "object" || Array.isArray(record)) {
    throw new Error("Bridge 事件必须是对象");
  }
  const type = String(record.type ?? "");
  if (!type) {
    throw new Error("Bridge 事件缺少 type 字段");
  }
  if (!SERVER_EVENT_TYPES.has(type)) {
    throw new Error(`未知 Bridge 事件: ${type}`);
  }
  if (record.version != null && Number(record.version) !== PROTOCOL_VERSION) {
    throw new Error(`Bridge 协议版本不兼容: ${record.version}`);
  }
  const payload = record.payload ?? {};
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("Bridge payload 必须是对象");
  }
  const normalized = {
    ...record,
    type,
    version: PROTOCOL_VERSION,
    payload: normalizeServerPayload(type, payload),
  };
  if (normalized.id != null) normalized.id = String(normalized.id);
  if (normalized.request_id != null) normalized.request_id = String(normalized.request_id);
  if (normalized.seq != null) normalized.seq = Number(normalized.seq);
  return normalized;
}

function normalizeServerPayload(type, payload) {
  if (type === "ready" || type === "runtime/status") {
    return normalizeRuntimeStatus(payload, type);
  }
  if (type === "user/message") {
    return { ...payload, content: String(payload.content ?? "") };
  }
  if (type === "task/created") {
    return {
      ...payload,
      mission: normalizeObject(payload.mission),
      task: normalizeObject(payload.task),
      issue: normalizeObject(payload.issue),
      workbench_snapshot: normalizeObject(payload.workbench_snapshot),
    };
  }
  if (type === "run/cancelled") {
    return {
      ...payload,
      target_request_id: String(payload.target_request_id ?? ""),
      intent: payload.intent === "task" ? "task" : "chat",
      task_id: String(payload.task_id ?? ""),
      mission_id: String(payload.mission_id ?? ""),
      task_status: String(payload.task_status ?? ""),
      reason: String(payload.reason ?? ""),
      receipt_id: String(payload.receipt_id ?? ""),
      run_id: String(payload.run_id ?? ""),
    };
  }
  if (type === "completion/receipt") {
    return normalizeCompletionReceipt(payload);
  }
  if (type === "inspector/snapshot") {
    return normalizeInspectorSnapshot(payload);
  }
  if (type === "inspector/update") {
    return normalizeInspectorUpdate(payload);
  }
  if (type === "agents/snapshot") {
    return normalizeAgentControlSnapshot(payload);
  }
  if (type === "agents/update") {
    return normalizeAgentControlUpdate(payload);
  }
  if (type === "agents/action") {
    return normalizeAgentAction(payload);
  }
  if (type === "ui/message") {
    const messageType = String(payload.type ?? "");
    if (!messageType) {
      throw new Error("ui/message payload 缺少 type 字段");
    }
    return { ...payload, type: messageType };
  }
  if (type === "mode/changed") {
    return {
      ...payload,
      mode: String(payload.mode ?? "").trim().toLowerCase(),
      status: normalizeRuntimeStatus(payload.status, "mode/changed.status"),
    };
  }
  if (type === "permission/resolved") {
    return {
      ...payload,
      request_id: String(payload.request_id ?? ""),
      choice: String(payload.choice ?? "").trim().toLowerCase(),
    };
  }
  if (type === "permission/grants_changed") {
    return {
      revoked: Number(payload.revoked ?? 0),
      grants: Array.isArray(payload.grants)
        ? payload.grants
          .filter((grant) => grant && typeof grant === "object" && !Array.isArray(grant))
          .map((grant) => ({
            ...grant,
            grant_id: String(grant.grant_id ?? ""),
            tool_family: String(grant.tool_family ?? ""),
          }))
        : [],
    };
  }
  if (type === "session/replayed") {
    return {
      ...payload,
      session_id: String(payload.session_id ?? ""),
      title: String(payload.title ?? ""),
      message_count: Number(payload.message_count ?? 0),
      clear: payload.clear == null ? true : toBool(payload.clear),
    };
  }
  if (type === "run/completed") {
    return {
      ...payload,
      status: String(payload.status ?? ""),
      response: String(payload.response ?? ""),
      error: String(payload.error ?? ""),
      receipt_id: String(payload.receipt_id ?? ""),
      run_id: String(payload.run_id ?? ""),
    };
  }
  if (type === "error") {
    return {
      ...payload,
      message: String(payload.message ?? "未知错误"),
      code: String(payload.code ?? "error"),
    };
  }
  if (type === "debug/trace") {
    return {
      ...payload,
      run_id: String(payload.run_id ?? ""),
      run_dir: String(payload.run_dir ?? ""),
      events_path: String(payload.events_path ?? ""),
      transcript_path: String(payload.transcript_path ?? ""),
    };
  }
  if (type === "workbench/snapshot") {
    return {
      ...payload,
      session_id: String(payload.session_id ?? ""),
      missions: Array.isArray(payload.missions) ? payload.missions : [],
      tasks: Array.isArray(payload.tasks) ? payload.tasks : [],
      issues: Array.isArray(payload.issues) ? payload.issues : [],
      failures: Array.isArray(payload.failures) ? payload.failures : [],
      events: Array.isArray(payload.events) ? payload.events : [],
    };
  }
  if (type === "workbench/event") {
    return {
      ...payload,
      id: String(payload.id ?? ""),
      type: String(payload.type ?? ""),
      actor: String(payload.actor ?? ""),
      subject_id: String(payload.subject_id ?? ""),
      payload: normalizeObject(payload.payload),
      timestamp: String(payload.timestamp ?? ""),
    };
  }
  return { ...payload };
}

function normalizeRuntimeStatus(payload, source = "runtime/status") {
  const status = normalizeObject(payload);
  const normalized = { ...status };
  for (const key of [
    "version",
    "workspace_root",
    "model",
    "provider",
    "api_format",
    "upstream_model",
    "mode",
    "permission_mode",
  ]) {
    if (!Object.hasOwn(status, key)) continue;
    const text = strictStatusText(status[key], `${source}.${key}`);
    normalized[key] = ["mode", "permission_mode"].includes(key)
      ? text.toLowerCase()
      : text;
  }
  if (Object.hasOwn(status, "budget")) {
    normalized.budget = normalizeBudgetStatus(status.budget, `${source}.budget`);
  }
  return normalized;
}

export function normalizeBudgetStatus(value, source = "budget") {
  const budget = requireObject(value, source);
  const maxUsd = optionalBudgetNumber(budget.max_usd, `${source}.max_usd`);
  const maxInputTokens = optionalBudgetInteger(
    budget.max_input_tokens,
    `${source}.max_input_tokens`,
  );
  const maxOutputTokens = optionalBudgetInteger(
    budget.max_output_tokens,
    `${source}.max_output_tokens`,
  );
  const enabled = Object.hasOwn(budget, "enabled")
    ? strictBudgetBoolean(budget.enabled, `${source}.enabled`)
    : [maxUsd, maxInputTokens, maxOutputTokens].some((limit) => limit !== null);
  return {
    enabled,
    used_usd: budgetNumber(budget.used_usd ?? 0, `${source}.used_usd`),
    max_usd: maxUsd,
    remaining_usd: optionalBudgetNumber(
      budget.remaining_usd,
      `${source}.remaining_usd`,
    ),
    cost_percentage: optionalBudgetNumber(
      budget.cost_percentage,
      `${source}.cost_percentage`,
    ),
    input_tokens: budgetInteger(
      budget.input_tokens ?? 0,
      `${source}.input_tokens`,
    ),
    max_input_tokens: maxInputTokens,
    input_percentage: optionalBudgetNumber(
      budget.input_percentage,
      `${source}.input_percentage`,
    ),
    output_tokens: budgetInteger(
      budget.output_tokens ?? 0,
      `${source}.output_tokens`,
    ),
    max_output_tokens: maxOutputTokens,
    output_percentage: optionalBudgetNumber(
      budget.output_percentage,
      `${source}.output_percentage`,
    ),
    percentage: optionalBudgetNumber(budget.percentage, `${source}.percentage`),
  };
}

function budgetNumber(value, name) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new Error(`${name} 必须是非负有限数字`);
  }
  return value;
}

function optionalBudgetNumber(value, name) {
  if (value == null) return null;
  return budgetNumber(value, name);
}

function budgetInteger(value, name) {
  const parsed = budgetNumber(value, name);
  if (!Number.isInteger(parsed)) throw new Error(`${name} 必须是非负整数`);
  return parsed;
}

function optionalBudgetInteger(value, name) {
  if (value == null) return null;
  return budgetInteger(value, name);
}

function strictBudgetBoolean(value, name) {
  if (typeof value !== "boolean") throw new Error(`${name} 必须是布尔值`);
  return value;
}

function strictStatusText(value, name) {
  if (typeof value !== "string") {
    throw new Error(`${name} 必须是字符串`);
  }
  return publicText(value);
}

function normalizeObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function normalizeCompletionReceipt(payload) {
  if (Number(payload.schema_version) !== 1) {
    throw new Error(`completion/receipt schema_version 不兼容: ${payload.schema_version}`);
  }
  const receiptId = String(payload.receipt_id ?? "").trim();
  const runId = String(payload.run_id ?? "").trim();
  const outcome = String(payload.outcome ?? "").trim().toLowerCase();
  if (!receiptId || !runId) {
    throw new Error("completion/receipt 缺少 receipt_id 或 run_id");
  }
  if (!["completed", "partial", "failed", "cancelled"].includes(outcome)) {
    throw new Error(`completion/receipt outcome 无效: ${outcome}`);
  }
  const gitState = normalizeObject(payload.git_state);
  const changes = normalizeObjectArray(payload.changes, 100).map((item) => ({
    ...item,
    scope: item.scope === "background" ? "background" : "task",
  }));
  return {
    schema_version: 1,
    receipt_id: receiptId,
    run_id: runId,
    outcome,
    summary: String(payload.summary ?? ""),
    changes,
    validations: normalizeObjectArray(payload.validations, 50),
    unverified: normalizeTextArray(payload.unverified, 50),
    approvals: normalizeObjectArray(payload.approvals, 50),
    risks: normalizeObjectArray(payload.risks, 50),
    git_state: {
      ...gitState,
      ...(Object.hasOwn(gitState, "available") ? { available: toBool(gitState.available) } : {}),
      ...(Object.hasOwn(gitState, "dirty") ? { dirty: toBool(gitState.dirty) } : {}),
      ...(Object.hasOwn(gitState, "ahead") ? { ahead: nonnegativeNumber(gitState.ahead) } : {}),
      ...(Object.hasOwn(gitState, "behind") ? { behind: nonnegativeNumber(gitState.behind) } : {}),
    },
    next_actions: normalizeObjectArray(payload.next_actions, 50),
    evidence_refs: normalizeTextArray(payload.evidence_refs, 100),
    started_at: String(payload.started_at ?? ""),
    completed_at: String(payload.completed_at ?? ""),
    duration_ms: nonnegativeNumber(payload.duration_ms),
  };
}

function normalizeInspectorSnapshot(payload) {
  const header = normalizeInspectorHeader(payload);
  const normalized = { ...header };
  for (const tab of INSPECTOR_TAB_NAMES) {
    if (!Object.hasOwn(payload, tab)) {
      throw new Error(`inspector/snapshot 缺少 ${tab} 标签`);
    }
    normalized[tab] = normalizeInspectorTab(tab, payload[tab]);
  }
  return normalized;
}

function normalizeInspectorUpdate(payload) {
  const header = normalizeInspectorHeader(payload);
  const rawTabs = requireObject(payload.changed_tabs, "inspector/update changed_tabs");
  const changedTabs = {};
  for (const [tab, value] of Object.entries(rawTabs)) {
    if (!INSPECTOR_TAB_NAMES.includes(tab)) {
      throw new Error(`未知 Inspector 标签: ${tab}`);
    }
    changedTabs[tab] = normalizeInspectorTab(tab, value);
  }
  if (Object.keys(changedTabs).length === 0) {
    throw new Error("inspector/update changed_tabs 不能为空");
  }
  return { ...header, changed_tabs: changedTabs };
}

function normalizeInspectorHeader(payload) {
  if (Number(payload.schema_version) !== 1) {
    throw new Error(`Inspector schema_version 不兼容: ${payload.schema_version}`);
  }
  const revision = strictNonnegativeInteger(payload.revision, "Inspector revision");
  return {
    schema_version: 1,
    session_id: publicText(payload.session_id),
    revision,
    generated_at: publicText(payload.generated_at),
    active_run_id: publicText(payload.active_run_id),
  };
}

function normalizeInspectorTab(name, value) {
  const tab = requireObject(value, `${name} 标签`);
  const state = publicText(tab.state).toLowerCase();
  if (!INSPECTOR_STATES.has(state)) {
    throw new Error(`Inspector ${name}.state 无效: ${state}`);
  }
  const warnings = strictTextArray(tab.warnings, `${name}.warnings`, 20);
  if (name === "plan") {
    return {
      state,
      items: strictObjectArray(tab.items, "plan.items", 50).map(normalizeInspectorTodo),
      next_actions: strictObjectArray(tab.next_actions, "plan.next_actions", 50).map(normalizeInspectorAction),
      warnings,
    };
  }
  if (name === "tools") {
    return {
      state,
      items: strictObjectArray(tab.items, "tools.items", 50).map(normalizeInspectorTool),
      approvals: strictObjectArray(tab.approvals, "tools.approvals", 50).map(normalizeInspectorApproval),
      warnings,
    };
  }
  if (name === "context") {
    return {
      state,
      workspace_root: publicText(tab.workspace_root),
      branch: publicText(tab.branch),
      commit: publicText(tab.commit),
      git_available: toBool(tab.git_available),
      git_dirty: toBool(tab.git_dirty),
      model: publicText(tab.model),
      runtime_mode: publicText(tab.runtime_mode),
      permission_mode: publicText(tab.permission_mode),
      context_used: strictNonnegativeInteger(tab.context_used, "context.context_used"),
      context_window: strictNonnegativeInteger(tab.context_window, "context.context_window"),
      context_percentage: nonnegativeNumber(tab.context_percentage),
      budget_enabled: strictBudgetBoolean(
        tab.budget_enabled ?? (tab.budget_max_usd != null),
        "context.budget_enabled",
      ),
      budget_used_usd: budgetNumber(tab.budget_used_usd ?? 0, "context.budget_used_usd"),
      budget_max_usd: optionalBudgetNumber(tab.budget_max_usd, "context.budget_max_usd"),
      budget_percentage: optionalBudgetNumber(
        tab.budget_percentage,
        "context.budget_percentage",
      ),
      budget_max_input_tokens: optionalBudgetInteger(
        tab.budget_max_input_tokens,
        "context.budget_max_input_tokens",
      ),
      budget_max_output_tokens: optionalBudgetInteger(
        tab.budget_max_output_tokens,
        "context.budget_max_output_tokens",
      ),
      input_tokens: strictNonnegativeInteger(tab.input_tokens, "context.input_tokens"),
      output_tokens: strictNonnegativeInteger(tab.output_tokens, "context.output_tokens"),
      turns: strictNonnegativeInteger(tab.turns, "context.turns"),
      warnings,
    };
  }
  if (name === "changes") {
    const git = requireObject(tab.git_state, "changes.git_state");
    return {
      state,
      source_run_id: publicText(tab.source_run_id),
      receipt_id: publicText(tab.receipt_id),
      summary: publicText(tab.summary),
      items: strictObjectArray(tab.items, "changes.items", 100).map(normalizeInspectorChange),
      git_state: {
        available: toBool(git.available),
        branch: publicText(git.branch),
        dirty: toBool(git.dirty),
        commit: publicText(git.commit),
        ahead: strictNonnegativeInteger(git.ahead ?? 0, "changes.git_state.ahead"),
        behind: strictNonnegativeInteger(git.behind ?? 0, "changes.git_state.behind"),
      },
      warnings,
    };
  }
  return {
    state,
    source_run_id: publicText(tab.source_run_id),
    receipt_id: publicText(tab.receipt_id),
    validations: strictObjectArray(tab.validations, "tests.validations", 50).map(normalizeInspectorValidation),
    unverified: strictTextArray(tab.unverified, "tests.unverified", 50),
    next_actions: strictObjectArray(tab.next_actions, "tests.next_actions", 50).map(normalizeInspectorAction),
    warnings,
  };
}

function normalizeInspectorTodo(item) {
  return {
    id: publicText(item.id),
    subject: publicText(item.subject),
    status: publicText(item.status),
    active_form: publicText(item.active_form),
    owner: publicText(item.owner),
    blocked_by: strictTextArray(item.blocked_by, "plan.item.blocked_by", 50),
  };
}

function normalizeInspectorTool(item) {
  return {
    call_id: publicText(item.call_id),
    name: publicText(item.name),
    status: publicText(item.status),
    summary: publicText(item.summary),
    duration_ms: strictNonnegativeInteger(item.duration_ms ?? 0, "tools.item.duration_ms"),
    run_id: publicText(item.run_id),
  };
}

function normalizeInspectorApproval(item) {
  return {
    request_id: publicText(item.request_id),
    tool_name: publicText(item.tool_name),
    decision: publicText(item.decision),
    reason: publicText(item.reason),
    run_id: publicText(item.run_id),
  };
}

function normalizeInspectorChange(item) {
  return {
    path: publicText(item.path),
    status: publicText(item.status),
    source_tool: publicText(item.source_tool),
    additions: strictNonnegativeInteger(item.additions ?? 0, "changes.item.additions"),
    deletions: strictNonnegativeInteger(item.deletions ?? 0, "changes.item.deletions"),
  };
}

function normalizeInspectorValidation(item) {
  return {
    command: publicText(item.command),
    scope: publicText(item.scope),
    status: publicText(item.status),
    exit_code: item.exit_code == null
      ? null
      : strictInteger(item.exit_code, "tests.validation.exit_code"),
    passed: strictNonnegativeInteger(item.passed ?? 0, "tests.validation.passed"),
    failed: strictNonnegativeInteger(item.failed ?? 0, "tests.validation.failed"),
    skipped: strictNonnegativeInteger(item.skipped ?? 0, "tests.validation.skipped"),
    log_ref: publicText(item.log_ref),
  };
}

function normalizeInspectorAction(item) {
  return {
    id: publicText(item.id),
    label: publicText(item.label),
    kind: publicText(item.kind),
  };
}

function normalizeAgentControlSnapshot(payload) {
  const header = normalizeAgentControlHeader(payload);
  for (const section of AGENT_CONTROL_SECTIONS) {
    if (!Object.hasOwn(payload, section)) {
      throw new Error(`agents/snapshot 缺少 ${section}`);
    }
  }
  return {
    ...header,
    summary: normalizeAgentSummary(payload.summary),
    agents: agentObjectArray(payload.agents, "agents", 100).map(normalizeAgentDescriptor),
    executions: agentObjectArray(payload.executions, "executions", 100).map(normalizeExecutionDescriptor),
    team_messages: agentObjectArray(payload.team_messages, "team_messages", 100).map(normalizeTeamMessage),
    blackboard: agentObjectArray(payload.blackboard, "blackboard", 100).map(normalizeBlackboard),
    warnings: agentTextArray(payload.warnings, "warnings", 20),
  };
}

function normalizeAgentControlUpdate(payload) {
  const header = normalizeAgentControlHeader(payload);
  const raw = agentObject(payload.changed_sections, "changed_sections");
  const changedSections = {};
  for (const [section, value] of Object.entries(raw)) {
    if (!AGENT_CONTROL_SECTIONS.includes(section)) {
      throw new Error(`未知 Agent Control section: ${section}`);
    }
    if (section === "summary") changedSections.summary = normalizeAgentSummary(value);
    else if (section === "agents") changedSections.agents = agentObjectArray(value, "agents", 100).map(normalizeAgentDescriptor);
    else if (section === "executions") changedSections.executions = agentObjectArray(value, "executions", 100).map(normalizeExecutionDescriptor);
    else if (section === "team_messages") changedSections.team_messages = agentObjectArray(value, "team_messages", 100).map(normalizeTeamMessage);
    else if (section === "blackboard") changedSections.blackboard = agentObjectArray(value, "blackboard", 100).map(normalizeBlackboard);
    else changedSections.warnings = agentTextArray(value, "warnings", 20);
  }
  if (Object.keys(changedSections).length === 0) {
    throw new Error("agents/update changed_sections 不能为空");
  }
  return { ...header, changed_sections: changedSections };
}

function normalizeAgentControlHeader(payload) {
  if (payload.schema_version !== 1) {
    throw new Error(`Agent Control schema_version 不兼容: ${payload.schema_version}`);
  }
  return {
    schema_version: 1,
    session_id: agentText(payload.session_id),
    revision: strictAgentNonnegativeInteger(payload.revision, "Agent Control revision"),
    generated_at: agentText(payload.generated_at),
  };
}

function normalizeAgentSummary(value) {
  const summary = agentObject(value, "summary");
  return {
    total_agents: strictAgentNonnegativeInteger(summary.total_agents, "summary.total_agents"),
    active_agents: strictAgentNonnegativeInteger(summary.active_agents, "summary.active_agents"),
    attention_agents: strictAgentNonnegativeInteger(summary.attention_agents, "summary.attention_agents"),
    stoppable_executions: strictAgentNonnegativeInteger(summary.stoppable_executions, "summary.stoppable_executions"),
    pending_messages: strictAgentNonnegativeInteger(summary.pending_messages, "summary.pending_messages"),
  };
}

function normalizeAgentDescriptor(item) {
  const kind = strictChoice(item.kind, "agent.kind", AGENT_KINDS);
  const state = strictChoice(item.state, "agent.state", AGENT_STATES);
  return {
    name: requiredAgentText(item.name, "agent.name"),
    description: agentText(item.description),
    kind,
    state,
    task_count: strictAgentNonnegativeInteger(item.task_count ?? 0, "agent.task_count"),
    model_tier: agentText(item.model_tier),
    capabilities: agentTextArray(item.capabilities, "agent.capabilities", 50),
    tools: agentTextArray(item.tools, "agent.tools", 50),
    permission_level: agentText(item.permission_level),
    age_ms: strictAgentNonnegativeInteger(item.age_ms ?? 0, "agent.age_ms"),
    heartbeat_age_ms: strictAgentNonnegativeInteger(item.heartbeat_age_ms ?? 0, "agent.heartbeat_age_ms"),
  };
}

function normalizeExecutionDescriptor(item) {
  const finishedAt = item.finished_at;
  return {
    task_id: requiredAgentText(item.task_id, "execution.task_id"),
    session_id: agentText(item.session_id),
    agent_name: requiredAgentText(item.agent_name, "execution.agent_name"),
    description: agentText(item.description),
    status: strictChoice(item.status, "execution.status", EXECUTION_STATUSES),
    phase: strictChoice(item.phase, "execution.phase", EXECUTION_PHASES),
    started_at: strictNonnegativeNumber(item.started_at, "execution.started_at"),
    finished_at: finishedAt == null ? null : strictNonnegativeNumber(finishedAt, "execution.finished_at"),
    elapsed_ms: strictAgentNonnegativeInteger(item.elapsed_ms ?? 0, "execution.elapsed_ms"),
    heartbeat_age_ms: strictAgentNonnegativeInteger(item.heartbeat_age_ms ?? 0, "execution.heartbeat_age_ms"),
    current_tool: agentText(item.current_tool),
    recent_tools: agentTextArray(item.recent_tools, "execution.recent_tools", 20),
    total_tokens: strictAgentNonnegativeInteger(item.total_tokens ?? 0, "execution.total_tokens"),
    total_cost_usd: strictNonnegativeNumber(item.total_cost_usd ?? 0, "execution.total_cost_usd"),
    turns: strictAgentNonnegativeInteger(item.turns ?? 0, "execution.turns"),
    error: agentText(item.error),
    stop_supported: strictBoolean(item.stop_supported, "execution.stop_supported"),
    stop_requested: strictBoolean(item.stop_requested, "execution.stop_requested"),
  };
}

function normalizeTeamMessage(item) {
  return {
    sender: requiredAgentText(item.sender, "team_message.sender"),
    recipient: agentText(item.recipient),
    topic: requiredAgentText(item.topic, "team_message.topic"),
    priority: strictChoice(item.priority, "team_message.priority", TEAM_PRIORITIES),
    timestamp: strictNonnegativeNumber(item.timestamp, "team_message.timestamp"),
    content: agentText(item.content),
  };
}

function normalizeBlackboard(item) {
  return {
    key: requiredAgentText(item.key, "blackboard.key"),
    author: requiredAgentText(item.author, "blackboard.author"),
    version: strictAgentNonnegativeInteger(item.version, "blackboard.version"),
    timestamp: strictNonnegativeNumber(item.timestamp, "blackboard.timestamp"),
    value_summary: agentText(item.value_summary),
  };
}

function normalizeAgentAction(payload) {
  return {
    task_id: requiredAgentText(payload.task_id, "agents/action task_id"),
    accepted: strictBoolean(payload.accepted, "agents/action accepted"),
    code: requiredAgentText(payload.code, "agents/action code"),
    message: agentText(payload.message),
  };
}

function agentObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`Agent Control ${name} 必须是对象`);
  }
  return value;
}

function agentObjectArray(value, name, limit) {
  if (!Array.isArray(value)) throw new Error(`Agent Control ${name} 必须是数组`);
  if (value.length > limit) throw new Error(`Agent Control ${name} 最多 ${limit} 项`);
  return value.map((item) => agentObject(item, name));
}

function agentTextArray(value, name, limit) {
  if (!Array.isArray(value)) throw new Error(`Agent Control ${name} 必须是数组`);
  if (value.length > limit) throw new Error(`Agent Control ${name} 最多 ${limit} 项`);
  return value.map((item) => {
    if (typeof item !== "string") {
      throw new Error(`Agent Control ${name} 必须只包含字符串`);
    }
    return agentText(item);
  });
}

function agentText(value) {
  return String(value ?? "").trim().slice(0, 2000);
}

function requiredAgentText(value, name) {
  const result = agentText(value);
  if (!result) throw new Error(`Agent Control ${name} 不能为空`);
  return result;
}

function strictChoice(value, name, allowed) {
  const result = agentText(value).toLowerCase();
  if (!allowed.has(result)) throw new Error(`Agent Control ${name} 无效: ${result}`);
  return result;
}

function strictBoolean(value, name) {
  if (typeof value !== "boolean") throw new Error(`Agent Control ${name} 必须是 boolean`);
  return value;
}

function strictNonnegativeNumber(value, name) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new Error(`${name} 必须是非负数`);
  }
  return value;
}

function strictAgentNonnegativeInteger(value, name) {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    throw new Error(`${name} 必须是非负整数`);
  }
  return value;
}

function strictObjectArray(value, name, limit) {
  if (!Array.isArray(value)) throw new Error(`Inspector ${name} 必须是数组`);
  return value.slice(0, limit).map((item) => requireObject(item, name));
}

function strictTextArray(value, name, limit) {
  if (!Array.isArray(value)) throw new Error(`Inspector ${name} 必须是数组`);
  return value.slice(0, limit).map((item) => publicText(item));
}

function requireObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`Inspector ${name} 必须是对象`);
  }
  return value;
}

function strictNonnegativeInteger(value, name) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 0) {
    throw new Error(`${name} 必须是非负整数`);
  }
  return parsed;
}

function strictInteger(value, name) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed)) {
    throw new Error(`${name} 必须是整数`);
  }
  return parsed;
}

function publicText(value) {
  return String(value ?? "").trim().slice(0, 500);
}

function normalizeObjectArray(value, limit) {
  if (!Array.isArray(value)) return [];
  return value.slice(0, limit).map((item) => ({ ...normalizeObject(item) }));
}

function normalizeTextArray(value, limit) {
  if (!Array.isArray(value)) return [];
  return value.slice(0, limit).map((item) => String(item ?? ""));
}

function nonnegativeNumber(value) {
  const parsed = Number(value ?? 0);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
}

function toBool(value) {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    return ["1", "true", "yes", "y", "on"].includes(value.trim().toLowerCase());
  }
  return false;
}

export function attachJsonlLineReader(stream, onLine) {
  const decoder = new StringDecoder("utf8");
  let buffer = "";
  stream.on("data", (chunk) => {
    buffer += typeof chunk === "string" ? chunk : decoder.write(chunk);
    while (true) {
      const index = buffer.indexOf("\n");
      if (index < 0) return;
      const line = buffer.slice(0, index).replace(/\r$/, "");
      buffer = buffer.slice(index + 1);
      onLine(line);
    }
  });
}
