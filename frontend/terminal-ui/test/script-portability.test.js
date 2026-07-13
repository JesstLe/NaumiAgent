import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import test from "node:test";

test("syntax checker resolves package paths outside the package cwd", () => {
  const script = fileURLToPath(new URL("../scripts/check-syntax.js", import.meta.url));
  const result = spawnSync(process.execPath, [script], {
    cwd: tmpdir(),
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /Syntax check passed/);
});
