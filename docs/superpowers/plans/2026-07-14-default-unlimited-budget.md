# Default-Unlimited Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make main and child Agent cost/input/output budgets unlimited by default, preserve explicit limits, raise the shared default turn ceiling to 50, and render the same truthful state in the Bridge, new Terminal UI, Textual TUI, Inspector, runtime context, and temporary legacy CLI.

**Architecture:** Nullable limits (`None` in Python, `null` in JSON) are the only unlimited representation. `BudgetTracker` remains the authoritative usage/enforcement component; `AgentEngine.get_budget_info()` becomes the shared JSON-safe status contract, while focused Python and JavaScript presentation helpers render it without inventing finite values. Permission bypass and explicit budget enforcement are independent.

**Tech Stack:** Python 3.13+, Pydantic Settings, pytest, Ruff, JSONL Bridge, Node.js ESM, Node test runner, ANSI Terminal UI, Textual TUI.

## Global Constraints

- Main Agent, dynamic Agents, and preset Agents default to unlimited cumulative cost, input tokens, and output tokens.
- `None`/`null` means unlimited; zero is an enabled zero allowance; negative limits are invalid.
- Main and child-Agent default `max_turns` is exactly 50; explicit smaller overrides remain valid.
- Bypass grants tool permission but never disables an explicitly enabled budget.
- Usage accounting remains active even when every budget limit is unlimited.
- Model context windows, per-call output caps, timeouts, Pursuit iteration policy, and permission rules remain unchanged.
- All user-visible copy is Chinese; code comments are English; commit messages are English.
- Python commands set `NAUMI_MODELS__API_KEY=unit-test-placeholder` and use `uv run python -m pytest`.
- Run only task-specific tests and syntax/Ruff checks; never run the full Python or Node suite in this slice.
- Each task ends with self-review and an independent commit.

---

### Task 1: Nullable Budget and Configuration Domain

**Files:**
- Modify: `src/naumi_agent/config/settings.py`
- Modify: `src/naumi_agent/safety/budget.py`
- Modify: `tests/unit/test_safety.py`
- Modify: `tests/integration/test_smoke.py`

**Interfaces:**
- Produces: `SafetyConfig.max_budget_usd: float | None`, `max_input_tokens: int | None`, `max_output_tokens: int | None`, and `max_turns: int = 50`.
- Produces: `TokenBudget.enabled: bool`, nullable maxima, `BudgetTracker.is_exceeded()`, and `BudgetSummary.remaining_usd: float | None`.
- Consumed by: Tasks 2 and 3.

- [ ] **Step 1: Write failing configuration and tracker tests**

Add focused assertions:

```python
def test_default_config_uses_unlimited_budgets_and_fifty_turns() -> None:
    config = AppConfig()
    assert config.safety.max_budget_usd is None
    assert config.safety.max_input_tokens is None
    assert config.safety.max_output_tokens is None
    assert config.safety.max_turns == 50


@pytest.mark.parametrize(
    "field",
    ("max_budget_usd", "max_input_tokens", "max_output_tokens"),
)
def test_negative_budget_limits_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        SafetyConfig(**{field: -1})


def test_unlimited_tracker_never_exceeds_and_keeps_usage() -> None:
    tracker = BudgetTracker(TokenBudget())
    tracker.track(
        TokenUsage(input_tokens=900_000, output_tokens=90_000, total_tokens=990_000, cost_usd=20.0),
        "test-model",
    )
    assert tracker.budget.enabled is False
    assert tracker.is_exceeded() is False
    assert tracker.get_summary().remaining_usd is None
    assert tracker.total_cost_usd == 20.0


@pytest.mark.parametrize(
    ("budget", "usage"),
    [
        (TokenBudget(max_input_tokens=0), TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0, cost_usd=0)),
        (TokenBudget(max_output_tokens=0), TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0, cost_usd=0)),
        (TokenBudget(max_usd=0), TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0, cost_usd=0)),
    ],
)
def test_zero_limit_is_already_exhausted(budget: TokenBudget, usage: TokenUsage) -> None:
    tracker = BudgetTracker(budget)
    assert tracker.is_exceeded() is True
```

