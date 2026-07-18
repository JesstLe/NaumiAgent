from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from naumi_agent.config.settings import (
    AppConfig,
    MemoryConfig,
    ModelConfig,
    SafetyConfig,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.memory.session import SessionStore
from naumi_agent.model.router import ModelRouter
from naumi_agent.runtime.composition import (
    build_runtime_paths,
    build_runtime_ports,
    build_runtime_resources,
    create_agent_engine,
)
from naumi_agent.runtime.dependencies import RuntimePortOverrides
from naumi_agent.runtime.paths import RuntimePaths
from naumi_agent.runtime.resources import RuntimeResourceOverrides, RuntimeResources
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.streaming.sinks import NullEventSink
from naumi_agent.tools.execution import LocalToolExecutor


class _FalseySink(NullEventSink):
    def __bool__(self) -> bool:
        return False


class _FalseyHarnessStore(HarnessStore):
    def __bool__(self) -> bool:
        return False


def _config(tmp_path: Path, *, catalog_path: str | None = None) -> AppConfig:
    return AppConfig(
        workspace_root=str(tmp_path),
        models=ModelConfig(catalog_path=catalog_path),
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


def test_build_runtime_ports_selects_all_production_defaults(tmp_path: Path) -> None:
    ports = build_runtime_ports(_config(tmp_path))

    assert isinstance(ports.session_port, SessionStore)
    assert isinstance(ports.permission_port, PermissionChecker)
    assert ports.permission_port.mode is PermissionMode.BYPASS
    assert isinstance(ports.model_port, ModelRouter)
    assert isinstance(ports.tool_execution_port, LocalToolExecutor)
    assert isinstance(ports.event_sink, NullEventSink)


def test_build_runtime_paths_resolves_one_absolute_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_home = tmp_path / "user-state"
    monkeypatch.setenv("NAUMI_STATE_HOME", str(state_home))

    paths = build_runtime_paths(_config(tmp_path))

    assert all(
        isinstance(getattr(paths, name), Path) and getattr(paths, name).is_absolute()
        for name in paths.__slots__
    )
    assert paths.workspace_root == tmp_path.resolve()
    assert paths.runtime_data_dir == (tmp_path / ".naumi").resolve()
    assert paths.worktree_storage_dir == paths.runtime_data_dir / "worktrees"
    assert paths.harness_db_path == state_home.resolve() / "harness.db"
    assert paths.harness_trust_db_path == state_home.resolve() / "harness-trust.db"
    assert not state_home.exists()


def test_runtime_paths_reject_relative_or_escaped_owned_paths(tmp_path: Path) -> None:
    absolute = tmp_path.resolve()
    values = {
        "workspace_root": absolute,
        "runtime_data_dir": absolute / "data",
        "worktree_storage_dir": absolute / "data" / "worktrees",
        "harness_db_path": absolute / "state" / "harness.db",
        "harness_trust_db_path": absolute / "state" / "harness-trust.db",
        "browser_data_dir": absolute / "data" / "browser",
        "browser_daemon_log_dir": absolute / "data" / "browser-daemon",
    }

    with pytest.raises(TypeError, match="workspace_root 必须是绝对 Path"):
        RuntimePaths(**{**values, "workspace_root": Path("relative")})
    with pytest.raises(ValueError, match="workspace_root 必须是已规范化"):
        RuntimePaths(
            **{
                **values,
                "workspace_root": absolute / "nested" / "..",
            }
        )
    with pytest.raises(ValueError, match="browser_data_dir 必须位于"):
        RuntimePaths(**{**values, "browser_data_dir": absolute / "outside"})


def test_build_runtime_ports_rejects_invalid_paths_before_defaults(
    tmp_path: Path,
) -> None:
    with (
        patch("naumi_agent.runtime.composition.SessionStore") as session_store,
        pytest.raises(TypeError, match="paths 必须是完整的 RuntimePaths"),
    ):
        build_runtime_ports(_config(tmp_path), paths=object())  # type: ignore[arg-type]

    session_store.assert_not_called()


def test_build_runtime_resources_selects_paths_and_preserves_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state"))
    paths = build_runtime_paths(_config(tmp_path))

    defaults = build_runtime_resources(paths)
    falsey_store = _FalseyHarnessStore(tmp_path / "custom-harness.db")
    trust_store = HarnessTrustStore(tmp_path / "custom-trust.db")
    overridden = build_runtime_resources(
        paths,
        overrides=RuntimeResourceOverrides(
            harness_store=falsey_store,
            harness_trust_store=trust_store,
        ),
    )

    assert defaults.harness_store.db_path == paths.harness_db_path
    assert defaults.harness_trust_store._db_path == paths.harness_trust_db_path
    assert overridden.harness_store is falsey_store
    assert overridden.harness_trust_store is trust_store
    assert not (tmp_path / "state").exists()


def test_invalid_resource_override_fails_before_default_constructor(
    tmp_path: Path,
) -> None:
    paths = build_runtime_paths(_config(tmp_path))
    with (
        patch("naumi_agent.runtime.composition.HarnessStore") as harness_store,
        pytest.raises(TypeError, match="harness_trust_store 必须是"),
    ):
        build_runtime_resources(
            paths,
            overrides=RuntimeResourceOverrides(
                harness_trust_store=object(),  # type: ignore[arg-type]
            ),
        )

    harness_store.assert_not_called()


def test_runtime_resources_reject_incomplete_bundle(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="harness_store 必须是"):
        RuntimeResources(
            harness_store=object(),  # type: ignore[arg-type]
            harness_trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        )


def test_build_runtime_ports_preserves_every_override_identity(
    tmp_path: Path,
) -> None:
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


def test_invalid_override_fails_before_any_default_constructor(
    tmp_path: Path,
) -> None:
    with (
        patch("naumi_agent.runtime.composition.SessionStore") as session_store,
        patch("naumi_agent.runtime.composition.PermissionChecker") as permission,
        pytest.raises(
            TypeError,
            match="event_sink 必须实现完整的 EventSink 契约",
        ),
    ):
        build_runtime_ports(
            _config(tmp_path),
            overrides=RuntimePortOverrides(
                event_sink=object(),  # type: ignore[arg-type]
            ),
        )

    session_store.assert_not_called()
    permission.assert_not_called()


def test_default_construction_is_not_a_singleton(tmp_path: Path) -> None:
    first = build_runtime_ports(_config(tmp_path))
    second = build_runtime_ports(_config(tmp_path))

    assert first.session_port is not second.session_port
    assert first.permission_port is not second.permission_port
    assert first.model_port is not second.model_port
    assert first.tool_execution_port is not second.tool_execution_port
    assert first.event_sink is not second.event_sink


def test_falsey_event_override_is_not_replaced(tmp_path: Path) -> None:
    sink = _FalseySink()

    ports = build_runtime_ports(
        _config(tmp_path),
        overrides=RuntimePortOverrides(event_sink=sink),
    )

    assert ports.event_sink is sink


def test_permission_paths_preserve_existing_stable_order(tmp_path: Path) -> None:
    ports = build_runtime_ports(_config(tmp_path))

    assert ports.permission_port._allowed_dirs == [
        str((tmp_path / "explicit").resolve()),
        str(tmp_path.resolve()),
        str((tmp_path / ".naumi" / "worktrees").resolve()),
    ]


def test_explicit_catalog_is_loaded_once_and_passed_to_model_router(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, catalog_path=str(tmp_path / "catalog.yaml"))
    catalog = object()
    router = ModelRouter(config.models)

    with (
        patch(
            "naumi_agent.runtime.composition.load_provider_catalog",
            return_value=catalog,
        ) as load_catalog,
        patch(
            "naumi_agent.runtime.composition.ModelRouter",
            return_value=router,
        ) as router_type,
    ):
        ports = build_runtime_ports(config)

    load_catalog.assert_called_once_with(config.models.catalog_path)
    router_type.assert_called_once_with(config.models, catalog=catalog)
    assert ports.model_port is router


def test_create_agent_engine_injects_every_built_port(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ports = build_runtime_ports(config)
    resources = build_runtime_resources(build_runtime_paths(config))
    sentinel = object()

    with (
        patch(
            "naumi_agent.runtime.composition.build_runtime_paths",
            return_value=build_runtime_paths(config),
        ) as build_paths,
        patch(
            "naumi_agent.runtime.composition.build_runtime_ports",
            return_value=ports,
        ) as build_ports,
        patch(
            "naumi_agent.runtime.composition.build_runtime_resources",
            return_value=resources,
        ) as build_resources,
        patch(
            "naumi_agent.orchestrator.engine.AgentEngine",
            return_value=sentinel,
        ) as engine_type,
    ):
        result = create_agent_engine(config)

    assert result is sentinel
    paths = build_paths.return_value
    build_ports.assert_called_once_with(config, paths=paths, overrides=None)
    build_resources.assert_called_once_with(paths, overrides=None)
    engine_type.assert_called_once_with(
        config,
        ports=ports,
        paths=paths,
        resources=resources,
    )
