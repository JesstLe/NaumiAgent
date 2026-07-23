from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.harness.models import (
    HarnessCheckSpec,
    HarnessEvalSpec,
    HarnessProfile,
)
from naumi_agent.harness.sandbox_request import HarnessSandboxEvalRequestBuilder
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.ui.bridge import JsonlEngineBridge


def _git_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "harness@example.invalid"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Harness Test"],
        cwd=workspace,
        check=True,
    )
    (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=workspace, check=True)
    return workspace


def _profile() -> HarnessProfile:
    return HarnessProfile(
        schema_version=1,
        checks=(
            HarnessCheckSpec(
                id="unit",
                label="定向单测",
                argv=("python3", "-c", "print('ok')"),
                timeout_seconds=30,
                provides=("unit",),
            ),
        ),
        evals=HarnessEvalSpec(max_duration_seconds=600),
    )


async def _seed_pending_dispatch(store: HarnessStore, workspace: Path):
    request = HarnessSandboxEvalRequestBuilder().build(
        workspace_root=workspace,
        profile=_profile(),
        profile_digest="a" * 64,
        profile_trusted=True,
        check_ids=("unit",),
        batch_id="sandbox-retry-startup-restart",
        requested_samples=5,
    )
    await store.record_sandbox_eval_request(
        request,
        created_at="2026-07-24T00:00:00+00:00",
    )
    source = await store.enqueue_sandbox_admission(
        workspace_root=workspace,
        ticket_id=f"hsadm_{'1' * 24}",
        authority_key=request.request_sha256,
        lane="sandbox",
        requested_samples=5,
        owner_id="source-process",
        now="2026-07-24T00:00:01+00:00",
        lease_seconds=300,
        max_active=10,
        max_queued=10,
    )
    cancel, _ = await store.cancel_sandbox_admission(
        workspace_root=workspace,
        action_id=f"hsac_{'2' * 24}",
        ticket_id=source.ticket_id,
        authority_key=source.authority_key,
        epoch=source.epoch,
        expected_state=source.state,
        actor_id="restart-fixture",
        reason="构造启动恢复事实",
        now="2026-07-24T00:00:02+00:00",
    )
    retry = await store.authorize_sandbox_admission_retry(
        workspace_root=workspace,
        action_id=f"hsar_{'3' * 24}",
        cancel_receipt_id=cancel.receipt_id,
        cancel_receipt_sha256=cancel.receipt_sha256,
        actor_id="restart-fixture",
        reason="构造 pending dispatch",
        authority_token="4" * 32,
        now="2026-07-24T00:00:03+00:00",
    )
    dispatch = await store.get_sandbox_retry_dispatch(
        workspace_root=workspace,
        retry_action_id=retry.action_id,
    )
    assert dispatch is not None
    assert dispatch.state == "pending"
    return retry, dispatch


class _Router:
    def resolve_model(self, _tier: str) -> str:
        return ""


class _BridgeEngine:
    def __init__(self, workspace: Path, service: HarnessService) -> None:
        self.workspace_root = workspace
        self.harness_service = service
        self.runtime_mode = SimpleNamespace(value="default")
        self.permission_mode = SimpleNamespace(value="moderate")
        self.usage = SimpleNamespace(
            total_input_tokens=0,
            total_output_tokens=0,
            turns=0,
        )
        self.router = _Router()
        self._session = None
        self._config = SimpleNamespace(ui=SimpleNamespace(show_reasoning=False))

    def set_permission_confirmer(self, confirmer) -> None:
        self.permission_confirmer = confirmer

    def set_user_interaction_handler(self, handler) -> None:
        self.user_interaction_handler = handler

    def get_context_info(self) -> dict[str, int]:
        return {"used": 0, "window": 1, "percentage": 0}

    def get_budget_info(self) -> dict[str, object]:
        return {"enabled": False, "used_usd": 0.0}

    def session_retention_worker_status(self) -> dict[str, object]:
        return {
            "configured_enabled": False,
            "owner_id": "",
            "state": "stopped",
            "lease_held": False,
            "pass_count": 0,
            "completed_session_count": 0,
            "retry_scheduled_count": 0,
            "failure_count": 0,
            "consecutive_empty_passes": 0,
            "next_delay_seconds": 0.0,
            "last_pass_status": "",
            "last_error_code": "",
            "started_at": "",
            "last_pass_at": "",
        }


@pytest.mark.asyncio
async def test_new_bridge_restart_discovers_pending_retry_without_claim(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path)
    first_store = HarnessStore(tmp_path / "harness.db")
    retry, before = await _seed_pending_dispatch(first_store, workspace)

    restarted_store = HarnessStore(first_store.db_path)
    restarted_service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=restarted_store,
    )
    writer = io.StringIO()
    bridge = JsonlEngineBridge(
        _BridgeEngine(workspace, restarted_service),  # type: ignore[arg-type]
        config_path="config.yaml",
    )
    bridge.bind_writer(writer)

    await bridge.emit_ready()

    ready = json.loads(writer.getvalue().splitlines()[0])
    snapshot = ready["payload"]["sandbox_retry_recovery"]
    after = await HarnessStore(first_store.db_path).get_sandbox_retry_dispatch(
        workspace_root=workspace,
        retry_action_id=retry.action_id,
    )

    assert snapshot["status"] == "ready"
    assert snapshot["total"] == 1
    assert snapshot["counts"]["actionable"] == 1
    assert snapshot["items"][0]["dispatch_id"] == before.dispatch_id
    assert snapshot["items"][0]["resume_command"].startswith(
        f"/harness eval sandbox resume {retry.action_id}"
    )
    assert workspace.as_posix() not in str(snapshot)
    assert after == before
    assert after is not None
    assert after.ticket_id == ""
    assert after.epoch == 0
