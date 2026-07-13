import { readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = fileURLToPath(new URL("..", import.meta.url));
const testDirectory = join(packageRoot, "test");
const testFiles = readdirSync(testDirectory, { withFileTypes: true })
  .filter((entry) => entry.isFile() && entry.name.endsWith(".test.js"))
  .map((entry) => join(testDirectory, entry.name))
  .sort();

if (testFiles.length === 0) {
  console.error("No terminal UI test files were found.");
  process.exit(1);
}

const result = spawnSync(process.execPath, ["--test", ...testFiles], {
  cwd: packageRoot,
  stdio: "inherit",
});
if (result.error) {
  console.error(`Unable to run terminal UI tests: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
