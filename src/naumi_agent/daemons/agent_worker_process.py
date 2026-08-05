"""Authenticated lifecycle and pre-start owner lease for one Agent worker.

This module establishes the OS-process, local-credential, registration, and
heartbeat boundary required before durable Agent jobs may move out of the
embedded Runtime. The process can reserve and claim one admitted durable Job,
authenticate an ephemeral AES-GCM dispatch into child memory, and renew both
authorities. When an encrypted model profile is configured it can also execute
one tool-free model call behind a two-phase running fence and commit an
authenticated terminal payload. Tool execution remains disabled.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import math
import multiprocessing
import os
import queue
import re
import secrets
import stat
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path

from naumi_agent.config.settings import ModelConfig
from naumi_agent.daemons.agent_jobs import (
    AgentJobError,
    AgentJobPayload,
    AgentJobState,
    AgentJobStore,
    AgentJobTerminalPayload,
    AgentJobTransitionResult,
    decode_agent_job_dispatch_payload,
    encode_agent_job_dispatch_payload,
)
from naumi_agent.daemons.agent_worker_contract import (
    AgentWorkerRequest,
    AgentWorkerResult,
    issue_agent_worker_result,
)
from naumi_agent.daemons.agent_worker_model_execution import (
    MAX_AGENT_MODEL_RESPONSE_BYTES,
    AgentWorkerModelExecutionPreparation,
    AgentWorkerModelExecutionResult,
    AgentWorkerModelExecutionStatus,
    AgentWorkerModelTurnResult,
    decode_model_execution_result,
    decode_model_profile,
    encode_model_execution_result,
    encode_model_profile,
    execute_model_turn,
    issue_model_execution_preparation,
    model_profile_sha256,
)
from naumi_agent.daemons.agent_worker_tool_rpc import (
    AgentWorkerToolCallBatch,
    AgentWorkerToolManifest,
    AgentWorkerToolResultBatch,
    decode_tool_call_batch,
    decode_tool_manifest,
    decode_tool_result_batch,
    encode_tool_call_batch,
    encode_tool_manifest,
    encode_tool_result_batch,
    issue_tool_call_batch,
    issue_tool_manifest,
    issue_tool_result_batch,
    tool_call_signature,
)
from naumi_agent.daemons.worker_contract import (
    WorkerCapability,
    WorkerContract,
    WorkerHealthReport,
    WorkerIsolationContract,
    WorkerKind,
    WorkerResourceEnvelope,
    detect_worker_platform,
    issue_worker_contract,
    issue_worker_health_report,
)
from naumi_agent.daemons.worker_registry import (
    WorkerRegistryConflictError,
    WorkerRegistryStore,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeat, HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore
from naumi_agent.safety.payload_envelope import (
    PayloadEnvelope,
    PayloadEnvelopeError,
    RuntimePayloadKey,
    open_runtime_payload,
    seal_runtime_payload,
)
from naumi_agent.tools.base import ToolCall, ToolResult

_PROTOCOL_VERSION = 1
_CONTROL_CAPABILITY = WorkerCapability.AGENT_CONTROL_TRANSPORT
_OWNER_LEASE_CAPABILITY = WorkerCapability.AGENT_JOB_OWNER_LEASE
_CONTEXT_SCOPE_CAPABILITY = WorkerCapability.AGENT_CONTEXT_SCOPE
_MODEL_EXECUTION_CAPABILITY = WorkerCapability.AGENT_MODEL_EXECUTION
_TOOL_RPC_CAPABILITY = WorkerCapability.AGENT_TOOL_RPC
_JOB_DISPATCH_AAD_PREFIX = b"NAUMI_AGENT_WORKER_JOB_DISPATCH_V1\x00"
_MODEL_PROFILE_AAD_PREFIX = b"NAUMI_AGENT_WORKER_MODEL_PROFILE_V1\x00"
_MODEL_TERMINAL_AAD_PREFIX = b"NAUMI_AGENT_WORKER_MODEL_TERMINAL_V1\x00"
_TOOL_MANIFEST_AAD_PREFIX = b"NAUMI_AGENT_WORKER_TOOL_MANIFEST_V1\x00"
_TOOL_CALL_AAD_PREFIX = b"NAUMI_AGENT_WORKER_TOOL_CALL_V1\x00"
_TOOL_RESULT_AAD_PREFIX = b"NAUMI_AGENT_WORKER_TOOL_RESULT_V1\x00"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AgentWorkerProcessError(RuntimeError):
    """Raised when the Agent worker process boundary cannot be trusted."""


class AgentWorkerProcessState(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentWorkerProcessSnapshot:
    worker_id: str
    instance_id: str
    epoch: int
    state: AgentWorkerProcessState
    process_id: int | None
    contract_sha256: str
    accepting_jobs: bool
    heartbeat_sequence: int
    failure_code: str
    bound_job_id: str
    bound_claim_epoch: int
    capacity_reservation_id: str


@dataclass(frozen=True, slots=True)
class AgentWorkerJobBinding:
    job_id: str
    request_sha256: str
    owner_id: str
    claim_epoch: int
    claim_expires_at: str
    reservation_id: str
    dispatch_envelope_sha256: str

    def __post_init__(self) -> None:
        for field in ("job_id", "owner_id", "reservation_id"):
            if not _IDENTIFIER_RE.fullmatch(getattr(self, field)):
                raise ValueError(f"Agent Worker Job {field} 格式无效。")
        for field in ("request_sha256", "dispatch_envelope_sha256"):
            if not _SHA256_RE.fullmatch(getattr(self, field)):
                raise ValueError(f"Agent Worker Job {field} 格式无效。")
        if (
            isinstance(self.claim_epoch, bool)
            or not isinstance(self.claim_epoch, int)
            or self.claim_epoch < 1
        ):
            raise ValueError("Agent Worker Job claim_epoch 必须是正整数。")
        try:
            expiry = datetime.fromisoformat(self.claim_expires_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("Agent Worker Job claim_expires_at 无效。") from exc
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise ValueError("Agent Worker Job claim_expires_at 必须包含时区。")


@dataclass(frozen=True, slots=True)
class AgentWorkerModelExecutionOutcome:
    transition: AgentJobTransitionResult
    result: AgentWorkerResult
    model: str
    provider_model: str
    provider_response_id_sha256: str
    tool_calls: int

    def __post_init__(self) -> None:
        if not isinstance(self.transition, AgentJobTransitionResult):
            raise TypeError("transition 类型无效。")
        if not isinstance(self.result, AgentWorkerResult):
            raise TypeError("result 类型无效。")
        for value, field_name in (
            (self.model, "model"),
            (self.provider_model, "provider_model"),
        ):
            if not isinstance(value, str) or len(value.encode("utf-8")) > 1024:
                raise ValueError(f"{field_name} 大小无效。")
        if self.provider_response_id_sha256 and not _SHA256_RE.fullmatch(
            self.provider_response_id_sha256
        ):
            raise ValueError("provider_response_id_sha256 格式无效。")
        if (
            isinstance(self.tool_calls, bool)
            or not isinstance(self.tool_calls, int)
            or not 0 <= self.tool_calls <= 64_000
        ):
            raise ValueError("tool_calls 必须在 0 到 64000 之间。")


NowProvider = Callable[[], str]
AgentWorkerToolExecutor = Callable[[ToolCall, str], Awaitable[ToolResult]]


class AuthenticatedAgentWorkerProcess:
    """Own one authenticated child process and its durable lifecycle facts."""

    def __init__(
        self,
        *,
        worker_registry: WorkerRegistryStore,
        heartbeat_store: HarnessStore,
        agent_job_store: AgentJobStore,
        workspace_root: str | Path,
        runtime_dir: str | Path,
        software_version: str,
        model_config: ModelConfig | None = None,
        worker_id: str = "agent-worker-local",
        max_concurrent_jobs: int = 1,
        heartbeat_interval_seconds: float = 10.0,
        heartbeat_timeout_seconds: int = 30,
        handshake_timeout_seconds: float = 15.0,
        shutdown_timeout_seconds: float = 5.0,
        job_claim_lease_seconds: int = 90,
        job_claim_renewal_interval_seconds: float = 30.0,
        now_provider: NowProvider = lambda: datetime.now(UTC).isoformat(),
    ) -> None:
        if not isinstance(worker_registry, WorkerRegistryStore):
            raise TypeError("worker_registry 必须是 WorkerRegistryStore。")
        if not isinstance(heartbeat_store, HarnessStore):
            raise TypeError("heartbeat_store 必须是 HarnessStore。")
        if not isinstance(agent_job_store, AgentJobStore):
            raise TypeError("agent_job_store 必须是 AgentJobStore。")
        workspace = Path(workspace_root).expanduser()
        runtime = Path(runtime_dir).expanduser()
        if not workspace.is_absolute():
            raise ValueError("Agent Worker workspace_root 必须是绝对路径。")
        if not runtime.is_absolute():
            raise ValueError("Agent Worker runtime_dir 必须是绝对路径。")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("Agent Worker worker_id 不能为空。")
        if (
            isinstance(max_concurrent_jobs, bool)
            or not isinstance(max_concurrent_jobs, int)
            or not 1 <= max_concurrent_jobs <= 10_000
        ):
            raise ValueError("Agent Worker max_concurrent_jobs 必须在 1 到 10000 之间。")
        interval = float(heartbeat_interval_seconds)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Agent Worker heartbeat interval 必须大于 0。")
        if (
            isinstance(heartbeat_timeout_seconds, bool)
            or not isinstance(heartbeat_timeout_seconds, int)
            or not 3 <= heartbeat_timeout_seconds <= 86_400
        ):
            raise ValueError("Agent Worker heartbeat timeout 必须在 3 到 86400 秒之间。")
        if interval >= heartbeat_timeout_seconds:
            raise ValueError("Agent Worker heartbeat interval 必须小于 timeout。")
        if not math.isfinite(handshake_timeout_seconds) or handshake_timeout_seconds <= 0:
            raise ValueError("Agent Worker handshake timeout 必须大于 0。")
        if not math.isfinite(shutdown_timeout_seconds) or shutdown_timeout_seconds <= 0:
            raise ValueError("Agent Worker shutdown timeout 必须大于 0。")
        if (
            isinstance(job_claim_lease_seconds, bool)
            or not isinstance(job_claim_lease_seconds, int)
            or not 3 <= job_claim_lease_seconds <= 86_400
        ):
            raise ValueError("Agent Worker Job lease 必须在 3 到 86400 秒之间。")
        renewal_interval = float(job_claim_renewal_interval_seconds)
        if (
            not math.isfinite(renewal_interval)
            or renewal_interval <= 0
            or renewal_interval >= job_claim_lease_seconds
        ):
            raise ValueError("Agent Worker Job renewal interval 必须大于 0 且小于 lease。")
        if not callable(now_provider):
            raise TypeError("now_provider 必须可调用。")
        if model_config is not None and not isinstance(model_config, ModelConfig):
            raise TypeError("model_config 必须是 ModelConfig 或 None。")

        self._registry = worker_registry
        self._heartbeats = heartbeat_store
        self._agent_jobs = agent_job_store
        self._workspace_root = workspace.resolve(strict=False)
        self._runtime_dir = runtime.resolve(strict=False)
        self._software_version = software_version
        self._model_profile = (
            encode_model_profile(model_config) if model_config is not None else None
        )
        self._worker_id = worker_id
        self._max_concurrent_jobs = max_concurrent_jobs
        self._heartbeat_interval_seconds = interval
        self._heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._handshake_timeout_seconds = float(handshake_timeout_seconds)
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._job_claim_lease_seconds = job_claim_lease_seconds
        self._job_claim_renewal_interval_seconds = renewal_interval
        self._now = now_provider

        self._state = AgentWorkerProcessState.CREATED
        self._instance_id = ""
        self._epoch = 0
        self._contract: WorkerContract | None = None
        self._last_heartbeat: HarnessHeartbeat | None = None
        self._sequence = 0
        self._failure_code = ""
        self._nonce = ""
        self._address: object | None = None
        self._family = ""
        self._listener: Listener | None = None
        self._connection: Connection | None = None
        self._process: multiprocessing.Process | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._job_renewal_task: asyncio.Task[None] | None = None
        self._job_ack: asyncio.Future[str] | None = None
        self._execution_ack: asyncio.Future[dict[str, object]] | None = None
        self._job_binding: AgentWorkerJobBinding | None = None
        self._dispatch_key: RuntimePayloadKey | None = None
        self._terminal_event = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._failure_lock = asyncio.Lock()
        self._expected_shutdown = False

    @property
    def contract(self) -> WorkerContract | None:
        return self._contract

    async def start(self) -> AgentWorkerProcessSnapshot:
        """Authenticate a child before registering its exact incarnation."""
        async with self._lifecycle_lock:
            if self._state is not AgentWorkerProcessState.CREATED:
                raise AgentWorkerProcessError("Agent Worker 进程不能重复启动。")
            self._state = AgentWorkerProcessState.STARTING
            try:
                self._prepare_runtime_dir()
            except BaseException as exc:
                await self._abort_start(exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, AgentWorkerProcessError):
                    raise
                raise AgentWorkerProcessError(
                    f"Agent Worker runtime_dir 准备失败：{type(exc).__name__}。"
                ) from exc
            self._instance_id = f"agent-process-{uuid.uuid4().hex}"
            try:
                self._epoch = await self._next_epoch()
            except BaseException as exc:
                await self._abort_start(exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise AgentWorkerProcessError(
                    "已有 active Agent Worker；本切片不允许隐式接管。"
                ) from exc
            try:
                self._spawn_child()
            except BaseException as exc:
                await self._abort_start(exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise AgentWorkerProcessError(
                    f"Agent Worker OS 进程创建失败：{type(exc).__name__}。"
                ) from exc

        try:
            connection = await asyncio.wait_for(
                asyncio.to_thread(self._listener.accept),
                timeout=self._handshake_timeout_seconds,
            )
            self._connection = connection
            hello = await asyncio.wait_for(
                asyncio.to_thread(connection.recv),
                timeout=self._handshake_timeout_seconds,
            )
            _validate_child_message(
                hello,
                expected_type="hello",
                nonce=self._nonce,
                contract=self._required_contract(),
                process_id=self._required_process().pid,
                expected_sequence=0,
            )
            registered_at = self._timestamp("registered_at")
            await self._registry.register(
                self._required_contract(),
                registered_at=registered_at,
            )
            await self._registry.register_process_witness(
                worker_id=self._required_contract().worker_id,
                instance_id=self._required_contract().instance_id,
                epoch=self._required_contract().epoch,
                contract_sha256=self._required_contract().contract_sha256,
                process_id=self._required_process().pid,
                witnessed_at=self._timestamp("process_witnessed_at"),
            )
            await self._record_heartbeat(
                sequence=1,
                phase=HarnessHeartbeatPhase.STARTING,
                detail_code="agent_worker_process_authenticated",
            )
            await asyncio.to_thread(
                connection.send,
                _parent_message(
                    "registered",
                    nonce=self._nonce,
                    contract=self._required_contract(),
                ),
            )
            running = await asyncio.wait_for(
                asyncio.to_thread(connection.recv),
                timeout=self._handshake_timeout_seconds,
            )
            _validate_child_message(
                running,
                expected_type="running",
                nonce=self._nonce,
                contract=self._required_contract(),
                process_id=self._required_process().pid,
                expected_sequence=2,
            )
            await self._record_heartbeat(
                sequence=2,
                phase=HarnessHeartbeatPhase.RUNNING,
                detail_code=(
                    "agent_worker_model_execution_ready"
                    if self._model_profile is not None
                    else "agent_worker_control_ready_dispatch_disabled"
                ),
            )
            async with self._lifecycle_lock:
                self._state = AgentWorkerProcessState.RUNNING
                self._monitor_task = asyncio.create_task(
                    self._monitor_child(),
                    name=f"naumi-agent-worker-monitor-{self._epoch}",
                )
            return self.snapshot()
        except BaseException as exc:
            await self._abort_start(exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, AgentWorkerProcessError):
                raise
            raise AgentWorkerProcessError(
                f"Agent Worker 本机认证启动失败：{type(exc).__name__}。"
            ) from exc
        finally:
            if self._listener is not None:
                self._listener.close()
                self._listener = None

    async def close(self) -> AgentWorkerProcessSnapshot:
        """Drain, stop, and revoke this exact incarnation."""
        if self._job_binding is not None:
            await self.release_job()
        async with self._lifecycle_lock:
            if self._state in {
                AgentWorkerProcessState.STOPPED,
                AgentWorkerProcessState.FAILED,
            }:
                return self.snapshot()
            if self._state is not AgentWorkerProcessState.RUNNING:
                raise AgentWorkerProcessError("Agent Worker 尚未完成认证启动。")
            self._expected_shutdown = True
            self._state = AgentWorkerProcessState.DRAINING
            connection = self._required_connection()
            await asyncio.to_thread(
                connection.send,
                _parent_message(
                    "shutdown",
                    nonce=self._nonce,
                    contract=self._required_contract(),
                ),
            )

        try:
            await asyncio.wait_for(
                self._terminal_event.wait(),
                timeout=self._shutdown_timeout_seconds,
            )
        except TimeoutError as exc:
            await self._mark_failed("agent_worker_shutdown_timeout")
            raise AgentWorkerProcessError("Agent Worker 排空停止超时。") from exc

        await self._join_process()
        if self._state is AgentWorkerProcessState.STOPPED:
            await self._revoke("agent_worker_stopped")
        await self._cleanup_transport()
        return self.snapshot()

    async def bind_job(self, job_id: str) -> AgentWorkerJobBinding:
        """Reserve, claim, and encrypt one admitted Job into child memory.

        The child only authenticates and stages the request. It cannot mark the
        Job running or invoke a model in this slice.
        """
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("Agent Worker job_id 不能为空。")
        async with self._command_lock:
            if self._state is not AgentWorkerProcessState.RUNNING:
                raise AgentWorkerProcessError("Agent Worker 尚未处于可绑定状态。")
            if self._job_binding is not None:
                raise AgentWorkerProcessError("Agent Worker 已持有一个 Job owner lease。")
            stored = await self._agent_jobs.get(job_id)
            if stored is None:
                raise AgentWorkerProcessError("AgentJob 不存在。")
            if stored.state is not AgentJobState.ADMITTED:
                raise AgentWorkerProcessError(
                    "独立 Agent Worker 只接受 admitted Job；过期 claim 需要 Supervisor。"
                )
            contract = self._required_contract()
            owner_id = _job_owner_id(contract)
            reservation_id = _job_reservation_id(contract, stored.job_id)
            reserved = False
            claimed_job = None
            dispatch_sent = False
            try:
                await self._registry.reserve_capacity(
                    reservation_id=reservation_id,
                    worker_id=contract.worker_id,
                    instance_id=contract.instance_id,
                    epoch=contract.epoch,
                    job_id=stored.job_id,
                    reserved_at=self._timestamp("reserved_at"),
                    ttl_seconds=self._job_claim_lease_seconds,
                )
                reserved = True
                transition = await self._agent_jobs.claim_admitted_for_worker(
                    stored.job_id,
                    owner_id=owner_id,
                    expected_request_sha256=stored.request_sha256,
                    lease_seconds=self._job_claim_lease_seconds,
                )
                claimed_job = transition.job
                payload = await self._agent_jobs.recover_payload(
                    claimed_job.job_id,
                    owner_id=owner_id,
                    claim_epoch=claimed_job.claim_epoch,
                )
                dispatch_plaintext = encode_agent_job_dispatch_payload(
                    claimed_job.request,
                    payload,
                )
                aad = _job_dispatch_aad(
                    contract=contract,
                    job_id=claimed_job.job_id,
                    request_sha256=claimed_job.request_sha256,
                    owner_id=owner_id,
                    claim_epoch=claimed_job.claim_epoch,
                    claim_expires_at=claimed_job.claim_expires_at or "",
                    reservation_id=reservation_id,
                )
                envelope = seal_runtime_payload(
                    dispatch_plaintext,
                    aad=aad,
                    key=self._required_dispatch_key(),
                )
                binding = AgentWorkerJobBinding(
                    job_id=claimed_job.job_id,
                    request_sha256=claimed_job.request_sha256,
                    owner_id=owner_id,
                    claim_epoch=claimed_job.claim_epoch,
                    claim_expires_at=claimed_job.claim_expires_at or "",
                    reservation_id=reservation_id,
                    dispatch_envelope_sha256=envelope.envelope_sha256,
                )
                self._job_binding = binding
                ack = asyncio.get_running_loop().create_future()
                self._job_ack = ack
                dispatch_sent = True
                await asyncio.to_thread(
                    self._required_connection().send,
                    _parent_job_message(
                        "bind_job",
                        nonce=self._nonce,
                        contract=contract,
                        binding=binding,
                        envelope=envelope,
                    ),
                )
                ack_type = await asyncio.wait_for(
                    asyncio.shield(ack),
                    timeout=self._handshake_timeout_seconds,
                )
                if ack_type != "job_bound":
                    raise AgentWorkerProcessError("Agent Worker Job bind 回执类型无效。")
                self._job_ack = None
                self._job_renewal_task = asyncio.create_task(
                    self._renew_bound_job(),
                    name=f"naumi-agent-worker-job-renew-{contract.epoch}",
                )
                return binding
            except BaseException as exc:
                self._job_ack = None
                if dispatch_sent:
                    await self._mark_failed("agent_worker_job_bind_failed")
                elif claimed_job is not None:
                    self._job_binding = AgentWorkerJobBinding(
                        job_id=claimed_job.job_id,
                        request_sha256=claimed_job.request_sha256,
                        owner_id=owner_id,
                        claim_epoch=claimed_job.claim_epoch,
                        claim_expires_at=claimed_job.claim_expires_at or "",
                        reservation_id=reservation_id,
                        dispatch_envelope_sha256="0" * 64,
                    )
                    await self._release_job_authority(
                        release_claim=True,
                        release_capacity=reserved,
                    )
                elif reserved:
                    with contextlib.suppress(Exception):
                        await self._registry.release_capacity(
                            reservation_id=reservation_id,
                            worker_id=contract.worker_id,
                            instance_id=contract.instance_id,
                            epoch=contract.epoch,
                            reason_code="agent_worker_job_bind_failed",
                            released_at=self._timestamp("released_at"),
                            accept_terminal=True,
                        )
                self._job_binding = None
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, AgentWorkerProcessError):
                    raise
                raise AgentWorkerProcessError(
                    f"Agent Worker Job owner lease 建立失败：{type(exc).__name__}。"
                ) from exc

    async def release_job(self) -> None:
        """Clear child plaintext, then release the exact pre-start authorities."""
        async with self._command_lock:
            binding = self._job_binding
            if binding is None:
                return
            await self._stop_job_renewal()
            if self._state is AgentWorkerProcessState.RUNNING:
                ack = asyncio.get_running_loop().create_future()
                self._job_ack = ack
                try:
                    await asyncio.to_thread(
                        self._required_connection().send,
                        _parent_job_message(
                            "release_job",
                            nonce=self._nonce,
                            contract=self._required_contract(),
                            binding=binding,
                        ),
                    )
                    ack_type = await asyncio.wait_for(
                        asyncio.shield(ack),
                        timeout=self._shutdown_timeout_seconds,
                    )
                    if ack_type != "job_released":
                        raise AgentWorkerProcessError(
                            "Agent Worker Job release 回执类型无效。"
                        )
                except BaseException as exc:
                    self._job_ack = None
                    await self._mark_failed("agent_worker_job_release_failed")
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise AgentWorkerProcessError(
                        "Agent Worker 未能确认清除加密 Job payload。"
                    ) from exc
                finally:
                    self._job_ack = None
            try:
                await self._release_job_authority(
                    release_claim=True,
                    release_capacity=True,
                )
            except AgentWorkerProcessError:
                await self._mark_failed("agent_worker_job_authority_release_failed")
                raise
            else:
                self._job_binding = None

    async def execute_bound_model_job(self) -> AgentWorkerModelExecutionOutcome:
        """Run and durably commit one tool-free model request in the child process."""
        return await self.execute_bound_agent_job(
            tool_schemas=(),
            tool_executor=None,
        )

    async def execute_bound_agent_job(
        self,
        *,
        tool_schemas: list[dict[str, object]] | tuple[dict[str, object], ...],
        tool_executor: AgentWorkerToolExecutor | None,
    ) -> AgentWorkerModelExecutionOutcome:
        """Run one bounded Agent loop while all tools remain parent-authorized."""
        async with self._command_lock:
            binding = self._job_binding
            profile = self._model_profile
            if self._state is not AgentWorkerProcessState.RUNNING:
                raise AgentWorkerProcessError("Agent Worker 尚未处于可执行状态。")
            if binding is None:
                raise AgentWorkerProcessError("Agent Worker 尚未绑定 AgentJob。")
            if profile is None:
                raise AgentWorkerProcessError("Agent Worker 未配置独立模型执行能力。")
            stored = await self._agent_jobs.get(binding.job_id)
            if (
                stored is None
                or stored.state is not AgentJobState.CLAIMED
                or stored.claim_owner_id != binding.owner_id
                or stored.claim_epoch != binding.claim_epoch
                or stored.request_sha256 != binding.request_sha256
            ):
                raise AgentWorkerProcessError("Agent Worker pre-start Job fence 已变化。")
            if tool_executor is not None and not callable(tool_executor):
                raise TypeError("tool_executor 必须可调用。")
            try:
                manifest = issue_tool_manifest(
                    tool_scope=stored.request.tool_scope,
                    tools=list(tool_schemas),
                )
            except (TypeError, ValueError) as exc:
                raise AgentWorkerProcessError(
                    "Agent Worker 工具 manifest 与 request scope 不一致。"
                ) from exc
            if manifest.tool_scope and tool_executor is None:
                raise AgentWorkerProcessError(
                    "独立 Agent 工具任务缺少父 Runtime 权威执行入口。"
                )
            if not manifest.tool_scope and tool_executor is not None:
                raise AgentWorkerProcessError(
                    "无工具 AgentJob 不得注入额外工具执行入口。"
                )
            preparation = issue_model_execution_preparation(
                job_id=binding.job_id,
                request_sha256=binding.request_sha256,
                claim_epoch=binding.claim_epoch,
                model_profile_sha256=model_profile_sha256(profile),
                tool_manifest_sha256=manifest.manifest_sha256,
            )
            profile_envelope = seal_runtime_payload(
                profile,
                aad=_model_profile_aad(
                    contract=self._required_contract(),
                    binding=binding,
                    preparation=preparation,
                ),
                key=self._required_dispatch_key(),
            )
            manifest_envelope = seal_runtime_payload(
                encode_tool_manifest(manifest),
                aad=_tool_manifest_aad(
                    contract=self._required_contract(),
                    binding=binding,
                    preparation=preparation,
                ),
                key=self._required_dispatch_key(),
            )
            try:
                prepared = await self._send_execution_command(
                    _parent_execution_prepare_message(
                        nonce=self._nonce,
                        contract=self._required_contract(),
                        binding=binding,
                        preparation=preparation,
                        profile_envelope=profile_envelope,
                        manifest_envelope=manifest_envelope,
                    ),
                    expected_types=("model_prepared",),
                    timeout=self._handshake_timeout_seconds,
                )
                _validate_child_execution_message(
                    prepared,
                    expected_type="model_prepared",
                    nonce=self._nonce,
                    contract=self._required_contract(),
                    process_id=self._required_process().pid,
                    expected_sequence=int(prepared["sequence"]),
                    binding=binding,
                    preparation=preparation,
                )
                running = await self._agent_jobs.mark_running(
                    binding.job_id,
                    owner_id=binding.owner_id,
                    claim_epoch=binding.claim_epoch,
                )
                running_receipt_sha256 = running.job.latest_receipt.receipt_sha256
                loop = asyncio.get_running_loop()
                work_deadline = (
                    loop.time() + stored.request.timeout_milliseconds / 1000
                )
                transport_deadline = (
                    work_deadline + self._handshake_timeout_seconds * 2
                )

                def remaining_work_timeout() -> float:
                    remaining = work_deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError("Agent Worker 工具循环总超时。")
                    return remaining

                def remaining_transport_timeout() -> float:
                    remaining = transport_deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError("Agent Worker Tool RPC 传输总超时。")
                    return remaining

                response = await self._send_execution_command(
                    _parent_execution_start_message(
                        nonce=self._nonce,
                        contract=self._required_contract(),
                        binding=binding,
                        preparation=preparation,
                        running_receipt_sha256=running_receipt_sha256,
                    ),
                    expected_types=("tool_request", "model_terminal"),
                    timeout=remaining_transport_timeout(),
                )
                tool_call_count = 0
                while str(response.get("type")) == "tool_request":
                    if tool_executor is None:
                        raise AgentWorkerProcessError(
                            "Agent Worker 收到未授权 Tool RPC。"
                        )
                    call_batch = _open_child_tool_request_message(
                        response,
                        nonce=self._nonce,
                        contract=self._required_contract(),
                        process_id=self._required_process().pid,
                        binding=binding,
                        preparation=preparation,
                        running_receipt_sha256=running_receipt_sha256,
                        tool_scope=manifest.tool_scope,
                        dispatch_key=self._required_dispatch_key(),
                    )
                    results: list[ToolResult] = []
                    for call in call_batch.calls:
                        async with asyncio.timeout(remaining_work_timeout()):
                            result_value = await tool_executor(
                                call,
                                stored.request.agent_name,
                            )
                        if not isinstance(result_value, ToolResult):
                            raise AgentWorkerProcessError(
                                "父 Runtime Tool authority 返回类型无效。"
                            )
                        results.append(result_value)
                    result_batch = issue_tool_result_batch(
                        call_batch=call_batch,
                        results=results,
                    )
                    result_envelope = seal_runtime_payload(
                        encode_tool_result_batch(result_batch),
                        aad=_tool_result_aad(
                            contract=self._required_contract(),
                            binding=binding,
                            preparation=preparation,
                            running_receipt_sha256=running_receipt_sha256,
                            call_batch=call_batch,
                            result_batch=result_batch,
                        ),
                        key=self._required_dispatch_key(),
                    )
                    tool_call_count += len(call_batch.calls)
                    response = await self._send_execution_command(
                        _parent_tool_result_message(
                            nonce=self._nonce,
                            contract=self._required_contract(),
                            binding=binding,
                            preparation=preparation,
                            running_receipt_sha256=running_receipt_sha256,
                            call_batch=call_batch,
                            result_batch=result_batch,
                            result_envelope=result_envelope,
                        ),
                        expected_types=("tool_request", "model_terminal"),
                        timeout=remaining_transport_timeout(),
                    )
                terminal = response
                model_result = _open_child_model_terminal_message(
                    terminal,
                    nonce=self._nonce,
                    contract=self._required_contract(),
                    process_id=self._required_process().pid,
                    binding=binding,
                    preparation=preparation,
                    running_receipt_sha256=running_receipt_sha256,
                    dispatch_key=self._required_dispatch_key(),
                )
                result = issue_agent_worker_result(
                    request=stored.request,
                    status=model_result.status.value,
                    response=model_result.response,
                    error=model_result.error or None,
                    total_tokens=model_result.total_tokens,
                    total_cost_usd=model_result.total_cost_usd,
                    turns=model_result.turns,
                    completed_at=self._timestamp("completed_at"),
                )
                transition = await self._agent_jobs.finish(
                    binding.job_id,
                    owner_id=binding.owner_id,
                    claim_epoch=binding.claim_epoch,
                    result=result,
                    terminal_payload=AgentJobTerminalPayload(
                        response=model_result.response,
                        error=model_result.error,
                    ),
                )
                await self._stop_job_renewal()
                committed = await self._send_execution_command(
                    _parent_execution_commit_message(
                        nonce=self._nonce,
                        contract=self._required_contract(),
                        binding=binding,
                        preparation=preparation,
                        result_sha256=result.result_sha256,
                        terminal_receipt_sha256=(
                            transition.job.latest_receipt.receipt_sha256
                        ),
                    ),
                    expected_types=("model_committed",),
                    timeout=self._shutdown_timeout_seconds,
                )
                _validate_child_execution_commit_message(
                    committed,
                    nonce=self._nonce,
                    contract=self._required_contract(),
                    process_id=self._required_process().pid,
                    binding=binding,
                    preparation=preparation,
                    result_sha256=result.result_sha256,
                    terminal_receipt_sha256=(
                        transition.job.latest_receipt.receipt_sha256
                    ),
                )
                await self._registry.release_capacity(
                    reservation_id=binding.reservation_id,
                    worker_id=self._required_contract().worker_id,
                    instance_id=self._required_contract().instance_id,
                    epoch=self._required_contract().epoch,
                    reason_code="agent_worker_model_terminal_committed",
                    released_at=self._timestamp("released_at"),
                    accept_terminal=True,
                )
                self._job_binding = None
                return AgentWorkerModelExecutionOutcome(
                    transition=transition,
                    result=result,
                    model=model_result.model,
                    provider_model=model_result.provider_model,
                    provider_response_id_sha256=(
                        model_result.provider_response_id_sha256
                    ),
                    tool_calls=tool_call_count,
                )
            except BaseException as exc:
                self._execution_ack = None
                await self._mark_failed("agent_worker_model_execution_failed")
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, AgentWorkerProcessError):
                    raise
                raise AgentWorkerProcessError(
                    f"独立 Agent 模型执行收口失败：{type(exc).__name__}。"
                ) from exc

    async def _send_execution_command(
        self,
        message: dict[str, object],
        *,
        expected_types: tuple[str, ...],
        timeout: float,
    ) -> dict[str, object]:
        if self._execution_ack is not None:
            raise AgentWorkerProcessError("已有等待中的 Agent model 命令。")
        ack: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        self._execution_ack = ack
        try:
            await asyncio.to_thread(self._required_connection().send, message)
            response = await asyncio.wait_for(asyncio.shield(ack), timeout=timeout)
            if str(response.get("type")) not in expected_types:
                raise AgentWorkerProcessError("Agent model 回执类型无效。")
            return response
        finally:
            self._execution_ack = None

    def health_report(self) -> WorkerHealthReport:
        """Return authenticated liveness and exact execution readiness."""
        contract = self._required_contract()
        heartbeat = self._last_heartbeat
        if heartbeat is None:
            raise AgentWorkerProcessError("Agent Worker 尚无可信心跳。")
        return issue_worker_health_report(
            contract=contract,
            heartbeat=heartbeat,
            active_jobs=1 if self._job_binding is not None else 0,
            accepting_jobs=self._model_profile is not None,
        )

    def snapshot(self) -> AgentWorkerProcessSnapshot:
        process_id = self._process.pid if self._process is not None else None
        digest = self._contract.contract_sha256 if self._contract is not None else ""
        binding = self._job_binding
        return AgentWorkerProcessSnapshot(
            worker_id=self._worker_id,
            instance_id=self._instance_id,
            epoch=self._epoch,
            state=self._state,
            process_id=process_id,
            contract_sha256=digest,
            accepting_jobs=self._model_profile is not None,
            heartbeat_sequence=self._sequence,
            failure_code=self._failure_code,
            bound_job_id=binding.job_id if binding is not None else "",
            bound_claim_epoch=binding.claim_epoch if binding is not None else 0,
            capacity_reservation_id=(
                binding.reservation_id if binding is not None else ""
            ),
        )

    async def _monitor_child(self) -> None:
        try:
            connection = self._required_connection()
            while True:
                message = await asyncio.to_thread(connection.recv)
                message_type = str(message.get("type")) if isinstance(message, dict) else ""
                expected_sequence = self._sequence + 1
                if message_type in {"hello", "running", "pulse", "draining", "stopped"}:
                    _validate_child_message(
                        message,
                        expected_type=message_type,
                        nonce=self._nonce,
                        contract=self._required_contract(),
                        process_id=self._required_process().pid,
                        expected_sequence=expected_sequence,
                    )
                if message_type == "pulse":
                    await self._record_heartbeat(
                        sequence=expected_sequence,
                        phase=HarnessHeartbeatPhase.RUNNING,
                        detail_code=(
                            "agent_worker_model_execution_alive"
                            if self._execution_ack is not None
                            else (
                                "agent_worker_model_execution_idle"
                                if self._model_profile is not None
                                else "agent_worker_control_alive_dispatch_disabled"
                            )
                        ),
                    )
                elif message_type == "draining":
                    await self._record_heartbeat(
                        sequence=expected_sequence,
                        phase=HarnessHeartbeatPhase.DRAINING,
                        detail_code="agent_worker_draining",
                    )
                    self._state = AgentWorkerProcessState.DRAINING
                elif message_type == "stopped":
                    await self._record_heartbeat(
                        sequence=expected_sequence,
                        phase=HarnessHeartbeatPhase.STOPPED,
                        detail_code="agent_worker_stopped",
                    )
                    self._state = AgentWorkerProcessState.STOPPED
                    self._terminal_event.set()
                    return
                elif message_type in {"job_bound", "job_released"}:
                    binding = self._job_binding
                    if binding is None:
                        raise AgentWorkerProcessError(
                            "Agent Worker 返回了无对应 authority 的 Job 回执。"
                        )
                    _validate_child_job_message(
                        message,
                        expected_type=message_type,
                        nonce=self._nonce,
                        contract=self._required_contract(),
                        process_id=self._required_process().pid,
                        expected_sequence=expected_sequence,
                        binding=binding,
                    )
                    await self._record_heartbeat(
                        sequence=expected_sequence,
                        phase=HarnessHeartbeatPhase.RUNNING,
                        detail_code=(
                            "agent_worker_job_owner_lease_bound"
                            if message_type == "job_bound"
                            else "agent_worker_job_owner_lease_released"
                        ),
                    )
                    ack = self._job_ack
                    if ack is None or ack.done():
                        raise AgentWorkerProcessError(
                            "Agent Worker Job 回执没有等待中的命令。"
                        )
                    ack.set_result(message_type)
                elif message_type in {
                    "model_prepared",
                    "tool_request",
                    "model_terminal",
                    "model_committed",
                }:
                    _validate_child_execution_identity(
                        message,
                        expected_type=message_type,
                        nonce=self._nonce,
                        contract=self._required_contract(),
                        process_id=self._required_process().pid,
                        expected_sequence=expected_sequence,
                    )
                    await self._record_heartbeat(
                        sequence=expected_sequence,
                        phase=HarnessHeartbeatPhase.RUNNING,
                        detail_code={
                            "model_prepared": "agent_worker_model_prepared",
                            "tool_request": "agent_worker_tool_authority_waiting",
                            "model_terminal": "agent_worker_model_terminal_ready",
                            "model_committed": "agent_worker_model_terminal_committed",
                        }[message_type],
                    )
                    execution_ack = self._execution_ack
                    if execution_ack is None or execution_ack.done():
                        raise AgentWorkerProcessError(
                            "Agent model 回执没有等待中的命令。"
                        )
                    execution_ack.set_result(message)
                else:
                    raise AgentWorkerProcessError("Agent Worker 返回未知生命周期消息。")
        except asyncio.CancelledError:
            raise
        except (EOFError, OSError, ValueError, AgentWorkerProcessError):
            if self._state is not AgentWorkerProcessState.STOPPED:
                await self._mark_failed(
                    "agent_worker_transport_closed"
                    if not self._expected_shutdown
                    else "agent_worker_shutdown_incomplete"
                )

    async def _renew_bound_job(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._job_claim_renewal_interval_seconds)
                binding = self._job_binding
                if binding is None:
                    return
                contract = self._required_contract()
                renewed_at = self._timestamp("renewed_at")
                await self._registry.renew_capacity(
                    reservation_id=binding.reservation_id,
                    worker_id=contract.worker_id,
                    instance_id=contract.instance_id,
                    epoch=contract.epoch,
                    job_id=binding.job_id,
                    renewed_at=renewed_at,
                    ttl_seconds=self._job_claim_lease_seconds,
                )
                await self._agent_jobs.renew_claim(
                    binding.job_id,
                    owner_id=binding.owner_id,
                    claim_epoch=binding.claim_epoch,
                    lease_seconds=self._job_claim_lease_seconds,
                )
        except asyncio.CancelledError:
            raise
        except (AgentJobError, WorkerRegistryConflictError, OSError, ValueError):
            await self._mark_failed("agent_worker_job_owner_lease_renewal_failed")

    async def _stop_job_renewal(self) -> None:
        task = self._job_renewal_task
        current = asyncio.current_task()
        if task is not None and task is not current and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if task is not current:
            self._job_renewal_task = None

    async def _release_job_authority(
        self,
        *,
        release_claim: bool,
        release_capacity: bool,
    ) -> None:
        binding = self._job_binding
        if binding is None:
            return
        contract = self._required_contract()
        errors: list[BaseException] = []
        if release_claim:
            try:
                await self._agent_jobs.release_prestart_worker_claim(
                    binding.job_id,
                    owner_id=binding.owner_id,
                    claim_epoch=binding.claim_epoch,
                    expected_request_sha256=binding.request_sha256,
                )
            except BaseException as exc:
                errors.append(exc)
        if release_capacity:
            try:
                await self._registry.release_capacity(
                    reservation_id=binding.reservation_id,
                    worker_id=contract.worker_id,
                    instance_id=contract.instance_id,
                    epoch=contract.epoch,
                    reason_code="agent_worker_job_released",
                    released_at=self._timestamp("released_at"),
                    accept_terminal=True,
                )
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise AgentWorkerProcessError(
                "Agent Worker Job authority 未能完整释放。"
            ) from errors[0]

    async def _mark_failed(self, code: str) -> None:
        async with self._failure_lock:
            if (
                self._state is AgentWorkerProcessState.FAILED
                and self._terminal_event.is_set()
            ):
                return
            self._failure_code = code
            self._state = AgentWorkerProcessState.FAILED
            ack = self._job_ack
            if ack is not None and not ack.done():
                ack.set_exception(
                    AgentWorkerProcessError("Agent Worker Job 控制通道已失败。")
                )
            execution_ack = self._execution_ack
            if execution_ack is not None and not execution_ack.done():
                execution_ack.set_exception(
                    AgentWorkerProcessError("Agent Worker model 控制通道已失败。")
                )
            await self._stop_job_renewal()
            await self._terminate_process()
            if self._job_binding is not None:
                with contextlib.suppress(Exception):
                    await self._release_job_authority(
                        release_claim=True,
                        release_capacity=True,
                    )
                self._job_binding = None
            if self._contract is not None:
                with contextlib.suppress(Exception):
                    await self._record_heartbeat(
                        sequence=self._sequence + 1,
                        phase=HarnessHeartbeatPhase.FAILED,
                        detail_code=code,
                    )
                with contextlib.suppress(Exception):
                    await self._revoke(code)
            self._terminal_event.set()
            await self._cleanup_transport()

    async def _abort_start(self, exc: BaseException) -> None:
        self._failure_code = (
            "agent_worker_registration_conflict"
            if isinstance(exc, WorkerRegistryConflictError)
            else "agent_worker_start_failed"
        )
        self._state = AgentWorkerProcessState.FAILED
        connection = self._connection
        if connection is not None:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(
                    connection.send,
                    _parent_message(
                        "abort",
                        nonce=self._nonce,
                        contract=self._required_contract(),
                    ),
                )
        if self._contract is not None:
            with contextlib.suppress(Exception):
                await self._revoke(self._failure_code)
        await self._terminate_process()
        await self._cleanup_transport()
        self._terminal_event.set()

    async def _record_heartbeat(
        self,
        *,
        sequence: int,
        phase: HarnessHeartbeatPhase,
        detail_code: str,
    ) -> HarnessHeartbeat:
        heartbeat = await self._heartbeats.record_heartbeat(
            workspace_root=self._workspace_root,
            subject_kind=HarnessRunKind.AGENT,
            subject_id=self._worker_id,
            instance_id=self._instance_id,
            epoch=self._epoch,
            sequence=sequence,
            phase=phase,
            observed_at=self._timestamp("observed_at"),
            timeout_seconds=self._heartbeat_timeout_seconds,
            detail_code=detail_code,
        )
        self._sequence = heartbeat.sequence
        self._last_heartbeat = heartbeat
        return heartbeat

    async def _next_epoch(self) -> int:
        active = await self._registry.get_active(self._worker_id)
        if active is not None:
            raise WorkerRegistryConflictError(
                "已有 active Agent Worker，必须由后续 Supervisor 显式 fencing。"
            )
        history = await self._registry.list_history(self._worker_id)
        return max((item.contract.epoch for item in history), default=0) + 1

    async def _revoke(self, reason_code: str) -> None:
        contract = self._required_contract()
        active = await self._registry.get_active(contract.worker_id)
        if active is None:
            return
        if (
            active.contract.instance_id != contract.instance_id
            or active.contract.epoch != contract.epoch
        ):
            return
        await self._registry.revoke(
            worker_id=contract.worker_id,
            instance_id=contract.instance_id,
            epoch=contract.epoch,
            reason_code=reason_code,
            revoked_at=self._timestamp("revoked_at"),
        )

    async def _join_process(self) -> None:
        process = self._process
        if process is None:
            return
        await asyncio.to_thread(process.join, self._shutdown_timeout_seconds)
        if process.is_alive():
            await self._terminate_process()
            raise AgentWorkerProcessError("Agent Worker 已回执停止，但 OS 进程仍存活。")
        if process.exitcode != 0 and self._state is AgentWorkerProcessState.STOPPED:
            await self._mark_failed("agent_worker_exit_nonzero")
            raise AgentWorkerProcessError("Agent Worker 停止后返回异常退出码。")

    async def _terminate_process(self) -> None:
        process = self._process
        if process is None:
            return
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, self._shutdown_timeout_seconds)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            await asyncio.to_thread(process.join, self._shutdown_timeout_seconds)

    async def _cleanup_transport(self) -> None:
        monitor = self._monitor_task
        current = asyncio.current_task()
        if monitor is not None and monitor is not current and not monitor.done():
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
        self._monitor_task = None
        if self._connection is not None:
            with contextlib.suppress(OSError):
                self._connection.close()
            self._connection = None
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self._family == "AF_UNIX" and self._address is not None:
            with contextlib.suppress(OSError):
                Path(str(self._address)).unlink()

    def _prepare_runtime_dir(self) -> None:
        self._runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._runtime_dir.is_symlink() or not self._runtime_dir.is_dir():
            raise AgentWorkerProcessError("Agent Worker runtime_dir 必须是真实目录。")
        if os.name != "nt":
            self._runtime_dir.chmod(0o700)
            mode = stat.S_IMODE(self._runtime_dir.stat().st_mode)
            if mode != 0o700:
                raise AgentWorkerProcessError("Agent Worker runtime_dir 权限必须为 0700。")

    def _spawn_child(self) -> None:
        issued_at = self._timestamp("issued_at")
        capabilities = [_CONTROL_CAPABILITY, _OWNER_LEASE_CAPABILITY]
        if self._model_profile is not None:
            capabilities.extend(
                (
                    _CONTEXT_SCOPE_CAPABILITY,
                    _MODEL_EXECUTION_CAPABILITY,
                    _TOOL_RPC_CAPABILITY,
                )
            )
        self._contract = issue_worker_contract(
            worker_id=self._worker_id,
            instance_id=self._instance_id,
            epoch=self._epoch,
            kind=WorkerKind.AGENT,
            protocol_min=_PROTOCOL_VERSION,
            protocol_max=_PROTOCOL_VERSION,
            software_version=self._software_version,
            platform=detect_worker_platform(),
            capabilities=tuple(sorted(capabilities, key=str)),
            resources=WorkerResourceEnvelope(
                max_concurrent_jobs=self._max_concurrent_jobs,
                max_memory_bytes=16 * 1024 * 1024,
                max_cpu_seconds=7 * 24 * 60 * 60,
                max_wall_seconds=7 * 24 * 60 * 60,
                max_output_bytes=(
                    MAX_AGENT_MODEL_RESPONSE_BYTES
                    if self._model_profile is not None
                    else 1024
                ),
            ),
            isolation=WorkerIsolationContract(
                ephemeral_workspace=False,
                network_default_deny=False,
                environment_allowlist=False,
                resource_limits_enforced=False,
                process_tree_cancel=False,
                artifact_digest=False,
            ),
            issued_at=issued_at,
        )
        self._nonce = secrets.token_hex(16)
        authkey = secrets.token_bytes(32)
        self._dispatch_key = RuntimePayloadKey.from_bytes(secrets.token_bytes(32))
        self._address, self._family = _transport_address(self._runtime_dir)
        self._listener = Listener(
            address=self._address,
            family=self._family,
            authkey=authkey,
        )
        process = multiprocessing.get_context("spawn").Process(
            target=_agent_worker_child_main,
            args=(
                self._listener.address,
                self._family,
                authkey,
                self._nonce,
                self._contract.worker_id,
                self._contract.instance_id,
                self._contract.epoch,
                self._contract.contract_sha256,
                self._heartbeat_interval_seconds,
                self._dispatch_key.key_bytes,
                self._model_profile is not None,
            ),
            name=f"naumi-agent-worker-{self._epoch}",
            daemon=False,
        )
        self._process = process
        process.start()

    def _timestamp(self, field: str) -> str:
        value = self._now()
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise AgentWorkerProcessError(f"Agent Worker {field} 时间无效。") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise AgentWorkerProcessError(f"Agent Worker {field} 时间必须包含时区。")
        return parsed.isoformat()

    def _required_contract(self) -> WorkerContract:
        if self._contract is None:
            raise AgentWorkerProcessError("Agent Worker 合同尚未签发。")
        return self._contract

    def _required_process(self) -> multiprocessing.Process:
        if self._process is None:
            raise AgentWorkerProcessError("Agent Worker OS 进程尚未创建。")
        return self._process

    def _required_connection(self) -> Connection:
        if self._connection is None:
            raise AgentWorkerProcessError("Agent Worker 认证连接尚未建立。")
        return self._connection

    def _required_dispatch_key(self) -> RuntimePayloadKey:
        if self._dispatch_key is None:
            raise AgentWorkerProcessError("Agent Worker dispatch key 尚未创建。")
        return self._dispatch_key


class AgentWorkerProcessFactory:
    """Create independent Agent worker control processes from Runtime authority."""

    def __init__(
        self,
        *,
        worker_registry: WorkerRegistryStore,
        heartbeat_store: HarnessStore,
        agent_job_store: AgentJobStore,
        workspace_root: str | Path,
        runtime_dir: str | Path,
        software_version: str,
        max_concurrent_jobs: int,
        model_config: ModelConfig | None = None,
    ) -> None:
        self.worker_registry = worker_registry
        self.heartbeat_store = heartbeat_store
        self.agent_job_store = agent_job_store
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.runtime_dir = Path(runtime_dir).expanduser().resolve(strict=False)
        self.software_version = software_version
        self.max_concurrent_jobs = max_concurrent_jobs
        self._model_profile_payload = (
            encode_model_profile(model_config) if model_config is not None else None
        )
        # Reuse constructor validation without causing filesystem or process side effects.
        AuthenticatedAgentWorkerProcess(
            worker_registry=worker_registry,
            heartbeat_store=heartbeat_store,
            agent_job_store=agent_job_store,
            workspace_root=self.workspace_root,
            runtime_dir=self.runtime_dir,
            software_version=software_version,
            max_concurrent_jobs=max_concurrent_jobs,
            model_config=model_config,
        )

    def create(
        self,
        *,
        worker_id: str = "agent-worker-local",
        heartbeat_interval_seconds: float = 10.0,
        heartbeat_timeout_seconds: int = 30,
        handshake_timeout_seconds: float = 15.0,
        shutdown_timeout_seconds: float = 5.0,
        job_claim_lease_seconds: int = 90,
        job_claim_renewal_interval_seconds: float = 30.0,
        now_provider: NowProvider = lambda: datetime.now(UTC).isoformat(),
    ) -> AuthenticatedAgentWorkerProcess:
        return AuthenticatedAgentWorkerProcess(
            worker_registry=self.worker_registry,
            heartbeat_store=self.heartbeat_store,
            agent_job_store=self.agent_job_store,
            workspace_root=self.workspace_root,
            runtime_dir=self.runtime_dir,
            software_version=self.software_version,
            model_config=(
                decode_model_profile(self._model_profile_payload)
                if self._model_profile_payload is not None
                else None
            ),
            worker_id=worker_id,
            max_concurrent_jobs=self.max_concurrent_jobs,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            handshake_timeout_seconds=handshake_timeout_seconds,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
            job_claim_lease_seconds=job_claim_lease_seconds,
            job_claim_renewal_interval_seconds=(
                job_claim_renewal_interval_seconds
            ),
            now_provider=now_provider,
        )


def _agent_worker_child_main(
    address: object,
    family: str,
    authkey: bytes,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    interval_seconds: float,
    dispatch_key_bytes: bytes,
    model_execution_enabled: bool,
) -> None:
    connection: Connection | None = None
    bound_job: tuple[
        AgentWorkerJobBinding,
        AgentWorkerRequest,
        AgentJobPayload,
    ] | None = None
    prepared_execution: tuple[
        AgentWorkerModelExecutionPreparation,
        bytes,
        AgentWorkerToolManifest,
    ] | None = None
    try:
        dispatch_key = RuntimePayloadKey.from_bytes(dispatch_key_bytes)
        connection = Client(address=address, family=family, authkey=authkey)
        process_id = os.getpid()
        sequence = 0
        connection.send(
            _child_message(
                "hello",
                nonce=nonce,
                worker_id=worker_id,
                instance_id=instance_id,
                epoch=epoch,
                contract_sha256=contract_sha256,
                process_id=process_id,
                sequence=sequence,
            )
        )
        registration = connection.recv()
        registration_type = (
            str(registration.get("type")) if isinstance(registration, dict) else ""
        )
        _validate_parent_message(
            registration,
            expected_type=registration_type,
            nonce=nonce,
            worker_id=worker_id,
            instance_id=instance_id,
            epoch=epoch,
            contract_sha256=contract_sha256,
        )
        if registration_type == "abort":
            return
        if registration_type != "registered":
            raise AgentWorkerProcessError("Agent Worker 父进程注册确认类型无效。")
        sequence = 2
        connection.send(
            _child_message(
                "running",
                nonce=nonce,
                worker_id=worker_id,
                instance_id=instance_id,
                epoch=epoch,
                contract_sha256=contract_sha256,
                process_id=process_id,
                sequence=sequence,
            )
        )
        while True:
            if connection.poll(interval_seconds):
                control = connection.recv()
                control_type = (
                    str(control.get("type")) if isinstance(control, dict) else ""
                )
                if control_type == "bind_job":
                    if bound_job is not None:
                        raise AgentWorkerProcessError(
                            "Agent Worker 子进程不能重复绑定 Job。"
                        )
                    binding, request, payload = _open_parent_job_message(
                        control,
                        expected_type="bind_job",
                        nonce=nonce,
                        worker_id=worker_id,
                        instance_id=instance_id,
                        epoch=epoch,
                        contract_sha256=contract_sha256,
                        dispatch_key=dispatch_key,
                    )
                    bound_job = (binding, request, payload)
                    sequence += 1
                    connection.send(
                        _child_job_message(
                            "job_bound",
                            nonce=nonce,
                            worker_id=worker_id,
                            instance_id=instance_id,
                            epoch=epoch,
                            contract_sha256=contract_sha256,
                            process_id=process_id,
                            sequence=sequence,
                            binding=binding,
                        )
                    )
                    continue
                if control_type == "prepare_model":
                    if not model_execution_enabled or bound_job is None:
                        raise AgentWorkerProcessError(
                            "Agent Worker 子进程未开放模型执行。"
                        )
                    if prepared_execution is not None:
                        raise AgentWorkerProcessError(
                            "Agent Worker 子进程已有 prepared model execution。"
                        )
                    preparation, profile, manifest = (
                        _open_parent_model_prepare_message(
                        control,
                        nonce=nonce,
                        worker_id=worker_id,
                        instance_id=instance_id,
                        epoch=epoch,
                        contract_sha256=contract_sha256,
                        binding=bound_job[0],
                        request=bound_job[1],
                        dispatch_key=dispatch_key,
                        )
                    )
                    prepared_execution = (preparation, profile, manifest)
                    sequence += 1
                    connection.send(
                        _child_execution_message(
                            "model_prepared",
                            nonce=nonce,
                            worker_id=worker_id,
                            instance_id=instance_id,
                            epoch=epoch,
                            contract_sha256=contract_sha256,
                            process_id=process_id,
                            sequence=sequence,
                            binding=bound_job[0],
                            preparation=preparation,
                        )
                    )
                    continue
                if control_type == "start_model":
                    if bound_job is None or prepared_execution is None:
                        raise AgentWorkerProcessError(
                            "Agent Worker model execution 尚未 prepare。"
                        )
                    preparation, profile, manifest = prepared_execution
                    running_receipt_sha256 = _validate_parent_model_start_message(
                        control,
                        nonce=nonce,
                        worker_id=worker_id,
                        instance_id=instance_id,
                        epoch=epoch,
                        contract_sha256=contract_sha256,
                        binding=bound_job[0],
                        preparation=preparation,
                    )
                    result_value, sequence = _run_child_agent_execution(
                        connection=connection,
                        interval_seconds=interval_seconds,
                        nonce=nonce,
                        worker_id=worker_id,
                        instance_id=instance_id,
                        epoch=epoch,
                        contract_sha256=contract_sha256,
                        process_id=process_id,
                        sequence=sequence,
                        dispatch_key=dispatch_key,
                        binding=bound_job[0],
                        request=bound_job[1],
                        payload=bound_job[2],
                        preparation=preparation,
                        profile=profile,
                        manifest=manifest,
                        running_receipt_sha256=running_receipt_sha256,
                    )
                    terminal_plaintext = encode_model_execution_result(result_value)
                    terminal_envelope = seal_runtime_payload(
                        terminal_plaintext,
                        aad=_model_terminal_aad_values(
                            worker_id=worker_id,
                            instance_id=instance_id,
                            epoch=epoch,
                            contract_sha256=contract_sha256,
                            binding=bound_job[0],
                            preparation=preparation,
                            running_receipt_sha256=running_receipt_sha256,
                        ),
                        key=dispatch_key,
                    )
                    sequence += 1
                    connection.send(
                        _child_execution_message(
                            "model_terminal",
                            nonce=nonce,
                            worker_id=worker_id,
                            instance_id=instance_id,
                            epoch=epoch,
                            contract_sha256=contract_sha256,
                            process_id=process_id,
                            sequence=sequence,
                            binding=bound_job[0],
                            preparation=preparation,
                            extra={
                                "running_receipt_sha256": running_receipt_sha256,
                                "terminal_envelope": terminal_envelope.to_dict(),
                                "terminal_envelope_sha256": (
                                    terminal_envelope.envelope_sha256
                                ),
                            },
                        )
                    )
                    while True:
                        if connection.poll(interval_seconds):
                            commit = connection.recv()
                            result_sha256, terminal_receipt_sha256 = (
                                _validate_parent_model_commit_message(
                                    commit,
                                    nonce=nonce,
                                    worker_id=worker_id,
                                    instance_id=instance_id,
                                    epoch=epoch,
                                    contract_sha256=contract_sha256,
                                    binding=bound_job[0],
                                    preparation=preparation,
                                )
                            )
                            break
                        sequence += 1
                        connection.send(
                            _child_message(
                                "pulse",
                                nonce=nonce,
                                worker_id=worker_id,
                                instance_id=instance_id,
                                epoch=epoch,
                                contract_sha256=contract_sha256,
                                process_id=process_id,
                                sequence=sequence,
                            )
                        )
                    bound_binding = bound_job[0]
                    bound_job = None
                    prepared_execution = None
                    sequence += 1
                    connection.send(
                        _child_execution_message(
                            "model_committed",
                            nonce=nonce,
                            worker_id=worker_id,
                            instance_id=instance_id,
                            epoch=epoch,
                            contract_sha256=contract_sha256,
                            process_id=process_id,
                            sequence=sequence,
                            binding=bound_binding,
                            preparation=preparation,
                            extra={
                                "result_sha256": result_sha256,
                                "terminal_receipt_sha256": (
                                    terminal_receipt_sha256
                                ),
                            },
                        )
                    )
                    continue
                if control_type == "release_job":
                    if bound_job is None:
                        raise AgentWorkerProcessError(
                            "Agent Worker 子进程没有可释放的 Job。"
                        )
                    binding = _validate_parent_job_release_message(
                        control,
                        nonce=nonce,
                        worker_id=worker_id,
                        instance_id=instance_id,
                        epoch=epoch,
                        contract_sha256=contract_sha256,
                        expected_binding=bound_job[0],
                    )
                    bound_job = None
                    prepared_execution = None
                    sequence += 1
                    connection.send(
                        _child_job_message(
                            "job_released",
                            nonce=nonce,
                            worker_id=worker_id,
                            instance_id=instance_id,
                            epoch=epoch,
                            contract_sha256=contract_sha256,
                            process_id=process_id,
                            sequence=sequence,
                            binding=binding,
                        )
                    )
                    continue
                _validate_parent_message(
                    control,
                    expected_type="shutdown",
                    nonce=nonce,
                    worker_id=worker_id,
                    instance_id=instance_id,
                    epoch=epoch,
                    contract_sha256=contract_sha256,
                )
                if bound_job is not None:
                    raise AgentWorkerProcessError(
                        "Agent Worker shutdown 前必须先清除 Job payload。"
                    )
                sequence += 1
                connection.send(
                    _child_message(
                        "draining",
                        nonce=nonce,
                        worker_id=worker_id,
                        instance_id=instance_id,
                        epoch=epoch,
                        contract_sha256=contract_sha256,
                        process_id=process_id,
                        sequence=sequence,
                    )
                )
                sequence += 1
                connection.send(
                    _child_message(
                        "stopped",
                        nonce=nonce,
                        worker_id=worker_id,
                        instance_id=instance_id,
                        epoch=epoch,
                        contract_sha256=contract_sha256,
                        process_id=process_id,
                        sequence=sequence,
                    )
                )
                return
            sequence += 1
            connection.send(
                _child_message(
                    "pulse",
                    nonce=nonce,
                    worker_id=worker_id,
                    instance_id=instance_id,
                    epoch=epoch,
                    contract_sha256=contract_sha256,
                    process_id=process_id,
                    sequence=sequence,
                )
            )
    except (EOFError, OSError, ValueError, AgentWorkerProcessError):
        return
    finally:
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.close()


def _parent_message(
    message_type: str,
    *,
    nonce: str,
    contract: WorkerContract,
) -> dict[str, object]:
    return {
        "type": message_type,
        "protocol_version": _PROTOCOL_VERSION,
        "nonce": nonce,
        "worker_id": contract.worker_id,
        "instance_id": contract.instance_id,
        "epoch": contract.epoch,
        "contract_sha256": contract.contract_sha256,
    }


def _parent_job_message(
    message_type: str,
    *,
    nonce: str,
    contract: WorkerContract,
    binding: AgentWorkerJobBinding,
    envelope: PayloadEnvelope | None = None,
) -> dict[str, object]:
    if message_type not in {"bind_job", "release_job"}:
        raise AgentWorkerProcessError("Agent Worker Job 控制消息类型无效。")
    message = {
        **_parent_message(message_type, nonce=nonce, contract=contract),
        **_job_binding_message_fields(binding),
    }
    if message_type == "bind_job":
        if not isinstance(envelope, PayloadEnvelope):
            raise AgentWorkerProcessError("Agent Worker bind 缺少加密 payload。")
        message["dispatch_envelope"] = envelope.to_dict()
    elif envelope is not None:
        raise AgentWorkerProcessError("Agent Worker release 不得携带 payload。")
    return message


def _preparation_message_fields(
    preparation: AgentWorkerModelExecutionPreparation,
) -> dict[str, object]:
    return {
        "execution_id": preparation.execution_id,
        "execution_job_id": preparation.job_id,
        "execution_request_sha256": preparation.request_sha256,
        "execution_claim_epoch": preparation.claim_epoch,
        "model_profile_sha256": preparation.model_profile_sha256,
        "tool_manifest_sha256": preparation.tool_manifest_sha256,
    }


def _preparation_from_message(
    message: dict[str, object],
) -> AgentWorkerModelExecutionPreparation:
    try:
        return AgentWorkerModelExecutionPreparation(
            execution_id=str(message["execution_id"]),
            job_id=str(message["execution_job_id"]),
            request_sha256=str(message["execution_request_sha256"]),
            claim_epoch=int(message["execution_claim_epoch"]),
            model_profile_sha256=str(message["model_profile_sha256"]),
            tool_manifest_sha256=str(message["tool_manifest_sha256"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentWorkerProcessError(
            "Agent Worker model preparation 字段无效。"
        ) from exc


def _parent_execution_prepare_message(
    *,
    nonce: str,
    contract: WorkerContract,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    profile_envelope: PayloadEnvelope,
    manifest_envelope: PayloadEnvelope,
) -> dict[str, object]:
    return {
        **_parent_message("prepare_model", nonce=nonce, contract=contract),
        **_job_binding_message_fields(binding),
        **_preparation_message_fields(preparation),
        "model_profile_envelope": profile_envelope.to_dict(),
        "model_profile_envelope_sha256": profile_envelope.envelope_sha256,
        "tool_manifest_envelope": manifest_envelope.to_dict(),
        "tool_manifest_envelope_sha256": manifest_envelope.envelope_sha256,
    }


def _open_parent_model_prepare_message(
    message: object,
    *,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    binding: AgentWorkerJobBinding,
    request: AgentWorkerRequest,
    dispatch_key: RuntimePayloadKey,
) -> tuple[
    AgentWorkerModelExecutionPreparation,
    bytes,
    AgentWorkerToolManifest,
]:
    if not isinstance(message, dict):
        raise AgentWorkerProcessError("Agent Worker model prepare 消息形状无效。")
    preparation = _preparation_from_message(message)
    if (
        preparation.job_id != binding.job_id
        or preparation.request_sha256 != binding.request_sha256
        or preparation.claim_epoch != binding.claim_epoch
        or request.request_sha256 != binding.request_sha256
    ):
        raise AgentWorkerProcessError("Agent Worker model prepare fence 无效。")
    try:
        envelope = PayloadEnvelope.from_dict(message["model_profile_envelope"])  # type: ignore[arg-type]
        manifest_envelope = PayloadEnvelope.from_dict(message["tool_manifest_envelope"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentWorkerProcessError(
            "Agent Worker model profile envelope 无效。"
        ) from exc
    expected = {
        "type": "prepare_model",
        "protocol_version": _PROTOCOL_VERSION,
        "nonce": nonce,
        "worker_id": worker_id,
        "instance_id": instance_id,
        "epoch": epoch,
        "contract_sha256": contract_sha256,
        **_job_binding_message_fields(binding),
        **_preparation_message_fields(preparation),
        "model_profile_envelope": envelope.to_dict(),
        "model_profile_envelope_sha256": envelope.envelope_sha256,
        "tool_manifest_envelope": manifest_envelope.to_dict(),
        "tool_manifest_envelope_sha256": manifest_envelope.envelope_sha256,
    }
    if not _secure_exact_message_matches(message, expected):
        raise AgentWorkerProcessError("Agent Worker model prepare 消息认证失败。")
    try:
        profile = open_runtime_payload(
            envelope,
            aad=_model_profile_aad_values(
                worker_id=worker_id,
                instance_id=instance_id,
                epoch=epoch,
                contract_sha256=contract_sha256,
                binding=binding,
                preparation=preparation,
            ),
            key=dispatch_key,
        )
        decode_model_profile(profile)
        manifest_plaintext = open_runtime_payload(
            manifest_envelope,
            aad=_tool_manifest_aad_values(
                worker_id=worker_id,
                instance_id=instance_id,
                epoch=epoch,
                contract_sha256=contract_sha256,
                binding=binding,
                preparation=preparation,
            ),
            key=dispatch_key,
        )
        manifest = decode_tool_manifest(manifest_plaintext)
    except (PayloadEnvelopeError, TypeError, ValueError) as exc:
        raise AgentWorkerProcessError(
            "Agent Worker model profile 无法认证。"
        ) from exc
    if not hmac.compare_digest(
        model_profile_sha256(profile),
        preparation.model_profile_sha256,
    ):
        raise AgentWorkerProcessError("Agent Worker model profile digest 无效。")
    if (
        not hmac.compare_digest(
            manifest.manifest_sha256,
            preparation.tool_manifest_sha256,
        )
        or manifest.tool_scope != request.tool_scope
    ):
        raise AgentWorkerProcessError("Agent Worker tool manifest fence 无效。")
    return preparation, profile, manifest


def _parent_execution_start_message(
    *,
    nonce: str,
    contract: WorkerContract,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    running_receipt_sha256: str,
) -> dict[str, object]:
    if not _SHA256_RE.fullmatch(running_receipt_sha256):
        raise AgentWorkerProcessError("running_receipt_sha256 格式无效。")
    return {
        **_parent_message("start_model", nonce=nonce, contract=contract),
        **_job_binding_message_fields(binding),
        **_preparation_message_fields(preparation),
        "running_receipt_sha256": running_receipt_sha256,
    }


def _validate_parent_model_start_message(
    message: object,
    *,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
) -> str:
    if not isinstance(message, dict):
        raise AgentWorkerProcessError("Agent Worker model start 消息形状无效。")
    running_receipt_sha256 = str(message.get("running_receipt_sha256", ""))
    if not _SHA256_RE.fullmatch(running_receipt_sha256):
        raise AgentWorkerProcessError("Agent Worker model running receipt 无效。")
    expected = {
        "type": "start_model",
        "protocol_version": _PROTOCOL_VERSION,
        "nonce": nonce,
        "worker_id": worker_id,
        "instance_id": instance_id,
        "epoch": epoch,
        "contract_sha256": contract_sha256,
        **_job_binding_message_fields(binding),
        **_preparation_message_fields(preparation),
        "running_receipt_sha256": running_receipt_sha256,
    }
    if not _secure_exact_message_matches(message, expected):
        raise AgentWorkerProcessError("Agent Worker model start 消息认证失败。")
    return running_receipt_sha256


def _parent_execution_commit_message(
    *,
    nonce: str,
    contract: WorkerContract,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    result_sha256: str,
    terminal_receipt_sha256: str,
) -> dict[str, object]:
    for value, field_name in (
        (result_sha256, "result_sha256"),
        (terminal_receipt_sha256, "terminal_receipt_sha256"),
    ):
        if not _SHA256_RE.fullmatch(value):
            raise AgentWorkerProcessError(f"{field_name} 格式无效。")
    return {
        **_parent_message("commit_model", nonce=nonce, contract=contract),
        **_job_binding_message_fields(binding),
        **_preparation_message_fields(preparation),
        "result_sha256": result_sha256,
        "terminal_receipt_sha256": terminal_receipt_sha256,
    }


def _validate_parent_model_commit_message(
    message: object,
    *,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
) -> tuple[str, str]:
    if not isinstance(message, dict):
        raise AgentWorkerProcessError("Agent Worker model commit 消息形状无效。")
    result_sha256 = str(message.get("result_sha256", ""))
    terminal_receipt_sha256 = str(message.get("terminal_receipt_sha256", ""))
    expected = {
        "type": "commit_model",
        "protocol_version": _PROTOCOL_VERSION,
        "nonce": nonce,
        "worker_id": worker_id,
        "instance_id": instance_id,
        "epoch": epoch,
        "contract_sha256": contract_sha256,
        **_job_binding_message_fields(binding),
        **_preparation_message_fields(preparation),
        "result_sha256": result_sha256,
        "terminal_receipt_sha256": terminal_receipt_sha256,
    }
    if (
        not _SHA256_RE.fullmatch(result_sha256)
        or not _SHA256_RE.fullmatch(terminal_receipt_sha256)
        or not _secure_exact_message_matches(message, expected)
    ):
        raise AgentWorkerProcessError("Agent Worker model commit 消息认证失败。")
    return result_sha256, terminal_receipt_sha256


def _parent_tool_result_message(
    *,
    nonce: str,
    contract: WorkerContract,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    running_receipt_sha256: str,
    call_batch: AgentWorkerToolCallBatch,
    result_batch: AgentWorkerToolResultBatch,
    result_envelope: PayloadEnvelope,
) -> dict[str, object]:
    if not isinstance(result_batch, AgentWorkerToolResultBatch):
        raise TypeError("result_batch 类型无效。")
    return {
        **_parent_message("tool_result", nonce=nonce, contract=contract),
        **_job_binding_message_fields(binding),
        **_preparation_message_fields(preparation),
        "running_receipt_sha256": running_receipt_sha256,
        "tool_turn": call_batch.turn,
        "tool_call_batch_sha256": call_batch.batch_sha256,
        "tool_result_batch_sha256": result_batch.batch_sha256,
        "tool_result_envelope": result_envelope.to_dict(),
        "tool_result_envelope_sha256": result_envelope.envelope_sha256,
    }


def _child_tool_request_message(
    *,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    process_id: int,
    sequence: int,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    running_receipt_sha256: str,
    call_batch: AgentWorkerToolCallBatch,
    call_envelope: PayloadEnvelope,
) -> dict[str, object]:
    return _child_execution_message(
        "tool_request",
        nonce=nonce,
        worker_id=worker_id,
        instance_id=instance_id,
        epoch=epoch,
        contract_sha256=contract_sha256,
        process_id=process_id,
        sequence=sequence,
        binding=binding,
        preparation=preparation,
        extra={
            "running_receipt_sha256": running_receipt_sha256,
            "tool_turn": call_batch.turn,
            "tool_call_batch_sha256": call_batch.batch_sha256,
            "tool_call_envelope": call_envelope.to_dict(),
            "tool_call_envelope_sha256": call_envelope.envelope_sha256,
        },
    )


def _open_child_tool_request_message(
    message: object,
    *,
    nonce: str,
    contract: WorkerContract,
    process_id: int | None,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    running_receipt_sha256: str,
    tool_scope: tuple[str, ...],
    dispatch_key: RuntimePayloadKey,
) -> AgentWorkerToolCallBatch:
    if not isinstance(message, dict) or process_id is None or process_id < 1:
        raise AgentWorkerProcessError("Agent Worker tool request 形状无效。")
    try:
        envelope = PayloadEnvelope.from_dict(message["tool_call_envelope"])  # type: ignore[arg-type]
        sequence = int(message["sequence"])
        turn = int(message["tool_turn"])
        batch_sha256 = str(message["tool_call_batch_sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentWorkerProcessError(
            "Agent Worker tool request envelope 无效。"
        ) from exc
    expected = _child_execution_message(
        "tool_request",
        nonce=nonce,
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        process_id=process_id,
        sequence=sequence,
        binding=binding,
        preparation=preparation,
        extra={
            "running_receipt_sha256": running_receipt_sha256,
            "tool_turn": turn,
            "tool_call_batch_sha256": batch_sha256,
            "tool_call_envelope": envelope.to_dict(),
            "tool_call_envelope_sha256": envelope.envelope_sha256,
        },
    )
    if not _secure_exact_message_matches(message, expected):
        raise AgentWorkerProcessError("Agent Worker tool request 认证失败。")
    try:
        plaintext = open_runtime_payload(
            envelope,
            aad=_tool_call_aad_identity(
                contract=contract,
                binding=binding,
                preparation=preparation,
                running_receipt_sha256=running_receipt_sha256,
                turn=turn,
                batch_sha256=batch_sha256,
            ),
            key=dispatch_key,
        )
        batch = decode_tool_call_batch(plaintext)
    except (PayloadEnvelopeError, TypeError, ValueError) as exc:
        raise AgentWorkerProcessError(
            "Agent Worker tool request payload 无法认证。"
        ) from exc
    if (
        batch.execution_id != preparation.execution_id
        or batch.turn != turn
        or not hmac.compare_digest(batch.batch_sha256, batch_sha256)
        or any(call.name not in tool_scope for call in batch.calls)
    ):
        raise AgentWorkerProcessError("Agent Worker tool request scope fence 无效。")
    return batch


def _open_parent_tool_result_message(
    message: object,
    *,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    running_receipt_sha256: str,
    call_batch: AgentWorkerToolCallBatch,
    dispatch_key: RuntimePayloadKey,
) -> AgentWorkerToolResultBatch:
    if not isinstance(message, dict):
        raise AgentWorkerProcessError("Agent Worker tool result 形状无效。")
    try:
        envelope = PayloadEnvelope.from_dict(message["tool_result_envelope"])  # type: ignore[arg-type]
        result_batch_sha256 = str(message["tool_result_batch_sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentWorkerProcessError(
            "Agent Worker tool result envelope 无效。"
        ) from exc
    expected = {
        "type": "tool_result",
        "protocol_version": _PROTOCOL_VERSION,
        "nonce": nonce,
        "worker_id": worker_id,
        "instance_id": instance_id,
        "epoch": epoch,
        "contract_sha256": contract_sha256,
        **_job_binding_message_fields(binding),
        **_preparation_message_fields(preparation),
        "running_receipt_sha256": running_receipt_sha256,
        "tool_turn": call_batch.turn,
        "tool_call_batch_sha256": call_batch.batch_sha256,
        "tool_result_batch_sha256": result_batch_sha256,
        "tool_result_envelope": envelope.to_dict(),
        "tool_result_envelope_sha256": envelope.envelope_sha256,
    }
    if not _secure_exact_message_matches(message, expected):
        raise AgentWorkerProcessError("Agent Worker tool result 认证失败。")
    try:
        plaintext = open_runtime_payload(
            envelope,
            aad=_tool_result_aad_identity_values(
                worker_id=worker_id,
                instance_id=instance_id,
                epoch=epoch,
                contract_sha256=contract_sha256,
                binding=binding,
                preparation=preparation,
                running_receipt_sha256=running_receipt_sha256,
                call_batch=call_batch,
                result_batch_sha256=result_batch_sha256,
            ),
            key=dispatch_key,
        )
        result_batch = decode_tool_result_batch(plaintext)
    except (PayloadEnvelopeError, TypeError, ValueError) as exc:
        raise AgentWorkerProcessError(
            "Agent Worker tool result payload 无法认证。"
        ) from exc
    if (
        result_batch.execution_id != preparation.execution_id
        or result_batch.turn != call_batch.turn
        or not hmac.compare_digest(
            result_batch.call_batch_sha256,
            call_batch.batch_sha256,
        )
        or not hmac.compare_digest(
            result_batch.batch_sha256,
            result_batch_sha256,
        )
        or tuple(result.call_id for result in result_batch.results)
        != tuple(call.id for call in call_batch.calls)
    ):
        raise AgentWorkerProcessError("Agent Worker tool result fence 无效。")
    return result_batch


def _child_execution_message(
    message_type: str,
    *,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    process_id: int,
    sequence: int,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    if message_type not in {
        "model_prepared",
        "tool_request",
        "model_terminal",
        "model_committed",
    }:
        raise AgentWorkerProcessError("Agent Worker model child 消息类型无效。")
    return {
        **_child_message(
            message_type,
            nonce=nonce,
            worker_id=worker_id,
            instance_id=instance_id,
            epoch=epoch,
            contract_sha256=contract_sha256,
            process_id=process_id,
            sequence=sequence,
        ),
        **_job_binding_message_fields(binding),
        **_preparation_message_fields(preparation),
        **(extra or {}),
    }


def _validate_child_execution_identity(
    message: object,
    *,
    expected_type: str,
    nonce: str,
    contract: WorkerContract,
    process_id: int | None,
    expected_sequence: int,
) -> None:
    if not isinstance(message, dict) or process_id is None or process_id < 1:
        raise AgentWorkerProcessError("Agent Worker model child identity 无效。")
    expected = _child_message(
        expected_type,
        nonce=nonce,
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        process_id=process_id,
        sequence=expected_sequence,
    )
    if any(
        not hmac.compare_digest(str(message.get(key)), str(value))
        for key, value in expected.items()
    ):
        raise AgentWorkerProcessError("Agent Worker model child identity 认证失败。")


def _validate_child_execution_message(
    message: object,
    *,
    expected_type: str,
    nonce: str,
    contract: WorkerContract,
    process_id: int | None,
    expected_sequence: int,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
) -> None:
    if process_id is None or process_id < 1:
        raise AgentWorkerProcessError("Agent Worker model child PID 无效。")
    expected = _child_execution_message(
        expected_type,
        nonce=nonce,
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        process_id=process_id,
        sequence=expected_sequence,
        binding=binding,
        preparation=preparation,
    )
    if not _secure_exact_message_matches(message, expected):
        raise AgentWorkerProcessError("Agent Worker model child 回执认证失败。")


def _open_child_model_terminal_message(
    message: object,
    *,
    nonce: str,
    contract: WorkerContract,
    process_id: int | None,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    running_receipt_sha256: str,
    dispatch_key: RuntimePayloadKey,
) -> AgentWorkerModelExecutionResult:
    if not isinstance(message, dict) or process_id is None or process_id < 1:
        raise AgentWorkerProcessError("Agent Worker model terminal 形状无效。")
    try:
        envelope = PayloadEnvelope.from_dict(message["terminal_envelope"])  # type: ignore[arg-type]
        sequence = int(message["sequence"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentWorkerProcessError("Agent Worker model terminal envelope 无效。") from exc
    expected = _child_execution_message(
        "model_terminal",
        nonce=nonce,
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        process_id=process_id,
        sequence=sequence,
        binding=binding,
        preparation=preparation,
        extra={
            "running_receipt_sha256": running_receipt_sha256,
            "terminal_envelope": envelope.to_dict(),
            "terminal_envelope_sha256": envelope.envelope_sha256,
        },
    )
    if not _secure_exact_message_matches(message, expected):
        raise AgentWorkerProcessError("Agent Worker model terminal 认证失败。")
    try:
        plaintext = open_runtime_payload(
            envelope,
            aad=_model_terminal_aad(
                contract=contract,
                binding=binding,
                preparation=preparation,
                running_receipt_sha256=running_receipt_sha256,
            ),
            key=dispatch_key,
        )
        result = decode_model_execution_result(plaintext)
    except (PayloadEnvelopeError, TypeError, ValueError) as exc:
        raise AgentWorkerProcessError(
            "Agent Worker model terminal payload 无法认证。"
        ) from exc
    if (
        result.execution_id != preparation.execution_id
        or result.request_sha256 != binding.request_sha256
    ):
        raise AgentWorkerProcessError("Agent Worker model terminal result fence 无效。")
    return result


def _validate_child_execution_commit_message(
    message: object,
    *,
    nonce: str,
    contract: WorkerContract,
    process_id: int | None,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    result_sha256: str,
    terminal_receipt_sha256: str,
) -> None:
    if not isinstance(message, dict) or process_id is None or process_id < 1:
        raise AgentWorkerProcessError("Agent Worker model committed 形状无效。")
    try:
        sequence = int(message["sequence"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentWorkerProcessError("Agent Worker model committed sequence 无效。") from exc
    expected = _child_execution_message(
        "model_committed",
        nonce=nonce,
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        process_id=process_id,
        sequence=sequence,
        binding=binding,
        preparation=preparation,
        extra={
            "result_sha256": result_sha256,
            "terminal_receipt_sha256": terminal_receipt_sha256,
        },
    )
    if not _secure_exact_message_matches(message, expected):
        raise AgentWorkerProcessError("Agent Worker model committed 回执认证失败。")


def _run_model_turn_thread(
    result_queue: queue.Queue[AgentWorkerModelTurnResult | BaseException],
    preparation: AgentWorkerModelExecutionPreparation,
    request: AgentWorkerRequest,
    profile: bytes,
    messages: list[dict[str, object]],
    tools: list[dict[str, object]] | None,
    timeout_milliseconds: int,
) -> None:
    try:
        result = asyncio.run(
            execute_model_turn(
                preparation=preparation,
                request=request,
                model_profile=profile,
                messages=messages,
                tools=tools,
                timeout_milliseconds=timeout_milliseconds,
            )
        )
    except BaseException as exc:
        result_queue.put(exc)
    else:
        result_queue.put(result)


def _run_child_agent_execution(
    *,
    connection: Connection,
    interval_seconds: float,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    process_id: int,
    sequence: int,
    dispatch_key: RuntimePayloadKey,
    binding: AgentWorkerJobBinding,
    request: AgentWorkerRequest,
    payload: AgentJobPayload,
    preparation: AgentWorkerModelExecutionPreparation,
    profile: bytes,
    manifest: AgentWorkerToolManifest,
    running_receipt_sha256: str,
) -> tuple[AgentWorkerModelExecutionResult, int]:
    messages: list[dict[str, object]] = []
    if payload.context:
        messages.append({"role": "system", "content": payload.context})
    messages.append({"role": "user", "content": payload.task})
    tools = list(manifest.tools) if manifest.tools else None
    deadline = time.monotonic() + request.timeout_milliseconds / 1000
    total_tokens = 0
    total_cost_microusd = 0
    model = ""
    provider_model = ""
    response_id_sha256 = ""
    executed_tool_signatures: set[str] = set()

    for turn in range(1, request.max_turns + 1):
        remaining_milliseconds = max(0, round((deadline - time.monotonic()) * 1000))
        if remaining_milliseconds < 1:
            return (
                _child_terminal_result(
                    preparation=preparation,
                    status=AgentWorkerModelExecutionStatus.TIMEOUT,
                    error="独立 Agent 总执行时间已耗尽，未获得可信终态响应。",
                    error_code="agent_model_timeout",
                    total_tokens=total_tokens,
                    total_cost_microusd=total_cost_microusd,
                    turns=max(0, turn - 1),
                    model=model,
                    provider_model=provider_model,
                    provider_response_id_sha256=response_id_sha256,
                ),
                sequence,
            )
        result_queue: queue.Queue[AgentWorkerModelTurnResult | BaseException] = (
            queue.Queue(maxsize=1)
        )
        execution_thread = threading.Thread(
            target=_run_model_turn_thread,
            args=(
                result_queue,
                preparation,
                request,
                profile,
                messages,
                tools,
                min(remaining_milliseconds, request.timeout_milliseconds),
            ),
            name=f"naumi-agent-model-{preparation.execution_id[-12:]}-{turn}",
            daemon=True,
        )
        execution_thread.start()
        while execution_thread.is_alive():
            if connection.poll(interval_seconds):
                raise AgentWorkerProcessError(
                    "model execution 期间收到未授权控制消息。"
                )
            sequence = _send_child_pulse(
                connection=connection,
                nonce=nonce,
                worker_id=worker_id,
                instance_id=instance_id,
                epoch=epoch,
                contract_sha256=contract_sha256,
                process_id=process_id,
                sequence=sequence,
            )
        execution_thread.join(timeout=1)
        result_value = result_queue.get_nowait()
        if isinstance(result_value, BaseException):
            raise AgentWorkerProcessError(
                "Agent Worker model execution kernel 失败。"
            )
        try:
            total_tokens = _bounded_sum(
                total_tokens,
                result_value.total_tokens,
                maximum=2**63 - 1,
                field_name="total_tokens",
            )
            total_cost_microusd = _bounded_sum(
                total_cost_microusd,
                result_value.total_cost_microusd,
                maximum=10**18,
                field_name="total_cost_microusd",
            )
        except ValueError:
            return (
                _child_terminal_result(
                    preparation=preparation,
                    status=AgentWorkerModelExecutionStatus.ERROR,
                    error="Provider 累计用量超过安全范围，结果未发布。",
                    error_code="agent_model_usage_invalid",
                    total_tokens=0,
                    total_cost_microusd=0,
                    turns=turn,
                ),
                sequence,
            )
        model = result_value.model or model
        provider_model = result_value.provider_model or provider_model
        response_id_sha256 = (
            result_value.provider_response_id_sha256 or response_id_sha256
        )
        if result_value.status is not AgentWorkerModelExecutionStatus.COMPLETED:
            return (
                _child_terminal_result(
                    preparation=preparation,
                    status=result_value.status,
                    error=result_value.error,
                    error_code=result_value.error_code,
                    total_tokens=total_tokens,
                    total_cost_microusd=total_cost_microusd,
                    turns=turn,
                    model=model,
                    provider_model=provider_model,
                    provider_response_id_sha256=response_id_sha256,
                ),
                sequence,
            )
        if (
            request.max_cost_microusd is not None
            and total_cost_microusd > request.max_cost_microusd
        ):
            return (
                _child_terminal_result(
                    preparation=preparation,
                    status=AgentWorkerModelExecutionStatus.ERROR,
                    error="独立 Agent 模型调用已超过本次预算，响应内容未发布。",
                    error_code="agent_model_budget_exceeded",
                    total_tokens=total_tokens,
                    total_cost_microusd=total_cost_microusd,
                    turns=turn,
                    model=model,
                    provider_model=provider_model,
                    provider_response_id_sha256=response_id_sha256,
                ),
                sequence,
            )
        if not result_value.tool_calls:
            return (
                AgentWorkerModelExecutionResult(
                    schema_version=1,
                    execution_id=preparation.execution_id,
                    request_sha256=request.request_sha256,
                    status=AgentWorkerModelExecutionStatus.COMPLETED,
                    response=result_value.response,
                    error="",
                    error_code="",
                    total_tokens=total_tokens,
                    total_cost_microusd=total_cost_microusd,
                    turns=turn,
                    model=model,
                    provider_model=provider_model,
                    provider_response_id_sha256=response_id_sha256,
                ),
                sequence,
            )
        if turn == request.max_turns:
            return (
                _child_terminal_result(
                    preparation=preparation,
                    status=AgentWorkerModelExecutionStatus.MAX_TURNS,
                    error="独立 Agent 已达到最大轮数，未继续执行待处理工具。",
                    error_code="agent_model_max_turns",
                    total_tokens=total_tokens,
                    total_cost_microusd=total_cost_microusd,
                    turns=turn,
                    model=model,
                    provider_model=provider_model,
                    provider_response_id_sha256=response_id_sha256,
                ),
                sequence,
            )
        try:
            call_batch = issue_tool_call_batch(
                execution_id=preparation.execution_id,
                turn=turn,
                raw_calls=list(result_value.tool_calls),
                tool_scope=manifest.tool_scope,
            )
        except (TypeError, ValueError):
            return (
                _child_terminal_result(
                    preparation=preparation,
                    status=AgentWorkerModelExecutionStatus.ERROR,
                    error="Provider 返回了无效或越权工具调用，未执行任何工具。",
                    error_code="agent_model_unexpected_tool_call",
                    total_tokens=total_tokens,
                    total_cost_microusd=total_cost_microusd,
                    turns=turn,
                    model=model,
                    provider_model=provider_model,
                    provider_response_id_sha256=response_id_sha256,
                ),
                sequence,
            )
        call_signatures = {tool_call_signature(call) for call in call_batch.calls}
        if executed_tool_signatures.intersection(call_signatures):
            return (
                _child_terminal_result(
                    preparation=preparation,
                    status=AgentWorkerModelExecutionStatus.ERROR,
                    error="Provider 重复请求了相同工具操作，为避免重复副作用已停止。",
                    error_code="agent_model_repeated_tool_call",
                    total_tokens=total_tokens,
                    total_cost_microusd=total_cost_microusd,
                    turns=turn,
                    model=model,
                    provider_model=provider_model,
                    provider_response_id_sha256=response_id_sha256,
                ),
                sequence,
            )
        executed_tool_signatures.update(call_signatures)
        messages.append(
            {
                "role": "assistant",
                "content": result_value.response or None,
                "tool_calls": list(result_value.tool_calls),
            }
        )
        call_envelope = seal_runtime_payload(
            encode_tool_call_batch(call_batch),
            aad=_tool_call_aad_values(
                worker_id=worker_id,
                instance_id=instance_id,
                epoch=epoch,
                contract_sha256=contract_sha256,
                binding=binding,
                preparation=preparation,
                running_receipt_sha256=running_receipt_sha256,
                call_batch=call_batch,
            ),
            key=dispatch_key,
        )
        sequence += 1
        connection.send(
            _child_tool_request_message(
                nonce=nonce,
                worker_id=worker_id,
                instance_id=instance_id,
                epoch=epoch,
                contract_sha256=contract_sha256,
                process_id=process_id,
                sequence=sequence,
                binding=binding,
                preparation=preparation,
                running_receipt_sha256=running_receipt_sha256,
                call_batch=call_batch,
                call_envelope=call_envelope,
            )
        )
        result_batch, sequence = _wait_for_parent_tool_result(
            connection=connection,
            interval_seconds=interval_seconds,
            nonce=nonce,
            worker_id=worker_id,
            instance_id=instance_id,
            epoch=epoch,
            contract_sha256=contract_sha256,
            process_id=process_id,
            sequence=sequence,
            dispatch_key=dispatch_key,
            binding=binding,
            preparation=preparation,
            running_receipt_sha256=running_receipt_sha256,
            call_batch=call_batch,
        )
        for result in result_batch.results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.content,
                }
            )
    raise AgentWorkerProcessError("Agent Worker model loop 未产生终态。")


def _child_terminal_result(
    *,
    preparation: AgentWorkerModelExecutionPreparation,
    status: AgentWorkerModelExecutionStatus,
    error: str,
    error_code: str,
    total_tokens: int,
    total_cost_microusd: int,
    turns: int,
    model: str = "",
    provider_model: str = "",
    provider_response_id_sha256: str = "",
) -> AgentWorkerModelExecutionResult:
    return AgentWorkerModelExecutionResult(
        schema_version=1,
        execution_id=preparation.execution_id,
        request_sha256=preparation.request_sha256,
        status=status,
        response="",
        error=error,
        error_code=error_code,
        total_tokens=total_tokens,
        total_cost_microusd=total_cost_microusd,
        turns=turns,
        model=model,
        provider_model=provider_model,
        provider_response_id_sha256=provider_response_id_sha256,
    )


def _bounded_sum(left: int, right: int, *, maximum: int, field_name: str) -> int:
    value = left + right
    if value < 0 or value > maximum:
        raise ValueError(f"{field_name} 累计值无效。")
    return value


def _send_child_pulse(
    *,
    connection: Connection,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    process_id: int,
    sequence: int,
) -> int:
    next_sequence = sequence + 1
    connection.send(
        _child_message(
            "pulse",
            nonce=nonce,
            worker_id=worker_id,
            instance_id=instance_id,
            epoch=epoch,
            contract_sha256=contract_sha256,
            process_id=process_id,
            sequence=next_sequence,
        )
    )
    return next_sequence


def _wait_for_parent_tool_result(
    *,
    connection: Connection,
    interval_seconds: float,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    process_id: int,
    sequence: int,
    dispatch_key: RuntimePayloadKey,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    running_receipt_sha256: str,
    call_batch: AgentWorkerToolCallBatch,
) -> tuple[AgentWorkerToolResultBatch, int]:
    while True:
        if connection.poll(interval_seconds):
            message = connection.recv()
            result = _open_parent_tool_result_message(
                message,
                nonce=nonce,
                worker_id=worker_id,
                instance_id=instance_id,
                epoch=epoch,
                contract_sha256=contract_sha256,
                binding=binding,
                preparation=preparation,
                running_receipt_sha256=running_receipt_sha256,
                call_batch=call_batch,
                dispatch_key=dispatch_key,
            )
            return result, sequence
        sequence = _send_child_pulse(
            connection=connection,
            nonce=nonce,
            worker_id=worker_id,
            instance_id=instance_id,
            epoch=epoch,
            contract_sha256=contract_sha256,
            process_id=process_id,
            sequence=sequence,
        )


def _child_job_message(
    message_type: str,
    *,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    process_id: int,
    sequence: int,
    binding: AgentWorkerJobBinding,
) -> dict[str, object]:
    if message_type not in {"job_bound", "job_released"}:
        raise AgentWorkerProcessError("Agent Worker Job 回执类型无效。")
    return {
        **_child_message(
            message_type,
            nonce=nonce,
            worker_id=worker_id,
            instance_id=instance_id,
            epoch=epoch,
            contract_sha256=contract_sha256,
            process_id=process_id,
            sequence=sequence,
        ),
        **_job_binding_message_fields(binding),
    }


def _open_parent_job_message(
    message: object,
    *,
    expected_type: str,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    dispatch_key: RuntimePayloadKey,
) -> tuple[AgentWorkerJobBinding, AgentWorkerRequest, AgentJobPayload]:
    if expected_type != "bind_job" or not isinstance(message, dict):
        raise AgentWorkerProcessError("Agent Worker bind 消息形状无效。")
    binding = _binding_from_message(message)
    expected_owner = _job_owner_id_values(
        worker_id=worker_id,
        instance_id=instance_id,
        epoch=epoch,
        contract_sha256=contract_sha256,
    )
    expected_reservation = _job_reservation_id_values(
        worker_id=worker_id,
        instance_id=instance_id,
        epoch=epoch,
        contract_sha256=contract_sha256,
        job_id=binding.job_id,
    )
    if not hmac.compare_digest(binding.owner_id, expected_owner):
        raise AgentWorkerProcessError("Agent Worker Job owner identity 无效。")
    if not hmac.compare_digest(binding.reservation_id, expected_reservation):
        raise AgentWorkerProcessError("Agent Worker Job reservation identity 无效。")
    envelope_value = message.get("dispatch_envelope")
    try:
        envelope = PayloadEnvelope.from_dict(envelope_value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise AgentWorkerProcessError("Agent Worker dispatch envelope 无效。") from exc
    expected = {
        "type": expected_type,
        "protocol_version": _PROTOCOL_VERSION,
        "nonce": nonce,
        "worker_id": worker_id,
        "instance_id": instance_id,
        "epoch": epoch,
        "contract_sha256": contract_sha256,
        **_job_binding_message_fields(binding),
        "dispatch_envelope": envelope.to_dict(),
    }
    if not _secure_exact_message_matches(message, expected):
        raise AgentWorkerProcessError("Agent Worker Job bind 消息认证失败。")
    if not hmac.compare_digest(
        binding.dispatch_envelope_sha256,
        envelope.envelope_sha256,
    ):
        raise AgentWorkerProcessError("Agent Worker dispatch envelope fence 无效。")
    aad = _job_dispatch_aad_values(
        worker_id=worker_id,
        instance_id=instance_id,
        epoch=epoch,
        contract_sha256=contract_sha256,
        job_id=binding.job_id,
        request_sha256=binding.request_sha256,
        owner_id=binding.owner_id,
        claim_epoch=binding.claim_epoch,
        claim_expires_at=binding.claim_expires_at,
        reservation_id=binding.reservation_id,
    )
    try:
        plaintext = open_runtime_payload(envelope, aad=aad, key=dispatch_key)
        request, payload = decode_agent_job_dispatch_payload(plaintext)
    except (PayloadEnvelopeError, TypeError, ValueError) as exc:
        raise AgentWorkerProcessError(
            "Agent Worker dispatch payload 无法认证。"
        ) from exc
    if not hmac.compare_digest(request.request_sha256, binding.request_sha256):
        raise AgentWorkerProcessError("Agent Worker dispatch request fence 无效。")
    return binding, request, payload


def _validate_parent_job_release_message(
    message: object,
    *,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    expected_binding: AgentWorkerJobBinding,
) -> AgentWorkerJobBinding:
    expected = {
        "type": "release_job",
        "protocol_version": _PROTOCOL_VERSION,
        "nonce": nonce,
        "worker_id": worker_id,
        "instance_id": instance_id,
        "epoch": epoch,
        "contract_sha256": contract_sha256,
        **_job_binding_message_fields(expected_binding),
    }
    if not _secure_exact_message_matches(message, expected):
        raise AgentWorkerProcessError("Agent Worker Job release 消息认证失败。")
    return expected_binding


def _validate_child_job_message(
    message: object,
    *,
    expected_type: str,
    nonce: str,
    contract: WorkerContract,
    process_id: int | None,
    expected_sequence: int,
    binding: AgentWorkerJobBinding,
) -> None:
    if process_id is None or process_id <= 0:
        raise AgentWorkerProcessError("Agent Worker 子进程 PID 不可信。")
    expected = _child_job_message(
        expected_type,
        nonce=nonce,
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        process_id=process_id,
        sequence=expected_sequence,
        binding=binding,
    )
    if not _secure_exact_message_matches(message, expected):
        raise AgentWorkerProcessError("Agent Worker Job 回执认证失败。")


def _binding_from_message(message: dict[str, object]) -> AgentWorkerJobBinding:
    try:
        binding = AgentWorkerJobBinding(
            job_id=str(message["job_id"]),
            request_sha256=str(message["request_sha256"]),
            owner_id=str(message["owner_id"]),
            claim_epoch=int(message["claim_epoch"]),
            claim_expires_at=str(message["claim_expires_at"]),
            reservation_id=str(message["reservation_id"]),
            dispatch_envelope_sha256=str(message["dispatch_envelope_sha256"]),
        )
        for field in ("job_id", "owner_id", "reservation_id"):
            if not _IDENTIFIER_RE.fullmatch(getattr(binding, field)):
                raise ValueError(field)
        for field in ("request_sha256", "dispatch_envelope_sha256"):
            if not _SHA256_RE.fullmatch(getattr(binding, field)):
                raise ValueError(field)
        if binding.claim_epoch < 1:
            raise ValueError("claim_epoch")
        expiry = datetime.fromisoformat(binding.claim_expires_at)
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise ValueError("claim_expires_at")
        return binding
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentWorkerProcessError("Agent Worker Job binding 字段无效。") from exc


def _job_binding_message_fields(
    binding: AgentWorkerJobBinding,
) -> dict[str, object]:
    return {
        "job_id": binding.job_id,
        "request_sha256": binding.request_sha256,
        "owner_id": binding.owner_id,
        "claim_epoch": binding.claim_epoch,
        "claim_expires_at": binding.claim_expires_at,
        "reservation_id": binding.reservation_id,
        "dispatch_envelope_sha256": binding.dispatch_envelope_sha256,
    }


def _child_message(
    message_type: str,
    *,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    process_id: int,
    sequence: int,
) -> dict[str, object]:
    return {
        "type": message_type,
        "protocol_version": _PROTOCOL_VERSION,
        "nonce": nonce,
        "worker_id": worker_id,
        "instance_id": instance_id,
        "epoch": epoch,
        "contract_sha256": contract_sha256,
        "process_id": process_id,
        "sequence": sequence,
        "accepting_jobs": False,
    }


def _validate_parent_message(
    message: object,
    *,
    expected_type: str,
    nonce: str,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
) -> None:
    expected = {
        "type": expected_type,
        "protocol_version": _PROTOCOL_VERSION,
        "nonce": nonce,
        "worker_id": worker_id,
        "instance_id": instance_id,
        "epoch": epoch,
        "contract_sha256": contract_sha256,
    }
    if not _secure_exact_message_matches(message, expected):
        raise AgentWorkerProcessError("Agent Worker 父进程控制消息认证失败。")


def _validate_child_message(
    message: object,
    *,
    expected_type: str,
    nonce: str,
    contract: WorkerContract,
    process_id: int | None,
    expected_sequence: int,
) -> None:
    if expected_type not in {"hello", "running", "pulse", "draining", "stopped"}:
        raise AgentWorkerProcessError("Agent Worker 子进程消息类型无效。")
    if process_id is None or process_id <= 0:
        raise AgentWorkerProcessError("Agent Worker 子进程 PID 不可信。")
    expected = _child_message(
        expected_type,
        nonce=nonce,
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        process_id=process_id,
        sequence=expected_sequence,
    )
    if not _secure_exact_message_matches(message, expected):
        raise AgentWorkerProcessError("Agent Worker 子进程生命周期消息认证失败。")


def _secure_exact_message_matches(
    message: object,
    expected: dict[str, object],
) -> bool:
    """Compare exact protocol shapes without timing-leaking credential fields."""
    if not isinstance(message, dict) or set(message) != set(expected):
        return False
    for field in ("nonce", "contract_sha256"):
        actual_value = message.get(field)
        expected_value = expected[field]
        if not isinstance(actual_value, str) or not isinstance(expected_value, str):
            return False
        if not secrets.compare_digest(actual_value, expected_value):
            return False
    return all(
        message[field] == value
        for field, value in expected.items()
        if field not in {"nonce", "contract_sha256"}
    )


def _job_owner_id(contract: WorkerContract) -> str:
    return _job_owner_id_values(
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
    )


def _job_owner_id_values(
    *,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
) -> str:
    digest = hashlib.sha256(
        f"{worker_id}\x00{instance_id}\x00{epoch}\x00{contract_sha256}".encode()
    ).hexdigest()
    return f"agent-worker-owner:{digest}"


def agent_worker_job_owner_id(contract: WorkerContract) -> str:
    """Return the durable Job owner identity bound to one Worker incarnation."""
    if not isinstance(contract, WorkerContract):
        raise TypeError("contract 必须是 WorkerContract。")
    return _job_owner_id(contract)


def _job_reservation_id(contract: WorkerContract, job_id: str) -> str:
    return _job_reservation_id_values(
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        job_id=job_id,
    )


def _job_reservation_id_values(
    *,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    job_id: str,
) -> str:
    digest = hashlib.sha256(
        (
            f"{worker_id}\x00{instance_id}\x00{epoch}\x00"
            f"{contract_sha256}\x00{job_id}"
        ).encode()
    ).hexdigest()
    return f"agent-job-slot:{digest}"


def agent_worker_job_reservation_id(contract: WorkerContract, job_id: str) -> str:
    """Return the physical slot identity bound to one Worker Job."""
    if not isinstance(contract, WorkerContract):
        raise TypeError("contract 必须是 WorkerContract。")
    if not isinstance(job_id, str) or not _IDENTIFIER_RE.fullmatch(job_id):
        raise ValueError("job_id 格式无效。")
    return _job_reservation_id(contract, job_id)


def _job_dispatch_aad(
    *,
    contract: WorkerContract,
    job_id: str,
    request_sha256: str,
    owner_id: str,
    claim_epoch: int,
    claim_expires_at: str,
    reservation_id: str,
) -> bytes:
    return _job_dispatch_aad_values(
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        job_id=job_id,
        request_sha256=request_sha256,
        owner_id=owner_id,
        claim_epoch=claim_epoch,
        claim_expires_at=claim_expires_at,
        reservation_id=reservation_id,
    )


def _job_dispatch_aad_values(
    *,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    job_id: str,
    request_sha256: str,
    owner_id: str,
    claim_epoch: int,
    claim_expires_at: str,
    reservation_id: str,
) -> bytes:
    payload = json.dumps(
        {
            "protocol_version": _PROTOCOL_VERSION,
            "worker_id": worker_id,
            "instance_id": instance_id,
            "epoch": epoch,
            "contract_sha256": contract_sha256,
            "job_id": job_id,
            "request_sha256": request_sha256,
            "owner_id": owner_id,
            "claim_epoch": claim_epoch,
            "claim_expires_at": claim_expires_at,
            "reservation_id": reservation_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _JOB_DISPATCH_AAD_PREFIX + payload


def _model_profile_aad(
    *,
    contract: WorkerContract,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
) -> bytes:
    return _model_profile_aad_values(
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        binding=binding,
        preparation=preparation,
    )


def _model_profile_aad_values(
    *,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
) -> bytes:
    return _MODEL_PROFILE_AAD_PREFIX + _canonical_execution_aad(
        worker_id=worker_id,
        instance_id=instance_id,
        epoch=epoch,
        contract_sha256=contract_sha256,
        binding=binding,
        preparation=preparation,
    )


def _tool_manifest_aad(
    *,
    contract: WorkerContract,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
) -> bytes:
    return _tool_manifest_aad_values(
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        binding=binding,
        preparation=preparation,
    )


def _tool_manifest_aad_values(
    *,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
) -> bytes:
    return _TOOL_MANIFEST_AAD_PREFIX + _canonical_execution_aad(
        worker_id=worker_id,
        instance_id=instance_id,
        epoch=epoch,
        contract_sha256=contract_sha256,
        binding=binding,
        preparation=preparation,
    )


def _tool_call_aad_values(
    *,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    running_receipt_sha256: str,
    call_batch: AgentWorkerToolCallBatch,
) -> bytes:
    return _tool_call_aad_identity_values(
        worker_id=worker_id,
        instance_id=instance_id,
        epoch=epoch,
        contract_sha256=contract_sha256,
        binding=binding,
        preparation=preparation,
        running_receipt_sha256=running_receipt_sha256,
        turn=call_batch.turn,
        batch_sha256=call_batch.batch_sha256,
    )


def _tool_call_aad_identity(
    *,
    contract: WorkerContract,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    running_receipt_sha256: str,
    turn: int,
    batch_sha256: str,
) -> bytes:
    return _tool_call_aad_identity_values(
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        binding=binding,
        preparation=preparation,
        running_receipt_sha256=running_receipt_sha256,
        turn=turn,
        batch_sha256=batch_sha256,
    )


def _tool_call_aad_identity_values(
    *,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    running_receipt_sha256: str,
    turn: int,
    batch_sha256: str,
) -> bytes:
    if not _SHA256_RE.fullmatch(running_receipt_sha256):
        raise AgentWorkerProcessError("running_receipt_sha256 格式无效。")
    if not _SHA256_RE.fullmatch(batch_sha256):
        raise AgentWorkerProcessError("tool call batch digest 格式无效。")
    if isinstance(turn, bool) or not isinstance(turn, int) or turn < 1:
        raise AgentWorkerProcessError("tool turn 格式无效。")
    return (
        _TOOL_CALL_AAD_PREFIX
        + _canonical_execution_aad(
            worker_id=worker_id,
            instance_id=instance_id,
            epoch=epoch,
            contract_sha256=contract_sha256,
            binding=binding,
            preparation=preparation,
        )
        + b"\x00"
        + running_receipt_sha256.encode("ascii")
        + b"\x00"
        + str(turn).encode("ascii")
        + b"\x00"
        + batch_sha256.encode("ascii")
    )


def _tool_result_aad(
    *,
    contract: WorkerContract,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    running_receipt_sha256: str,
    call_batch: AgentWorkerToolCallBatch,
    result_batch: AgentWorkerToolResultBatch,
) -> bytes:
    return _tool_result_aad_identity_values(
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        binding=binding,
        preparation=preparation,
        running_receipt_sha256=running_receipt_sha256,
        call_batch=call_batch,
        result_batch_sha256=result_batch.batch_sha256,
    )


def _tool_result_aad_identity_values(
    *,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    running_receipt_sha256: str,
    call_batch: AgentWorkerToolCallBatch,
    result_batch_sha256: str,
) -> bytes:
    if not _SHA256_RE.fullmatch(result_batch_sha256):
        raise AgentWorkerProcessError("tool result batch digest 格式无效。")
    return (
        _TOOL_RESULT_AAD_PREFIX
        + _tool_call_aad_values(
            worker_id=worker_id,
            instance_id=instance_id,
            epoch=epoch,
            contract_sha256=contract_sha256,
            binding=binding,
            preparation=preparation,
            running_receipt_sha256=running_receipt_sha256,
            call_batch=call_batch,
        )
        + b"\x00"
        + result_batch_sha256.encode("ascii")
    )


def _model_terminal_aad(
    *,
    contract: WorkerContract,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    running_receipt_sha256: str,
) -> bytes:
    return _model_terminal_aad_values(
        worker_id=contract.worker_id,
        instance_id=contract.instance_id,
        epoch=contract.epoch,
        contract_sha256=contract.contract_sha256,
        binding=binding,
        preparation=preparation,
        running_receipt_sha256=running_receipt_sha256,
    )


def _model_terminal_aad_values(
    *,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
    running_receipt_sha256: str,
) -> bytes:
    if not _SHA256_RE.fullmatch(running_receipt_sha256):
        raise AgentWorkerProcessError("running_receipt_sha256 格式无效。")
    return (
        _MODEL_TERMINAL_AAD_PREFIX
        + _canonical_execution_aad(
            worker_id=worker_id,
            instance_id=instance_id,
            epoch=epoch,
            contract_sha256=contract_sha256,
            binding=binding,
            preparation=preparation,
        )
        + b"\x00"
        + running_receipt_sha256.encode("ascii")
    )


def _canonical_execution_aad(
    *,
    worker_id: str,
    instance_id: str,
    epoch: int,
    contract_sha256: str,
    binding: AgentWorkerJobBinding,
    preparation: AgentWorkerModelExecutionPreparation,
) -> bytes:
    return json.dumps(
        {
            "protocol_version": _PROTOCOL_VERSION,
            "worker_id": worker_id,
            "instance_id": instance_id,
            "epoch": epoch,
            "contract_sha256": contract_sha256,
            **_job_binding_message_fields(binding),
            **_preparation_message_fields(preparation),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _transport_address(runtime_dir: Path) -> tuple[object, str]:
    token = secrets.token_hex(12)
    if os.name == "nt":
        return rf"\\.\pipe\naumi-agent-{token}", "AF_PIPE"
    socket_path = str(runtime_dir / f"agent-{token}.sock")
    if len(os.fsencode(socket_path)) < 100:
        return socket_path, "AF_UNIX"
    return ("127.0.0.1", 0), "AF_INET"


__all__ = [
    "AgentWorkerJobBinding",
    "AgentWorkerModelExecutionOutcome",
    "AgentWorkerProcessError",
    "AgentWorkerProcessFactory",
    "AgentWorkerProcessSnapshot",
    "AgentWorkerProcessState",
    "AuthenticatedAgentWorkerProcess",
    "agent_worker_job_owner_id",
    "agent_worker_job_reservation_id",
]
