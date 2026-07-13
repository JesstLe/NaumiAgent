# Default-Unlimited Budget Design

**Date:** 2026-07-14
**Status:** Approved and implemented
**Scope:** Main Agent and child-Agent session budgets, runtime budget status, and current CLI/TUI surfaces

## 1. Objective

NaumiAgent must stop imposing hidden finite session budgets on ordinary work. The main Agent,
dynamic Agents, and preset Agents default to unlimited cumulative input tokens, cumulative output
tokens, and model cost. A user or caller can explicitly enable any subset of those limits when a
bounded run is required.

The default turn ceiling increases to 50 for the main Agent and child Agents. It remains a separate
loop-safety boundary rather than being represented as a token or cost budget.

This change must preserve usage accounting. Unlimited means “do not stop on a session budget,” not
“stop measuring usage” or “ignore model context-window constraints.”

## 2. Current-State Evidence

The current implementation has several independent finite defaults:

- `SafetyConfig.max_budget_usd` defaults to `$5.00`.
- `SafetyConfig.max_input_tokens` defaults to `500,000`.
- `TokenBudget.max_output_tokens` silently defaults to `50,000`, although it is not exposed by
  `SafetyConfig`.
- `SafetyConfig.max_turns` defaults to 30.
- `DynamicAgentFactory` invents `$0.08`, `$0.15`, or `$0.25` limits when the caller provides no
  child-Agent budget.
- preset coder, researcher, and browser Agents impose `$0.50`, `$0.50`, and `$0.30` limits and use
  smaller turn ceilings.
- `AgentEngine._check_budget()` skips all budget enforcement in bypass permission mode.
- the Bridge, context assembler, new Terminal UI, Textual TUI, runtime Inspector, runtime status
  tool, and temporary legacy CLI format `max_usd` as an always-present finite number.

Changing only the `$5.00` configuration default would leave token ceilings, child-Agent ceilings,
incorrect UI text, and bypass-mode semantics unchanged. The feature therefore requires one shared
budget contract across these consumers.

## 3. Chosen Representation

### 3.1 Nullable limits

Each optional resource limit uses `None` in Python and `null` over JSON:

- `max_budget_usd: float | None = None`
- `max_input_tokens: int | None = None`
- `max_output_tokens: int | None = None`

`None` means no limit for that resource. A non-negative number enables that specific limit. Negative
values are invalid configuration. Zero is a real zero allowance, not an unlimited sentinel, and must
prevent the first model call.

This representation is selected over two rejected alternatives:

1. `float("inf")` is convenient inside Python but is not portable JSON and produces fragile YAML,
   JavaScript, SQLite, and UI behavior.
2. `0` as an unlimited sentinel is ambiguous and makes an intentional zero-cost or zero-token policy
   impossible.

### 3.2 Turn ceiling

The main Agent and child-Agent default `max_turns` value is 50. Preset and dynamically generated
Agents no longer silently lower it to 15, 20, or 25. Explicit caller overrides remain supported.

This is not an unlimited setting. It is the final loop-safety guard against a non-progressing model.
Pursuit iteration controls, model context windows, per-call model output caps, tool timeouts, and
permission rules remain separate boundaries.

## 4. Configuration Contract

`SafetyConfig` owns the main-session defaults:

```yaml
safety:
  max_budget_usd: null
  max_input_tokens: null
  max_output_tokens: null
  max_turns: 50
```

All three budget keys may be omitted because their defaults are `null`. Environment variables and
YAML numeric values continue to enable a limit. For example:

```yaml
safety:
  max_budget_usd: 2.0
  max_output_tokens: 100000
  max_turns: 50
```

An existing user configuration containing `max_budget_usd: 5.0`, `max_input_tokens: 500000`, or
`max_turns: 30` remains an explicit override. The migration is non-destructive: users remove the
finite keys or set budget keys to `null` to adopt the new unlimited defaults.

`config.yaml.example` documents the unlimited defaults and shows a commented finite example. It must
not ship an active `$5.00` or token ceiling that silently cancels the new product default.

## 5. Budget Domain Model

### 5.1 TokenBudget

`TokenBudget` stores the three nullable resource limits and exposes whether any limit is enabled.
The object does not store `Infinity`, magic numbers, or UI strings.

### 5.2 BudgetTracker

`BudgetTracker` always records input tokens, output tokens, cost, per-model breakdown, and timestamps.
Its enforcement logic evaluates only configured limits:

- no limits: `is_exceeded()` is always false;
- one limit: only that resource can stop the run;
- multiple limits: the first check reports every resource already over its limit;
- a zero limit: the run is already exhausted before the first model call;
- reset: usage returns to zero while the configured limit policy remains unchanged.

Remaining cost is nullable. It is `None` when cost is unlimited and a non-negative number when a cost
limit is active.

### 5.3 Main-engine enforcement

`AgentEngine._check_budget()` no longer contains a bypass-mode exemption. Permission bypass means all
tool approvals are granted; it does not disable an explicitly requested resource policy.

