# Model reasoning-effort design

## Objective

Add a real, capability-aware model reasoning-intensity control to NaumiAgent. The selected effort
must be validated against the active model, translated into a provider request that is known to be
accepted by the current transport, and remain visible and controllable from the API, new terminal
UI, classic CLI, and Textual TUI.

This control is different from the existing `/reasoning` switch. `/reasoning` only decides whether
returned reasoning/thinking text is shown. `/effort` changes how much reasoning work the upstream
model is asked to perform.

## Decision

NaumiAgent exposes one public effort vocabulary:

`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`.

`auto` is a runtime/configuration state, not an upstream effort value. It means NaumiAgent omits the
reasoning-effort request field and lets the provider/model use its own default.

Capabilities are model-scoped. A model catalog entry declares the exact effort values that can be
transported for that model and may declare a display-only default. NaumiAgent never infers a list
from a broad provider name and never sends an unlisted value. Legacy `reasoning: true` remains
accepted as a boolean capability but has no selectable effort values; it therefore reports that the
model can reason while leaving `/effort` in `auto`.

## Alternatives considered

1. Pass any user string to LiteLLM as `reasoning_effort`. This is small but unsafe: different models
   accept different subsets, and the installed LiteLLM rejects some values that newer provider docs
   advertise.
2. Use one global `low/medium/high` list. This hides useful model-specific levels such as OpenAI
   `minimal`/`xhigh` and Claude `max`, and still cannot prove transport support.
3. Use a catalog-backed capability object and a shared resolver. This is selected because the UI,
   validation, and provider request all derive from the same immutable source of truth.

## Catalog contract

The `capabilities.reasoning` field accepts either the existing boolean or this object:

```json
{
  "capabilities": {
    "reasoning": {
      "efforts": ["low", "medium", "high"],
      "defaultEffort": "medium"
    }
  }
}
```

Rules:

- `efforts` is required, non-empty, contains no duplicates, and preserves the catalog order.
- Every value must belong to the public vocabulary; `auto` is forbidden in `efforts`.
- `defaultEffort`, when present, must appear in `efforts`.
- An object means `supports_reasoning=true`.
- `reasoning: false` means no reasoning capability.
- `reasoning: true` preserves compatibility but exposes an empty selectable list.
- Remotely discovered models do not inherit static effort capabilities unless they match a static
  catalog model by local or upstream identity.

The parsed `ProviderModelSpec`, discovery `AvailableModel`, REST `ModelInfo`, and `/models` output all
carry `reasoning_efforts` and `default_reasoning_effort`.

## Configuration contract

`.naumi/config.yaml` may select a default effort globally and override it for named models:

```yaml
models:
  reasoning_effort: auto
  model_info:
    openai/gpt-5.4:
      reasoning_efforts: [none, low, medium, high, xhigh]
      default_reasoning_effort: medium
      reasoning_effort: high
```

The values are validated by Pydantic against `auto` plus the public vocabulary. Configuration does
not contain credentials. The per-model override may use the requested model id, canonical catalog
id, or configured catalog alias; target resolution applies the same canonicalization used for
context and price metadata. `reasoning_efforts` and `default_reasoning_effort` are capability
overrides for direct/legacy models that have no catalog entry. When both sources exist, explicit
`model_info` capability metadata wins, allowing a project to narrow transport levels after a
provider or LiteLLM compatibility change. The default must occur in the declared effort list.

Resolution order for one request is:

1. current runtime override set by `/effort`;
2. per-model `model_info` selection;
3. global `models.reasoning_effort` selection;
4. `auto`.

The runtime override is process-local and does not rewrite `.naumi/config.yaml`. `/effort reset`
clears it and returns to configuration resolution; `/effort auto` explicitly overrides configured
values with provider-default behavior until reset.

## Validation and request transport

`ModelRouter` owns the effort resolver because it owns alias resolution and the final transport
request. Resolution returns an immutable status containing the requested model, effective value,
source (`runtime`, `model`, `global`, or `auto`), supported values, model default, and an optional
Chinese warning.

For every call and stream:

- `auto` omits `reasoning_effort` completely.
- An explicit value must occur in the active model's declared capability list or the request fails
  before network I/O with a Chinese error listing the available values.
- For a direct model without catalog metadata, a `model_info` effort list supplies the same
  capability contract; models with neither source remain safely on `auto`.
