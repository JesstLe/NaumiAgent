"""Authoritative production composition root for the Agent runtime."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from naumi_agent import __version__
from naumi_agent.config.settings import AppConfig
from naumi_agent.daemons.agent_jobs import AgentJobStore
from naumi_agent.daemons.agent_worker_process import AgentWorkerProcessFactory
from naumi_agent.daemons.agent_worker_supervisor import AgentWorkerSupervisorFactory
from naumi_agent.daemons.execution_grants import ExecutionGrantStore
from naumi_agent.daemons.permission_decisions import PermissionDecisionReceiptStore
from naumi_agent.daemons.run_delegation_grants import RunDelegationGrantStore
from naumi_agent.daemons.tool_jobs import ToolJobStore
from naumi_agent.daemons.worker_registry import WorkerRegistryStore
from naumi_agent.evolution.stable_remote_finalization_http_transport import (
    MTLSStableRemoteFinalizationInstallationTransport,
    StableRemoteFinalizationHTTPClientPolicy,
)
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
from naumi_agent.orchestrator.goal_store import GoalStore
from naumi_agent.orchestrator.pursuit_store import PursuitStore
from naumi_agent.release.runtime_identity import discover_runtime_identity
from naumi_agent.runs.store import ChatRunStore
from naumi_agent.runtime.agent_heartbeat import AgentExecutionHeartbeatFactory
from naumi_agent.runtime.browser_heartbeat import BrowserExecutionHeartbeatFactory
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
from naumi_agent.runtime.services import RuntimeServiceOverrides, RuntimeServices
from naumi_agent.runtime.terminal_events import TerminalEventJournalStore
from naumi_agent.runtime.terminal_runtime import TerminalRuntimeLifecycleFactory
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
        terminal_event_db_path=runtime_data_dir / "terminal-events.db",
        worker_registry_db_path=runtime_data_dir / "worker-registry.db",
        execution_grant_db_path=runtime_data_dir / "execution-grants.db",
        run_delegation_grant_db_path=(
            runtime_data_dir / "run-delegation-grants.db"
        ),
        permission_decision_db_path=runtime_data_dir / "permission-decisions.db",
        tool_job_db_path=runtime_data_dir / "tool-jobs.db",
        agent_job_db_path=runtime_data_dir / "agent-jobs.db",
        agent_worker_runtime_dir=runtime_data_dir / "agent-worker" / "transport",
        shell_worker_runtime_dir=runtime_data_dir / "shell-worker" / "transport",
        shell_worker_sandbox_dir=runtime_data_dir / "shell-worker" / "sandboxes",
        shell_worker_artifact_dir=runtime_data_dir / "shell-worker" / "artifacts",
        worktree_storage_dir=runtime_data_dir / "worktrees",
        goal_storage_dir=runtime_data_dir / "goals",
        pursuit_storage_dir=runtime_data_dir / "pursuit",
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
    """Build typed runtime resources without opening databases or starting work."""
    if not isinstance(paths, RuntimePaths):
        raise TypeError("paths 必须是完整的 RuntimePaths。")
    resolved = RuntimeResourceOverrides() if overrides is None else overrides
    if not isinstance(resolved, RuntimeResourceOverrides):
        raise TypeError("overrides 必须是 RuntimeResourceOverrides。")
    validate_runtime_resource_overrides(resolved)
    if (
        resolved.terminal_event_store is not None
        and Path(resolved.terminal_event_store.workspace_root)
        != paths.workspace_root
    ):
        raise ValueError(
            "terminal_event_store workspace_root 必须与 RuntimePaths 一致。"
        )

    chat_run_store = resolved.chat_run_store
    if chat_run_store is None:
        chat_run_store = ChatRunStore(paths.chat_run_db_path)

    terminal_event_store = resolved.terminal_event_store
    if terminal_event_store is None:
        terminal_event_store = TerminalEventJournalStore(
            paths.terminal_event_db_path,
            workspace_root=paths.workspace_root,
        )

    worker_registry_store = resolved.worker_registry_store
    if worker_registry_store is None:
        worker_registry_store = WorkerRegistryStore(paths.worker_registry_db_path)

    execution_grant_store = resolved.execution_grant_store
    if execution_grant_store is None:
        execution_grant_store = ExecutionGrantStore(paths.execution_grant_db_path)

    run_delegation_grant_store = resolved.run_delegation_grant_store
    if run_delegation_grant_store is None:
        run_delegation_grant_store = RunDelegationGrantStore(
            paths.run_delegation_grant_db_path
        )

    permission_decision_store = resolved.permission_decision_store
    if permission_decision_store is None:
        permission_decision_store = PermissionDecisionReceiptStore(
            paths.permission_decision_db_path
        )

    tool_job_store = resolved.tool_job_store
    if tool_job_store is None:
        tool_job_store = ToolJobStore(paths.tool_job_db_path)

    agent_job_store = resolved.agent_job_store
    if agent_job_store is None:
        agent_job_store = AgentJobStore(paths.agent_job_db_path)

    evolution_candidate_store = resolved.evolution_candidate_store
    if evolution_candidate_store is None:
        evolution_candidate_store = EvolutionCandidateStore(paths.evolution_db_path)

    harness_store = resolved.harness_store
    if harness_store is None:
        harness_store = HarnessStore(paths.harness_db_path)

    harness_trust_store = resolved.harness_trust_store
    if harness_trust_store is None:
        harness_trust_store = HarnessTrustStore(paths.harness_trust_db_path)

    goal_store = resolved.goal_store
    if goal_store is None:
        goal_store = GoalStore(paths.goal_storage_dir)

    pursuit_store = resolved.pursuit_store
    if pursuit_store is None:
        pursuit_store = PursuitStore(paths.pursuit_storage_dir)

    task_store = resolved.task_store
    if task_store is None:
        task_store = TaskStore(str(paths.session_db_path))

    workbench_store = resolved.workbench_store
    if workbench_store is None:
        workbench_store = WorkbenchStore(str(paths.session_db_path))

    return RuntimeResources(
        chat_run_store=chat_run_store,
        terminal_event_store=terminal_event_store,
        worker_registry_store=worker_registry_store,
        execution_grant_store=execution_grant_store,
        run_delegation_grant_store=run_delegation_grant_store,
        permission_decision_store=permission_decision_store,
        tool_job_store=tool_job_store,
        agent_job_store=agent_job_store,
        evolution_candidate_store=evolution_candidate_store,
        harness_store=harness_store,
        harness_trust_store=harness_trust_store,
        goal_store=goal_store,
        pursuit_store=pursuit_store,
        task_store=task_store,
        workbench_store=workbench_store,
    )


def build_runtime_services(
    config: AppConfig,
    *,
    paths: RuntimePaths,
    resources: RuntimeResources,
    overrides: RuntimeServiceOverrides | None = None,
) -> RuntimeServices:
    """Build validated long-running services without starting background work."""
    if not isinstance(paths, RuntimePaths):
        raise TypeError("paths 必须是完整的 RuntimePaths。")
    if not isinstance(resources, RuntimeResources):
        raise TypeError("resources 必须是完整的 RuntimeResources。")
    resolved = RuntimeServiceOverrides() if overrides is None else overrides
    if not isinstance(resolved, RuntimeServiceOverrides):
        raise TypeError("overrides 必须是 RuntimeServiceOverrides。")
    factory = resolved.terminal_runtime_lifecycle_factory
    if factory is not None and not isinstance(factory, TerminalRuntimeLifecycleFactory):
        raise TypeError(
            "terminal_runtime_lifecycle_factory 必须是 "
            "TerminalRuntimeLifecycleFactory。"
        )
    if factory is None:
        factory = TerminalRuntimeLifecycleFactory(
            store=resources.harness_store,
            workspace_root=paths.workspace_root,
            retention_config=config.harness.runtime_heartbeat_retention,
            runtime_identity_provider=lambda: discover_runtime_identity(
                environment=os.environ,
                runtime_path=sys.executable,
            ),
        )
    agent_factory = resolved.agent_execution_heartbeat_factory
    if agent_factory is not None and not isinstance(
        agent_factory,
        AgentExecutionHeartbeatFactory,
    ):
        raise TypeError(
            "agent_execution_heartbeat_factory 必须是 "
            "AgentExecutionHeartbeatFactory。"
        )
    if agent_factory is None:
        agent_factory = AgentExecutionHeartbeatFactory(
            store=resources.harness_store,
            workspace_root=paths.workspace_root,
        )
    browser_factory = resolved.browser_execution_heartbeat_factory
    if browser_factory is not None and not isinstance(
        browser_factory,
        BrowserExecutionHeartbeatFactory,
    ):
        raise TypeError(
            "browser_execution_heartbeat_factory 必须是 "
            "BrowserExecutionHeartbeatFactory。"
        )
    if browser_factory is None:
        browser_factory = BrowserExecutionHeartbeatFactory(
            store=resources.harness_store,
            workspace_root=paths.workspace_root,
        )
    agent_worker_factory = resolved.agent_worker_process_factory
    if agent_worker_factory is not None and not isinstance(
        agent_worker_factory,
        AgentWorkerProcessFactory,
    ):
        raise TypeError(
            "agent_worker_process_factory 必须是 AgentWorkerProcessFactory。"
        )
    if agent_worker_factory is None:
        agent_worker_factory = AgentWorkerProcessFactory(
            worker_registry=resources.worker_registry_store,
            heartbeat_store=resources.harness_store,
            agent_job_store=resources.agent_job_store,
            workspace_root=paths.workspace_root,
            runtime_dir=paths.agent_worker_runtime_dir,
            software_version=__version__,
            max_concurrent_jobs=config.safety.max_parallel_agents,
            model_config=config.models,
        )
    agent_worker_supervisor_factory = resolved.agent_worker_supervisor_factory
    if agent_worker_supervisor_factory is not None and not isinstance(
        agent_worker_supervisor_factory,
        AgentWorkerSupervisorFactory,
    ):
        raise TypeError(
            "agent_worker_supervisor_factory 必须是 "
            "AgentWorkerSupervisorFactory。"
        )
    if agent_worker_supervisor_factory is None:
        agent_worker_supervisor_factory = AgentWorkerSupervisorFactory(
            worker_registry=resources.worker_registry_store,
            heartbeat_store=resources.harness_store,
            agent_job_store=resources.agent_job_store,
            workspace_root=paths.workspace_root,
        )
    stable_remote_transport = resolved.stable_remote_finalization_transport
    http_transport = config.harness.stable_remote_finalization_http_transport
    if stable_remote_transport is None and http_transport.enabled:
        stable_remote_transport = MTLSStableRemoteFinalizationInstallationTransport(
            StableRemoteFinalizationHTTPClientPolicy(
                endpoint_url=http_transport.endpoint_url,
                server_ca_path=Path(http_transport.server_ca_path),
                client_certificate_path=Path(http_transport.client_certificate_path),
                client_private_key_path=Path(http_transport.client_private_key_path),
                server_certificate_sha256_pins=tuple(
                    http_transport.server_certificate_sha256_pins
                ),
                connect_timeout_seconds=http_transport.connect_timeout_seconds,
                request_timeout_seconds=http_transport.request_timeout_seconds,
                max_response_bytes=http_transport.max_response_bytes,
            )
        )
    return RuntimeServices(
        terminal_runtime_lifecycle_factory=factory,
        agent_execution_heartbeat_factory=agent_factory,
        browser_execution_heartbeat_factory=browser_factory,
        agent_worker_process_factory=agent_worker_factory,
        agent_worker_supervisor_factory=agent_worker_supervisor_factory,
        stable_stage_completion_inspector=(
            resolved.stable_stage_completion_inspector
        ),
        stable_remote_finalization_transport=stable_remote_transport,
    )


def create_agent_engine(
    config: AppConfig,
    *,
    port_overrides: RuntimePortOverrides[Session] | None = None,
    resource_overrides: RuntimeResourceOverrides | None = None,
    service_overrides: RuntimeServiceOverrides | None = None,
) -> AgentEngine:
    """Create one Engine from the authoritative default Port composition."""
    from naumi_agent.orchestrator.engine import AgentEngine

    paths = build_runtime_paths(config)
    ports = build_runtime_ports(config, paths=paths, overrides=port_overrides)
    resources = build_runtime_resources(paths, overrides=resource_overrides)
    services = build_runtime_services(
        config,
        paths=paths,
        resources=resources,
        overrides=service_overrides,
    )
    return AgentEngine(
        config,
        ports=ports,
        paths=paths,
        resources=resources,
        services=services,
    )


__all__ = [
    "build_runtime_paths",
    "build_runtime_ports",
    "build_runtime_resources",
    "build_runtime_services",
    "create_agent_engine",
]
