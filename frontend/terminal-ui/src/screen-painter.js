import { ANSI } from "./ansi.js";

export function createScreenPainter({ write }) {
  if (typeof write !== "function") {
    throw new TypeError("屏幕绘制器需要 write 回调");
  }

  let previous = null;
  let previousWidth = null;
  let previousHeight = null;

  function paint(lines, width, height) {
    const frame = validateFrame(lines, width, height);
    const requiresFullPaint = previous === null
      || previousWidth !== frame.width
      || previousHeight !== frame.height
      || previous.length !== frame.lines.length;

    if (requiresFullPaint) {
      const output = ANSI.clear + frame.lines.join("\n");
      commit(output);
      remember(frame);
      return { mode: "full", changedRows: frame.lines.length, written: true };
    }

    const changes = [];
    for (let index = 0; index < frame.lines.length; index += 1) {
      if (frame.lines[index] === previous[index]) continue;
      changes.push(`${ANSI.cursorTo(index + 1, 1)}${frame.lines[index]}`);
    }

    if (!changes.length) {
      return { mode: "none", changedRows: 0, written: false };
    }

    commit(changes.join(""));
    remember(frame);
    return { mode: "diff", changedRows: changes.length, written: true };
  }

  function commit(output) {
    write(`${ANSI.synchronizedOutputOn}${output}${ANSI.synchronizedOutputOff}`);
  }

  function remember(frame) {
    previous = frame.lines.slice();
    previousWidth = frame.width;
    previousHeight = frame.height;
  }

  return {
    paint,
    reset() {
      previous = null;
      previousWidth = null;
      previousHeight = null;
    },
  };
}

function validateFrame(lines, width, height) {
  if (!Array.isArray(lines) || lines.some((line) => typeof line !== "string")) {
    throw new TypeError("终端画面必须是字符串行数组");
  }
  const safeWidth = Math.trunc(Number(width));
  const safeHeight = Math.trunc(Number(height));
  if (!Number.isInteger(safeWidth) || safeWidth < 1) {
    throw new RangeError("终端画面宽度必须是正整数");
  }
  if (!Number.isInteger(safeHeight) || safeHeight < 1) {
    throw new RangeError("终端画面高度必须是正整数");
  }
  if (lines.length !== safeHeight) {
    throw new RangeError(`终端画面行数 ${lines.length} 与高度 ${safeHeight} 不一致`);
  }
  return { lines, width: safeWidth, height: safeHeight };
}
