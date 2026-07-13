# Provider JSON Catalog Implementation Plan

> **For agentic workers:** Use test-driven development and only the new catalog test module.

**Goal:** Parse and validate multi-provider JSON into a safe, immutable catalog without network or secret resolution.

**Architecture:** A standalone model catalog module performs bounded duplicate-aware JSON parsing, shape adaptation for Naumi/OpenCode inputs, strict normalization, secret-reference validation and deterministic model filtering.

**Tech Stack:** Python 3.14 standard library, dataclasses, StrEnum, pytest, Ruff.

## Constraints

- No network calls, Keychain reads or environment secret reads.
- No plaintext secrets accepted.
- No npm package execution.
- No changes to `ModelRouter` in this slice.
- No full test suite.

### Task 1: Typed normalized catalog

**Files:**
- Create: `src/naumi_agent/model/catalog.py`
- Test: `tests/unit/test_provider_catalog.py`

- [ ] Write failing native-shape tests for provider, auth, models, limits, capabilities, discovery and filters.
- [ ] Implement immutable enums/dataclasses and strict field normalization.
- [ ] Run the new module tests and targeted Ruff.

### Task 2: OpenCode shape adapter and secret references

**Files:**
- Modify: `src/naumi_agent/model/catalog.py`
- Test: `tests/unit/test_provider_catalog.py`

- [ ] Write failing tests for `provider/options/baseURL/npm/models`, env/file refs and unknown adapter hints.
- [ ] Implement known adapter mapping without importing JavaScript runtime code.
- [ ] Reject inline keys and sensitive headers with redacted errors.

### Task 3: Bounded file loader and real fixture

**Files:**
- Modify: `src/naumi_agent/model/catalog.py`
- Test: `tests/unit/test_provider_catalog.py`

- [ ] Write failing tests for duplicate keys, malformed JSON, size cap and safe paths.
- [ ] Implement UTF-8 bounded load and duplicate-aware decoder.
- [ ] Run a real copy of the local OpenCode provider fixture after replacing only path placeholders; prove secret files are never read.
- [ ] Run targeted tests, Ruff, import smoke and self-review.
- [ ] Commit with `git commit -m "feat: load provider catalogs from json"`.

