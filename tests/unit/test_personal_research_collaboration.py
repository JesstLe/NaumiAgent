"""Semantic review bindings reuse the real collaboration/AgentJob journals.

Execution bodies and the domain authority port are controlled here. Exact
Artifact storage, Session storage and ChatRun storage remain real where used.
Provider/model acceptance belongs to the separate integrated research suite.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from naumi_agent.daemons.agent_jobs import AgentJobState
from naumi_agent.personal.collaboration_store import (
    CollaborationStore,
    normalize_research_execution,
)
from naumi_agent.personal.models import PersonalAgentError
from tests.unit.test_personal_artifacts import _publish, _share
from tests.unit.test_personal_artifacts import artifacts as artifacts
from tests.unit.test_personal_collaboration import ControlledChild, finish
from tests.unit.test_personal_collaboration import setup as _collaboration_setup

collaboration_setup = _collaboration_setup

pytestmark = pytest.mark.usefixtures("runtime_payload_key")
AGENT = "pa_" + "a" * 32
OTHER = "pa_" + "b" * 32
PACKET = {
    "artifact_id": "art_" + "c" * 32,
    "version": 1,
    "sha256": "d" * 64,
    "grant_id": "asg_" + "e" * 32,
    "grant_revision": 1,
}


def binding(**updates):
    return {
        "review_id": "rsv_" + "f" * 32,
        "review_digest": "1" * 64,
        "packet_ref": copy.deepcopy(PACKET),
        "reviewer_id": AGENT,
        "reviewer_version": 1,
        **updates,
    }


def step(**updates):
    return {
        "id": "review",
        "agent_id": AGENT,
        "agent_version": 1,
        "task": "按确切复核资料包逐项提交结构化结论。",
        "depends_on": [],
        "tool_names": ["personal_research"],
        **updates,
    }


def stored(store, **updates):
    return store.create(
        **{
            "workspace_key": "workspace",
            "source_session_id": "source-session",
            "source_agent_id": "",
            "request_id": "semantic-review",
            "title": "独立复核",
            "context": "明确的确切版本资料包，不包含日常会话。",
            "steps": [step()],
            "research_execution": binding(),
            **updates,
        }
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        [],
        "review",
        binding(extra=True),
        {key: value for key, value in binding().items() if key != "review_digest"},
        binding(review_id="rsv_" + "A" * 32),
        binding(review_id="rsv_short"),
        binding(review_digest="x" * 64),
        binding(review_digest="1" * 63),
        binding(reviewer_id="main"),
        binding(reviewer_version=True),
        binding(reviewer_version=0),
        binding(reviewer_version=1.5),
        binding(packet_ref={**PACKET, "version": True}),
        binding(packet_ref={**PACKET, "sha256": "latest"}),
        binding(packet_ref={**PACKET, "grant_revision": 0}),
        binding(packet_ref={**PACKET, "extra": "untrusted"}),
    ],
)
def test_review_execution_binding_is_strict(invalid):
    with pytest.raises(PersonalAgentError) as caught:
        normalize_research_execution(invalid)
    assert caught.value.code == "invalid_input"


def test_review_execution_binding_is_detached():
    original = binding()
    normalized = normalize_research_execution(original)
    assert normalized == original and normalized is not original
    original["packet_ref"]["version"] = 9
    assert normalized["packet_ref"]["version"] == 1
    assert normalize_research_execution(None) is None


@pytest.mark.parametrize(
    "steps",
    [
        [step(), step(id="second")],
        [step(agent_id=OTHER)],
        [step(agent_version=2)],
        [step(tool_names=["personal_research", "file_read"])],
        [step(tool_names=[])],
        [{key: value for key, value in step().items() if key != "tool_names"}],
        [step(depends_on=["upstream"]), step(id="upstream")],
        [
            step(
                message={
                    "recipient_agent_id": OTHER,
                    "recipient_agent_version": 1,
                    "request_id": "send",
                    "content": "额外派送",
                    "context": "",
                }
            )
        ],
    ],
)
def test_review_plan_has_one_exact_reviewer_and_no_other_execution(tmp_path, steps):
    store = CollaborationStore(tmp_path)
    with pytest.raises(PersonalAgentError) as caught:
        stored(store, steps=steps)
    assert caught.value.code == "invalid_input"
    assert store.list("workspace") == []


@pytest.mark.parametrize(
    "extra",
    [
        {"peer_agents": [{"agent_id": OTHER, "agent_version": 1}]},
        {
            "team_execution": {
                "turn_id": "pt_" + "2" * 32,
                "phase": "members",
                "team_id": OTHER,
                "team_version": 1,
                "max_budget_usd": None,
                "max_input_tokens": None,
                "max_output_tokens": None,
                "max_turns": 3,
            }
        },
    ],
)
def test_review_plan_cannot_mix_peer_or_team_execution(tmp_path, extra):
    store = CollaborationStore(tmp_path)
    with pytest.raises(PersonalAgentError) as caught:
        stored(store, **extra)
    assert caught.value.code == "invalid_input"
    assert store.list("workspace") == []


def test_binding_is_in_immutable_plan_and_original_request_fingerprint(tmp_path):
    store = CollaborationStore(tmp_path)
    original = stored(store)
    assert original["research_execution"] == binding()
    assert stored(CollaborationStore(tmp_path)) == original
    with pytest.raises(PersonalAgentError) as caught:
        stored(store, research_execution=binding(review_digest="3" * 64))
    assert caught.value.code == "idempotency_conflict"
    assert store.get(original["id"], "workspace") == original
    assert len(store.list("workspace")) == 1


def schema_version(store):
    with sqlite3.connect(store.db_path) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def test_only_new_research_plans_require_schema_two_and_old_readers_fail_closed(
    tmp_path, monkeypatch
):
    store = CollaborationStore(tmp_path)
    ordinary_steps = [{key: item for key, item in step().items() if key != "tool_names"}]
    ordinary = stored(store, request_id="ordinary", steps=ordinary_steps, research_execution=None)
    assert schema_version(store) == 1
    assert CollaborationStore(tmp_path).get(ordinary["id"], "workspace") == ordinary
    assert schema_version(store) == 1
    research = stored(store)
    assert schema_version(store) == 2
    reopened = CollaborationStore(tmp_path)
    assert reopened.get(research["id"], "workspace") == research
    assert reopened.get(ordinary["id"], "workspace") == ordinary
    assert schema_version(store) == 2
    monkeypatch.setattr("naumi_agent.personal.collaboration_store._SCHEMA_VERSION", 1)
    with pytest.raises(PersonalAgentError) as caught:
        CollaborationStore(tmp_path).get(research["id"], "workspace")
    assert caught.value.code == "unsupported_schema" and schema_version(store) == 2


def test_existing_v1_research_requires_explicit_metadata_promotion_without_business_writes(
    tmp_path,
):
    store = CollaborationStore(tmp_path)
    original = stored(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("PRAGMA user_version=1")
    reopened = CollaborationStore(tmp_path)
    assert reopened.get(original["id"], "workspace") == original
    assert schema_version(store) == 1, "A cold GET must not promote compatibility metadata"
    promoted = reopened.promote_research_schema()
    assert promoted["before_version"] == 1 and promoted["after_version"] == 2
    assert promoted["changed"] and promoted["research_plans"] == 1
    assert promoted["business_sha256_before"] == promoted["business_sha256_after"]
    assert reopened.get(original["id"], "workspace") == original
    replay = CollaborationStore(tmp_path).promote_research_schema()
    assert not replay["changed"] and replay["before_version"] == replay["after_version"] == 2
    assert replay["business_sha256_before"] == promoted["business_sha256_before"]


def test_legacy_only_schema_promotion_is_noop_and_missing_store_is_not_created(tmp_path):
    store = CollaborationStore(tmp_path)
    with pytest.raises(PersonalAgentError) as caught:
        store.promote_research_schema()
    assert caught.value.code == "not_found" and not store.db_path.exists()
    ordinary_steps = [{key: item for key, item in step().items() if key != "tool_names"}]
    stored(store, steps=ordinary_steps, research_execution=None)
    result = store.promote_research_schema()
    assert result["research_plans"] == 0 and not result["changed"]
    assert result["before_version"] == result["after_version"] == schema_version(store) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda plan: plan["research_execution"].update(reviewer_version=2),
        lambda plan: plan["research_execution"].update(extra="forged"),
        lambda plan: plan["steps"][0].update(tool_names=["file_read"]),
        lambda plan: plan.update(peer_agents=[{"agent_id": OTHER, "agent_version": 1}]),
    ],
)
def test_corrupt_reopened_review_plan_fails_closed(tmp_path, change):
    store = CollaborationStore(tmp_path)
    original = stored(store)
    with sqlite3.connect(store.db_path) as db:
        raw = json.loads(db.execute("SELECT plan_json FROM collaborations").fetchone()[0])
        change(raw)
        db.execute("UPDATE collaborations SET plan_json=?", (json.dumps(raw),))
    with pytest.raises(PersonalAgentError) as caught:
        store.get(original["id"], "workspace")
    assert caught.value.code == "storage_error"


class Authority:
    """Controlled domain port; collaboration must consult it at every fence."""

    def __init__(self, expected):
        self.expected = expected
        self.validation_calls = []
        self.authorization_calls = []
        self.bindings = []
        self.revoked = False

    async def validate(self, supplied, **inputs):
        self.validation_calls.append((copy.deepcopy(supplied), copy.deepcopy(inputs)))
        if supplied != self.expected or "FORGED" in inputs["context"]:
            raise PersonalAgentError("review_binding_conflict", "复核原始凭据不一致")

    async def authorize(self, plan):
        self.authorization_calls.append(plan["id"])
        if self.revoked:
            raise PersonalAgentError("artifact_access_revoked", "复核原始资料授权已撤销")

    async def bind(self, child, plan, step):
        self.bindings.append((child, plan["id"], step["task_id"]))
        child._unit_semantic_binding = plan["research_execution"]


class ArtifactPort:
    """Record calls to the integration boundary, not an Artifact acceptance oracle."""

    enabled = True

    def __init__(self):
        self.binds = []
        self.propagations = []

    async def bind_refs(self, session_id, actor_id, refs):
        self.binds.append((session_id, actor_id, copy.deepcopy(refs)))

    async def propagate(self, source_id, recipient_id, actor_id):
        self.propagations.append((source_id, recipient_id, actor_id))


class ReviewChild(ControlledChild):
    """A controlled turn with an actual ChatRun on the original execution Session."""

    def __init__(self, config, step, records, *, chat_runs, artifacts, gate=None):
        super().__init__(config, step, records, gate=gate)
        self.config = config.model_copy(deep=True)
        self.config.memory.long_term_enabled = True
        self._personal_memory_parent_enabled = True
        self.chat_run_store = chat_runs
        if artifacts is not None:
            self.personal_artifacts = artifacts

    async def run_streaming(self, task, callback, turn_context=""):
        await self._personal_execution_authorizer()
        run = await self.chat_run_store.start_run(
            session_id=self.session.id, user_message_id="controlled-review-message"
        )
        try:

            async def observe(event, payload):
                await callback(event, {**payload, "name": "personal_research"})

            result = await super().run_streaming(task, observe, turn_context=turn_context)
        except asyncio.CancelledError:
            await self.chat_run_store.finish_run(run.id, status="cancelled")
            raise
        else:
            await self.chat_run_store.finish_run(run.id, status=result.status)
            return result


def install_authority(monkeypatch, engine, expected):
    authority = Authority(expected)
    for name, operation in (
        ("validate_semantic_execution", authority.validate),
        ("authorize_semantic_execution", authority.authorize),
        ("bind_semantic_execution", authority.bind),
    ):
        monkeypatch.setattr(engine.personal_research, name, operation, raising=False)
    return authority


@pytest.fixture
def review_setup(collaboration_setup, monkeypatch):
    engine, service, profiles, records = collaboration_setup
    reviewer = engine.personal_agents.update(
        profiles[0].id,
        profiles[0].version,
        name="Mira",
        tool_names=["file_read", "personal_research"],
    )
    expected = binding(reviewer_id=reviewer.id, reviewer_version=reviewer.version)
    authority = install_authority(monkeypatch, engine, expected)
    port = ArtifactPort()
    monkeypatch.setattr(engine, "personal_artifacts", port)
    children = []

    def child_factory(step, job):
        child = ReviewChild(
            engine.config, step, records, chat_runs=engine.chat_run_store, artifacts=port
        )
        children.append(child)
        return child

    service._child_factory = child_factory
    request = {
        "source_session_id": engine._session.id,
        "request_id": "semantic-review",
        "title": "独立复核",
        "context": "SERVICE_GENERATED_EXACT_PACKET",
        "steps": [step(agent_id=reviewer.id, agent_version=reviewer.version)],
        "_research_execution": expected,
    }
    return SimpleNamespace(
        engine=engine,
        service=service,
        reviewer=reviewer,
        authority=authority,
        port=port,
        children=children,
        request=request,
        records=records,
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"_manual_dispatch": True},
        {"_team_execution": {}},
        {"peer_agents": [{"agent_id": OTHER, "agent_version": 1}]},
    ],
)
async def test_service_rejects_mixed_review_ownership_before_persistence(review_setup, extra):
    env = review_setup
    with pytest.raises(PersonalAgentError) as caught:
        await env.service.create(**{**env.request, **extra})
    assert caught.value.code == "invalid_input"
    assert env.service.store.list(env.service.workspace_key) == []
    assert env.records == []


async def test_forged_review_binding_is_validated_by_domain_before_persistence(review_setup):
    env = review_setup
    with pytest.raises(PersonalAgentError) as caught:
        await env.service.create(**{**env.request, "context": "FORGED_CONTEXT"})
    assert caught.value.code == "review_binding_conflict"
    assert len(env.authority.validation_calls) == 1
    assert env.service.store.list(env.service.workspace_key) == []


async def test_missing_domain_validator_rejects_before_persistence(review_setup, monkeypatch):
    env = review_setup
    monkeypatch.setattr(env.engine.personal_research, "validate_semantic_execution", None)
    with pytest.raises(PersonalAgentError) as caught:
        await env.service.create(**env.request)
    assert caught.value.code == "configuration_error"
    assert env.service.store.list(env.service.workspace_key) == []


async def test_review_requires_research_in_original_reviewer_profile(review_setup):
    env = review_setup
    reviewer = env.engine.personal_agents.update(
        env.reviewer.id, env.reviewer.version, tool_names=["file_read"]
    )
    expected = binding(reviewer_id=reviewer.id, reviewer_version=reviewer.version)
    env.authority.expected = expected
    request = {
        **env.request,
        "_research_execution": expected,
        "steps": [step(agent_id=reviewer.id, agent_version=reviewer.version)],
    }
    with pytest.raises(PersonalAgentError) as caught:
        await env.service.create(**request)
    assert caught.value.code in {"team_scope_denied", "scope_denied"}
    assert env.service.store.list(env.service.workspace_key) == []


async def test_exact_packet_only_original_agent_job_and_chat_run_are_reused(review_setup):
    env = review_setup
    source_id = env.engine._session.id
    source_messages = list(env.engine._session.messages)
    first = await env.service.create(**env.request)
    replay = await env.service.create(**env.request)
    assert first["id"] == replay["id"]
    assert first["steps"][0]["session_id"] == replay["steps"][0]["session_id"]
    result = await finish(env.service, first["id"])
    assert result["status"] == "completed" and len(env.records) == 1
    child = env.records[0]["child"]
    execution = result["steps"][0]
    assert env.port.binds == [(execution["session_id"], env.reviewer.id, [PACKET])]
    assert env.port.propagations == []
    assert child._personal_effective_tools == frozenset({"personal_research"})
    assert child._personal_memory_parent_enabled is False
    assert child.config.memory.long_term_enabled is False
    assert child._unit_semantic_binding == env.request["_research_execution"]
    context = json.loads(env.records[0]["context"].split("\n", 1)[1])
    assert context["research_execution"] == env.request["_research_execution"]
    assert "MAIN_PRIVATE_DO_NOT_SHARE" not in env.records[0]["context"]
    assert len(env.authority.authorization_calls) >= 4
    assert env.authority.bindings == [(child, first["id"], execution["task_id"])]
    saved = await env.engine.session_store.load(execution["session_id"])
    assert saved.personal_agent_id == env.reviewer.id
    assert saved.personal_execution_id == execution["task_id"] and len(saved.messages) == 2
    job = await env.service.jobs.get(execution["job_id"])
    assert job.state is AgentJobState.COMPLETED
    assert job.request.task_id_sha256 == hashlib.sha256(execution["task_id"].encode()).hexdigest()
    assert job.request.tool_scope == ("personal_research",)
    assert job.result.total_tokens == 10
    runs = await env.engine.chat_run_store.list_runs(execution["session_id"])
    assert len(runs) == 1 and runs[0].status == "completed"
    result_event = next(event for event in result["events"] if event["kind"] == "result")
    assert result_event["data"]["run_id"] == runs[0].id
    assert env.engine._session.id == source_id
    assert env.engine._session.messages == source_messages
    final_replay = await env.service.create(**env.request)
    assert final_replay["id"] == first["id"]
    assert final_replay["steps"][0]["job_id"] == execution["job_id"]
    await env.service.tick()
    assert len(env.records) == 1


async def test_readonly_projection_never_repairs_an_execution_marker(review_setup, monkeypatch):
    env = review_setup
    created = await env.service.create(**env.request)
    value = await finish(env.service, created["id"])
    session_id = value["steps"][0]["session_id"]
    with sqlite3.connect(env.engine.config.memory.session_db_path) as db:
        db.execute("UPDATE sessions SET personal_execution_id='' WHERE id=?", (session_id,))

    async def forbidden_repair(*args, **kwargs):
        raise AssertionError("A read-only admission query must not repair a Session")

    monkeypatch.setattr(env.engine.session_store, "bind_personal_execution", forbidden_repair)
    with pytest.raises(PersonalAgentError) as caught:
        await env.service.get(created["id"], _repair_bindings=False)
    assert caught.value.code == "binding_conflict"
    assert (await env.engine.session_store.load(session_id)).personal_execution_id == ""


@pytest.mark.parametrize(
    "missing", ["parent_missing", "parent_disabled", "child_missing", "child_disabled"]
)
async def test_review_fails_closed_without_enabled_packet_broker(
    review_setup, monkeypatch, missing
):
    env = review_setup
    if missing.startswith("parent"):
        monkeypatch.setattr(
            env.engine,
            "personal_artifacts",
            None if missing == "parent_missing" else SimpleNamespace(enabled=False),
        )
    else:
        child_port = None if missing == "child_missing" else SimpleNamespace(enabled=False)
        env.service._child_factory = lambda step, job: ReviewChild(
            env.engine.config,
            step,
            env.records,
            chat_runs=env.engine.chat_run_store,
            artifacts=child_port,
        )
    created = await env.service.create(**env.request)
    result = await finish(env.service, created["id"])
    assert result["status"] == "error" and env.records == []
    execution = result["steps"][0]
    assert "资料访问边界" in execution["error"]
    job = await env.service.jobs.get(execution["job_id"])
    assert job.state is AgentJobState.ERROR and job.result.total_tokens == 0
    assert await env.engine.chat_run_store.list_runs(execution["session_id"]) == []


async def test_revocation_before_admission_preserves_original_plan_without_job(review_setup):
    env = review_setup
    created = await env.service.create(**env.request)
    env.authority.revoked = True
    await env.service.tick()
    value = await env.service.get(created["id"])
    assert value["cancel_requested"] and env.records == []
    assert value["steps"][0]["job_id"] == ""
    assert any(event["kind"] == "research_authority_lost" for event in value["events"])
    replay = await env.service.create(**env.request)
    assert replay["id"] == created["id"]
    assert replay["steps"][0]["session_id"] == created["steps"][0]["session_id"]
    assert len(env.service.store.list(env.service.workspace_key)) == 1


async def test_revocation_cancels_original_running_job_and_never_replays(review_setup):
    env = review_setup
    gate = asyncio.Event()
    env.service._child_factory = lambda step, job: ReviewChild(
        env.engine.config,
        step,
        env.records,
        chat_runs=env.engine.chat_run_store,
        artifacts=env.port,
        gate=gate,
    )
    created = await env.service.create(**env.request)
    await env.service.tick()
    for _ in range(100):
        if env.records:
            break
        await asyncio.sleep(0.005)
    assert len(env.records) == 1
    running = await env.service.get(created["id"])
    original_job = running["steps"][0]["job_id"]
    env.authority.revoked = True
    result = await finish(env.service, created["id"])
    assert result["status"] == "cancelled"
    assert result["steps"][0]["job_id"] == original_job
    assert (await env.service.jobs.get(original_job)).state is AgentJobState.CANCELLED
    replay = await env.service.create(**env.request)
    assert replay["id"] == created["id"]
    await env.service.tick()
    assert len(env.records) == 1


async def test_lease_renewal_checks_review_authority_and_cancels_owner(review_setup, monkeypatch):
    env = review_setup
    created = await env.service.create(**env.request)
    execution = asyncio.create_task(asyncio.Event().wait())
    env.authority.revoked = True
    renewals = []

    async def renewed(*args, **kwargs):
        renewals.append((args, kwargs))

    async def no_delay(_seconds):
        return None

    job = SimpleNamespace(job_id="existing-job", claim_epoch=4)
    env.service._job_session[job.job_id] = created["steps"][0]["session_id"]
    monkeypatch.setattr(env.service.jobs, "renew_claim", renewed)
    monkeypatch.setattr("naumi_agent.personal.collaboration.asyncio.sleep", no_delay)
    try:
        with pytest.raises(PersonalAgentError) as caught:
            await env.service._renew(job, execution)
        assert caught.value.code == "artifact_access_revoked"
        assert execution.cancelling() and len(renewals) == 1
        assert env.authority.authorization_calls == [created["id"]]
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


async def test_review_binds_real_exact_packet_not_other_daily_pins(artifacts, monkeypatch):
    env = artifacts
    reviewer = env.engine.personal_agents.update(
        env.profiles[1].id,
        env.profiles[1].version,
        tool_names=["personal_research", "file_read"],
    )
    packet = await _publish(
        env,
        owner="main",
        request="review-packet",
        filename="review.json",
        title="明确复核资料包",
        content=b'{"claim":"exact-packet"}',
    )
    await _share(env, packet, request="review-packet-share")
    reviewer_packet = await env.service.get(
        actor_id=reviewer.id,
        artifact_id=packet["artifact_id"],
        version=1,
    )
    unrelated = await _publish(
        env,
        owner="main",
        request="unrelated-daily",
        content=b"PRIVATE_DAILY_NOT_PACKET\n",
        filename="unrelated.txt",
        title="其他日常资料",
    )
    source = env.engine._session.id
    daily_refs = [env.service.reference(packet), env.service.reference(unrelated)]
    daily_refs = await env.service.bind_refs(source, "main", daily_refs)
    packet_ref = env.service.reference(reviewer_packet)
    expected = binding(
        packet_ref=packet_ref,
        reviewer_id=reviewer.id,
        reviewer_version=reviewer.version,
    )
    authority = install_authority(monkeypatch, env.engine, expected)
    records = []
    collaboration = env.engine.personal_collaborations
    collaboration._child_factory = lambda step, job: ReviewChild(
        env.config,
        step,
        records,
        chat_runs=env.engine.chat_run_store,
        artifacts=env.service,
    )

    async def forbidden_daily_propagation(*args, **kwargs):
        raise AssertionError("A semantic reviewer must never copy the source Session ALL pins")

    monkeypatch.setattr(env.service, "propagate", forbidden_daily_propagation)
    created = await collaboration.create(
        source_session_id=source,
        request_id="real-review-packet",
        title="独立复核",
        context="EXACT_PACKET_ONLY",
        steps=[
            step(
                agent_id=reviewer.id,
                agent_version=reviewer.version,
            )
        ],
        _research_execution=expected,
    )
    result = await finish(collaboration, created["id"])
    assert result["status"] == "completed", result
    execution = result["steps"][0]
    assert await env.service.session_refs(execution["session_id"], reviewer.id) == [packet_ref]
    assert await env.service.session_refs(source, "main") == daily_refs
    with pytest.raises(PersonalAgentError) as caught:
        await env.service.get(actor_id=reviewer.id, artifact_id=unrelated["artifact_id"], version=1)
    assert caught.value.code == "not_found"
    assert len(records) == 1 and authority.bindings[0][2] == execution["task_id"]


async def test_ordinary_collaboration_keeps_full_source_artifact_propagation(artifacts):
    env = artifacts
    first = await _publish(env, owner="main", request="ordinary-one")
    second = await _publish(
        env,
        owner="main",
        request="ordinary-two",
        filename="more.txt",
        content=b"MORE\n",
    )
    for index, material in enumerate((first, second)):
        await _share(env, material, request=f"ordinary-share-{index}")
    source = env.engine._session.id
    refs = [env.service.reference(first), env.service.reference(second)]
    await env.service.bind_refs(source, "main", refs)
    records = []
    recipient = env.profiles[1]
    collaboration = env.engine.personal_collaborations
    collaboration._child_factory = lambda step, job: ReviewChild(
        env.config,
        step,
        records,
        chat_runs=env.engine.chat_run_store,
        artifacts=env.service,
    )
    created = await collaboration.create(
        source_session_id=source,
        request_id="ordinary-all-pins",
        title="日常协作",
        context="显式共享全部资料。",
        steps=[
            {
                key: value
                for key, value in step(
                    agent_id=recipient.id,
                    agent_version=recipient.version,
                ).items()
                if key != "tool_names"
            }
        ],
    )
    result = await finish(collaboration, created["id"])
    assert result["status"] == "completed", result
    received = await env.service.session_refs(result["steps"][0]["session_id"], recipient.id)
    assert {ref["artifact_id"] for ref in received} == {ref["artifact_id"] for ref in refs}
    assert len(received) == 2 and len(records) == 1
    assert "research_execution" not in result
