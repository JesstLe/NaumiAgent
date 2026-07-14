# Brave Search Configuration Implementation Plan

**Goal:** Make advanced Brave search configurable from `.naumi/config.yaml` through a safe environment reference.

1. Add failing `AppConfig` tests for `SearchConfig` defaults, validation and secret non-disclosure.
2. Implement typed `SearchConfig` / `BraveSearchConfig` and attach it to `AppConfig`.
3. Add failing web-tool tests for provider order, runtime secret resolution and official Brave parameters.
4. Inject search config through `AgentEngine` and refactor `WebSearchTool` to use it while retaining zero-config fallback.
5. Make Doctor consume the same resolved readiness and update sample config/README.
6. Run only `test_config.py`, `test_web_tools.py`, `test_doctor.py` selections, relevant Engine registration tests, Ruff and diff checks; self-review then commit independently.
