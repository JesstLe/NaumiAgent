"""Typed long-running services owned by the runtime composition root."""

from __future__ import annotations

from dataclasses import dataclass

from naumi_agent.daemons.agent_worker_process import AgentWorkerProcessFactory
from naumi_agent.daemons.agent_worker_supervisor import AgentWorkerSupervisorFactory
from naumi_agent.evolution.stable_population_candidate_previews import (
    EvolutionStableStageCompletionInspectionPort,
)
from naumi_agent.evolution.stable_remote_finalization_delivery_worker import (
    EvolutionStableRemoteFinalizationInstallationTransport,
)
from naumi_agent.evolution.stable_remote_finalization_installation_daemon import (
    StableRemoteFinalizationInstallationDaemonFactory,
)
from naumi_agent.evolution.stable_remote_finalization_result_return_worker import (
    EvolutionStableRemoteFinalizationResultTransport,
)
from naumi_agent.runtime.agent_heartbeat import AgentExecutionHeartbeatFactory
from naumi_agent.runtime.browser_heartbeat import BrowserExecutionHeartbeatFactory
from naumi_agent.runtime.terminal_runtime import TerminalRuntimeLifecycleFactory


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    """Services migrated out of AgentEngine so far."""

    terminal_runtime_lifecycle_factory: TerminalRuntimeLifecycleFactory
    agent_execution_heartbeat_factory: AgentExecutionHeartbeatFactory
    browser_execution_heartbeat_factory: BrowserExecutionHeartbeatFactory
    agent_worker_process_factory: AgentWorkerProcessFactory
    agent_worker_supervisor_factory: AgentWorkerSupervisorFactory
    stable_stage_completion_inspector: (
        EvolutionStableStageCompletionInspectionPort | None
    ) = None
    stable_remote_finalization_transport: (
        EvolutionStableRemoteFinalizationInstallationTransport | None
    ) = None
    stable_remote_finalization_result_transport: (
        EvolutionStableRemoteFinalizationResultTransport | None
    ) = None
    stable_remote_finalization_installation_daemon_factory: (
        StableRemoteFinalizationInstallationDaemonFactory | None
    ) = None

    def __post_init__(self) -> None:
        if not isinstance(
            self.terminal_runtime_lifecycle_factory,
            TerminalRuntimeLifecycleFactory,
        ):
            raise TypeError(
                "terminal_runtime_lifecycle_factory 必须是 "
                "TerminalRuntimeLifecycleFactory。"
            )
        if not isinstance(
            self.agent_execution_heartbeat_factory,
            AgentExecutionHeartbeatFactory,
        ):
            raise TypeError(
                "agent_execution_heartbeat_factory 必须是 "
                "AgentExecutionHeartbeatFactory。"
            )
        if not isinstance(
            self.browser_execution_heartbeat_factory,
            BrowserExecutionHeartbeatFactory,
        ):
            raise TypeError(
                "browser_execution_heartbeat_factory 必须是 "
                "BrowserExecutionHeartbeatFactory。"
            )
        if not isinstance(
            self.agent_worker_process_factory,
            AgentWorkerProcessFactory,
        ):
            raise TypeError(
                "agent_worker_process_factory 必须是 AgentWorkerProcessFactory。"
            )
        if not isinstance(
            self.agent_worker_supervisor_factory,
            AgentWorkerSupervisorFactory,
        ):
            raise TypeError(
                "agent_worker_supervisor_factory 必须是 "
                "AgentWorkerSupervisorFactory。"
            )
        if self.stable_stage_completion_inspector is not None and not isinstance(
            self.stable_stage_completion_inspector,
            EvolutionStableStageCompletionInspectionPort,
        ):
            raise TypeError(
                "stable_stage_completion_inspector 必须实现 5f5r inspect 契约。"
            )
        if self.stable_remote_finalization_transport is not None and not isinstance(
            self.stable_remote_finalization_transport,
            EvolutionStableRemoteFinalizationInstallationTransport,
        ):
            raise TypeError(
                "stable_remote_finalization_transport 必须实现认证安装传输契约。"
            )
        if (
            self.stable_remote_finalization_result_transport is not None
            and not isinstance(
                self.stable_remote_finalization_result_transport,
                EvolutionStableRemoteFinalizationResultTransport,
            )
        ):
            raise TypeError(
                "stable_remote_finalization_result_transport 必须实现认证结果回传契约。"
            )
        if (
            self.stable_remote_finalization_installation_daemon_factory is not None
            and not isinstance(
                self.stable_remote_finalization_installation_daemon_factory,
                StableRemoteFinalizationInstallationDaemonFactory,
            )
        ):
            raise TypeError(
                "stable_remote_finalization_installation_daemon_factory 类型无效。"
            )


@dataclass(frozen=True, slots=True)
class RuntimeServiceOverrides:
    """Optional service instances; None alone selects the production default."""

    terminal_runtime_lifecycle_factory: TerminalRuntimeLifecycleFactory | None = None
    agent_execution_heartbeat_factory: AgentExecutionHeartbeatFactory | None = None
    browser_execution_heartbeat_factory: BrowserExecutionHeartbeatFactory | None = None
    agent_worker_process_factory: AgentWorkerProcessFactory | None = None
    agent_worker_supervisor_factory: AgentWorkerSupervisorFactory | None = None
    stable_stage_completion_inspector: (
        EvolutionStableStageCompletionInspectionPort | None
    ) = None
    stable_remote_finalization_transport: (
        EvolutionStableRemoteFinalizationInstallationTransport | None
    ) = None
    stable_remote_finalization_result_transport: (
        EvolutionStableRemoteFinalizationResultTransport | None
    ) = None
    stable_remote_finalization_installation_daemon_factory: (
        StableRemoteFinalizationInstallationDaemonFactory | None
    ) = None


__all__ = ["RuntimeServiceOverrides", "RuntimeServices"]
