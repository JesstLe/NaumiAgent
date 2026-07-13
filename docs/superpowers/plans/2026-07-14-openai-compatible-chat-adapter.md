# OpenAI-Compatible Chat Adapter Implementation Plan

**Goal:** Execute catalog-backed `openai_chat` models through a secure shared
Router transport mapping without changing legacy requests.

**Constraints:** one feature, TDD, targeted tests only, no real secrets, no
external network, no Responses implementation.

## Task 1: Provider runtime mapping

- Add `src/naumi_agent/model/provider_runtime.py`.
- Add `tests/unit/test_provider_runtime.py` and first prove RED.
- Implement `ProviderRuntimeError`, transport result, API-format validation,
  model prefix, base URL, headers, timeout and selected-provider credential
  resolution.
- Confine relative file references to the catalog directory and bound reads.
- Prevent Keychain legacy fallback and global OpenAI key fallback.
- Run only `test_provider_runtime.py`, `test_provider_catalog.py`,
  `test_model_targets.py` and focused Ruff.
- Commit independently and review.

## Task 2: Router call and stream integration

- Add failing Router tests that capture `litellm.acompletion` kwargs for both
  `call()` and `stream()`.
- Make both paths use one `_transport_kwargs()` helper.
- Keep legacy `_base_kwargs()`, public response model, metadata, tools,
  thinking, usage and stream aggregation behavior unchanged.
- Assert unsupported catalog formats fail before LiteLLM.
- Run only Router/provider-runtime/catalog/target tests and focused Ruff.
- Commit independently and review.

## Task 3: Real local HTTP verification and integration review

- Start a temporary loopback Chat Completions server that records a request and
  returns a valid OpenAI-compatible response.
- Load a temporary native catalog with a non-secret test env token and execute
  `ModelRouter.call()` through real LiteLLM.
- Assert path, upstream model, selected auth/header and returned content.
- Re-run the same targeted tests, Ruff, import compile and `git diff --check`.
- Perform a fresh whole-branch review, fix findings, merge to `main`, re-run the
  targeted checks and push.
