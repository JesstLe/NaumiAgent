"""Authoritative production composition root for the Agent runtime."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from naumi_agent.config.settings import AppConfig
from naumi_agent.evolution.store import (
    EvolutionCandidateStore,
    resolve_evolution_db_path,
)
from naumi_agent.harness.store import HarnessStore, resolve_harness_db_path
from naumi_agent.harness.trust import (
    HarnessTrustStore,
    resolve_harness_trust_db_path,
)
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.model.catalog import load_provider_catalog
from naumi_agent.model.router import ModelRouter
from naumi_agent.runs.store import ChatRunStore
from naumi_agent.runtime.dependencies import (
    RuntimePortOverrides,
    RuntimePorts,
    validate_runtime_port_overrides,
)
from naumi_agent.runtime.paths import RuntimePaths
from naumi_agent.runtime.resources import (
    RuntimeResourceOverrides,
    RuntimeResources,
    validate_runtime_resource_overrides,
)
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.streaming.sinks import NullEventSink
from naumi_agent.tasks.store import TaskStore
from naumi_agent.tools.execution import LocalToolExecutor
from naumi_agent.workbench.store import WorkbenchStore

if TYPE_CHECKING:
    from naumi_agent.orchestrator.engine import AgentEngine


def build_runtime_ports(
    config: AppConfig,
    *,
    paths: RuntimePaths | None = None,
    overrides: RuntimePortOverrides[Session] | None = None,
) -> RuntimePorts[Session]:
    """Build one independent, fully validated Runtime Port bundle."""
    resolved = RuntimePortOverrides[Session]() if overrides is None else overrides
    validate_runtime_port_overrides(resolved)

    if paths is not None and not isinstance(paths, RuntimePaths):
        raise TypeError("paths 必须是完整的 RuntimePaths。")
    resolved_paths = build_runtime_paths(config) if paths is None else paths

    session_port = resolved.session_port
    if session_port is None:
        session_port = SessionStore(config.memory)

    permission_port = resolved.permission_port
    if permission_port is None:
        permission_port = PermissionChecker(
            mode=PermissionMode(config.safety.permission_mode),
            allowed_dirs=[
                *config.safety.allowed_dirs,
                str(resolved_paths.workspace_root),
                str(resolved_paths.worktree_storage_dir),
            ],
            workspace_root=str(resolved_paths.workspace_root),
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


def build_runtime_paths(config: AppConfig) -> RuntimePaths:
    """Resolve the complete runtime path snapshot without creating directories."""
    session_db_path = Path(config.memory.session_db_path).expanduser().resolve()
    runtime_data_dir = session_db_path.parent
    return RuntimePaths(
        workspace_root=config.resolve_workspace_root(),
        session_db_path=session_db_path,
        runtime_data_dir=runtime_data_dir,
        chat_run_db_path=runtime_data_dir / "chat-runs.db",
        worktree_storage_dir=runtime_data_dir / "worktrees",
        harness_db_path=resolve_harness_db_path(),
        harness_trust_db_path=resolve_harness_trust_db_path(),
        evolution_db_path=resolve_evolution_db_path(),
        browser_data_dir=runtime_data_dir / "browser",
        browser_daemon_log_dir=runtime_data_dir / "browser-daemon",
    )


def build_runtime_resources(
    paths: RuntimePaths,
    *,
    overrides: RuntimeResourceOverrides | None = None,
) -> RuntimeResources:
    """Build Harness resources without opening databases or creating directories."""
    if not isinstance(paths, RuntimePaths):
        raise TypeError("paths 必须是完整的 RuntimePaths。")
    resolved = RuntimeResourceOverrides() if overrides is None else overrides
    if not isinstance(resolved, RuntimeResourceOverrides):
        raise TypeError("overrides 必须是 RuntimeResourceOverrides。")
    validate_runtime_resource_overrides(resolved)

    chat_run_store = resolved.chat_run_store
    if chat_run_store is None:
        chat_run_store = ChatRunStore(paths.chat_run_db_path)

    evolution_candidate_store = resolved.evolution_candidate_store
    if evolution_candidate_store is None:
        evolution_candidate_store = EvolutionCandidateStore(paths.evolution_db_path)

    harness_store = resolved.harness_store
    if harness_store is None:
        harness_store = HarnessStore(paths.harness_db_path)

    harness_trust_store = resolved.harness_trust_store
    if harness_trust_store is None:
        harness_trust_store = HarnessTrustStore(paths.harness_trust_db_path)

    task_store = resolved.task_store
    if task_store is None:
        task_store = TaskStore(str(paths.session_db_path))

    workbench_store = resolved.workbench_store
    if workbench_store is None:
        workbench_store = WorkbenchStore(str(paths.session_db_path))

    return RuntimeResources(
        chat_run_store=chat_run_store,
        evolution_candidate_store=evolution_candidate_store,
        harness_store=harness_store,
        harness_trust_store=harness_trust_store,
        task_store=task_store,
        workbench_store=workbench_store,
    )


def create_agent_engine(
    config: AppConfig,
    *,
    port_overrides: RuntimePortOverrides[Session] | None = None,
    resource_overrides: RuntimeResourceOverrides | None = None,
) -> AgentEngine:
    """Create one Engine from the authoritative default Port composition."""
    from naumi_agent.orchestrator.engine import AgentEngine

    paths = build_runtime_paths(config)
    ports = build_runtime_ports(config, paths=paths, overrides=port_overrides)
    resources = build_runtime_resources(paths, overrides=resource_overrides)
    return AgentEngine(config, ports=ports, paths=paths, resources=resources)


__all__ = [
    "build_runtime_paths",
    "build_runtime_ports",
    "build_runtime_resources",
    "create_agent_engine",
]
