from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.evolution.candidate import build_candidate_draft
from naumi_agent.evolution.capability_proposal import generate_capability_proposal
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
from naumi_agent.harness.store import HarnessStore
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

    async def detail_snapshot(self, _workspace: Path, _candidate_id: str):
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
