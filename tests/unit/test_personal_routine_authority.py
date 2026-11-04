"""Real journal, artifact, memory, Item receipt and AgentJob authority boundaries."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.daemons.agent_jobs import AgentJobPayload, AgentJobStore
from naumi_agent.daemons.agent_worker_contract import AgentWorkerRequest, issue_agent_worker_request
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.personal.artifacts import PersonalArtifactService
from naumi_agent.personal.item_execution import main_policy_sha256
from naumi_agent.personal.item_inputs import capture_inputs
from naumi_agent.personal.items import PersonalItemService
from naumi_agent.personal.memories import PersonalMemoryService
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.routine_authority import RoutineAuthorityMixin
from naumi_agent.personal.routine_budget import budget_initialize, budget_reserve
from naumi_agent.personal.routine_models import canonical, digest
from naumi_agent.personal.routine_schedule import normalize_schedule
from naumi_agent.personal.routine_store import RoutineStore
from naumi_agent.personal.service import PersonalAgentService
from naumi_agent.personal.store import PersonalAgentStore
from naumi_agent.scheduler.runner import SchedulerRunner
from naumi_agent.scheduler.store import SchedulerStore
from naumi_agent.tools.base import Tool, ToolRegistry

NOW = datetime(2030, 1, 1, tzinfo=UTC)


class Capability(Tool):
    name = "test-capability"
    description = "测试注册能力"
    parameters_schema = {"type": "object"}

    def __init__(self, name):
        self.name = name

    async def execute(self, **kwargs):
        raise AssertionError("Authority tests do not run models or tools")


class NoSemanticIndex:
    @staticmethod
    def _unavailable(*args, **kwargs):
        raise PersonalAgentError("memory_index_unavailable", "本地来源测试不加载向量模型")

    upsert = delete = query = ensure_current = _unavailable

    @staticmethod
    def status():
        return {"status": "unavailable", "automatic_download": False}


class ExecutionPort:
    """No transport; the Job database used by the gate remains the real authority."""

    def __init__(self, jobs):
        self.jobs = jobs

    async def view(self, intent):
        return {"occurrence_id": intent["occurrence_id"], "status": "prepared", "artifacts": []}

    async def stop(self):
        pass


class Authority(RoutineAuthorityMixin):
    """Narrow owner-journal host implementing the production mixin's declared ports."""

    def __init__(self, engine, items, clock, directory):
        self.engine, self.items, self.clock = engine, items, clock
        self.personal = engine.personal_agents
        self.owner_key, self.workspace_key = items.owner_key, items.workspace_key
        self.store = RoutineStore(directory, owner_key=self.owner_key)
        self.boundary = self.personal.store.base_dir / ".personal-routines-instance-v1"

    def now(self):
        return self.clock[0]

    def stamp(self):
        return self.now().isoformat(timespec="microseconds")

    def actor_version(self, actor):
        return 0 if actor == "main" else self.personal._executable(actor).version

    async def _read(self, callback):
        return await asyncio.to_thread(self.store.read, callback)

    async def _write(self, callback):
        return await asyncio.to_thread(self.store.write, callback)

    async def _record(self, kind, identity, *, revision=None, missing=False):
        return await self._read(
            lambda tx: tx.get(kind, identity, revision=revision, missing=missing)
        )

    def _scope(self, value, caller_actor=None):
        if value["workspace_key"] != self.workspace_key or value["definition"]["route"] != (
            self.items._route()
        ):
            raise PersonalAgentError("not_found", "原计划不在当前工作区")
        if digest(value["definition"]) != value["definition_sha256"]:
            raise PersonalAgentError("storage_error", "原计划摘要不一致")
        if (
            caller_actor is not None
            and caller_actor != value["definition"]["assignee"]["id"]
            and not (caller_actor == "main" and value["origin_actor_id"] == "main")
        ):
            raise PersonalAgentError("not_found", "原计划不在当前身份范围")
        return value

    async def _routine(self, identity, caller_actor=None, *, revision=None):
        return self._scope(await self._record("routine", identity, revision=revision), caller_actor)

    async def change(self, kind, identity, **changes):
        def write(tx):
            value = tx.get(kind, identity)
            return tx.put(kind, {**value, **changes}, expected_revision=value["revision"])

        return await self._write(write)

    async def definition(
        self, *, actor="main", refs=None, sources=None, memories=None, directories=None
    ):
        template = await self.items._definition(
            {
                "title": "原周期计划",
                "instructions": "读取实际资料并保留原始来源",
                "assignee_id": actor,
                "assignee_version": self.actor_version(actor),
                "input_refs": refs or [],
                "directories": directories or [],
                "schedule": {"mode": "now"},
            }
        )
        return {
            "schema_version": 1,
            **{
                key: template[key]
                for key in (
                    "title",
                    "instructions",
                    "tags",
                    "assignee",
                    "route",
                    "inputs",
                    "policy",
                )
            },
            "schedule": normalize_schedule({"kind": "cron", "expression": "0 9 * * *"}, self.now()),
            "limits": {
                "max_executions": 100,
                "total_cost_microusd": 100_000_000,
                "max_concurrent": 4,
                "max_pending": 10,
            },
            "recovery": {
                "misfire_policy": "latest",
                "grace_seconds": 3600,
                "max_catch_up": 2,
                "overlap_policy": "queue",
            },
            "source_context": {
                "artifact_refs": sources or [],
                "memory_refs": memories or [],
                "memory_item_id": None,
                "context_session_ids": [],
            },
        }

    async def routine(self, definition=None, *, origin="main", **definition_args):
        definition = definition or await self.definition(**definition_args)
        value = {
            "id": "pr_" + uuid.uuid4().hex,
            "status": "active",
            "epoch": uuid.uuid4().hex,
            "workspace_key": self.workspace_key,
            "origin_actor_id": origin,
            "definition": definition,
            "definition_sha256": digest(definition),
            "created_at": self.stamp(),
            "updated_at": self.stamp(),
        }

        def write(tx):
            routine = tx.put("routine", value, expected_revision=0)
            budget_initialize(tx, routine)
            return routine

        return await self._write(write)

    async def reserve(self, routine):
        identity = "pro_" + uuid.uuid4().hex
        value = {
            "id": identity,
            "routine_id": routine["id"],
            "epoch": routine["epoch"],
            "definition_revision": routine["revision"],
            "definition_sha256": routine["definition_sha256"],
            "actor_id": routine["definition"]["assignee"]["id"],
            "workspace_key": self.workspace_key,
            "status": "reserved",
            "item_id": None,
            "item_request_id": "routine-item:" + identity,
            "item_create_attempted": False,
            "guard_ref": None,
            "child_input_refs": [],
            "nominal_due_at": self.stamp(),
        }

        def write(tx):
            budget_reserve(tx, routine, identity)
            return tx.put("occurrence", value, expected_revision=0)

        return await self._write(write)

    @staticmethod
    def public(routine):
        definition = copy.deepcopy(routine["definition"])
        definition.pop("source_context")
        definition["schedule"].pop("calendar_sessions", None)
        return {
            **{
                key: routine[key]
                for key in (
                    "id",
                    "revision",
                    "status",
                    "epoch",
                    "definition_sha256",
                    "created_at",
                    "updated_at",
                )
            },
            "definition": definition,
            "budget": {"remaining_executions": 100},
            "next_due_at": None,
            "attention_code": None,
        }

    async def child(self, routine, *, backfill=True):
        occurrence = await self.reserve(routine)
        guard, child_refs = await self.create_occurrence_guard(occurrence, routine)
        occurrence = await self.change(
            "occurrence",
            occurrence["id"],
            guard_ref=guard,
            child_input_refs=child_refs,
            item_create_attempted=True,
        )
        definition = routine["definition"]
        child = await self.items.create(
            {
                "request_id": occurrence["item_request_id"],
                "title": definition["title"],
                "instructions": definition["instructions"],
                "assignee_id": occurrence["actor_id"],
                "assignee_version": definition["assignee"]["version"],
                "input_refs": child_refs,
                "directories": definition["inputs"]["directories"],
                "policy": definition["policy"],
                "schedule": {"mode": "now"},
            }
        )
        item = await self.items._item(child["item_id"])
        identity, session = "pio_" + uuid.uuid4().hex, "pie_" + uuid.uuid4().hex
        refs, manifest, context = await capture_inputs(self.engine, item["definition"], identity)
        payload = AgentJobPayload(
            task_id=identity,
            session_id=session,
            task=definition["instructions"],
            context=context,
            message_topic="item:" + item["once_key"],
        )
        policy = item["definition"]["policy"]
        request = issue_agent_worker_request(
            task_id=identity,
            session_id=session,
            agent_name=occurrence["actor_id"],
            task=payload.task,
            context=context,
            message_topic=payload.message_topic,
            tool_scope=policy["tool_names"],
            permission_mode="bypass",
            model_tier=policy["model_tier"],
            max_turns=policy["max_turns"],
            max_budget_usd=policy["max_cost_microusd"] / 1_000_000,
            timeout_seconds=policy["timeout_seconds"],
            issued_at=self.stamp(),
        )
        intent = {
            "occurrence_id": identity,
            "item_id": item["item_id"],
            "item_revision": item["revision"],
            "once_key": item["once_key"],
            "definition_sha256": item["definition_sha256"],
            "workspace_key": self.workspace_key,
            "schedule_id": "schedule:" + occurrence["id"],
            "event_id": "event:" + occurrence["id"],
            "execution_session_id": session,
            "task_id": identity,
            "actor_id": occurrence["actor_id"],
            "agent_version": definition["assignee"]["version"],
            "input_refs": refs,
            "input_manifest": manifest,
            "policy": policy,
            "main_policy_sha256": main_policy_sha256(self.engine)
            if occurrence["actor_id"] == "main"
            else "",
            "request": asdict(request),
            "payload": asdict(payload),
            "issued_at": request.issued_at,
        }
        intent = json.loads(json.dumps(intent))
        intent["prepared_sha256"] = digest(intent)
        intent = await self.items._call(
            "prepare",
            item_id=item["item_id"],
            expected_definition_sha256=item["definition_sha256"],
            intent=intent,
        )
        if backfill:
            occurrence = await self.change("occurrence", occurrence["id"], item_id=item["item_id"])
        return occurrence, intent

    async def job(self, intent, state="running"):
        values = dict(intent["request"])
        values["tool_scope"] = tuple(values["tool_scope"])
        request = AgentWorkerRequest(**values)
        job = await self.items.executor.jobs.admit(
            request=request, payload=AgentJobPayload(**intent["payload"])
        )
        if state in {"claimed", "running"}:
            transition = await self.items.executor.jobs.claim(
                job.job_id, owner_id="authority-worker", lease_seconds=60
            )
            if state == "running":
                await self.items.executor.jobs.mark_running(
                    job.job_id,
                    owner_id="authority-worker",
                    claim_epoch=transition.job.claim_epoch,
                )
        return await self.items._call(
            "bind",
            occurrence_id=intent["occurrence_id"],
            expected_prepared_sha256=intent["prepared_sha256"],
            references={"job_id": job.job_id},
        )