- A configured global value that is unsupported by the selected model also fails visibly. Silent
  fallback would make the status bar lie about request behavior.
- Explicit effort is sent to LiteLLM as top-level `reasoning_effort`; it is not nested in
  `extra_body`.
- When explicit effort is active, NaumiAgent omits `temperature`. Reasoning APIs commonly restrict
  sampling parameters, and an omitted parameter preserves provider defaults.
- Explicit Kimi `thinking` and reasoning effort cannot be combined in one call. The router raises a
  clear conflict instead of sending two competing controls. Existing Kimi automatic thinking
  remains unchanged when effort is `auto`.
- The same logic is shared by `call()` and `stream()`.

Transport support is verified against the installed LiteLLM using local loopback HTTP, not a real
credential. At design time the installed transport correctly produced Claude native
`output_config.effort` plus adaptive thinking for `medium` and `max`, but rejected Claude `xhigh`.
Catalog examples and tests therefore expose only values proven by the current transport for each
model. The public vocabulary can still expose `xhigh` for other models whose transport accepts it.

## Runtime command and surfaces

`/effort` is a shared slash command used by classic CLI, Textual TUI, and the new terminal UI:

- `/effort` shows the active model, effective effort, resolution source, supported values, and
  catalog default.
- `/effort <value>` validates and sets a process-local override.
- `/effort auto` explicitly selects provider default.
- `/effort reset` clears the runtime override.

`/model` adds the same effective effort and supported-value summary. `/models` annotates reasoning
models with their exact effort list rather than a bare boolean.

The JSONL status payload adds a bounded `reasoning_effort` object. The new terminal UI welcome view
shows `思考强度 <value>` and the footer renders a compact `强度: <value>` segment. The existing
footer label becomes `思考文本: on/off` so users do not confuse visibility with compute effort.
Textual TUI status uses the same terminology. The REST `/config` response adds the selected effort
status and per-model effort metadata.

The new terminal UI does not maintain a second effort state machine. It sends `/effort` through the
shared slash-command bridge, then consumes the authoritative status emitted after command
completion.

## Errors and edge cases

- Invalid catalog shapes report the provider/model field path and accepted effort values.
- Invalid YAML values fail configuration loading before runtime construction.
- `/effort` on a legacy boolean-capable or unknown/discovered model explains that no selectable
  strength was declared and remains in `auto`.
- Switching model tiers or aliases immediately recomputes effective effort for the newly active
  model; the status payload never caches the previous model's supported list.
- Runtime overrides are validated again at request time so a model switch cannot send an invalid
  effort.
- Empty model ids and runtime-identity lookup failures degrade to `auto` status without exposing
  secrets or provider response bodies.

## Verification

Targeted tests cover:

- catalog boolean compatibility and strict reasoning-object parsing;
- propagation through discovery and REST schemas;
- global, per-model, runtime, explicit-auto, and reset resolution precedence;
- alias/canonical metadata lookup;
- invalid and unsupported values before network I/O;
- call/stream parity, temperature omission, and Kimi conflict behavior;
- loopback capture of actual OpenAI-compatible and Anthropic request bodies;
- shared `/effort`, `/model`, and `/models` rendering;
- JSONL bridge status and command refresh;
- new terminal UI protocol normalization, welcome, footer, and command routing;
- Textual TUI status wording;
- Ruff/import checks for only the touched Python modules.

The repository-wide test suite is intentionally excluded.

## Self-review

This design deliberately does not guess capabilities from model names. That requires explicit
catalog or `model_info` metadata for selectable effort, but it prevents a polished UI from promising
a control the transport ignores. Direct models remain practical because their metadata can live in
the same `.naumi/config.yaml`; a separate provider catalog is optional. It also keeps Kimi's binary
thinking protocol separate instead of forcing it into a false low/medium/high abstraction.

The remaining limitation is that catalog authors must maintain effort lists as provider models
evolve. Automatic model discovery APIs generally do not publish reasoning-level capability data,
so that limitation cannot be removed reliably in this slice. A future signed/curated built-in
model capability registry can reduce manual maintenance without weakening validation.

## Out of scope

- Persisting `/effort` runtime changes back into `.naumi/config.yaml`.
- Upgrading or patching LiteLLM solely to unlock a provider level it currently rejects.
- Converting Kimi binary thinking into a synthetic numeric effort.
- Guessing reasoning capabilities for arbitrary discovered models.
