import test from "node:test";
import assert from "node:assert/strict";
import { ANSI, stripAnsi, visibleWidth } from "../src/ansi.js";
import {
  renderWelcomeScreen,
  selectWelcomeLayout,
  shouldRenderWelcome,
} from "../src/components/welcome-screen.js";
import { createInitialState } from "../src/state.js";

function readyState() {
  const state = createInitialState();
  state.welcome = { phase: "ready_empty", dismissed: false };
  state.status = {
    version: "0.1.214",
    workspace_root: "/Users/lv/Workspace/NaumiAgent",
    model: "openai/gpt-5.4",
    provider: "openai",
    api_format: "openai_responses",
    upstream_model: "gpt-5.4-2026-06-01",
    mode: "default",
    permission_mode: "moderate",
  };
  return state;
}

test("selects the four exact welcome layouts", () => {
  assert.equal(selectWelcomeLayout(120, 20), "wide");
  assert.equal(selectWelcomeLayout(80, 12), "medium");
  assert.equal(selectWelcomeLayout(48, 8), "compact");
  assert.equal(selectWelcomeLayout(23, 20), "minimal");
  assert.equal(selectWelcomeLayout(120, 3), "minimal");
});

test("renders bounded authoritative facts in every layout", () => {
  for (const [width, height] of [[120, 20], [80, 12], [48, 8], [23, 3]]) {
    const lines = renderWelcomeScreen(readyState(), width, height, {
      home: "/Users/lv",
    });
    const plain = lines.map(stripAnsi).join("\n");
    assert.equal(lines.length, height);
    assert(lines.every((line) => visibleWidth(line) <= width));
    assert.match(plain, /NAUMI|NaumiAgent/);
    assert.match(plain, /已就绪/);
  }

  const wide = renderWelcomeScreen(readyState(), 120, 20, {
    home: "/Users/lv",
  }).map(stripAnsi).join("\n");
  assert.match(wide, /NaumiAgent v0\.1\.214/);
  assert.match(wide, /工作区 ~\/Workspace\/NaumiAgent/);
  assert.match(wide, /模型 openai\/gpt-5\.4/);
  assert.match(wide, /提供方 openai · 接口 OpenAI Responses/);
  assert.match(wide, /上游 gpt-5\.4-2026-06-01/);
  assert.match(wide, /模式 default · 权限 moderate/);
});

test("booting and missing facts never invent runtime values", () => {
  const state = createInitialState();
  const booting = renderWelcomeScreen(state, 100, 16).map(stripAnsi).join("\n");
  assert.match(booting, /正在启动本地运行时/);
  assert.doesNotMatch(booting, /未解析/);

  state.welcome = { phase: "ready_empty", dismissed: false };
  const unresolved = renderWelcomeScreen(state, 100, 16).map(stripAnsi).join("\n");
  assert.match(unresolved, /NaumiAgent v未解析/);
  assert.match(unresolved, /模型 未解析/);
});

test("uses semantic ANSI colors including bypass warning", () => {
  const state = readyState();
  state.status.mode = "bypass";
  state.status.permission_mode = "bypass";
  const rendered = renderWelcomeScreen(state, 120, 20).join("\n");
  assert(rendered.includes(`${ANSI.bold}${ANSI.cyan}█`));
  assert(rendered.includes(`${ANSI.green}已就绪`));
  assert(rendered.includes(`${ANSI.yellow}bypass`));
});

test("welcome visibility is pure and excludes other pages", () => {
  const state = createInitialState();
  assert.equal(shouldRenderWelcome(state), true);
  state.inspector.open = true;
  assert.equal(shouldRenderWelcome(state), false);
  state.inspector.open = false;
  state.route = { name: "agents" };
  assert.equal(shouldRenderWelcome(state), false);
  state.route = { name: "conversation" };
  state.welcome = { phase: "dismissed", dismissed: true };
  assert.equal(shouldRenderWelcome(state), false);
});
