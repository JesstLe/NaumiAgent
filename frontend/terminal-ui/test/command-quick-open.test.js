import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

import { stripAnsi } from "../src/ansi.js";
import {
  acceptCommandQuickOpen,
  applyCommandQuickOpenAgentSnapshot,
  applyCommandQuickOpenFileSnapshot,
  agentTemplate,
  parseAgentDeepLink,
  appendCommandQuickOpenQuery,
  backspaceCommandQuickOpenQuery,
  closeCommandQuickOpen,
  getCommandQuickOpenItems,
  moveCommandQuickOpenSelection,
  openCommandQuickOpen,
  pageTemplate,
  recordRecentCommand,
  searchCommandEntries,
  searchTaskEntries,
  searchSessionEntries,
  searchAgentEntries,
  searchPageEntries,
  sessionTemplate,
  switchCommandQuickOpenProvider,
  requestCommandQuickOpenFiles,
} from "../src/command-quick-open.js";
import { renderCommandQuickOpenPage } from "../src/components/command-quick-open-page.js";
import { renderScreen } from "../src/render.js";
import {
  createInitialState,
  handleSubmitText,
  handleInteractionRequest,
  handlePermissionRequest,
  reduceServerEvent,
} from "../src/state.js";
import { normalizeServerRecord } from "../src/protocol.js";

const recencyGolden = JSON.parse(readFileSync(new URL(
  "../../../tests/fixtures/ui14/command-recency-golden.json",
  import.meta.url,
), "utf8"));
const taskGolden = JSON.parse(readFileSync(new URL(
  "../../../tests/fixtures/ui14/task-quick-open-golden.json",
  import.meta.url,
), "utf8"));

const COMMANDS = [
  command("/help", { aliases: ["/h"], risk: "read_only", description: "显示帮助" }),
  command("/world", { risk: "tool_execution", description: "世界模型审计" }),
  command("/write", {
    risk: "workspace_write",
    description: "写入工作区文件",
    syntax: "<path> <content>",
  }),
  command("/delete", { risk: "destructive", description: "删除会话" }),
];
const PAGES = [
  page("conversation", "/chat", "对话", "返回主对话与输入区。", 0, ["chat", "聊天"]),
  page("goals", "/goal", "Goal", "查看持久 Goal、Pursuit 状态与阻塞信息。", 20, [
    "goal",
    "pursuit",
    "目标",
  ]),
  page("permissions", "/permissions", "权限", "查看待确认请求、授权范围与撤销入口。", 50, [
    "approval",
    "permission",
    "权限",
  ]),
];

test("command QuickOpen ranks canonical alias fuzzy and localized risk metadata", () => {
  assert.equal(searchCommandEntries(COMMANDS, "/h")[0].command, "/help");
  assert.equal(searchCommandEntries(COMMANDS, "wr")[0].command, "/write");
  assert.deepEqual(
    searchCommandEntries(COMMANDS, "工作区写入").map((item) => item.command),
    ["/write"],
  );
});

test("command QuickOpen cancel preserves draft and accept only fills composer", () => {
  const state = createInitialState();
  state.slashCommands = COMMANDS;
  state.input = "保留草稿";
  state.inputCursor = 2;

  openCommandQuickOpen(state);
  appendCommandQuickOpenQuery(state, "wr");
  assert.equal(getCommandQuickOpenItems(state)[0].command, "/write");
  assert.equal(closeCommandQuickOpen(state), true);
  assert.equal(state.input, "保留草稿");
  assert.equal(state.inputCursor, 2);

  openCommandQuickOpen(state);
  appendCommandQuickOpenQuery(state, "del");
  assert.equal(moveCommandQuickOpenSelection(state, "next"), true);
  assert.equal(moveCommandQuickOpenSelection(state, "previous"), true);
  assert.equal(acceptCommandQuickOpen(state), true);
  assert.equal(state.input, "/delete");
  assert.equal(state.commandQuickOpen.open, false);
});

test("command QuickOpen query backspace respects grapheme clusters", () => {
  const state = createInitialState();
  openCommandQuickOpen(state);
  appendCommandQuickOpenQuery(state, "e\u0301");
  assert.equal(backspaceCommandQuickOpenQuery(state), true);
  assert.equal(state.commandQuickOpen.query, "");
});

