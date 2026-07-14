import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi, visibleWidth } from "../src/ansi.js";
import {
  WORKING_FRAME_COUNT,
  renderWorkingIndicator,
  shouldAnimateWorkingIndicator,
  workingIndicatorStatus,
} from "../src/components/working-indicator.js";
import { createWorkingAnimationController } from "../src/working-animation.js";

function runningState(phase = "generating", overrides = {}) {
  return {
    running: true,
    workingAnimationFrame: 0,
    activeRunActivity: {
      phase,
      phaseLabel: {
        preparing: "准备运行",
        generating: "生成响应",
        executing: "执行工具",
        awaiting_permission: "等待权限",
        summarizing: "整理结果",
      }[phase] ?? phase,
    },
    permission: null,
    cancelPending: false,
    ...overrides,
  };
}

test("working indicator renders four stable wide image frames", () => {
  const frames = Array.from({ length: WORKING_FRAME_COUNT }, (_, frame) => {
    const state = runningState("generating", { workingAnimationFrame: frame });
    return renderWorkingIndicator(state, 100, { bodyHeight: 12, term: "xterm-256color" });
  });

  assert.equal(frames.length, 4);
  assert(frames.every((lines) => lines.length === 3));
  assert.equal(new Set(frames.map((lines) => lines.map(stripAnsi).join("\n"))).size, 4);
  assert(frames.every((lines) => lines.some((line) => stripAnsi(line).includes("模型工作中"))));
  assert(frames.every((lines) => lines.every((line) => visibleWidth(line) <= 100)));

  const widths = frames.map((lines) => lines.map((line) => visibleWidth(line)));
  assert.deepEqual(widths[0], widths[1]);
  assert.deepEqual(widths[1], widths[2]);
  assert.deepEqual(widths[2], widths[3]);
});

test("working indicator uses compact and dumb-terminal fallbacks", () => {
  const state = runningState("generating", { workingAnimationFrame: 2 });
  const compact = renderWorkingIndicator(state, 60, { bodyHeight: 6, term: "xterm" });
  const dumb = renderWorkingIndicator(state, 100, { bodyHeight: 12, term: "dumb" });

  assert.equal(compact.length, 1);
  assert.match(stripAnsi(compact[0]), /◑ 模型工作中 · 生成响应/);
  assert.equal(dumb.length, 1);
  assert.match(stripAnsi(dumb[0]), /^\[o\] 模型工作中 · 生成响应$/);
  assert.doesNotMatch(stripAnsi(dumb[0]), /[◐◓◑◒╭╮│─◉•]/);
});

test("working indicator bounds long untrusted CJK phase labels", () => {
  const state = runningState("generating", {
    activeRunActivity: {
      phase: "generating",
      phaseLabel: `${"非常长的模型阶段".repeat(20)}\x1b]8;;https://evil.test\x07隐藏\x1b]8;;\x07`,
    },
  });

  const rendered = renderWorkingIndicator(state, 70, {
    bodyHeight: 10,
    term: "xterm-256color",
  });

  assert.equal(rendered.length, 3);
  assert(rendered.every((line) => visibleWidth(line) <= 70));
  assert.doesNotMatch(rendered.join(""), /evil\.test|\x1b]8/);
  assert(rendered.some((line) => stripAnsi(line).includes("…")));
});

test("working indicator distinguishes tool permission and cancellation phases", () => {
  const tool = workingIndicatorStatus(runningState("executing"));
  const waitingState = runningState("awaiting_permission", {
    permission: { requestId: "p-1" },
  });
  const waiting0 = renderWorkingIndicator(
    { ...waitingState, workingAnimationFrame: 0 },
    100,
    { bodyHeight: 12, term: "xterm" },
  );
  const waiting3 = renderWorkingIndicator(
    { ...waitingState, workingAnimationFrame: 3 },
    100,
    { bodyHeight: 12, term: "xterm" },
  );
  const cancelling = workingIndicatorStatus(runningState("executing", { cancelPending: true }));

  assert.equal(tool.label, "工具执行中");
  assert.equal(tool.animate, true);
  assert.equal(stripAnsi(waiting0.join("\n")), stripAnsi(waiting3.join("\n")));
  assert.match(stripAnsi(waiting0.join("\n")), /等待权限确认/);
  assert.equal(waiting0.length, 3);
  assert.equal(cancelling.label, "正在取消");
  assert.equal(cancelling.animate, false);
});

test("working indicator animation capability follows terminal and run state", () => {
  const capable = { isTTY: true, term: "xterm-256color", ci: false, reduceMotion: false };

  assert.equal(shouldAnimateWorkingIndicator(runningState("generating"), capable), true);
  assert.equal(shouldAnimateWorkingIndicator(runningState("executing"), capable), true);
  assert.equal(shouldAnimateWorkingIndicator(runningState("awaiting_permission"), capable), false);
  assert.equal(shouldAnimateWorkingIndicator(runningState("generating", { permission: {} }), capable), false);
  assert.equal(shouldAnimateWorkingIndicator(runningState("generating", { cancelPending: true }), capable), false);
  assert.equal(shouldAnimateWorkingIndicator({ running: false }, capable), false);
  assert.equal(shouldAnimateWorkingIndicator(runningState(), { ...capable, isTTY: false }), false);
  assert.equal(shouldAnimateWorkingIndicator(runningState(), { ...capable, term: "dumb" }), false);
  assert.equal(shouldAnimateWorkingIndicator(runningState(), { ...capable, ci: true }), false);
  assert.equal(shouldAnimateWorkingIndicator(runningState(), { ...capable, reduceMotion: true }), false);
});

test("working animation controller owns one resumable unref timer", () => {
  const callbacks = [];
  const cleared = [];
  const frames = [];
  let unrefCount = 0;
  const setTimer = (callback, intervalMs) => {
    const handle = {
      callback,
      intervalMs,
      unref() {
        unrefCount += 1;
      },
    };
    callbacks.push(handle);
    return handle;
  };
  const clearTimer = (handle) => cleared.push(handle);
  const controller = createWorkingAnimationController({
    onFrame: (frame) => frames.push(frame),
    setTimer,
    clearTimer,
    intervalMs: 120,
    frameCount: WORKING_FRAME_COUNT,
  });

  controller.sync(true);
  controller.sync(true);
  assert.equal(callbacks.length, 1);
  assert.equal(callbacks[0].intervalMs, 120);
  assert.equal(unrefCount, 1);
  assert.equal(controller.active, true);

  callbacks[0].callback();
  callbacks[0].callback();
  callbacks[0].callback();
  callbacks[0].callback();
  assert.deepEqual(frames, [1, 2, 3, 0]);

  controller.sync(false);
  controller.sync(false);
  assert.equal(cleared.length, 1);
  assert.equal(controller.active, false);

  controller.sync(true);
  assert.equal(callbacks.length, 2);
  controller.stop();
  assert.equal(cleared.length, 2);
});
