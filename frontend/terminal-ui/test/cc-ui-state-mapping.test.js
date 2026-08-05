import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { verifyUiStateMapping } from "../scripts/verify-cc-ui-state-mapping.js";

const UI_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PROJECT_ROOT = path.resolve(UI_ROOT, "../..");
const MAPPING_PATH = path.join(UI_ROOT, "cc-ui-state-mapping.v1.json");
const SEMANTIC_PATH = path.join(UI_ROOT, "cc-semantic-mapping.v1.json");
const CONTRACT_PATH = path.join(UI_ROOT, "protocol-contract.json");
const STATE_PATH = path.join(UI_ROOT, "src/state.js");
const SCRIPT_PATH = path.join(UI_ROOT, "scripts/verify-cc-ui-state-mapping.js");

function inputs() {
  return {
    mapping: JSON.parse(readFileSync(MAPPING_PATH, "utf8")),
    semantic: JSON.parse(readFileSync(SEMANTIC_PATH, "utf8")),
    contract: JSON.parse(readFileSync(CONTRACT_PATH, "utf8")),
  };
}

function verify(mapping, semantic, contract, extra = {}) {
  return verifyUiStateMapping(mapping, semantic, contract, {
    projectRoot: PROJECT_ROOT,
    stateModulePath: "frontend/terminal-ui/src/state.js",
    ...extra,
  });
}

test("CC-03.2b mapping executes all UI-local probes deterministically", () => {
  const { mapping, semantic, contract } = inputs();

  const first = verify(mapping, semantic, contract);
  const second = verify(mapping, semantic, contract);

  assert.deepEqual(first, second);
  assert.equal(first.status, "valid");
  assert.deepEqual(first.finding_codes, []);
  assert.equal(first.ui_local_cell_count, 14);
  assert.equal(first.mapping_count, 14);
  assert.equal(first.verified_probe_count, 14);
  assert.equal(first.verified_target_test_count, 16);
});

test("CC-03.2b mapping rejects incomplete UI-local coverage", () => {
  const { mapping, semantic, contract } = inputs();
  mapping.mappings.pop();

  const audit = verify(mapping, semantic, contract);

  assert.equal(audit.status, "invalid");
  assert(audit.finding_codes.includes("mapping_shape_invalid"));
  assert(audit.finding_codes.includes("ui_local_coverage_mismatch"));
});

test("CC-03.2b mapping reports stale semantic and state bindings", () => {
  const { mapping, semantic, contract } = inputs();
  mapping.semantic_mapping_sha256 = "0".repeat(64);
  mapping.state_module_sha256 = "1".repeat(64);

  const audit = verify(mapping, semantic, contract);

  assert.equal(audit.status, "stale");
  assert.deepEqual(audit.finding_codes, ["mapping_binding_mismatch", "state_module_stale"]);
});

test("CC-03.2b mapping checks protocol registry and target test anchors", () => {
  const { mapping, semantic, contract } = inputs();
  delete contract.event_registry.client.task_cancel;
  const cancel = mapping.mappings.find((item) => item.cell_id === "task.cancel");
  cancel.target_tests[0].test_name = "missing task cancel contract test";

  const audit = verify(mapping, semantic, contract);

  assert.equal(audit.status, "invalid");
  assert(audit.finding_codes.includes("client_event_missing"));
  assert(audit.finding_codes.includes("protocol_contract_stale"));
  assert(audit.finding_codes.includes("target_test_missing"));
});

test("CC-03.2b mapping fails closed when a real transition probe fails", () => {
  const { mapping, semantic, contract } = inputs();

  const audit = verify(mapping, semantic, contract, {
    probeOverrides: {
      "task.loading": { run() { throw new Error("forced transition failure"); } },
    },
  });

  assert.equal(audit.status, "invalid");
  assert.deepEqual(audit.finding_codes, ["probe_failed"]);
  assert.equal(audit.verified_probe_count, 13);
});

test("CC-03.2b verifier CLI is read-only", () => {
  const before = [MAPPING_PATH, SEMANTIC_PATH, CONTRACT_PATH, STATE_PATH]
    .map((file) => readFileSync(file));
  const cwd = mkdtempSync(path.join(tmpdir(), "naumi-cc-ui-state-"));
  const result = spawnSync(process.execPath, [
    SCRIPT_PATH,
    "--mapping", "frontend/terminal-ui/cc-ui-state-mapping.v1.json",
    "--semantic-mapping", "frontend/terminal-ui/cc-semantic-mapping.v1.json",
    "--protocol-contract", "frontend/terminal-ui/protocol-contract.json",
    "--state-module", "frontend/terminal-ui/src/state.js",
    "--project-root", PROJECT_ROOT,
  ], { cwd, encoding: "utf8" });

  assert.equal(result.status, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).status, "valid");
  for (const [index, file] of [MAPPING_PATH, SEMANTIC_PATH, CONTRACT_PATH, STATE_PATH].entries()) {
    assert.deepEqual(readFileSync(file), before[index]);
  }
});
