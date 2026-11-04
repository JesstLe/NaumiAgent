"""Actual store/scheduler/artifact service contracts with a controlled execution port."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from naumi_agent.config.settings import AppConfig
from naumi_agent.daemons.agent_jobs import AgentJobPayload
from naumi_agent.daemons.agent_worker_contract import issue_agent_worker_request
from naumi_agent.memory.session import SessionStore
from naumi_agent.personal.artifacts import PersonalArtifactService
from naumi_agent.personal.item_inputs import _files, normalize_directories, resolve_schedule
from naumi_agent.personal.item_models import digest
from naumi_agent.personal.items import PersonalItemService
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.service import PersonalAgentService
from naumi_agent.personal.store import PersonalAgentStore
from naumi_agent.scheduler.models import ScheduleTarget
from naumi_agent.scheduler.runner import SchedulerRunner
from naumi_agent.scheduler.store import SchedulerStore
from naumi_agent.tools.base import Tool, ToolRegistry


class Capability(Tool):
    name = "test-capability"
    description = "测试能力"
    parameters_schema = {"type": "object"}

    def __init__(self, name):
        self.name = name

    async def execute(self, **kwargs):
        raise AssertionError("The service must not directly execute tools")


class ExecutionPort:
    """Explicit controlled port, not a claim of real provider/Job acceptance."""

    def __init__(self):
        self.views, self.submissions, self.reconciliations = {}, [], []

    def prepare(self, item, **fields):
        policy = item["definition"]["policy"]
        actor = item["definition"]["assignee"]
        payload = AgentJobPayload(
            task_id=fields["occurrence_id"],
            session_id=fields["execution_session_id"],
            task=fields["task"],
            context=fields["context"],
            message_topic="item:" + item["once_key"],
        )
        request = issue_agent_worker_request(
            task_id=payload.task_id,
            session_id=payload.session_id,
            agent_name=actor["id"],
            task=payload.task,
            context=payload.context,
            message_topic=payload.message_topic,
            tool_scope=policy["tool_names"],
            permission_mode="bypass",
            model_tier=policy["model_tier"],
            max_turns=policy["max_turns"],
            max_budget_usd=policy["max_cost_microusd"] / 1000000,
            timeout_seconds=policy["timeout_seconds"],
            issued_at="2030-01-01T00:00:00+00:00",
        )
        value = {
            "occurrence_id": fields["occurrence_id"],
            "item_id": item["item_id"],
            "item_revision": item["revision"],
            "once_key": item["once_key"],
            "definition_sha256": item["definition_sha256"],
            "workspace_key": item["definition"]["route"]["workspace_key"],
            "schedule_id": fields["schedule_id"],
            "event_id": fields["event_id"],
            "execution_session_id": payload.session_id,
            "task_id": payload.task_id,
            "actor_id": actor["id"],
            "agent_version": actor["version"],
            "input_refs": fields["input_refs"],
            "input_manifest": fields["input_manifest"],
            "policy": policy,
            "main_policy_sha256": "c" * 64 if actor["id"] == "main" else "",
            "request": asdict(request),
            "payload": asdict(payload),
            "issued_at": request.issued_at,
        }
        value = json.loads(json.dumps(value))
        value["prepared_sha256"] = digest(value)
        return value

    async def view(self, intent):
        return {
            "occurrence_id": intent["occurrence_id"],
            "status": "prepared",
            "job_id": "",
            "execution_session_id": intent["execution_session_id"],
            "run_id": "",
            "artifacts": [],
            **self.views.get(intent["occurrence_id"], {}),
        }

    async def submit(self, intent):
        self.submissions.append(intent["occurrence_id"])
        return await self.view(intent)

    async def reconcile(self, intent):
        self.reconciliations.append(intent["occurrence_id"])
        return await self.view(intent)

    async def cancel(self, intent):
        self.views[intent["occurrence_id"]] = {"status": "cancelled"}
        return await self.view(intent)

    def start(self):
        pass

    async def stop(self):
        pass


@pytest.fixture
async def system(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AppConfig(
        workspace_root=str(workspace),
        personal={"data_dir": str(tmp_path / "personal")},
        memory={
            "session_db_path": str(tmp_path / "runtime" / "sessions.db"),
            "long_term_enabled": False,
        },
        safety={"permission_mode": "bypass"},
    )
    sessions, registry = SessionStore(config.memory), ToolRegistry()
    for name in ("personal_artifacts", "code_execute", "file_read"):
        registry.register(Capability(name))
    clock = [datetime(2030, 1, 1, tzinfo=UTC)]
    engine = SimpleNamespace(
        config=config,
        workspace_root=workspace,
        session_store=sessions,
        tool_registry=registry,
    )
    engine.personal_agents = PersonalAgentService(
        engine, PersonalAgentStore(config.personal.data_dir)
    )
    engine.personal_artifacts = PersonalArtifactService(engine)
    engine.scheduler_runner = SchedulerRunner(
        SchedulerStore(tmp_path / "scheduler"),
        now_fn=lambda: clock[0],
    )
    port = ExecutionPort()
    service = PersonalItemService(engine, executor=port, clock=lambda: clock[0])
    yield service, engine, port, clock
    await service.stop()
    await sessions.close()


def payload(**updates):
    return {
        "request_id": "original-create",
        "title": "整理实际资料",
        "instructions": "保留来源并报告",
        "schedule": {"mode": "now"},
        **updates,
    }


async def dispatch(service, engine):
    engine.scheduler_runner.tick_due()
    await service.tick()


async def test_create_now_retry_preserves_time_and_has_no_presets(system):
    service, engine, _, clock = system
    first = await service.create(payload())
    clock[0] += timedelta(hours=1)
    retry = await service.create(payload())
    assert first == retry
    assert engine.personal_agents.list() == ()
    assert len((await service.list())["items"]) == 1
    assert len(engine.scheduler_runner.store.list_jobs()) == 1
    with pytest.raises(PersonalAgentError, match="另一项操作"):
        await service.create(payload(title="changed"))


async def test_parallel_create_returns_durable_winner(system):
    service, engine, _, _ = system
    values = await asyncio.gather(*(service.create(payload()) for _ in range(12)))
    assert len({v["item_id"] for v in values}) == 1
    assert len(engine.scheduler_runner.store.list_jobs()) == 1


async def test_pause_resume_and_original_control_replay(system):
    service, engine, port, _ = system
    item = await service.create(payload())
    control = {"expected_revision": 1, "request_id": "pause-once"}
    paused = await service.control(item["item_id"], "pause", control)
    await dispatch(service, engine)
    assert port.submissions == [] and paused["state"] == "paused"
    assert paused["once_key"] == item["once_key"]
    resumed = await service.control(
        item["item_id"],
        "resume",
        {
            "expected_revision": 2,
            "request_id": "resume-once",
        },
    )
    replay = await service.control(item["item_id"], "pause", control)
    assert replay == resumed
    await dispatch(service, engine)
    assert len(set(port.submissions)) == 1


async def test_update_now_and_restart_retries_do_not_resample_time(system):
    service, engine, _, clock = system
    item = await service.create(payload())
    update = payload(request_id="update-one", expected_revision=1, title="新事项")
    clock[0] += timedelta(seconds=3)
    changed = await service.update(item["item_id"], update)
    clock[0] += timedelta(minutes=5)
    assert await service.update(item["item_id"], update) == changed
    assert engine.scheduler_runner.tick_due() == 1
    closed = await service.control(
        item["item_id"],
        "close",
        {
            "expected_revision": changed["revision"],
            "request_id": "manual-close",
        },
    )
    command = {"expected_revision": closed["revision"], "request_id": "restart-original"}
    restarted = await service.control(item["item_id"], "restart", command)
    clock[0] += timedelta(minutes=5)
    assert await service.control(item["item_id"], "restart", command) == restarted
    assert restarted["once_key"] != closed["once_key"]


async def test_original_run_now_after_restart_does_not_fire_new_once(system):
    service, engine, _, clock = system
    future = {"mode": "once", "local_time": "2030-01-03T08:00:00", "timezone": "Asia/Shanghai"}
    item = await service.create(payload(schedule=future))
    command = {"expected_revision": 1, "request_id": "run-now-original"}
    await service.control(item["item_id"], "run-now", command)
    cancelled = await service.control(
        item["item_id"],
        "cancel",
        {
            "expected_revision": 1,
            "request_id": "cancel-original",
        },
    )
    restarted = await service.control(
        item["item_id"],
        "restart",
        {
            "expected_revision": cancelled["revision"],
            "request_id": "explicit-restart",
            "schedule": future,
        },
    )
    await service.control(item["item_id"], "run-now", command)
    new_id, _, _ = service.schedule_identity(restarted)
    assert engine.scheduler_runner.get(new_id).fired_count == 0
    assert len(engine.scheduler_runner.store.pending_events()) == 1
    assert clock[0] < datetime.fromisoformat(restarted["definition"]["schedule"]["due_at"])


async def test_event_consumed_only_after_durable_intent_and_recovers_without_prepare(system):
    service, engine, port, _ = system
    item = await service.create(payload())
    await dispatch(service, engine)
    first = await service.get(item["item_id"])
    assert first["occurrence"]["status"] == "prepared"
    assert not engine.scheduler_runner.store.pending_events(target=ScheduleTarget.PERSONAL_ITEM)
    again = PersonalItemService(engine, executor=port, clock=service.clock)
    await again.tick()
    assert (await again.get(item["item_id"])) == first
    assert len(await again._intents()) == 1
    assert port.submissions == [first["occurrence"]["occurrence_id"]]


async def test_misfire_and_resume_preserve_explicit_window(system):
    service, engine, port, clock = system
    item = await service.create(payload(schedule={"mode": "now", "grace_seconds": 1}))
    clock[0] += timedelta(seconds=10)
    await dispatch(service, engine)
    value = await service.get(item["item_id"])
    assert value["state"] == "closed" and value["occurrence"]["status"] == "skipped"
    assert value["occurrence"]["receipt"]["decision"] == "expired"
    assert not port.submissions
    assert not engine.scheduler_runner.store.pending_events()


async def test_real_directory_capture_bytes_manifest_and_input_error_once(system):
    service, engine, port, _ = system
    root = engine.workspace_root / "inputs"
    root.mkdir()
    (root / "sales.csv").write_text("金额\n36\n64\n", encoding="utf-8")
    item = await service.create(payload(directories=[{"path": str(root)}]))
    await dispatch(service, engine)
    intent = (await service._intents())[0]
    assert len(intent["input_refs"]) == 1
    assert intent["input_manifest"][0]["relative_path"] == "sales.csv"
    assert "36" in intent["payload"]["context"]
    assert len(port.submissions) == 1
    other = await service.create(
        payload(request_id="missing-dir", directories=[{"path": str(root)}])
    )
    (root / "sales.csv").unlink()
    root.rmdir()
    await dispatch(service, engine)
    failed = await service.get(other["item_id"])
    assert failed["state"] == "closed" and failed["occurrence"]["status"] == "error"
    assert failed["occurrence"]["receipt"]["decision"] == "input-error"
    await dispatch(service, engine)
    assert len(port.submissions) == 1
    assert item["item_id"] != other["item_id"]


async def test_loss_of_whole_store_does_not_create_replacement_on_restart(system, tmp_path):
    service, engine, port, _ = system
    await service.create(payload())
    assert service.witness.is_file()
    target = service.store.base_dir.resolve()
    assert target.is_relative_to(tmp_path.resolve())
    shutil.rmtree(target)
    restarted = PersonalItemService(engine, executor=port, clock=service.clock)
    with pytest.raises(PersonalAgentError) as exc:
        await restarted.create(payload(request_id="new-request"))
    assert exc.value.code == "storage_error"
    assert not target.exists()


async def test_model_actor_privacy_and_no_cross_agent_management(system):
    service, engine, _, _ = system
    profile = engine.personal_agents.create(
        request_id="agent-a",
        name="Echo",
        instructions="按要求整理",
        tool_names=[],
    )
    other = engine.personal_agents.create(
        request_id="agent-b",
        name="Nova",
        instructions="按要求整理",
        tool_names=[],
    )
    item = await service.create(payload(assignee_id=profile.id, assignee_version=profile.version))
    assert (await service.list(caller_actor="main"))["items"] == []
    assert len((await service.list(caller_actor=profile.id))["items"]) == 1
    with pytest.raises(PersonalAgentError) as exc:
        await service.get(item["item_id"], caller_actor="main")
    assert exc.value.code == "not_found"
    with pytest.raises(PersonalAgentError) as exc:
        await service.create(
            payload(request_id="cross", assignee_id=other.id, assignee_version=other.version),
            caller_actor=profile.id,
        )
    assert exc.value.code == "scope_denied"


async def test_unknown_and_admitted_cannot_pause_or_restart(system):
    service, engine, port, _ = system
    item = await service.create(payload())
    await dispatch(service, engine)
    intent = (await service._intents())[0]
    port.views[intent["occurrence_id"]] = {"status": "queued", "job_id": "controlled-admission"}
    with pytest.raises(PersonalAgentError) as exc:
        await service.control(
            item["item_id"], "pause", {"request_id": "pause", "expected_revision": 1}
        )
    assert exc.value.code == "lifecycle_conflict"
    port.views[intent["occurrence_id"]] = {"status": "unknown"}
    for action in ("restart", "close"):
        with pytest.raises(PersonalAgentError) as exc:
            await service.control(
                item["item_id"],
                action,
                {
                    "request_id": "unknown-" + action,
                    "expected_revision": 1,
                },
            )
        assert exc.value.code == "lifecycle_conflict"


@pytest.mark.parametrize(
    "zone,local,fold,code",
    [
        ("America/New_York", "2030-03-10T02:30:00", None, "nonexistent_time"),
        ("America/New_York", "2030-11-03T01:30:00", None, "ambiguous_time"),
        ("Not/AZone", "2030-01-01T01:00:00", None, "invalid_timezone"),
    ],
)
def test_real_iana_resolution_errors(zone, local, fold, code):
    with pytest.raises(PersonalAgentError) as exc:
        resolve_schedule(
            {"mode": "once", "local_time": local, "timezone": zone, "fold": fold},
            datetime(2030, 1, 1, tzinfo=UTC),
        )
    assert exc.value.code == code


async def test_private_directory_and_hardlink_rejected_before_bytes(system, tmp_path):
    _, engine, _, _ = system
    private = engine.workspace_root / ".naumi" / "vault"
    private.mkdir(parents=True)
    with pytest.raises(PersonalAgentError):
        normalize_directories(engine, [{"path": str(private)}])
    directory = engine.workspace_root / "docs"
    directory.mkdir()
    source = tmp_path / "original.txt"
    source.write_text("outside", encoding="utf-8")
    os.link(source, directory / "alias.txt")
    with pytest.raises(PersonalAgentError) as exc:
        _files(engine, normalize_directories(engine, [{"path": str(directory)}]))
    assert exc.value.code == "path_denied"


async def test_paused_pending_page_does_not_starve_later_active_event(system, monkeypatch):
    service, engine, _, _ = system
    database = engine.scheduler_runner.store.db_path
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO schedule_events "
            "(id,schedule_id,fired_at,prompt,target,delivered,scope_key,binding_sha256) "
            "VALUES (?,?,?,?,?,0,?,?)",
            [
                (
                    f"event-{index:04d}",
                    f"managed-test-{index}",
                    "2030-01-01T00:00:00+00:00",
                    "{}",
                    "personal_item",
                    service.workspace_key,
                    "b" * 64,
                )
                for index in range(1002)
            ],
        )
    seen = []

    async def consume(event):
        seen.append(event.id)
        if event.id == "event-1001":
            service._consume(event)

    monkeypatch.setattr(service, "_consume_event", consume)
    await service.tick()
    assert len(seen) == 1002
    assert engine.scheduler_runner.store.get_event("event-1001").delivered
    assert not engine.scheduler_runner.store.get_event("event-0000").delivered


async def test_original_decision_recovers_after_close_before_event_consumption(system, monkeypatch):
    service, engine, port, _ = system
    root = engine.workspace_root / "source"
    root.mkdir()
    item = await service.create(payload(directories=[{"path": str(root)}]))
    root.rmdir()
    engine.scheduler_runner.tick_due()
    original = service._consume
    monkeypatch.setattr(
        service, "_consume", lambda event: (_ for _ in ()).throw(RuntimeError("cut"))
    )
    with pytest.raises(RuntimeError, match="cut"):
        await service.tick()
    assert (await service.get(item["item_id"]))["state"] == "closed"
    assert len(engine.scheduler_runner.store.pending_events()) == 1
    monkeypatch.setattr(service, "_consume", original)
    await service.tick()
    assert not port.submissions
    assert not engine.scheduler_runner.store.pending_events()
    assert (await service.get(item["item_id"]))["occurrence"]["receipt"][
        "decision"
    ] == "input-error"
