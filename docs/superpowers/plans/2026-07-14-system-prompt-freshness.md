# System Prompt Freshness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make NaumiAgent verify time-sensitive claims against current evidence and automatically refresh legacy generated prompts without overwriting user prompts.

**Architecture:** Keep stable freshness policy in the composable base system prompt and keep current date/time in the per-turn Harness snapshot. Upgrade the generated marker to v2 while recognizing all valid Naumi section marker versions so existing sessions migrate through the engine's current refresh path.

**Tech Stack:** Python 3.14, dataclasses, regular expressions, pytest, ruff.

## Global Constraints

- Current date, time, and timezone remain exclusively in `HarnessContextAssembler` runtime snapshots.
- Generated v1 prompts migrate automatically; arbitrary custom system prompts remain untouched.
- No provider, model, tool version, current date, or build timestamp is hard-coded into the base prompt.
- Verification is limited to relevant unit modules; do not run the full test suite.

---

### Task 1: Versioned freshness policy and legacy migration

**Files:**
- Modify: `src/naumi_agent/orchestrator/system_prompt.py`
- Modify: `docs/02-core-engine.md`
- Modify: `tests/unit/test_system_prompt.py`
- Modify: `tests/unit/test_engine.py`

**Interfaces:**
- Consumes: `build_system_prompt(context: PromptAssemblyInput | None = None, *, sections: Iterable[PromptSection] = DEFAULT_PROMPT_SECTIONS) -> str`
- Produces: `SYSTEM_PROMPT_MARKER = '<naumi_system_prompt version="sections-v2">'`
- Produces: `is_generated_system_prompt(content: str) -> bool` that recognizes complete Naumi `sections-vN` markers.
- Preserves: `AgentEngine._ensure_system_prompt()` custom-prompt behavior.

- [x] **Step 1: Write failing prompt tests**

Add assertions proving the default prompt contains `## Knowledge Freshness`, current-evidence rules, and the v2 marker; add parameterized recognition tests for valid v1/v2 markers and invalid lookalikes.

```python
def test_generated_marker_recognizes_legacy_versions_without_false_positives():
    assert is_generated_system_prompt('<naumi_system_prompt version="sections-v1">')
    assert is_generated_system_prompt('<naumi_system_prompt version="sections-v2">')
    assert not is_generated_system_prompt('<naumi_system_prompt version="custom">')
    assert not is_generated_system_prompt('prefix <naumi_system_prompt')
```

- [x] **Step 2: Write failing engine migration test**

Seed `_messages` and `_full_history` with a v1 generated prompt, call `_ensure_system_prompt()`, and assert both copies use v2 and contain the freshness section.

```python
def test_ensure_system_prompt_migrates_legacy_generated_prompt(engine):
    legacy = '<naumi_system_prompt version="sections-v1">\nlegacy'
    engine._messages = [{"role": "system", "content": legacy}]
    engine._full_history = [{"role": "system", "content": legacy}]
    engine._ensure_system_prompt()
    assert "sections-v2" in engine._messages[0]["content"]
    assert "## Knowledge Freshness" in engine._full_history[0]["content"]
```

- [x] **Step 3: Run tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/unit/test_system_prompt.py tests/unit/test_engine.py::TestSetSystemPrompt -q`

Expected: failures because the marker is still v1, no freshness section exists, and v1-compatible recognition has not been implemented.

- [x] **Step 4: Implement the stable freshness section and marker recognition**

In `system_prompt.py`, add a compiled full marker pattern, upgrade the current marker, insert `KNOWLEDGE_FRESHNESS_SECTION` into `DEFAULT_PROMPT_SECTIONS`, and use the pattern in `is_generated_system_prompt()`.

```python
SYSTEM_PROMPT_MARKER = '<naumi_system_prompt version="sections-v2">'
GENERATED_SYSTEM_PROMPT_MARKER_RE = re.compile(
    r'\A<naumi_system_prompt version="sections-v[1-9][0-9]*">'
)

def is_generated_system_prompt(content: str) -> bool:
    return GENERATED_SYSTEM_PROMPT_MARKER_RE.search(content) is not None
```

The freshness section must require current evidence for volatile facts, prefer local source/config/runtime metadata or authoritative sources, disclose the verification basis, and mark unverified claims as potentially stale.

Replace the obsolete `BASE_SYSTEM_PROMPT` example in `docs/02-core-engine.md` with the v2 composable prompt and per-turn Harness boundary.

- [x] **Step 5: Run focused tests and confirm GREEN**

Run: `.venv/bin/python -m pytest tests/unit/test_system_prompt.py tests/unit/test_engine.py::TestSetSystemPrompt tests/unit/test_context_assembly.py -q`

Expected: all selected tests pass, including the existing dynamic-time tests.

- [x] **Step 6: Run lint, compile, and diff checks**

Run: `.venv/bin/python -m ruff check src/naumi_agent/orchestrator/system_prompt.py tests/unit/test_system_prompt.py tests/unit/test_engine.py`

Run: `.venv/bin/python -m compileall -q src/naumi_agent/orchestrator/system_prompt.py`

Run: `git diff --check`

Expected: every command exits with code 0.

- [x] **Step 7: Commit and push**

```bash
git add docs/superpowers/plans/2026-07-14-system-prompt-freshness.md \
  src/naumi_agent/orchestrator/system_prompt.py \
  tests/unit/test_system_prompt.py tests/unit/test_engine.py
git commit -m "feat: keep system prompt knowledge current"
git push origin main
```
