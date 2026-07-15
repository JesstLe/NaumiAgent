# ARC-01.4a Runtime Ports Composition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all five default Runtime Port adapter constructions into one Composition Root and route every production Engine startup through it without changing runtime behavior.

**Architecture:** Add frozen protocol-only `RuntimePorts` and `RuntimePortOverrides` bundles, then construct their default adapters exclusively in `runtime/composition.py`. `AgentEngine` consumes a complete bundle; its temporary legacy keyword path delegates to the same builder, while CLI, TUI, API, run, and New UI use `create_agent_engine()` directly.

**Tech Stack:** Python 3.12+, PEP 695 generics, dataclasses, runtime-checkable Protocols, Pydantic settings, asyncio, pytest, Ruff, compileall, AST/static source audits.

## Global Constraints

- Implement only ARC-01.4a; do not migrate Store/Resource/Service construction from Engine in this plan.
- Do not add a third-party dependency injection container, Service Locator, mutable singleton, or dict/Any dependency bag.
- All user-visible validation errors are Chinese; code comments and commit messages are English.
- `None` is the only default-selection sentinel; complete falsey overrides preserve object identity.
- Production entrypoints must use one root; test factory overrides remain supported.
- Do not change ReAct, tool authorization, event ordering, completion receipt, workspace binding, or shutdown behavior.
- Do not run the full test suite. Run only commands named in each task.
- Use `/Users/lv/Workspace/NaumiAgent/.venv/bin/python` and `/Users/lv/Workspace/NaumiAgent/.venv/bin/ruff` in the isolated worktree.
- One task, one red/green cycle, one self-review, one independent English commit.

---

## File Map

| File | Responsibility |
| --- | --- |
| `src/naumi_agent/runtime/dependencies.py` | Protocol-only immutable Port/override bundles and validation |
| `src/naumi_agent/runtime/composition.py` | Default adapter selection and authoritative Engine factory |
| `src/naumi_agent/orchestrator/engine.py` | Consume RuntimePorts; temporary legacy arguments delegate to root |
| `src/naumi_agent/main.py` | TUI, fallback CLI, and run use the root factory |
| `src/naumi_agent/api/app.py` | FastAPI lifespan uses the root factory |
| `src/naumi_agent/ui/bridge.py` | New UI default factory uses the root; explicit test factory preserved |
| `tests/unit/test_runtime_dependencies.py` | Bundle structure, frozen behavior, complete/partial/falsey contracts |
| `tests/unit/test_runtime_composition.py` | Defaults, override identity, config-derived defaults, builder isolation |
| `tests/unit/test_engine_port_bundle.py` | Engine bundle path, conflict rejection, legacy delegation |
| `tests/unit/test_runtime_composition_entrypoints.py` | Production constructor routing and lifecycle preservation |
| `tests/integration/test_runtime_composition_streaming.py` | Real Engine streaming path from root through receipt and shutdown |
| `tests/unit/test_architecture_runtime_composition.py` | AST/import/direct-construction budgets |
| `docs/development/architecture/ARC-01-domain-boundaries.md` | ARC-01.4a implementation status and evidence links |
| `docs/development/architecture/ARC-01-4a-runtime-ports-composition-design.md` | Final audit evidence and self-review append |

---

### Task 1: Define protocol-only Runtime Port bundles

**Files:**
- Create: `tests/unit/test_runtime_dependencies.py`
- Create: `src/naumi_agent/runtime/dependencies.py`

**Interfaces:**
- Consumes: `SessionPort[SessionT]`, `PermissionPort`, `ModelPort`, `ToolExecutionPort`, `EventSink`.
- Produces: `RuntimePorts[SessionT]`, `RuntimePortOverrides[SessionT]`, `validate_runtime_port_overrides(overrides) -> None`.

- [ ] **Step 1: Write failing bundle contract tests**

Create `tests/unit/test_runtime_dependencies.py` with concrete complete adapters and deliberately incomplete objects:

```python
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from naumi_agent.config.settings import MemoryConfig, ModelConfig
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.model.router import ModelRouter
from naumi_agent.runtime.dependencies import (
    RuntimePortOverrides,
    RuntimePorts,
    validate_runtime_port_overrides,
)
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.streaming.sinks import NullEventSink
from naumi_agent.tools.execution import LocalToolExecutor


class _FalseySink(NullEventSink):
    def __bool__(self) -> bool:
        return False


def _complete_ports(tmp_path):
    return {
        "session_port": SessionStore(MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            long_term_enabled=False,
        )),
        "permission_port": PermissionChecker(PermissionMode.MODERATE),
        "model_port": ModelRouter(ModelConfig()),
        "tool_execution_port": LocalToolExecutor(),
        "event_sink": _FalseySink(),
    }


def test_runtime_ports_are_frozen_and_preserve_complete_falsey_identity(tmp_path):
    values = _complete_ports(tmp_path)
    ports = RuntimePorts[Session](**values)

    assert ports.event_sink is values["event_sink"]
    with pytest.raises(FrozenInstanceError):
        ports.event_sink = NullEventSink()  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("session_port", "session_port 必须实现完整的 SessionPort 契约"),
        ("permission_port", "permission_port 必须实现完整的 PermissionPort 契约"),
        ("model_port", "model_port 必须实现完整的 ModelPort 契约"),
        ("tool_execution_port", "tool_execution_port 必须实现完整的 ToolExecutionPort 契约"),
        ("event_sink", "event_sink 必须实现完整的 EventSink 契约"),
    ],
)
def test_runtime_ports_reject_each_incomplete_field_before_use(
    tmp_path, field, message,
):
    values = _complete_ports(tmp_path)
    values[field] = object()

    with pytest.raises(TypeError, match=message):
        RuntimePorts[Session](**values)


def test_overrides_allow_none_and_reject_non_none_partial_port():
    empty = RuntimePortOverrides[Session]()
    validate_runtime_port_overrides(empty)

    invalid = RuntimePortOverrides[Session](event_sink=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="event_sink 必须实现完整的 EventSink 契约"):
        validate_runtime_port_overrides(invalid)
```

