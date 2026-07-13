# Terminal Scoped Permissions Implementation Plan

> 历史执行计划：2026-07-14 用户取消了高风险二次确认，并将 bypass 定义为全权限通过。本文保留原执行记录，不再代表当前权限语义；当前行为见 `docs/product/terminal-ui/03-execution-timeline-and-permissions.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build revocable session-scoped tool-family grants, non-bypassable high-risk double confirmation, and a parallel-safe Terminal permission queue.

**Architecture:** `PermissionChecker` classifies calls and preserves hard safety boundaries; an in-memory `PermissionGrantStore` owned by `AgentEngine` is the authorization source. Bridge and API adapters enforce server-issued high-risk challenges, while Terminal renders an ordered queue without deciding policy.

**Tech Stack:** Python 3.11+, asyncio, dataclasses, pytest, FastAPI/Pydantic, JSONL Bridge protocol, Node.js ES modules, `node:test`.

## Global Constraints

- Chinese-first user-visible copy; English code comments and commit messages.
- Hard-blocked commands and path violations remain blocked in every runtime mode.
- High-risk calls always require server-validated two-stage confirmation and never accept session grants.
- Session grants use backend-derived `session_id + tool_family`, are never persisted, and clear on session change, reset, shutdown, or revoke.
- Parallel permission responses resolve only their matching request.
- Legacy `allow` maps to `allow_once`; legacy `bypass` maps only to a medium-risk `grant_session`.
- Never touch or stage the user-owned `.superpowers/` directory.
- Run targeted tests per task; run the complete feature set only in Task 6.

## File Map

- Create `src/naumi_agent/safety/permission_grants.py` for grant records and lifecycle.
- Modify `src/naumi_agent/safety/permissions.py` for explicit outcome/risk/family policy.
- Modify `src/naumi_agent/orchestrator/engine.py` to enforce grants and expose revoke APIs.
- Create `src/naumi_agent/ui/permission_confirmation.py` for redaction and challenge tokens.
- Modify Bridge/protocol and API broker/schema/routes for confirmation transport.
- Modify Terminal state/input/cards/footer/contract/fake bridge for queue UX.
- Align Prompt Toolkit and Textual clients, then prove the real JSONL flow.

---

### Task 1: Safety Classification And Grant Store

**Files:**
- Create: `src/naumi_agent/safety/permission_grants.py`
- Modify: `src/naumi_agent/safety/permissions.py`
- Test: `tests/unit/test_permissions.py`
- Test: `tests/unit/test_permission_grants.py`

**Interfaces:**
- Produces: `PermissionOutcome`, enriched `PermissionDecision`, enriched `PermissionRule`.
- Produces: `PermissionGrantStore.create()`, `allows()`, `list_session()`, `revoke()`, `revoke_session()`, and `clear()`.
- Consumes: `PermissionMode`, `ToolMetadata`, and canonical rule resolution.

- [ ] **Step 1: Write failing classification tests**

```python
def test_shell_confirmation_is_medium_and_session_grantable(tmp_path):
    checker = PermissionChecker(
        PermissionMode.MODERATE,
        allowed_dirs=[str(tmp_path)],
        workspace_root=str(tmp_path),
    )
    decision = checker.check("bash_run", {"command": "git status", "cwd": str(tmp_path)})
    assert decision.outcome is PermissionOutcome.CONFIRM
    assert decision.risk_level is PermissionRiskLevel.MEDIUM
    assert decision.tool_family == "shell"
    assert decision.allow_session_grant is True
    assert decision.requires_double_confirm is False


def test_dangerous_command_remains_blocked_in_bypass(tmp_path):
    checker = PermissionChecker(
        PermissionMode.BYPASS,
        allowed_dirs=[str(tmp_path)],
        workspace_root=str(tmp_path),
    )
    decision = checker.check("bash_run", {"command": "sudo rm -rf /", "cwd": str(tmp_path)})
    assert decision.outcome is PermissionOutcome.BLOCK
    assert decision.code is PermissionReasonCode.DANGEROUS_COMMAND
