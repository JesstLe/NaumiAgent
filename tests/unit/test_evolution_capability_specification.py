from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.candidate import build_candidate_draft
from naumi_agent.evolution.capability_artifact import (
    CapabilityArtifactError,
    EvolutionCapabilityArtifactService,
    EvolutionCapabilityArtifactStore,
    build_capability_implementation_artifact,
    render_capability_artifact,
)
from naumi_agent.evolution.capability_governance import (
    CapabilityGovernanceError,
    CapabilitySpecificationAssessor,
    EvolutionCapabilityGovernanceService,
    EvolutionCapabilityGovernanceStore,
    render_capability_governance,
)
from naumi_agent.evolution.capability_proposal import generate_capability_proposal
from naumi_agent.evolution.capability_sandbox_request import (
    CapabilitySandboxRequestError,
    EvolutionCapabilitySandboxRequestService,
    EvolutionCapabilitySandboxRequestStore,
    build_capability_sandbox_execution_request,
    render_capability_sandbox_request,
)
from naumi_agent.evolution.capability_scenario_binding import (
    CapabilityScenarioBindingError,
    EvolutionCapabilityScenarioBindingService,
    EvolutionCapabilityScenarioBindingStore,
    build_capability_scenario_binding,
    render_capability_scenario_binding,
)
from naumi_agent.evolution.capability_specification import (
    CapabilityDataSpecification,
    CapabilityInterfaceSpecification,
    CapabilityPermissionSpecification,
    CapabilitySpecificationStoreError,
    EvolutionCapabilitySpecificationService,
    EvolutionCapabilitySpecificationStore,
    render_capability_specification,
)
from naumi_agent.evolution.eligibility import (
    CandidateGovernanceContext,
    assess_candidate_eligibility,
)
from naumi_agent.evolution.evidence import EvolutionEvidence, EvolutionEvidenceRef
from naumi_agent.evolution.prioritization import (
    CandidatePrioritySubject,
    prioritize_candidates,
)
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.interaction import new_interaction_record
from naumi_agent.harness.sandbox_request import capture_clean_revision
from naumi_agent.harness.store import HarnessStore
from naumi_agent.tools.evolution_review import (
    EvolutionCapabilityArtifactTool,
    EvolutionCapabilityGovernanceTool,
    EvolutionCapabilitySandboxRequestTool,
    EvolutionCapabilityScenarioBindingTool,
)
from naumi_agent.user_interaction import normalize_interaction_request

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _proposal(tmp_path: Path):
    uri = "tool-search://misses/tsm_" + "a" * 24
    scope = "capability:tool:browser_trace_compare"
    evidence = EvolutionEvidence(
        evidence_id=f"eve_{_digest(uri)[:24]}",
        source_kind="tool_catalog_miss",
        source_uri=uri,
        observed_at=NOW.isoformat(),
        finding_code="missing_tool_capability",
        scope=scope,
        root_fingerprint=_digest(f"missing_tool_capability:{scope}"),
        refs=(EvolutionEvidenceRef(uri=uri, sha256=_digest(uri)),),
    )
    candidate = build_candidate_draft((evidence,))
    candidate_store = EvolutionCandidateStore(tmp_path / "evolution.db")
    stored = await candidate_store.upsert_candidate(tmp_path, candidate)
    governance = CandidateGovernanceContext(
        allowed=True,
        reason="no_active_cooldown",
    )
    eligibility = assess_candidate_eligibility(
        candidate,
        governance=governance,
        source_authority_valid=True,
    )
    portfolio = prioritize_candidates((CandidatePrioritySubject(
        candidate=candidate,
        eligibility=eligibility,
    ),))
    proposal = generate_capability_proposal(
        stored,
        eligibility=eligibility,
        priority=portfolio.priorities[0],
        portfolio=portfolio,
    )
    assert proposal is not None
    return proposal


class _Review:
    def __init__(self, proposal) -> None:
        self.proposal = proposal

    async def detail_snapshot(
        self,
        _workspace: Path,
        _candidate_id: str,
        **_kwargs: object,
    ):
        selected = (
            None
            if self.proposal is None
            else SimpleNamespace(capability_proposal=self.proposal)
        )
        return SimpleNamespace(selected=selected)


class _Clock:
    def __init__(self) -> None:
        self.index = 0

    def __call__(self) -> datetime:
        value = NOW + timedelta(seconds=self.index * 2)
        self.index += 1
        return value


