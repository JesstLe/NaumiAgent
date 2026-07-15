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
    """Build one independent, fully validated Runtime Port bundle."""
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
    """Create one Engine from the authoritative default Port composition."""
    from naumi_agent.orchestrator.engine import AgentEngine

    ports = build_runtime_ports(config, overrides=port_overrides)
    return AgentEngine(config, ports=ports)


__all__ = ["build_runtime_ports", "create_agent_engine"]