- [ ] **Step 2: Run RED tests**

Run:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder \
  uv run python -m pytest -q \
  tests/unit/test_safety.py tests/integration/test_smoke.py \
  -k 'unlimited or fifty_turns or negative_budget or zero_limit'
```

Expected: failures show current `$5`, `500000`, implicit `50000`, `30`, and missing nullable behavior.

- [ ] **Step 3: Implement nullable validated configuration**

Use Pydantic constraints without magic sentinels:

```python
class SafetyConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NAUMI_SAFETY__")

    permission_mode: str = "moderate"
    allowed_dirs: list[str] = Field(default_factory=lambda: ["/workspace", str(Path.cwd())])
    max_budget_usd: float | None = Field(default=None, ge=0)
    max_turns: int = Field(default=50, ge=1)
    max_parallel_tools: int = Field(default=4, ge=1, le=16)
    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
```

- [ ] **Step 4: Implement the budget domain contract**

Use this boundary in `budget.py`:

```python
@dataclass(frozen=True)
class TokenBudget:
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_usd: float | None = None

    @property
    def enabled(self) -> bool:
        return any(
            limit is not None
            for limit in (self.max_input_tokens, self.max_output_tokens, self.max_usd)
        )


def _is_limit_exceeded(total: int | float, limit: int | float | None) -> bool:
    if limit is None:
        return False
    if limit == 0:
        return True
    return total > limit
```

`BudgetTracker.is_exceeded()` calls `_is_limit_exceeded()` for all three resources. `remaining_usd` and `BudgetSummary.remaining_usd` return `None` when `max_usd` is `None`; otherwise they return a non-negative value.

- [ ] **Step 5: Run GREEN tests and Ruff**

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder \
  uv run python -m pytest -q tests/unit/test_safety.py tests/integration/test_smoke.py \
  -k 'BudgetTracker or default_config or negative_budget'
NAUMI_MODELS__API_KEY=unit-test-placeholder \
  uv run ruff check src/naumi_agent/config/settings.py src/naumi_agent/safety/budget.py \
  tests/unit/test_safety.py tests/integration/test_smoke.py
```

Expected: selected tests and Ruff pass.

- [ ] **Step 6: Self-review and commit**

Check zero, exact-limit, reset, and nullable summary semantics. Then:

```bash
git add src/naumi_agent/config/settings.py src/naumi_agent/safety/budget.py \
  tests/unit/test_safety.py tests/integration/test_smoke.py
git commit -m "feat: make runtime budgets optional by default"
```

---

### Task 2: Main Engine Enforcement and Runtime Budget Status

**Files:**
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Create: `src/naumi_agent/ui/budget.py`
- Modify: `src/naumi_agent/orchestrator/context_assembly.py`
- Modify: `src/naumi_agent/tools/runtime.py`
- Modify: `tests/unit/test_engine.py`
- Create: `tests/unit/test_budget_display.py`
- Modify: `tests/unit/test_context_assembly.py`
- Modify: `tests/unit/test_runtime_status.py`
- Modify: `tests/unit/test_ui_bridge.py`

**Interfaces:**
- Consumes: nullable `SafetyConfig` and `TokenBudget` from Task 1.
- Produces: `AgentEngine.get_budget_info() -> dict[str, bool | int | float | None]`.
- Produces: `format_budget_detail(info: Mapping[str, Any]) -> str` for Python presentation surfaces.
- Consumed by: Tasks 4 and 5.

- [ ] **Step 1: Write failing engine/status/display tests**

Cover the default, explicit, bypass, zero, and JSON-safe contracts:

```python
def test_default_engine_budget_is_unlimited(engine: AgentEngine) -> None:
    info = engine.get_budget_info()
    assert info == {
        "enabled": False,
        "used_usd": 0.0,
        "max_usd": None,
        "remaining_usd": None,
        "cost_percentage": None,
        "input_tokens": 0,
        "max_input_tokens": None,
        "input_percentage": None,
        "output_tokens": 0,
        "max_output_tokens": None,
        "output_percentage": None,
        "percentage": None,
    }
    assert engine._check_budget() is None
    json.dumps(info, allow_nan=False)


def test_explicit_budget_applies_in_bypass_mode() -> None:
    config = AppConfig(safety=SafetyConfig(permission_mode="bypass", max_budget_usd=0))
    engine = AgentEngine(config)
    result = engine._check_budget()
    assert result is not None
    assert result.status == "budget_exceeded"


def test_budget_display_distinguishes_unlimited_and_finite() -> None:
    assert format_budget_detail({"enabled": False, "used_usd": 0.0123}) == "不限 · 已用 $0.0123"
    assert "$0.0123/$2.00" in format_budget_detail({
        "enabled": True,
        "used_usd": 0.0123,
        "max_usd": 2.0,
        "cost_percentage": 0.6,
    })
```

Add a Bridge test asserting `ready.payload.budget.max_usd is None`, `enabled is False`, and `json.dumps(..., allow_nan=False)` succeeds with a real `AgentEngine` fixture.

- [ ] **Step 2: Run RED tests**

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder \
  uv run python -m pytest -q tests/unit/test_engine.py tests/unit/test_budget_display.py \
  tests/unit/test_context_assembly.py tests/unit/test_runtime_status.py tests/unit/test_ui_bridge.py \
  -k 'budget_is_unlimited or explicit_budget_applies or budget_display or nullable_budget'
```

Expected: missing status fields/helper and bypass exemption failures.

- [ ] **Step 3: Wire all three limits into AgentEngine**

Construct `TokenBudget` with `max_input_tokens`, `max_output_tokens`, and `max_usd`. Remove the permission-mode early return from `_check_budget()`. Build reasons only for non-`None` limits, formatting numbers without passing `None` to numeric formatters.

Compute status percentages with a focused helper:

```python
def _budget_percentage(used: int | float, limit: int | float | None) -> float | None:
    if limit is None:
        return None
    if limit == 0:
        return 100.0
    return round(float(used) / float(limit) * 100, 1)
```

`percentage` is `max(active_percentages)` or `None`; no value is `Infinity` or `NaN`.

- [ ] **Step 4: Add the shared Python display helper**

Create `src/naumi_agent/ui/budget.py` with:

```python
def format_budget_detail(info: Mapping[str, Any]) -> str:
    used = _nonnegative_float(info.get("used_usd"))
    max_usd = _optional_nonnegative_float(info.get("max_usd"))
    parts: list[str] = []
    if max_usd is None:
        parts.append(f"不限 · 已用 ${used:.4f}" if not info.get("enabled") else f"不限费用 · 已用 ${used:.4f}")
    else:
        percent = _optional_nonnegative_float(info.get("cost_percentage"))
        suffix = f" ({percent:.1f}%)" if percent is not None else ""
        parts.append(f"${used:.4f}/${max_usd:.2f}{suffix}")
    if info.get("max_input_tokens") is not None:
        parts.append(f"输入 {_format_tokens(info.get('input_tokens'))}/{_format_tokens(info.get('max_input_tokens'))}")
    if info.get("max_output_tokens") is not None:
        parts.append(f"输出 {_format_tokens(info.get('output_tokens'))}/{_format_tokens(info.get('max_output_tokens'))}")
    return " · ".join(parts)
```

Private numeric helpers reject negative/non-finite display values by falling back to zero/`None`; they never emit `nan` or `inf`.

- [ ] **Step 5: Route context and runtime status through the helper**

Replace hard-coded `$used/$max` formatting in `HarnessContextAssembler._budget_section()` and `_RuntimeSnapshot.context_section()` with `format_budget_detail()`. In recommendations, compare `percentage` only when it is numeric:

```python
budget_percentage = budget.get("percentage")
if isinstance(budget_percentage, int | float) and budget_percentage >= 80:
    recommendations.append("预算接近上限：优先使用 fast/本地扫描，避免长推理。")