class _InteractionHost:
    def __init__(
        self,
        *,
        store: HarnessStore,
        workspace: Path,
        responses: list[dict[str, str]],
        clock: _Clock,
        fail_after_commit: bool = False,
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.responses = responses
        self.clock = clock
        self.fail_after_commit = fail_after_commit
        self.after_answer: Callable[[], None] | None = None
        self.requests: list[dict[str, object]] = []

    async def __call__(self, payload: dict[str, object]) -> dict[str, str]:
        request = normalize_interaction_request(payload)
        interaction_id = str(payload["_interaction_id"])
        created = self.clock()
        record = new_interaction_record(
            request=request,
            subject_kind="tool",
            subject_id=str(payload["_durable_subject_id"]),
            session_id="spec-session",
            agent_name="Human",
            owner_id="test-host",
            created_at=created.isoformat(),
            owner_lease_seconds=60,
            interaction_id=interaction_id,
        )
        record = await self.store.create_interaction(
            workspace_root=self.workspace,
            record=record,
        )
        response = self.responses.pop(0)
        await self.store.answer_interaction(
            workspace_root=self.workspace,
            interaction_id=interaction_id,
            expected_sequence=record.sequence,
            owner_id=record.owner_id,
            owner_epoch=record.owner_epoch,
            response=response,
            answered_by="user",
            now=(created + timedelta(seconds=1)).isoformat(),
        )
        self.requests.append(payload)
        if self.after_answer is not None:
            self.after_answer()
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise RuntimeError("simulated host crash after durable answer")
        return response


def _answers() -> list[dict[str, str]]:
    payloads = [
        {
            "tool_name": "browser_trace_compare",
            "parameters_schema": {
                "type": "object",
                "properties": {"left": {"type": "string"}},
                "required": ["left"],
                "additionalProperties": False,
            },
            "result_schema": {
                "type": "object",
                "properties": {"differences": {"type": "array"}},
                "required": ["differences"],
                "additionalProperties": False,
            },
            "errors": [
                {"code": "trace_missing", "message": "轨迹不存在", "retryable": False}
            ],
            "version": "1.0.0",
        },
        {
            "requirements": [
                {
                    "family": "workspace_read",
                    "scopes": ["data/traces/**"],
                    "justification": "读取用户指定的轨迹引用",
                }
            ]
        },
        {
            "input_classes": ["workspace_content"],
            "output_classes": ["generated_content"],
            "retention": "none",
            "sensitive_handling": "deny",
        },
        {
            "scenarios": [
                {
                    "name": "比较两份真实轨迹",
                    "fixture": "两份已脱敏且存在的轨迹引用",
                    "expected": "稳定输出差异集合并标记无法解析的事件",
                }
            ]
        },
        {
            "owner": "runtime-tools",
            "latency_p95_ms": 1500,
            "success_rate_percent": 99.0,
            "maintenance": "每个 minor 版本回放固定真实轨迹集并复核 schema 兼容性",
            "additional_retirement_criteria": ["trace_format_removed"],
        },
    ]
    return [
        {"kind": "custom", "custom_text": json.dumps(item, ensure_ascii=False)}
        for item in payloads
    ]


def test_specification_fields_reject_secret_ref_and_unsafe_scope() -> None:
    interface = json.loads(_answers()[0]["custom_text"])
    interface["parameters_schema"]["properties"]["secret"] = {
        "type": "string",
        "description": "api_key=sk-secretvalue",
    }
    with pytest.raises(ValueError, match="secret"):
        CapabilityInterfaceSpecification.model_validate(interface)

    interface = json.loads(_answers()[0]["custom_text"])
    interface["result_schema"] = {"type": "object", "$ref": "remote.json"}
    with pytest.raises(ValueError, match=r"\$ref"):
        CapabilityInterfaceSpecification.model_validate(interface)

    with pytest.raises(ValueError, match="安全相对路径"):
        CapabilityPermissionSpecification.model_validate({
            "requirements": [{
                "family": "workspace_write",
                "scopes": ["../outside/**"],
                "justification": "写入生成物",
            }],
        })

    with pytest.raises(ValueError, match="reference_only"):
        CapabilityDataSpecification.model_validate({
            "input_classes": ["credential_reference"],
            "output_classes": ["generated_content"],
            "retention": "durable_reference_only",
            "sensitive_handling": "deny",
        })


def _service(
    *,
    proposal,
    workspace: Path,
    responses: list[dict[str, str]],
    clock: _Clock | None = None,
    fail_after_commit: bool = False,
):
    active_clock = clock or _Clock()
    harness_store = HarnessStore(workspace / "harness.db")
    specification_store = EvolutionCapabilitySpecificationStore(
        workspace / "evolution.db"
    )
    host = _InteractionHost(
        store=harness_store,
        workspace=workspace,
        responses=responses,
        clock=active_clock,
        fail_after_commit=fail_after_commit,
    )
    service = EvolutionCapabilitySpecificationService(
        review_service=_Review(proposal),
        store=specification_store,
        interaction_store=harness_store,
        request_user_input=host,
        clock=active_clock,
    )
    return service, specification_store, harness_store, host


async def _complete_specification(tmp_path: Path):
    proposal = await _proposal(tmp_path)
    service, specification_store, harness_store, host = _service(
        proposal=proposal,
        workspace=tmp_path,
        responses=_answers(),
    )
    view = None
    for _ in range(5):
        view = await service.advance(
            tmp_path,
            candidate_id=proposal.source.candidate_id,
            session_id="spec-session",
            agent_name="Human",
        )
    assert view is not None and view.state == "complete"
    return proposal, service, specification_store, harness_store, host, view


def _governance_service(
    *,
    tmp_path: Path,
    proposal,
    specification_service,
    harness_store: HarnessStore,
    responses: list[dict[str, str]],
    fail_after_commit: bool = False,
):
    clock = _Clock()
    host = _InteractionHost(
        store=harness_store,
        workspace=tmp_path,
        responses=responses,
        clock=clock,
        fail_after_commit=fail_after_commit,
    )
    store = EvolutionCapabilityGovernanceStore(tmp_path / "evolution.db")
    service = EvolutionCapabilityGovernanceService(
        review_service=_Review(proposal),
        specification_service=specification_service,
        assessor=CapabilitySpecificationAssessor(harness_store),
        store=store,
        interaction_store=harness_store,
        request_user_input=host,
    )
    return service, store, host


@pytest.mark.asyncio
async def test_five_interactions_form_append_only_complete_specification(
    tmp_path: Path,
) -> None:
    proposal = await _proposal(tmp_path)
    service, store, _, host = _service(
        proposal=proposal,
        workspace=tmp_path,
        responses=_answers(),
    )

    views = []
    for _ in range(5):
        views.append(await service.advance(
            tmp_path,
            candidate_id=proposal.source.candidate_id,
            session_id="spec-session",
            agent_name="Human",
        ))

    assert [item.revision for item in views] == [1, 2, 3, 4, 5]
    final = views[-1]
    assert final.state == "complete"
    assert final.pending_step is None
    assert final.unresolved_requirements == ()
    assert final.specification is not None
    assert final.specification.interface is not None
    assert final.specification.interface.tool_name == "browser_trace_compare"
    assert final.specification.permissions is not None
    assert final.specification.permissions.requirements[0].scopes == (
        "data/traces/**",
    )
    assert final.specification.sandbox_eligible is False
    assert final.specification.shadow_eligible is False
    assert final.specification.executable is False
    assert len(final.specification.interaction_sources) == 5
    assert all(
        source.interaction_sequence == 2
        for source in final.specification.interaction_sources
    )
    assert len(host.requests) == 5
    assert "规格完整只表示" in render_capability_specification(final)

    latest = await store.latest(tmp_path, final.specification_id)
    assert latest == final.specification
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM evolution_capability_specifications"
        ).fetchone() == (5,)


@pytest.mark.asyncio
async def test_defer_does_not_create_revision_and_next_attempt_can_complete(
    tmp_path: Path,
) -> None:
    proposal = await _proposal(tmp_path)
    answers = [{"kind": "option", "value": "defer"}, _answers()[0]]
    service, store, _, host = _service(
        proposal=proposal,
        workspace=tmp_path,
        responses=answers,
    )

    deferred = await service.advance(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
        session_id="spec-session",
        agent_name="Human",
    )
    assert deferred.revision == 0
    assert await store.latest(tmp_path, deferred.specification_id) is None

    completed = await service.advance(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
        session_id="spec-session",
        agent_name="Human",
    )

    assert completed.revision == 1
    assert host.requests[0]["_interaction_id"].endswith("-interface-1")
    assert host.requests[1]["_interaction_id"].endswith("-interface-2")


@pytest.mark.asyncio
async def test_invalid_answer_does_not_poison_following_valid_attempt(tmp_path: Path) -> None:
    proposal = await _proposal(tmp_path)
    invalid = _answers()[0].copy()
    payload = json.loads(invalid["custom_text"])
    payload["tool_name"] = "forged_name"
    invalid["custom_text"] = json.dumps(payload)
    service, store, _, host = _service(
        proposal=proposal,
        workspace=tmp_path,
        responses=[invalid, _answers()[0]],
    )

    with pytest.raises(ValueError, match="不得改写"):
        await service.advance(
            tmp_path,
            candidate_id=proposal.source.candidate_id,
            session_id="spec-session",
            agent_name="Human",
        )
    view = await service.advance(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
        session_id="spec-session",
        agent_name="Human",
    )

    assert view.revision == 1
    assert await store.latest(tmp_path, view.specification_id) is not None
    assert host.requests[-1]["_interaction_id"].endswith("-interface-2")


