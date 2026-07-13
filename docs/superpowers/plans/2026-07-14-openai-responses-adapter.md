# OpenAI Responses Adapter Implementation Plan

**Goal:** Add Responses transport selection while preserving one Router response
pipeline and all existing Chat/legacy behavior.

**Constraints:** TDD, one feature, no subagents, targeted tests only, no real
secrets or external network.

## Task 1: Runtime dispatcher

- Add failing provider runtime tests for `openai/responses/<upstream>` and API
  format dispatch.
- Refactor shared OpenAI transport construction without duplicating auth/file
  logic.
- Keep `build_openai_chat_transport()` compatibility.
- Run only provider runtime/catalog/target tests and focused Ruff; commit.

## Task 2: Router integration

- Add failing call and stream tests for a Responses provider.
- Replace Chat-only Router builder call with the runtime dispatcher.
- Assert public alias, Chat path, legacy path and unsupported formats.
- Run only Router/runtime/catalog/target tests and focused Ruff; commit.

## Task 3: Real bridge verification and merge

- Run a temporary loopback `/v1/responses` server with a valid minimal Responses
  payload.
- Execute real `ModelRouter.call()` through LiteLLM and assert path, model,
  auth/header, input conversion and returned content/usage.
- Perform self-review for secret leakage, protocol drift and error boundaries.
- Re-run targeted tests, Engine composition, Ruff, import compile and diff check.
- Merge to `main`, repeat targeted checks, push, then clean the worktree.
