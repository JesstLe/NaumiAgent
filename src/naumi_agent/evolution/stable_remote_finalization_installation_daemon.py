"""Owner-fenced installation daemon for remote stable finalization."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import os
import re
import ssl
import stat
import uuid
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.stable_remote_finalization_deliveries import (
    EvolutionStableRemoteFinalizationDeliveryAck,
    EvolutionStableRemoteFinalizationDeliveryPackage,
    EvolutionStableRemoteFinalizationTargetJournal,
)
from naumi_agent.evolution.stable_remote_finalization_delivery_worker import (
    EvolutionStableRemoteFinalizationTransportError,
)
from naumi_agent.evolution.stable_remote_finalization_http_transport import (
    STABLE_REMOTE_FINALIZATION_HTTP_PATH,
    StableRemoteFinalizationHTTPServer,
    StableRemoteFinalizationHTTPServerPolicy,
)
from naumi_agent.evolution.stable_remote_finalization_result_return_worker import (
    EvolutionStableRemoteFinalizationCredentialResolver,
    EvolutionStableRemoteFinalizationResultReturnWorker,
    EvolutionStableRemoteFinalizationResultReturnWorkerSnapshot,
)
from naumi_agent.harness.heartbeat import assess_heartbeat
from naumi_agent.harness.heartbeat_runtime import (
    HeartbeatLifecycleDetailCodes,
    RuntimeHeartbeatProducer,
)
from naumi_agent.harness.run_lease import HarnessRunKind, HarnessRunLease
from naumi_agent.release.installation_keys import ReleaseInstallationKeyService
from naumi_agent.release.rollout_control_keys import (
    ReleaseRolloutControlTrustPolicyDocument,
)

_MEMBER_ID_RE = re.compile(r"^relpopmember_[0-9a-f]{24}$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\."
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_DISCOVERY_POLICY = "stable-remote-finalization-installation-discovery-v1"
_RUN_ID_PREFIX = "stable-finalization-installation"
_MAX_DISCOVERY_BYTES = 64 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class StableRemoteFinalizationInstallationDiscoveryDescriptor(_StrictModel):
    """Bounded local service-discovery fact; never contains credential paths."""

    schema_version: Literal[1] = 1
    policy_version: Literal[
        "stable-remote-finalization-installation-discovery-v1"
    ] = _DISCOVERY_POLICY
    descriptor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    endpoint_url: str = Field(min_length=1, max_length=2048)
    server_certificate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instance_id: str = Field(pattern=r"^stable-installation-[0-9a-f]{32}$")
    lease_epoch: int = Field(ge=1, le=1_000_000_000)
    process_id: int = Field(ge=1, le=2_147_483_647)
    started_at: str = Field(min_length=1, max_length=100)
    updated_at: str = Field(min_length=1, max_length=100)
    worker_state: str = Field(min_length=1, max_length=32)
    worker_pass_count: int = Field(ge=0, le=1_000_000_000)
    worker_returned_count: int = Field(ge=0, le=1_000_000_000)
    worker_dead_lettered_count: int = Field(ge=0, le=1_000_000_000)
    worker_failure_count: int = Field(ge=0, le=1_000_000_000)
    failure_code: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        started = _aware(self.started_at)
        updated = _aware(self.updated_at)
        if updated < started:
            raise ValueError("Installation discovery 更新时间不能早于启动时间。")
        _validate_endpoint(self.endpoint_url)
        digest = _digest(
            self.model_dump(mode="json", exclude={"descriptor_sha256"})
        )
        if self.descriptor_sha256 != digest:
            raise ValueError("Installation discovery 摘要无效。")
        return self


class StableRemoteFinalizationInstallationDaemonState(StrEnum):
    CREATED = "created"
    STANDBY = "standby"
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StableRemoteFinalizationInstallationDaemonPolicy:
    installation_member_id: str
    bind_host: str = "127.0.0.1"
    advertise_host: str = "localhost"
    port: int = 0
    lease_seconds: int = 60
    renew_interval_seconds: float = 10.0
    heartbeat_interval_seconds: float = 10.0
    heartbeat_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        member = str(self.installation_member_id or "").strip()
        if _MEMBER_ID_RE.fullmatch(member) is None:
            raise ValueError("Installation daemon member ID 无效。")
        bind = str(self.bind_host or "").strip()
        if not bind or len(bind) > 255 or any(character.isspace() for character in bind):
            raise ValueError("Installation daemon bind host 无效。")
        advertise = _advertise_host(self.advertise_host)
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 0 <= self.port <= 65535
        ):
            raise ValueError("Installation daemon port 无效。")
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, int)
            or not 10 <= self.lease_seconds <= 86_400
        ):
            raise ValueError("Installation daemon lease 必须在 10 到 86400 秒之间。")
        if not _finite(self.renew_interval_seconds, 0.1, self.lease_seconds / 3):
            raise ValueError("Installation daemon renew interval 必须不大于 lease 的三分之一。")
        if (
            isinstance(self.heartbeat_timeout_seconds, bool)
            or not isinstance(self.heartbeat_timeout_seconds, int)
            or not 3 <= self.heartbeat_timeout_seconds <= 86_400
        ):
            raise ValueError("Installation daemon heartbeat timeout 无效。")
        if not _finite(
            self.heartbeat_interval_seconds,
            0.1,
            self.heartbeat_timeout_seconds - 0.1,
        ):
            raise ValueError("Installation daemon heartbeat interval 无效。")
        object.__setattr__(self, "installation_member_id", member)
        object.__setattr__(self, "bind_host", bind)
        object.__setattr__(self, "advertise_host", advertise)


@dataclass(frozen=True, slots=True)
class StableRemoteFinalizationInstallationDaemonSnapshot:
    state: StableRemoteFinalizationInstallationDaemonState
    installation_member_sha256: str
    endpoint_url: str
    instance_id: str
    lease_epoch: int
    heartbeat_phase: str
    heartbeat_failure_code: str
    worker: EvolutionStableRemoteFinalizationResultReturnWorkerSnapshot
    active_requests: int
    discovery_published: bool
    failure_code: str
    started_at: str
    stopped_at: str


@dataclass(frozen=True, slots=True)
class StableRemoteFinalizationInstallationDaemonInspection:
    state: str
    installation_member_sha256: str
    endpoint_url: str
    instance_id: str
    lease_epoch: int
    heartbeat_phase: str
    heartbeat_health: str
    worker_state: str
    worker_pass_count: int
    worker_returned_count: int
    worker_dead_lettered_count: int
    worker_failure_count: int
    discovery_published: bool
    failure_code: str
    updated_at: str


class StableRemoteFinalizationInstallationDaemonPort(Protocol):
    async def acquire_run_lease(self, **kwargs: object) -> HarnessRunLease | None: ...

    async def renew_run_lease(self, **kwargs: object) -> HarnessRunLease | None: ...

    async def release_run_lease(self, **kwargs: object) -> HarnessRunLease | None: ...

    async def record_heartbeat(self, **kwargs: object) -> object: ...


class ResolvingStableRemoteFinalizationInstallationTransport:
    """Resolve current installation authority for each inbound package."""

    def __init__(
        self,
        *,
        installation_member_id: str,
        journal: EvolutionStableRemoteFinalizationTargetJournal,
        installation_key_service: ReleaseInstallationKeyService,
        trust_policy_provider: Callable[[], ReleaseRolloutControlTrustPolicyDocument],
        credential_resolver: EvolutionStableRemoteFinalizationCredentialResolver,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        member = str(installation_member_id or "").strip()
        if _MEMBER_ID_RE.fullmatch(member) is None:
            raise ValueError("Installation transport member ID 无效。")
        if installation_key_service.release_root != journal.release_root:
            raise ValueError("Installation transport Journal 与 key root 必须一致。")
        self.installation_member_id = member
        self.journal = journal
        self.installation_key_service = installation_key_service
        self.trust_policy_provider = trust_policy_provider
        self.credential_resolver = credential_resolver
        self.clock = clock

    async def receive(
        self,
        package: EvolutionStableRemoteFinalizationDeliveryPackage,
    ) -> EvolutionStableRemoteFinalizationDeliveryAck:
        if package.installation_member_id != self.installation_member_id:
            raise EvolutionStableRemoteFinalizationTransportError(
                "stable_remote_installation_daemon_member_mismatch",
                "Delivery 目标与当前 Installation daemon 不一致。",
                retryable=False,
            )
        credential = await self.credential_resolver(package)
        if credential.payload.member_id != self.installation_member_id:
            raise EvolutionStableRemoteFinalizationTransportError(
                "stable_remote_installation_daemon_credential_mismatch",
                "Installation credential 与 daemon identity 不一致。",
                retryable=False,
            )
        return self.journal.receive(
            package=package,
            trust_policy=self.trust_policy_provider(),
            credential=credential,
            installation_key_service=self.installation_key_service,
            clock=self.clock,
        )


class StableRemoteFinalizationInstallationDiscovery:
    """Atomically publish and exact-instance remove a local descriptor."""

    def __init__(self, path: str | Path) -> None:
        source = Path(path).expanduser()
        if not source.is_absolute():
            raise ValueError("Installation discovery path 必须是绝对路径。")
        self.path = source
        self.lock_path = source.parent / f".{source.name}.lock"

    def publish(
        self,
        descriptor: StableRemoteFinalizationInstallationDiscoveryDescriptor,
    ) -> None:
        directory = self.path.parent
        self._prepare_directory()
        payload = _canonical(descriptor.model_dump(mode="json")) + b"\n"
        if len(payload) > _MAX_DISCOVERY_BYTES:
            raise ValueError("Installation discovery descriptor 超过大小上限。")
        with _exclusive_file_lock(self.lock_path):
            temporary = directory / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            file_descriptor = os.open(temporary, flags, 0o600)
            try:
                with os.fdopen(file_descriptor, "wb", closefd=True) as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                if os.name != "nt":
                    self.path.chmod(0o600)
                _fsync_directory(directory)
            except Exception:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise

    def read(self) -> StableRemoteFinalizationInstallationDiscoveryDescriptor | None:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OSError("Installation discovery descriptor 必须是普通文件。")
        if not 1 <= metadata.st_size <= _MAX_DISCOVERY_BYTES:
            raise OSError("Installation discovery descriptor 大小无效。")
        try:
            return StableRemoteFinalizationInstallationDiscoveryDescriptor.model_validate_json(
                self.path.read_bytes()
            )
        except (OSError, ValueError) as exc:
            raise OSError("Installation discovery descriptor 无法验证。") from exc

    def remove(self, *, instance_id: str) -> bool:
        self._prepare_directory()
        with _exclusive_file_lock(self.lock_path):
            current = self.read()
            if current is None or current.instance_id != instance_id:
                return False
            self.path.unlink(missing_ok=True)
            _fsync_directory(self.path.parent)
            return True

    def _prepare_directory(self) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = directory.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise OSError("Installation discovery 目录必须是普通目录。")
        if os.name != "nt":
            directory.chmod(0o700)


class StableRemoteFinalizationInstallationDaemon:
    """Supervise one inbound server and one durable Result worker."""

    def __init__(
        self,
        *,
        policy: StableRemoteFinalizationInstallationDaemonPolicy,
        server_policy: StableRemoteFinalizationHTTPServerPolicy,
        inbound_transport: ResolvingStableRemoteFinalizationInstallationTransport,
        result_worker: EvolutionStableRemoteFinalizationResultReturnWorker,
        authority: StableRemoteFinalizationInstallationDaemonPort,
        workspace_root: str | Path,
        discovery: StableRemoteFinalizationInstallationDiscovery,
        now_provider: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep_provider: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if inbound_transport.installation_member_id != policy.installation_member_id:
            raise ValueError("Installation daemon transport member 不一致。")
        workspace = Path(workspace_root).expanduser()
        if not workspace.is_absolute():
            raise ValueError("Installation daemon workspace 必须是绝对路径。")
        if result_worker.policy.shutdown_drain_seconds >= policy.lease_seconds:
            raise ValueError("Installation daemon lease 必须大于 Result Worker drain。")
        self.policy = policy
        self.server_policy = server_policy
        self.inbound_transport = inbound_transport
        self.result_worker = result_worker
        self.authority = authority
        self.workspace_root = workspace.resolve(strict=False)
        self.discovery = discovery
        self.now_provider = now_provider
        self.sleep_provider = sleep_provider
        self._server = StableRemoteFinalizationHTTPServer(
            bind_host=policy.bind_host,
            port=policy.port,
            policy=server_policy,
            transport=inbound_transport,
        )
        self._state = StableRemoteFinalizationInstallationDaemonState.CREATED
        self._lock = asyncio.Lock()
        self._lease: HarnessRunLease | None = None
        self._producer: RuntimeHeartbeatProducer | None = None
        self._renew_task: asyncio.Task[None] | None = None
        self._failure_task: asyncio.Task[None] | None = None
        self._failure_event = asyncio.Event()
        self._terminated_event = asyncio.Event()
        self._failure_code = ""
        self._instance_id = ""
        self._endpoint_url = ""
        self._started_at = ""
        self._stopped_at = ""
        self._descriptor: StableRemoteFinalizationInstallationDiscoveryDescriptor | None = None

    async def start(self) -> bool:
        async with self._lock:
            if self._state in {
                StableRemoteFinalizationInstallationDaemonState.STARTING,
                StableRemoteFinalizationInstallationDaemonState.RUNNING,
                StableRemoteFinalizationInstallationDaemonState.DRAINING,
            }:
                return False
            self._state = StableRemoteFinalizationInstallationDaemonState.STARTING
            self._failure_event.clear()
            self._terminated_event.clear()
            self._failure_code = ""
            self._instance_id = f"stable-installation-{uuid.uuid4().hex}"
            owner_id = f"{self._run_id()}:{self._instance_id}"
            timestamp = self._timestamp()
            lease = await self.authority.acquire_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=self._run_id(),
                owner_id=owner_id,
                now=timestamp,
                lease_seconds=self.policy.lease_seconds,
            )
            if lease is None:
                self._state = StableRemoteFinalizationInstallationDaemonState.STANDBY
                self._terminated_event.set()
                return False
            self._lease = lease
            self._started_at = timestamp
            self._stopped_at = ""
            try:
                if not self._server.start():
                    raise RuntimeError("Installation daemon 入站服务已经运行。")
                self._endpoint_url = _endpoint_url(
                    self.policy.advertise_host,
                    self._server.bound_port,
                )
                producer = RuntimeHeartbeatProducer(
                    port=self.authority,
                    workspace_root=self.workspace_root,
                    subject_kind=HarnessRunKind.RUNTIME,
                    subject_id=self._run_id(),
                    instance_id=self._instance_id,
                    epoch=lease.epoch,
                    interval_seconds=self.policy.heartbeat_interval_seconds,
                    timeout_seconds=self.policy.heartbeat_timeout_seconds,
                    now_provider=self._timestamp,
                    on_failure=self._on_heartbeat_failure,
                    detail_codes=HeartbeatLifecycleDetailCodes(
                        starting="stable_installation_starting",
                        running="stable_installation_ready",
                        alive="stable_installation_alive",
                        draining="stable_installation_draining",
                        stopped="stable_installation_stopped",
                        failed="stable_installation_failed",
                    ),
                )
                await producer.start()
                self._producer = producer
                if not self.result_worker.start():
                    raise RuntimeError("Installation daemon Result Worker 已被其他 owner 启动。")
                self._publish_descriptor(updated_at=self._timestamp())
                self._renew_task = asyncio.create_task(
                    self._renew_loop(),
                    name="naumi-stable-installation-lease",
                )
                self._failure_task = asyncio.create_task(
                    self._failure_loop(),
                    name="naumi-stable-installation-fail-closed",
                )
            except Exception:
                self._failure_code = "stable_installation_start_failed"
                await self._rollback_start()
                self._state = StableRemoteFinalizationInstallationDaemonState.FAILED
                self._terminated_event.set()
                raise
            self._state = StableRemoteFinalizationInstallationDaemonState.RUNNING
            return True

    async def supervise_once(self) -> bool:
        """Renew the exact daemon epoch or trigger asynchronous fail-closed drain."""
        lease = self._lease
        if (
            lease is None
            or self._state
            is not StableRemoteFinalizationInstallationDaemonState.RUNNING
        ):
            return False
        renewed = await self.authority.renew_run_lease(
            workspace_root=self.workspace_root,
            run_kind=HarnessRunKind.RUNTIME,
            run_id=lease.run_id,
            owner_id=lease.owner_id,
            epoch=lease.epoch,
            now=self._timestamp(),
            lease_seconds=self.policy.lease_seconds,
        )
        if renewed is None:
            self._failure_code = "stable_installation_lease_lost"
            self._failure_event.set()
            return False
        self._lease = renewed
        self._publish_descriptor(updated_at=self._timestamp())
        return True

    async def stop(self, *, failed: bool = False) -> bool:
        async with self._lock:
            if self._state not in {
                StableRemoteFinalizationInstallationDaemonState.RUNNING,
                StableRemoteFinalizationInstallationDaemonState.STARTING,
            }:
                return False
            self._state = StableRemoteFinalizationInstallationDaemonState.DRAINING
            await self._cancel_background_tasks()
            producer = self._producer
            if producer is not None:
                try:
                    await producer.begin_draining()
                except Exception:
                    failed = True
                    self._failure_code = self._failure_code or "stable_installation_draining_failed"
            try:
                if self._instance_id:
                    self.discovery.remove(instance_id=self._instance_id)
            except OSError:
                failed = True
                self._failure_code = (
                    self._failure_code
                    or "stable_installation_discovery_remove_failed"
                )
            try:
                self._server.stop()
            except Exception:
                failed = True
                self._failure_code = self._failure_code or "stable_installation_server_stop_failed"
            try:
                await self.result_worker.stop()
            except Exception:
                failed = True
                self._failure_code = self._failure_code or "stable_installation_worker_stop_failed"
            if producer is not None:
                try:
                    if failed:
                        await producer.fail(detail_code=self._failure_code or None)
                    else:
                        await producer.close()
                except Exception:
                    failed = True
                    self._failure_code = (
                        self._failure_code
                        or "stable_installation_terminal_heartbeat_failed"
                    )
            released = await self._release_lease()
            if not released and self._lease is not None:
                failed = True
                self._failure_code = (
                    self._failure_code or "stable_installation_lease_release_failed"
                )
            self._producer = None
            self._descriptor = None
            self._stopped_at = self._timestamp()
            self._state = (
                StableRemoteFinalizationInstallationDaemonState.FAILED
                if failed
                else StableRemoteFinalizationInstallationDaemonState.STOPPED
            )
            self._terminated_event.set()
            return True

    async def wait_terminated(
        self,
    ) -> StableRemoteFinalizationInstallationDaemonSnapshot:
        """Wait until the foreground service reaches a terminal/standby state."""
        await self._terminated_event.wait()
        return self.snapshot()

    def snapshot(self) -> StableRemoteFinalizationInstallationDaemonSnapshot:
        producer = self._producer
        phase = None if producer is None else producer.phase
        lease = self._lease
        return StableRemoteFinalizationInstallationDaemonSnapshot(
            state=self._state,
            installation_member_sha256=hashlib.sha256(
                self.policy.installation_member_id.encode("utf-8")
            ).hexdigest(),
            endpoint_url=self._endpoint_url,
            instance_id=self._instance_id,
            lease_epoch=0 if lease is None else lease.epoch,
            heartbeat_phase="" if phase is None else phase.value,
            heartbeat_failure_code="" if producer is None else producer.failure_code,
            worker=self.result_worker.snapshot(),
            active_requests=self._server.active_requests,
            discovery_published=self._descriptor is not None,
            failure_code=self._failure_code,
            started_at=self._started_at,
            stopped_at=self._stopped_at,
        )

    async def inspect(
        self,
    ) -> StableRemoteFinalizationInstallationDaemonInspection:
        """Read cross-process discovery and heartbeat authority without side effects."""
        try:
            descriptor = self.discovery.read()
        except OSError:
            return self._inspection_from_local(
                heartbeat_health="descriptor_invalid",
                failure_code="stable_installation_discovery_invalid",
            )
        if descriptor is None:
            return self._inspection_from_local(heartbeat_health="absent")
        subject_id = self._run_id()
        try:
            heartbeat = await self.authority.get_heartbeat(
                workspace_root=self.workspace_root,
                subject_kind=HarnessRunKind.RUNTIME,
                subject_id=subject_id,
            )
        except Exception:
            heartbeat = None
        heartbeat_phase = ""
        heartbeat_health = "missing"
        if heartbeat is not None:
            heartbeat_phase = heartbeat.phase.value
            if (
                heartbeat.instance_id != descriptor.instance_id
                or heartbeat.epoch != descriptor.lease_epoch
            ):
                heartbeat_health = "identity_mismatch"
            else:
                try:
                    heartbeat_health = assess_heartbeat(
                        heartbeat,
                        now=self._timestamp(),
                    ).health.value
                except ValueError:
                    heartbeat_health = "invalid"
        return StableRemoteFinalizationInstallationDaemonInspection(
            state=(
                "running"
                if heartbeat_health in {"starting", "healthy"}
                else "unhealthy"
            ),
            installation_member_sha256=hashlib.sha256(
                descriptor.installation_member_id.encode("utf-8")
            ).hexdigest(),
            endpoint_url=descriptor.endpoint_url,
            instance_id=descriptor.instance_id,
            lease_epoch=descriptor.lease_epoch,
            heartbeat_phase=heartbeat_phase,
            heartbeat_health=heartbeat_health,
            worker_state=descriptor.worker_state,
            worker_pass_count=descriptor.worker_pass_count,
            worker_returned_count=descriptor.worker_returned_count,
            worker_dead_lettered_count=descriptor.worker_dead_lettered_count,
            worker_failure_count=descriptor.worker_failure_count,
            discovery_published=True,
            failure_code=descriptor.failure_code,
            updated_at=descriptor.updated_at,
        )

    async def _renew_loop(self) -> None:
        while self._state is StableRemoteFinalizationInstallationDaemonState.RUNNING:
            await self.sleep_provider(self.policy.renew_interval_seconds)
            if self._state is not StableRemoteFinalizationInstallationDaemonState.RUNNING:
                return
            try:
                if not await self.supervise_once():
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failure_code = "stable_installation_lease_renew_failed"
                self._failure_event.set()
                return

    async def _failure_loop(self) -> None:
        await self._failure_event.wait()
        if self._state is StableRemoteFinalizationInstallationDaemonState.RUNNING:
            await self.stop(failed=True)

    async def _on_heartbeat_failure(self, code: str) -> None:
        self._failure_code = str(code or "stable_installation_heartbeat_failed")[:128]
        self._failure_event.set()

    async def _cancel_background_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in (self._renew_task, self._failure_task)
            if task is not None and task is not current
        ]
        self._renew_task = None
        self._failure_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _rollback_start(self) -> None:
        try:
            if self._instance_id:
                self.discovery.remove(instance_id=self._instance_id)
        except OSError:
            pass
        try:
            self._server.stop()
        except Exception:
            pass
        try:
            await self.result_worker.stop()
        except Exception:
            pass
        producer = self._producer
        if producer is not None:
            try:
                await producer.fail(detail_code=self._failure_code)
            except Exception:
                pass
        self._producer = None
        await self._release_lease()

    async def _release_lease(self) -> bool:
        lease = self._lease
        if lease is None:
            return True
        try:
            released = await self.authority.release_run_lease(
                workspace_root=self.workspace_root,
                run_kind=HarnessRunKind.RUNTIME,
                run_id=lease.run_id,
                owner_id=lease.owner_id,
                epoch=lease.epoch,
                now=self._timestamp(),
            )
        except Exception:
            return False
        if released is None:
            return False
        self._lease = None
        return True

    def _publish_descriptor(self, *, updated_at: str) -> None:
        lease = self._lease
        if lease is None:
            raise RuntimeError("Installation daemon 缺少 live lease。")
        worker = self.result_worker.snapshot()
        core = {
            "schema_version": 1,
            "policy_version": _DISCOVERY_POLICY,
            "installation_member_id": self.policy.installation_member_id,
            "endpoint_url": self._endpoint_url,
            "server_certificate_sha256": _certificate_fingerprint(
                self.server_policy.server_certificate_path
            ),
            "instance_id": self._instance_id,
            "lease_epoch": lease.epoch,
            "process_id": os.getpid(),
            "started_at": self._started_at,
            "updated_at": updated_at,
            "worker_state": worker.state.value,
            "worker_pass_count": worker.pass_count,
            "worker_returned_count": worker.returned_count,
            "worker_dead_lettered_count": worker.dead_lettered_count,
            "worker_failure_count": worker.failure_count,
            "failure_code": self._failure_code,
        }
        descriptor = StableRemoteFinalizationInstallationDiscoveryDescriptor(
            descriptor_sha256=_digest(core),
            **core,
        )
        self.discovery.publish(descriptor)
        self._descriptor = descriptor

    def _inspection_from_local(
        self,
        *,
        heartbeat_health: str,
        failure_code: str = "",
    ) -> StableRemoteFinalizationInstallationDaemonInspection:
        snapshot = self.snapshot()
        return StableRemoteFinalizationInstallationDaemonInspection(
            state=snapshot.state.value,
            installation_member_sha256=snapshot.installation_member_sha256,
            endpoint_url=snapshot.endpoint_url,
            instance_id=snapshot.instance_id,
            lease_epoch=snapshot.lease_epoch,
            heartbeat_phase=snapshot.heartbeat_phase,
            heartbeat_health=heartbeat_health,
            worker_state=snapshot.worker.state.value,
            worker_pass_count=snapshot.worker.pass_count,
            worker_returned_count=snapshot.worker.returned_count,
            worker_dead_lettered_count=snapshot.worker.dead_lettered_count,
            worker_failure_count=snapshot.worker.failure_count,
            discovery_published=False,
            failure_code=failure_code or snapshot.failure_code,
            updated_at=snapshot.stopped_at or snapshot.started_at,
        )

    def _run_id(self) -> str:
        member_hash = hashlib.sha256(
            self.policy.installation_member_id.encode("utf-8")
        ).hexdigest()[:24]
        return f"{_RUN_ID_PREFIX}-{member_hash}"

    def _timestamp(self) -> str:
        value = self.now_provider()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("Installation daemon 时钟必须包含时区。")
        return value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class StableRemoteFinalizationInstallationDaemonFactory:
    """Composition-root factory; construction never starts background work."""

    policy: StableRemoteFinalizationInstallationDaemonPolicy
    server_policy: StableRemoteFinalizationHTTPServerPolicy
    authority: StableRemoteFinalizationInstallationDaemonPort
    workspace_root: Path
    discovery: StableRemoteFinalizationInstallationDiscovery

    def __post_init__(self) -> None:
        workspace = Path(self.workspace_root).expanduser()
        if not workspace.is_absolute():
            raise ValueError("Installation daemon factory workspace 必须是绝对路径。")
        object.__setattr__(self, "workspace_root", workspace.resolve(strict=False))

    def create(
        self,
        *,
        inbound_transport: ResolvingStableRemoteFinalizationInstallationTransport,
        result_worker: EvolutionStableRemoteFinalizationResultReturnWorker,
    ) -> StableRemoteFinalizationInstallationDaemon:
        return StableRemoteFinalizationInstallationDaemon(
            policy=self.policy,
            server_policy=self.server_policy,
            inbound_transport=inbound_transport,
            result_worker=result_worker,
            authority=self.authority,
            workspace_root=self.workspace_root,
            discovery=self.discovery,
        )


def _endpoint_url(host: str, port: int) -> str:
    authority = f"[{host}]" if ":" in host else host
    return f"https://{authority}:{port}{STABLE_REMOTE_FINALIZATION_HTTP_PATH}"


def _validate_endpoint(value: str) -> None:
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Installation discovery endpoint 无效。") from exc
    if not (
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path == STABLE_REMOTE_FINALIZATION_HTTP_PATH
        and port is not None
        and 1 <= port <= 65535
    ):
        raise ValueError("Installation discovery endpoint 无效。")
    _advertise_host(parsed.hostname)


def _advertise_host(value: str) -> str:
    normalized = str(value or "").strip().lower().rstrip(".")
    if not normalized or not normalized.isascii():
        raise ValueError("Installation daemon advertise host 无效。")
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        if _HOSTNAME_RE.fullmatch(normalized) is None:
            raise ValueError("Installation daemon advertise host 无效。") from None
        return normalized
    if address.is_unspecified:
        raise ValueError("Installation daemon 不能发布 unspecified address。")
    return address.compressed


def _certificate_fingerprint(path: Path) -> str:
    try:
        pem = path.read_text(encoding="ascii")
        der = ssl.PEM_cert_to_DER_cert(pem)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("Installation daemon 服务端证书无法读取。") from exc
    return hashlib.sha256(der).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Installation daemon 时间必须是 ISO 8601。") from exc
    if parsed.tzinfo is None:
        raise ValueError("Installation daemon 时间必须包含时区。")
    return parsed.astimezone(UTC)


def _finite(value: float, minimum: float, maximum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and minimum <= float(value) <= maximum
    )


@contextmanager
def _exclusive_file_lock(path: Path):
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("Installation discovery lock 必须是普通文件。")
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        if metadata.st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def render_stable_remote_finalization_installation_daemon(
    snapshot: StableRemoteFinalizationInstallationDaemonInspection,
) -> str:
    """Render one redacted daemon authority snapshot for all terminal surfaces."""
    return "\n".join((
        "## Remote Finalization Installation Daemon",
        "",
        f"- 状态：**{snapshot.state}**",
        f"- Installation：`{snapshot.installation_member_sha256[:16]}…`",
        f"- Endpoint：`{snapshot.endpoint_url or '未发布'}`",
        f"- Lease epoch：`{snapshot.lease_epoch}`",
        f"- Heartbeat：`{snapshot.heartbeat_phase or '未启动'}`",
        f"- Health：`{snapshot.heartbeat_health}`",
        f"- Discovery：`{'published' if snapshot.discovery_published else 'absent'}`",
        f"- Worker：`{snapshot.worker_state}` · passes `{snapshot.worker_pass_count}`",
        f"- Result returned：`{snapshot.worker_returned_count}`",
        f"- Dead letter：`{snapshot.worker_dead_lettered_count}`",
        f"- Worker failures：`{snapshot.worker_failure_count}`",
        f"- Failure：`{snapshot.failure_code or 'none'}`",
    ))


__all__ = [
    "ResolvingStableRemoteFinalizationInstallationTransport",
    "StableRemoteFinalizationInstallationDaemon",
    "StableRemoteFinalizationInstallationDaemonFactory",
    "StableRemoteFinalizationInstallationDaemonInspection",
    "StableRemoteFinalizationInstallationDaemonPolicy",
    "StableRemoteFinalizationInstallationDaemonSnapshot",
    "StableRemoteFinalizationInstallationDaemonState",
    "StableRemoteFinalizationInstallationDiscovery",
    "StableRemoteFinalizationInstallationDiscoveryDescriptor",
    "render_stable_remote_finalization_installation_daemon",
]
