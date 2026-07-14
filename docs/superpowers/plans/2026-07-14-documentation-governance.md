# Documentation Governance Implementation Plan

> **Goal:** Turn `docs/README.md` into the current documentation map, classify every Markdown document, update live entry instructions, and enforce the result with a real repository scanner.

**Architecture:** Keep historical plans, specs, audits, and migration notes immutable. A stdlib-only Python checker loads `docs/governance.json`, classifies every root/docs Markdown file with non-overlapping glob rules, validates local links, and applies retired-command rules only to current documents. Current user-facing documents are updated to the implemented Terminal UI/Textual fallback behavior; old Prompt Toolkit source remains untouched.

**Verification policy:** Run only the focused docs-governance tests, focused Ruff checks, the real repository scanner, narrow `rg` assertions, and `git diff --check`. Do not run the full test suite.

---

## Task 1: Add failing governance tests

**Files:**

- Create: `tests/unit/test_docs_governance.py`
- Planned implementation: `scripts/check_docs.py`

Test the checker as an importable module against temporary repositories:

1. a valid manifest and document tree passes;
2. an unclassified Markdown file fails with its path;
3. overlapping rules fail instead of silently taking the first match;
4. a missing local Markdown/image target fails;
5. external links, pure anchors and links inside fenced code are ignored;
6. retired public commands fail in `current` documents;
7. the same command text is allowed in `historical` documents;
8. malformed manifest versions, statuses and rules produce Chinese actionable errors;
9. CLI exit codes are zero on success and nonzero on validation failure.

Run the test and record the expected import/file failure before implementation.

## Task 2: Implement the deterministic scanner

**Files:**

- Create: `scripts/check_docs.py`

Implement without importing the NaumiAgent runtime or optional model dependencies:

- JSON manifest loading and Schema validation;
- `README.md` plus `docs/**/*.md` enumeration;
- POSIX-style repository-relative path normalization;
- full classification coverage with conflict detection;
- fenced-code removal before Markdown link parsing;
- local file target resolution with URL decoding and fragment/query removal;
- retired-command pattern checks scoped to `current` rules;
- stable Chinese diagnostics and a classification count summary;
- `main(argv)` plus script execution entry.

Run the focused tests until green, then run focused Ruff and `py_compile`.

## Task 3: Add the repository governance manifest

**Files:**

- Create: `docs/governance.json`
- Modify: `tests/unit/test_docs_governance.py`

Define non-overlapping rules for:

- root/current user documentation;
- current Terminal UI/configuration/harness documents;
- product and design specifications;
- historical numbered architecture chapters, plans, specs, audits and logs;
- migrations;
- quality/release gates;
- examples, sales/learning material and references.

Add a focused test that loads the real manifest and asserts every current repository Markdown document is classified. The assertion must use real files rather than a hard-coded count.

Run the checker against the repository. Expected first result: classification succeeds or reports exact uncovered paths; link/current-command failures are allowed at this RED stage and become the concrete repair list.

## Task 4: Replace the stale documentation entry point

**Files:**

- Replace: `docs/README.md`
- Create: `docs/superpowers/README.md`

The new documentation map must state implemented behavior:

- `naumi`, `naumi chat`, and `naumi ui` enter the new Terminal UI;
- launch/runtime failure falls back once to Textual;
- `naumi tui` is the supported explicit fallback;
- `naumi ui --legacy` is only a deprecated migration alias;
- Prompt Toolkit implementation/tests/dependency are retained but have no public launcher;
- project-local state belongs under `.naumi/` and secrets belong in the credential store or environment.

Link current guides, product specs, architecture history, migrations, quality records and references in separate sections. The superpowers index must explain that dated plans/specs preserve historical context and are not current usage instructions.

Run the focused test and real scanner; repair any new link errors before continuing.

## Task 5: Update current Terminal UI product documents

**Files:**

- Modify: `docs/product/terminal-ui/01-default-entry-and-runtime-shell.md`
- Modify: `docs/product/terminal-ui/07-cli-compatibility-and-migration.md`

Replace planned/default-entry language with the implemented command matrix. Remove the public `--classic` path. Describe Textual as supported fallback, not a frontend scheduled for removal. Explicitly state the old Prompt Toolkit code-retention boundary so future cleanup cannot infer deletion permission.

Keep forward-looking doctor/installer requirements labelled as pending rather than claiming completion.

Run the focused test, real scanner, and narrow retired-command `rg` check over current docs.

## Task 6: Update NaumiAgent Lite launch instructions

**Files:**

- Modify: `docs/product/naumiagent-lite/README.md`
- Modify: `docs/product/naumiagent-lite/课程大纲.md`
- Modify: `docs/product/naumiagent-lite/交付检查清单.md`

Replace internal module execution with the public installed commands. Use `naumi` for the default experience and `naumi tui` as the explicit no-Node/fallback demonstration. Do not change the educational or commercial claims unrelated to runtime entry.

Run narrow `rg` assertions across `docs/product/naumiagent-lite/` and the real scanner.

## Task 7: Repair real local links reported by the scanner

**Files:**

- Modify only documents with confirmed broken local targets.

For every reported link:

- resolve typos or outdated relative paths when the intended target is unambiguous;
- remove a link only when the target truly no longer exists and the text remains honest;
- do not create placeholder files to make the checker pass;
- do not rewrite external links or historical prose unnecessarily.

Repeat the real scanner until clean. Record the number of scanned/classified documents from its actual output.

## Task 8: Focused final verification and self-review

Run only:

```bash
uv run pytest tests/unit/test_docs_governance.py -q
uv run ruff check scripts/check_docs.py tests/unit/test_docs_governance.py
uv run python -m py_compile scripts/check_docs.py
uv run python scripts/check_docs.py
rg -n --glob '*.md' 'naumi chat --classic|python -m naumi_agent\.main chat' README.md docs/README.md docs/terminal-ui-integration.md docs/15-model-provider-configuration.md docs/product/terminal-ui docs/product/naumiagent-lite
git diff --check
```

The final `rg` result may only contain explicitly labelled historical quotations in non-current files; the named current/live product files should contain none.

Self-review before commit:

- old Prompt Toolkit source, tests and dependency are still present and unmodified;
- all Markdown documents are classified by real enumeration;
- historical records remain intact;
- current commands match actual `naumi --help` behavior;
- checker errors are actionable Chinese text;
- no unrelated untracked files are staged or modified;
- no full test suite was run.

Commit the documentation-governance slice independently, merge it to `main`, push, and preserve the feature worktree for audit.