@pytest.mark.asyncio
async def test_answer_committed_before_host_crash_is_reconciled(tmp_path: Path) -> None:
    proposal = await _proposal(tmp_path)
    clock = _Clock()
    service, store, harness, host = _service(
        proposal=proposal,
        workspace=tmp_path,
        responses=[_answers()[0]],
        clock=clock,
        fail_after_commit=True,
    )
    with pytest.raises(RuntimeError, match="host crash"):
        await service.advance(
            tmp_path,
            candidate_id=proposal.source.candidate_id,
            session_id="spec-session",
            agent_name="Human",
        )
    interactions = await harness.list_interactions(
        workspace_root=tmp_path,
        subject_kind="tool",
        subject_ids=(),
        limit=10,
    )
    assert len(interactions) == 1
    assert await store.latest(tmp_path, interactions[0].subject_id) is None

    recovered_service = EvolutionCapabilitySpecificationService(
        review_service=_Review(proposal),
        store=store,
        interaction_store=harness,
        request_user_input=host,
        clock=clock,
    )
    recovered = await recovered_service.advance(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
        session_id="spec-session",
        agent_name="Human",
    )

    assert recovered.revision == 1
    assert recovered.specification is not None
    assert len(host.requests) == 1


@pytest.mark.asyncio
async def test_same_interaction_is_idempotent_under_concurrent_store_retries(
    tmp_path: Path,
) -> None:
    proposal = await _proposal(tmp_path)
    service, store, harness, _ = _service(
        proposal=proposal,
        workspace=tmp_path,
        responses=[_answers()[0]],
    )
    first = await service.advance(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
        session_id="spec-session",
        agent_name="Human",
    )
    assert first.specification is not None
    source = first.specification.interaction_sources[0]
    interaction = await harness.get_interaction(
        workspace_root=tmp_path,
        interaction_id=source.interaction_id,
    )
    assert interaction is not None

    results = await asyncio.gather(*(
        store.record_step(
            tmp_path,
            proposal=proposal,
            step="interface",
            value=first.specification.interface,
            interaction=interaction,
            created_at=NOW.isoformat(),
        )
        for _ in range(8)
    ))

    assert all(item == first.specification for item in results)


@pytest.mark.asyncio
async def test_tamper_and_revoked_current_proposal_fail_closed(tmp_path: Path) -> None:
    proposal = await _proposal(tmp_path)
    service, store, _, _ = _service(
        proposal=proposal,
        workspace=tmp_path,
        responses=[_answers()[0]],
    )
    view = await service.advance(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
        session_id="spec-session",
        agent_name="Human",
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_capability_specifications SET payload_sha256 = ?",
            ("0" * 64,),
        )
        db.commit()
    with pytest.raises(CapabilitySpecificationStoreError, match="摘要"):
        await store.latest(tmp_path, view.specification_id)

    service.review_service.proposal = None
    with pytest.raises(CapabilitySpecificationStoreError, match="没有"):
        await service.inspect(tmp_path, proposal.source.candidate_id)


@pytest.mark.asyncio
async def test_authority_revoked_while_answering_prevents_revision(tmp_path: Path) -> None:
    proposal = await _proposal(tmp_path)
    service, store, _, host = _service(
        proposal=proposal,
        workspace=tmp_path,
        responses=[_answers()[0]],
    )
    host.after_answer = lambda: setattr(service.review_service, "proposal", None)

    with pytest.raises(CapabilitySpecificationStoreError, match="没有"):
        await service.advance(
            tmp_path,
            candidate_id=proposal.source.candidate_id,
            session_id="spec-session",
            agent_name="Human",
        )

    specification_id = str(host.requests[0]["_durable_subject_id"])
    assert await store.latest(tmp_path, specification_id) is None


@pytest.mark.asyncio
async def test_complete_specification_replays_and_user_approval_is_authority_closed(
    tmp_path: Path,
) -> None:
    proposal, specification_service, _, harness, _, specification = (
        await _complete_specification(tmp_path)
    )
    governance, store, host = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[{"kind": "option", "value": "approve"}],
    )

    view = await governance.decide(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )

    assert view.state == "approved"
    assert view.decision_effective is True
    assert view.sandbox_design_eligible is True
    assert view.registration_authorized is False
    assert view.shadow_authorized is False
    assert view.executable is False
    assert view.assessment.eligible_for_decision is True
    assert all(check.passed for check in view.assessment.checks)
    assert view.decision is not None
    assert view.decision.specification_sha256 == specification.specification.digest()
    assert view.decision.source_interaction_id.startswith("ask-evcgov-")
    assert await store.latest(tmp_path, view.specification_id) == view.decision
    assert len(host.requests) == 1
    rendered = render_capability_governance(view)
    assert "Sandbox 实现设计资格：是" in rendered
    assert "Registry 注册：否" in rendered
    engine = SimpleNamespace(
        workspace_root=tmp_path,
        evolution_capability_governance_service=governance,
        evolution_review_service=governance.review_service,
        _session=SimpleNamespace(id="spec-session"),
    )
    tool_output = await EvolutionCapabilityGovernanceTool(engine).execute(
        candidate_id=proposal.source.candidate_id,
        action="inspect",
    )
    slash_output = await execute_slash_command(
        engine,
        f"/evolution capability-govern {proposal.source.candidate_id}",
    )
    assert view.decision.decision_id in tool_output
    assert view.decision.decision_id in slash_output


@pytest.mark.asyncio
async def test_governance_inspect_requires_complete_specification(tmp_path: Path) -> None:
    proposal = await _proposal(tmp_path)
    specification_service, _, harness, _ = _service(
        proposal=proposal,
        workspace=tmp_path,
        responses=[],
    )
    governance, _, _ = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[],
    )

    with pytest.raises(CapabilityGovernanceError, match="尚未完成"):
        await governance.inspect(tmp_path, proposal.source.candidate_id)


