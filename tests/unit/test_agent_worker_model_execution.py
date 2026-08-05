from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from naumi_agent.config.settings import ModelConfig
from naumi_agent.daemons.agent_jobs import AgentJobPayload, AgentJobState, AgentJobStore
from naumi_agent.daemons.agent_worker_contract import issue_agent_worker_request
from naumi_agent.daemons.agent_worker_model_execution import (
    MAX_AGENT_MODEL_RESPONSE_BYTES,
    AgentWorkerModelExecutionStatus,
    decode_model_execution_result,
    decode_model_profile,
    encode_model_execution_result,
    encode_model_profile,
    execute_model_only,
    issue_model_execution_preparation,
    model_profile_sha256,
)
from naumi_agent.daemons.agent_worker_process import (
    AgentWorkerProcessError,
    AgentWorkerProcessState,
    AuthenticatedAgentWorkerProcess,
)
from naumi_agent.daemons.worker_authority_health import (
    inspect_worker_authority_health,
)
from naumi_agent.daemons.worker_contract import WorkerCapability
from naumi_agent.daemons.worker_registry import (
    WorkerCapacityReservationState,
    WorkerRegistryStore,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.ui.doctor import _worker_authority_check


class _LoopbackServer:
    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        self.requests: list[dict[str, Any]] = []
        self.authorization_headers: list[str] = []
        requests = self.requests
        authorization_headers = self.authorization_headers

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length))
                requests.append(body)
                authorization_headers.append(self.headers.get("authorization", ""))
                if delay_seconds:
                    time.sleep(delay_seconds)
                response = {
                    "id": "chatcmpl-worker-loopback",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "独立进程模型执行完成",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 4,
                        "total_tokens": 9,
                    },
                }
                encoded = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except BrokenPipeError:
                    return

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> _LoopbackServer:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def _model_config(base_url: str) -> ModelConfig:
    return ModelConfig(
        default_model="openai/gpt-4o-mini",
        fast_model="openai/gpt-4o-mini",
        reasoning_model="openai/gpt-4o-mini",
        api_base=base_url,
        api_key="loopback-worker-secret",
        max_tokens=32,
        temperature=0,
    )


def _process(
    tmp_path: Path,
    *,
    model_config: ModelConfig,
) -> AuthenticatedAgentWorkerProcess:
    return AuthenticatedAgentWorkerProcess(
        worker_registry=WorkerRegistryStore(tmp_path / "worker-registry.db"),
        heartbeat_store=HarnessStore(tmp_path / "harness.db"),
        agent_job_store=AgentJobStore(
            tmp_path / "agent-jobs.db",
            key_provider=lambda: b"m" * 32,
        ),
        workspace_root=tmp_path / "workspace",
        runtime_dir=tmp_path / "runtime" / "agent-worker",
        software_version="0.1.214",
        model_config=model_config,
        max_concurrent_jobs=1,
        heartbeat_interval_seconds=0.05,
        heartbeat_timeout_seconds=3,
        handshake_timeout_seconds=5,
        shutdown_timeout_seconds=3,
        job_claim_lease_seconds=5,
        job_claim_renewal_interval_seconds=0.2,
    )


async def _admit_model_job(
    process: AuthenticatedAgentWorkerProcess,
    *,
    tool_scope: tuple[str, ...] = (),
    timeout_seconds: float = 5,
) -> str:
    payload = AgentJobPayload(
        task_id="task-independent-model",
        session_id="session-independent-model",
        task="只回答一条完成信息",
        context="系统上下文：不得调用工具",
        message_topic="agent.result.independent-model",
    )
    request = issue_agent_worker_request(
        task_id=payload.task_id,
        session_id=payload.session_id,
        agent_name="Explore",
        task=payload.task,
        context=payload.context,
        tool_scope=tool_scope,
        permission_mode="moderate",
        model_tier="capable",
        max_turns=50,
        max_budget_usd=None,
        timeout_seconds=timeout_seconds,
        message_topic=payload.message_topic,
        issued_at=datetime.now(UTC).isoformat(),
    )
    job = await process._agent_jobs.admit(request=request, payload=payload)
    return job.job_id


