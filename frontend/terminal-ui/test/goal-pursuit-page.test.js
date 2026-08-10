import test from "node:test";
import assert from "node:assert/strict";

import { stripAnsi } from "../src/ansi.js";
import { renderGoalPursuitPage } from "../src/components/goal-pursuit-page.js";

test("Goal page renders typed terminal outbox health without a Goal", () => {
  const lines = renderGoalPursuitPage({
    snapshot: {
      include_finished: true,
      goals: [],
      terminal_outbox: {
        enabled: true,
        status: "degraded",
        worker_state: "waiting",
        counts: {
          total_pending: 3,
          due: 1,
          backoff: 1,
          live_claimed: 0,
          expired_claimed: 1,
          dead_letter: 0,
        },
        pass_count: 9,
        delivered_count: 4,
        retry_scheduled_count: 2,
        dead_lettered_count: 0,
        failure_count: 1,
        next_delay_seconds: 12.5,
        failure_codes: ["dispatch_claim_failed"],
        warning: "",
      },
    },
  }, 160, 20).map(stripAnsi).join("\n");

  assert.match(lines, /终态自动恢复/);
  assert.match(lines, /部分失败 · Worker 等待中/);
  assert.match(lines, /队列 3 · 到期 1 · 退避 1 · 认领 0 · 过期认领 1 · 死信 0/);
  assert.match(lines, /累计 · 轮次 9 · 已收口 4 · 已退避 2 · 死信 0 · 失败 1/);
  assert.match(lines, /最近失败 · dispatch_claim_failed/);
  assert.match(lines, /下次检查 · 约 12\.5s/);
});

test("Goal page renders dead-letter authority as an actionable degraded state", () => {
  const lines = renderGoalPursuitPage({
    snapshot: {
      include_finished: true,
      goals: [],
      terminal_outbox: {
        enabled: true,
        status: "degraded",
        worker_state: "waiting",
        counts: {
          total_pending: 1,
          due: 0,
          backoff: 0,
          live_claimed: 0,
          expired_claimed: 0,
          dead_letter: 1,
        },
        pass_count: 1,
        delivered_count: 0,
        retry_scheduled_count: 0,
        dead_lettered_count: 1,
        failure_count: 1,
        next_delay_seconds: 30,
        failure_codes: ["dead_letter_present", "lease_missing"],
        warning: "存在 1 条终态恢复死信，自动重试已停止，请人工审查。",
        dead_letters: [{
          dead_letter_id: `ptfail_${"a".repeat(24)}`,
          disposition: "permanent",
          failure_code: "lease_missing",
          failure_attempts: 1,
          total_claim_attempts: 3,
          occurred_at: "2026-08-05T00:00:10+00:00",
          manual_review_required: true,
        }],
        dead_letters_truncated: false,
      },
    },
  }, 160, 20).map(stripAnsi).join("\n");

  assert.match(lines, /队列 1 .* 死信 1/);
  assert.match(lines, /累计 .* 死信 1 .* 失败 1/);
  assert.match(lines, /自动重试已停止，请人工审查/);
  assert.match(lines, /ptfail_a{24} · 机械不变量破坏 · lease_missing/);
  assert.match(lines, /▶ 死信 ptfail_a{24}/);
  assert.match(lines, /\/pursue outbox requeue ptfail_a{24}/);
  assert.match(lines, /放弃原因 · 不再需要 \(no_longer_required\)/);
  assert.match(lines, /\/pursue outbox abandon ptfail_a{24} no_longer_required/);
});

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
            schema_version: 2,
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

test("Goal page renders authority-owned resume action and attempt state", () => {
  const lines = renderGoalPursuitPage({
    recoveryActionPending: false,
    recoveryActionNotice: "恢复请求已准入。",
    snapshot: {
      current_goal_id: "goal-1",
      goals: [{
        goal_id: "goal-1",
        objective: "恢复长期任务",
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
          next_action: "继续",
          waits: [],
          evidence: [],
          recovery: {
            recovery_state: "waiting",
            heartbeat: { health: "missing", instance_id: "", sequence: 0, age_seconds: 0 },
            lease: { status: "missing", owner_id: "", epoch: 0, expired: false },
            checkpoint: { status: "ready", sequence: 2, phase: "waiting" },
            reconcile_required: false,
            alerts: [],
            resume_action: {
              state: "available",
              code: "resume_ready",
              reason: "Checkpoint 与恢复边界可验证。",
              command: "/pursue resume pursuit-1",
            },
            attempts: [{
              attempt_id: `recovery-${"a".repeat(64)}`,
              state: "admitted",
              result_code: "",
              updated_at: "2026-07-24T00:00:00+00:00",
            }],
          },
        },
      }],
      interactions: [],
    },
  }, 180, 32).map(stripAnsi).join("\n");

  assert.match(lines, /x 恢复当前 Pursuit/);
  assert.match(lines, /恢复请求已准入/);
  assert.match(lines, /恢复动作 · 可恢复 · resume_ready/);
  assert.match(lines, /\/pursue resume pursuit-1/);
  assert.match(lines, /恢复请求 · 最近 1 项/);
  assert.match(lines, /已准入/);
  assert.match(lines, new RegExp(`/pursue reconcile recovery-${"a".repeat(64)}`));
});