- [ ] **Step 2: Run the test and verify the missing module failure**

Run:

```bash
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q \
  tests/unit/test_runtime_dependencies.py
```

Expected: collection fails with `ModuleNotFoundError: No module named 'naumi_agent.runtime.dependencies'`.

- [ ] **Step 3: Implement exact bundle types and validation**

Create `src/naumi_agent/runtime/dependencies.py`:

```python
"""Explicit dependency bundles consumed by the Agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, fields

from naumi_agent.runtime.ports.events import EventSink
from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.runtime.ports.permission import PermissionPort
from naumi_agent.runtime.ports.session import SessionPort
from naumi_agent.runtime.ports.tool_execution import ToolExecutionPort

_PORT_CONTRACTS: dict[str, tuple[type[object], str]] = {
    "session_port": (
        SessionPort,
        "session_port 必须实现完整的 SessionPort 契约："
        "create_session/save/load/list_sessions/delete/archive/close",
    ),
    "permission_port": (
        PermissionPort,
        "permission_port 必须实现完整的 PermissionPort 契约："
        "mode/set_mode/check/reset_counts",
    ),
    "model_port": (
        ModelPort,
        "model_port 必须实现完整的 ModelPort 契约："
        "metadata/routing/capability/discovery/reasoning/call/stream",
    ),
    "tool_execution_port": (
        ToolExecutionPort,
        "tool_execution_port 必须实现完整的 ToolExecutionPort 契约：invoke",
    ),
    "event_sink": (
        EventSink,
        "event_sink 必须实现完整的 EventSink 契约：emit",
    ),
}


@dataclass(frozen=True, slots=True)
class RuntimePorts[SessionT]:
    session_port: SessionPort[SessionT]
    permission_port: PermissionPort
    model_port: ModelPort
    tool_execution_port: ToolExecutionPort
    event_sink: EventSink

    def __post_init__(self) -> None:
        for item in fields(self):
            _require_port(item.name, getattr(self, item.name))


@dataclass(frozen=True, slots=True)
class RuntimePortOverrides[SessionT]:
    session_port: SessionPort[SessionT] | None = None
    permission_port: PermissionPort | None = None
    model_port: ModelPort | None = None
    tool_execution_port: ToolExecutionPort | None = None
    event_sink: EventSink | None = None


def validate_runtime_port_overrides[SessionT](
    overrides: RuntimePortOverrides[SessionT],
) -> None:
    for item in fields(overrides):
        value = getattr(overrides, item.name)
        if value is not None:
            _require_port(item.name, value)


def _require_port(name: str, value: object) -> None:
    protocol, message = _PORT_CONTRACTS[name]
    if not isinstance(value, protocol):
        raise TypeError(message)


__all__ = [
    "RuntimePortOverrides",
    "RuntimePorts",
    "validate_runtime_port_overrides",
]
```

Keep `runtime/__init__.py` unchanged. Callers import this focused contract from
`naumi_agent.runtime.dependencies`, preventing an unrelated `naumi_agent.runtime.*` import from eagerly loading
composition adapters.

- [ ] **Step 4: Run focused contract, Ruff, and compile checks**

Run:

```bash
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q \
  tests/unit/test_runtime_dependencies.py
/Users/lv/Workspace/NaumiAgent/.venv/bin/ruff check \
  src/naumi_agent/runtime/dependencies.py \
  tests/unit/test_runtime_dependencies.py
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m compileall -q \
  src/naumi_agent/runtime/dependencies.py
```

Expected: all new tests pass, Ruff exits 0, compileall exits 0.

- [ ] **Step 5: Self-review and commit the contract**

Check:

```bash
rg -n "SessionStore|PermissionChecker|ModelRouter|LocalToolExecutor|NullEventSink|AgentEngine|\bAny\b" \
  src/naumi_agent/runtime/dependencies.py
git diff --check
```

Expected: the first command has no matches; `git diff --check` exits 0.

Commit:

```bash
git add src/naumi_agent/runtime/dependencies.py \
  tests/unit/test_runtime_dependencies.py
git commit -m "feat(runtime): define composition port bundles [ARC-01.4a]"
```

---

### Task 2: Build the authoritative default Port Composition Root

**Files:**
- Create: `tests/unit/test_runtime_composition.py`
- Create: `src/naumi_agent/runtime/composition.py`

**Interfaces:**
- Consumes: `AppConfig`, `RuntimePortOverrides[Session]` from Task 1, five concrete default adapters.
- Produces: `build_runtime_ports(config, *, overrides=None) -> RuntimePorts[Session]` and `create_agent_engine(config, *, port_overrides=None) -> AgentEngine`.

- [ ] **Step 1: Write failing default and override tests**

Create `tests/unit/test_runtime_composition.py`. Use `_config(tmp_path)` with explicit workspace/session/chroma paths and these assertions:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig, SafetyConfig
from naumi_agent.memory.session import SessionStore
from naumi_agent.model.router import ModelRouter
from naumi_agent.runtime.composition import build_runtime_ports
from naumi_agent.runtime.dependencies import RuntimePortOverrides
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.streaming.sinks import NullEventSink
from naumi_agent.tools.execution import LocalToolExecutor


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
            vector_db_path=str(tmp_path / ".naumi" / "chroma"),
            long_term_enabled=False,
        ),
        safety=SafetyConfig(
            permission_mode="bypass",
            allowed_dirs=[str(tmp_path / "explicit")],
        ),
    )