```

- [ ] **Step 6: Run focused GREEN tests and Ruff**

Repeat the RED command with `-k 'budget or runtime_status or nullable_budget'`; do not run the rest of the large `test_engine.py` module. Then run Ruff only on changed Python files. Expected: selected tests pass and unrelated cases are deselected.

- [ ] **Step 7: Self-review and commit**

Verify default unlimited, explicit zero before model call, bypass enforcement, max-of-active percentages, and JSON safety. Commit:

```bash
git add src/naumi_agent/orchestrator/engine.py src/naumi_agent/ui/budget.py \
  src/naumi_agent/orchestrator/context_assembly.py src/naumi_agent/tools/runtime.py \
  tests/unit/test_engine.py tests/unit/test_budget_display.py tests/unit/test_context_assembly.py \
  tests/unit/test_runtime_status.py tests/unit/test_ui_bridge.py
git commit -m "feat: expose truthful runtime budget status"
```

---

### Task 3: Child-Agent Unlimited Defaults and 50-Turn Ceiling

**Files:**
- Modify: `src/naumi_agent/agents/base.py`
- Modify: `src/naumi_agent/agents/factory.py`
- Modify: `src/naumi_agent/agents/presets.py`
- Modify: `tests/unit/test_agents.py`
- Modify: `tests/unit/test_agent_factory.py`

**Interfaces:**
- Produces: `AgentConfig.max_budget_usd: float | None = None`, `max_turns: int = 50`.
- Preserves: explicit `max_budget_usd` and `max_turns` overrides through `DynamicAgentFactory` and `SubAgentManager`.

- [ ] **Step 1: Write failing child-Agent tests**

```python
def test_dynamic_agent_defaults_to_unlimited_budget_and_fifty_turns(factory: DynamicAgentFactory) -> None:
    config = factory.create_config(name="worker", task_description="实现完整后端")
    assert config.max_budget_usd is None
    assert config.max_turns == 50


@pytest.mark.parametrize("config", ALL_AGENT_CONFIGS.values())
def test_presets_use_shared_unlimited_budget_and_turn_default(config: AgentConfig) -> None:
    assert config.max_budget_usd is None
    assert config.max_turns == 50
```

Replace the auto-budget estimator tests with explicit override tests. Add execution tests proving unlimited child Agents continue past high recorded cost and `max_budget_usd=0` stops before the first router call.

- [ ] **Step 2: Run RED tests**

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder \
  uv run python -m pytest -q tests/unit/test_agents.py tests/unit/test_agent_factory.py \
  -k 'unlimited_budget or fifty_turns or zero_budget or explicit_overrides'
```

- [ ] **Step 3: Implement child-Agent defaults**

Change `AgentConfig` defaults, guard the execution check with `limit is not None`, and treat zero as exhausted before the router call. In `DynamicAgentFactory`, omitted budget remains `None`; omitted turns become 50. Remove `_detect_budget()` and `_detect_max_turns()` plus their obsolete tests. Remove finite budget/turn fields from presets so the shared defaults are authoritative.

- [ ] **Step 4: Run focused tests and Ruff**

Run only `tests/unit/test_agents.py` and `tests/unit/test_agent_factory.py`, then Ruff changed files. Expected: pass.

- [ ] **Step 5: Self-review and commit**

Check explicit override flow and no remaining hidden finite preset/estimator. Commit:

```bash
git add src/naumi_agent/agents/base.py src/naumi_agent/agents/factory.py \
  src/naumi_agent/agents/presets.py tests/unit/test_agents.py tests/unit/test_agent_factory.py
git commit -m "feat: remove hidden child agent budgets"
```

---

### Task 4: Python Inspector, Textual TUI, and Temporary CLI Presentation

