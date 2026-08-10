from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.post_rollback_remote_results import (
    EvolutionPostRollbackRemoteResultArtifact,
    EvolutionPostRollbackRemoteResultError,
    EvolutionPostRollbackRemoteResultManifest,
    EvolutionPostRollbackRemoteResultPayload,
    EvolutionPostRollbackRemoteResultService,
    EvolutionPostRollbackRemoteResultStore,
    issue_post_rollback_remote_result_manifest,
)
from naumi_agent.harness.eval_identity import (
    HarnessEvalConfigurationIdentity,
    HarnessEvalPlatformIdentity,
    HarnessEvalSourceIdentity,
    build_eval_baseline_identity,
)
from naumi_agent.harness.eval_models import (
    EvalCaseStatus,
    EvalRunStatus,
    HarnessEvalCaseResult,
    HarnessEvalComparisonPolicy,
    HarnessEvalSuiteResult,
)
from naumi_agent.harness.eval_receipt import (
    EvalReceiptSample,
    build_eval_comparison_receipt,
)
from naumi_agent.harness.store import HarnessStore
from naumi_agent.release.runtime_eval import (
    ReleaseRuntimeEvalResponse,
    build_runtime_eval_process_request,
    execute_runtime_eval_process_request,
)
from naumi_agent.safety.permissions import PermissionChecker, PermissionMode
from naumi_agent.tools.base import ToolCall, ToolRegistry, ToolResult
from naumi_agent.tools.evolution_review import EvolutionPostRollbackRemoteResultTool
from tests.unit.test_post_rollback_remote_execution_authorizations import (
    _execution_fixture,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _response(process_request, platform, evaluated_at: str):
    local = execute_runtime_eval_process_request(
        process_request,
        evaluated_at=evaluated_at,
    )
    core = local.model_dump(
        mode="json",
        exclude={"response_id", "response_sha256", "runtime_platform"},
    )
    core["runtime_platform"] = platform.model_dump(mode="json")
    digest = hashlib.sha256(_canonical(core)).hexdigest()
    return ReleaseRuntimeEvalResponse.model_validate(
        {
            **core,
            "response_id": f"relruntimeevalresp_{digest[:24]}",
            "response_sha256": digest,
        }
    )


def _h5_result(payload, baseline, *, duration_ms: float) -> HarnessEvalSuiteResult:
    policy = HarnessEvalComparisonPolicy()
    configuration = HarnessEvalConfigurationIdentity.create(
        suite_id=payload.suite_id,
        suite_sha256=payload.suite_sha256,
        profile_sha256="1" * 64,
        policy_sha256=policy.sha256,
        runner_version=payload.runtime_eval_request.runner_version,
        repetitions=payload.repetitions,
        live=False,
    )
    identity = build_eval_baseline_identity(
        Path(baseline.workspace_root),
        configuration=configuration,
        source_identity=HarnessEvalSourceIdentity(
            commit=baseline.local_baseline_source_commit,
            tree_sha256=f"sha256:{baseline.local_baseline_source_tree_sha256}",
            dirty=False,
        ),
        platform_identity=payload.platform_identity,
        profile_trusted=True,
    )
    return HarnessEvalSuiteResult(
        suite_id=payload.suite_id,
        title="Remote protocol result",
        suite_path="docs/harness/evals/protocol-hello-core.yaml",
        suite_sha256=payload.suite_sha256,
        status=EvalRunStatus.PASSED,
        cases=(
            HarnessEvalCaseResult(
                case_id=payload.runtime_eval_request.cases[0].case_id,
                runner="protocol_hello",
                status=EvalCaseStatus.PASSED,
                primary_metric="protocol_outcome_match",
                duration_ms=duration_ms,
            ),
        ),
        comparison_policy=policy,
        baseline_identity=identity,
        duration_ms=duration_ms,
    )


def _samples(records):
    return tuple(
        EvalReceiptSample(
            sample_index=item.sample_index,
            result_sha256=item.result_sha256,
            result=item.result,
        )
        for item in records
    )


class _BehavioralLaneService:
    def __init__(self, *, workspace: Path, store: HarnessStore, baseline) -> None:
        self.workspace = workspace
        self.store = store
        self.baseline = baseline
        self.validated = 0

    async def validate_remote_runtime_receipts(self, **arguments) -> None:
        receipts = arguments["receipts"]
        assert len(receipts) == 5
        assert arguments["request_id"] == self.baseline.request_id
        assert arguments["comparison_id"] == self.baseline.comparison_id
        self.validated += 1

    async def record_remote_runtime_receipts(self, **arguments):
        receipts = arguments["receipts"]
        batch_id = arguments["batch_id"]
        payload = self.payload
        baseline_records = []
        current_records = []
        for index, receipt in enumerate(receipts):
            result = _h5_result(
                payload,
                self.baseline,
                duration_ms=receipt.response.results[0].duration_ms,
            )
            baseline_records.append(
                await self.store.record_eval_result(
                    workspace_root=self.workspace,
                    batch_id="remote-result-baseline",
                    sample_index=index,
                    result=result,
                    created_at=f"2026-08-10T08:02:0{index}+00:00",
                )
            )
            current_records.append(
                await self.store.record_eval_result(
                    workspace_root=self.workspace,
                    batch_id=batch_id,
                    sample_index=index,
                    result=result,
                    created_at=receipt.evaluated_at,
                )
            )
        baseline = await self.store.register_eval_comparison_reference(
            workspace_root=self.workspace,
            batch_id="remote-result-baseline",
            suite_id=payload.suite_id,
            registered_by="remote-result-test",
            registration_reason="remote result fixture",
            created_at="2026-08-10T08:02:30+00:00",
        )
        comparison = build_eval_comparison_receipt(
            workspace_root=self.workspace,
            suite_id=payload.suite_id,
            baseline_id=baseline.id,
            baseline_batch_id=baseline.batch_id,
            baseline_samples_sha256=baseline.samples_sha256,
            baseline_samples=_samples(baseline_records),
            current_batch_id=batch_id,
            current_samples=_samples(current_records),
            created_at=max(item.created_at for item in current_records),
        )
        stored = await self.store.record_eval_comparison_receipt(comparison)
        return tuple(current_records), stored.receipt


async def _fixture(tmp_path: Path):
    (
        authorization_service,
        delivered,
        dispatch,
        _claim,
        private_key,
        parent,
        harness_store,
        _grant_authority,
        authorization_store,
    ) = await _execution_fixture(tmp_path)
    prepared = await authorization_service.prepare(
        delivery_id=delivered.offer.delivery_id,
        parent_permission_receipt_id=parent.receipt_id,
        issued_at="2026-08-10T08:03:07+00:00",
    )
    start_signature = base64.b64encode(
        private_key.sign(prepared.challenge.payload.canonical_bytes())
    ).decode("ascii")
    authorized = await authorization_service.submit(
        challenge_id=prepared.challenge.challenge_id,
        worker_signature_base64=start_signature,
        authorized_at="2026-08-10T08:03:08+00:00",
    )
    authorization = authorized.authorization
    assert authorization is not None
    baseline = await authorization_service.delivery_service.baseline_store.get_by_id(
        dispatch.baseline_resolution_id
    )
    assert baseline is not None
    platform = HarnessEvalPlatformIdentity(
        system="windows",
        release="11.0.26100",
        machine="AMD64",
        python_implementation="CPython",
        python_version="3.13.5",
        naumi_version="0.1.214",
    )
    process_request = build_runtime_eval_process_request(
        authorization.start_payload.runtime_eval_request
    )
    artifacts = tuple(
        EvolutionPostRollbackRemoteResultArtifact(
            sample_index=index,
            process_request=process_request,
            response=(
                response := _response(
                    process_request,
                    platform,
                    f"2026-08-10T08:03:08.{index + 1}00000+00:00",
                )
            ),
            input_bytes=len(process_request.model_dump_json().encode("utf-8")),
            output_bytes=len(response.model_dump_json().encode("utf-8")),
        )
        for index in range(5)
    )
    start = authorization.start_payload
    payload = EvolutionPostRollbackRemoteResultPayload(
        authorization_id=authorization.authorization_id,
        authorization_sha256=authorization.authorization_sha256,
        attempt_id=authorization.attempt_id,
        delivery_receipt_id=start.delivery_receipt_id,
        delivery_receipt_sha256=start.delivery_receipt_sha256,
        dispatch_id=start.dispatch_id,
        dispatch_sha256=start.dispatch_sha256,
        claim_receipt_id=start.claim_receipt_id,
        claim_receipt_sha256=start.claim_receipt_sha256,
        claim_lease_epoch=start.claim_lease_epoch,
        identity_id=start.identity_id,
        identity_sha256=start.identity_sha256,
        worker_id=start.worker_id,
        worker_instance_id=start.worker_instance_id,
        worker_epoch=start.worker_epoch,
        baseline_resolution_id=baseline.baseline_resolution_id,
        baseline_resolution_sha256=start.baseline_resolution_sha256,
        outcome_id=dispatch.outcome_id,
        request_id=dispatch.request_id,
        comparison_id=dispatch.comparison_id,
        release_target=start.release_target,
        archive_sha256=start.archive_sha256,
        manifest_sha256=start.manifest_sha256,
        installed_binary_sha256="d" * 64,
        suite_id=start.suite_id,
        suite_sha256=start.suite_sha256,
        repetitions=start.repetitions,
        runtime_eval_request=start.runtime_eval_request,
        platform_identity=platform,
        artifacts=artifacts,
        result_bytes=sum(item.output_bytes for item in artifacts),
        run_grant_sha256=authorization.run_grant.grant_sha256,
        runtime_lease_epoch=authorization.runtime_lease_epoch,
        started_at="2026-08-10T08:03:08+00:00",
        completed_at="2026-08-10T08:03:09+00:00",
    )
    manifest = issue_post_rollback_remote_result_manifest(
        payload=payload,
        private_key=private_key,
    )
    behavioral = _BehavioralLaneService(
        workspace=Path(dispatch.workspace_root),
        store=harness_store,
        baseline=baseline,
    )
    behavioral.payload = payload
    result_store = EvolutionPostRollbackRemoteResultStore(
        authorization_store.db_path,
        control_plane_key_provider=lambda: b"remote-result-control-plane-key-32",
    )
    service = EvolutionPostRollbackRemoteResultService(
        workspace_root=dispatch.workspace_root,
        authorization_service=authorization_service,
        authorization_store=authorization_store,
        behavioral_lane_service=behavioral,  # type: ignore[arg-type]
        harness_store=harness_store,
        store=result_store,
    )
    return service, manifest, private_key, behavioral, result_store


@pytest.mark.asyncio
async def test_signed_result_is_atomically_admitted_and_ingested_idempotently(
    tmp_path: Path,
) -> None:
    service, manifest, _key, behavioral, store = await _fixture(tmp_path)
    first = await service.submit(
        manifest=manifest,
        received_at="2026-08-10T08:03:10+00:00",
    )
    repeated = await service.submit(
        manifest=manifest,
        received_at="2026-08-10T08:03:12+00:00",
    )

    assert repeated == first
    assert first.status == "ingested"
    assert first.h5a_authority and first.h5c_authority
    assert first.receipt is not None and first.receipt.full_cohort_received
    assert not first.behavioral_matrix_authority
    assert behavioral.validated == 1
    assert await store.get_admitted_at(manifest.manifest_id) is not None
    assert await store.list_by_lane(
        outcome_id=manifest.payload.outcome_id,
        comparison_id=manifest.payload.comparison_id,
    ) == (manifest,)


@pytest.mark.asyncio
async def test_forged_or_late_unadmitted_result_fails_before_h5_side_effects(
    tmp_path: Path,
) -> None:
    service, manifest, key, behavioral, store = await _fixture(tmp_path)
    payload = manifest.payload.model_copy(update={"installed_binary_sha256": "e" * 64})
    forged = EvolutionPostRollbackRemoteResultManifest.model_validate(
        {
            **manifest.model_dump(mode="json"),
            "payload": payload.model_dump(mode="json"),
            "manifest_sha256": hashlib.sha256(
                _canonical(payload.model_dump(mode="json"))
            ).hexdigest(),
            "manifest_id": "evpostresultmanifest_"
            + hashlib.sha256(_canonical(payload.model_dump(mode="json"))).hexdigest()[:24],
        }
    )
    with pytest.raises(EvolutionPostRollbackRemoteResultError) as signature_error:
        await service.submit(
            manifest=forged,
            received_at="2026-08-10T08:03:10+00:00",
        )
    assert signature_error.value.code == "post_rollback_remote_result_signature_invalid"
    assert behavioral.validated == 0
    assert await store.get_manifest(forged.manifest_id) is None

    valid = issue_post_rollback_remote_result_manifest(payload=payload, private_key=key)
    with pytest.raises(EvolutionPostRollbackRemoteResultError) as late:
        await service.submit(
            manifest=valid,
            received_at="2026-08-10T08:03:12+00:00",
        )
    assert late.value.code == "post_rollback_remote_result_authorization_not_current"
    assert behavioral.validated == 0
    assert await store.get_manifest(valid.manifest_id) is None


@pytest.mark.asyncio
async def test_concurrent_result_ingestion_converges_across_services(
    tmp_path: Path,
) -> None:
    service, manifest, _key, behavioral, store = await _fixture(tmp_path)
    peer_store = EvolutionPostRollbackRemoteResultStore(
        store.db_path,
        control_plane_key_provider=lambda: b"remote-result-control-plane-key-32",
    )
    peer = EvolutionPostRollbackRemoteResultService(
        workspace_root=service.workspace_root,
        authorization_service=service.authorization_service,
        authorization_store=service.authorization_store,
        behavioral_lane_service=behavioral,  # type: ignore[arg-type]
        harness_store=service.harness_store,
        store=peer_store,
    )
    left, right = await asyncio.gather(
        service.submit(
            manifest=manifest,
            received_at="2026-08-10T08:03:10+00:00",
        ),
        peer.submit(
            manifest=manifest,
            received_at="2026-08-10T08:03:10+00:00",
        ),
    )
    assert left == right and left.status == "ingested"
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_post_rollback_remote_result_manifests"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_post_rollback_remote_result_receipts"
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_admission_tamper_and_incomplete_cohort_fail_closed(tmp_path: Path) -> None:
    service, manifest, _key, _behavioral, store = await _fixture(tmp_path)
    artifacts = [
        item.model_dump(mode="json") for item in manifest.payload.artifacts
    ]
    with pytest.raises(ValidationError):
        EvolutionPostRollbackRemoteResultPayload.model_validate(
            {
                **manifest.payload.model_dump(mode="json"),
                "artifacts": artifacts[:-1],
            }
        )
    non_contiguous = [dict(item) for item in artifacts]
    non_contiguous[1]["sample_index"] = 2
    with pytest.raises(ValidationError):
        EvolutionPostRollbackRemoteResultPayload.model_validate(
            {
                **manifest.payload.model_dump(mode="json"),
                "artifacts": non_contiguous,
            }
        )
    duplicate_response = [dict(item) for item in artifacts]
    duplicate_response[1]["response"] = artifacts[0]["response"]
    with pytest.raises(ValidationError):
        EvolutionPostRollbackRemoteResultPayload.model_validate(
            {
                **manifest.payload.model_dump(mode="json"),
                "artifacts": duplicate_response,
            }
        )
    await service.submit(
        manifest=manifest,
        received_at="2026-08-10T08:03:10+00:00",
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_post_rollback_remote_result_manifests "
            "SET admission_attestation_sha256 = ? WHERE manifest_id = ?",
            ("0" * 64, manifest.manifest_id),
        )
    with pytest.raises(EvolutionPostRollbackRemoteResultError) as tampered:
        await store.get_manifest(manifest.manifest_id)
    assert tampered.value.code == "post_rollback_remote_result_admission_tampered"


@pytest.mark.asyncio
async def test_remote_result_lane_index_tamper_fails_closed(tmp_path: Path) -> None:
    service, manifest, _key, _behavioral, store = await _fixture(tmp_path)
    await service.submit(
        manifest=manifest,
        received_at="2026-08-10T08:03:10+00:00",
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_post_rollback_remote_result_lane_index "
            "SET suite_id = ? WHERE manifest_id = ?",
            ("tampered-suite", manifest.manifest_id),
        )
    with pytest.raises(EvolutionPostRollbackRemoteResultError) as tampered:
        await store.list_by_lane(
            outcome_id=manifest.payload.outcome_id,
            comparison_id=manifest.payload.comparison_id,
        )
    assert tampered.value.code == "post_rollback_remote_result_lane_index_tampered"


@pytest.mark.asyncio
async def test_remote_result_lane_index_backfill_converges_concurrently(
    tmp_path: Path,
) -> None:
    service, manifest, _key, _behavioral, store = await _fixture(tmp_path)
    await service.submit(
        manifest=manifest,
        received_at="2026-08-10T08:03:10+00:00",
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "DELETE FROM evolution_post_rollback_remote_result_lane_index "
            "WHERE manifest_id = ?",
            (manifest.manifest_id,),
        )
    peer = EvolutionPostRollbackRemoteResultStore(
        store.db_path,
        control_plane_key_provider=lambda: b"remote-result-control-plane-key-32",
    )
    kwargs = {
        "outcome_id": manifest.payload.outcome_id,
        "comparison_id": manifest.payload.comparison_id,
    }
    left, right = await asyncio.gather(
        store.list_by_lane(**kwargs),
        peer.list_by_lane(**kwargs),
    )
    assert left == right == (manifest,)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_post_rollback_remote_result_lane_index"
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_remote_result_tool_and_slash_share_the_same_service(tmp_path: Path) -> None:
    service, manifest, _key, _behavioral, _store = await _fixture(tmp_path)
    service.clock = lambda: datetime(2026, 8, 10, 8, 3, 10, tzinfo=UTC)
    tool = EvolutionPostRollbackRemoteResultTool(
        SimpleNamespace(evolution_post_rollback_remote_result_service=service)
    )
    arguments = {
        "action": "submit",
        "manifest_json": manifest.model_dump_json(),
    }
    for mode in (PermissionMode.MODERATE, PermissionMode.BYPASS):
        decision = PermissionChecker(mode).check(tool.name, arguments, tool=tool)
        assert decision.allowed and not decision.requires_confirmation
    rendered = await tool.execute(**arguments)
    assert "H5a/H5c：`true` / `true`" in rendered

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
        "/evolution outcome-ingest-behavior inspect " + manifest.manifest_id,
    )
    assert manifest.manifest_id in slash and "状态：`ingested`" in slash
