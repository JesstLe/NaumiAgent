# Harness H2 Repository Knowledge Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans and superpowers:test-driven-development to implement this plan inline. Do not dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give NaumiAgent a deterministic, secure, budgeted repository knowledge plane that discovers applicable repository instructions and related engineering evidence, injects only trusted L0/L1 knowledge into the existing ephemeral Harness snapshot, and exposes the same L2 reader to users and the Agent.

**Architecture:** `RepositoryKnowledgeIndex` owns bounded discovery, normalization, fingerprints, ranking inputs, and safe reads. `HarnessKnowledgeContextComposer` converts one trusted index snapshot plus the current task/model window into L0/L1 text under hard budgets. `HarnessService` remains the shared facade and cache owner. `AgentEngine` asks the service for knowledge while refreshing its existing temporary Harness snapshot; it never persists the bundle. The read-only `harness_read_knowledge` tool and `/harness knowledge` command call the same service method. H2 does not execute checks, create completion contracts, call an embedding model, or add H3-H7 placeholders.

**Tech Stack:** Python 3.12+, `pathlib`, `fnmatch`, `subprocess` argv without shell, SHA-256, Pydantic v2 profile contracts, pytest/pytest-asyncio, existing ToolRegistry and Rich/Textual/terminal UI command surfaces.

## Global Constraints

- Execute inline only; the user explicitly prohibited further subagents.
- Implement only H2. Do not execute Profile checks or create Completion/Evidence/Eval shells.
- Repository text is untrusted until the exact `.naumi/harness.yaml` digest is user-trusted. Missing, invalid, changed, or untrusted profiles inject no repository instruction body.
- Discovery and ranking are deterministic. Do not use embeddings, model calls, hidden prompts, or network access.
- Resolve every candidate against the canonical workspace. Reject absolute paths, `..`, symlink escapes, directories, devices, sockets, unreadable files, and files exceeding `knowledge.max_file_bytes`.
- Exclude secrets, VCS/runtime/cache directories, binary/NUL data, raw images, base64-like payloads, huge logs, and full large diffs. Return a path, digest, reason, and bounded excerpt instead.
- Root and nested `AGENTS.md` instructions are ordered from broad to specific. A nested file applies only to descendants of its directory; closer rules supplement or override ordinary guidance but cannot weaken project red lines.
- Use a conservative deterministic UTF-8 token estimate. L0 is at most 1,000 tokens, L1 is at most Profile `max_turn_tokens` (default 8,000), and all Harness knowledge is at most `min(12_000, floor(model_window * 0.15))`.
- Cache identity is workspace root + Profile digest + Git HEAD + changed-path digest + file fingerprints. A cache hit must not serve stale bytes after a file changes.
- Git discovery uses argv-only `git` with timeout and no shell. A missing/non-Git/busy Git executable degrades to an actionable warning instead of breaking the Agent.
- All visible copy is Chinese and actionable. Internal code comments remain English.
- Do not run the full repository test suite. Run only H2 knowledge/context/tool/surface tests, the existing Harness context tests, one Node command-registry test, and the H2 real-workspace scenario.
- Preserve the untracked `.superpowers/` directory.

---

### Task 1: Knowledge contracts and conservative budgets

**Files:**
- Create: `src/naumi_agent/harness/knowledge.py`
- Create: `tests/unit/test_harness_knowledge.py`

**Interfaces:**
- Produces immutable `KnowledgeKind`, `KnowledgeLevel`, `KnowledgeCandidate`, `KnowledgeIndexSnapshot`, `KnowledgeSelection`, `KnowledgeReadResult`, `KnowledgeWarning`, and `KnowledgeBudget` values.
- Produces `estimate_knowledge_tokens(text)`, `clip_text_to_token_budget(text, budget)`, canonical path/ID helpers, and stable SHA-256 fingerprints.
- Every selection/result reports estimated tokens, applied budget, truncation state, source paths, digests, and deterministic relevance reasons.

- [x] **Step 1: Write failing contract and budget tests**

Cover empty and Unicode text, ASCII/code/Chinese estimates, zero/negative boundaries, exact-fit and truncated excerpts, stable IDs/digests, immutable results, deterministic ordering, and the 1K/8K/12K/15% budget calculation.

```python
budget = KnowledgeBudget.for_model(profile_l1=8_000, model_window=32_000)
assert budget.l0_tokens == 1_000
assert budget.total_tokens == 4_800
assert budget.l1_tokens == 3_800
```

