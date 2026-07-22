import { createHash } from "node:crypto";

import { stripAnsi, visibleWidth } from "../ansi.js";
import {
  createGoldenCaptureState,
  validateCaptureFixture,
} from "../golden-capture.js";
import {
  INK_EXPERIMENT_RENDERER,
  renderInkExperimentScreen,
} from "./ink-renderer.js";

export function captureInkExperimentGoldenFrame(fixture, options = {}) {
  const capture = validateCaptureFixture(fixture);
  const width = options.width == null ? capture.width : Number(options.width);
  const height = options.height == null ? capture.height : Number(options.height);
  const state = createGoldenCaptureState(fixture);
  const lines = renderInkExperimentScreen(state, width, height);
  const ansi = `${lines.join("\n")}\n`;
  const text = `${lines.map(stripAnsi).join("\n")}\n`;
  return {
    schema: "naumi.terminal-golden-frame.v1",
    surface: "new_ui_experiment",
    renderer: INK_EXPERIMENT_RENDERER,
    width,
    height,
    ansi,
    text,
    ansi_sha256: sha256(ansi),
    text_sha256: sha256(text),
    line_count: lines.length,
    max_visible_width: Math.max(0, ...lines.map(visibleWidth)),
    required_anchors: [...capture.required_anchors],
    missing_anchors: capture.required_anchors.filter((anchor) => !text.includes(anchor)),
  };
}

function sha256(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}