test("command QuickOpen page renders textual safety and no-auto-execute contract", () => {
  const state = createInitialState();
  state.slashCommands = COMMANDS;
  openCommandQuickOpen(state);
  appendCommandQuickOpenQuery(state, "write");

  const rendered = stripAnsi(renderCommandQuickOpenPage(state, 100, 24).join("\n"));
  assert.match(rendered, /命令 QuickOpen/);
  assert.match(rendered, /\/write <path> <content>/);
  assert.match(rendered, /工作区写入/);
  assert.match(rendered, /不会自动发送或执行/);

  state.route = { name: "workbench", originAnchor: null };
  const screen = stripAnsi(renderScreen(state, 100, 30).join("\n"));
  assert.equal(renderScreen(state, 100, 30).length, 30);
  assert.match(screen, /命令 QuickOpen/);
  assert.doesNotMatch(screen, /Workbench 权威视图/);
});

test("page QuickOpen consumes strict status metadata and only fills navigation command", () => {
  const state = createInitialState();
  const [action] = reduceServerEvent(state, normalizeServerRecord({
    type: "runtime/status",
    version: 1,
    payload: { navigation_pages: PAGES },
  }));
  assert.equal(action, undefined);
  assert.deepEqual(state.navigationPages, PAGES);
  state.route = {
    name: "workbench",
    originAnchor: { scrollOffset: 4, followTail: false },
  };

  openCommandQuickOpen(state);
  for (let index = 0; index < 5; index += 1) switchCommandQuickOpenProvider(state);
  assert.equal(state.commandQuickOpen.provider, "pages");
  appendCommandQuickOpenQuery(state, "目标");
  assert.equal(getCommandQuickOpenItems(state)[0].page_id, "goals");

  const rendered = stripAnsi(renderCommandQuickOpenPage(state, 100, 24).join("\n"));
  assert.match(rendered, /页面 QuickOpen/);
  assert.match(rendered, /Goal · \/goal/);
  assert.match(rendered, /不会自动发送或执行/);

  assert.equal(acceptCommandQuickOpen(state), true);
  assert.equal(state.input, "/goal");
  assert.equal(state.commandQuickOpen.open, false);
  assert.equal(state.route.name, "workbench");
});

test("page QuickOpen search is bounded and rejects unsafe templates", () => {
  assert.equal(searchPageEntries(PAGES, "授权")[0].page_id, "permissions");
  assert.equal(searchPageEntries(PAGES, "prmssn")[0].page_id, "permissions");
  assert.equal(pageTemplate(PAGES[0]), "/chat");
  assert.throws(
    () => pageTemplate({ ...PAGES[0], command: "/chat now" }),
    /无法安全填入/,
  );
  assert.deepEqual(
    searchPageEntries([{ ...PAGES[0], label: "坏\n页面" }], ""),
    [],
  );

  const legacyBridgeState = createInitialState();
  openCommandQuickOpen(legacyBridgeState);
  for (let index = 0; index < 5; index += 1) {
    switchCommandQuickOpenProvider(legacyBridgeState);
  }
  assert.match(
    stripAnsi(renderCommandQuickOpenPage(legacyBridgeState, 100, 24).join("\n")),
    /当前 Bridge 未提供页面索引/,
  );
});

test("explicit /chat submission returns from a page and closes live overlays", () => {
  const state = createInitialState();
  state.currentSessionId = "session-page";
  state.route = {
    name: "agents",
    originAnchor: { scrollOffset: 7, followTail: false },
  };
  state.agents.open = true;
  state.agents.revision = 4;
  state.inspector.open = true;
  state.inspector.revision = 3;
  const sent = [];

  handleSubmitText(state, "/chat", (type, payload) => {
    sent.push({ type, payload });
  });

  assert.equal(state.route.name, "conversation");
  assert.equal(state.scrollOffset, 7);
  assert.equal(state.followTail, false);
  assert.equal(state.composerIntent, "chat");
  assert.equal(state.agents.open, false);
  assert.equal(state.inspector.open, false);
  assert.deepEqual(sent.map((item) => item.type), ["agents/request", "inspector/request"]);
  assert(sent.every((item) => item.payload.open === false));
  assert.equal(sent.some((item) => item.type === "submit"), false);
});

test("blocking permission or interaction closes command QuickOpen", () => {
  const permissionState = createInitialState();
  openCommandQuickOpen(permissionState);
  handlePermissionRequest(permissionState, {
    request_id: "perm-1",
    payload: { tool_name: "bash_run", choices: ["allow_once", "deny"] },
  });
  assert.equal(permissionState.commandQuickOpen.open, false);
  assert.equal(permissionState.permission.requestId, "perm-1");

  const interactionState = createInitialState();
  openCommandQuickOpen(interactionState);
  handleInteractionRequest(interactionState, {
    request_id: "ask-1",
    payload: {
      request_id: "ask-1",
      question: "请选择",
      options: [{ label: "继续", value: "continue" }],
    },
  });
  assert.equal(interactionState.commandQuickOpen.open, false);
  assert.equal(interactionState.interaction.requestId, "ask-1");
});