- [x] **Step 2: Verify RED**

Run: `uv run pytest -q tests/unit/test_harness_knowledge.py -k 'budget or contract or token'`

Expected: collection fails because `naumi_agent.harness.knowledge` does not exist.

- [x] **Step 3: Implement contracts and budget primitives**

Keep token estimation independent from vendor tokenizers so it works offline and across model providers. Clip on Unicode-safe line boundaries, include an explicit truncation marker, and prove the clipped result itself remains within the estimate.

- [x] **Step 4: Verify GREEN and Ruff**

Run: `uv run pytest -q tests/unit/test_harness_knowledge.py -k 'budget or contract or token'`

Run: `uv run ruff check src/naumi_agent/harness/knowledge.py tests/unit/test_harness_knowledge.py`

---

### Task 2: Safe repository discovery and nested instruction scope

**Files:**
- Modify: `src/naumi_agent/harness/knowledge.py`
- Modify: `tests/unit/test_harness_knowledge.py`

**Interfaces:**
- Adds `RepositoryKnowledgeIndex.build(profile, profile_digest)` and bounded `read_candidate()` behavior.
- Discovers root/nested `AGENTS.md`, trusted Profile entrypoints/include globs, `pyproject.toml`, `package.json`, `Package.swift`, related source/tests, Git HEAD, and changed paths.
- Builds metadata once and never reads an excluded or unsafe candidate body during ranking.

- [x] **Step 1: Write failing discovery tests**

Use real temporary directories and Git repositories. Cover root and three-level nested `AGENTS.md`, descendant scope, exact path precedence, Profile entrypoints, include/exclude conflicts, build manifests, changed/untracked files, missing Git, Git timeout, unreadable files, too-large files, binary/NUL data, image/base64/log/diff suppression, Unicode names, symlink escape, broken symlink, secret-like paths, duplicate aliases, and concurrent index builds.

```python
snapshot = index.build(profile, profile_digest="a" * 64)
assert [item.path for item in snapshot.instructions_for("src/pkg/api.py")] == [
    "AGENTS.md",
    "src/AGENTS.md",
    "src/pkg/AGENTS.md",
]
```

- [x] **Step 2: Verify RED**

Run: `uv run pytest -q tests/unit/test_harness_knowledge.py -k 'discover or agents or git or unsafe'`

- [x] **Step 3: Implement bounded discovery**

Use `Path.rglob` only for scoped instruction/build manifests and Profile globs for source/test candidates. Use `subprocess.run(["git", ...], cwd=workspace, timeout=...)` with `shell=False`, NUL-delimited porcelain output, stable POSIX-relative paths, and structured warnings. Never follow directory symlinks.

- [x] **Step 4: Verify GREEN and Ruff**

Run the command from Step 2, then the Task 1 Ruff command.

---

### Task 3: Deterministic L0/L1 selection and on-demand L2 reads

**Files:**
- Modify: `src/naumi_agent/harness/knowledge.py`
- Create: `src/naumi_agent/harness/context.py`
- Create: `tests/unit/test_harness_knowledge_context.py`
- Modify: `tests/unit/test_harness_knowledge.py`

**Interfaces:**
- Adds deterministic ranking by explicit path, applicable nested instructions, changed path, filename/stem, symbol/text token, import adjacency, source-test pairing, and Profile entrypoint priority.
- `HarnessKnowledgeContextComposer.compose(task, snapshot, model_window)` returns one L0 manifest and one L1 relevant bundle with separate and total token accounting.
- `RepositoryKnowledgeIndex.read(query=None, path=None, max_tokens=...)` implements bounded L2 lookup and reports ambiguity/missing/unsafe states without throwing raw filesystem errors.

- [x] **Step 1: Write failing ranking and context tests**

Cover different Engine, terminal UI, and Mac Workbench tasks selecting different files; exact path outranking fuzzy text; nested instruction inclusion; source-test/import relationships; changed-file boosts; stable tie-breaking; no-match fallback; ambiguous query; budget exhaustion; small model windows; repeated calls; malicious prompt text; and L2 path/query reads.

```python
engine = composer.compose("修改 AgentEngine 上下文注入", snapshot, 124_000)
terminal = composer.compose("优化 terminal-ui 状态栏", snapshot, 124_000)
assert engine.source_paths != terminal.source_paths
assert engine.total_tokens <= 12_000
assert terminal.total_tokens <= 12_000
```

