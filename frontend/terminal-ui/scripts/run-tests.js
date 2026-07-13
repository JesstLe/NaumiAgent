import { readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join } from "node:path";

const testFiles = readdirSync("test", { withFileTypes: true })
  .filter((entry) => entry.isFile() && entry.name.endsWith(".test.js"))
  .map((entry) => join("test", entry.name))
  .sort();

if (testFiles.length === 0) {
  console.error("No terminal UI test files were found.");
  process.exit(1);
}

const result = spawnSync(process.execPath, ["--test", ...testFiles], {
  stdio: "inherit",
});
if (result.error) {
  console.error(`Unable to run terminal UI tests: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);