@pytest.mark.asyncio
async def test_governance_defer_writes_no_terminal_and_reject_is_first_terminal(
    tmp_path: Path,
) -> None:
    proposal, specification_service, _, harness, _, _ = (
        await _complete_specification(tmp_path)
    )
    governance, store, host = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[
            {"kind": "option", "value": "defer"},
            {"kind": "custom", "custom_text": "真实场景验收范围仍不足"},
        ],
    )

    deferred = await governance.decide(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    assert deferred.state == "awaiting_decision"
    assert await store.latest(tmp_path, deferred.specification_id) is None

    rejected = await governance.decide(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    assert rejected.state == "rejected"
    assert rejected.sandbox_design_eligible is False
    assert rejected.decision is not None
    assert rejected.decision.reason == "真实场景验收范围仍不足"
    assert host.requests[0]["_interaction_id"].endswith("-1")
    assert host.requests[1]["_interaction_id"].endswith("-2")


@pytest.mark.asyncio
async def test_invalid_governance_reason_does_not_poison_next_attempt(tmp_path: Path) -> None:
    proposal, specification_service, _, harness, _, _ = (
        await _complete_specification(tmp_path)
    )
    governance, store, host = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[
            {"kind": "custom", "custom_text": "x" * 1_001},
            {"kind": "option", "value": "approve"},
        ],
    )

    with pytest.raises(CapabilityGovernanceError, match="拒绝原因无效"):
        await governance.decide(
            tmp_path,
            candidate_id=proposal.source.candidate_id,
        )
    approved = await governance.decide(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    assert approved.state == "approved"
    assert await store.latest(tmp_path, approved.specification_id) is not None
    assert host.requests[1]["_interaction_id"].endswith("-2")


@pytest.mark.asyncio
async def test_governance_answered_before_crash_is_recovered_once(tmp_path: Path) -> None:
    proposal, specification_service, _, harness, _, _ = (
        await _complete_specification(tmp_path)
    )
    governance, store, _ = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[{"kind": "option", "value": "approve"}],
        fail_after_commit=True,
    )
    with pytest.raises(RuntimeError, match="host crash"):
        await governance.decide(
            tmp_path,
            candidate_id=proposal.source.candidate_id,
        )
    assert await store.latest(tmp_path, f"evcs_{'0' * 24}") is None

    recovered, _, recovery_host = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[],
    )
    view = await recovered.decide(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    assert view.state == "approved"
    assert recovery_host.requests == []


@pytest.mark.asyncio
async def test_governance_rejects_non_user_specification_evidence(tmp_path: Path) -> None:
    proposal, specification_service, _, harness, _, specification = (
        await _complete_specification(tmp_path)
    )
    assert specification.specification is not None
    first_source = specification.specification.interaction_sources[0]
    original = await harness.get_interaction(
        workspace_root=tmp_path,
        interaction_id=first_source.interaction_id,
    )
    assert original is not None

    class _ForgedInteractionStore:
        async def get_interaction(self, **kwargs: object):
            if kwargs["interaction_id"] == original.interaction_id:
                return original.model_copy(update={"answered_by": "agent"})
            return await harness.get_interaction(**kwargs)

    assessment = await CapabilitySpecificationAssessor(
        _ForgedInteractionStore()  # type: ignore[arg-type]
    ).assess(
        tmp_path,
        proposal=proposal,
        specification_view=specification,
    )
    assert assessment.eligible_for_decision is False
    assert next(
        check for check in assessment.checks if check.code == "interaction_integrity"
    ).passed is False


@pytest.mark.asyncio
async def test_governance_revalidates_authority_after_user_answer(tmp_path: Path) -> None:
    proposal, specification_service, _, harness, _, _ = (
        await _complete_specification(tmp_path)
    )
    governance, store, host = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[{"kind": "option", "value": "approve"}],
    )
    host.after_answer = lambda: setattr(governance.review_service, "proposal", None)

    with pytest.raises(CapabilityGovernanceError, match="没有"):
        await governance.decide(
            tmp_path,
            candidate_id=proposal.source.candidate_id,
        )
    specification_id = str(host.requests[0]["_durable_subject_id"])
    assert await store.latest(tmp_path, specification_id) is None


@pytest.mark.asyncio
async def test_governance_first_terminal_wins_concurrent_decisions(tmp_path: Path) -> None:
    proposal, specification_service, _, harness, _, specification = (
        await _complete_specification(tmp_path)
    )
    assert specification.specification is not None
    assessor = CapabilitySpecificationAssessor(harness)
    assessment = await assessor.assess(
        tmp_path,
        proposal=proposal,
        specification_view=specification,
    )
    clock = _Clock()
    host = _InteractionHost(
        store=harness,
        workspace=tmp_path,
        responses=[
            {"kind": "option", "value": "approve"},
            {"kind": "custom", "custom_text": "拒绝并发覆盖"},
        ],
        clock=clock,
    )
    base_payload = {
        "header": "能力规格治理",
        "question": "请选择批准，或填写拒绝原因。",
        "options": [
            {"value": "approve", "label": "批准规格", "description": "进入设计。"},
            {"value": "defer", "label": "稍后决定", "description": "暂不决定。"},
        ],
        "allow_custom": True,
        "custom_label": "填写拒绝原因",
        "priority": "high",
        "_durable_subject_kind": "tool",
        "_durable_subject_id": specification.specification_id,
    }
    first_id = f"ask-evcgov-{specification.specification_id[5:]}-1"
    second_id = f"ask-evcgov-{specification.specification_id[5:]}-2"
    await host({**base_payload, "_interaction_id": first_id})
    await host({**base_payload, "_interaction_id": second_id})
    first = await harness.get_interaction(
        workspace_root=tmp_path,
        interaction_id=first_id,
    )
    second = await harness.get_interaction(
        workspace_root=tmp_path,
        interaction_id=second_id,
    )
    assert first is not None and second is not None
    store = EvolutionCapabilityGovernanceStore(tmp_path / "evolution.db")
    competing_store = EvolutionCapabilityGovernanceStore(tmp_path / "evolution.db")

    results = await asyncio.gather(
        competing_store.record(
            tmp_path,
            assessment=assessment,
            interaction=first,
            outcome="approved",
            reason="用户明确批准该完整规格进入 Sandbox 实现设计阶段。",
        ),
        store.record(
            tmp_path,
            assessment=assessment,
            interaction=second,
            outcome="rejected",
            reason="拒绝并发覆盖",
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, CapabilityGovernanceError) for result in results) == 1
    terminal = await store.latest(tmp_path, specification.specification_id)
    assert terminal is not None
    assert terminal.source_interaction_id in {first_id, second_id}


@pytest.mark.asyncio
async def test_governance_tampered_assessment_snapshot_fails_closed(tmp_path: Path) -> None:
    proposal, specification_service, _, harness, _, _ = (
        await _complete_specification(tmp_path)
    )
    governance, store, _ = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[{"kind": "option", "value": "approve"}],
    )
    view = await governance.decide(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE evolution_capability_governance_decisions "
            "SET assessment_json = ? WHERE specification_id = ?",
            ("{}", view.specification_id),
        )
        db.commit()

    with pytest.raises(CapabilityGovernanceError, match="JSON 损坏"):
        await store.latest(tmp_path, view.specification_id)