def test_build_runtime_ports_selects_all_production_defaults(tmp_path):
    ports = build_runtime_ports(_config(tmp_path))
    assert isinstance(ports.session_port, SessionStore)
    assert isinstance(ports.permission_port, PermissionChecker)
    assert ports.permission_port.mode is PermissionMode.BYPASS
    assert isinstance(ports.model_port, ModelRouter)
    assert isinstance(ports.tool_execution_port, LocalToolExecutor)
    assert isinstance(ports.event_sink, NullEventSink)


def test_build_runtime_ports_preserves_every_override_identity(tmp_path):
    defaults = build_runtime_ports(_config(tmp_path))
    overrides = RuntimePortOverrides(
        session_port=defaults.session_port,
        permission_port=defaults.permission_port,
        model_port=defaults.model_port,
        tool_execution_port=defaults.tool_execution_port,
        event_sink=defaults.event_sink,
    )
    ports = build_runtime_ports(_config(tmp_path), overrides=overrides)
    assert ports.session_port is overrides.session_port
    assert ports.permission_port is overrides.permission_port
    assert ports.model_port is overrides.model_port
    assert ports.tool_execution_port is overrides.tool_execution_port
    assert ports.event_sink is overrides.event_sink


def test_invalid_override_fails_before_any_default_constructor(tmp_path):
    with (
        patch("naumi_agent.runtime.composition.SessionStore") as session_store,
        patch("naumi_agent.runtime.composition.PermissionChecker") as permission,
        pytest.raises(TypeError, match="event_sink 必须实现完整的 EventSink 契约"),
    ):
        build_runtime_ports(
            _config(tmp_path),
            overrides=RuntimePortOverrides(event_sink=object()),  # type: ignore[arg-type]
        )
    session_store.assert_not_called()
    permission.assert_not_called()


def test_default_construction_is_not_a_singleton(tmp_path):
    first = build_runtime_ports(_config(tmp_path))
    second = build_runtime_ports(_config(tmp_path))
    assert first.session_port is not second.session_port
    assert first.permission_port is not second.permission_port
    assert first.model_port is not second.model_port
    assert first.tool_execution_port is not second.tool_execution_port
    assert first.event_sink is not second.event_sink


class _FalseySink(NullEventSink):
    def __bool__(self) -> bool:
        return False


def test_falsey_event_override_is_not_replaced(tmp_path):
    sink = _FalseySink()
    ports = build_runtime_ports(
        _config(tmp_path),
        overrides=RuntimePortOverrides(event_sink=sink),
    )
    assert ports.event_sink is sink


def test_permission_paths_preserve_existing_stable_order(tmp_path):
    ports = build_runtime_ports(_config(tmp_path))
    assert ports.permission_port._allowed_dirs == [
        str((tmp_path / "explicit").resolve()),
        str(tmp_path.resolve()),
        str((tmp_path / ".naumi" / "worktrees").resolve()),
    ]


def test_create_agent_engine_injects_every_built_port(tmp_path):
    from naumi_agent.runtime.composition import create_agent_engine

    config = _config(tmp_path)
    ports = build_runtime_ports(config)
    sentinel = object()
    with (
        patch(
            "naumi_agent.runtime.composition.build_runtime_ports",
            return_value=ports,
        ),
        patch(
            "naumi_agent.orchestrator.engine.AgentEngine",
            return_value=sentinel,
        ) as engine_type,
    ):
        result = create_agent_engine(config)

    assert result is sentinel
    engine_type.assert_called_once_with(
        config,
        session_port=ports.session_port,
        permission_port=ports.permission_port,
        model_port=ports.model_port,
        tool_execution_port=ports.tool_execution_port,
        event_sink=ports.event_sink,
    )
```

- [ ] **Step 2: Run the tests and verify the missing composition module failure**

Run:

```bash
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q \
  tests/unit/test_runtime_composition.py