- [x] **Step 2: Verify RED**

Run: `uv run pytest -q tests/unit/test_harness_knowledge.py tests/unit/test_harness_knowledge_context.py -k 'rank or select or context or read'`

- [x] **Step 3: Implement selection, excerpts, and rendering**

Normalize task tokens without erasing Chinese or path punctuation. Rank from metadata first, then read only top candidates for bounded match excerpts. L0 lists project identity, applicable instruction chain, entrypoints, checks-as-names-only, and available knowledge IDs. L1 labels repository content as data/instructions from trusted files and includes path, line range, digest, score reasons, and bounded text. L2 never returns more than the requested bounded budget.

- [x] **Step 4: Verify GREEN, determinism, and Ruff**

Run the command from Step 2 twice and require byte-identical rendered selections for unchanged inputs.

Run: `uv run ruff check src/naumi_agent/harness/knowledge.py src/naumi_agent/harness/context.py tests/unit/test_harness_knowledge.py tests/unit/test_harness_knowledge_context.py`

---

### Task 4: Trusted service cache and ephemeral Engine integration

**Files:**
- Modify: `src/naumi_agent/harness/service.py`
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Modify: `src/naumi_agent/orchestrator/context_assembly.py`
- Create: `tests/unit/test_harness_knowledge_integration.py`
- Modify: `tests/unit/test_harness_service.py`
- Modify: `tests/unit/test_context_assembly.py`

**Interfaces:**
- Adds `HarnessService.knowledge_context(task, model_window)`, `.read_knowledge(...)`, and cache diagnostics using the same current Profile/trust decision as H1.
- Engine derives the current task from the latest user message, resolves the capable model window through `ModelRouter`, and appends H2 text to the existing marked Harness snapshot.
- H2 content exists in `_messages` only. It is removed/rebuilt on every snapshot refresh, excluded from `_full_history`, and dropped before reactive compaction.

- [x] **Step 1: Write failing service and Engine tests**

Cover missing/invalid/untrusted/trusted/digest-changed Profiles; trust database failure; cache hit/miss/invalidation; changed knowledge bytes without Profile change; 50 concurrent readers; Engine non-streaming/streaming refresh; latest-user-task selection; no duplicate snapshots; compaction reinjection; Hook extra sections; shutdown; and disabled compatibility.

```python
await engine._inject_harness_context_snapshot()
active = [m for m in engine._messages if is_harness_context_message(m)]
assert len(active) == 1
assert "Repository Knowledge" in active[0]["content"]
assert not any("Repository Knowledge" in str(m) for m in engine._full_history)
```

- [x] **Step 2: Verify RED**

Run: `uv run pytest -q tests/unit/test_harness_service.py tests/unit/test_harness_knowledge_integration.py tests/unit/test_context_assembly.py -k 'knowledge or harness_context'`

- [x] **Step 3: Implement trusted cache and injection**

Keep one async lock per service cache, avoid holding it while doing slow file reads, and double-check fingerprints before publishing a snapshot. Missing/untrusted states return an empty knowledge section plus structured diagnostics to manual callers; they do not inject repository bodies. Append H2 via the existing `_append_harness_context_sections` path so Hook ordering and compaction semantics remain intact.

- [x] **Step 4: Verify GREEN, concurrency, and performance**

Run the command from Step 2.

Add a focused benchmark assertion over a warmed unchanged temporary repository: 100 sequential context builds must have P95 below 50 ms on the local test machine. Report timing separately; do not make a flaky wall-clock assertion under CI load.

Run: `uv run ruff check src/naumi_agent/harness src/naumi_agent/orchestrator/context_assembly.py src/naumi_agent/orchestrator/engine.py tests/unit/test_harness_service.py tests/unit/test_harness_knowledge_integration.py tests/unit/test_context_assembly.py`

---

### Task 5: Shared L2 Agent tool and user/UI surfaces