```

Add a destructive-metadata test asserting high risk, `requires_double_confirm=True`, and `allow_session_grant=False` even in bypass.

- [ ] **Step 2: Run classification tests and verify RED**

Run: `python3 -m pytest tests/unit/test_permissions.py -k 'session_grantable or destructive_metadata or dangerous_command_remains' -q`

Expected: FAIL because enriched policy fields do not exist and bypass skips command checks.

- [ ] **Step 3: Implement explicit safety policy**

Add:

```python
class PermissionOutcome(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    BLOCK = "block"


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str = ""
    requires_confirmation: bool = False
    code: PermissionReasonCode = PermissionReasonCode.ALLOWED
    risk_level: PermissionRiskLevel = PermissionRiskLevel.LOW
    outcome: PermissionOutcome = PermissionOutcome.ALLOW
    tool_family: str = ""
    allow_session_grant: bool = False
    requires_double_confirm: bool = False
```

Extend `PermissionRule` with `risk_level`, `tool_family`, and `allow_session_grant`. Map shell/code/background/MCP-connect families explicitly. Run path and command checks regardless of mode. Metadata `destructive=True` upgrades to high; bypass suppresses only medium confirmation.

- [ ] **Step 4: Write failing grant-store tests**

```python
def test_grant_matches_only_same_session_and_family():
    store = PermissionGrantStore()
    grant = store.create("session-a", "shell", "call-1")
    assert store.allows("session-a", "shell") is True
    assert store.allows("session-a", "code_execution") is False
    assert store.allows("session-b", "shell") is False
    assert grant.source_request_id == "call-1"


def test_revoke_and_session_cleanup_are_idempotent():
    store = PermissionGrantStore()
    first = store.create("session-a", "shell", "call-1")
    store.create("session-a", "code_execution", "call-2")
    assert store.revoke(first.grant_id, "session-a") is True
    assert store.revoke(first.grant_id, "session-a") is False
    assert store.revoke_session("session-a") == 1
```

- [ ] **Step 5: Run grant tests and verify RED**

Run: `python3 -m pytest tests/unit/test_permission_grants.py -q`

Expected: FAIL with missing `permission_grants` module.

- [ ] **Step 6: Implement immutable in-memory grants**

Use `uuid4().hex`, UTC ISO `created_at`, `expires_at=None`, and deduplicate `(session_id, tool_family)`. Reject blank session/family with `ValueError`; return immutable records from list methods.

- [ ] **Step 7: Verify and commit Task 1**

Run: `python3 -m pytest tests/unit/test_permissions.py tests/unit/test_permission_grants.py -q`

Run: `ruff check src/naumi_agent/safety/permissions.py src/naumi_agent/safety/permission_grants.py tests/unit/test_permissions.py tests/unit/test_permission_grants.py`

Expected: all pass.

```bash
git add src/naumi_agent/safety/permissions.py src/naumi_agent/safety/permission_grants.py tests/unit/test_permissions.py tests/unit/test_permission_grants.py
git commit -m "feat: add scoped permission policy"
```

---

### Task 2: Engine Enforcement And Grant Lifecycle

**Files:**
- Modify: `src/naumi_agent/orchestrator/engine.py`
- Test: `tests/unit/test_engine.py`

**Interfaces:**
- Consumes: Task 1 grant store and decisions.
- Produces: `list_permission_grants()`, `revoke_permission_grant()`, `revoke_all_permission_grants()`.
- Produces callback fields `tool_family`, `choices`, `scope`, `expires_at`, and `requires_double_confirm`.

- [ ] **Step 1: Write failing Engine tests**

```python
@pytest.mark.asyncio
async def test_medium_grant_skips_only_same_session_family(engine):
    confirmations = []

    async def confirm(payload):
        confirmations.append(payload)
        return "grant_session"

    engine.set_permission_confirmer(confirm)
    first = await engine._execute_tool(
        ToolCall(id="shell-1", name="bash_run", arguments='{"command":"printf first"}')
    )
    second = await engine._execute_tool(
        ToolCall(id="shell-2", name="bash_run", arguments='{"command":"printf second"}')
    )
    assert first.status == "success"
    assert second.status == "success"
    assert len(confirmations) == 1
    assert confirmations[0]["choices"] == ["allow_once", "deny", "grant_session"]
    assert engine.list_permission_grants()[0].tool_family == "shell"
```

Add tests that high-risk `grant_session` is rejected, a grant does not match another family/session, and reset/load/shutdown clear the proper scope.

- [ ] **Step 2: Run Engine tests and verify RED**

Run: `python3 -m pytest tests/unit/test_engine.py -k 'permission_grant or grant_lifecycle or high_risk' -q`

Expected: FAIL because Engine still changes global mode and has no grant API.

- [ ] **Step 3: Enforce scoped grants in Engine**

Instantiate `PermissionGrantStore` beside `PermissionChecker`. Skip a confirmation only when a medium decision matches current session/family. Normalize choices with:

```python
if normalized in {"allow", "allowed", "yes", "y", "allow_once"}:
    return "allow_once"
if normalized in {"grant_session", "bypass", "b"}:
    return "grant_session"
return "deny"
```

Re-check `allow_session_grant` before creating a grant. Never call `set_runtime_mode()` from a permission response. Emit `confirmed`, `session_granted`, `grant_rejected`, and `denied` audit bubbles.

- [ ] **Step 4: Add lifecycle and revoke APIs**

Clear grants in `reset()` and `shutdown()`. Revoke the previous session before `load_session()` replaces it. Scope all list/revoke methods to the current session.

- [ ] **Step 5: Verify and commit Task 2**

Run: `python3 -m pytest tests/unit/test_engine.py -k 'permission or confirmation or runtime_mode' -q`

Run: `ruff check src/naumi_agent/orchestrator/engine.py tests/unit/test_engine.py`

Expected: all pass.

```bash
git add src/naumi_agent/orchestrator/engine.py tests/unit/test_engine.py
git commit -m "feat: enforce session permission grants"
```

---

### Task 3: Bridge Protocol And High-Risk Challenge

**Files:**
- Create: `src/naumi_agent/ui/permission_confirmation.py`
- Modify: `src/naumi_agent/ui/protocol.py`
- Modify: `src/naumi_agent/ui/bridge.py`
- Modify: `src/naumi_agent/ui/permission_panel.py`
- Create: `tests/unit/test_ui_protocol.py`
- Test: `tests/unit/test_ui_bridge.py`
- Test: `tests/unit/test_permission_panel.py`

**Interfaces:**
- Produces: `summarize_arguments()` and `PermissionChallengeStore`.
- Produces client event `permission_revoke`; server events `permission/confirmation_required` and `permission/grants_changed`.
- Consumes Task 2 Engine grant APIs.

- [ ] **Step 1: Write failing protocol/redaction tests**

Accept `allow_once`, `deny`, `grant_session`, and `confirm`; require a nonblank token for confirm. Accept revoke by grant ID or `scope=all`, never an empty revoke. Add:

```python
def test_argument_summary_redacts_and_bounds_output():
    summary = summarize_arguments({
        "command": "x" * 400,
        "authorization": "Bearer private",
        "nested": {"password": "private"},
    })
    assert summary["authorization"] == "[已隐藏]"
    assert summary["nested"]["password"] == "[已隐藏]"
    assert len(summary["command"]) <= 160
    assert len(json.dumps(summary, ensure_ascii=False)) <= 1200
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest tests/unit/test_ui_protocol.py tests/unit/test_ui_bridge.py -k 'permission_choice or permission_revoke or redacts or double_confirmation' -q`

Expected: FAIL because new events, choices, token, and helper do not exist.

- [ ] **Step 3: Implement redaction and one-use challenges**

`PermissionChallengeStore.issue(request_id, session_id, call_id)` uses `secrets.token_urlsafe()` and a 30-second monotonic expiry. `consume()` returns `valid`, `unknown`, `mismatch`, `expired`, or `consumed`; only valid consumption authorizes. Recursively redact keys matching token/secret/password/authorization/cookie, cap strings at 160, collections at 50 items, and total JSON at 1200 characters.

- [ ] **Step 4: Implement independent Bridge pending records**

Replace parallel Future/payload maps with one dataclass per request containing Future, public payload, choices, double-confirm flag, and challenge state. Use UUID when call ID is blank. Emit only redacted arguments:

```python
request_payload = {
    **public_payload,
    "request_id": request_id,
    "arguments_summary": summarize_arguments(payload.get("arguments", {})),
    "choices": choices,
    "scope": "session" if "grant_session" in choices else "call",
    "expires_at": None,
    "requires_double_confirm": requires_double_confirm,
}
```

For high risk, first `allow_once` emits `permission/confirmation_required` without resolving. Only matching `confirm + token` resolves. Deny resolves from either stage. Reject grant when absent from backend choices. Map legacy medium bypass to grant; reject high-risk bypass.

- [ ] **Step 5: Add grant listing/revoke and concurrency tests**

Handle `permission_revoke` through Engine and emit `permission/grants_changed`. Include grants in `/permissions`. Test two pending confirmations resolved in reverse order, bad/cross/expired/reused tokens, disconnect cleanup, and generated IDs.

- [ ] **Step 6: Verify and commit Task 3**

Run: `python3 -m pytest tests/unit/test_ui_protocol.py tests/unit/test_ui_bridge.py tests/unit/test_permission_panel.py -q`

Run: `ruff check src/naumi_agent/ui/permission_confirmation.py src/naumi_agent/ui/protocol.py src/naumi_agent/ui/bridge.py src/naumi_agent/ui/permission_panel.py tests/unit/test_ui_protocol.py tests/unit/test_ui_bridge.py tests/unit/test_permission_panel.py`

Expected: all pass.

```bash
git add src/naumi_agent/ui/permission_confirmation.py src/naumi_agent/ui/protocol.py src/naumi_agent/ui/bridge.py src/naumi_agent/ui/permission_panel.py tests/unit/test_ui_protocol.py tests/unit/test_ui_bridge.py tests/unit/test_permission_panel.py
git commit -m "feat: add permission confirmation challenges"
```

---

### Task 4: HTTP API Confirmation Parity

**Files:**
- Modify: `src/naumi_agent/api/permission_broker.py`
- Modify: `src/naumi_agent/api/schemas.py`
- Modify: `src/naumi_agent/api/routes/messages.py`
- Test: `tests/unit/test_permission_broker.py`
- Test: `tests/unit/test_api.py`

**Interfaces:**
- Consumes Engine callback policy and shared challenge store.
- Produces API result status `resolved | confirmation_required` and optional token.

- [ ] **Step 1: Write failing broker/API tests**

Test medium allow/grant/deny, unavailable grants, high-risk first-stage challenge, valid second-stage confirm, bad token, timeout, and close cleanup. Verify HTTP 409 for policy-invalid choices and 404 for unknown requests.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest tests/unit/test_permission_broker.py tests/unit/test_api.py -k 'permission' -q`

Expected: FAIL because the broker returns bool and schema accepts only old decisions.

- [ ] **Step 3: Implement API parity**

Reuse `PermissionChallengeStore`. Return a frozen broker result with `accepted`, `status`, and token. Replace schemas with:

```python
class PermissionResolutionCreate(BaseModel):
    decision: Literal["allow", "allow_once", "deny", "bypass", "grant_session", "confirm"]
    confirmation_token: str = Field(default="", max_length=256)


class PermissionResolutionResponse(BaseModel):
    status: Literal["resolved", "confirmation_required"]
    confirmation_token: str = ""
```

Pass the token through the route. A valid first-stage high-risk response returns HTTP 200 `confirmation_required`; it does not resolve the Engine Future.

- [ ] **Step 4: Verify and commit Task 4**

Run: `python3 -m pytest tests/unit/test_permission_broker.py tests/unit/test_api.py -k 'permission' -q`

Run: `ruff check src/naumi_agent/api/permission_broker.py src/naumi_agent/api/schemas.py src/naumi_agent/api/routes/messages.py tests/unit/test_permission_broker.py tests/unit/test_api.py`

Expected: all pass.

```bash
git add src/naumi_agent/api/permission_broker.py src/naumi_agent/api/schemas.py src/naumi_agent/api/routes/messages.py tests/unit/test_permission_broker.py tests/unit/test_api.py
git commit -m "feat: secure api permission confirmation"
```

---

### Task 5: Terminal Permission Queue And UX

**Files:**
- Modify: `frontend/terminal-ui/src/state.js`
- Modify: `frontend/terminal-ui/src/index.js`
- Modify: `frontend/terminal-ui/src/components/permission-card.js`
- Modify: `frontend/terminal-ui/src/components/footer.js`
- Modify: `frontend/terminal-ui/protocol-contract.json`
- Modify: `frontend/terminal-ui/test/fixtures/fake-bridge.js`
- Test: `frontend/terminal-ui/test/state.test.js`
- Test: `frontend/terminal-ui/test/components.test.js`
- Test: `frontend/terminal-ui/test/protocol.test.js`
- Test: `frontend/terminal-ui/test/index-process.test.js`

**Interfaces:**
- Consumes Task 3 Bridge events.
- Produces ordered `permissionQueue`, focused `permission`, and `permissionGrants`.

- [ ] **Step 1: Write failing queue/render tests**

```javascript
assert.deepEqual(state.permissionQueue.map((item) => item.requestId), ["perm-1", "perm-2"]);
assert.equal(state.permission.requestId, "perm-1");
handleServerRecord(state, {
  type: "permission/resolved",
  payload: { request_id: "perm-2", choice: "deny" },
});
assert.equal(state.permission.requestId, "perm-1");
assert.equal(state.permissionQueue.length, 1);
```

Also test duplicate upsert, focused advancement, medium `y/g/n` copy, and no grant/bypass copy on high risk.

- [ ] **Step 2: Run unit tests and verify RED**

Run: `node --test frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/components.test.js frontend/terminal-ui/test/protocol.test.js`

Expected: FAIL because state stores only one permission and renders bypass.

- [ ] **Step 3: Implement queue and grants state**

Initialize `permissionQueue: []` and `permissionGrants: []`. Upsert request by ID without stealing focus. Resolve/remove only matching IDs and advance only if focused. Replace grants only from authoritative `permission/grants_changed`.

- [ ] **Step 4: Implement keyboard stages**

Medium: `y -> allow_once`, `g -> grant_session` only when offered, `n -> deny`. Shift+Tab cycles runtime mode and never answers permission. High risk: first `y` requests challenge; `permission/confirmation_required` stores token/stage on matching item; Enter sends confirm; Escape returns to choice; n denies.

- [ ] **Step 5: Update components, contract, and fake Bridge**

Render:

```text
中风险: y 允许一次 · g 本会话允许 · n 拒绝
高风险第一阶段: y 查看最终确认 · n 拒绝
高风险第二阶段: Enter 确认执行 · Esc 返回 · n 拒绝
```

Show queue position, risk, family, redacted summary, and scope. Fake Bridge keeps multiple IDs, emits deterministic test challenge tokens and grants-changed, and never changes runtime mode for permission grants.

- [ ] **Step 6: Verify child-process interactions**

Cover medium g, high-risk y then Enter, reverse-order queue resolution, and Shift+Tab mode change while permission stays pending.

Run: `node --test frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/components.test.js frontend/terminal-ui/test/protocol.test.js frontend/terminal-ui/test/index-process.test.js`

Expected: PASS.

- [ ] **Step 7: Commit Task 5**

```bash
git add frontend/terminal-ui/src/state.js frontend/terminal-ui/src/index.js frontend/terminal-ui/src/components/permission-card.js frontend/terminal-ui/src/components/footer.js frontend/terminal-ui/protocol-contract.json frontend/terminal-ui/test/fixtures/fake-bridge.js frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/components.test.js frontend/terminal-ui/test/protocol.test.js frontend/terminal-ui/test/index-process.test.js
git commit -m "feat: add terminal permission queue"
```

---

### Task 6: Cross-Surface Regression And Real E2E

**Files:**
- Modify: `src/naumi_agent/cli/layout.py`
- Modify: `src/naumi_agent/tui/app.py`
- Modify: `src/naumi_agent/ui/keybindings.py`
- Modify: `frontend/terminal-ui/README.md`
- Modify: `docs/03-terminal-ui-product-spec.md`
- Test: `tests/unit/test_cli_layout.py`
- Test: `tests/unit/test_tui.py`
- Create: `frontend/terminal-ui/test/real-bridge-e2e.test.js`

**Interfaces:**
- Consumes Tasks 1-5 semantics.
- Produces consistent legacy UI behavior and a real Python Bridge E2E proof.

- [ ] **Step 1: Write failing legacy UI tests**

Change Prompt Toolkit and Textual expectations from “Bypass 并执行” to medium-risk “本会话允许”. Assert high-risk cards never return `grant_session` and require a second explicit action before returning `allow_once`.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest tests/unit/test_cli_layout.py tests/unit/test_tui.py -k 'permission' -q`

Expected: FAIL on old bypass labels/callbacks.

- [ ] **Step 3: Align legacy clients**

Return `allow_once`, `deny`, or `grant_session` according to callback `choices`. Use `requires_double_confirm` rather than inferring risk from tool name. Keep Shift+Tab only as runtime mode cycling. High risk must render and complete a second explicit confirmation.

- [ ] **Step 4: Add real Bridge E2E**

Launch the real Python JSONL Bridge from Node in a temporary workspace. Trigger a real confirmation-requiring shell tool, grant the shell family, invoke it again without a prompt, revoke the grant, and verify the third invocation prompts again. Register a high-risk fixture tool and prove first-stage allow emits `permission/confirmation_required` without `tool_end`; valid confirm then emits exactly one `tool_end`.

- [ ] **Step 5: Update product docs**

Document `y/g/n`, high-risk Enter confirmation, `/permissions revoke <id|all>`, cleanup boundaries, and runtime bypass versus scoped grants. Remove every claim that bypass skips paths, dangerous commands, or high-risk confirmation.

- [ ] **Step 6: Run complete targeted verification**

```bash
python3 -m pytest tests/unit/test_permissions.py tests/unit/test_permission_grants.py tests/unit/test_engine.py tests/unit/test_ui_protocol.py tests/unit/test_ui_bridge.py tests/unit/test_permission_panel.py tests/unit/test_permission_broker.py tests/unit/test_api.py tests/unit/test_cli_layout.py tests/unit/test_tui.py -q
```

```bash
node --test frontend/terminal-ui/test/state.test.js frontend/terminal-ui/test/components.test.js frontend/terminal-ui/test/protocol.test.js frontend/terminal-ui/test/index-process.test.js frontend/terminal-ui/test/real-bridge-e2e.test.js
```

Expected: both suites pass.

- [ ] **Step 7: Run lint and syntax verification**

```bash
ruff check src/naumi_agent/safety src/naumi_agent/orchestrator/engine.py src/naumi_agent/ui src/naumi_agent/api tests/unit/test_permissions.py tests/unit/test_permission_grants.py tests/unit/test_engine.py tests/unit/test_ui_protocol.py tests/unit/test_ui_bridge.py tests/unit/test_permission_panel.py tests/unit/test_permission_broker.py tests/unit/test_api.py tests/unit/test_cli_layout.py tests/unit/test_tui.py
node --check frontend/terminal-ui/src/state.js
node --check frontend/terminal-ui/src/index.js
node --check frontend/terminal-ui/src/components/permission-card.js
```

Expected: every command exits 0.

- [ ] **Step 8: Run one real manual scenario**

Start Bridge-backed Terminal in a temporary workspace. Execute a harmless shell command, choose session grant, execute another harmless shell command, inspect `/permissions`, revoke all, and execute a third command. Prompts must occur on calls one and three only. Then use runtime bypass with a hard-block command string and confirm rejection before tool execution.

- [ ] **Step 9: Self-review stale behavior and commit**

```bash
rg -n "bypass 并执行|b/Shift\+Tab|高风险工具将不再" src/naumi_agent frontend/terminal-ui docs/03-terminal-ui-product-spec.md
git diff --check
```

Expected: no stale UI matches and clean diff.

```bash
git add src/naumi_agent/cli/layout.py src/naumi_agent/tui/app.py src/naumi_agent/ui/keybindings.py frontend/terminal-ui/README.md docs/03-terminal-ui-product-spec.md tests/unit/test_cli_layout.py tests/unit/test_tui.py frontend/terminal-ui/test/real-bridge-e2e.test.js
git commit -m "feat: complete scoped permission workflow"
```

- [ ] **Step 10: Push feature branch**

Run: `git status --short --branch`

Expected: only user-owned `?? .superpowers/` remains.

Run: `git push origin codex/terminal-scoped-permissions`

Expected: remote branch contains design, plan, and six implementation commits.
