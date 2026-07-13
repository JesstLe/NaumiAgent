# Provider-Scoped Model Credentials Implementation Plan

> **For agentic workers:** Execute in order with test-driven development. Run only the named small test modules.

**Goal:** Store and resolve model API keys by explicit provider without breaking legacy installations.

**Architecture:** The credential store derives a validated provider-specific keyring account. Configuration, onboarding and migration pass the explicit provider; environment injection remains highest priority and legacy global credentials remain read-only fallback.

**Tech Stack:** Python 3.14, keyring protocol, Pydantic settings, pytest, Ruff.

## Constraints

- Never enumerate all provider credentials at startup.
- Never serialize API keys into YAML or JSON.
- Never infer a credential from base URL.
- Never expose secret values in errors or tests.
- Do not run the full test suite.

### Task 1: Credential account namespace

**Files:**
- Modify: `src/naumi_agent/config/credentials.py`
- Test: `tests/unit/test_credentials.py`

- [ ] Add failing tests for independent provider keys, normalization, invalid IDs, legacy fallback, disabled fallback and backend failures.
- [ ] Implement validated provider account derivation and backward-compatible function signatures.
- [ ] Run `NAUMI_MODELS__API_KEY=unit-test-placeholder ... pytest tests/unit/test_credentials.py -q` and targeted Ruff.

### Task 2: Active-provider configuration loading

**Files:**
- Modify: `src/naumi_agent/config/settings.py`
- Test: `tests/unit/test_config.py`
- Test: `tests/unit/test_deploy.py`

- [ ] Add failing tests proving YAML provider is forwarded and environment keys skip credential access.
- [ ] Load only the active provider with legacy fallback.
- [ ] Run only config and deploy tests that mention credentials/provider.

### Task 3: Configure and onboarding writes

**Files:**
- Modify: `src/naumi_agent/config/configurator.py`
- Modify: `src/naumi_agent/cli/onboarding.py`
- Test: `tests/unit/test_configurator.py`
- Test: `tests/unit/test_configure_command.py`
- Test: `tests/unit/test_onboarding.py`

- [ ] Add failing tests proving configure, onboarding and legacy migration write the selected provider account.
- [ ] Preserve existing injected test-store callback compatibility while production uses provider-scoped storage.
- [ ] Run the named test modules with credential placeholder and targeted Ruff.
- [ ] Run a real in-memory backend scenario with three providers and legacy fallback.
- [ ] Self-review precedence, Keychain access count, error redaction and backward compatibility.
- [ ] Commit with `git commit -m "feat: scope model credentials by provider"`.

