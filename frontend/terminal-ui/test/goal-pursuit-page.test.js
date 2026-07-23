import test from "node:test";
import assert from "node:assert/strict";

import { stripAnsi } from "../src/ansi.js";
import { renderGoalPursuitPage } from "../src/components/goal-pursuit-page.js";

test("Goal page exposes shared interaction detail command for every state", () => {
  const lines = renderGoalPursuitPage({
    snapshot: {
      current_goal_id: "goal-1",
      goals: [{
        goal_id: "goal-1",
        objective: "完成持久交互",
        status: "active",
        session_id: "session-1",
        updated_at: "2026-07-23T00:00:00+00:00",
        pursuit_link_status: "ready",
        pursuit: {
          run_id: "pursuit-1",
          status: "waiting",
          phase: "waiting",
          criteria_verified: 1,
          criteria_total: 2,
          iteration: 1,
          failure_count: 0,
          next_action: "等待回答",
          waits: [],
          evidence: [],
        },
      }],
      interactions: [
        {
          interaction_id: "ask-pending",
          pursuit_run_id: "pursuit-1",
          state: "pending",
          header: "继续方式",
          question: "是否继续？",
          can_cancel: true,
          can_takeover: true,
        },
        {
          interaction_id: "ask-answered",
          pursuit_run_id: "pursuit-1",
          state: "answered",
          header: "继续方式",
          question: "是否继续？",
          can_cancel: false,
          can_takeover: false,
        },
      ],
    },
  }, 160, 30).map(stripAnsi).join("\n");

  assert.match(lines, /\/goal interaction detail ask-pending/);
  assert.match(lines, /\/goal interaction detail ask-answered/);
  assert.match(lines, /\/goal interaction cancel ask-pending/);
  assert.match(lines, /\/goal interaction takeover ask-pending/);
  assert.doesNotMatch(lines, /\/goal interaction cancel ask-answered/);
  assert.doesNotMatch(lines, /\/goal interaction takeover ask-answered/);
});