The engine checks the budget before every model call and after tracked usage at the existing control
points. An explicit zero limit stops before a paid call. An exceeded result remains
`status="budget_exceeded"` and names only the enabled resources that caused the stop.

### 5.4 Child Agents

`AgentConfig.max_budget_usd` becomes nullable and defaults to `None`. Child Agents continue tracking
their real cost but only stop for cost when the caller provides a numeric limit.

`DynamicAgentFactory` treats an omitted budget as unlimited instead of invoking `_detect_budget()`.
The automatic budget estimator and its tests are removed because a hidden estimator contradicts the
new product contract. Explicit `max_budget_usd` overrides still flow through the factory and
SubAgentManager.

Preset Agents remove their finite cost fields and use the shared 50-turn default. Explicit per-run
overrides remain available for intentionally bounded delegations.

## 6. Runtime Status Contract

`AgentEngine.get_budget_info()` returns a JSON-safe object with usage and nullable limits:

```json
{
  "enabled": false,
  "used_usd": 0.0123,
  "max_usd": null,
  "remaining_usd": null,
  "cost_percentage": null,
  "input_tokens": 42000,
  "max_input_tokens": null,
  "input_percentage": null,
  "output_tokens": 8000,
  "max_output_tokens": null,
  "output_percentage": null,
  "percentage": null
}
```

When one or more limits are enabled:

- `enabled` is true;
- each resource percentage is present only when that resource has a limit;
- `percentage` is the maximum active-resource percentage and is used only as a compact warning
  signal;
- values may exceed 100 after a provider response because actual usage is known only after the call;
- no field contains `Infinity`, `NaN`, or a string in place of a number.

The JSONL Bridge passes this status through without inventing defaults. Inspector models make budget
maxima and percentages optional and carry `budget_enabled` so Python, JavaScript, and Textual
renderers do not collapse unlimited into zero.

## 7. User-Visible Behavior

All current status surfaces use the same semantics:

- no active limits: `预算: 不限 · 已用 $0.0123`;
- cost limit: `预算: $0.0123/$2.00 (0.6%)`;
- token-only limit: `预算: 不限费用 · 输入 42K/500K` or
  `预算: 不限费用 · 输出 8K/100K`;
- multiple limits: show cost plus active token limits, with bounded wrapping in narrow terminals;
- exceeded: the existing Chinese stop message lists the precise resource or resources.

The surfaces in scope are:

- new Terminal UI footer and Runtime Inspector;
- Textual TUI startup status, usage blocks, restored-session status, and Runtime Inspector;
- runtime context injected into the model;
- runtime status tool output;
- temporary legacy CLI status output while the separate CLI-deprecation slice is pending.

Unlimited display never hides actual cost. It removes the stop limit while preserving transparent
usage reporting.

## 8. Error Handling and Compatibility

- negative budget values fail configuration validation with a clear field-specific error;
- malformed non-numeric environment or YAML values fail through Pydantic instead of being coerced to
  unlimited;
- old numeric configuration remains finite and is never silently widened;
- old UI fixtures are migrated to `null` and `enabled=false` where they represent defaults;
- tests that intentionally exercise budget exhaustion keep explicit numeric limits;
- protocol consumers must preserve `null`; converting it to `0` is a correctness bug;
- budget information remains available even if model price metadata reports zero cost.

## 9. Verification Strategy

Only targeted small-module verification is run for this slice.

### Python

- `SafetyConfig` defaults all budget fields to `None` and `max_turns` to 50;
- YAML and environment numeric overrides enable limits; negative values fail;
- `BudgetTracker` unlimited, single-limit, multiple-limit, zero-limit, reset, and summary behavior;
- `AgentEngine` unlimited default, explicit enforcement in default and bypass permission modes, and
  JSON-safe status payload;
- dynamic and preset child Agents default unlimited with 50 turns, while explicit budget and turn
  overrides still stop correctly;
- Bridge ready/status events carry nullable budget fields;
- context, runtime tool, CLI, Textual TUI, and Inspector formatting distinguish unlimited and finite
  states.

### JavaScript

- protocol normalization preserves nullable maxima and rejects invalid finite fields;
- Terminal UI footer and Runtime Inspector render unlimited, cost-limited, token-limited, and narrow
  layouts without `Infinity`, `$0.00` fake limits, or overflow;
- a real process connected to the Python JSONL Bridge displays `预算: 不限` by default.

The full Python and Node suites are explicitly deferred. Each implementation task runs its own
focused tests, Ruff or syntax checks, and is committed independently.

## 10. Scope Boundaries

This slice does not:

- remove model context-window or per-call output constraints;
- make the loop turn ceiling unlimited;
- redesign provider pricing or model routing;
- add a budget settings screen or new slash command;
- change Pursuit iteration policy;
- deprecate the old CLI;
- implement the broader tool, provider, documentation-cleanup, color-system, or loading-animation
  goals.

Those remain separate feature slices so this budget contract can be verified and shipped without
mixing unrelated behavior.