@pytest.mark.asyncio
async def test_historical_approval_is_revoked_when_spec_evidence_no_longer_replays(
    tmp_path: Path,
) -> None:
    proposal, specification_service, _, harness, _, specification = (
        await _complete_specification(tmp_path)
    )
    governance, _, _ = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[{"kind": "option", "value": "approve"}],
    )
    approved = await governance.decide(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    assert approved.state == "approved"
    assert specification.specification is not None
    source = specification.specification.interaction_sources[0]
    original = await harness.get_interaction(
        workspace_root=tmp_path,
        interaction_id=source.interaction_id,
    )
    assert original is not None

    class _ChangedEvidenceStore:
        async def get_interaction(self, **kwargs: object):
            if kwargs["interaction_id"] == original.interaction_id:
                return original.model_copy(update={"answered_by": "agent"})
            return await harness.get_interaction(**kwargs)

    governance.assessor = CapabilitySpecificationAssessor(
        _ChangedEvidenceStore()  # type: ignore[arg-type]
    )
    revoked = await governance.inspect(
        tmp_path,
        proposal.source.candidate_id,
    )
    assert revoked.state == "revoked"
    assert revoked.decision_effective is False
    assert revoked.sandbox_design_eligible is False
    assert revoked.registration_authorized is False
    assert revoked.executable is False


def _implementation_source(tool_name: str, schema: dict[str, object]) -> str:
    return f'''from naumi_agent.tools.base import Tool


class BrowserTraceCompareTool(Tool):
    @property
    def name(self):
        return {tool_name!r}

    @property
    def description(self):
        return "比较两份真实浏览器轨迹。"

    @property
    def parameters_schema(self):
        return {schema!r}

    async def execute(self, **kwargs):
        return "待 Sandbox Eval 验证"
'''


@pytest.mark.asyncio
async def test_capability_artifact_seals_real_source_without_registration(
    tmp_path: Path,
) -> None:
    proposal, specification_service, _, harness, _, specification_view = (
        await _complete_specification(tmp_path)
    )
    governance, _, _ = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[{"kind": "option", "value": "approve"}],
    )
    approved = await governance.decide(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    specification = specification_view.specification
    assert specification is not None and specification.interface is not None
    source_path = tmp_path / "sandbox_tools" / "browser_trace_compare.py"
    source_path.parent.mkdir()
    source_path.write_text(
        _implementation_source(
            specification.interface.tool_name,
            specification.interface.parameters_schema,
        ),
        encoding="utf-8",
    )

    artifact = build_capability_implementation_artifact(
        tmp_path,
        specification=specification,
        governance=approved,
        source_path="sandbox_tools/browser_trace_compare.py",
        class_name="BrowserTraceCompareTool",
        registered_tool_names=("bash_run", "default.web_search"),
        created_at=NOW.isoformat(),
    )

    assert artifact.admission_ready is True
    assert artifact.registry_state == "preview_only"
    assert artifact.registration_authorized is False
    assert artifact.executable is False
    assert artifact.source_text == source_path.read_text(encoding="utf-8")
    assert artifact.temporary_tool_name.startswith("evolution_sandbox:")
    assert "没有 import、注册或执行" in render_capability_artifact(
        SimpleNamespace(
            artifact=artifact,
            state="preview_ready",
            source_current=True,
            governance_current=True,
        )
    )


@pytest.mark.asyncio
async def test_capability_artifact_blocks_builtin_alias_and_import_time_effect(
    tmp_path: Path,
) -> None:
    proposal, specification_service, _, harness, _, specification_view = (
        await _complete_specification(tmp_path)
    )
    governance, _, _ = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[{"kind": "option", "value": "approve"}],
    )
    approved = await governance.decide(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    specification = specification_view.specification
    assert specification is not None and specification.interface is not None
    source = _implementation_source(
        specification.interface.tool_name,
        specification.interface.parameters_schema,
    ) + "\nprint('must not run')\n"
    path = tmp_path / "candidate.py"
    path.write_text(source, encoding="utf-8")

    artifact = build_capability_implementation_artifact(
        tmp_path,
        specification=specification,
        governance=approved,
        source_path="candidate.py",
        class_name="BrowserTraceCompareTool",
        registered_tool_names=(f"default.{specification.interface.tool_name}",),
        created_at=NOW.isoformat(),
    )

    assert artifact.admission_ready is False
    failed = {item.code for item in artifact.checks if not item.passed}
    assert failed == {"import_time_safety", "builtin_conflict"}


@pytest.mark.asyncio
async def test_capability_artifact_rejects_workspace_escape_and_revoked_governance(
    tmp_path: Path,
) -> None:
    proposal, specification_service, _, harness, _, specification_view = (
        await _complete_specification(tmp_path)
    )
    governance, _, _ = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[{"kind": "option", "value": "approve"}],
    )
    approved = await governance.decide(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    specification = specification_view.specification
    assert specification is not None

    for source_path, message in (
        (str(tmp_path / "candidate.py"), "绝对路径"),
        (r"C:\outside\candidate.py", "绝对路径"),
        ("../candidate.py", "父目录跳转"),
    ):
        with pytest.raises(CapabilityArtifactError, match=message):
            build_capability_implementation_artifact(
                tmp_path,
                specification=specification,
                governance=approved,
                source_path=source_path,
                class_name="BrowserTraceCompareTool",
                registered_tool_names=(),
                created_at=NOW.isoformat(),
            )

    target = tmp_path / "target.py"
    target.write_text("# target\n", encoding="utf-8")
    (tmp_path / "linked.py").symlink_to(target)
    with pytest.raises(CapabilityArtifactError, match="符号链接"):
        build_capability_implementation_artifact(
            tmp_path,
            specification=specification,
            governance=approved,
            source_path="linked.py",
            class_name="BrowserTraceCompareTool",
            registered_tool_names=(),
            created_at=NOW.isoformat(),
        )

    revoked = approved.model_copy(
        update={
            "state": "revoked",
            "decision_effective": False,
            "sandbox_design_eligible": False,
        }
    )
    with pytest.raises(CapabilityArtifactError, match="治理 Decision"):
        build_capability_implementation_artifact(
            tmp_path,
            specification=specification,
            governance=revoked,
            source_path="candidate.py",
            class_name="BrowserTraceCompareTool",
            registered_tool_names=(),
            created_at=NOW.isoformat(),
        )


@pytest.mark.asyncio
async def test_capability_artifact_store_is_idempotent_and_detects_tampering(
    tmp_path: Path,
) -> None:
    proposal, specification_service, _, harness, _, specification_view = (
        await _complete_specification(tmp_path)
    )
    governance, _, _ = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[{"kind": "option", "value": "approve"}],
    )
    approved = await governance.decide(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    specification = specification_view.specification
    assert specification is not None and specification.interface is not None
    path = tmp_path / "candidate.py"
    path.write_text(
        _implementation_source(
            specification.interface.tool_name,
            specification.interface.parameters_schema,
        ),
        encoding="utf-8",
    )
    artifact = build_capability_implementation_artifact(
        tmp_path,
        specification=specification,
        governance=approved,
        source_path="candidate.py",
        class_name="BrowserTraceCompareTool",
        registered_tool_names=(),
        created_at=NOW.isoformat(),
    )
    store = EvolutionCapabilityArtifactStore(tmp_path / "evolution.db")
    competing_store = EvolutionCapabilityArtifactStore(tmp_path / "evolution.db")
    first, second = await asyncio.gather(
        store.record(tmp_path, artifact),
        competing_store.record(tmp_path, artifact),
    )
    assert first.artifact_id == second.artifact_id == artifact.artifact_id

    with sqlite3.connect(tmp_path / "evolution.db") as db:
        db.execute(
            "UPDATE evolution_capability_implementation_artifacts "
            "SET payload_sha256 = ? WHERE artifact_id = ?",
            ("0" * 64, artifact.artifact_id),
        )
        db.commit()
    with pytest.raises(CapabilityArtifactError, match="持久摘要"):
        await store.latest(tmp_path, specification.specification_id)


@pytest.mark.asyncio
async def test_capability_artifact_service_revokes_changed_source(
    tmp_path: Path,
) -> None:
    proposal, specification_service, _, harness, _, specification_view = (
        await _complete_specification(tmp_path)
    )
    governance, _, _ = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[{"kind": "option", "value": "approve"}],
    )
    await governance.decide(tmp_path, candidate_id=proposal.source.candidate_id)
    specification = specification_view.specification
    assert specification is not None and specification.interface is not None
    path = tmp_path / "candidate.py"
    path.write_text(
        _implementation_source(
            specification.interface.tool_name,
            specification.interface.parameters_schema,
        ),
        encoding="utf-8",
    )
    service = EvolutionCapabilityArtifactService(
        review_service=_Review(proposal),
        specification_service=specification_service,
        governance_service=governance,
        store=EvolutionCapabilityArtifactStore(tmp_path / "evolution.db"),
        registered_tool_names=lambda: ("bash_run",),
        now=lambda: NOW.isoformat(),
    )
    created = await service.create(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
        source_path="candidate.py",
        class_name="BrowserTraceCompareTool",
    )
    assert created.state == "preview_ready"
    engine = SimpleNamespace(
        workspace_root=tmp_path,
        evolution_capability_artifact_service=service,
        evolution_review_service=service.review_service,
    )
    tool_output = await EvolutionCapabilityArtifactTool(engine).execute(
        candidate_id=proposal.source.candidate_id,
        action="inspect",
    )
    slash_output = await execute_slash_command(
        engine,
        f"/evolution capability-artifact {proposal.source.candidate_id}",
    )
    assert created.artifact is not None
    assert created.artifact.artifact_id in tool_output
    assert created.artifact.artifact_id in slash_output

    path.write_text(path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    revoked = await service.inspect(tmp_path, proposal.source.candidate_id)
    assert revoked.state == "revoked"
    assert revoked.source_current is False
    assert revoked.governance_current is True
    assert revoked.registration_authorized is False


def _scenario_binding_answer() -> dict[str, str]:
    return {
        "kind": "custom",
        "custom_text": json.dumps({
            "scenarios": [{
                "name": "比较两份真实轨迹",
                "arguments": {"left": "data/traces/a.json"},
                "expectation": {
                    "kind": "result",
                    "value": {"differences": []},
                },
                "timeout_ms": 1500,
            }],
        }, ensure_ascii=False),
    }


async def _approved_artifact(tmp_path: Path):
    proposal, specification_service, _, harness, _, specification_view = (
        await _complete_specification(tmp_path)
    )
    governance, _, _ = _governance_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        harness_store=harness,
        responses=[{"kind": "option", "value": "approve"}],
    )
    await governance.decide(tmp_path, candidate_id=proposal.source.candidate_id)
    specification = specification_view.specification
    assert specification is not None and specification.interface is not None
    path = tmp_path / "candidate.py"
    path.write_text(
        _implementation_source(
            specification.interface.tool_name,
            specification.interface.parameters_schema,
        ),
        encoding="utf-8",
    )
    artifact_service = EvolutionCapabilityArtifactService(
        review_service=_Review(proposal),
        specification_service=specification_service,
        governance_service=governance,
        store=EvolutionCapabilityArtifactStore(tmp_path / "evolution.db"),
        registered_tool_names=lambda: ("bash_run",),
        now=lambda: NOW.isoformat(),
    )
    artifact_view = await artifact_service.create(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
        source_path="candidate.py",
        class_name="BrowserTraceCompareTool",
    )
    assert artifact_view.artifact is not None
    return (
        proposal,
        specification_service,
        harness,
        specification,
        artifact_service,
        artifact_view.artifact,
        path,
    )


