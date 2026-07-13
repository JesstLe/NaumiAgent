# Zero-Config Search Routing Implementation Plan

**Goal:** Deliver deterministic, observable web search for first-time users without provider
credentials.

**Architecture:** Provider adapters normalize direct-search outcomes. WebSearchTool selects
keyed then keyless providers, and a bounded engine-level browser fallback handles the final
route without model guesswork or retry loops.

## Task 1: Normalized direct-search providers

**Files:**
- Modify: `src/naumi_agent/tools/web.py`
- Test: `tests/unit/test_web_tools.py`

- [ ] Add tests for Brave success/failure and keyless success/failure/empty outcomes.
- [ ] Introduce internal provider result and normalized search items.
- [ ] Implement deterministic provider ordering and safe diagnostics.
- [ ] Run web-tool tests and ruff.
- [ ] Commit direct search routing.

## Task 2: Deterministic browser fallback

**Files:**
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Modify or add: focused browser-search routing module
- Test: `tests/unit/test_engine.py`
- Test: browser routing unit tests

- [ ] Add failing tests for one browser fallback and loop prevention.
- [ ] Implement the bounded browser search adapter using existing browser tools/runtime.
- [ ] Normalize browser results to the direct-provider output contract.
- [ ] Run routing, engine, and browser tests.
- [ ] Commit browser fallback behavior.

## Task 3: First-run readiness and real validation

**Files:**
- Modify: `src/naumi_agent/cli/onboarding.py`
- Modify: `src/naumi_agent/ui/doctor.py`
- Modify: `README.md` and `config.yaml.example` only as needed
- Test: onboarding and doctor unit tests

- [ ] Add readiness tests for zero-config, enhanced, and restricted states.
- [ ] Present search credentials as optional enhancement.
- [ ] Run all Python and Node tests.
- [ ] Remove search keys from the test environment and complete a real web search through the
      installed Windows TUI.
- [ ] Commit onboarding/readiness behavior.

