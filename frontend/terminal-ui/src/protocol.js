import { StringDecoder } from "node:string_decoder";
import { createHash } from "node:crypto";
import EMBEDDED_PROTOCOL_CONTRACT from "../protocol-contract.json" with { type: "json" };


export const PROTOCOL_CONTRACT = loadProtocolContract();
export const PROTOCOL_VERSION = Number(PROTOCOL_CONTRACT.version);
export const PROTOCOL_REGISTRY_SHA256 = protocolRegistryDigest(PROTOCOL_CONTRACT);

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
const REASONING_EFFORTS = new Set(["auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"]);
const REASONING_EFFORT_SOURCES = new Set(["runtime", "model", "global", "auto"]);
const MODEL_CONTRACT_STATUSES = new Set(["verified", "partial", "unverified", "incompatible"]);
const HARNESS_LOOKUP_STATUSES = new Set(["ok", "not_found", "unavailable"]);
const HARNESS_RUN_STATUSES = new Set([
  "running",
  "completed_verified",
  "completed_unverified",
  "blocked",
]);
const HARNESS_FAILURE_CLASSES = new Set([
  "specification_gap",
  "knowledge_gap",
  "context_overflow",
  "tool_contract_error",
  "permission_block",
  "environment_error",
  "implementation_error",
  "verification_failure",
  "evaluation_error",
  "agent_premature_finish",
  "agent_repetition",
  "human_judgment_required",
]);
const HARNESS_CRITERION_STATUSES = new Set(["satisfied", "unsatisfied"]);
const HARNESS_REPLAY_STATUSES = new Set(["reproduced", "changed", "partial", "corrupt"]);
const HARNESS_ARTIFACT_STATUSES = new Set([
  "verified",
  "missing",
  "digest_mismatch",
  "unsafe_path",
  "unsupported",
  "malformed",
  "unreadable",
]);
const HARNESS_EVAL_BASELINE_STATUSES = new Set(["ok", "empty", "unavailable"]);
const HARNESS_EVAL_DECISIONS = new Set([
  "passed",
  "failed",
  "flaky",
  "inconclusive",
  "incompatible",
]);
const HARNESS_EVAL_BATCH_STAGES = new Set([
  "preparing",
  "evaluating",
  "persisting",
  "completed",
  "partial",
  "error",
]);
const HARNESS_EVAL_PROMOTION_STAGES = new Set([
  "awaiting_reason",
  "awaiting_confirmation",
  "promoted",
  "already_active",
  "not_selected",
  "cancelled",
  "error",
]);
const DOCTOR_HEALTH_SEVERITIES = new Set(["ok", "degraded", "error", "unknown"]);
const DOCTOR_HEALTH_DOMAINS = new Set([
  "runtime", "model", "provider", "store", "git", "node", "browser", "mcp", "terminal",
]);
const DOCTOR_HEALTH_RESPONSIBILITIES = new Set([
  "user_config", "local_environment", "external_service", "product_runtime", "unknown",
]);
const PERMISSION_RUNTIME_MODES = new Set(["default", "plan", "bypass"]);
const PERMISSION_MODES = new Set(["bypass", "permissive", "moderate", "strict", "lockdown"]);
const PERMISSION_RISKS = new Set(["", "low", "medium", "high"]);
const PERMISSION_STATUSES = new Set([
  "needs_confirmation",
  "allowed",
  "confirmed",
  "denied",
  "blocked",
  "bypass_enabled",
  "granted",
  "cancelled",
  "expired",
]);
const PERMISSION_CHOICES = new Set(["allow_once", "deny", "grant_session", "bypass"]);
const TASK_SOURCES = new Set(["todo", "subagent", "background", "browser"]);
const TASK_STATUSES = new Set(["pending", "running", "blocked", "completed", "failed", "cancelled"]);
const GOAL_STATUSES = new Set(["active", "paused", "blocked", "completed", "cancelled"]);
const PURSUIT_STATUSES = new Set([
  "running", "waiting", "blocked", "completed", "failed", "cancelled", "budget_exceeded",
]);
const PURSUIT_LINK_STATUSES = new Set(["not_linked", "ready", "missing"]);
const PURSUIT_RECOVERY_STATES = new Set([
  "active", "waiting", "blocked", "reconcile_required", "orphaned",
  "inconsistent", "terminal", "unknown",
]);
const PURSUIT_HEARTBEAT_HEALTH = new Set([
  "starting", "healthy", "draining", "stale", "offline", "stopped",
  "failed", "clock_regression", "missing", "error",
]);
const PURSUIT_LEASE_STATUSES = new Set(["active", "released", "missing", "error"]);
const PURSUIT_CHECKPOINT_STATUSES = new Set(["ready", "missing", "error"]);

export function parseArgs(argv) {
  const parsed = { config: ".naumi/config.yaml", bridgeCommand: "", bridgeCommandJson: "", selfTest: false };
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
    } else if (arg === "--self-test") {
      parsed.selfTest = true;
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
  const contract = structuredClone(EMBEDDED_PROTOCOL_CONTRACT);
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
  const negotiation = contract.negotiation;
  if (!negotiation || typeof negotiation !== "object" || Array.isArray(negotiation)) {
    throw new Error("protocol-contract.json 缺少 negotiation 对象");
  }
  for (const key of ["minimum_version", "maximum_version"]) {
    if (!Number.isInteger(negotiation[key]) || negotiation[key] <= 0) {
      throw new Error(`protocol-contract.json negotiation.${key} 必须是正整数`);
    }
  }
  if (negotiation.minimum_version > negotiation.maximum_version) {
    throw new Error("protocol-contract.json negotiation 版本区间无效");
  }
  for (const key of ["capabilities", "required_capabilities"]) {
    if (!Array.isArray(negotiation[key])
      || negotiation[key].some((item) => typeof item !== "string" || !/^[a-z][a-z0-9_]{0,63}$/.test(item))) {
      throw new Error(`protocol-contract.json negotiation.${key} 必须是合法能力数组`);
    }
    negotiation[key] = [...new Set(negotiation[key])].sort();
  }
  if (negotiation.required_capabilities.some((item) => !negotiation.capabilities.includes(item))) {
    throw new Error("protocol-contract.json required_capabilities 必须是 capabilities 的子集");
  }
  validateEventRegistry(contract);
  return contract;
}

export function validateEventRegistry(contract) {
  const registry = contract?.event_registry;
  if (!registry || typeof registry !== "object" || Array.isArray(registry)) {
    throw new Error("protocol-contract.json 缺少 event_registry 对象");
  }
  if (JSON.stringify(Object.keys(registry).sort()) !== JSON.stringify(["client", "server"])) {
    throw new Error("protocol-contract.json event_registry 只能包含 client/server");
  }
  const allowed = {
    owner: new Set(["protocol", "runtime", "harness", "inspector", "agents", "safety", "workbench", "evolution", "diagnostics", "sessions", "tasks", "ui"]),
    stability: new Set(["stable", "experimental", "deprecated"]),
    criticality: new Set(["informational", "control", "terminal"]),
    persistence: new Set(["never", "timeline", "snapshot", "audit"]),
    redaction: new Set(["none", "required"]),
  };
  const requiredFields = ["owner", "stability", "criticality", "persistence", "sensitive_fields", "redaction"];
  for (const [direction, eventKey] of [["client", "client_events"], ["server", "server_events"]]) {
    const policies = registry[direction];
    if (!policies || typeof policies !== "object" || Array.isArray(policies)) {
      throw new Error(`event_registry.${direction} 必须是对象`);
    }
    const expected = [...contract[eventKey]].sort();
    const actual = Object.keys(policies).sort();
    if (JSON.stringify(actual) !== JSON.stringify(expected)) {
      throw new Error(`event_registry.${direction} 必须精确覆盖 ${eventKey}`);
    }
    for (const [eventType, policy] of Object.entries(policies)) {
      if (!policy || typeof policy !== "object" || Array.isArray(policy)) {
        throw new Error(`event_registry ${direction}:${eventType} policy 必须是对象`);
      }
      if (JSON.stringify(Object.keys(policy).sort()) !== JSON.stringify([...requiredFields].sort())) {
        throw new Error(`event_registry ${direction}:${eventType} policy 字段不完整`);
      }
      for (const field of ["owner", "stability", "criticality", "persistence", "redaction"]) {
        if (!allowed[field].has(policy[field])) {
          throw new Error(`event_registry ${direction}:${eventType} ${field} 无效`);
        }
      }
      if (!Array.isArray(policy.sensitive_fields)
        || policy.sensitive_fields.some((field) => typeof field !== "string" || !/^payload(?:\.[a-z][a-z0-9_]*)+$/.test(field))
        || new Set(policy.sensitive_fields).size !== policy.sensitive_fields.length) {
        throw new Error(`event_registry ${direction}:${eventType} sensitive_fields 无效`);
      }
      if ((policy.sensitive_fields.length > 0) !== (policy.redaction === "required")) {
        throw new Error(`event_registry ${direction}:${eventType} redaction 与敏感字段不一致`);
      }
    }
  }
  return true;
}

export function eventPolicy(direction, eventType) {
  if (!new Set(["client", "server"]).has(direction)) {
    throw new Error(`未知事件方向: ${direction}`);
  }
  const policy = PROTOCOL_CONTRACT.event_registry[direction]?.[String(eventType ?? "")];
  if (!policy) throw new Error(`未注册 ${direction} 事件: ${eventType}`);
  return structuredClone(policy);
}