```

Expected: collection fails because `naumi_agent.runtime.composition` does not exist.

- [ ] **Step 3: Implement the root builder**

Create `src/naumi_agent/runtime/composition.py`:

```python
"""Authoritative production composition root for the Agent runtime."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from naumi_agent.config.settings import AppConfig
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.model.catalog import load_provider_catalog
from naumi_agent.model.router import ModelRouter
from naumi_agent.runtime.dependencies import (
    RuntimePortOverrides,
    RuntimePorts,
    validate_runtime_port_overrides,
)
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.streaming.sinks import NullEventSink
from naumi_agent.tools.execution import LocalToolExecutor

if TYPE_CHECKING:
    from naumi_agent.orchestrator.engine import AgentEngine


def build_runtime_ports(
    config: AppConfig,
    *,
    overrides: RuntimePortOverrides[Session] | None = None,
) -> RuntimePorts[Session]:
    resolved = RuntimePortOverrides[Session]() if overrides is None else overrides
    validate_runtime_port_overrides(resolved)

    workspace_root = config.resolve_workspace_root()
    runtime_data_dir = Path(config.memory.session_db_path).parent
    worktree_storage_dir = runtime_data_dir / "worktrees"

    session_port = resolved.session_port
    if session_port is None:
        session_port = SessionStore(config.memory)

    permission_port = resolved.permission_port
    if permission_port is None:
        permission_port = PermissionChecker(
            mode=PermissionMode(config.safety.permission_mode),
            allowed_dirs=[
                *config.safety.allowed_dirs,
                str(workspace_root),
                str(worktree_storage_dir),
            ],
            workspace_root=str(workspace_root),
        )

    model_port = resolved.model_port
    if model_port is None:
        catalog = (
            load_provider_catalog(config.models.catalog_path)
            if config.models.catalog_path
            else None
        )
        model_port = ModelRouter(config.models, catalog=catalog)

    tool_execution_port = resolved.tool_execution_port
    if tool_execution_port is None:
        tool_execution_port = LocalToolExecutor()

    event_sink = resolved.event_sink
    if event_sink is None:
        event_sink = NullEventSink()

    return RuntimePorts(
        session_port=session_port,
        permission_port=permission_port,
        model_port=model_port,
        tool_execution_port=tool_execution_port,
        event_sink=event_sink,
    )


def create_agent_engine(
    config: AppConfig,
    *,
    port_overrides: RuntimePortOverrides[Session] | None = None,
) -> AgentEngine:
    from naumi_agent.orchestrator.engine import AgentEngine

    ports = build_runtime_ports(config, overrides=port_overrides)
    return AgentEngine(
        config,
        session_port=ports.session_port,
        permission_port=ports.permission_port,
        model_port=ports.model_port,
        tool_execution_port=ports.tool_execution_port,
        event_sink=ports.event_sink,
    )


__all__ = ["build_runtime_ports", "create_agent_engine"]
```

This Task 2 factory deliberately uses the Engine's existing five explicit keywords so the commit is independently
runnable. Task 3 atomically changes the factory to `AgentEngine(config, ports=ports)` after the Engine accepts the
bundle. Do not create directories and do not catch catalog/permission/config errors. Keep `runtime/__init__.py` unchanged;
entrypoints import `naumi_agent.runtime.composition` explicitly so default adapters are never loaded as a side effect.

- [ ] **Step 4: Run focused tests and verify current Port contracts did not regress**

Run:

```bash
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q \
  tests/unit/test_runtime_dependencies.py \
  tests/unit/test_runtime_composition.py \
  tests/unit/test_session_port.py \
  tests/unit/test_permission_port.py \
  tests/unit/test_model_port.py \
  tests/unit/test_tool_execution_port.py \
  tests/unit/test_event_sink_port.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Self-review and commit the root builder**

Run:

```bash
/Users/lv/Workspace/NaumiAgent/.venv/bin/ruff check \
  src/naumi_agent/runtime/composition.py \
  tests/unit/test_runtime_composition.py
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m compileall -q \
  src/naumi_agent/runtime/composition.py
git diff --check
```

Commit:

```bash
git add src/naumi_agent/runtime/composition.py \
  tests/unit/test_runtime_composition.py
git commit -m "feat(runtime): add port composition root [ARC-01.4a]"
```

---

### Task 3: Make AgentEngine consume the Port bundle

**Files:**
- Create: `tests/unit/test_engine_port_bundle.py`
- Modify: `src/naumi_agent/orchestrator/engine.py:1-560`
- Modify: existing Port injection tests only where patch targets move to composition.

**Interfaces:**
- Consumes: `RuntimePorts[Session]`, `RuntimePortOverrides[Session]`, `build_runtime_ports()`.
- Produces: `AgentEngine(config, *, ports=...)`; legacy individual args delegate to the root without concrete imports.

- [ ] **Step 1: Write failing bundle, conflict, and delegation tests**

Create `tests/unit/test_engine_port_bundle.py`:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.runtime.composition import build_runtime_ports
from naumi_agent.streaming.sinks import NullEventSink


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
            vector_db_path=str(tmp_path / ".naumi" / "chroma"),
            long_term_enabled=False,
        ),
    )


@pytest.mark.asyncio
async def test_engine_consumes_one_complete_port_bundle(tmp_path):
    ports = build_runtime_ports(_config(tmp_path))
    engine = AgentEngine(_config(tmp_path), ports=ports)
    try:
        assert engine.session_store is ports.session_port
        assert engine._permission_checker is ports.permission_port
        assert engine.router is ports.model_port
        assert engine.tool_executor is ports.tool_execution_port
        assert engine.event_sink is ports.event_sink
    finally:
        await engine.shutdown()


def test_engine_rejects_bundle_plus_legacy_override(tmp_path):
    ports = build_runtime_ports(_config(tmp_path))
    with pytest.raises(TypeError, match="ports 与单独 Port 参数不能同时提供"):
        AgentEngine(_config(tmp_path), ports=ports, event_sink=NullEventSink())


@pytest.mark.asyncio
async def test_legacy_default_path_delegates_to_composition_builder(tmp_path):
    ports = build_runtime_ports(_config(tmp_path))
    with patch(
        "naumi_agent.runtime.composition.build_runtime_ports",
        return_value=ports,
    ) as build:
        engine = AgentEngine(_config(tmp_path))
    try:
        build.assert_called_once()
        assert engine.event_sink is ports.event_sink
    finally:
        await engine.shutdown()
```

The existing `test_engine_preserves_explicit_falsey_event_sink` remains the legacy explicit-override identity test.
Change `test_engine_uses_null_sink_by_default_and_keeps_legacy_emitter` to construct through
`create_agent_engine(_config(tmp_path))`. This removes one redundant legacy constructor while the new delegation test
adds one, keeping the legacy test-construction budget from increasing.

- [ ] **Step 2: Run the tests and verify `ports` is rejected by the current constructor**

Run:

```bash
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q \
  tests/unit/test_engine_port_bundle.py
```

Expected: fails with `TypeError: AgentEngine.__init__() got an unexpected keyword argument 'ports'`.

- [ ] **Step 3: Replace Engine adapter construction with bundle resolution**

Modify imports in `engine.py`:

- add `RuntimePortOverrides, RuntimePorts` from `naumi_agent.runtime.dependencies`;
- remove `SessionStore`, `load_provider_catalog`, `ModelRouter`, `PermissionChecker`, `LocalToolExecutor`, and `NullEventSink` imports;
- retain concrete `Session`, `ModelTier`, `TokenUsage`, `PermissionMode`, `PermissionOutcome`, `EventEmitter`, and runtime sink combinators actually used after construction.

At the start of `__init__`, implement:

```python
legacy_ports = (
    session_port,
    permission_port,
    model_port,
    tool_execution_port,
    event_sink,
)
if ports is not None and any(value is not None for value in legacy_ports):
    raise TypeError("ports 与单独 Port 参数不能同时提供")
if ports is None:
    from naumi_agent.runtime.composition import build_runtime_ports

    ports = build_runtime_ports(
        config,
        overrides=RuntimePortOverrides(
            session_port=session_port,
            permission_port=permission_port,
            model_port=model_port,
            tool_execution_port=tool_execution_port,
            event_sink=event_sink,
        ),
    )

self._event_sink = ports.event_sink
self._session_port = ports.session_port
self._permission_port = ports.permission_port
self._model_port = ports.model_port
self._tool_execution_port = ports.tool_execution_port
```

Delete the five old default-resolution blocks and their duplicate TypeError checks. Keep workspace/path calculation immediately after bundle assignment so later resource construction remains unchanged.

In `runtime/composition.py`, replace the five individual keyword arguments with the final bundle call:

```python
return AgentEngine(config, ports=ports)
```

Update `test_create_agent_engine_injects_every_built_port` to expect
`engine_type.assert_called_once_with(config, ports=ports)` in the same Task 3 commit.

- [ ] **Step 4: Move stale patch targets and run the focused Engine/Port tests**

Update tests that patch `naumi_agent.orchestrator.engine.load_provider_catalog` to patch
`naumi_agent.runtime.composition.load_provider_catalog`. Preserve the assertion that an injected model override does not load the catalog.

In `tests/unit/test_event_sink_injection.py`, import `create_agent_engine` and change only the default test body to:

```python
engine = create_agent_engine(_config(tmp_path))
try:
    assert isinstance(engine.event_sink, NullEventSink)
    assert isinstance(engine.emitter, EventEmitter)
finally:
    await engine.shutdown()
```

Run:

```bash
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q \
  tests/unit/test_engine_port_bundle.py \
  tests/unit/test_session_port.py \
  tests/unit/test_permission_port.py \
  tests/unit/test_model_port.py \
  tests/unit/test_tool_execution_port.py \
  tests/unit/test_event_sink_injection.py \
  tests/unit/test_engine_event_pipeline.py
```

Expected: all selected tests pass; explicit incomplete/falsey/default behavior remains green.

- [ ] **Step 5: Prove Engine no longer owns concrete Port adapters and commit**

Run:

```bash
rg -n "SessionStore|PermissionChecker|ModelRouter|LocalToolExecutor|NullEventSink|load_provider_catalog" \
  src/naumi_agent/orchestrator/engine.py
/Users/lv/Workspace/NaumiAgent/.venv/bin/ruff check \
  src/naumi_agent/orchestrator/engine.py \
  tests/unit/test_engine_port_bundle.py \
  tests/unit/test_session_port.py \
  tests/unit/test_model_port.py
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m compileall -q \
  src/naumi_agent/orchestrator/engine.py
git diff --check
```

Expected: the `rg` command has no matches; all checks exit 0.

Commit:

```bash
git add src/naumi_agent/orchestrator/engine.py \
  tests/unit/test_engine_port_bundle.py \
  tests/unit/test_session_port.py \
  tests/unit/test_model_port.py
git commit -m "refactor(runtime): inject composed port bundle [ARC-01.4a]"
```

---

### Task 4: Route all production entrypoints through the root

**Files:**
- Create: `tests/unit/test_runtime_composition_entrypoints.py`
- Modify: `src/naumi_agent/main.py:741-765,1487-1500,1608-1620`
- Modify: `src/naumi_agent/api/app.py:10-34`
- Modify: `src/naumi_agent/ui/bridge.py:2244-2260`
- Modify: `tests/unit/test_main_run.py`
- Modify: `tests/unit/test_ui_bridge.py`

**Interfaces:**
- Consumes: `create_agent_engine(config)` from Task 2.
- Produces: five product surfaces with no direct default AgentEngine construction.

- [ ] **Step 1: Write failing routing and lifecycle tests**

Create `tests/unit/test_runtime_composition_entrypoints.py` with source-routing assertions plus executable API lifespan coverage:

```python
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import naumi_agent.api.app as api_app


def test_product_sources_do_not_construct_agent_engine_directly():
    root = Path(__file__).resolve().parents[2] / "src" / "naumi_agent"
    for relative in ("main.py", "api/app.py"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "AgentEngine(config)" not in source


@pytest.mark.asyncio
async def test_api_lifespan_uses_root_and_shuts_down(monkeypatch, tmp_path):
    engine = SimpleNamespace(
        chat_run_store=object(),
        set_permission_confirmer=lambda _callback: None,
        shutdown=AsyncMock(),
    )
    config = SimpleNamespace()
    monkeypatch.setattr(api_app.AppConfig, "from_yaml", lambda _path: config)
    monkeypatch.setattr(api_app, "create_agent_engine", lambda value: engine)
    app = SimpleNamespace(state=SimpleNamespace())

    async with api_app.lifespan(app):
        assert app.state.engine is engine
        assert app.state.config is config

    engine.shutdown.assert_awaited_once()
```

Replace `tests/unit/test_main_run.py::_install_engine` with this focused fake root:

```python
def _install_engine(
    monkeypatch: pytest.MonkeyPatch,
    engine: object,
) -> dict[str, object]:
    captured: dict[str, object] = {}
    module = ModuleType("naumi_agent.runtime.composition")

    def create_agent_engine(config: object) -> object:
        captured["config"] = config
        return engine

    module.create_agent_engine = create_agent_engine  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(main_module, "_resolve_config_path", lambda path: path)
    parsed = SimpleNamespace(log_level="INFO")
    monkeypatch.setattr(main_module.AppConfig, "from_yaml", lambda _path: parsed)
    monkeypatch.setattr(main_module, "_check_api_key", lambda _config: None)
    captured["parsed"] = parsed
    return captured
```

In both run tests, retain the returned dict and assert
`captured["config"] is captured["parsed"]` after `_run_task()`.

Add this default Bridge root test next to the existing explicit factory test:

```python
@pytest.mark.asyncio
async def test_create_bridge_uses_composition_root_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from naumi_agent.runtime import composition

    config_path = tmp_path / "config.yaml"
    config_path.write_text("models:\n  default_model: test-model\n", encoding="utf-8")
    engine = _FakeEngine()
    captured: list[AppConfig] = []

    def create(config: AppConfig) -> _FakeEngine:
        captured.append(config)
        engine.workspace_root = config.resolve_workspace_root()
        return engine

    monkeypatch.setattr(composition, "create_agent_engine", create)
    monkeypatch.setenv("NAUMI_MODELS__API_KEY", "test-secret")
    bridge = await ui_bridge.create_bridge(config_path=str(config_path))
    bridge.bind_writer(io.StringIO())
    try:
        assert len(captured) == 1
        assert bridge.engine is engine
    finally:
        await bridge.shutdown()
```

In the existing explicit `engine_factory` test, install this default-root guard before calling `create_bridge`:

```python
from naumi_agent.runtime import composition

def forbidden_default_root(_config: AppConfig) -> _FakeEngine:
    raise AssertionError("显式 factory 不得调用默认 root")

monkeypatch.setattr(composition, "create_agent_engine", forbidden_default_root)
```

The existing explicit factory test must still pass, proving test injection is not wrapped or replaced.

- [ ] **Step 2: Run routing tests and verify they fail on current direct constructors**

Run:

```bash
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q \
  tests/unit/test_runtime_composition_entrypoints.py \
  tests/unit/test_main_run.py \
  tests/unit/test_ui_bridge.py
```

Expected: source assertion or monkeypatch route fails because production entrypoints still import/call AgentEngine.

- [ ] **Step 3: Migrate main, API, and Bridge defaults**

For each of `_launch_tui`, `_chat`, and `_run_task` in `main.py`, replace the local Engine import with:

```python
from naumi_agent.runtime.composition import create_agent_engine
```

and replace `AgentEngine(config)` with `create_agent_engine(config)`.

In `api/app.py`, import `create_agent_engine` and replace the lifespan construction. Keep permission broker setup,
state assignment, yield, broker close, and engine shutdown unchanged.

In `ui/bridge.py`, preserve the explicit factory branch and replace only the default:

```python
if engine_factory is None:
    from naumi_agent.runtime.composition import create_agent_engine

    engine_factory = create_agent_engine
engine = engine_factory(config)
```

- [ ] **Step 4: Run focused product-surface tests**

Run:

```bash
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q \
  tests/unit/test_runtime_composition_entrypoints.py \
  tests/unit/test_main_run.py \
  tests/unit/test_ui_bridge.py \
  tests/unit/test_api.py::TestHealthEndpoint::test_health_check
```

Expected: all selected tests pass. Do not run full `test_api.py` or full TUI tests.

- [ ] **Step 5: Self-review direct-construction counts and commit**

Run:

```bash
rg -n "AgentEngine\(config\)" src/naumi_agent --glob '*.py'
rg -n "engine_factory = AgentEngine" src/naumi_agent/ui/bridge.py
/Users/lv/Workspace/NaumiAgent/.venv/bin/ruff check \
  src/naumi_agent/main.py src/naumi_agent/api/app.py src/naumi_agent/ui/bridge.py \
  tests/unit/test_runtime_composition_entrypoints.py tests/unit/test_main_run.py \
  tests/unit/test_ui_bridge.py
git diff --check
```

Expected: both `rg` commands have no matches; checks exit 0.

Commit:

```bash
git add src/naumi_agent/main.py src/naumi_agent/api/app.py src/naumi_agent/ui/bridge.py \
  tests/unit/test_runtime_composition_entrypoints.py tests/unit/test_main_run.py \
  tests/unit/test_ui_bridge.py
git commit -m "refactor(runtime): route entrypoints through composition [ARC-01.4a]"
```

---

### Task 5: Prove a real root-composed streaming run and lock architecture budgets

**Files:**
- Create: `tests/integration/test_runtime_composition_streaming.py`
- Create: `tests/unit/test_architecture_runtime_composition.py`
- Modify: `docs/development/architecture/ARC-01-4a-runtime-ports-composition-design.md`
- Modify: `docs/development/architecture/ARC-01-domain-boundaries.md`

**Interfaces:**
- Consumes: completed RuntimePorts bundle, root factory, AgentEngine streaming/event/session contracts.
- Produces: real execution evidence, static ownership gates, final 4a audit record.

- [ ] **Step 1: Write the real streaming acceptance test**

Create `tests/integration/test_runtime_composition_streaming.py` with a real SQLite SessionStore, real bypass
PermissionChecker, real LocalToolExecutor, actual `file_read` tool execution, a ModelRouter whose network stream is
replaced by a deterministic two-turn stream, and one recording base EventSink:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from naumi_agent.config.settings import (
    AppConfig,
    MemoryConfig,
    ModelConfig,
    SafetyConfig,
)
from naumi_agent.memory.session import SessionStore
from naumi_agent.model.router import ModelRouter, StreamChunk, TokenUsage
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.runtime.dependencies import RuntimePortOverrides
from naumi_agent.runtime.ports.events import RuntimeEvent, RuntimeEventType
from naumi_agent.streaming.sinks import NullEventSink


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    async def emit(self, event: RuntimeEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_root_composed_engine_runs_tool_persists_receipt_and_closes_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readable = tmp_path / "proof.txt"
    readable.write_text("composition-root-proof", encoding="utf-8")
    config = AppConfig(
        workspace_root=str(tmp_path),
        models=ModelConfig(),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
            vector_db_path=str(tmp_path / ".naumi" / "chroma"),
            long_term_enabled=False,
        ),
        safety=SafetyConfig(
            permission_mode="bypass",
            allowed_dirs=[str(tmp_path)],
        ),
    )
    model = ModelRouter(config.models)
    events = _RecordingSink()
    engine = create_agent_engine(
        config,
        port_overrides=RuntimePortOverrides(
            model_port=model,
            event_sink=events,
        ),
    )
    assert isinstance(engine.session_store, SessionStore)
    call_count = 0

    async def stream_response(**_: object):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield StreamChunk(
                tool_call={
                    0: {
                        "id": "read-proof",
                        "type": "function",
                        "function": {
                            "name": "file_read",
                            "arguments": json.dumps({"path": str(readable)}),
                        },
                    }
                },
                finish_reason="tool_calls",
            )
            return
        yield StreamChunk(token="Composition Root 流式完成")
        yield StreamChunk(
            finish_reason="stop",
            usage=TokenUsage(
                input_tokens=5,
                output_tokens=6,
                total_tokens=11,
                cost_usd=0.001,
            ),
        )

    monkeypatch.setattr(model, "stream", stream_response)
    try:
        result = await engine.run_streaming(
            "读取 proof.txt 后确认结果",
            NullEventSink(),
        )

        assert result.status == "completed"
        assert result.response == "Composition Root 流式完成"
        assert result.receipt is not None
        assert call_count == 2
        assert engine._session is not None
        saved = await engine.session_store.load(engine._session.id)
        assert saved is not None
        assert any(
            message.get("role") == "tool"
            and "composition-root-proof" in str(message.get("content", ""))
            for message in saved.messages
        )
        assert [event.sequence for event in events.events] == list(
            range(1, len(events.events) + 1)
        )
        assert any(
            event.type is RuntimeEventType.TOOL_START
            for event in events.events
        )
        assert any(
            event.type is RuntimeEventType.TOOL_END
            for event in events.events
        )
        assert sum(
            event.type is RuntimeEventType.COMPLETION_RECEIPT
            for event in events.events
        ) == 1
        assert engine.session_store._db is not None
    finally:
        await engine.shutdown()

    assert engine.session_store._db is None
```

The caller sink is deliberately a separate `NullEventSink`, so the injected base sink observes each authoritative
event once and sequence assertions are not distorted by fan-out duplication.

- [ ] **Step 2: Write AST/static architecture tests**

Create `tests/unit/test_architecture_runtime_composition.py` using `ast.parse` instead of fragile raw substring
checks:

```python
from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src" / "naumi_agent"
TEST_ROOT = PROJECT_ROOT / "tests"
ENGINE_PATH = SOURCE_ROOT / "orchestrator" / "engine.py"
DEPENDENCIES_PATH = SOURCE_ROOT / "runtime" / "dependencies.py"
COMPOSITION_PATH = SOURCE_ROOT / "runtime" / "composition.py"
_BANNED_ENGINE_NAMES = {
    "LocalToolExecutor",
    "ModelRouter",
    "NullEventSink",
    "PermissionChecker",
    "SessionStore",
    "load_provider_catalog",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_names(path: Path) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            imported.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(alias.asname or alias.name for alias in node.names)
    return imported


def _agent_engine_calls(root: Path) -> list[tuple[Path, ast.Call]]:
    calls: list[tuple[Path, ast.Call]] = []
    for path in root.rglob("*.py"):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AgentEngine"
            ):
                calls.append((path, node))
    return calls


def test_engine_imports_and_constructs_no_default_port_adapter() -> None:
    assert _imported_names(ENGINE_PATH).isdisjoint(_BANNED_ENGINE_NAMES)
    called = {
        node.func.id
        for node in ast.walk(_tree(ENGINE_PATH))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called.isdisjoint(_BANNED_ENGINE_NAMES)


def test_dependency_bundle_has_protocol_only_imports() -> None:
    allowed = {
        "__future__",
        "dataclasses",
        "naumi_agent.runtime.ports.events",
        "naumi_agent.runtime.ports.model",
        "naumi_agent.runtime.ports.permission",
        "naumi_agent.runtime.ports.session",
        "naumi_agent.runtime.ports.tool_execution",
    }
    modules = {
        node.module
        for node in ast.walk(_tree(DEPENDENCIES_PATH))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert modules <= allowed


def test_only_composition_root_constructs_agent_engine_in_product_source() -> None:
    calls = _agent_engine_calls(SOURCE_ROOT)
    assert [path for path, _ in calls] == [COMPOSITION_PATH]
    assert all(any(keyword.arg == "ports" for keyword in call.keywords) for _, call in calls)


def test_bridge_default_factory_does_not_assign_agent_engine() -> None:
    bridge = _tree(SOURCE_ROOT / "ui" / "bridge.py")
    assignments = [
        node
        for node in ast.walk(bridge)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, ast.Name)
        and node.value.id == "AgentEngine"
    ]
    assert assignments == []


def test_legacy_test_constructor_budget_never_exceeds_arc_01_4a_baseline() -> None:
    legacy = [
        (path, call)
        for path, call in _agent_engine_calls(TEST_ROOT)
        if not any(keyword.arg == "ports" for keyword in call.keywords)
    ]
    assert len(legacy) <= 171
```

This gate distinguishes new explicit `ports=` bundle tests from legacy default/individual constructors. The current
authoritative legacy baseline is 171; Task 3 replaces one redundant default test with the root as it adds one focused
delegation test, so the budget stays flat or decreases.

- [ ] **Step 3: Run red/green acceptance and architecture checks**

Run the two tests before adding any missing implementation; expected failure must identify an actual uncovered route
or contract, not a syntax error. Fix only the 4a behavior required by those failures, then rerun:

```bash
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q \
  tests/integration/test_runtime_composition_streaming.py \
  tests/unit/test_architecture_runtime_composition.py
```

Expected final result: all selected tests pass.

- [ ] **Step 4: Run the complete ARC-01.4a focused verification matrix**

Run in separate small groups so a failure identifies its domain:

```bash
PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q \
  tests/unit/test_runtime_dependencies.py \
  tests/unit/test_runtime_composition.py \
  tests/unit/test_engine_port_bundle.py

PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q \
  tests/unit/test_session_port.py \
  tests/unit/test_permission_port.py \
  tests/unit/test_model_port.py \
  tests/unit/test_tool_execution_port.py \
  tests/unit/test_event_sink_port.py \
  tests/unit/test_event_sink_injection.py \
  tests/unit/test_engine_event_pipeline.py

PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q \
  tests/unit/test_runtime_composition_entrypoints.py \
  tests/unit/test_main_run.py \
  tests/unit/test_ui_bridge.py \
  tests/unit/test_api.py::TestHealthEndpoint::test_health_check

PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest -q \
  tests/integration/test_runtime_composition_streaming.py \
  tests/unit/test_architecture_runtime_composition.py \
  tests/unit/test_architecture_import_graph.py
```

Then run:

```bash
/Users/lv/Workspace/NaumiAgent/.venv/bin/ruff check \
  src/naumi_agent/runtime/dependencies.py \
  src/naumi_agent/runtime/composition.py \
  src/naumi_agent/orchestrator/engine.py \
  src/naumi_agent/main.py \
  src/naumi_agent/api/app.py \
  src/naumi_agent/ui/bridge.py \
  tests/unit/test_runtime_dependencies.py \
  tests/unit/test_runtime_composition.py \
  tests/unit/test_engine_port_bundle.py \
  tests/unit/test_runtime_composition_entrypoints.py \
  tests/unit/test_architecture_runtime_composition.py \
  tests/integration/test_runtime_composition_streaming.py

PYTHONPATH=src /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m compileall -q \
  src/naumi_agent/runtime \
  src/naumi_agent/orchestrator/engine.py \
  src/naumi_agent/api/app.py \
  src/naumi_agent/ui/bridge.py
```

Expected: every command exits 0. Record exact pass counts in the design audit; do not report a total based on
arithmetic without reading each command result.

- [ ] **Step 5: Update documentation with authoritative audit evidence**

Append to `ARC-01-4a-runtime-ports-composition-design.md`:

- final commit-before-doc hash;
- exact production constructor/factory counts;
- final test direct-construction count;
- each focused pytest command and pass count;
- Ruff/compile result;
- real streaming response, receipt count, session close count;
- any honest remaining limitation, especially the temporary legacy Engine path and un-migrated Store/Service work.

Update `ARC-01-domain-boundaries.md` status to:

```text
ARC-01.4 Composition root | 4a 已实现，4b 待开发
```

Link the overall design, 4a design, implementation plan, and final audit section.

- [ ] **Step 6: Self-review, commit acceptance evidence, and stop before 4b**

Run:

```bash
rg -n "TB[D]|TO[D]O|implement[ ]later|fill[ ]in[ ]details" \
  docs/development/architecture/ARC-01-4*.md
git diff --check
git status --short
```

Expected: no placeholder matches, no whitespace errors, and only Task 5 files are pending.

Commit:

```bash
git add tests/integration/test_runtime_composition_streaming.py \
  tests/unit/test_architecture_runtime_composition.py \
  docs/development/architecture/ARC-01-4a-runtime-ports-composition-design.md \
  docs/development/architecture/ARC-01-domain-boundaries.md
git commit -m "test(runtime): accept ARC-01.4a composition root"
```

Do not begin ARC-01.4b in this commit. First inspect `git log`, rerun the smallest real streaming and architecture
tests from the committed tree, and review the complete diff from the design commit.

---

## Final Review Checklist

- [ ] Exactly one production default Port builder exists.
- [ ] Runtime dependency types import no concrete adapter or AgentEngine.
- [ ] AgentEngine imports and constructs no default Port adapter.
- [ ] Full bundle and legacy individual overrides preserve identity.
- [ ] Bundle plus individual override is rejected explicitly.
- [ ] Invalid override fails before any default construction.
- [ ] Falsey complete override is never replaced.
- [ ] TUI, fallback CLI, run, API, and New UI Bridge use the root.
- [ ] Explicit Bridge factory override bypasses the default root exactly as requested.
- [ ] A real streaming run reaches session persistence, model stream, events, receipt, and shutdown.
- [ ] Direct production constructor count is zero; test count does not exceed 171.
- [ ] Focused pytest, Ruff, compileall, and architecture checks are fresh and passing.
- [ ] Documentation states that 4b/4c/4d remain incomplete; ARC-01.4 overall is not falsely marked complete.