test("command QuickOpen recent ranking matches shared privacy-safe golden", () => {
  let recent = [];
  for (const submission of recencyGolden.submissions) {
    recent = recordRecentCommand(COMMANDS, recent, submission);
  }
  assert.deepEqual(recent, recencyGolden.expected_recent_commands);
  assert.doesNotMatch(JSON.stringify(recent), /private/);
  assert.deepEqual(
    searchCommandEntries(COMMANDS, "", 10, recent)
      .slice(0, 2).map((item) => item.command),
    recencyGolden.empty_query_order_prefix,
  );
  assert.deepEqual(
    searchCommandEntries(COMMANDS, recencyGolden.query, 10, recent)
      .slice(0, 1).map((item) => item.command),
    recencyGolden.query_order_prefix,
  );
});

test("submitted known commands update bounded recency while unknown text does not", () => {
  const state = createInitialState();
  state.slashCommands = COMMANDS;
  handleSubmitText(state, "/h", () => {});
  handleSubmitText(state, "/write secret", () => {});
  handleSubmitText(state, "/unknown secret", () => {});

  assert.deepEqual(state.commandQuickOpen.recentCommands, ["/write", "/help"]);
  openCommandQuickOpen(state);
  assert.equal(getCommandQuickOpenItems(state)[0].command, "/write");
  const rendered = stripAnsi(renderCommandQuickOpenPage(state, 100, 24).join("\n"));
  assert.match(rendered, /\/write.*最近/);
  assert.throws(
    () => recordRecentCommand(COMMANDS, [], "/help", 21),
    /limit/,
  );
});

test("task QuickOpen consumes correlated typed snapshot without opening task panel", () => {
  const state = createInitialState();
  state.input = "保留草稿";
  openCommandQuickOpen(state);
  assert.equal(switchCommandQuickOpenProvider(state, () => "quick-task-1"), "tasks");
  assert.equal(state.commandQuickOpen.taskLoading, true);

  const record = normalizeServerRecord({
    id: "server-task-1",
    request_id: "quick-task-1",
    type: "tasks/snapshot",
    version: 1,
    payload: taskGolden,
  });
  reduceServerEvent(state, record);

  assert.equal(state.commandQuickOpen.taskLoaded, true);
  assert.equal(state.taskPanel.snapshot, null);
  assert.equal(state.messages.length, 0);
  assert.deepEqual(
    getCommandQuickOpenItems(state).map((item) => item.task_id),
    taskGolden.expected_empty_order,
  );
  assert.deepEqual(
    searchTaskEntries(taskGolden.items, taskGolden.expected_localized_query)
      .map((item) => item.task_id),
    [taskGolden.expected_localized_result],
  );
  appendCommandQuickOpenQuery(state, taskGolden.expected_localized_query);
  const rendered = stripAnsi(renderCommandQuickOpenPage(state, 100, 24).join("\n"));
  assert.match(rendered, /任务 QuickOpen/);
  assert.match(rendered, /子智能体/);
  assert.match(rendered, /不会自动发送或执行/);
  assert.equal(acceptCommandQuickOpen(state), true);
  assert.equal(state.input, taskGolden.expected_template);
  assert.equal(state.commandQuickOpen.open, false);

  openCommandQuickOpen(state);
  assert.equal(state.commandQuickOpen.taskLoaded, false);
  assert.equal(state.commandQuickOpen.taskItems.length, 0);
  assert.equal(switchCommandQuickOpenProvider(state, () => "quick-task-2"), "tasks");
  assert.equal(state.commandQuickOpen.taskRequestId, "quick-task-2");
});

test("task QuickOpen keeps correlated load errors inside the overlay", () => {
  const state = createInitialState();
  openCommandQuickOpen(state);
  switchCommandQuickOpenProvider(state, () => "quick-task-error");

  reduceServerEvent(state, {
    id: "server-error",
    request_id: "quick-task-error",
    type: "error",
    payload: { code: "task_panel_failed", message: "任务快照暂不可用" },
  });

  assert.equal(state.commandQuickOpen.taskLoading, false);
  assert.match(state.commandQuickOpen.taskError, /暂不可用/);
  assert.equal(state.messages.length, 0);
  assert.match(
    stripAnsi(renderCommandQuickOpenPage(state, 100, 24).join("\n")),
    /任务快照暂不可用/,
  );
});

