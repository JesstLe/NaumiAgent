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
          boundary_decision: {
            schema_version: 1,
            decision_id: "a".repeat(64),
            facts_sha256: "b".repeat(64),
            status: "waiting",
            code: "waiting_for_interaction",
            reason: "目标追踪正在等待用户回答。",
            next_action: "回答当前交互后继续。",
            terminal: false,
            resumable: true,
          },
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
  assert.match(lines, /最近裁判 · waiting_for_interaction · waiting · a{12}/);
  assert.match(lines, /目标追踪正在等待用户回答/);
  assert.doesNotMatch(lines, /\/goal interaction cancel ask-answered/);
  assert.doesNotMatch(lines, /\/goal interaction takeover ask-answered/);
});

test("Goal page highlights ledger selection and renders typed detail", () => {
  const lines = renderGoalPursuitPage({
    selectedInteractionIndex: 0,
    snapshot: {
      current_goal_id: "goal-1",
      goals: [{
        goal_id: "goal-1",
        objective: "查看详情",
        status: "active",
        session_id: "session-1",
        updated_at: "now",
        pursuit_link_status: "ready",
        pursuit: {
          run_id: "pursuit-1",
          status: "waiting",
          phase: "waiting",
          criteria_verified: 0,
          criteria_total: 1,
          iteration: 1,
          failure_count: 0,
          next_action: "等待",
          waits: [],
          evidence: [],
        },
      }],
      interaction_filter: "pending",
      interaction_cursor: "",
      interaction_has_more: true,
      interactions: [{
        interaction_id: "ask-selected",
        pursuit_run_id: "pursuit-1",
        state: "pending",
        header: "测试方式",
        question: "请选择。",
        can_cancel: true,
        can_takeover: false,
      }],
      selected_interaction: {
        interaction_id: "ask-selected",
        pursuit_run_id: "pursuit-1",
        state: "pending",
        header: "测试方式",
        question: "请选择。",
        options: [{
          value: "focused",
          label: "小模块测试",
          description: "只跑定向测试",
        }],
        allow_custom: true,
        custom_label: "其他",
        sequence: 2,
        owner_epoch: 1,
        question_expired: false,
        lease_expired: true,
      },
    },
  }, 160, 40).map(stripAnsi).join("\n");

  assert.match(lines, /› ask-selected · 等待回答/);
  assert.match(lines, /用户交互账本 · 等待回答 · 第 1 页/);
  assert.match(lines, /小模块测试 \(focused\) · 只跑定向测试/);
  assert.match(lines, /owner 租约已过期/);
  assert.match(lines, /n 下一页/);
});