**Files:**
- Modify: `src/naumi_agent/inspector/models.py`
- Modify: `src/naumi_agent/inspector/service.py`
- Modify: `src/naumi_agent/tui/runtime_inspector.py`
- Modify: `src/naumi_agent/tui/app.py`
- Modify: `src/naumi_agent/main.py`
- Modify: `tests/unit/test_runtime_inspector.py`
- Modify: `tests/unit/test_tui_runtime_inspector.py`
- Modify: `tests/unit/test_tui.py`
- Modify: `tests/unit/test_cli_rendering.py`
- Modify: `tests/unit/test_cli_sessions.py`

**Interfaces:**
- Consumes: `format_budget_detail()` and `AgentEngine.get_budget_info()` from Task 2.
- Produces: nullable Inspector budget fields and consistent Chinese display across current Python UI surfaces.

- [ ] **Step 1: Write failing Python presentation tests**

Add Inspector round-trip tests for:

```python
context = InspectorContext.from_dict({
    "state": "ready",
    "budget_enabled": False,
    "budget_used_usd": 0.0123,
    "budget_max_usd": None,
    "budget_percentage": None,
    "budget_max_input_tokens": None,
    "budget_max_output_tokens": None,
})
assert context.budget_enabled is False
assert context.budget_max_usd is None
assert RuntimeInspectorSnapshot.from_dict(snapshot.to_dict()) == snapshot
```

Add TUI/CLI tests asserting default output contains `预算: 不限` and never `$0.0000/$0.00`, while finite fixtures still render `$used/$limit`.

- [ ] **Step 2: Run RED tests**

Run only the five listed Python presentation test files with `-k 'budget or startup_status or session_stats'`. Expected: nullable model/format failures.

- [ ] **Step 3: Extend Inspector context fields**

Add:

```python
budget_enabled: bool = False
budget_used_usd: float = 0.0
budget_max_usd: float | None = None
budget_percentage: float | None = None
budget_max_input_tokens: int | None = None
budget_max_output_tokens: int | None = None
```

Use strict optional-number/integer parsers that accept `None`, reject booleans and negatives, and preserve `None` through `to_dict()`/`from_dict()`. `RuntimeInspectorService` maps the engine budget contract without `_float(None) -> 0` coercion.

- [ ] **Step 4: Replace Python UI formatting**

Every current budget string in `main.py`, `tui/app.py`, and `tui/runtime_inspector.py` uses `format_budget_detail()` or equivalent Inspector fields. No formatter applies `:.2f` to `None`. Narrow Textual status continues using its existing layout/wrapping behavior.

- [ ] **Step 5: Run focused GREEN tests and Ruff**

Run only the five presentation files; Ruff the changed Python files. Expected: pass.

- [ ] **Step 6: Self-review and commit**

Search active Python source for hard-coded `budget['max_usd']:.2f` and ensure no matches remain. Commit:

```bash
git add src/naumi_agent/inspector/models.py src/naumi_agent/inspector/service.py \
  src/naumi_agent/tui/runtime_inspector.py src/naumi_agent/tui/app.py src/naumi_agent/main.py \
  tests/unit/test_runtime_inspector.py tests/unit/test_tui_runtime_inspector.py \
  tests/unit/test_tui.py tests/unit/test_cli_rendering.py tests/unit/test_cli_sessions.py
git commit -m "feat: render unlimited budgets across python ui"
```

---

### Task 5: New Terminal UI Nullable Budget Protocol and Rendering

**Files:**
- Modify: `frontend/terminal-ui/src/protocol.js`
- Create: `frontend/terminal-ui/src/components/budget-status.js`
- Modify: `frontend/terminal-ui/src/components/footer.js`
- Modify: `frontend/terminal-ui/src/components/runtime-inspector.js`
- Modify: `frontend/terminal-ui/test/protocol.test.js`
- Create: `frontend/terminal-ui/test/budget-status.test.js`
- Modify: `frontend/terminal-ui/test/render.test.js`
- Modify: `frontend/terminal-ui/test/fixtures/fake-bridge.js`
- Modify: `frontend/terminal-ui/test/fixtures/history-bridge.js`
- Modify: `frontend/terminal-ui/test/fixtures/message-lifecycle-bridge.js`
- Modify: `frontend/terminal-ui/test/fixtures/python-bridge-fixture.py`
- Modify: `frontend/terminal-ui/test/index-process.test.js`