def _scenario_binding_service(
    *,
    tmp_path: Path,
    proposal,
    specification_service,
    artifact_service,
    harness,
    responses: list[dict[str, str]],
    fail_after_commit: bool = False,
):
    clock = _Clock()
    host = _InteractionHost(
        store=harness,
        workspace=tmp_path,
        responses=responses,
        clock=clock,
        fail_after_commit=fail_after_commit,
    )
    store = EvolutionCapabilityScenarioBindingStore(tmp_path / "evolution.db")
    service = EvolutionCapabilityScenarioBindingService(
        review_service=_Review(proposal),
        specification_service=specification_service,
        artifact_service=artifact_service,
        store=store,
        interaction_store=harness,
        request_user_input=host,
    )
    return service, store, host


@pytest.mark.asyncio
async def test_scenario_binding_uses_real_harness_answer_and_json_schema(
    tmp_path: Path,
) -> None:
    (
        proposal,
        specification_service,
        harness,
        specification,
        artifact_service,
        artifact,
        _,
    ) = await _approved_artifact(tmp_path)
    service, store, host = _scenario_binding_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        artifact_service=artifact_service,
        harness=harness,
        responses=[_scenario_binding_answer()],
    )

    view = await service.advance(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )

    assert view.state == "ready"
    assert view.sandbox_execution_eligible is True
    assert view.sandbox_execution_authorized is False
    assert view.registration_authorized is False
    assert view.executable is False
    assert view.binding is not None
    assert view.binding.specification_sha256 == specification.digest()
    assert view.binding.artifact_id == artifact.artifact_id
    assert view.binding.source_interaction_id.startswith("ask-evcsbind-")
    assert view.binding.scenarios[0].arguments["left"] == "data/traces/a.json"
    assert await store.get(tmp_path, artifact.artifact_id) == view.binding
    assert len(host.requests) == 1
    rendered = render_capability_scenario_binding(view)
    assert "Sandbox 执行资格：是" in rendered
    assert "Sandbox 执行授权：否" in rendered
    engine = SimpleNamespace(
        workspace_root=tmp_path,
        evolution_capability_scenario_binding_service=service,
        evolution_review_service=service.review_service,
    )
    tool_output = await EvolutionCapabilityScenarioBindingTool(engine).execute(
        candidate_id=proposal.source.candidate_id,
        action="inspect",
    )
    slash_output = await execute_slash_command(
        engine,
        f"/evolution capability-bind {proposal.source.candidate_id}",
    )
    assert view.binding.binding_id in tool_output
    assert view.binding.binding_id in slash_output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["scenarios"][0].update(arguments={}),
            "arguments 不满足 JSON Schema",
        ),
        (
            lambda payload: payload["scenarios"][0]["expectation"].update(
                value={"wrong": []}
            ),
            "expectation.value 不满足 JSON Schema",
        ),
        (
            lambda payload: payload["scenarios"][0].update(
                expectation={"kind": "error", "error_code": "unknown"}
            ),
            "error_code 未在接口错误契约声明",
        ),
    ],
)
async def test_scenario_binding_rejects_invalid_machine_oracle(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    (
        proposal,
        specification_service,
        harness,
        _,
        artifact_service,
        _,
        _,
    ) = await _approved_artifact(tmp_path)
    answer = json.loads(_scenario_binding_answer()["custom_text"])
    mutate(answer)
    service, _, _ = _scenario_binding_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        artifact_service=artifact_service,
        harness=harness,
        responses=[{
            "kind": "custom",
            "custom_text": json.dumps(answer, ensure_ascii=False),
        }],
    )

    with pytest.raises(CapabilityScenarioBindingError, match=message):
        await service.advance(tmp_path, candidate_id=proposal.source.candidate_id)


@pytest.mark.asyncio
async def test_scenario_binding_retries_after_invalid_durable_answer(
    tmp_path: Path,
) -> None:
    (
        proposal,
        specification_service,
        harness,
        _,
        artifact_service,
        _,
        _,
    ) = await _approved_artifact(tmp_path)
    invalid, _, _ = _scenario_binding_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        artifact_service=artifact_service,
        harness=harness,
        responses=[{"kind": "custom", "custom_text": "not-json"}],
    )
    with pytest.raises(CapabilityScenarioBindingError, match="有效 JSON"):
        await invalid.advance(tmp_path, candidate_id=proposal.source.candidate_id)

    retrying, _, host = _scenario_binding_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        artifact_service=artifact_service,
        harness=harness,
        responses=[_scenario_binding_answer()],
    )
    recovered = await retrying.advance(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )

    assert recovered.state == "ready"
    assert recovered.binding is not None
    assert recovered.binding.source_interaction_id.endswith("-2")
    assert len(host.requests) == 1


