import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi, visibleWidth } from "../src/ansi.js";
import { StatusFooter } from "../src/components/footer.js";
import { createInitialState } from "../src/state.js";

test("footer distinguishes thinking visibility from model reasoning effort", () => {
  const state = createInitialState();
  state.showReasoning = true;
  state.status = {
    model: "openai/gpt-5.4",
    workspace_root: "/tmp/project",
    reasoning_effort: {
      model: "openai/gpt-5.4",
      effective: "high",
      source: "runtime",
      supported: ["low", "high"],
      default: "low",
      warning: null,
    },
    usage: { total_tokens: 12 },
  };

  const lines = StatusFooter({ state, env: { cwd: "/tmp/project" } })
    .render({ width: 52 });
  const plain = lines.map(stripAnsi).join("\n");

  assert.match(plain, /思考文本: on/);
  assert.match(plain, /强度: high/);
  assert(lines.every((line) => visibleWidth(line) <= 52));
  assert.equal(plain.includes("..."), false);
});