**Files:**
- Modify: `src/naumi_agent/harness/tools.py`
- Modify: `src/naumi_agent/main.py`
- Modify: `src/naumi_agent/cli/completer.py`
- Modify: `src/naumi_agent/cli_completer.py`
- Modify: `src/naumi_agent/ui/bridge.py`
- Modify: `frontend/terminal-ui/src/state.js`
- Create: `tests/unit/test_harness_knowledge_tool.py`
- Modify: `tests/unit/test_harness_surfaces.py`
- Modify: `tests/unit/test_cli_completer.py`
- Modify: `tests/unit/test_ui_bridge.py`
- Modify: `frontend/terminal-ui/test/state.test.js`

**Interfaces:**
- Registers `harness_read_knowledge` with `read_only=True`, `concurrency_safe=True`, optional `query`, optional `path`, and bounded `max_tokens`.
- `/harness knowledge <query-or-relative-path>` calls the same `HarnessService.read_knowledge()` and renders path/digest/relevance/truncation clearly.
- Classic completion, Textual bridge metadata, and new terminal UI advertise the new Harness subcommand consistently; no separate UI-side knowledge implementation exists.

- [x] **Step 1: Write failing tool and surface tests**

Assert tool schema, metadata, bounded defaults, query/path validation, trusted/untrusted behavior, concurrent calls, Chinese errors, slash quoting/Unicode, missing argument/unknown option usage, and identical service output semantics. Assert all command registries show `[status|doctor|knowledge|trust|untrust]`.

- [x] **Step 2: Verify RED**

Run: `uv run pytest -q tests/unit/test_harness_knowledge_tool.py tests/unit/test_harness_surfaces.py tests/unit/test_cli_completer.py tests/unit/test_ui_bridge.py -k 'harness or knowledge'`

Run: `node --test --test-name-pattern 'harness' frontend/terminal-ui/test/state.test.js`

- [x] **Step 3: Implement the shared tool and slash command**

Reject calls that provide neither query nor path, calls that try to escape the workspace, and `max_tokens` outside the bounded range. Keep trust/untrust user-only. Render file bodies as fenced excerpts with source identity, never as an unlabeled system instruction.

- [x] **Step 4: Verify GREEN and Ruff**

Run both Step 2 commands.

Run: `uv run ruff check src/naumi_agent/harness/tools.py src/naumi_agent/main.py src/naumi_agent/cli/completer.py src/naumi_agent/cli_completer.py src/naumi_agent/ui/bridge.py tests/unit/test_harness_knowledge_tool.py tests/unit/test_harness_surfaces.py tests/unit/test_cli_completer.py tests/unit/test_ui_bridge.py`

---

### Task 6: Canonical Harness docs and real NaumiAgent H2 proof

**Files:**
- Create: `docs/harness/index.md`
- Create: `docs/harness/architecture.md`
- Create: `docs/harness/golden-principles.md`
- Create: `docs/harness/debt.md`
- Modify: `.naumi/harness.yaml`
- Modify: `docs/superpowers/specs/2026-07-14-harness-engineering-design.md`
- Modify: `docs/superpowers/plans/2026-07-14-harness-knowledge-plane.md`
- Create: `tests/integration/test_harness_h2_real_workspace.py`

**Interfaces:**
- `docs/harness/index.md` is the concise canonical knowledge map. It records source paths and a verified commit without claiming future phases are implemented.
- The real profile adds the canonical Harness docs and H2-targeted test/lint paths, while retaining bounded include/exclude policy.
- The integration scenario uses the actual NaumiAgent checkout and a temporary trust database; it never mutates the live trust database.

- [x] **Step 1: Write the failing real-workspace scenario**

Query three real tasks:

1. `修改 AgentEngine 的 Harness 上下文注入` must select Engine/context/Harness sources.
2. `优化 frontend/terminal-ui 的状态栏和语义颜色` must select terminal UI sources/tests.
3. `调整 Mac Workbench issue 运行控制` must select Workbench Swift/controller sources or their canonical docs.

Assert the selected path sets differ, each includes applicable root instructions, all L0/L1/total budgets hold, the warmed no-change path is measured, and an exact copied Profile edit revokes trust. Use real Git/filesystem/YAML/SQLite and no model or network.

- [x] **Step 2: Verify RED**

Run: `uv run pytest -q tests/integration/test_harness_h2_real_workspace.py`

- [x] **Step 3: Write truthful canonical docs and update Profile**

Document only current H1/H2 behavior, ownership, trust boundary, temporary context, budgets, extension rules, and known debt. Add H2 knowledge files and targeted checks to the Profile; do not add a full-suite command.

- [x] **Step 4: Update status and focused acceptance evidence**