@pytest.mark.asyncio
async def test_scenario_binding_recovers_answer_and_revokes_with_artifact(
    tmp_path: Path,
) -> None:
    (
        proposal,
        specification_service,
        harness,
        _,
        artifact_service,
        artifact,
        source_path,
    ) = await _approved_artifact(tmp_path)
    crashing, store, _ = _scenario_binding_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        artifact_service=artifact_service,
        harness=harness,
        responses=[_scenario_binding_answer()],
        fail_after_commit=True,
    )
    with pytest.raises(RuntimeError, match="simulated host crash"):
        await crashing.advance(tmp_path, candidate_id=proposal.source.candidate_id)
    recovering = EvolutionCapabilityScenarioBindingService(
        review_service=_Review(proposal),
        specification_service=specification_service,
        artifact_service=artifact_service,
        store=store,
        interaction_store=harness,
        request_user_input=lambda _payload: pytest.fail("不应创建第二条交互"),
    )
    recovered = await recovering.advance(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    assert recovered.state == "ready"
    assert recovered.binding is not None

    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "\n# drift\n",
        encoding="utf-8",
    )
    revoked = await recovering.inspect(tmp_path, proposal.source.candidate_id)
    assert revoked.state == "revoked"
    assert revoked.artifact_current is False
    assert revoked.sandbox_execution_eligible is False
    assert revoked.binding.artifact_id == artifact.artifact_id  # type: ignore[union-attr]

    recovering.interaction_store = SimpleNamespace(
        get_interaction=lambda **_kwargs: asyncio.sleep(0, result=None),
    )
    interaction_revoked = await recovering.inspect(
        tmp_path,
        proposal.source.candidate_id,
    )
    assert interaction_revoked.state == "revoked"
    assert interaction_revoked.binding_current is False
    assert interaction_revoked.sandbox_execution_eligible is False


@pytest.mark.asyncio
async def test_scenario_binding_store_first_wins_and_detects_tampering(
    tmp_path: Path,
) -> None:
    (
        proposal,
        specification_service,
        harness,
        specification,
        artifact_service,
        artifact,
        _,
    ) = await _approved_artifact(tmp_path)
    service, store, _ = _scenario_binding_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        artifact_service=artifact_service,
        harness=harness,
        responses=[_scenario_binding_answer()],
    )
    view = await service.advance(tmp_path, candidate_id=proposal.source.candidate_id)
    assert view.binding is not None
    interaction = await harness.get_interaction(
        workspace_root=tmp_path,
        interaction_id=view.binding.source_interaction_id,
    )
    assert interaction is not None
    competing = build_capability_scenario_binding(
        specification=specification,
        artifact=artifact,
        interaction=interaction.model_copy(update={"interaction_id": (
            f"ask-evcsbind-{artifact.artifact_id[6:]}-2"
        )}),
    )
    with pytest.raises(CapabilityScenarioBindingError, match="拒绝覆盖"):
        await EvolutionCapabilityScenarioBindingStore(
            tmp_path / "evolution.db"
        ).record(tmp_path, competing)

    with sqlite3.connect(tmp_path / "evolution.db") as db:
        db.execute(
            "UPDATE evolution_capability_scenario_bindings SET payload_sha256 = ? "
            "WHERE artifact_id = ?",
            ("0" * 64, artifact.artifact_id),
        )
        db.commit()
    with pytest.raises(CapabilityScenarioBindingError, match="持久摘要"):
        await store.get(tmp_path, artifact.artifact_id)


def _successful_implementation_source(
    tool_name: str,
    schema: dict[str, object],
) -> str:
    return f'''import json
from pathlib import Path

from naumi_agent.tools.base import Tool


class BrowserTraceCompareTool(Tool):
    @property
    def name(self):
        return {tool_name!r}

    @property
    def description(self):
        return "比较两份真实浏览器轨迹。"

    @property
    def parameters_schema(self):
        return {schema!r}

    async def execute(self, **kwargs):
        Path(str(kwargs["left"])).read_text(encoding="utf-8")
        return json.dumps({{"differences": []}}, ensure_ascii=False)
'''


async def _ready_sandbox_request_fixture(tmp_path: Path):
    (
        proposal,
        specification_service,
        harness,
        specification,
        artifact_service,
        _,
        source_path,
    ) = await _approved_artifact(tmp_path)
    assert specification.interface is not None
    trace = tmp_path / "data" / "traces" / "a.json"
    trace.parent.mkdir(parents=True)
    trace.write_text('{"events": []}\n', encoding="utf-8")
    source_path.write_text(
        _successful_implementation_source(
            specification.interface.tool_name,
            specification.interface.parameters_schema,
        ),
        encoding="utf-8",
    )
    artifact_view = await artifact_service.create(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
        source_path="candidate.py",
        class_name="BrowserTraceCompareTool",
    )
    assert artifact_view.artifact is not None
    binding_service, _, _ = _scenario_binding_service(
        tmp_path=tmp_path,
        proposal=proposal,
        specification_service=specification_service,
        artifact_service=artifact_service,
        harness=harness,
        responses=[_scenario_binding_answer()],
    )
    binding_view = await binding_service.advance(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    assert binding_view.state == "ready" and binding_view.binding is not None
    return (
        proposal,
        specification_service,
        specification,
        artifact_service,
        artifact_view.artifact,
        binding_service,
        binding_view,
    )


def _commit_sandbox_request_fixture(workspace: Path) -> tuple[str, str]:
    (workspace / ".gitignore").write_text(
        "evolution.db\nharness.db\n.naumi/\n__pycache__/\n",
        encoding="utf-8",
    )
    commands = (
        ("init",),
        ("config", "user.email", "sandbox-request@example.invalid"),
        ("config", "user.name", "Sandbox Request Test"),
        ("add", "."),
        ("commit", "-m", "fixture"),
    )
    for args in commands:
        subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
        )
    _, revision, tree_sha256 = capture_clean_revision(workspace)
    return revision, tree_sha256