@pytest.mark.asyncio
async def test_model_profile_and_result_contracts_reject_unknown_fields() -> None:
    config = _model_config("http://127.0.0.1:1/v1")
    profile = encode_model_profile(config)
    assert decode_model_profile(profile) == config
    tampered_profile = json.loads(profile)
    tampered_profile["unexpected"] = True
    with pytest.raises(ValueError, match="字段集合"):
        decode_model_profile(json.dumps(tampered_profile).encode("utf-8"))

    payload = AgentJobPayload("task", "session", "task", "", "topic")
    request = issue_agent_worker_request(
        task_id=payload.task_id,
        session_id=payload.session_id,
        agent_name="Explore",
        task=payload.task,
        context=payload.context,
        tool_scope=(),
        permission_mode="moderate",
        model_tier="capable",
        max_turns=50,
        max_budget_usd=0,
        timeout_seconds=1,
        message_topic=payload.message_topic,
        issued_at=datetime.now(UTC).isoformat(),
    )
    preparation = issue_model_execution_preparation(
        job_id="agent-job-contract",
        request_sha256=request.request_sha256,
        claim_epoch=1,
        model_profile_sha256=model_profile_sha256(profile),
    )
    result = await execute_model_only(
        preparation=preparation,
        request=request,
        payload=payload,
        model_profile=profile,
    )
    assert result.status is AgentWorkerModelExecutionStatus.ERROR
    assert result.error_code == "agent_model_budget_zero"
    encoded = encode_model_execution_result(result)
    assert decode_model_execution_result(encoded) == result
    tampered_result = json.loads(encoded)
    tampered_result["unexpected"] = True
    with pytest.raises(ValueError, match="字段集合"):
        decode_model_execution_result(json.dumps(tampered_result).encode("utf-8"))


@pytest.mark.asyncio
async def test_real_child_model_call_commits_encrypted_terminal_and_outbox(
    tmp_path: Path,
) -> None:
    with _LoopbackServer(delay_seconds=0.2) as loopback:
        process = _process(tmp_path, model_config=_model_config(loopback.base_url))
        started = await process.start()
        assert started.accepting_jobs is True
        assert process.contract is not None
        assert process.contract.capabilities == (
            WorkerCapability.AGENT_CONTEXT_SCOPE,
            WorkerCapability.AGENT_CONTROL_TRANSPORT,
            WorkerCapability.AGENT_JOB_OWNER_LEASE,
            WorkerCapability.AGENT_MODEL_EXECUTION,
        )
        assert (
            process.contract.resources.max_output_bytes
            == MAX_AGENT_MODEL_RESPONSE_BYTES
        )
        authority = inspect_worker_authority_health(
            registry_db_path=process._registry.db_path,
            harness_db_path=process._heartbeats.db_path,
            workspace_root=tmp_path / "workspace",
            now=datetime.now(UTC).isoformat(),
        )
        doctor = _worker_authority_check(authority)
        assert doctor.status == "pass"
        assert "model-only 执行就绪、工具 RPC 未开放" in doctor.detail
        job_id = await _admit_model_job(process)
        binding = await process.bind_job(job_id)

        execution = asyncio.create_task(process.execute_bound_model_job())
        for _ in range(100):
            running = await process._agent_jobs.get(job_id)
            if running is not None and running.state is AgentJobState.RUNNING:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("AgentJob 未进入 running")
        running_sequence = process.snapshot().heartbeat_sequence
        for _ in range(100):
            await asyncio.sleep(0.01)
            if process.snapshot().heartbeat_sequence > running_sequence:
                break
        else:
            pytest.fail("独立模型调用期间 Agent Worker 心跳未推进")

        outcome = await execution

        assert outcome.transition.job.state is AgentJobState.COMPLETED
        assert outcome.result.status.value == "completed"
        assert outcome.result.total_tokens == 9
        assert outcome.provider_response_id_sha256 == hashlib.sha256(
            b"chatcmpl-worker-loopback"
        ).hexdigest()
        terminal = await process._agent_jobs.recover_terminal_payload(
            job_id,
            expected_result_sha256=outcome.result.result_sha256,
        )
        assert terminal.response == "独立进程模型执行完成"
        assert terminal.error == ""
        backlog = await process._agent_jobs.publication_backlog()
        assert backlog.pending == 1
        reservation = await process._registry.get_capacity_reservation(
            binding.reservation_id,
            assessed_at=datetime.now(UTC).isoformat(),
        )
        assert reservation is not None
        assert reservation.state is WorkerCapacityReservationState.RELEASED
        assert process.snapshot().bound_job_id == ""
        assert loopback.requests
        assert loopback.requests[0]["messages"] == [
            {"role": "system", "content": "系统上下文：不得调用工具"},
            {"role": "user", "content": "只回答一条完成信息"},
        ]
        assert loopback.authorization_headers == ["Bearer loopback-worker-secret"]
        for path in (
            tmp_path / "worker-registry.db",
            tmp_path / "harness.db",
            tmp_path / "agent-jobs.db",
        ):
            raw = path.read_bytes()
            assert b"loopback-worker-secret" not in raw
            assert "独立进程模型执行完成".encode() not in raw
        stopped = await process.close()
        assert stopped.state is AgentWorkerProcessState.STOPPED