Mark H2 complete in the design only after all focused tests pass. Record exact test counts, P95 timing, three selected path summaries, and remaining H3-H7 debt.

Run: `uv run pytest -q tests/unit/test_harness_knowledge.py tests/unit/test_harness_knowledge_context.py tests/unit/test_harness_knowledge_tool.py tests/unit/test_harness_knowledge_integration.py tests/unit/test_harness_profile.py tests/unit/test_harness_service.py tests/unit/test_harness_tools.py tests/unit/test_harness_surfaces.py tests/unit/test_context_assembly.py tests/integration/test_harness_h2_real_workspace.py`

Run: `uv run ruff check src/naumi_agent/harness src/naumi_agent/orchestrator/context_assembly.py src/naumi_agent/orchestrator/engine.py tests/unit/test_harness_knowledge*.py tests/unit/test_harness_service.py tests/unit/test_harness_tools.py tests/unit/test_harness_surfaces.py tests/unit/test_context_assembly.py tests/integration/test_harness_h2_real_workspace.py`

Run: `node --test --test-name-pattern 'harness' frontend/terminal-ui/test/state.test.js`

Run: `git diff --check`

Expected: all focused checks pass; no full repository suite runs.

Evidence: 103 focused H1/H2 Python tests passed; Ruff passed; the one filtered
Terminal UI Node test passed; `git diff --check` passed. The real warm path was
sampled 100 times at P50 19.183 ms, P95 20.155 ms, max 23.244 ms.

- [x] **Step 5: Manual real command smoke test**

Run `/harness doctor`, `/harness knowledge AgentEngine`, and the `harness_read_knowledge` service path against a temporary copied trust DB. Confirm Chinese actionable output, bounded excerpts, stable digests, no Profile command execution, no persistent Harness messages, and no changed live trust record.

Evidence: real `/harness doctor`, exact-path `/harness knowledge`, and the Agent
Tool returned the same stable source digest; a 240-token request rendered 233
tokens. Temporary state was removed, live trust was untouched, and
`_full_history` was unchanged.

- [x] **Step 6: Multi-round self-review**

Review at least these questions and record fixes before commit:

- Does an untrusted or changed Profile leak any repository instruction body?
- Can a symlink, glob, query, Git path, or L2 read escape the workspace?
- Are binary/base64/log/diff payloads represented only by safe metadata?
- Does nested `AGENTS.md` scope match the target paths rather than every task?
- Can token clipping or Markdown fences exceed the final hard budget?
- Are selection order and rendered output deterministic across runs/platforms?
- Does the cache invalidate on Profile, HEAD, changed paths, or knowledge bytes?
- Are CLI, TUI, new terminal UI, and Agent tool semantics backed by one service?
- Did H2 accidentally execute checks, call a model, persist context, or add H3-H7 shells?

Fixes from review: rechecked trust after same-metadata digest rebuilds, removed
slow filesystem work from cache locks, switched discovery/L2 to bounded stream
reads, made Markdown evidence fences longer than source backtick runs, aligned
cache documentation with periodic Git audits, and synchronized CLI/TUI/new UI
command metadata.

- [ ] **Step 7: Commit, merge, and push the feature**

Commit the isolated branch with:

```bash
git commit -m "feat: add repository knowledge plane"
```

Fast-forward merge into `main`, re-run only the focused acceptance commands on `main`, and push `origin/main`.

## Plan Self-Review

- Spec coverage: nested instructions, deterministic discovery/ranking, L0/L1/L2, safe reads, token budgets, trust gating, temporary context, cache identity, shared tool/command surface, and three real repository scenarios are assigned.
- Scope: Profile check execution, Completion Contract, Evidence Store, Eval, feedback promotion, and long-running control plane remain H3-H7 and are explicitly excluded.
- Security: exact Profile trust gates instruction injection; every filesystem path is canonicalized; Git is argv-only and bounded; secret/binary/base64/large payloads never become raw context.
- Fidelity: Engine, Git, filesystem, YAML, SQLite, command registries, and real repository selection are exercised. No model or network is needed for H2 correctness.
- UX: all manual failures identify the unsafe/missing input and the next action; users and Agent share one L2 service instead of divergent behavior.
- Performance: unchanged cached assembly has an explicit local P95 measurement and cache invalidation has correctness tests.
- User constraint: all verification commands are focused; no full-suite test command is present.
