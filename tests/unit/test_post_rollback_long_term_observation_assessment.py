from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.post_rollback_long_term_observation_assessments import (
    EvolutionPostRollbackLongTermObservationAssessmentError,
    EvolutionPostRollbackLongTermObservationAssessmentService,
    EvolutionPostRollbackLongTermObservationAssessmentStore,
    EvolutionPostRollbackLongTermObservationStatus,
)
from naumi_agent.evolution.post_rollback_runtime_observation_admissions import (
    EvolutionPostRollbackRuntimeObservationAdmissionService,
    EvolutionPostRollbackRuntimeObservationAdmissionStore,
)
from naumi_agent.harness.heartbeat import HarnessHeartbeatPhase
from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.runtime_release_binding import build_runtime_release_binding
from naumi_agent.harness.store import HarnessStore
from naumi_agent.release.runtime_identity import ReleaseRuntimeIdentity
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import (
    EvolutionPostRollbackLongTermObservationAssessmentTool,
)
from tests.unit.test_post_rollback_long_term_observation_contract import (
    _fixture as _contract_fixture,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _identity(workspace: Path, contract) -> ReleaseRuntimeIdentity:
    core = {
        "schema_version": 1,
        "policy_version": "naumi-release-runtime-identity-v1",
        "invocation_kind": "terminal_session",
        "pointer_id": contract.rollback_pointer_id,
        "pointer_sha256": contract.rollback_pointer_sha256,
        "pointer_generation": contract.rollback_pointer_generation,
        "slot_id": contract.baseline_slot_id,
        "slot_sha256": contract.baseline_slot_sha256,
        "version": contract.baseline_version,
        "target": contract.baseline_target,
        "boot_receipt_id": "relboot_" + "3" * 24,
        "boot_receipt_sha256": "4" * 64,
        "binary_sha256": contract.baseline_binary_sha256,
        "runtime_path": str((workspace / "bin" / "naumi").resolve()),
        "install_root": str((workspace / "install").resolve()),
        "checks": [
            "active_chain_verified",
            "manifest_verified",
            "boot_receipt_verified",
            "runtime_binary_verified",
            "environment_binding_verified",
        ],
        "runtime_process_started": True,
        "terminal_session_process": True,
        "health_probe_process": False,
        "verified_at": "2026-08-10T09:02:30+00:00",
    }
    digest = _digest(core)
    return ReleaseRuntimeIdentity.model_validate(
        {
            **core,
            "identity_id": f"relruntimeidentity_{digest[:24]}",
            "identity_sha256": digest,
        }
    )


async def _fixture(tmp_path: Path):
    (
        contract_service,
        contract_store,
        matrix,
        _verification,
        _matrix_service,
        _verification_service,
    ) = _contract_fixture(tmp_path)
    contract_view = await contract_service.record(request_id=matrix.request_id)
    contract = contract_view.contract
    identity = _identity(contract_service.workspace_root, contract)
    harness_store = HarnessStore(tmp_path / "harness.db")
    binding = build_runtime_release_binding(
        workspace_root=contract_service.workspace_root,
        surface="new_ui",
        subject_id="runtime-long-term",
        instance_id="instance-long-term",
        epoch=1,
        runtime_identity=identity,
        bound_at="2026-08-10T09:03:00+00:00",
    )
    origin_at = datetime(2026, 8, 10, 9, 3, 1, tzinfo=UTC)
    await harness_store.record_runtime_release_binding_startup(
        binding=binding,
        observed_at=origin_at.isoformat(),
        timeout_seconds=300,
        detail_code="runtime_starting",
    )
    admission_store = EvolutionPostRollbackRuntimeObservationAdmissionStore(
        contract_store.db_path
    )
    admission_service = EvolutionPostRollbackRuntimeObservationAdmissionService(
        workspace_root=contract_service.workspace_root,
        contract_store=contract_store,
        contract_service=contract_service,
        harness_store=harness_store,
        store=admission_store,
    )
    admission_view = await admission_service.record(
        request_id=contract.request_id,
        subject_id=binding.subject_id,
    )
    now = [origin_at]
    assessment_store = EvolutionPostRollbackLongTermObservationAssessmentStore(
        contract_store.db_path,
        contract_store=contract_store,
        admission_store=admission_store,
        harness_store=harness_store,
    )
    assessment_service = EvolutionPostRollbackLongTermObservationAssessmentService(
        workspace_root=contract_service.workspace_root,
        contract_store=contract_store,
        contract_service=contract_service,
        admission_store=admission_store,
        admission_service=admission_service,
        harness_store=harness_store,
        store=assessment_store,
        clock=lambda: now[0],
    )
    return (
        assessment_service,
        assessment_store,
        admission_view.admission,
        harness_store,
        binding,
        origin_at,
        now,
    )


async def _heartbeat(
    harness_store: HarnessStore,
    binding,
    *,
    sequence: int,
    observed_at: datetime,
    phase: HarnessHeartbeatPhase = HarnessHeartbeatPhase.RUNNING,
) -> None:
    await harness_store.record_heartbeat(
        workspace_root=binding.workspace_root,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id=binding.subject_id,
        instance_id=binding.instance_id,
        epoch=binding.epoch,
        sequence=sequence,
        phase=phase,
        observed_at=observed_at.isoformat(),
        timeout_seconds=300,
        detail_code=f"runtime_{phase.value}",
    )


@pytest.mark.asyncio
async def test_short_active_window_is_insufficient(tmp_path: Path) -> None:
    service, _store, admission, harness, binding, origin, now = await _fixture(
        tmp_path
    )
    latest = origin + timedelta(seconds=1)
    await _heartbeat(harness, binding, sequence=2, observed_at=latest)
    now[0] = latest
    view = await service.assess(
        request_id=admission.request_id,
        subject_id=admission.subject_id,
    )

    assert view.current_assessment is not None
    assert view.current_assessment.status is (
        EvolutionPostRollbackLongTermObservationStatus.INSUFFICIENT
    )
    assert view.current_assessment.insufficient_reasons == (
        "minimum_observation_seconds",
        "minimum_operational_samples",
    )
    assert not view.long_term_health_authority
    assert not view.health_alert_authority


@pytest.mark.asyncio
async def test_hour_long_contiguous_window_passes(tmp_path: Path) -> None:
    service, store, admission, harness, binding, origin, now = await _fixture(tmp_path)
    first = origin + timedelta(seconds=1)
    for offset in range(13):
        latest = first + timedelta(seconds=300 * offset)
        await _heartbeat(
            harness,
            binding,
            sequence=offset + 2,
            observed_at=latest,
        )
    now[0] = latest
    view = await service.assess(
        request_id=admission.request_id,
        subject_id=admission.subject_id,
    )

    assert view.current_assessment is not None
    assert view.current_assessment.status is (
        EvolutionPostRollbackLongTermObservationStatus.PASSING
    )
    assert view.current_assessment.observation_seconds == 3600
    assert view.current_assessment.operational_sample_count == 13
    assert view.long_term_health_authority
    assert not view.health_alert_authority
    assert await store.latest(admission_id=admission.admission_id) == view.receipt


@pytest.mark.asyncio
async def test_gap_or_failed_phase_breaches_window(tmp_path: Path) -> None:
    service, _store, admission, harness, binding, origin, now = await _fixture(
        tmp_path
    )
    first = origin + timedelta(seconds=1)
    second = first + timedelta(seconds=301)
    await _heartbeat(harness, binding, sequence=2, observed_at=first)
    await _heartbeat(harness, binding, sequence=3, observed_at=second)
    now[0] = second
    gap = await service.assess(
        request_id=admission.request_id,
        subject_id=admission.subject_id,
    )
    assert gap.current_assessment is not None
    assert gap.current_assessment.status is (
        EvolutionPostRollbackLongTermObservationStatus.BREACHED
    )
    assert gap.current_assessment.breach_reasons == ("heartbeat_gap",)
    assert gap.health_alert_authority

    failed_at = second + timedelta(seconds=1)
    await _heartbeat(
        harness,
        binding,
        sequence=4,
        observed_at=failed_at,
        phase=HarnessHeartbeatPhase.FAILED,
    )
    now[0] = failed_at
    failed = await service.assess(
        request_id=admission.request_id,
        subject_id=admission.subject_id,
    )
    assert failed.current_assessment is not None
    assert "runtime_failed" in failed.current_assessment.breach_reasons
    assert failed.health_alert_authority


@pytest.mark.asyncio
async def test_graceful_terminal_phase_is_censored(tmp_path: Path) -> None:
    service, _store, admission, harness, binding, origin, now = await _fixture(
        tmp_path
    )
    running = origin + timedelta(seconds=1)
    await _heartbeat(harness, binding, sequence=2, observed_at=running)
    stopped = running + timedelta(seconds=1)
    await _heartbeat(
        harness,
        binding,
        sequence=3,
        observed_at=stopped,
        phase=HarnessHeartbeatPhase.STOPPED,
    )
    now[0] = stopped
    view = await service.assess(
        request_id=admission.request_id,
        subject_id=admission.subject_id,
    )

    assert view.current_assessment is not None
    assert view.current_assessment.status is (
        EvolutionPostRollbackLongTermObservationStatus.CENSORED
    )
    assert view.current_assessment.censor_reasons == ("runtime_stopped",)
    assert not view.long_term_health_authority
    assert not view.health_alert_authority


@pytest.mark.asyncio
async def test_passing_receipt_is_dynamically_revoked_when_head_stales(
    tmp_path: Path,
) -> None:
    service, _store, admission, harness, binding, origin, now = await _fixture(
        tmp_path
    )
    first = origin + timedelta(seconds=1)
    for offset in range(13):
        latest = first + timedelta(seconds=300 * offset)
        await _heartbeat(
            harness,
            binding,
            sequence=offset + 2,
            observed_at=latest,
        )
    now[0] = latest
    passing = await service.assess(
        request_id=admission.request_id,
        subject_id=admission.subject_id,
    )
    assert passing.long_term_health_authority

    now[0] = latest + timedelta(seconds=301)
    stale = await service.inspect(admission_id=admission.admission_id)
    assert stale.current_assessment is not None
    assert stale.current_assessment.status is (
        EvolutionPostRollbackLongTermObservationStatus.BREACHED
    )
    assert stale.current_assessment.breach_reasons == ("heartbeat_stale",)
    assert not stale.long_term_health_authority
    assert stale.health_alert_authority


@pytest.mark.asyncio
async def test_concurrent_same_head_assessment_converges(tmp_path: Path) -> None:
    service, store, admission, harness, binding, origin, now = await _fixture(tmp_path)
    latest = origin + timedelta(seconds=1)
    await _heartbeat(harness, binding, sequence=2, observed_at=latest)
    now[0] = latest
    peer = EvolutionPostRollbackLongTermObservationAssessmentService(
        workspace_root=service.workspace_root,
        contract_store=service.contract_store,
        contract_service=service.contract_service,
        admission_store=service.admission_store,
        admission_service=service.admission_service,
        harness_store=harness,
        store=EvolutionPostRollbackLongTermObservationAssessmentStore(
            store.db_path,
            contract_store=service.contract_store,
            admission_store=service.admission_store,
            harness_store=harness,
        ),
        clock=lambda: now[0],
    )
    left, right = await asyncio.gather(
        service.assess(
            request_id=admission.request_id,
            subject_id=admission.subject_id,
        ),
        peer.assess(
            request_id=admission.request_id,
            subject_id=admission.subject_id,
        ),
    )
    assert left == right
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_post_rollback_long_term_assessments"
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_latest_prefers_higher_head_when_assessed_at_is_equal(
    tmp_path: Path,
) -> None:
    service, store, admission, harness, binding, origin, now = await _fixture(tmp_path)
    first = origin + timedelta(seconds=1)
    assessed = origin + timedelta(seconds=10)
    await _heartbeat(harness, binding, sequence=2, observed_at=first)
    now[0] = assessed
    earlier = await service.assess(
        request_id=admission.request_id,
        subject_id=admission.subject_id,
    )

    second = origin + timedelta(seconds=2)
    await _heartbeat(harness, binding, sequence=3, observed_at=second)
    later = await service.assess(
        request_id=admission.request_id,
        subject_id=admission.subject_id,
    )
    assert earlier.receipt.assessed_at == later.receipt.assessed_at
    assert earlier.receipt.ledger_head_sequence == 2
    assert later.receipt.ledger_head_sequence == 3
    assert await store.latest(admission_id=admission.admission_id) == later.receipt


@pytest.mark.asyncio
async def test_ledger_reads_across_500_sample_page(tmp_path: Path) -> None:
    service, _store, admission, harness, binding, origin, now = await _fixture(
        tmp_path
    )
    for offset in range(501):
        latest = origin + timedelta(seconds=offset + 1)
        await _heartbeat(
            harness,
            binding,
            sequence=offset + 2,
            observed_at=latest,
        )
    now[0] = latest
    view = await service.assess(
        request_id=admission.request_id,
        subject_id=admission.subject_id,
    )

    assert view.current_assessment is not None
    assert view.current_assessment.sample_count == 502
    assert view.current_assessment.first_sequence == 1
    assert view.current_assessment.ledger_head_sequence == 502
    assert view.current_assessment.status is (
        EvolutionPostRollbackLongTermObservationStatus.INSUFFICIENT
    )


@pytest.mark.asyncio
async def test_assessment_row_tamper_fails_closed(tmp_path: Path) -> None:
    service, store, admission, harness, binding, origin, now = await _fixture(tmp_path)
    latest = origin + timedelta(seconds=1)
    await _heartbeat(harness, binding, sequence=2, observed_at=latest)
    now[0] = latest
    view = await service.assess(
        request_id=admission.request_id,
        subject_id=admission.subject_id,
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_post_rollback_long_term_assessments "
            "SET status = 'passing' WHERE assessment_id = ?",
            (view.receipt.assessment_id,),
        )
    with pytest.raises(EvolutionPostRollbackLongTermObservationAssessmentError) as exc:
        await store.get(view.receipt.assessment_id)
    assert exc.value.code == "post_rollback_long_term_assessment_store_corrupt"


@pytest.mark.asyncio
async def test_long_term_assessment_tool_and_slash_share_service(
    tmp_path: Path,
) -> None:
    service, _store, admission, harness, binding, origin, now = await _fixture(
        tmp_path
    )
    latest = origin + timedelta(seconds=1)
    await _heartbeat(harness, binding, sequence=2, observed_at=latest)
    now[0] = latest
    tool = EvolutionPostRollbackLongTermObservationAssessmentTool(
        type(
            "Engine",
            (),
            {"evolution_post_rollback_long_term_assessment_service": service},
        )()
    )
    arguments = {
        "request_id": admission.request_id,
        "subject_id": admission.subject_id,
    }
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed and not decision.requires_confirmation
    rendered = await tool.execute(**arguments)
    assert "当前判定：`insufficient`" in rendered
    assert "Learning / Promotion / Execution authority：`false / false / false`" in (
        rendered
    )

    registry = ToolRegistry()
    registry.register(tool)

    class _SlashEngine:
        tool_registry = registry

        async def execute_tool(self, call: ToolCall, *, agent_name=None):
            registered = self.tool_registry.get(call.name)
            assert registered is not None and agent_name == "cli"
            parsed = registered.parse_arguments(call.arguments)
            return ToolResult(
                call_id=call.id,
                status="success",
                content=await registered.execute(**parsed),
            )

    slash = await execute_slash_command(
        _SlashEngine(),
        f"/evolution outcome-assess-long-term {admission.request_id} "
        f"{admission.subject_id}",
    )
    assert "Post-Rollback Long-Term Observation" in slash
    assert admission.subject_id in slash