**Interfaces:**
- Consumes: Bridge budget object and nullable Inspector fields from Tasks 2 and 4.
- Produces: `normalizeBudgetStatus(value, source): object` and `formatBudgetStatus(info): string`.

- [ ] **Step 1: Write failing JavaScript protocol/component/render tests**

Test exact semantics:

```javascript
test("normalizes nullable unlimited budget without inventing zero", () => {
  assert.deepEqual(normalizeBudgetStatus({
    enabled: false,
    used_usd: 0.0123,
    max_usd: null,
    remaining_usd: null,
    percentage: null,
    input_tokens: 42,
    max_input_tokens: null,
    output_tokens: 8,
    max_output_tokens: null,
  }), expectedUnlimitedBudget);
});

test("formats unlimited and finite budget states", () => {
  assert.equal(formatBudgetStatus({ enabled: false, used_usd: 0.0123 }), "预算: 不限 · 已用 $0.0123");
  assert.match(formatBudgetStatus({ enabled: true, used_usd: 0.1, max_usd: 2, cost_percentage: 5 }), /\$0\.1000\/\$2\.00/);
});
```

Add footer/Inspector width assertions and a process assertion for `预算: 不限` from the real Python fixture.

- [ ] **Step 2: Run RED tests**

```bash
node --test test/budget-status.test.js
node --test --test-name-pattern='nullable budget|unlimited budget|预算: 不限' \
  test/protocol.test.js test/render.test.js test/index-process.test.js
```

- [ ] **Step 3: Implement strict nullable protocol normalization**

`normalizeBudgetStatus()` preserves `null`, rejects strings/objects/negative/non-finite numbers, computes no missing limits, and lower layers never use `Number(null)` for optional fields. Route `ready`, `runtime/status`, and `mode/changed.status` budget payloads through it. Extend Inspector context normalization with the same optional-number helper.

- [ ] **Step 4: Add the pure budget formatter and integrate renderers**

`budget-status.js` renders the same four states as the Python helper and returns bounded plain text. Footer prefixes it directly; Runtime Inspector uses it with Inspector field names mapped into the shared input object. Existing ANSI truncation/wrapping remains authoritative.

- [ ] **Step 5: Migrate fixtures and process acceptance**

Default fixtures emit `enabled: false`, nullable maxima/percentages, and actual usage. Explicit finite tests keep numeric maxima. The Python fixture delegates budget status to its fake engine and the process test asserts the latest screen contains `预算: 不限` and not `$0.00`.

- [ ] **Step 6: Run focused GREEN tests and syntax check**

Run only `budget-status.test.js` and budget-named cases from protocol/render/process tests, followed by `npm run check`. Expected: pass.

- [ ] **Step 7: Self-review and commit**

Search active frontend source for `formatMoney(budget.max_usd)` and `nonnegativeNumber(tab.budget_max_usd)`; both old assumptions must be gone. Commit:

```bash
git add frontend/terminal-ui/src/protocol.js \
  frontend/terminal-ui/src/components/budget-status.js \
  frontend/terminal-ui/src/components/footer.js \
  frontend/terminal-ui/src/components/runtime-inspector.js \
  frontend/terminal-ui/test/protocol.test.js \
  frontend/terminal-ui/test/budget-status.test.js \
  frontend/terminal-ui/test/render.test.js \
  frontend/terminal-ui/test/fixtures/fake-bridge.js \
  frontend/terminal-ui/test/fixtures/history-bridge.js \
  frontend/terminal-ui/test/fixtures/message-lifecycle-bridge.js \
  frontend/terminal-ui/test/fixtures/python-bridge-fixture.py \
  frontend/terminal-ui/test/index-process.test.js
git commit -m "feat: render unlimited budgets in terminal ui"
```