function protocolRegistryDigest(contract) {
  const canonical = canonicalJson({
    client: contract.event_registry.client,
    server: contract.event_registry.server,
  });
  return createHash("sha256").update(canonical, "utf8").digest("hex");
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map(
      (key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`,
    ).join(",")}}`;
  }
  return JSON.stringify(value);
}

export function createHelloPayload(client = "naumi-terminal-ui") {
  const name = String(client ?? "").trim() || "naumi-terminal-ui";
  if (name.length > 100) throw new Error("hello client 不能超过 100 个字符");
  return {
    client: name,
    minimum_version: PROTOCOL_CONTRACT.negotiation.minimum_version,
    maximum_version: PROTOCOL_CONTRACT.negotiation.maximum_version,
    capabilities: [...PROTOCOL_CONTRACT.negotiation.capabilities],
  };
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
  if (type === "ack" && payload.event === "hello") {
    return { ...payload, negotiation: normalizeHelloNegotiation(payload.negotiation) };
  }
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
  if (type === "harness/receipt") {
    return normalizeHarnessReceipt(payload);
  }
  if (type === "harness/explain") {
    return normalizeHarnessExplain(payload);
  }
  if (type === "harness/replay") {
    return normalizeHarnessReplay(payload);
  }
  if (type === "harness/eval-baseline") {
    return normalizeHarnessEvalBaseline(payload);
  }
  if (type === "harness/eval-batch") {
    return normalizeHarnessEvalBatch(payload);
  }
  if (type === "harness/eval-promotion") {
    return normalizeHarnessEvalPromotion(payload);
  }
  if (type === "doctor/health") {
    return normalizeDoctorHealth(payload);
  }
  if (type === "permissions/snapshot") {
    return normalizePermissionSnapshot(payload);
  }
  if (type === "evolution/review") {
    return normalizeEvolutionReview(payload);
  }
  if (type === "goals/snapshot") {
    return normalizeGoalSnapshot(payload);
  }
  if (type === "workbench/review") {
    return normalizeWorkbenchReview(payload);
  }
  if (type === "workbench/proposal/action_result") {
    return normalizeWorkbenchProposalActionResult(payload);
  }
  if (type === "tasks/snapshot") {
    return normalizeTaskSnapshot(payload);
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
  if (type === "interaction/request") {
    return {
      request_id: String(payload.request_id ?? ""),
      header: String(payload.header ?? ""),
      question: String(payload.question ?? ""),
      options: Array.isArray(payload.options)
        ? payload.options
          .filter((option) => option && typeof option === "object" && !Array.isArray(option))
          .map((option) => ({
            value: String(option.value ?? ""),
            label: String(option.label ?? ""),
            description: String(option.description ?? ""),
          }))
        : [],
      allow_custom: toBool(payload.allow_custom),
      custom_label: String(payload.custom_label ?? "其他"),
    };
  }
  if (type === "interaction/resolved") {
    return {
      request_id: String(payload.request_id ?? ""),
      status: String(payload.status ?? "answered"),
      kind: String(payload.kind ?? "option"),
      value: String(payload.value ?? ""),
      label: String(payload.label ?? ""),
      custom_text: String(payload.custom_text ?? ""),
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
    const counts = normalizeObject(payload.counts);
    const selection = normalizeObject(payload.active_selection);
    const worktrees = Array.isArray(payload.worktrees)
      ? payload.worktrees.slice(0, 200).map(normalizeWorkbenchWorktree)
      : [];
    const worktreesStatus = ["ready", "unavailable"].includes(payload.worktrees_status)
      ? payload.worktrees_status
      : (Array.isArray(payload.worktrees) ? "ready" : "unavailable");
    const worktreesTotal = Math.max(worktrees.length, Number(payload.worktrees_total) || 0);
    return {
      ...payload,
      schema_version: Number(payload.schema_version) || 1,
      stream_id: String(payload.stream_id ?? ""),
      revision: Math.max(0, Number(payload.revision) || 0),
      generated_at: String(payload.generated_at ?? ""),
      full: payload.full !== false,
      session_id: String(payload.session_id ?? ""),
      counts: {
        missions: Math.max(0, Number(counts.missions) || 0),
        tasks: Math.max(0, Number(counts.tasks) || 0),
        worktrees: Math.max(0, Number(counts.worktrees) || 0),
        reviews: Math.max(0, Number(counts.reviews) || 0),
        failures: Math.max(0, Number(counts.failures) || 0),
      },
      active_selection: {
        mission_id: String(selection.mission_id ?? ""),
        task_id: String(selection.task_id ?? ""),
        worktree: String(selection.worktree ?? ""),
        review_id: String(selection.review_id ?? ""),
        review_kind: ["approval", "proposal"].includes(selection.review_kind)
          ? selection.review_kind
          : "",
      },
      worktrees_status: worktreesStatus,
      worktrees_code: String(payload.worktrees_code ?? "").slice(0, 120),
      worktrees_total: worktreesTotal,
      worktrees_truncated: payload.worktrees_truncated === true || worktreesTotal > worktrees.length,
      worktrees,
      missions: Array.isArray(payload.missions) ? payload.missions : [],
      tasks: Array.isArray(payload.tasks) ? payload.tasks : [],
      issues: Array.isArray(payload.issues) ? payload.issues : [],
      leases: normalizeObjectArray(payload.leases, 200),
      validation_runs: normalizeObjectArray(payload.validation_runs, 200),
      approvals: Array.isArray(payload.approvals)
        ? payload.approvals.slice(0, 100).map(normalizeWorkbenchApproval)
        : [],
      proposals: Array.isArray(payload.proposals)
        ? harnessObjectArray(payload.proposals, "workbench/snapshot proposals", 200)
          .map(normalizeWorkbenchProposal)
        : [],
      failures: Array.isArray(payload.failures) ? payload.failures : [],
      events: Array.isArray(payload.events) ? payload.events : [],
    };
  }
  if (type === "workbench/event") {
    return {
      ...payload,
      session_id: String(payload.session_id ?? ""),
      stream_id: String(payload.stream_id ?? ""),
      revision: Math.max(0, Number(payload.revision) || 0),
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

function normalizeWorkbenchApproval(value) {
  const item = normalizeObject(value);
  return {
    id: String(item.id ?? "").slice(0, 500),
    session_id: String(item.session_id ?? "").slice(0, 500),
    mission_id: String(item.mission_id ?? "").slice(0, 500),
    task_id: String(item.task_id ?? "").slice(0, 500),
    state: item.state === "waiting" ? "waiting" : String(item.state ?? "").slice(0, 80),
    title: String(item.title ?? "").slice(0, 2_000),
    detail: String(item.detail ?? "").slice(0, 10_000),
    requester: String(item.requester ?? "").slice(0, 500),
    reviewer: String(item.reviewer ?? "").slice(0, 500),
    decision_note: String(item.decision_note ?? "").slice(0, 10_000),
    created_at: String(item.created_at ?? "").slice(0, 100),
    updated_at: String(item.updated_at ?? "").slice(0, 100),
  };
}

function normalizeWorkbenchProposal(value) {
  const item = harnessObject(value, "workbench proposal");
  return {
    id: workbenchText(item.id, "workbench proposal.id", 128),
    session_id: workbenchText(item.session_id, "workbench proposal.session_id", 500),
    mission_id: workbenchText(item.mission_id, "workbench proposal.mission_id", 500),
    task_id: workbenchText(item.task_id, "workbench proposal.task_id", 500),
    agent_id: workbenchText(item.agent_id, "workbench proposal.agent_id", 500),
    title: workbenchText(item.title, "workbench proposal.title", 2_000),
    impact_scope: workbenchText(item.impact_scope, "workbench proposal.impact_scope", 4_000),
    intended_files: harnessTextArray(item.intended_files, "workbench proposal.intended_files", 200),
    validation_plan: harnessTextArray(item.validation_plan, "workbench proposal.validation_plan", 100),
    risk_level: harnessChoice(
      item.risk_level,
      "workbench proposal.risk_level",
      new Set(["low", "medium", "high", "critical"]),
    ),
    questions: harnessTextArray(item.questions, "workbench proposal.questions", 100),
    state: harnessChoice(
      item.state,
      "workbench proposal.state",
      new Set(["open", "approved", "rejected", "deferred", "merged", "converted"]),
    ),
    decision_note: workbenchText(item.decision_note, "workbench proposal.decision_note", 2_000),
    source_kind: harnessChoice(
      item.source_kind,
      "workbench proposal.source_kind",
      new Set(["manual", "evolution_candidate"]),
    ),
    source_id: workbenchText(item.source_id, "workbench proposal.source_id", 128),
    source_revision: harnessNonnegativeInteger(item.source_revision, "workbench proposal.source_revision"),
    source_occurrence_count: harnessNonnegativeInteger(
      item.source_occurrence_count,
      "workbench proposal.source_occurrence_count",
    ),
    source_proposal_id: workbenchText(
      item.source_proposal_id,
      "workbench proposal.source_proposal_id",
      128,
    ),
    proposal_kind: workbenchText(item.proposal_kind, "workbench proposal.proposal_kind", 80),
    reviewer: workbenchText(item.reviewer, "workbench proposal.reviewer", 128),
    decision_at: workbenchText(item.decision_at, "workbench proposal.decision_at", 100),
    cooldown_until: workbenchText(item.cooldown_until, "workbench proposal.cooldown_until", 100),
    merged_into_id: workbenchText(item.merged_into_id, "workbench proposal.merged_into_id", 128),
    governance_policy_version: workbenchText(
      item.governance_policy_version,
      "workbench proposal.governance_policy_version",
      80,
    ),
    created_at: workbenchText(item.created_at, "workbench proposal.created_at", 100),
    updated_at: workbenchText(item.updated_at, "workbench proposal.updated_at", 100),
  };
}

function normalizeWorkbenchProposalActionResult(payload) {
  if (Number(payload.schema_version) !== 1) {
    throw new Error(`workbench/proposal/action_result schema_version 不兼容: ${payload.schema_version}`);
  }
  const status = harnessChoice(
    payload.status,
    "workbench/proposal/action_result status",
    new Set(["needs_confirmation", "completed", "blocked", "conflict", "not_found", "error"]),
  );
  return {
    schema_version: 1,
    session_id: harnessText(payload.session_id, "workbench/proposal/action_result session_id"),
    proposal_id: harnessText(payload.proposal_id, "workbench/proposal/action_result proposal_id"),
    action: harnessChoice(
      payload.action,
      "workbench/proposal/action_result action",
      new Set(["approve", "reject"]),
    ),
    status,
    message: workbenchText(payload.message, "workbench/proposal/action_result message", 2_000),
    proposal: payload.proposal == null ? null : normalizeWorkbenchProposal(payload.proposal),
    workbench_snapshot: payload.workbench_snapshot == null
      ? null
      : normalizeServerPayload("workbench/snapshot", payload.workbench_snapshot),
  };
}

function normalizeWorkbenchReview(payload) {
  if (Number(payload.schema_version) !== 1) {
    throw new Error(`workbench/review schema_version 不兼容: ${payload.schema_version}`);
  }
  const status = harnessChoice(
    payload.status,
    "workbench/review status",
    new Set(["ready", "unavailable"]),
  );
  const normalized = {
    schema_version: 1,
    session_id: harnessText(payload.session_id, "workbench/review session_id"),
    review_id: harnessText(payload.review_id, "workbench/review review_id"),
    status,
    code: harnessText(payload.code, "workbench/review code"),
    evidence: null,
  };
  if (status === "unavailable") return normalized;
  const evidence = harnessObject(payload.evidence, "workbench/review evidence");
  const rawApproval = harnessObject(evidence.approval, "workbench/review approval");
  const approval = {
    id: workbenchText(rawApproval.id, "workbench/review approval.id", 500),
    session_id: workbenchText(rawApproval.session_id, "workbench/review approval.session_id", 500),
    mission_id: workbenchText(rawApproval.mission_id, "workbench/review approval.mission_id", 500),
    task_id: workbenchText(rawApproval.task_id, "workbench/review approval.task_id", 500),
    state: workbenchText(rawApproval.state, "workbench/review approval.state", 80),
    title: workbenchText(rawApproval.title, "workbench/review approval.title", 2_000),
    detail: workbenchText(rawApproval.detail, "workbench/review approval.detail", 10_000),
    requester: workbenchText(rawApproval.requester, "workbench/review approval.requester", 500),
    reviewer: workbenchText(rawApproval.reviewer, "workbench/review approval.reviewer", 500),
    decision_note: workbenchText(rawApproval.decision_note, "workbench/review approval.decision_note", 10_000),
    created_at: workbenchText(rawApproval.created_at, "workbench/review approval.created_at", 100),
    updated_at: workbenchText(rawApproval.updated_at, "workbench/review approval.updated_at", 100),
  };
  if (!approval.id || approval.id !== normalized.review_id) {
    throw new Error("workbench/review approval id 不匹配");
  }
  const rawIssue = evidence.issue == null
    ? null
    : harnessObject(evidence.issue, "workbench/review issue");
  const issue = rawIssue == null ? null : {
    id: workbenchText(rawIssue.id, "workbench/review issue.id", 500),
    session_id: workbenchText(rawIssue.session_id, "workbench/review issue.session_id", 500),
    mission_id: workbenchText(rawIssue.mission_id, "workbench/review issue.mission_id", 500),
    task_id: workbenchText(rawIssue.task_id, "workbench/review issue.task_id", 500),
    risk_level: workbenchText(rawIssue.risk_level, "workbench/review issue.risk_level", 80),
    related_branch: workbenchText(rawIssue.related_branch, "workbench/review issue.related_branch", 500),
    related_worktree: workbenchText(rawIssue.related_worktree, "workbench/review issue.related_worktree", 500),
    related_pr: workbenchText(rawIssue.related_pr, "workbench/review issue.related_pr", 500),
  };
  const worktree = harnessObject(evidence.worktree, "workbench/review worktree");
  normalized.evidence = {
    approval,
    issue,
    worktree: {
      name: workbenchText(worktree.name, "workbench/review worktree.name", 500),
      path: workbenchText(worktree.path, "workbench/review worktree.path", 2_000),
      status: harnessChoice(
        worktree.status,
        "workbench/review worktree.status",
        new Set(["present", "missing", "unbound"]),
      ),
    },
    validation_runs: harnessObjectArray(
      evidence.validation_runs,
      "workbench/review validation_runs",
      20,
    ).map((item) => ({
      id: workbenchText(item.id, "workbench/review validation.id", 500),
      status: workbenchText(item.status, "workbench/review validation.status", 80),
      command: Array.isArray(item.command)
        ? harnessTextArray(item.command, "workbench/review validation.command", 30)
          .map((part) => part.slice(0, 500))
        : workbenchText(item.command, "workbench/review validation.command", 2_000),
      exit_code: item.exit_code == null
        ? null
        : harnessNonnegativeInteger(item.exit_code, "workbench/review validation.exit_code"),
      started_at: workbenchText(item.started_at, "workbench/review validation.started_at", 100),
      completed_at: workbenchText(item.completed_at, "workbench/review validation.completed_at", 100),
    })),
    changed_files: harnessObjectArray(
      evidence.changed_files,
      "workbench/review changed_files",
      200,
    ).map((item) => ({
      path: workbenchText(item.path, "workbench/review changed_file.path", 2_000),
      status: harnessChoice(
        item.status,
        "workbench/review changed_file.status",
        new Set(["modified", "added", "deleted", "untracked", "renamed"]),
      ),
    })),
    diff_hunks: harnessObjectArray(
      evidence.diff_hunks,
      "workbench/review diff_hunks",
      30,
    ).map((item) => ({
      path: workbenchText(item.path, "workbench/review diff_hunk.path", 2_000),
      patch: workbenchText(item.patch, "workbench/review diff_hunk.patch", 4_000),
    })),
    agent_notes: harnessObjectArray(evidence.agent_notes, "workbench/review agent_notes", 50)
      .map((item) => ({
        actor: workbenchText(item.actor, "workbench/review agent_note.actor", 500),
        note: workbenchText(item.note, "workbench/review agent_note.note", 2_000),
        type: workbenchText(item.type, "workbench/review agent_note.type", 200),
        timestamp: workbenchText(item.timestamp, "workbench/review agent_note.timestamp", 100),
      })),
    events: harnessObjectArray(evidence.events, "workbench/review events", 50)
      .map((item) => ({
        id: workbenchText(item.id, "workbench/review event.id", 500),
        type: workbenchText(item.type, "workbench/review event.type", 200),
        actor: workbenchText(item.actor, "workbench/review event.actor", 500),
        subject_id: workbenchText(item.subject_id, "workbench/review event.subject_id", 500),
        timestamp: workbenchText(item.timestamp, "workbench/review event.timestamp", 100),
      })),
  };
  return normalized;
}

function workbenchText(value, name, limit) {
  return harnessText(value, name).slice(0, limit);
}

function normalizeWorkbenchWorktree(value) {
  const item = normalizeObject(value);
  const task = normalizeObject(item.task);
  const lease = normalizeObject(item.lease);
  const status = ["clean", "dirty", "missing", "kept"].includes(item.status)
    ? item.status
    : "unknown";
  const normalizedTask = Object.keys(task).length ? {
    id: String(task.id ?? "").slice(0, 200),
    subject: String(task.subject ?? "").slice(0, 1_000),
    status: String(task.status ?? "").slice(0, 80),
    owner: String(task.owner ?? "").slice(0, 200),
  } : null;
  const normalizedLease = Object.keys(lease).length ? {
    id: String(lease.id ?? "").slice(0, 200),
    task_id: String(lease.task_id ?? "").slice(0, 200),
    agent_id: String(lease.agent_id ?? "").slice(0, 200),
    state: String(lease.state ?? "").slice(0, 80),
    expires_at: String(lease.expires_at ?? "").slice(0, 100),
    worktree_name: String(lease.worktree_name ?? "").slice(0, 200),
  } : null;
  return {
    name: String(item.name ?? "").slice(0, 200),
    path: String(item.path ?? "").slice(0, 2_000),
    branch: String(item.branch ?? "").slice(0, 500),
    base_ref: String(item.base_ref ?? "").slice(0, 200),
    status,
    task_id: String(item.task_id ?? "").slice(0, 200),
    dirty_files: Math.max(0, Number(item.dirty_files) || 0),
    commits_ahead: Math.max(0, Number(item.commits_ahead) || 0),
    created_at: String(item.created_at ?? "").slice(0, 100),
    updated_at: String(item.updated_at ?? "").slice(0, 100),
    kept_reason: String(item.kept_reason ?? "").slice(0, 1_000),
    removable: item.removable === true,
    task: normalizedTask,
    lease: normalizedLease,
    agent_id: String(item.agent_id ?? lease.agent_id ?? "").slice(0, 200),
  };
}

function normalizeHelloNegotiation(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("Bridge hello ACK 缺少 negotiation");
  }
  const selected = Number(raw.selected_version);
  const serverMinimum = Number(raw.server_minimum_version);
  const serverMaximum = Number(raw.server_maximum_version);
  if (![selected, serverMinimum, serverMaximum].every(Number.isInteger)
    || serverMinimum <= 0 || serverMaximum < serverMinimum
    || selected < serverMinimum || selected > serverMaximum
    || selected < PROTOCOL_CONTRACT.negotiation.minimum_version
    || selected > PROTOCOL_CONTRACT.negotiation.maximum_version) {
    throw new Error("Bridge hello 协商版本不兼容");
  }
  if (!Array.isArray(raw.capabilities)
    || raw.capabilities.some((item) => typeof item !== "string"
      || !PROTOCOL_CONTRACT.negotiation.capabilities.includes(item))) {
    throw new Error("Bridge hello 协商能力无效");
  }
  const capabilities = [...new Set(raw.capabilities)].sort();
  const missing = PROTOCOL_CONTRACT.negotiation.required_capabilities
    .filter((item) => !capabilities.includes(item));
  if (missing.length > 0) throw new Error(`Bridge hello 缺少必需能力: ${missing.join(", ")}`);
  return {
    selected_version: selected,
    server_minimum_version: serverMinimum,
    server_maximum_version: serverMaximum,
    capabilities,
  };
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
  if (Object.hasOwn(status, "reasoning_effort")) {
    normalized.reasoning_effort = normalizeReasoningEffort(
      status.reasoning_effort,
      `${source}.reasoning_effort`,
    );
  }
  if (Object.hasOwn(status, "model_contract") && status.model_contract != null) {
    normalized.model_contract = normalizeModelContract(
      status.model_contract,
      `${source}.model_contract`,
    );
  }
  if (Object.hasOwn(status, "retention_worker")) {
    normalized.retention_worker = normalizeRetentionWorkerStatus(
      status.retention_worker,
      `${source}.retention_worker`,
    );
  }
  if (Object.hasOwn(status, "evolution_patch_recovery")) {
    normalized.evolution_patch_recovery = normalizePatchRecoveryStatus(
      status.evolution_patch_recovery,
      `${source}.evolution_patch_recovery`,
    );
  }
  if (Object.hasOwn(status, "protocol_registry")) {
    const registry = requireObject(status.protocol_registry, `${source}.protocol_registry`);
    const digest = strictStatusText(
      registry.registry_sha256,
      `${source}.protocol_registry.registry_sha256`,
    );
    if (!/^[0-9a-f]{64}$/.test(digest)) {
      throw new Error(`${source}.protocol_registry.registry_sha256 必须是 SHA-256`);
    }
    const contractVersion = strictPositiveInteger(
      registry.contract_version,
      `${source}.protocol_registry.contract_version`,
    );
    const clientEventCount = strictPositiveInteger(
      registry.client_event_count,
      `${source}.protocol_registry.client_event_count`,
    );
    const serverEventCount = strictPositiveInteger(
      registry.server_event_count,
      `${source}.protocol_registry.server_event_count`,
    );
    if (contractVersion !== PROTOCOL_VERSION
      || digest !== PROTOCOL_REGISTRY_SHA256
      || clientEventCount !== PROTOCOL_CONTRACT.client_events.length
      || serverEventCount !== PROTOCOL_CONTRACT.server_events.length) {
      throw new Error(`${source}.protocol_registry 与内置协议不一致`);
    }
    normalized.protocol_registry = {
      contract_version: contractVersion,
      registry_sha256: digest,
      client_event_count: clientEventCount,
      server_event_count: serverEventCount,
    };
  }
  return normalized;
}

function normalizeRetentionWorkerStatus(value, source) {
  const worker = requireObject(value, source);
  const state = strictStatusText(worker.state, `${source}.state`).toLowerCase();
  const allowedStates = new Set([
    "stopped", "starting", "standby", "running", "waiting", "stopping",
  ]);
  if (!allowedStates.has(state)) throw new Error(`${source}.state 无效: ${state}`);
  const booleanField = (name) => {
    if (typeof worker[name] !== "boolean") {
      throw new Error(`${source}.${name} 必须是 boolean`);
    }
    return worker[name];
  };
  const textField = (name) => {
    if (typeof worker[name] !== "string" || worker[name].length > 256) {
      throw new Error(`${source}.${name} 必须是最多 256 字符的字符串`);
    }
    return worker[name];
  };
  return {
    configured_enabled: booleanField("configured_enabled"),
    owner_id: textField("owner_id"),
    state,
    lease_held: booleanField("lease_held"),
    pass_count: strictNonnegativeInteger(worker.pass_count, `${source}.pass_count`),
    completed_session_count: strictNonnegativeInteger(
      worker.completed_session_count,
      `${source}.completed_session_count`,
    ),
    retry_scheduled_count: strictNonnegativeInteger(
      worker.retry_scheduled_count,
      `${source}.retry_scheduled_count`,
    ),
    failure_count: strictNonnegativeInteger(worker.failure_count, `${source}.failure_count`),
    consecutive_empty_passes: strictNonnegativeInteger(
      worker.consecutive_empty_passes,
      `${source}.consecutive_empty_passes`,
    ),
    next_delay_seconds: strictNonnegativeNumber(
      worker.next_delay_seconds,
      `${source}.next_delay_seconds`,
    ),
    last_pass_status: textField("last_pass_status"),
    last_error_code: textField("last_error_code"),
    started_at: textField("started_at"),
    last_pass_at: textField("last_pass_at"),
  };
}

function normalizePatchRecoveryStatus(value, source) {
  const recovery = requireObject(value, source);
  const count = (name) => strictNonnegativeInteger(recovery[name], `${source}.${name}`);
  const normalized = {
    total: count("total"),
    single_file_total: Object.hasOwn(recovery, "single_file_total")
      ? count("single_file_total")
      : count("total"),
    multi_file_total: Object.hasOwn(recovery, "multi_file_total")
      ? count("multi_file_total")
      : 0,
    completed: count("completed"),
    rolled_back: count("rolled_back"),
    already_baseline: count("already_baseline"),
    orphan_lock_removed: count("orphan_lock_removed"),
    deferred: count("deferred"),
    failed: count("failed"),
    filesystem_changed: count("filesystem_changed"),
    failure_codes: [],
  };
  if (!Array.isArray(recovery.failure_codes) || recovery.failure_codes.length > 32) {
    throw new Error(`${source}.failure_codes 必须是最多 32 项的字符串数组`);
  }
  normalized.failure_codes = recovery.failure_codes.map((item) => (
    strictStatusText(item, `${source}.failure_codes[]`)
  ));
  if (
    normalized.total
    !== normalized.single_file_total + normalized.multi_file_total
  ) {
    throw new Error(`${source}.total 与单/多文件事务分类不一致`);
  }
  if (
    normalized.completed
    !== normalized.rolled_back
      + normalized.already_baseline
      + normalized.orphan_lock_removed
  ) {
    throw new Error(`${source}.completed 与完成分类不一致`);
  }
  if (
    normalized.total
    !== normalized.completed + normalized.deferred + normalized.failed
  ) {
    throw new Error(`${source}.total 与恢复分类不一致`);
  }
  if (normalized.filesystem_changed > normalized.rolled_back) {
    throw new Error(`${source}.filesystem_changed 超过 rolled_back`);
  }
  return normalized;
}

function normalizeModelContract(value, source) {
  const contract = requireObject(value, source);
  const status = strictStatusText(contract.status, `${source}.status`).toLowerCase();
  if (!MODEL_CONTRACT_STATUSES.has(status)) {
    throw new Error(`${source}.status 无效: ${status}`);
  }
  const capability = (name) => {
    const raw = contract[name];
    if (raw == null) return null;
    if (typeof raw !== "boolean") throw new Error(`${source}.${name} 必须是 boolean 或 null`);
    return raw;
  };
  const textArray = (name) => {
    const raw = contract[name];
    if (!Array.isArray(raw) || raw.length > 32 || raw.some((item) => typeof item !== "string")) {
      throw new Error(`${source}.${name} 必须是最多 32 项的字符串数组`);
    }
    return raw.map((item) => strictStatusText(item, `${source}.${name}[]`));
  };
  const fieldSources = requireObject(contract.field_sources, `${source}.field_sources`);
  if (Object.keys(fieldSources).length > 32) {
    throw new Error(`${source}.field_sources 最多 32 项`);
  }
  const normalizedSources = {};
  for (const [key, raw] of Object.entries(fieldSources)) {
    normalizedSources[strictStatusText(key, `${source}.field_sources key`)] = strictStatusText(
      raw,
      `${source}.field_sources.${key}`,
    );
  }
  return {
    requested_model: strictStatusText(contract.requested_model, `${source}.requested_model`),
    canonical_model: strictStatusText(contract.canonical_model, `${source}.canonical_model`),
    upstream_model: strictStatusText(contract.upstream_model, `${source}.upstream_model`),
    provider: strictStatusText(contract.provider, `${source}.provider`),
    api_format: strictStatusText(contract.api_format, `${source}.api_format`),
    max_context: strictPositiveInteger(contract.max_context, `${source}.max_context`),
    max_output: strictPositiveInteger(contract.max_output, `${source}.max_output`),
    request_max_tokens: strictPositiveInteger(contract.request_max_tokens, `${source}.request_max_tokens`),
    input_cost_per_million: strictNonnegativeNumber(contract.input_cost_per_million, `${source}.input_cost_per_million`),
    output_cost_per_million: strictNonnegativeNumber(contract.output_cost_per_million, `${source}.output_cost_per_million`),
    supports_tools: capability("supports_tools"),
    supports_streaming: capability("supports_streaming"),
    supports_parallel_tools: capability("supports_parallel_tools"),
    supports_structured_output: capability("supports_structured_output"),
    supports_reasoning: capability("supports_reasoning"),
    supports_vision: capability("supports_vision"),
    input_modalities: textArray("input_modalities"),
    output_modalities: textArray("output_modalities"),
    field_sources: normalizedSources,
    status,
    warnings: textArray("warnings"),
    errors: textArray("errors"),
  };
}

function strictPositiveInteger(value, name) {
  if (typeof value !== "number" || !Number.isInteger(value) || value <= 0) {
    throw new Error(`${name} 必须是正整数`);
  }
  return value;
}

function normalizeReasoningEffort(value, source) {
  const effort = requireObject(value, source);
  const effective = strictStatusText(effort.effective, `${source}.effective`).toLowerCase();
  const effortSource = strictStatusText(effort.source, `${source}.source`).toLowerCase();
  if (!REASONING_EFFORTS.has(effective)) {
    throw new Error(`${source}.effective 无效: ${effective}`);
  }
  if (!REASONING_EFFORT_SOURCES.has(effortSource)) {
    throw new Error(`${source}.source 无效: ${effortSource}`);
  }
  if (!Array.isArray(effort.supported) || effort.supported.length > 8) {
    throw new Error(`${source}.supported 必须是最多 8 项的字符串数组`);
  }
  const supported = effort.supported.map((item, index) => {
    const normalized = strictStatusText(item, `${source}.supported[${index}]`).toLowerCase();
    if (normalized === "auto" || !REASONING_EFFORTS.has(normalized)) {
      throw new Error(`${source}.supported[${index}] 无效: ${normalized}`);
    }
    return normalized;
  });
  const defaultEffort = effort.default == null
    ? null
    : strictStatusText(effort.default, `${source}.default`).toLowerCase();
  if (defaultEffort !== null && !supported.includes(defaultEffort)) {
    throw new Error(`${source}.default 必须出现在 supported 中`);
  }
  return {
    model: strictStatusText(effort.model ?? "", `${source}.model`),
    effective,
    source: effortSource,
    supported,
    default: defaultEffort,
    warning: effort.warning == null
      ? null
      : strictStatusText(effort.warning, `${source}.warning`),
  };
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
  const runId = publicText(payload.run_id);
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

function normalizeHarnessReceipt(payload) {
  if (Number(payload.schema_version) !== 1) {
    throw new Error(`harness/receipt schema_version 不兼容: ${payload.schema_version}`);
  }
  const runId = String(payload.run_id ?? "").trim();
  if (!runId) throw new Error("harness/receipt 缺少 run_id");
  const status = String(payload.status ?? "").trim().toLowerCase();
  if (!["completed_verified", "completed_unverified", "blocked"].includes(status)) {
    throw new Error(`harness/receipt status 无效: ${status}`);
  }
  const revision = Number(payload.revision);
  if (!Number.isSafeInteger(revision) || revision < 1) {
    throw new Error("harness/receipt revision 必须是正整数");
  }
  return {
    schema_version: 1,
    revision,
    run_id: runId,
    status,
    task_kind: publicText(payload.task_kind),
    changed_files: normalizeTextArray(payload.changed_files, 100).map(publicText),
    checks: normalizeObjectArray(payload.checks, 50).map((item) => ({
      id: publicText(item.id),
      status: publicText(item.status),
      tree_fingerprint: publicText(item.tree_fingerprint),
    })),
    criteria: normalizeObjectArray(payload.criteria, 100).map((item) => ({
      id: publicText(item.id),
      status: publicText(item.status),
      evidence_ids: normalizeTextArray(item.evidence_ids, 100).map(publicText),
    })),
    warnings: normalizeTextArray(payload.warnings, 50).map(publicText),
    tree_fingerprint: publicText(payload.tree_fingerprint),
  };
}

function normalizeHarnessEvalBaseline(payload) {
  if (Number(payload.schema_version) !== 1) {
    throw new Error(`harness/eval-baseline schema_version 不兼容: ${payload.schema_version}`);
  }
  const status = harnessChoice(
    payload.status,
    "harness/eval-baseline status",
    HARNESS_EVAL_BASELINE_STATUSES,
  );
  const suiteId = harnessText(payload.suite_id, "harness/eval-baseline suite_id");
  if (!/^[a-z][a-z0-9_-]{0,63}$/.test(suiteId)) {
    throw new Error("harness/eval-baseline suite_id 无效");
  }
  const snapshotSha256 = harnessSha256(
    payload.snapshot_sha256,
    "harness/eval-baseline snapshot_sha256",
  );
  let active = null;
  if (status === "ok") {
    const value = harnessObject(payload.active, "harness/eval-baseline active");
    const version = harnessNonnegativeInteger(value.version, "harness/eval-baseline active.version");
    const sampleCount = harnessNonnegativeInteger(
      value.sample_count,
      "harness/eval-baseline active.sample_count",
    );
    if (version < 1 || sampleCount < 1) {
      throw new Error("harness/eval-baseline active 版本或样本数无效");
    }
    active = {
      id: harnessSha256(value.id, "harness/eval-baseline active.id"),
      version,
      batch_id: harnessText(value.batch_id, "harness/eval-baseline active.batch_id"),
      sample_count: sampleCount,
      identity_sha256: harnessSha256(
        value.identity_sha256,
        "harness/eval-baseline active.identity_sha256",
      ),
      samples_sha256: harnessSha256(
        value.samples_sha256,
        "harness/eval-baseline active.samples_sha256",
      ),
      promoted_by: harnessText(value.promoted_by, "harness/eval-baseline active.promoted_by"),
      promotion_reason: harnessText(
        value.promotion_reason,
        "harness/eval-baseline active.promotion_reason",
      ),
      created_at: harnessText(value.created_at, "harness/eval-baseline active.created_at"),
    };
  } else if (payload.active !== null && payload.active !== undefined) {
    throw new Error("harness/eval-baseline 非 ok 状态不能包含 active");
  }
  const comparisons = harnessObjectArray(
    payload.comparisons ?? [],
    "harness/eval-baseline comparisons",
    20,
  ).map((item) => ({
    id: harnessSha256(item.id, "harness/eval-baseline comparison.id"),
    baseline_id: harnessSha256(
      item.baseline_id,
      "harness/eval-baseline comparison.baseline_id",
    ),
    current_batch_id: harnessText(
      item.current_batch_id,
      "harness/eval-baseline comparison.current_batch_id",
    ),
    decision: harnessChoice(
      item.decision,
      "harness/eval-baseline comparison.decision",
      HARNESS_EVAL_DECISIONS,
    ),
    statistical_verdict: harnessText(
      item.statistical_verdict,
      "harness/eval-baseline comparison.statistical_verdict",
    ),
    current_samples: harnessNonnegativeInteger(
      item.current_samples,
      "harness/eval-baseline comparison.current_samples",
    ),
    created_at: harnessText(item.created_at, "harness/eval-baseline comparison.created_at"),
  }));
  if (status !== "ok" && comparisons.length) {
    throw new Error("harness/eval-baseline 非 ok 状态不能包含 comparisons");
  }
  if (active && comparisons.some((item) => item.baseline_id !== active.id || item.current_samples < 1)) {
    throw new Error("harness/eval-baseline comparison 与 active 不一致");
  }
  return {
    schema_version: 1,
    snapshot_sha256: snapshotSha256,
    status,
    suite_id: suiteId,
    message: harnessText(payload.message, "harness/eval-baseline message"),
    active,
    comparisons,
  };
}

function normalizeHarnessEvalBatch(payload) {
  if (Number(payload.schema_version) !== 1) {
    throw new Error(`harness/eval-batch schema_version 不兼容: ${payload.schema_version}`);
  }
  const stage = harnessChoice(payload.stage, "harness/eval-batch stage", HARNESS_EVAL_BATCH_STAGES);
  const terminal = harnessBoolean(payload.terminal, "harness/eval-batch terminal");
  if (terminal !== ["completed", "partial", "error"].includes(stage)) {
    throw new Error("harness/eval-batch terminal 与 stage 不一致");
  }
  const requested = harnessPositiveInteger(payload.requested, "harness/eval-batch requested");
  const completed = harnessNonnegativeInteger(payload.completed, "harness/eval-batch completed");
  const persisted = harnessNonnegativeInteger(payload.persisted, "harness/eval-batch persisted");
  if (requested < 5 || requested > 100 || completed > requested || persisted > completed) {
    throw new Error("harness/eval-batch 进度计数无效");
  }
  if (stage === "completed" && (completed !== requested || persisted !== requested)) {
    throw new Error("harness/eval-batch completed 缺少完整样本");
  }
  const identity = harnessText(payload.identity_sha256, "harness/eval-batch identity_sha256");
  if (identity && !/^[0-9a-f]{64}$/.test(identity)) {
    throw new Error("harness/eval-batch identity_sha256 必须是 SHA-256");
  }
  const baselineEligible = harnessBoolean(
    payload.baseline_eligible,
    "harness/eval-batch baseline_eligible",
  );
  if (baselineEligible && (stage !== "completed" || !identity)) {
    throw new Error("harness/eval-batch 仅完整终态可声明 Baseline eligible");
  }
  const batchId = harnessText(payload.batch_id, "harness/eval-batch batch_id");
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(batchId)) {
    throw new Error("harness/eval-batch batch_id 无效");
  }
  const suiteId = harnessText(payload.suite_id, "harness/eval-batch suite_id");
  if (!/^[a-z][a-z0-9_-]{0,63}$/.test(suiteId)) {
    throw new Error("harness/eval-batch suite_id 无效");
  }
  return {
    schema_version: 1,
    stage,
    terminal,
    batch_id: batchId,
    suite_id: suiteId,
    requested,
    completed,
    persisted,
    passed_cases: harnessNonnegativeInteger(payload.passed_cases, "harness/eval-batch passed_cases"),
    implementation_failures: harnessNonnegativeInteger(
      payload.implementation_failures,
      "harness/eval-batch implementation_failures",
    ),
    evaluation_errors: harnessNonnegativeInteger(
      payload.evaluation_errors,
      "harness/eval-batch evaluation_errors",
    ),
    skipped: harnessNonnegativeInteger(payload.skipped, "harness/eval-batch skipped"),
    duration_ms: harnessNonnegativeFiniteNumber(
      payload.duration_ms,
      "harness/eval-batch duration_ms",
    ),
    baseline_eligible: baselineEligible,
    identity_sha256: identity,
    code: harnessText(payload.code, "harness/eval-batch code"),
    message: harnessText(payload.message, "harness/eval-batch message"),
  };
}

function normalizeHarnessEvalPromotion(payload) {
  if (Number(payload.schema_version) !== 1) {
    throw new Error(`harness/eval-promotion schema_version 不兼容: ${payload.schema_version}`);
  }
  const stage = harnessChoice(
    payload.stage,
    "harness/eval-promotion stage",
    HARNESS_EVAL_PROMOTION_STAGES,
  );
  const terminal = harnessBoolean(payload.terminal, "harness/eval-promotion terminal");
  if (terminal !== !["awaiting_reason", "awaiting_confirmation"].includes(stage)) {
    throw new Error("harness/eval-promotion terminal 与 stage 不一致");
  }
  const suiteId = harnessText(payload.suite_id, "harness/eval-promotion suite_id");
  const batchId = harnessText(payload.batch_id, "harness/eval-promotion batch_id");
  if (!/^[a-z][a-z0-9_-]{0,63}$/.test(suiteId)) {
    throw new Error("harness/eval-promotion suite_id 无效");
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(batchId)) {
    throw new Error("harness/eval-promotion batch_id 无效");
  }
  const optionalSha = (value, field) => {
    const normalized = harnessText(value, field);
    if (normalized && !/^[0-9a-f]{64}$/.test(normalized)) {
      throw new Error(`${field} 必须是 SHA-256`);
    }
    return normalized;
  };
  const baselineId = optionalSha(payload.baseline_id, "harness/eval-promotion baseline_id");
  const activeBaselineId = optionalSha(
    payload.active_baseline_id,
    "harness/eval-promotion active_baseline_id",
  );
  const previousBaselineId = optionalSha(
    payload.previous_baseline_id,
    "harness/eval-promotion previous_baseline_id",
  );
  const version = harnessNonnegativeInteger(payload.version, "harness/eval-promotion version");
  const sampleCount = harnessNonnegativeInteger(
    payload.sample_count,
    "harness/eval-promotion sample_count",
  );
  const promotionReason = harnessText(
    payload.promotion_reason,
    "harness/eval-promotion promotion_reason",
  );
  const promotedBy = harnessText(payload.promoted_by, "harness/eval-promotion promoted_by");
  const createdAt = harnessText(payload.created_at, "harness/eval-promotion created_at");
  if (stage === "awaiting_confirmation" && !promotionReason) {
    throw new Error("harness/eval-promotion 确认阶段缺少晋升理由");
  }
  if (["promoted", "already_active"].includes(stage)
    && (
      !baselineId
      || version < 1
      || sampleCount < 1
      || !promotedBy
      || !promotionReason
      || !createdAt
    )) {
    throw new Error("harness/eval-promotion 成功终态缺少权威字段");
  }
  if (stage === "not_selected" && (!baselineId || !activeBaselineId || version < 1)) {
    throw new Error("harness/eval-promotion not_selected 缺少权威字段");
  }
  return {
    schema_version: 1,
    stage,
    terminal,
    suite_id: suiteId,
    batch_id: batchId,
    code: harnessText(payload.code, "harness/eval-promotion code"),
    message: harnessText(payload.message, "harness/eval-promotion message"),
    baseline_id: baselineId,
    active_baseline_id: activeBaselineId,
    previous_baseline_id: previousBaselineId,
    version,
    sample_count: sampleCount,
    promoted_by: promotedBy,
    promotion_reason: promotionReason,
    created_at: createdAt,
  };
}

function normalizeDoctorHealth(payload) {
  if (Number(payload.schema_version) !== 1) {
    throw new Error(`doctor/health schema_version 不兼容: ${payload.schema_version}`);
  }
  const items = harnessObjectArray(payload.items, "doctor/health items", 65);
  if (items.length > 64) throw new Error("doctor/health items 超过 64 项");
  const snapshotSha256 = harnessText(payload.snapshot_sha256, "doctor/health snapshot_sha256");
  if (!/^[0-9a-f]{64}$/.test(snapshotSha256)) {
    throw new Error("doctor/health snapshot_sha256 必须是 SHA-256");
  }
  const generatedAt = harnessText(payload.generated_at, "doctor/health generated_at");
  if (!generatedAt) throw new Error("doctor/health generated_at 不能为空");
  const normalizedItems = items.map((item) => {
    const id = harnessText(item.id, "doctor/health item.id");
    if (!/^[a-z][a-z0-9_-]{0,63}$/.test(id)) throw new Error("doctor/health item.id 无效");
    return {
      id,
      domain: harnessChoice(item.domain, "doctor/health item.domain", DOCTOR_HEALTH_DOMAINS),
      label: harnessText(item.label, "doctor/health item.label"),
      severity: harnessChoice(
        item.severity,
        "doctor/health item.severity",
        DOCTOR_HEALTH_SEVERITIES,
      ),
      responsibility: harnessChoice(
        item.responsibility,
        "doctor/health item.responsibility",
        DOCTOR_HEALTH_RESPONSIBILITIES,
      ),
      detail: harnessText(item.detail, "doctor/health item.detail"),
      suggestion: harnessText(item.suggestion, "doctor/health item.suggestion"),
    };
  });
  if (new Set(normalizedItems.map((item) => item.id)).size !== normalizedItems.length) {
    throw new Error("doctor/health item.id 必须唯一");
  }
  return {
    schema_version: 1,
    status: harnessChoice(payload.status, "doctor/health status", DOCTOR_HEALTH_SEVERITIES),
    generated_at: generatedAt,
    live_probe: harnessBoolean(payload.live_probe, "doctor/health live_probe"),
    snapshot_sha256: snapshotSha256,
    items: normalizedItems,
  };
}

function harnessSha256(value, field) {
  const normalized = harnessText(value, field);
  if (!/^[0-9a-f]{64}$/.test(normalized)) throw new Error(`${field} 必须是 SHA-256`);
  return normalized;
}

function normalizeHarnessExplain(payload) {
  const normalized = normalizeHarnessDetailHeader(payload, "harness/explain");
  if (normalized.lookup_status !== "ok") return normalized;
  const explanation = harnessObject(payload.explanation, "harness/explain explanation");
  const status = harnessChoice(
    explanation.status,
    "harness/explain explanation.status",
    HARNESS_RUN_STATUSES,
  );
  const running = harnessBoolean(explanation.running, "harness/explain running");
  if (status === "running" || running) {
    throw new Error("harness/explain 运行尚未完成，不能作为 revision 1 缓存");
  }
  return {
    ...normalized,
    explanation: {
      status,
      objective: harnessText(explanation.objective, "harness/explain objective"),
      started_at: harnessText(explanation.started_at, "harness/explain started_at"),
      completed_at: harnessText(explanation.completed_at, "harness/explain completed_at"),
      verified: harnessBoolean(explanation.verified, "harness/explain verified"),
      running,
      summary: harnessText(explanation.summary, "harness/explain summary"),
      criteria: harnessObjectArray(explanation.criteria ?? [], "harness/explain criteria", 100)
        .map((item) => ({
          id: harnessText(item.id, "harness/explain criterion.id"),
          description: harnessText(
            item.description,
            "harness/explain criterion.description",
          ),
          status: harnessChoice(
            item.status,
            "harness/explain criterion.status",
            HARNESS_CRITERION_STATUSES,
          ),
          evidence_ids: harnessTextArray(
            item.evidence_ids,
            "harness/explain criterion.evidence_ids",
            100,
          ),
        })),
      failure_classes: harnessTextArray(
        explanation.failure_classes,
        "harness/explain failure_classes",
        20,
      ).map((item) => harnessChoice(item, "harness/explain failure_class", HARNESS_FAILURE_CLASSES)),
      findings: harnessObjectArray(
        explanation.findings,
        "harness/explain findings",
        20,
      ).map((item) => ({
        failure_class: harnessChoice(
          item.failure_class,
          "harness/explain finding.failure_class",
          HARNESS_FAILURE_CLASSES,
        ),
        source: harnessText(item.source, "harness/explain finding.source"),
        message: harnessText(item.message, "harness/explain finding.message"),
        next_step: harnessText(item.next_step, "harness/explain finding.next_step"),
        check_ids: harnessTextArray(
          item.check_ids,
          "harness/explain finding.check_ids",
          50,
        ),
        evidence_ids: harnessTextArray(
          item.evidence_ids,
          "harness/explain finding.evidence_ids",
          100,
        ),
      })),
      checks: harnessObjectArray(explanation.checks, "harness/explain checks", 50)
        .map((item) => ({
          id: harnessText(item.id, "harness/explain check.id"),
          status: harnessText(item.status, "harness/explain check.status"),
          duration_ms: harnessNonnegativeInteger(
            item.duration_ms,
            "harness/explain check.duration_ms",
          ),
        })),
      evidence: harnessObjectArray(explanation.evidence, "harness/explain evidence", 100)
        .map((item) => ({
          id: harnessText(item.id, "harness/explain evidence.id"),
          kind: harnessText(item.kind, "harness/explain evidence.kind"),
          status: harnessText(item.status, "harness/explain evidence.status"),
          digest_prefix: harnessText(
            item.digest_prefix,
            "harness/explain evidence.digest_prefix",
          ),
          uri: harnessText(item.uri, "harness/explain evidence.uri"),
        })),
    },
  };
}

function normalizeHarnessReplay(payload) {
  const normalized = normalizeHarnessDetailHeader(payload, "harness/replay");
  if (normalized.lookup_status !== "ok") return normalized;
  const result = harnessObject(payload.result, "harness/replay result");
  const anomalies = harnessTextArray(result.anomalies, "harness/replay anomalies", 50);
  if (anomalies.includes("run_not_finished")) {
    throw new Error("harness/replay 运行尚未完成，不能作为 revision 1 缓存");
  }
  return {
    ...normalized,
    result: {
      status: harnessChoice(result.status, "harness/replay result.status", HARNESS_REPLAY_STATUSES),
      baseline_manifest_sha256: harnessText(
        result.baseline_manifest_sha256,
        "harness/replay baseline_manifest_sha256",
      ),
      current_manifest_sha256: harnessText(
        result.current_manifest_sha256,
        "harness/replay current_manifest_sha256",
      ),
      baseline_rule_version: harnessText(
        result.baseline_rule_version,
        "harness/replay baseline_rule_version",
      ),
      current_rule_version: harnessText(
        result.current_rule_version,
        "harness/replay current_rule_version",
      ),
      baseline_explanation_sha256: harnessText(
        result.baseline_explanation_sha256,
        "harness/replay baseline_explanation_sha256",
      ),
      current_explanation_sha256: harnessText(
        result.current_explanation_sha256,
        "harness/replay current_explanation_sha256",
      ),
      timeline: harnessObjectArray(result.timeline, "harness/replay timeline", 200)
        .map((item) => ({
          kind: harnessText(item.kind, "harness/replay timeline.kind"),
          id: harnessText(item.id, "harness/replay timeline.id"),
          timestamp: harnessText(item.timestamp, "harness/replay timeline.timestamp"),
          status: harnessText(item.status, "harness/replay timeline.status"),
        })),
      artifacts: harnessObjectArray(result.artifacts, "harness/replay artifacts", 100)
        .map((item) => ({
          id: harnessText(item.id, "harness/replay artifact.id"),
          kind: harnessText(item.kind, "harness/replay artifact.kind"),
          reference: harnessText(item.reference, "harness/replay artifact.reference"),
          status: harnessChoice(
            item.status,
            "harness/replay artifact.status",
            HARNESS_ARTIFACT_STATUSES,
          ),
          expected_sha256: harnessText(
            item.expected_sha256,
            "harness/replay artifact.expected_sha256",
          ),
          actual_sha256: harnessText(
            item.actual_sha256,
            "harness/replay artifact.actual_sha256",
          ),
        })),
      anomalies,
      differences: harnessObjectArray(
        result.differences,
        "harness/replay differences",
        50,
      ).map((item) => ({
        field: harnessText(item.field, "harness/replay difference.field"),
        baseline: harnessText(item.baseline, "harness/replay difference.baseline"),
        current: harnessText(item.current, "harness/replay difference.current"),
      })),
      legacy_baseline_created: harnessBoolean(
        result.legacy_baseline_created,
        "harness/replay legacy_baseline_created",
      ),
    },
  };
}

function normalizePermissionSnapshot(payload) {
  if (Number(payload.schema_version) !== 1) {
    throw new Error(`permissions/snapshot schema_version 不兼容: ${payload.schema_version}`);
  }
  return {
    schema_version: 1,
    runtime_mode: harnessChoice(
      payload.runtime_mode,
      "permissions/snapshot runtime_mode",
      PERMISSION_RUNTIME_MODES,
    ),
    permission_mode: harnessChoice(
      payload.permission_mode,
      "permissions/snapshot permission_mode",
      PERMISSION_MODES,
    ),
    pending: normalizePermissionItems(payload.pending, "pending", 50),
    grants: harnessObjectArray(payload.grants, "permissions/snapshot grants", 50)
      .map((item) => ({
        grant_id: harnessText(item.grant_id, "permissions/snapshot grant.grant_id"),
        tool_family: harnessText(item.tool_family, "permissions/snapshot grant.tool_family"),
        created_at: harnessText(item.created_at, "permissions/snapshot grant.created_at"),
        expires_at: harnessText(item.expires_at, "permissions/snapshot grant.expires_at"),
        source_request_id: harnessText(
          item.source_request_id,
          "permissions/snapshot grant.source_request_id",
        ),
      })),
    history: normalizePermissionItems(payload.history, "history", 50),
    warnings: harnessTextArray(payload.warnings, "permissions/snapshot warnings", 20),
  };
}

function normalizeEvolutionReview(payload) {
  if (Number(payload.schema_version) !== 1) {
    throw new Error(`evolution/review schema_version 不兼容: ${payload.schema_version}`);
  }
  const mode = harnessChoice(payload.mode, "evolution/review mode", new Set(["list", "detail"]));
  const filters = harnessObject(payload.filters, "evolution/review filters");
  const selected = payload.selected == null ? null : normalizeEvolutionItem(payload.selected, true);
  const limit = harnessNonnegativeInteger(filters.limit, "evolution/review filters.limit");
  const items = harnessObjectArray(payload.items, "evolution/review items", 100)
    .map((item) => normalizeEvolutionItem(item, false));
  const readOnly = harnessBoolean(payload.read_only, "evolution/review read_only");
  if (limit < 1 || limit > 100) throw new Error("evolution/review filters.limit 必须在 1..100");
  if (!readOnly) throw new Error("evolution/review 必须保持只读");
  if (mode === "list" && selected !== null) throw new Error("evolution/review list 不得携带 selected");
  if (mode === "detail" && items.length) throw new Error("evolution/review detail 不得携带 items");
  return {
    schema_version: 1,
    mode,
    filters: {
      query: harnessText(filters.query, "evolution/review filters.query"),
      risk: harnessChoice(filters.risk, "evolution/review filters.risk", new Set(["", "low", "medium", "high", "critical"])),
      source_kind: harnessText(filters.source_kind, "evolution/review filters.source_kind"),
      limit,
    },
    items,
    selected,
    events: harnessObjectArray(payload.events, "evolution/review events", 100).map((event) => ({
      revision: harnessNonnegativeInteger(event.revision, "evolution/review event.revision"),
      event_type: harnessText(event.event_type, "evolution/review event.event_type"),
      added_evidence_count: harnessNonnegativeInteger(event.added_evidence_count, "evolution/review event.added_evidence_count"),
      occurred_at: harnessText(event.occurred_at, "evolution/review event.occurred_at"),
    })),
    read_only: readOnly,
  };
}

function normalizeEvolutionItem(value, detail) {
  const item = harnessObject(value, "evolution/review item");
  const candidateId = harnessText(item.candidate_id, "evolution/review item.candidate_id");
  if (!/^evc_[0-9a-f]{24}$/.test(candidateId)) throw new Error("evolution/review candidate_id 无效");
  const normalized = {
    candidate_id: candidateId,
    finding_code: harnessText(item.finding_code, "evolution/review item.finding_code"),
    kind: harnessChoice(item.kind, "evolution/review item.kind", new Set(["correctness", "maintainability", "reliability", "safety"])),
    scope: harnessText(item.scope, "evolution/review item.scope"),
    risk: harnessChoice(item.risk, "evolution/review item.risk", new Set(["low", "medium", "high", "critical"])),
    occurrence_count: harnessNonnegativeInteger(item.occurrence_count, "evolution/review item.occurrence_count"),
    source_kinds: harnessTextArray(item.source_kinds, "evolution/review item.source_kinds", 16),
    last_observed_at: harnessText(item.last_observed_at, "evolution/review item.last_observed_at"),
    revision: harnessNonnegativeInteger(item.revision, "evolution/review item.revision"),
    decision: harnessChoice(item.decision, "evolution/review item.decision", new Set(["blocked", "needs_evidence", "review_ready"])),
    review_ready: harnessBoolean(item.review_ready, "evolution/review item.review_ready"),
    human_review_required: harnessBoolean(item.human_review_required, "evolution/review item.human_review_required"),
    experiment_eligible: harnessBoolean(item.experiment_eligible, "evolution/review item.experiment_eligible"),
  };
  if (normalized.experiment_eligible) {
    throw new Error("evolution/review item 不得授予实验资格");
  }
  if (!detail) return normalized;
  const proposal = item.proposal == null ? null : normalizeEvolutionProposal(item.proposal);
  if (normalized.review_ready && proposal === null) {
    throw new Error("evolution/review review_ready detail 必须包含 Proposal Preview");
  }
  if (!normalized.review_ready && proposal !== null) {
    throw new Error("evolution/review 未就绪 Candidate 不得包含 Proposal Preview");
  }
  if (proposal && (
    proposal.source.candidate_id !== candidateId
    || proposal.source.candidate_revision !== normalized.revision
    || proposal.source.occurrence_count !== normalized.occurrence_count
    || proposal.impact_scope !== normalized.scope
    || proposal.risk_level !== normalized.risk
  )) {
    throw new Error("evolution/review Proposal Preview 与 Candidate 不一致");
  }
  return {
    ...normalized,
    status: harnessText(item.status, "evolution/review item.status"),
    hypothesis: harnessText(item.hypothesis, "evolution/review item.hypothesis"),
    providers: harnessTextArray(item.providers, "evolution/review item.providers", 50),
    models: harnessTextArray(item.models, "evolution/review item.models", 50),
    platforms: harnessTextArray(item.platforms, "evolution/review item.platforms", 50),
    first_observed_at: harnessText(item.first_observed_at, "evolution/review item.first_observed_at"),
    expected_metrics: harnessTextArray(item.expected_metrics, "evolution/review item.expected_metrics", 8),
    evidence_refs: harnessTextArray(item.evidence_refs, "evolution/review item.evidence_refs", 200),
    policy_version: harnessText(item.policy_version, "evolution/review item.policy_version"),
    checks: harnessObjectArray(item.checks, "evolution/review item.checks", 16).map((check) => ({
      code: harnessText(check.code, "evolution/review check.code"),
      passed: harnessBoolean(check.passed, "evolution/review check.passed"),
      hard_block: harnessBoolean(check.hard_block, "evolution/review check.hard_block"),
      detail: harnessText(check.detail, "evolution/review check.detail"),
    })),
    governance: normalizeEvolutionGovernance(item.governance),
    aggregation: normalizeEvolutionAggregation(item.aggregation),
    proposal,
  };
}

function normalizeEvolutionGovernance(value) {
  if (value == null) return null;
  const item = harnessObject(value, "evolution/review governance");
  return {
    policy_version: harnessText(item.policy_version, "evolution/review governance.policy_version"),
    allowed: harnessBoolean(item.allowed, "evolution/review governance.allowed"),
    reason: harnessChoice(
      item.reason,
      "evolution/review governance.reason",
      new Set([
        "no_active_cooldown",
        "cooldown_expired",
        "significant_new_evidence",
        "cooldown_active",
        "cooldown_record_missing",
      ]),
    ),
    proposal_state: harnessChoice(
      item.proposal_state,
      "evolution/review governance.proposal_state",
      new Set(["", "open", "approved", "rejected", "deferred", "merged", "converted"]),
    ),
    proposal_revision: harnessNonnegativeInteger(
      item.proposal_revision,
      "evolution/review governance.proposal_revision",
    ),
    cooldown_until: harnessText(
      item.cooldown_until,
      "evolution/review governance.cooldown_until",
    ),
    significant_new_evidence: harnessBoolean(
      item.significant_new_evidence,
      "evolution/review governance.significant_new_evidence",
    ),
  };
}

function normalizeEvolutionProposal(value) {
  const item = harnessObject(value, "evolution/review proposal");
  const source = harnessObject(item.source, "evolution/review proposal.source");
  const executable = harnessBoolean(item.executable, "evolution/review proposal.executable");
  const experimentEligible = harnessBoolean(
    item.experiment_eligible,
    "evolution/review proposal.experiment_eligible",
  );
  const humanReview = harnessBoolean(
    item.requires_human_review,
    "evolution/review proposal.requires_human_review",
  );
  if (executable || experimentEligible || !humanReview || item.state !== "preview") {
    throw new Error("evolution/review Proposal Preview authority contract 无效");
  }
  const proposalId = harnessText(item.proposal_id, "evolution/review proposal.proposal_id");
  if (!/^evp_[0-9a-f]{24}$/.test(proposalId)) {
    throw new Error("evolution/review proposal_id 无效");
  }
  const schemaVersion = harnessNonnegativeInteger(
      item.schema_version,
      "evolution/review proposal.schema_version",
  );
  if (schemaVersion !== 1) throw new Error("evolution/review Proposal schema_version 不兼容");
  const generatorVersion = harnessChoice(
    item.generator_version,
    "evolution/review proposal.generator_version",
    new Set(["evolution-proposal-v1"]),
  );
  const proposalKind = harnessChoice(
    item.proposal_kind,
    "evolution/review proposal.proposal_kind",
    new Set(["knowledge", "profile", "prompt", "tool", "test", "code"]),
  );
  const candidateId = harnessText(
    source.candidate_id,
    "evolution/review proposal.source.candidate_id",
  );
  const candidateRevision = harnessNonnegativeInteger(
    source.candidate_revision,
    "evolution/review proposal.source.candidate_revision",
  );
  const candidateSha256 = harnessText(
    source.candidate_sha256,
    "evolution/review proposal.source.candidate_sha256",
  );
  const occurrenceCount = harnessNonnegativeInteger(
    source.occurrence_count,
    "evolution/review proposal.source.occurrence_count",
  );
  if (!/^evc_[0-9a-f]{24}$/.test(candidateId) || !/^[0-9a-f]{64}$/.test(candidateSha256)) {
    throw new Error("evolution/review Proposal source identity 无效");
  }
  if (candidateRevision < 1 || occurrenceCount < 1) {
    throw new Error("evolution/review Proposal source revision/count 无效");
  }
  const expectedProposalId = `evp_${createHash("sha256").update(JSON.stringify({
    candidate_id: candidateId,
    candidate_revision: candidateRevision,
    candidate_sha256: candidateSha256,
    generator_version: generatorVersion,
    proposal_kind: proposalKind,
  })).digest("hex").slice(0, 24)}`;
  if (proposalId !== expectedProposalId) {
    throw new Error("evolution/review proposal_id 与 source snapshot 不一致");
  }
  const validationPlan = harnessObjectArray(
    item.validation_plan,
    "evolution/review proposal.validation_plan",
    8,
  ).map((step) => ({
    metric_name: evolutionProposalText(step.metric_name, "evolution/review proposal.metric_name", 128),
    direction: harnessChoice(
      step.direction,
      "evolution/review proposal.direction",
      new Set(["decrease", "increase"]),
    ),
    target: harnessFiniteNumber(
      step.target,
      "evolution/review proposal.target",
    ),
    verifier: harnessChoice(
      step.verifier,
      "evolution/review proposal.verifier",
      new Set(["harness_replay", "self_review_static", "feedback_recurrence"]),
    ),
    procedure: evolutionProposalText(step.procedure, "evolution/review proposal.procedure", 1_000),
  }));
  const reviewNotes = harnessTextArray(
    item.review_notes,
    "evolution/review proposal.review_notes",
    8,
  );
  if (!validationPlan.length || !reviewNotes.length) {
    throw new Error("evolution/review Proposal validation/review notes 不能为空");
  }
  return {
    schema_version: schemaVersion,
    proposal_id: proposalId,
    generator_version: generatorVersion,
    proposal_kind: proposalKind,
    classification_reason: evolutionProposalText(
      item.classification_reason,
      "evolution/review proposal.classification_reason",
      128,
    ),
    title: evolutionProposalText(item.title, "evolution/review proposal.title", 300),
    summary: evolutionProposalText(item.summary, "evolution/review proposal.summary", 2_000),
    impact_scope: evolutionProposalText(
      item.impact_scope,
      "evolution/review proposal.impact_scope",
      1_024,
    ),
    intended_files: evolutionProposalTextArray(
      item.intended_files,
      "evolution/review proposal.intended_files",
      16,
      1_024,
    ),
    validation_plan: validationPlan,
    risk_level: harnessChoice(
      item.risk_level,
      "evolution/review proposal.risk_level",
      new Set(["low", "medium", "high", "critical"]),
    ),
    review_notes: reviewNotes,
    source: {
      candidate_id: candidateId,
      candidate_revision: candidateRevision,
      candidate_sha256: candidateSha256,
      occurrence_count: occurrenceCount,
      last_observed_at: harnessText(
        source.last_observed_at,
        "evolution/review proposal.source.last_observed_at",
      ),
      aggregation_policy: harnessChoice(
        source.aggregation_policy,
        "evolution/review proposal.source.aggregation_policy",
        new Set(["candidate-aggregation-v1"]),
      ),
      trend: harnessChoice(
        source.trend,
        "evolution/review proposal.source.trend",
        new Set(["new", "increasing", "stable", "decreasing", "insufficient"]),
      ),
    },
    requires_human_review: humanReview,
    executable,
    experiment_eligible: experimentEligible,
    state: "preview",
  };
}

function normalizeEvolutionAggregation(value) {
  const item = harnessObject(value, "evolution/review aggregation");
  return {
    policy_version: harnessText(item.policy_version, "evolution/review aggregation.policy_version"),
    anchor_at: harnessText(item.anchor_at, "evolution/review aggregation.anchor_at"),
    span_seconds: harnessNonnegativeInteger(item.span_seconds, "evolution/review aggregation.span_seconds"),
    total_count: harnessNonnegativeInteger(item.total_count, "evolution/review aggregation.total_count"),
    count_24h: harnessNonnegativeInteger(item.count_24h, "evolution/review aggregation.count_24h"),
    count_7d: harnessNonnegativeInteger(item.count_7d, "evolution/review aggregation.count_7d"),
    count_30d: harnessNonnegativeInteger(item.count_30d, "evolution/review aggregation.count_30d"),
    previous_7d_count: harnessNonnegativeInteger(item.previous_7d_count, "evolution/review aggregation.previous_7d_count"),
    trend: harnessChoice(item.trend, "evolution/review aggregation.trend", new Set(["new", "increasing", "stable", "decreasing", "insufficient"])),
    source_counts: normalizeEvolutionDimensions(item.source_counts, "source_counts"),
    source_unique_count: harnessNonnegativeInteger(item.source_unique_count, "evolution/review aggregation.source_unique_count"),
    provider_counts: normalizeEvolutionDimensions(item.provider_counts, "provider_counts"),
    provider_unique_count: harnessNonnegativeInteger(item.provider_unique_count, "evolution/review aggregation.provider_unique_count"),
    model_counts: normalizeEvolutionDimensions(item.model_counts, "model_counts"),
    model_unique_count: harnessNonnegativeInteger(item.model_unique_count, "evolution/review aggregation.model_unique_count"),
    platform_counts: normalizeEvolutionDimensions(item.platform_counts, "platform_counts"),
    platform_unique_count: harnessNonnegativeInteger(item.platform_unique_count, "evolution/review aggregation.platform_unique_count"),
    representatives: harnessObjectArray(item.representatives, "evolution/review aggregation.representatives", 16).map((entry) => ({
      evidence_id: harnessText(entry.evidence_id, "evolution/review representative.evidence_id"),
      source_kind: harnessText(entry.source_kind, "evolution/review representative.source_kind"),
      observed_at: harnessText(entry.observed_at, "evolution/review representative.observed_at"),
      ref_uri: harnessText(entry.ref_uri, "evolution/review representative.ref_uri"),
      ref_sha256_prefix: harnessText(entry.ref_sha256_prefix, "evolution/review representative.ref_sha256_prefix"),
    })),
  };
}

function normalizeEvolutionDimensions(value, name) {
  return harnessObjectArray(value, `evolution/review aggregation.${name}`, 20).map((item) => ({
    value: harnessText(item.value, `evolution/review aggregation.${name}.value`),
    count: harnessNonnegativeInteger(item.count, `evolution/review aggregation.${name}.count`),
    percentage: harnessNonnegativeFiniteNumber(item.percentage, `evolution/review aggregation.${name}.percentage`),
  }));
}

function normalizeTaskSnapshot(payload) {
  if (Number(payload.schema_version) !== 1) {
    throw new Error(`tasks/snapshot schema_version 不兼容: ${payload.schema_version}`);
  }
  const filters = harnessObject(payload.filters, "tasks/snapshot filters");
  return {
    schema_version: 1,
    generated_at: harnessText(payload.generated_at, "tasks/snapshot generated_at"),
    full: harnessBoolean(payload.full, "tasks/snapshot full"),
    filters: {
      source: harnessText(filters.source, "tasks/snapshot filters.source"),
      status: harnessText(filters.status, "tasks/snapshot filters.status"),
      detail_id: harnessText(filters.detail_id, "tasks/snapshot filters.detail_id"),
      history: harnessBoolean(filters.history, "tasks/snapshot filters.history"),
    },
    items: harnessObjectArray(payload.items, "tasks/snapshot items", 200)
      .map(normalizeTaskItem),
    timeline: harnessObjectArray(payload.timeline, "tasks/snapshot timeline", 200)
      .map(normalizeTaskTimelineEvent),
    warnings: harnessTextArray(payload.warnings, "tasks/snapshot warnings", 20),
  };
}

function normalizeGoalSnapshot(payload) {
  if (Number(payload.schema_version) !== 1) {
    throw new Error(`goals/snapshot schema_version 不兼容: ${payload.schema_version}`);
  }
  const goals = harnessObjectArray(payload.goals, "goals/snapshot goals", 50)
    .map(normalizeGoalItem);
  const ids = goals.map((item) => item.goal_id);
  if (new Set(ids).size !== ids.length) {
    throw new Error("goals/snapshot goal_id 不得重复");
  }
  const currentGoalId = harnessText(payload.current_goal_id, "goals/snapshot current_goal_id");
  validateGoalId(currentGoalId, "goals/snapshot current_goal_id", true);
  if (currentGoalId && !goals.some((item) => item.goal_id === currentGoalId)) {
    throw new Error("goals/snapshot current_goal_id 不在 goals 中");
  }
  return {
    schema_version: 1,
    generated_at: harnessText(payload.generated_at, "goals/snapshot generated_at"),
    full: harnessBoolean(payload.full, "goals/snapshot full"),
    current_goal_id: currentGoalId,
    goals,
    warnings: harnessTextArray(payload.warnings, "goals/snapshot warnings", 20),
    truncated: harnessBoolean(payload.truncated, "goals/snapshot truncated"),
    include_finished: harnessBoolean(
      payload.include_finished,
      "goals/snapshot include_finished",
    ),
  };
}

function normalizeGoalItem(item) {
  const goalId = harnessText(item.goal_id, "goals/snapshot goal.goal_id");
  validateGoalId(goalId, "goals/snapshot goal.goal_id");
  const pursuitRunId = harnessText(
    item.pursuit_run_id,
    "goals/snapshot goal.pursuit_run_id",
  );
  validateGoalId(pursuitRunId, "goals/snapshot goal.pursuit_run_id", true);
  const linkStatus = harnessChoice(
    item.pursuit_link_status,
    "goals/snapshot goal.pursuit_link_status",
    PURSUIT_LINK_STATUSES,
  );
  const pursuit = item.pursuit == null ? null : normalizePursuitItem(item.pursuit);
  if (linkStatus === "ready" && (!pursuit || pursuit.run_id !== pursuitRunId)) {
    throw new Error("goals/snapshot ready Pursuit 必须与 pursuit_run_id 一致");
  }
  if (linkStatus !== "ready" && pursuit !== null) {
    throw new Error("goals/snapshot 非 ready 关联不得包含 Pursuit");
  }
  if (linkStatus === "not_linked" && pursuitRunId) {
    throw new Error("goals/snapshot not_linked 不得包含 pursuit_run_id");
  }
  if (linkStatus === "missing" && !pursuitRunId) {
    throw new Error("goals/snapshot missing 必须包含 pursuit_run_id");
  }
  return {
    goal_id: goalId,
    objective: harnessText(item.objective, "goals/snapshot goal.objective"),
    status: harnessChoice(item.status, "goals/snapshot goal.status", GOAL_STATUSES),
    note: harnessText(item.note, "goals/snapshot goal.note"),
    session_id: harnessText(item.session_id, "goals/snapshot goal.session_id"),
    pursuit_run_id: pursuitRunId,
    pursuit_link_status: linkStatus,
    created_at: harnessText(item.created_at, "goals/snapshot goal.created_at"),
    updated_at: harnessText(item.updated_at, "goals/snapshot goal.updated_at"),
    pursuit,
  };
}

function normalizePursuitItem(value) {
  const item = harnessObject(value, "goals/snapshot pursuit");
  const criteriaTotal = harnessNonnegativeInteger(
    item.criteria_total,
    "goals/snapshot pursuit.criteria_total",
  );
  const criteriaVerified = harnessNonnegativeInteger(
    item.criteria_verified,
    "goals/snapshot pursuit.criteria_verified",
  );
  if (criteriaVerified > criteriaTotal) {
    throw new Error("goals/snapshot 已验证标准不能超过标准总数");
  }
  const runId = harnessText(item.run_id, "goals/snapshot pursuit.run_id");
  validateGoalId(runId, "goals/snapshot pursuit.run_id");
  const recovery = item.recovery == null ? null : normalizePursuitRecovery(item.recovery, runId);
  return {
    run_id: runId,
    goal: harnessText(item.goal, "goals/snapshot pursuit.goal"),
    status: harnessChoice(item.status, "goals/snapshot pursuit.status", PURSUIT_STATUSES),
    phase: harnessText(item.phase, "goals/snapshot pursuit.phase"),
    started_at: harnessText(item.started_at, "goals/snapshot pursuit.started_at"),
    updated_at: harnessText(item.updated_at, "goals/snapshot pursuit.updated_at"),
    iteration: harnessNonnegativeInteger(item.iteration, "goals/snapshot pursuit.iteration"),
    criteria_total: criteriaTotal,
    criteria_verified: criteriaVerified,
    failure_count: harnessNonnegativeInteger(
      item.failure_count,
      "goals/snapshot pursuit.failure_count",
    ),
    blocked_reason: harnessText(
      item.blocked_reason,
      "goals/snapshot pursuit.blocked_reason",
    ),
    next_action: harnessText(item.next_action, "goals/snapshot pursuit.next_action"),
    worktree_name: harnessText(
      item.worktree_name,
      "goals/snapshot pursuit.worktree_name",
    ),
    worktree_path: harnessText(
      item.worktree_path,
      "goals/snapshot pursuit.worktree_path",
    ),
    waits: harnessObjectArray(item.waits, "goals/snapshot pursuit.waits", 20)
      .map(normalizePursuitWait),
    evidence: harnessObjectArray(item.evidence, "goals/snapshot pursuit.evidence", 20)
      .map(normalizePursuitEvidence),
    recovery,
  };
}

function normalizePursuitRecovery(value, runId) {
  const item = harnessObject(value, "goals/snapshot pursuit.recovery");
  if (Number(item.schema_version) !== 1) {
    throw new Error("goals/snapshot pursuit.recovery schema_version 不兼容");
  }
  const recoveryRunId = harnessText(item.run_id, "goals/snapshot recovery.run_id");
  if (recoveryRunId !== runId) {
    throw new Error("goals/snapshot recovery.run_id 与 Pursuit 不一致");
  }
  const heartbeat = harnessObject(item.heartbeat, "goals/snapshot recovery.heartbeat");
  const lease = harnessObject(item.lease, "goals/snapshot recovery.lease");
  const checkpoint = harnessObject(item.checkpoint, "goals/snapshot recovery.checkpoint");
  const normalizedHeartbeat = {
    health: harnessChoice(heartbeat.health, "goals/snapshot heartbeat.health", PURSUIT_HEARTBEAT_HEALTH),
    phase: harnessText(heartbeat.phase, "goals/snapshot heartbeat.phase"),
    instance_id: harnessText(heartbeat.instance_id, "goals/snapshot heartbeat.instance_id"),
    epoch: harnessNonnegativeInteger(heartbeat.epoch, "goals/snapshot heartbeat.epoch"),
    sequence: harnessNonnegativeInteger(heartbeat.sequence, "goals/snapshot heartbeat.sequence"),
    observed_at: harnessText(heartbeat.observed_at, "goals/snapshot heartbeat.observed_at"),
    timeout_seconds: harnessNonnegativeInteger(heartbeat.timeout_seconds, "goals/snapshot heartbeat.timeout_seconds"),
    age_seconds: harnessNonnegativeInteger(heartbeat.age_seconds, "goals/snapshot heartbeat.age_seconds"),
    detail_code: harnessText(heartbeat.detail_code, "goals/snapshot heartbeat.detail_code"),
  };
  const normalizedLease = {
    status: harnessChoice(lease.status, "goals/snapshot lease.status", PURSUIT_LEASE_STATUSES),
    owner_id: harnessText(lease.owner_id, "goals/snapshot lease.owner_id"),
    epoch: harnessNonnegativeInteger(lease.epoch, "goals/snapshot lease.epoch"),
    expires_at: harnessText(lease.expires_at, "goals/snapshot lease.expires_at"),
    updated_at: harnessText(lease.updated_at, "goals/snapshot lease.updated_at"),
    expired: harnessBoolean(lease.expired, "goals/snapshot lease.expired"),
  };
  const normalizedCheckpoint = {
    status: harnessChoice(
      checkpoint.status,
      "goals/snapshot checkpoint.status",
      PURSUIT_CHECKPOINT_STATUSES,
    ),
    checkpoint_id: harnessText(checkpoint.checkpoint_id, "goals/snapshot checkpoint.checkpoint_id"),
    sequence: harnessNonnegativeInteger(checkpoint.sequence, "goals/snapshot checkpoint.sequence"),
    phase: harnessText(checkpoint.phase, "goals/snapshot checkpoint.phase"),
    iteration: harnessNonnegativeInteger(checkpoint.iteration, "goals/snapshot checkpoint.iteration"),
    created_at: harnessText(checkpoint.created_at, "goals/snapshot checkpoint.created_at"),
  };
  if (!normalizedHeartbeat.observed_at && !["missing", "error"].includes(normalizedHeartbeat.health)) {
    throw new Error("goals/snapshot heartbeat observed_at 不能为空");
  }
  if (!["missing", "error"].includes(normalizedHeartbeat.health)
      && (!normalizedHeartbeat.instance_id || normalizedHeartbeat.epoch < 1
        || normalizedHeartbeat.sequence < 1 || normalizedHeartbeat.timeout_seconds < 3
        || normalizedHeartbeat.timeout_seconds > 86_400)) {
    throw new Error("goals/snapshot heartbeat identity 或 timeout 无效");
  }
  if (["active", "released"].includes(normalizedLease.status)
      && (!normalizedLease.owner_id || normalizedLease.epoch < 1)) {
    throw new Error("goals/snapshot lease identity 无效");
  }
  if (normalizedCheckpoint.status === "ready"
      && (!normalizedCheckpoint.checkpoint_id || normalizedCheckpoint.sequence < 1)) {
    throw new Error("goals/snapshot checkpoint identity 无效");
  }
  if (["missing", "error"].includes(normalizedHeartbeat.health)
      && (normalizedHeartbeat.instance_id || normalizedHeartbeat.epoch || normalizedHeartbeat.sequence)) {
    throw new Error("goals/snapshot 缺失 heartbeat 不得携带 owner identity");
  }
  if (["missing", "error"].includes(normalizedLease.status)
      && (normalizedLease.owner_id || normalizedLease.epoch)) {
    throw new Error("goals/snapshot 缺失 lease 不得携带 owner identity");
  }
  if (["missing", "error"].includes(normalizedCheckpoint.status)
      && (normalizedCheckpoint.checkpoint_id || normalizedCheckpoint.sequence)) {
    throw new Error("goals/snapshot 缺失 checkpoint 不得携带 identity");
  }
  const generatedAt = harnessText(item.generated_at, "goals/snapshot recovery.generated_at");
  if (!generatedAt) throw new Error("goals/snapshot recovery.generated_at 不能为空");
  return {
    schema_version: 1,
    run_id: recoveryRunId,
    generated_at: generatedAt,
    recovery_state: harnessChoice(
      item.recovery_state,
      "goals/snapshot recovery.recovery_state",
      PURSUIT_RECOVERY_STATES,
    ),
    heartbeat: normalizedHeartbeat,
    lease: normalizedLease,
    checkpoint: normalizedCheckpoint,
    reconcile_required: harnessBoolean(
      item.reconcile_required,
      "goals/snapshot recovery.reconcile_required",
    ),
    reconcile_reason: harnessText(item.reconcile_reason, "goals/snapshot recovery.reconcile_reason"),
    alerts: harnessTextArray(item.alerts, "goals/snapshot recovery.alerts", 8),
  };
}

function validateGoalId(value, name, optional = false) {
  if (optional && !value) return;
  if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(value)) {
    throw new Error(`${name} 格式无效`);
  }
}

function normalizePursuitWait(item) {
  return {
    task_id: harnessText(item.task_id, "goals/snapshot wait.task_id"),
    action_id: harnessText(item.action_id, "goals/snapshot wait.action_id"),
    command: harnessText(item.command, "goals/snapshot wait.command"),
    created_at: harnessText(item.created_at, "goals/snapshot wait.created_at"),
  };
}

function normalizePursuitEvidence(item) {
  return {
    kind: harnessText(item.kind, "goals/snapshot evidence.kind"),
    source: harnessText(item.source, "goals/snapshot evidence.source"),
    summary: harnessText(item.summary, "goals/snapshot evidence.summary"),
    is_hard: harnessBoolean(item.is_hard, "goals/snapshot evidence.is_hard"),
    timestamp: harnessText(item.timestamp, "goals/snapshot evidence.timestamp"),
  };
}

function normalizeTaskItem(item) {
  const source = harnessChoice(item.source, "tasks/snapshot item.source", TASK_SOURCES);
  const status = harnessChoice(item.status, "tasks/snapshot item.status", TASK_STATUSES);
  const priority = item.priority == null
    ? null
    : harnessNonnegativeInteger(item.priority, "tasks/snapshot item.priority");
  const ageSeconds = item.age_seconds == null
    ? null
    : harnessNonnegativeInteger(item.age_seconds, "tasks/snapshot item.age_seconds");
  return {
    view_id: harnessText(item.view_id, "tasks/snapshot item.view_id"),
    source,
    task_id: harnessText(item.task_id, "tasks/snapshot item.task_id"),
    status,
    raw_status: harnessText(item.raw_status, "tasks/snapshot item.raw_status"),
    title: harnessText(item.title, "tasks/snapshot item.title"),
    owner: harnessText(item.owner, "tasks/snapshot item.owner"),
    priority,
    dependency_ids: harnessTextArray(
      item.dependency_ids,
      "tasks/snapshot item.dependency_ids",
      100,
    ),
    child_ids: harnessTextArray(item.child_ids, "tasks/snapshot item.child_ids", 100),
    created_at: harnessText(item.created_at, "tasks/snapshot item.created_at"),
    updated_at: harnessText(item.updated_at, "tasks/snapshot item.updated_at"),
    age_seconds: ageSeconds,
    detail: harnessText(item.detail, "tasks/snapshot item.detail"),
    artifact_refs: harnessTextArray(
      item.artifact_refs,
      "tasks/snapshot item.artifact_refs",
      50,
    ),
  };
}

function normalizeTaskTimelineEvent(item) {
  return {
    event_id: harnessText(item.event_id, "tasks/snapshot timeline.event_id"),
    source: harnessText(item.source, "tasks/snapshot timeline.source"),
    task_id: harnessText(item.task_id, "tasks/snapshot timeline.task_id"),
    status: harnessChoice(item.status, "tasks/snapshot timeline.status", TASK_STATUSES),
    raw_status: harnessText(item.raw_status, "tasks/snapshot timeline.raw_status"),
    title: harnessText(item.title, "tasks/snapshot timeline.title"),
    detail: harnessText(item.detail, "tasks/snapshot timeline.detail"),
    timestamp: harnessText(item.timestamp, "tasks/snapshot timeline.timestamp"),
  };
}

function normalizePermissionItems(value, section, limit) {
  const prefix = `permissions/snapshot ${section}`;
  return harnessObjectArray(value, prefix, limit).map((item) => ({
    request_id: harnessText(item.request_id, `${prefix}.request_id`),
    call_id: harnessText(item.call_id, `${prefix}.call_id`),
    session_id: harnessText(item.session_id, `${prefix}.session_id`),
    run_id: harnessText(item.run_id, `${prefix}.run_id`),
    agent_name: harnessText(item.agent_name, `${prefix}.agent_name`),
    tool_name: harnessText(item.tool_name, `${prefix}.tool_name`),
    tool_family: harnessText(item.tool_family, `${prefix}.tool_family`),
    arguments_summary: harnessText(item.arguments_summary, `${prefix}.arguments_summary`),
    status: harnessChoice(item.status, `${prefix}.status`, PERMISSION_STATUSES),
    reason: harnessText(item.reason, `${prefix}.reason`),
    risk_level: harnessChoice(item.risk_level, `${prefix}.risk_level`, PERMISSION_RISKS),
    choices: harnessTextArray(item.choices, `${prefix}.choices`, 10)
      .map((choice) => harnessChoice(choice, `${prefix}.choice`, PERMISSION_CHOICES)),
    scope: harnessText(item.scope, `${prefix}.scope`),
    expires_at: harnessText(item.expires_at, `${prefix}.expires_at`),
    policy: normalizePermissionPolicy(item.policy, prefix),
  }));
}

function normalizePermissionPolicy(value, prefix) {
  const policy = harnessObject(value, `${prefix}.policy`);
  return {
    source: harnessText(policy.source, `${prefix}.policy.source`),
    risk: harnessChoice(policy.risk, `${prefix}.policy.risk`, PERMISSION_RISKS),
    modes: harnessText(policy.modes, `${prefix}.policy.modes`),
    confirmation: harnessText(policy.confirmation, `${prefix}.policy.confirmation`),
    bypass: harnessText(policy.bypass, `${prefix}.policy.bypass`),
  };
}

function normalizeHarnessDetailHeader(payload, eventName) {
  if (Number(payload.schema_version) !== 1) {
    throw new Error(`${eventName} schema_version 不兼容: ${payload.schema_version}`);
  }
  const revision = harnessPositiveInteger(payload.revision, `${eventName} revision`);
  const runId = harnessText(payload.run_id, `${eventName} run_id`);
  if (!/^[A-Za-z0-9._:-]{1,128}$/.test(runId)) {
    throw new Error(`${eventName} run_id 格式无效`);
  }
  const lookupStatus = harnessChoice(
    payload.lookup_status,
    `${eventName} lookup_status`,
    HARNESS_LOOKUP_STATUSES,
  );
  return {
    schema_version: 1,
    revision,
    run_id: runId,
    lookup_status: lookupStatus,
    message: harnessText(payload.message, `${eventName} message`),
  };
}

function harnessObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${name} 必须是对象`);
  }
  return value;
}

function harnessObjectArray(value, name, limit) {
  if (!Array.isArray(value)) throw new Error(`${name} 必须是数组`);
  return value.slice(0, limit).map((item) => harnessObject(item, name));
}

function harnessTextArray(value, name, limit) {
  if (!Array.isArray(value)) throw new Error(`${name} 必须是数组`);
  return value.slice(0, limit).map((item) => harnessText(item, name));
}

function harnessText(value, name) {
  if (typeof value !== "string") throw new Error(`${name} 必须是字符串`);
  return value.trim().slice(0, 500);
}

function harnessChoice(value, name, allowed) {
  const normalized = harnessText(value, name).toLowerCase();
  if (!allowed.has(normalized)) throw new Error(`${name} 无效: ${normalized}`);
  return normalized;
}

function harnessBoolean(value, name) {
  if (typeof value !== "boolean") throw new Error(`${name} 必须是布尔值`);
  return value;
}

function harnessNonnegativeInteger(value, name) {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new Error(`${name} 必须是非负整数`);
  }
  return value;
}

function harnessNonnegativeFiniteNumber(value, name) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new Error(`${name} 必须是非负有限数值`);
  }
  return value;
}

function harnessFiniteNumber(value, name) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${name} 必须是有限数值`);
  }
  return value;
}

function evolutionProposalText(value, name, limit) {
  if (typeof value !== "string") throw new Error(`${name} 必须是字符串`);
  return value.trim().slice(0, limit);
}

function evolutionProposalTextArray(value, name, countLimit, textLimit) {
  if (!Array.isArray(value)) throw new Error(`${name} 必须是数组`);
  return value.slice(0, countLimit).map((entry) => (
    evolutionProposalText(entry, name, textLimit)
  ));
}

function harnessPositiveInteger(value, name) {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) {
    throw new Error(`${name} 必须是正整数`);
  }
  return value;
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