test("session QuickOpen consumes correlated workspace snapshot and only fills load", () => {
  const state = createInitialState();
  openCommandQuickOpen(state);
  const requests = { tasks: () => "task-request", sessions: () => "session-request" };
  assert.equal(switchCommandQuickOpenProvider(state, requests), "tasks");
  assert.equal(switchCommandQuickOpenProvider(state, requests), "sessions");
  assert.equal(state.commandQuickOpen.sessionLoading, true);

  reduceServerEvent(state, normalizeServerRecord({
    id: "server-sessions",
    request_id: "session-request",
    type: "sessions/list",
    payload: {
      schema_version: 1, generated_at: "2026-07-22T08:00:00+00:00",
      scope: "workspace", page: 1, page_size: 100, total: 2, query: "",
      items: [
        { session_id: "older", title: "旧会话", model: "m", updated_at: "2026-07-20", message_count: 2, user_message_count: 1, git_branch: "main", is_current: false, resumable: true },
        { session_id: "current", title: "当前会话", model: "m", updated_at: "2026-07-19", message_count: 2, user_message_count: 1, git_branch: "main", is_current: true, resumable: true },
      ], warnings: [],
    },
  }));

  assert.deepEqual(searchSessionEntries(state.commandQuickOpen.sessionItems, "当前").map((item) => item.session_id), ["current"]);
  assert.equal(getCommandQuickOpenItems(state)[0].session_id, "current");
  assert.match(stripAnsi(renderCommandQuickOpenPage(state, 100, 24).join("\n")), /会话 QuickOpen/);
  assert.equal(sessionTemplate(getCommandQuickOpenItems(state)[0]), "/load current");
  assert.equal(acceptCommandQuickOpen(state), true);
  assert.equal(state.input, "/load current");
  assert.equal(state.messages.length, 0);
});

test("file QuickOpen consumes correlated bounded results and only fills read", () => {
  const state = createInitialState();
  openCommandQuickOpen(state);
  const requests = {
    tasks: () => "task-request",
    sessions: () => "session-request",
    files: (query, refresh) => `${query || "root"}-${refresh}`,
  };
  switchCommandQuickOpenProvider(state, requests);
  switchCommandQuickOpenProvider(state, requests);
  assert.equal(switchCommandQuickOpenProvider(state, requests), "files");
  assert.equal(state.commandQuickOpen.fileRequestId, "root-true");

  const payload = {
    schema_version: 1,
    status: "ready",
    revision: 2,
    index_sha256: "a".repeat(64),
    query: "",
    items: [{
      path: "src/中文 file.py",
      name: "中文 file.py",
      directory: "src",
      extension: ".py",
      template: "/read 'src/中文 file.py'",
    }],
    total_indexed: 320,
    truncated: false,
    source: "git",
    built_at: "2026-07-23T00:00:00+00:00",
    message: "",
  };
  assert.equal(applyCommandQuickOpenFileSnapshot(
    state,
    "stale-request",
    payload,
  ), false);
  reduceServerEvent(state, normalizeServerRecord({
    id: "server-files",
    request_id: "root-true",
    type: "workspace/files",
    payload,
  }));

  assert.equal(getCommandQuickOpenItems(state)[0].path, "src/中文 file.py");
  assert.match(stripAnsi(renderCommandQuickOpenPage(state, 100, 24).join("\n")), /Git ignore-aware/);
  assert.equal(acceptCommandQuickOpen(state), true);
  assert.equal(state.input, "/read 'src/中文 file.py'");
  assert.equal(state.messages.length, 0);

  openCommandQuickOpen(state);
  switchCommandQuickOpenProvider(state, requests);
  switchCommandQuickOpenProvider(state, requests);
  switchCommandQuickOpenProvider(state, requests);
  state.commandQuickOpen.query = "router";
  assert.equal(requestCommandQuickOpenFiles(state, requests.files), true);
  assert.equal(state.commandQuickOpen.fileRequestId, "router-false");
});