---

### Task 6: Configuration Documentation and Real Bridge Acceptance

**Files:**
- Modify: `config.yaml.example`
- Modify: `docs/07-safety-guardrails.md`
- Modify: `docs/product/terminal-ui/README.md`
- Modify: `docs/product/terminal-ui/01-default-entry-and-runtime-shell.md`
- Test: targeted files from Tasks 1-5

**Interfaces:**
- Documents: nullable defaults, explicit finite examples, max turns 50, and permission/budget separation.
- Verifies: accumulated feature branch against the approved specification.

- [ ] **Step 1: Update active configuration and product documentation**

`config.yaml.example` uses active `null` values and `max_turns: 50`, plus a commented finite example. Safety docs explain that unlimited is the default, usage is still tracked, and bypass does not disable explicit budgets. Terminal UI product docs record truthful unlimited/finite display.

- [ ] **Step 2: Run fresh targeted Python verification**

Run only:

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run python -m pytest -q \
  tests/unit/test_safety.py tests/integration/test_smoke.py \
  tests/unit/test_engine.py tests/unit/test_budget_display.py \
  tests/unit/test_context_assembly.py tests/unit/test_runtime_status.py \
  tests/unit/test_agents.py tests/unit/test_agent_factory.py \
  tests/unit/test_runtime_inspector.py tests/unit/test_tui_runtime_inspector.py \
  tests/unit/test_tui.py tests/unit/test_cli_rendering.py tests/unit/test_cli_sessions.py \
  tests/unit/test_ui_bridge.py \
  -k 'budget or default_config or max_turns or runtime_status or startup_status'
```

Expected: selected tests pass; non-matching cases are deselected.

- [ ] **Step 3: Run fresh targeted JavaScript verification**

```bash
cd frontend/terminal-ui
node --test test/budget-status.test.js
NAUMI_TEST_PYTHON="$(cd ../.. && pwd)/.venv/bin/python" \
  node --test --test-name-pattern='budget|预算: 不限|real Python JSONL Bridge' \
  test/protocol.test.js test/render.test.js test/index-process.test.js
npm run check
```

Expected: selected tests and syntax check pass.

- [ ] **Step 4: Run focused Ruff and branch audits**

```bash
NAUMI_MODELS__API_KEY=unit-test-placeholder uv run ruff check \
  src/naumi_agent/config/settings.py src/naumi_agent/safety/budget.py \
  src/naumi_agent/orchestrator/engine.py src/naumi_agent/ui/budget.py \
  src/naumi_agent/orchestrator/context_assembly.py src/naumi_agent/tools/runtime.py \
  src/naumi_agent/agents/base.py src/naumi_agent/agents/factory.py \
  src/naumi_agent/agents/presets.py src/naumi_agent/inspector/models.py \
  src/naumi_agent/inspector/service.py src/naumi_agent/tui/runtime_inspector.py \
  src/naumi_agent/tui/app.py src/naumi_agent/main.py
git diff --check origin/main...HEAD
rg -n 'max_budget_usd: float = 5\.0|max_input_tokens: int = 500_000|max_output_tokens: int = 50_000|max_turns: int = 30|\$0\.0000/\$0\.00' \
  src frontend/terminal-ui config.yaml.example docs/07-safety-guardrails.md
```

Expected: Ruff and diff checks pass; stale-default search has no active matches.

- [ ] **Step 5: Commit documentation**

```bash
git add config.yaml.example docs/07-safety-guardrails.md \
  docs/product/terminal-ui/README.md \
  docs/product/terminal-ui/01-default-entry-and-runtime-shell.md
git commit -m "docs: document default unlimited budgets"
```

- [ ] **Step 6: Final slice review**

Inspect `git status --short --branch`, `git diff --stat origin/main...HEAD`, and `git log --oneline origin/main..HEAD`. Confirm only the budget feature, its specification, plan, tests, fixtures, and active docs changed. Do not claim the full repository suite passed.