@pytest.mark.asyncio
async def test_sandbox_execution_request_seals_real_driver_and_scenario(
    tmp_path: Path,
) -> None:
    (
        _,
        _,
        specification,
        _,
        artifact,
        _,
        binding_view,
    ) = await _ready_sandbox_request_fixture(tmp_path)
    revision, tree_sha256 = _commit_sandbox_request_fixture(tmp_path)

    request = build_capability_sandbox_execution_request(
        specification=specification,
        binding_view=binding_view,
        artifact=artifact,
        source_revision=revision,
        source_tree_sha256=tree_sha256,
        python_executable=str(Path(sys.executable).resolve(strict=True)),
        created_at=NOW.isoformat(),
    )

    assert request.request_ready is True
    assert request.sandbox_execution_authorized is False
    assert request.registration_authorized is False
    assert request.source_revision == revision
    assert [item.kind for item in request.overlays] == [
        "candidate",
        "driver",
        "permission_manifest",
        "scenario_input",
    ]
    assert request.checks[0].timeout_ms == 1500
    assert request.checks[0].timeout_seconds == 2
    assert request.permissions[0].family == "workspace_read"
    assert request.permissions[0].scopes == ("data/traces/**",)

    for overlay in request.overlays:
        target = tmp_path / overlay.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(overlay.content_utf8, encoding="utf-8")
    completed = subprocess.run(
        list(request.checks[0].argv),
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(completed.stdout) == {
        "kind": "result",
        "value": {"differences": []},
        "permission_observation": {
            "complete": True,
            "events": [{
                "decision": "allow",
                "family": "workspace_read",
                "scope": "data/traces/a.json",
            }],
            "violations": [],
        },
    }

    candidate = next(item for item in request.overlays if item.kind == "candidate")
    (tmp_path / candidate.path).write_text(
        artifact.source_text.replace(
            'return json.dumps({"differences": []}, ensure_ascii=False)',
            (
                'from naumi_agent.tools.base import ToolExecutionError\n'
                '        raise ToolExecutionError('
                '"trace_missing", "轨迹不存在", retryable=False)'
            ),
        ),
        encoding="utf-8",
    )
    declared_failure = subprocess.run(
        list(request.checks[0].argv),
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(declared_failure.stdout) == {
        "kind": "error",
        "error_code": "trace_missing",
        "retryable": False,
        "permission_observation": {
            "complete": True,
            "events": [{
                "decision": "allow",
                "family": "workspace_read",
                "scope": "data/traces/a.json",
            }],
            "violations": [],
        },
    }

    (tmp_path / candidate.path).write_text(artifact.source_text, encoding="utf-8")
    scenario = next(item for item in request.overlays if item.kind == "scenario_input")
    forbidden_input = json.loads(scenario.content_utf8)
    forbidden_input["arguments"]["left"] = "private.json"
    (tmp_path / scenario.path).write_text(
        json.dumps(forbidden_input),
        encoding="utf-8",
    )
    (tmp_path / "private.json").write_text('{"secret": false}\n', encoding="utf-8")
    denied = subprocess.run(
        list(request.checks[0].argv),
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(denied.stdout) == {
        "kind": "permission_violation",
        "permission_observation": {
            "complete": True,
            "events": [],
            "violations": [{
                "decision": "deny",
                "family": "workspace_read",
                "scope": "private.json",
            }],
        },
    }


@pytest.mark.asyncio
async def test_sandbox_execution_request_service_persists_and_revokes_on_git_drift(
    tmp_path: Path,
) -> None:
    (
        proposal,
        specification_service,
        _,
        _,
        _,
        binding_service,
        _,
    ) = await _ready_sandbox_request_fixture(tmp_path)
    _commit_sandbox_request_fixture(tmp_path)
    store = EvolutionCapabilitySandboxRequestStore(tmp_path / "evolution.db")
    service = EvolutionCapabilitySandboxRequestService(
        binding_service=binding_service,
        specification_store=specification_service.store,
        store=store,
        now=lambda: NOW.isoformat(),
    )

    prepared = await service.prepare(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    repeated = await service.prepare(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    assert prepared.state == repeated.state == "ready"
    assert prepared.request == repeated.request
    assert prepared.request is not None
    assert "尚未签发 Run Grant" in render_capability_sandbox_request(prepared)
    engine = SimpleNamespace(
        workspace_root=tmp_path,
        evolution_capability_sandbox_request_service=service,
        evolution_review_service=binding_service.review_service,
    )
    tool_output = await EvolutionCapabilitySandboxRequestTool(engine).execute(
        candidate_id=proposal.source.candidate_id,
        action="inspect",
    )
    slash_output = await execute_slash_command(
        engine,
        f"/evolution capability-sandbox {proposal.source.candidate_id}",
    )
    assert prepared.request.request_id in tool_output
    assert prepared.request.request_id in slash_output

    (tmp_path / "README.md").write_text("drift\n", encoding="utf-8")
    revoked = await service.inspect(tmp_path, proposal.source.candidate_id)
    assert revoked.state == "revoked"
    assert revoked.binding_current is True
    assert revoked.source_current is False
    assert revoked.sandbox_execution_authorized is False


@pytest.mark.asyncio
async def test_sandbox_execution_request_store_detects_payload_tampering(
    tmp_path: Path,
) -> None:
    (
        proposal,
        specification_service,
        _,
        _,
        _,
        binding_service,
        binding_view,
    ) = await _ready_sandbox_request_fixture(tmp_path)
    _commit_sandbox_request_fixture(tmp_path)
    store = EvolutionCapabilitySandboxRequestStore(tmp_path / "evolution.db")
    service = EvolutionCapabilitySandboxRequestService(
        binding_service=binding_service,
        specification_store=specification_service.store,
        store=store,
        now=lambda: NOW.isoformat(),
    )
    prepared = await service.prepare(
        tmp_path,
        candidate_id=proposal.source.candidate_id,
    )
    assert prepared.request is not None and binding_view.binding is not None

    with sqlite3.connect(tmp_path / "evolution.db") as db:
        db.execute(
            "UPDATE evolution_capability_sandbox_requests SET payload_sha256 = ? "
            "WHERE request_id = ?",
            ("0" * 64, prepared.request.request_id),
        )
        db.commit()
    with pytest.raises(CapabilitySandboxRequestError, match="持久摘要"):
        await store.latest(tmp_path, binding_view.binding.binding_id)


@pytest.mark.asyncio
async def test_sandbox_execution_request_store_is_first_wins_under_race(
    tmp_path: Path,
) -> None:
    (
        _,
        _,
        specification,
        _,
        artifact,
        _,
        binding_view,
    ) = await _ready_sandbox_request_fixture(tmp_path)
    revision, tree_sha256 = _commit_sandbox_request_fixture(tmp_path)
    assert binding_view.binding is not None
    request_a = build_capability_sandbox_execution_request(
        specification=specification,
        binding_view=binding_view,
        artifact=artifact,
        source_revision=revision,
        source_tree_sha256=tree_sha256,
        python_executable=str(Path(sys.executable).resolve(strict=True)),
        created_at=NOW.isoformat(),
    )
    request_b = build_capability_sandbox_execution_request(
        specification=specification,
        binding_view=binding_view,
        artifact=artifact,
        source_revision=revision,
        source_tree_sha256=tree_sha256,
        python_executable=str(Path(sys.executable).resolve(strict=True)),
        created_at=(NOW + timedelta(seconds=1)).isoformat(),
    )
    assert request_a.request_id != request_b.request_id
    store_a = EvolutionCapabilitySandboxRequestStore(tmp_path / "evolution.db")
    store_b = EvolutionCapabilitySandboxRequestStore(tmp_path / "evolution.db")

    stored_a, stored_b = await asyncio.gather(
        store_a.record(tmp_path, request_a),
        store_b.record(tmp_path, request_b),
    )

    assert stored_a.request_id == stored_b.request_id
    with sqlite3.connect(tmp_path / "evolution.db") as db:
        count = db.execute(
            "SELECT COUNT(*) FROM evolution_capability_sandbox_requests "
            "WHERE workspace_root = ? AND binding_id = ? AND source_revision = ?",
            (str(tmp_path.resolve()), binding_view.binding.binding_id, revision),
        ).fetchone()
    assert count == (1,)