@pytest.fixture
async def authority(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    config = AppConfig(
        workspace_root=str(work),
        personal={"data_dir": str(tmp_path / "personal")},
        memory={
            "session_db_path": str(tmp_path / "runtime" / "sessions.db"),
            "vector_db_path": str(tmp_path / "legacy"),
            "long_term_enabled": True,
        },
        safety={"permission_mode": "bypass", "allowed_dirs": [str(work)], "max_turns": 100},
    )
    registry = ToolRegistry()
    for name in ("personal_artifacts", "code_execute", "file_read"):
        registry.register(Capability(name))
    clock = [NOW]
    engine = SimpleNamespace(
        config=config,
        workspace_root=work,
        session_store=SessionStore(config.memory),
        tool_registry=registry,
        router=SimpleNamespace(resolve_model=lambda tier: "local-no-model"),
        _compactor=SimpleNamespace(_long_term_memory=None),
        _messages=[],
        _full_history=[],
        _personal_artifact_refs=[],
        _personal_active_turn=None,
    )
    engine.personal_agents = PersonalAgentService(
        engine, PersonalAgentStore(config.personal.data_dir)
    )
    engine.personal_artifacts = PersonalArtifactService(engine)
    memory_service = PersonalMemoryService(engine, index=NoSemanticIndex())
    engine.personal_memory = memory_service
    engine.scheduler_runner = SchedulerRunner(
        SchedulerStore(tmp_path / "scheduler"), now_fn=lambda: clock[0]
    )
    jobs = AgentJobStore(
        (tmp_path / "jobs.db").resolve(), key_provider=lambda: b"a" * 32, clock=lambda: clock[0]
    )
    engine.personal_items = PersonalItemService(
        engine, executor=ExecutionPort(jobs), clock=lambda: clock[0]
    )
    service = Authority(engine, engine.personal_items, clock, tmp_path / "routines")
    engine.personal_routines = service
    engine._session = Session(workspace_root=str(work))
    await engine.session_store.save(engine._session)
    profile = engine.personal_agents.create(
        request_id="create-independent-agent",
        name="Echo",
        instructions="按用户要求保留原始资料",
        tool_names=["personal_artifacts", "code_execute", "file_read"],
    )
    try:
        yield SimpleNamespace(
            service=service, engine=engine, profile=profile, clock=clock, work=work
        )
    finally:
        await engine.personal_items.stop()
        await memory_service.stop()
        await engine.session_store.close()


async def material(f, *, actor="main", content=b"original actual bytes\n", request=None):
    return await f.engine.personal_artifacts.publish(
        actor_id=actor,
        request_id=request or "material:" + uuid.uuid4().hex,
        title="原始资料",
        kind="material",
        filename="original.txt",
        content=content,
    )


async def memory(f):
    return await f.engine.personal_memory.dispatch(
        "create",
        {
            "request_id": "create-original-memory",
            "topic": "来源偏好",
            "content": "原始记忆正文",
            "category": "preference",
            "scope": "personal",
        },
        trusted_channel="management_web",
    )


async def test_guard_copies_actual_input_bytes_and_freezes_source_context(authority):
    f = authority
    source = await material(f, content=b"line1\r\nline2\xef\xbb\xbf tail")
    ref = f.engine.personal_artifacts.reference(source)
    note = await memory(f)
    routine = await f.service.routine(refs=[ref], sources=[ref], memories=[note["ref"]])
    occurrence = await f.service.reserve(routine)
    guard, child_refs = await f.service.create_occurrence_guard(occurrence, routine)
    snapshot, content = await f.engine.personal_artifacts.read(
        actor_id="main",
        artifact_id=guard["artifact_id"],
        version=guard["version"],
    )
    assert content == b"line1\r\nline2\xef\xbb\xbf tail"
    assert child_refs == [guard] and snapshot["source_refs"] == [ref]
    assert snapshot["evidence"]["source_context"] == routine["definition"]["source_context"]
    assert snapshot["evidence"]["routine_refs"] == [f.service._occurrence_reference(occurrence)]
    with sqlite3.connect(f.engine.personal_artifacts.store.db_path) as connection:
        row = connection.execute(
            "SELECT record_json FROM artifact_versions WHERE artifact_id=?", (guard["artifact_id"],)
        ).fetchone()
    assert json.loads(row[0])["authority_version"] == 3


async def test_guard_replaces_one_slot_even_at_real_64_input_capacity(authority):
    f = authority
    snapshots = [await material(f, content=f"real source {index}".encode()) for index in range(64)]
    refs = [f.engine.personal_artifacts.reference(value) for value in snapshots]
    routine = await f.service.routine(refs=refs)
    original = routine["definition"]["inputs"]["artifact_refs"]
    occurrence = await f.service.reserve(routine)
    guard, child_refs = await f.service.create_occurrence_guard(occurrence, routine)
    assert len(child_refs) == 64 and guard in child_refs and original[0] not in child_refs
    assert all(ref in child_refs for ref in original[1:])
    _, content = await f.engine.personal_artifacts.read(
        actor_id="main", artifact_id=guard["artifact_id"], version=1
    )
    assert hashlib.sha256(content).hexdigest() == original[0]["sha256"]


async def test_no_input_guard_is_real_text_and_directory_capacity_is_not_hidden(authority):
    f = authority
    directory = f.work / "selected"
    directory.mkdir()
    for index in range(64):
        (directory / f"{index:02d}.txt").write_text(f"actual file {index}", encoding="utf-8")
    routine = await f.service.routine(
        directories=[{"path": "selected", "pattern": "*.txt", "max_files": 64}]
    )
    occurrence = await f.service.reserve(routine)
    guard, refs = await f.service.create_occurrence_guard(occurrence, routine)
    assert refs == [guard]
    _, content = await f.engine.personal_artifacts.read(
        actor_id="main", artifact_id=guard["artifact_id"], version=1
    )
    assert json.loads(content)["routine_ref"] == f.service._occurrence_reference(occurrence)
    template = copy.deepcopy(routine["definition"])
    template["inputs"]["artifact_refs"] = refs
    with pytest.raises(PersonalAgentError) as exc:
        await capture_inputs(f.engine, template, "pio_" + "f" * 32)
    assert exc.value.code == "input_limit" and "64" in exc.value.message


async def test_guard_context_union_over_64_fails_without_truncating(authority):
    f = authority
    snapshots = [await material(f, content=f"context {index}".encode()) for index in range(65)]
    refs = [f.engine.personal_artifacts.reference(value) for value in snapshots]
    routine = await f.service.routine(refs=refs[:1], sources=refs[1:])
    occurrence = await f.service.reserve(routine)
    with pytest.raises(PersonalAgentError, match="64"):
        await f.service.create_occurrence_guard(occurrence, routine)
    assert (await f.service._record("occurrence", occurrence["id"]))["guard_ref"] is None


@pytest.mark.parametrize(
    "change", ["missing", "wrong_epoch", "wrong_revision", "settled", "missing_reservation"]
)
async def test_guard_requires_actual_original_reserved_occurrence(authority, change):
    f = authority
    routine = await f.service.routine()
    occurrence = await f.service.reserve(routine)
    if change == "missing":
        occurrence["id"] = "pro_" + "f" * 32
    elif change == "wrong_epoch":
        occurrence["epoch"] = "f" * 32
    elif change == "wrong_revision":
        occurrence["definition_revision"] = 2
    elif change == "settled":
        await f.service.change(
            "budget", "reservation:" + occurrence["id"], status="settled", settlement="known"
        )
    else:
        original = f.service._record

        async def without_reservation(kind, identity, **kwargs):
            if kind == "budget":
                raise PersonalAgentError("not_found", "原预算预留不存在")
            return await original(kind, identity, **kwargs)

        f.service._record = without_reservation
    with pytest.raises(PersonalAgentError):
        await f.service.create_occurrence_guard(occurrence, routine)


async def test_definition_executor_inputs_do_not_grant_main_input_bytes(authority):
    f = authority
    private = await material(f, actor=f.profile.id)
    definition = await f.service.definition(
        actor=f.profile.id, refs=[f.engine.personal_artifacts.reference(private)]
    )
    await f.service.authorize_definition_sources(definition, "main")
    with pytest.raises(PersonalAgentError):
        await f.engine.personal_artifacts.read(
            actor_id="main", artifact_id=private["artifact_id"], version=1
        )


async def test_definition_reader_must_have_same_original_context_permissions(authority):
    f = authority
    private = await material(f, actor=f.profile.id)
    ref = f.engine.personal_artifacts.reference(private)
    definition = await f.service.definition(actor=f.profile.id, sources=[ref])
    await f.service.authorize_definition_sources(definition)
    with pytest.raises(PersonalAgentError):
        await f.service.authorize_definition_sources(definition, "main")
    await f.engine.personal_artifacts.share(
        actor_id=f.profile.id,
        artifact_id=private["artifact_id"],
        version=1,
        recipient_id="main",
        request_id="new-main-grant",
    )
    # A different new grant is not substituted for the frozen owner reference.
    with pytest.raises(PersonalAgentError):
        await f.service.authorize_definition_sources(definition, "main")


async def test_original_memory_correction_and_missing_component_block_body(authority):
    f = authority
    note = await memory(f)
    definition = await f.service.definition(memories=[note["ref"]])
    await f.service.authorize_definition_sources(definition)
    with pytest.raises(PersonalAgentError):
        await f.service.authorize_definition_sources(definition, f.profile.id)
    saved = f.engine.personal_memory
    f.engine.personal_memory = None
    with pytest.raises(PersonalAgentError) as exc:
        await f.service.authorize_definition_sources(definition)
    assert exc.value.code == "routine_source_revoked"
    f.engine.personal_memory = saved
    await saved.dispatch(
        "revise",
        {
            "request_id": "correct-note",
            "memory_id": note["memory_id"],
            "expected_revision": note["revision"],
            "expected_content_sha256": note["content_sha256"],
            "content": "已纠正的新记忆",
        },
        trusted_channel="management_web",
    )
    with pytest.raises(PersonalAgentError):
        await f.service.authorize_definition_sources(definition)


async def test_missing_artifact_component_never_bypasses_even_empty_definition(authority):
    f = authority
    definition = await f.service.definition()
    f.engine.personal_artifacts = None
    with pytest.raises(PersonalAgentError) as exc:
        await f.service.authorize_definition_sources(definition)
    assert exc.value.code == "routine_source_revoked"


async def test_memory_item_scope_is_forwarded_as_frozen_source_evidence(authority):
    f = authority
    note = await memory(f)
    definition = await f.service.definition(memories=[note["ref"]])
    definition["source_context"]["memory_item_id"] = "pi_" + "d" * 32
    calls = []

    class ItemScopedMemoryPort:
        async def resolve_ref(self, ref, actor, *, item_id):
            calls.append((copy.deepcopy(ref), actor, item_id))
            raise PersonalAgentError("memory_access_revoked", "原事项范围不匹配")

    f.engine.personal_memory = ItemScopedMemoryPort()
    with pytest.raises(PersonalAgentError):
        await f.service.authorize_definition_sources(definition)
    assert calls == [(note["ref"], "main", "pi_" + "d" * 32)]


async def test_plan_read_pins_real_bytes_to_actual_current_session_and_replays(authority):
    f = authority
    routine = await f.service.routine()
    public = f.service.public(routine)
    result = await f.service.pin_plan_read(public, "main")
    assert result is public
    refs = await f.engine.personal_artifacts.session_refs(f.engine._session.id, "main")
    assert len(refs) == 1
    snapshot, content = await f.engine.personal_artifacts.read(
        actor_id="main", artifact_id=refs[0]["artifact_id"], version=1
    )
    assert content == canonical(public).encode()
    assert snapshot["evidence"]["routine_plan_refs"] == [f.service._plan_reference(routine)]
    assert await f.service.pin_plan_read(public, "main") is public
    assert await f.engine.personal_artifacts.session_refs(f.engine._session.id, "main") == refs


@pytest.mark.parametrize(
    "mode", ["none", "wrong_actor", "unpersisted", "wrong_body", "foreign_main"]
)
async def test_plan_read_does_not_publish_body_without_valid_actor_session_and_scope(
    authority, mode
):
    f = authority
    routine = (
        await f.service.routine(actor=f.profile.id, origin=f.profile.id)
        if mode == "foreign_main"
        else await f.service.routine()
    )
    public = f.service.public(routine)
    if mode == "none":
        f.engine._session = None
    elif mode == "wrong_actor":
        f.engine._session.personal_agent_id = f.profile.id
    elif mode == "unpersisted":
        f.engine._session = Session(workspace_root=str(f.work))
    elif mode == "wrong_body":
        public["definition"]["instructions"] = "unbound substituted body"
    with pytest.raises(PersonalAgentError):
        await f.service.pin_plan_read(public, "main")


async def test_human_plan_read_remains_unmodified_without_model_session(authority):
    f = authority
    f.engine._session = None
    public = {"human": "unchanged"}
    assert await f.service.pin_plan_read(public, None) is public


async def test_session_switch_during_pin_does_not_return_plan_body(authority, monkeypatch):
    f = authority
    public = f.service.public(await f.service.routine())
    original = f.engine.personal_artifacts.publish
    previous = f.engine._session

    async def switch(**kwargs):
        value = await original(**kwargs)
        f.engine._session = Session(workspace_root=str(f.work))
        await f.engine.session_store.save(f.engine._session)
        return value

    monkeypatch.setattr(f.engine.personal_artifacts, "publish", switch)
    with pytest.raises(PersonalAgentError) as exc:
        await f.service.pin_plan_read(public, "main")
    assert exc.value.code == "binding_conflict"
    assert f.engine._session is not previous
    assert await f.engine.personal_artifacts.session_refs(f.engine._session.id, "main") == []


async def test_immutable_ancestor_walk_uses_primitive_get_not_recursive_public_get(
    authority, monkeypatch
):
    f = authority
    routine = await f.service.routine()
    snapshot = await f.engine.personal_artifacts.publish(
        actor_id="main",
        request_id="ancestor-plan",
        title="原计划",
        kind="material",
        filename="plan.txt",
        content=b"original plan bytes",
        evidence={"routine_plan_refs": [f.service._plan_reference(routine)]},
    )
    derivative = await f.engine.personal_artifacts.publish(
        actor_id="main",
        request_id="ancestor-copy",
        title="原派生产物",
        kind="material",
        filename="copy.txt",
        content=b"derived bytes",
        source_refs=[f.engine.personal_artifacts.reference(snapshot)],
    )

    async def recursive_get(**kwargs):
        raise AssertionError("Routine ancestry must not reenter public artifact get")

    monkeypatch.setattr(f.engine.personal_artifacts, "get", recursive_get)
    await f.service.authorize_artifact(derivative, "main")


@pytest.mark.parametrize(
    "field,value",
    [
        ("definition_sha256", "e" * 64),
        ("definition_revision", 2),
        ("actor_id", "pa_" + "e" * 32),
        ("workspace_key", "e" * 64),
        ("epoch", "e" * 32),
        ("occurrence_id", "pro_" + "e" * 32),
    ],
)
async def test_typed_occurrence_refs_reject_all_original_binding_changes(authority, field, value):
    f = authority
    routine = await f.service.routine()
    occurrence = await f.service.reserve(routine)
    snapshot = await material(f)
    ref = f.service._occurrence_reference(occurrence) | {field: value}
    snapshot["evidence"] = {"routine_refs": [ref]}
    with pytest.raises(PersonalAgentError):
        await f.service.authorize_artifact(snapshot, "main")


async def test_completed_history_ignores_new_parent_state_epoch_and_agent_version(authority):
    f = authority
    routine = await f.service.routine(actor=f.profile.id, origin=f.profile.id)
    occurrence = await f.service.reserve(routine)
    guard, _ = await f.service.create_occurrence_guard(occurrence, routine)
    snapshot = await f.engine.personal_artifacts._call(
        "get", actor_id=f.profile.id, artifact_id=guard["artifact_id"], version=1
    )
    await f.service.change("routine", routine["id"], status="cancelled", epoch="f" * 32)
    f.engine.personal_agents.update(
        f.profile.id, f.profile.version, instructions="更新后的 Agent 配置"
    )
    await f.service.authorize_artifact(snapshot, f.profile.id)
    with pytest.raises(PersonalAgentError) as exc:
        await f.service.authorize_execution_definition(routine["definition"])
    assert exc.value.code == "version_conflict"


async def test_old_plan_source_ref_is_not_replaced_by_new_definition_grants(authority):
    f = authority
    source = await material(f, actor=f.profile.id)
    grant = await f.engine.personal_artifacts.share(
        actor_id=f.profile.id,
        artifact_id=source["artifact_id"],
        version=1,
        recipient_id="main",
        request_id="old-share",
    )
    shared = await f.engine.personal_artifacts.get(
        actor_id="main", artifact_id=source["artifact_id"], version=1
    )
    definition = await f.service.definition(sources=[f.engine.personal_artifacts.reference(shared)])
    routine = await f.service.routine(definition)
    snapshot = await material(f)
    snapshot["evidence"] = {"routine_plan_refs": [f.service._plan_reference(routine)]}
    await f.service.authorize_artifact(snapshot, "main")
    await f.engine.personal_artifacts.revoke(
        actor_id=f.profile.id,
        grant_id=grant["grant_id"],
        expected_revision=grant["revision"],
        request_id="revoke-old-share",
    )
    await f.engine.personal_artifacts.share(
        actor_id=f.profile.id,
        artifact_id=source["artifact_id"],
        version=1,
        recipient_id="main",
        request_id="replacement-share",
    )
    replacement = await f.engine.personal_artifacts.get(
        actor_id="main", artifact_id=source["artifact_id"], version=1
    )
    current = copy.deepcopy(definition)
    current["source_context"]["artifact_refs"] = [
        f.engine.personal_artifacts.reference(replacement)
    ]
    await f.service.change(
        "routine", routine["id"], definition=current, definition_sha256=digest(current)
    )
    await f.service.authorize_definition_sources(current)
    with pytest.raises(PersonalAgentError):
        await f.service.authorize_artifact(snapshot, "main")


async def test_artifact_self_dependency_and_deep_chains_are_rejected(authority, monkeypatch):
    f = authority
    snapshot = await material(f)
    ref = f.engine.personal_artifacts.reference(snapshot)
    snapshot["source_refs"] = [ref]
    original = f.engine.personal_artifacts._call

    async def cycle(method, **kwargs):
        if method == "get":
            return copy.deepcopy(snapshot)
        return await original(method, **kwargs)

    monkeypatch.setattr(f.engine.personal_artifacts, "_call", cycle)
    with pytest.raises(PersonalAgentError) as exc:
        await f.service.authorize_artifact(snapshot, "main")
    assert exc.value.code == "storage_error"


async def test_execution_uses_original_create_receipt_when_item_id_backfill_is_unknown(authority):
    f = authority
    routine = await f.service.routine()
    occurrence, intent = await f.service.child(routine, backfill=False)
    assert occurrence["item_id"] is None
    await f.service.authorize_occurrence_execution(occurrence, intent)
    assert (await f.service._record("occurrence", occurrence["id"]))["item_id"] is None
    receipt = await f.engine.personal_items._call(
        "find_request", request_id=occurrence["item_request_id"]
    )
    assert receipt["committed_item"]["item_id"] == intent["item_id"]


@pytest.mark.parametrize("state", ["prepared", "admitted", "claimed", "running"])
async def test_pause_only_allows_real_live_running_not_claimed_or_ui_status(authority, state):
    f = authority
    routine = await f.service.routine()
    occurrence, intent = await f.service.child(routine)
    if state != "prepared":
        intent = await f.service.job(intent, state)
    await f.service.change("routine", routine["id"], status="paused")
    if state == "running":
        await f.service.authorize_occurrence_execution(occurrence, intent)
    else:
        with pytest.raises(PersonalAgentError) as exc:
            await f.service.authorize_occurrence_execution(occurrence, intent)
        assert exc.value.code == "occurrence_paused"


@pytest.mark.parametrize("state", ["prepared", "running"])
async def test_new_epoch_only_fences_unstarted_occurrences(authority, state):
    f = authority
    routine = await f.service.routine()
    occurrence, intent = await f.service.child(routine)
    if state == "running":
        intent = await f.service.job(intent)
    await f.service.change("routine", routine["id"], epoch="f" * 32)
    if state == "running":
        await f.service.authorize_occurrence_execution(occurrence, intent)
    else:
        with pytest.raises(PersonalAgentError) as exc:
            await f.service.authorize_occurrence_execution(occurrence, intent)
        assert exc.value.code == "occurrence_superseded"


@pytest.mark.parametrize("status", ["cancelled", "closed"])
async def test_parent_end_blocks_even_real_running_at_next_boundary(authority, status):
    f = authority
    routine = await f.service.routine()
    occurrence, intent = await f.service.child(routine)
    intent = await f.service.job(intent)
    await f.service.change("routine", routine["id"], status=status)
    with pytest.raises(PersonalAgentError) as exc:
        await f.service.authorize_occurrence_execution(occurrence, intent)
    assert exc.value.code == "occurrence_cancelled"


async def test_expired_running_lease_and_missing_job_original_never_allow_replay(authority):
    f = authority
    occurrence, intent = await f.service.child(await f.service.routine())
    intent = await f.service.job(intent)
    f.clock[0] += timedelta(seconds=61)
    with pytest.raises(PersonalAgentError) as exc:
        await f.service.authorize_occurrence_execution(occurrence, intent)
    assert exc.value.code == "lease_expired"
    f.clock[0] = NOW
    f.engine.personal_items.executor.jobs = AgentJobStore(
        (f.work / "empty-jobs.db").resolve(),
        key_provider=lambda: b"a" * 32,
        clock=lambda: f.clock[0],
    )
    with pytest.raises(PersonalAgentError) as exc:
        await f.service.authorize_occurrence_execution(occurrence, intent)
    assert exc.value.code == "storage_error"


async def test_missing_item_receipt_stays_unknown_and_does_not_create_again(authority):
    f = authority
    occurrence, intent = await f.service.child(await f.service.routine(), backfill=False)
    occurrence = await f.service.change(
        "occurrence", occurrence["id"], item_request_id="unknown-original-request"
    )
    with pytest.raises(PersonalAgentError) as exc:
        await f.service.authorize_occurrence_execution(occurrence, intent)
    assert exc.value.code == "storage_error"
    assert (
        await f.engine.personal_items._call("find_request", request_id="unknown-original-request")
        is None
    )


async def test_foreign_real_item_receipt_intent_and_caller_tampering_are_rejected(authority):
    f = authority
    first, first_intent = await f.service.child(await f.service.routine())
    _, other_intent = await f.service.child(await f.service.routine())
    with pytest.raises(PersonalAgentError):
        await f.service.authorize_occurrence_execution(first, other_intent)
    forged = copy.deepcopy(first_intent)
    forged["policy"]["max_cost_microusd"] += 1
    with pytest.raises(PersonalAgentError):
        await f.service.authorize_occurrence_execution(first, forged)


async def test_missing_job_component_and_current_policy_tightening_fail_closed(authority):
    f = authority
    occurrence, intent = await f.service.child(await f.service.routine())
    saved = f.engine.personal_items.executor.jobs
    f.engine.personal_items.executor.jobs = None
    with pytest.raises(PersonalAgentError) as exc:
        await f.service.authorize_occurrence_execution(occurrence, intent)
    assert exc.value.code == "storage_error"
    f.engine.personal_items.executor.jobs = saved
    f.engine.config.safety.max_turns = 1
    with pytest.raises(PersonalAgentError):
        await f.service.authorize_occurrence_execution(occurrence, intent)


async def test_source_revocation_during_model_pin_does_not_return_body(authority, monkeypatch):
    f = authority
    source = await material(f, actor=f.profile.id)
    grant = await f.engine.personal_artifacts.share(
        actor_id=f.profile.id,
        artifact_id=source["artifact_id"],
        version=1,
        recipient_id="main",
        request_id="pin-original-grant",
    )
    selected = await f.engine.personal_artifacts.get(
        actor_id="main",
        artifact_id=source["artifact_id"],
        version=1,
    )
    routine = await f.service.routine(sources=[f.engine.personal_artifacts.reference(selected)])
    publish = f.engine.personal_artifacts.publish
    accepted = []

    async def revoke_after_publication(**kwargs):
        snapshot = await publish(**kwargs)
        accepted.append(snapshot)
        await f.engine.personal_artifacts.revoke(
            actor_id=f.profile.id,
            grant_id=grant["grant_id"],
            expected_revision=grant["revision"],
            request_id="revoke-during-pin",
        )
        return snapshot

    monkeypatch.setattr(f.engine.personal_artifacts, "publish", revoke_after_publication)
    with pytest.raises(PersonalAgentError):
        await f.service.pin_plan_read(f.service.public(routine), "main")
    assert len(accepted) == 1


async def test_explicit_artifact_sharing_does_not_require_routine_management_scope(authority):
    f = authority
    routine = await f.service.routine()
    occurrence = await f.service.reserve(routine)
    guard, _ = await f.service.create_occurrence_guard(occurrence, routine)
    with pytest.raises(PersonalAgentError):
        await f.service._routine(routine["id"], f.profile.id)
    await f.engine.personal_artifacts.share(
        actor_id="main",
        artifact_id=guard["artifact_id"],
        version=guard["version"],
        recipient_id=f.profile.id,
        request_id="explicit-guard-share",
    )
    _, content = await f.engine.personal_artifacts.read(
        actor_id=f.profile.id,
        artifact_id=guard["artifact_id"],
        version=guard["version"],
    )
    assert json.loads(content)["routine_ref"] == f.service._occurrence_reference(occurrence)


async def test_missing_routine_component_blocks_guard_through_actual_artifact_service(authority):
    f = authority
    routine = await f.service.routine()
    occurrence = await f.service.reserve(routine)
    guard, _ = await f.service.create_occurrence_guard(occurrence, routine)
    f.engine.personal_routines = None
    with pytest.raises(PersonalAgentError) as exc:
        await f.engine.personal_artifacts.read(
            actor_id="main",
            artifact_id=guard["artifact_id"],
            version=guard["version"],
        )
    assert exc.value.code == "routine_source_unavailable"


async def test_actor_version_change_during_plan_pin_does_not_return_body(authority, monkeypatch):
    f = authority
    routine = await f.service.routine(actor=f.profile.id, origin=f.profile.id)
    opened = await f.engine.personal_agents.open_session(f.profile.id)
    f.engine._session = await f.engine.session_store.load(opened["session_id"])
    publish = f.engine.personal_artifacts.publish

    async def change_after_publication(**kwargs):
        snapshot = await publish(**kwargs)
        f.engine.personal_agents.update(
            f.profile.id, f.profile.version, instructions="读取期间变化的版本"
        )
        return snapshot

    monkeypatch.setattr(f.engine.personal_artifacts, "publish", change_after_publication)
    with pytest.raises(PersonalAgentError) as exc:
        await f.service.pin_plan_read(f.service.public(routine), f.profile.id)
    assert exc.value.code in {"binding_conflict", "version_conflict"}
