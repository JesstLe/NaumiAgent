import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

import { stripAnsi, visibleWidth } from "./ansi.js";
import { renderScreen } from "./render.js";
import {
  createInitialState,
  reduceServerEvent,
  submitUserMessage,
} from "./state.js";

const CAPTURE_SCHEMA = "naumi.terminal-golden-frame.v1";
const MIN_WIDTH = 40;
const MAX_WIDTH = 400;
const MIN_HEIGHT = 12;
const MAX_HEIGHT = 200;

export function captureNewUiGoldenFrame(fixture, options = {}) {
  const capture = validateCaptureFixture(fixture);
  const width = boundedDimension(
    options.width ?? capture.width,
    "width",
    MIN_WIDTH,
    MAX_WIDTH,
  );
  const height = boundedDimension(
    options.height ?? capture.height,
    "height",
    MIN_HEIGHT,
    MAX_HEIGHT,
  );
  const state = createGoldenCaptureState(fixture);
  const lines = renderScreen(state, width, height, {
    cwd: "/workspace/naumi",
    home: "/home/naumi",
    clockText: "12:34:56",
  });
  if (lines.length !== height || lines.some((line) => visibleWidth(line) > width)) {
    throw new Error("New UI capture 超出固定终端视口");
  }
  const ansi = `${lines.join("\n")}\n`;
  const text = `${lines.map(stripAnsi).join("\n")}\n`;
  const missingAnchors = capture.required_anchors.filter(
    (anchor) => !text.includes(anchor),
  );
  return {
    schema: CAPTURE_SCHEMA,
    surface: "new_ui",
    renderer: "node-ansi",
    width,
    height,
    ansi,
    text,
    ansi_sha256: sha256(ansi),
    text_sha256: sha256(text),
    line_count: lines.length,
    max_visible_width: Math.max(0, ...lines.map(visibleWidth)),
    required_anchors: [...capture.required_anchors],
    missing_anchors: missingAnchors,
  };
}

export function validateCaptureFixture(fixture) {
  if (!fixture || typeof fixture !== "object" || Array.isArray(fixture)) {
    throw new Error("golden fixture 必须是 JSON 对象");
  }
  if (fixture.schema_version !== 1) {
    throw new Error("golden fixture schema_version 必须为 1");
  }
  const capture = fixture.capture;
  if (!capture || typeof capture !== "object" || capture.schema_version !== 1) {
    throw new Error("golden fixture 缺少 capture schema v1");
  }
  if (!fixture.submission?.text || !fixture.submission?.request_id) {
    throw new Error("golden fixture 缺少稳定 submission identity");
  }
  if (!Array.isArray(fixture.tool_lifecycle?.events)
      || fixture.tool_lifecycle.events.length === 0) {
    throw new Error("golden fixture 缺少 tool lifecycle events");
  }
  for (const event of fixture.tool_lifecycle.events) {
    if (!event?.engine_event || !event?.expected_message?.type) {
      throw new Error("golden fixture tool event 不完整");
    }
  }
  if (!fixture.completion?.expected_message?.receipt) {
    throw new Error("golden fixture 缺少 completion receipt");
  }
  const anchors = capture.required_anchors;
  if (!Array.isArray(anchors) || anchors.length === 0 || anchors.length > 20) {
    throw new Error("capture.required_anchors 必须包含 1-20 项");
  }
  const requiredAnchors = anchors.map((value) => String(value ?? "").trim());
  if (requiredAnchors.some((value) => !value || value.length > 200)) {
    throw new Error("capture.required_anchors 含空值或超长内容");
  }
  return {
    width: boundedDimension(capture.width, "width", MIN_WIDTH, MAX_WIDTH),
    height: boundedDimension(capture.height, "height", MIN_HEIGHT, MAX_HEIGHT),
    required_anchors: requiredAnchors,
  };
}

export function createGoldenCaptureState(fixture) {
  const state = createInitialState();
  submitUserMessage(state, fixture.submission.text, () => {});
  const requestId = String(fixture.submission.request_id);
  const receipt = structuredClone(fixture.completion.expected_message.receipt);
  reduceServerEvent(state, {
    type: "run/started",
    request_id: requestId,
    payload: {
      run_id: receipt.run_id,
      task: fixture.submission.text,
    },
  });
  for (const event of fixture.tool_lifecycle.events) {
    reduceServerEvent(state, {
      type: "ui/message",
      request_id: requestId,
      payload: structuredClone(event.expected_message),
    });
  }
  reduceServerEvent(state, {
    type: "completion/receipt",
    request_id: requestId,
    payload: receipt,
  });
  reduceServerEvent(state, {
    type: "run/completed",
    request_id: requestId,
    payload: {
      run_id: receipt.run_id,
      receipt_id: receipt.receipt_id,
      status: receipt.outcome,
    },
  });
  const runActivity = state.messages.find((message) => message.kind === "run_activity");
  if (runActivity) {
    runActivity.durationMs = Math.max(0, Number(receipt.duration_ms) || 0);
    runActivity.startedAtMs = 0;
    runActivity.completedAtMs = 0;
  }
  return state;
}

function boundedDimension(value, name, minimum, maximum) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new Error(`${name} 必须是 ${minimum}-${maximum} 的整数`);
  }
  return parsed;
}

function sha256(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (!["--fixture", "--width", "--height"].includes(key)) {
      throw new Error(`未知参数: ${key}`);
    }
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`${key} 缺少值`);
    values[key.slice(2)] = value;
    index += 1;
  }
  if (!values.fixture) throw new Error("--fixture 为必填项");
  return values;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const fixtureBytes = readFileSync(args.fixture);
  const fixture = JSON.parse(fixtureBytes.toString("utf8"));
  const frame = captureNewUiGoldenFrame(fixture, {
    width: args.width,
    height: args.height,
  });
  process.stdout.write(`${JSON.stringify({
    fixture_sha256: createHash("sha256").update(fixtureBytes).digest("hex"),
    frame,
  })}\n`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    main();
  } catch (error) {
    process.stderr.write(`终端 golden capture 失败: ${error.message}\n`);
    process.exitCode = 2;
  }
}