test("Agent QuickOpen uses one-shot snapshot and deep-links without execution", () => {
  const state = createInitialState();
  state.currentSessionId = "session-agent";
  openCommandQuickOpen(state);
  const requests = {
    tasks: () => "task-request",
    sessions: () => "session-request",
    files: () => "file-request",
    agents: () => "agent-request",
  };
  switchCommandQuickOpenProvider(state, requests);
  switchCommandQuickOpenProvider(state, requests);
  switchCommandQuickOpenProvider(state, requests);
  assert.equal(switchCommandQuickOpenProvider(state, requests), "agents");
  assert.equal(state.commandQuickOpen.agentLoading, true);

  reduceServerEvent(state, normalizeServerRecord({
    id: "server-agents",
    request_id: "agent-request",
    type: "agents/snapshot",
    payload: {
      schema_version: 2,
      session_id: "session-agent",
      revision: 4,
      generated_at: "2026-07-23T00:00:00+00:00",
      summary: {
        total_agents: 2,
        active_agents: 1,
        attention_agents: 0,
        stoppable_executions: 0,
        pending_messages: 0,
      },
      agents: [
        {
          name: "reviewer",
          description: "代码审查",
          kind: "preset",
          state: "idle",
          task_count: 0,
          model_tier: "capable",
          capabilities: ["review"],
          tools: ["read"],
          permission_level: "read_only",
          age_ms: 10,
          heartbeat_age_ms: 5,
        },
        {
          name: "Explore Worker",
          description: "探索项目",
          kind: "dynamic",
          state: "running",
          task_count: 1,
          model_tier: "fast",
          capabilities: ["explore"],
          tools: ["glob"],
          permission_level: "read_only",
          age_ms: 20,
          heartbeat_age_ms: 2,
        },
      ],
      executions: [],
      team_messages: [],
      blackboard: [],
      warnings: [],
    },
  }));

  assert.equal(state.agents.snapshot, null);
  assert.equal(searchAgentEntries(state.commandQuickOpen.agentItems, "代码审查")[0].name, "reviewer");
  assert.equal(getCommandQuickOpenItems(state)[0].name, "Explore Worker");
  assert.equal(agentTemplate(getCommandQuickOpenItems(state)[0]), "/agents agent 'Explore Worker'");
  assert.match(stripAnsi(renderCommandQuickOpenPage(state, 100, 24).join("\n")), /Agent QuickOpen/);
  assert.equal(acceptCommandQuickOpen(state), true);
  assert.equal(state.input, "/agents agent 'Explore Worker'");

  const sent = [];
  handleSubmitText(state, state.input, (type, payload) => {
    sent.push({ type, payload });
    return "agent-open";
  });
  assert.equal(state.route.name, "agents");
  assert.equal(state.agents.detailId, "Explore Worker");
  assert.equal(state.agents.selectedByTab.agents, "Explore Worker");
  assert.equal(sent[0].type, "agents/request");
  assert.equal(sent.some((item) => item.type === "submit"), false);
});

test("Agent QuickOpen quotes shell metacharacters as a plain Agent name", () => {
  const name = "O'Brien $(touch nope)";
  const template = agentTemplate({
    name,
    state: "ready",
    kind: "dynamic",
  });

  assert.equal(template, "/agents agent 'O'\"'\"'Brien $(touch nope)'");
  assert.equal(parseAgentDeepLink(template), name);
});

test("dismissed Agent QuickOpen consumes late correlated snapshots", () => {
  const state = createInitialState();
  state.currentSessionId = "session-agent";
  openCommandQuickOpen(state);
  const requests = {
    tasks: () => "task-request",
    sessions: () => "session-request",
    files: () => "file-request",
    agents: () => "agent-late",
  };
  switchCommandQuickOpenProvider(state, requests);
  switchCommandQuickOpenProvider(state, requests);
  switchCommandQuickOpenProvider(state, requests);
  switchCommandQuickOpenProvider(state, requests);
  closeCommandQuickOpen(state);

  assert.deepEqual(state.commandQuickOpen.agentDiscardRequestIds, ["agent-late"]);
  assert.equal(applyCommandQuickOpenAgentSnapshot(state, "agent-late", {
    session_id: "session-agent",
    revision: 1,
    agents: [],
    warnings: [],
  }), true);
  assert.deepEqual(state.commandQuickOpen.agentDiscardRequestIds, []);
  assert.equal(state.agents.snapshot, null);
});

test("invalid Agent deep links remain local and explain the accepted form", () => {
  const state = createInitialState();
  const sent = [];
  handleSubmitText(state, "/agents execution task-1", (type, payload) => {
    sent.push({ type, payload });
  });

  assert.equal(sent.length, 0);
  assert.match(state.messages.at(-1).content, /\/agents agent <name>/);
});

function page(pageId, commandName, label, description, order, keywords) {
  return {
    schema_version: 1,
    page_id: pageId,
    command: commandName,
    label,
    description,
    keywords: [...keywords].sort(),
    order,
    surface: "new_ui",
  };
}

function command(commandName, { aliases = [], risk, description, syntax = "" }) {
  return {
    command: commandName,
    aliases,
    description,
    schema_version: 1,
    category: "control",
    source: "shared_runtime",
    readonly: risk === "read_only",
    permission_risk: risk,
    arguments: {
      takes_arguments: Boolean(syntax),
      syntax,
      required: syntax.startsWith("<"),
    },
  };
}