@pytest.mark.asyncio
async def test_tool_scope_is_rejected_before_running_and_can_release_safely(
    tmp_path: Path,
) -> None:
    process = _process(
        tmp_path,
        model_config=_model_config("http://127.0.0.1:1/v1"),
    )
    await process.start()
    job_id = await _admit_model_job(process, tool_scope=("file_read",))
    await process.bind_job(job_id)

    with pytest.raises(AgentWorkerProcessError, match="不接受工具调用"):
        await process.execute_bound_model_job()

    claimed = await process._agent_jobs.get(job_id)
    assert claimed is not None and claimed.state is AgentJobState.CLAIMED
    await process.release_job()
    released = await process._agent_jobs.get(job_id)
    assert released is not None and released.state is AgentJobState.ADMITTED
    await process.close()


@pytest.mark.asyncio
async def test_provider_timeout_commits_safe_terminal_without_killing_worker(
    tmp_path: Path,
) -> None:
    with _LoopbackServer(delay_seconds=0.3) as loopback:
        process = _process(tmp_path, model_config=_model_config(loopback.base_url))
        await process.start()
        job_id = await _admit_model_job(process, timeout_seconds=0.05)
        binding = await process.bind_job(job_id)

        outcome = await process.execute_bound_model_job()

        assert outcome.transition.job.state is AgentJobState.TIMEOUT
        assert outcome.result.status.value == "timeout"
        assert outcome.result.turns == 1
        terminal = await process._agent_jobs.recover_terminal_payload(
            job_id,
            expected_result_sha256=outcome.result.result_sha256,
        )
        assert terminal.response == ""
        assert terminal.error == "独立 Agent 模型请求超时，未获得可信终态响应。"
        reservation = await process._registry.get_capacity_reservation(
            binding.reservation_id,
            assessed_at=datetime.now(UTC).isoformat(),
        )
        assert reservation is not None
        assert reservation.state is WorkerCapacityReservationState.RELEASED
        assert process.snapshot().state is AgentWorkerProcessState.RUNNING
        await process.close()


@pytest.mark.asyncio
async def test_child_loss_after_running_leaves_unknown_side_effect_for_recovery(
    tmp_path: Path,
) -> None:
    with _LoopbackServer(delay_seconds=1.0) as loopback:
        process = _process(tmp_path, model_config=_model_config(loopback.base_url))
        await process.start()
        job_id = await _admit_model_job(process, timeout_seconds=3)
        binding = await process.bind_job(job_id)
        task = asyncio.create_task(process.execute_bound_model_job())
        for _ in range(100):
            running = await process._agent_jobs.get(job_id)
            if running is not None and running.state is AgentJobState.RUNNING:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("AgentJob 未进入 running")
        assert process._process is not None
        process._process.terminate()
        process._process.join(timeout=2)

        with pytest.raises(AgentWorkerProcessError):
            await task
        stored = await process._agent_jobs.get(job_id)
        assert stored is not None and stored.state is AgentJobState.RUNNING
        reservation = await process._registry.get_capacity_reservation(
            binding.reservation_id,
            assessed_at=datetime.now(UTC).isoformat(),
        )
        assert reservation is not None
        assert reservation.state is WorkerCapacityReservationState.RELEASED
        assert process.snapshot().state is AgentWorkerProcessState.FAILED
