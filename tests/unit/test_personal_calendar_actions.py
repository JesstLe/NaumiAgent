from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.daemons.agent_jobs import AgentJobStore
from naumi_agent.memory.session import SessionStore
from naumi_agent.personal.calendar_actions import CalendarActionMixin
from naumi_agent.personal.connection_models import source_ref
from naumi_agent.personal.connection_store import ConnectionStore
from naumi_agent.personal.connections import ConnectionAuthority
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.runs.store import ChatRunStore

ACTOR = "pa_" + "b" * 32
CONNECTION = "pc_" + "c" * 32


class FixtureService(CalendarActionMixin, ConnectionAuthority):
    async def capture_source(self, ref, actor_id, *, allow_manual_rebind=False):
        connection = await self._connection(ref["connection_id"], actor_id)
        grant = self._grant(connection, actor_id)
        if ref.get("grant_id", "") != grant["id"] and not allow_manual_rebind:
            raise PersonalAgentError("scope_denied", "来源未分享给当前 Agent")
        result = {**source_ref(ref), "grant_id": grant["id"], "grant_revision": grant["revision"]}
        await self.authorize_sources([result], actor_id)
        return result

    async def authorize_sources(self, refs, actor_id):
        for ref in refs:
            connection = await self._connection(ref["connection_id"], actor_id)
            grant = self._grant(connection, actor_id)
            if (
                self.source_revoked
                or ref["auth_epoch"] != connection["auth_epoch"]
                or ref["revision"] != self.source_revision
                or ref.get("grant_id", "") != grant["id"]
                or ref.get("grant_revision", 0) != grant["revision"]
            ):
                raise PersonalAgentError("source_unavailable", "原日程来源版本或分享已变化")

    async def token_snapshot(self, identity, actor_id="main", *, write=False):
        connection = await self._connection(identity, actor_id, write=write)
        if self.token_entered is not None:
            self.token_entered.set()
            await self.token_release.wait()
        return await self._bind_token("fixture-access", connection, actor_id, write=write)


@pytest.fixture
async def action_env(tmp_path):
    base, workspace = tmp_path / "personal", tmp_path / "workspace"
    base.mkdir()
    workspace.mkdir()
    clock = [datetime.now(UTC)]
    versions = {ACTOR: 1}
    registry = SimpleNamespace(db_path=base / "catalog.db", base_dir=base)
    registry.register_execution_session = lambda *_, **__: None
    personal = SimpleNamespace(
        store=registry,
        workspace_key="d" * 64,
        workspace_root=str(workspace),
        session_db_key="e" * 64,
        _executable=lambda actor: SimpleNamespace(version=versions[actor]),
    )
    memory = MemoryConfig(session_db_path=str(tmp_path / "sessions.db"))
    sessions = SessionStore(memory)
    jobs = AgentJobStore(
        tmp_path / "jobs.db", key_provider=lambda: b"q" * 32, clock=lambda: clock[0]
    )
    engine = SimpleNamespace(
        personal_agents=personal,
        workspace_root=workspace,
        config=SimpleNamespace(
            memory=memory,
            safety=SimpleNamespace(
                permission_mode="bypass", max_parallel_agents=1, max_queued_agents=10
            ),
        ),
        personal_collaborations=SimpleNamespace(jobs=jobs),
        session_store=sessions,
        chat_run_store=ChatRunStore(tmp_path / "runs.db"),
        _session="do-not-switch-active-session",
    )
    service = FixtureService(
        engine,
        store=ConnectionStore(tmp_path / "connections", owner_key="a" * 64),
        vault=object(),
        clock=lambda: clock[0],
    )
    service.source_revision = 1
    service.source_revoked = False
    service.token_entered = service.token_release = None
    service._closing = True

    class NoProvider:
        def __init__(self):
            self.calls = []

        async def insert_event(self, *args, **kwargs):
            self.calls.append("POST")
            raise AssertionError("Authority fixtures never access a provider")

        async def update_event(self, *args, **kwargs):
            self.calls.append("PATCH")
            raise AssertionError("Authority fixtures never access a provider")

        async def get_event(self, *args, **kwargs):
            self.calls.append("GET")
            raise AssertionError("Authority fixtures never access a provider")

    service.adapter = NoProvider()
    await service._write(
        lambda tx: tx.put(
            "connection",
            {
                "id": CONNECTION,
                "status": "connected",
                "issuer": "https://accounts.google.com",
                "subject": "fixture-stable-sub",
                "email": "fixture@example.invalid",
                "email_verified": True,
                "auth_epoch": 1,
                "token_generation": 1,
                "client_id_digest": "1" * 64,
                "can_write": True,
                "scopes": ["openid", "email", "owned"],
                "grants": {
                    personal.workspace_key + ":" + ACTOR: {
                        "id": "fixture-grant",
                        "revision": 1,
                        "actor_id": ACTOR,
                        "actor_version": 1,
                        "workspace_key": personal.workspace_key,
                        "auth_epoch": 1,
                        "state": "active",
                    }
                },
            },
            expected_revision=0,
        )
    )
    try:
        yield SimpleNamespace(service=service, engine=engine, clock=clock, versions=versions)
    finally:
        await service.stop_actions()
        await sessions.close()


def source():
    return {
        "connection_id": CONNECTION,
        "auth_epoch": 1,
        "event_id": "source_event",
        "revision": 1,
        "payload_sha256": "f" * 64,
        "occurrence_key": "event:source_event",
        "grant_id": "",
        "grant_revision": 0,
    }


def proposal(env, **changes):
    value = {
        "request_id": uuid.uuid4().hex,
        "connection_id": CONNECTION,
        "actor_id": "main",
        "source_refs": [source()],
        "operation": "insert",
        "body": {
            "summary": "准备材料",
            "description": "只根据已批准材料安排",
            "start": {"dateTime": "2026-11-06T09:00:00+08:00", "timeZone": "Asia/Shanghai"},
            "end": {"dateTime": "2026-11-06T10:00:00+08:00", "timeZone": "Asia/Shanghai"},
        },
        "expires_at": (env.clock[0] + timedelta(hours=1)).isoformat(),
    }
    return {**value, **changes}


async def prepare(env, **changes):
    return await env.service.prepare_action(proposal(env, **changes))


async def approve(env, action, **changes):
    payload = {
        "request_id": uuid.uuid4().hex,
        "expected_revision": action["revision"],
        "expected_digest": action["digest"],
        **changes,
    }
    return await env.service.approve_action(action["id"], payload, trusted_channel="manual_api")


async def settle(env):
    executor = env.service._calendar_action_executor()
    await env.service.action_tick()
    if executor.active:
        results = await __import__("asyncio").gather(
            *executor.active.values(), return_exceptions=True
        )
        assert not [result for result in results if isinstance(result, BaseException)], results
    await env.service.action_tick()


@pytest.mark.asyncio
async def test_draft_approval_exact_intent_and_public_privacy(action_env):
    env = action_env
    draft = await prepare(env)
    assert draft["status"] == "draft" and draft["event_id"]
    approved = await approve(env, draft)
    assert approved["approval"]["digest"] == draft["digest"]
    internal = await env.service._record("action", draft["id"])
    assert internal["execution"]["admission"] == "prepared"
    assert (
        await env.engine.personal_collaborations.jobs.get(internal["execution"]["job_id"]) is None
    )
    assert not {"issuer", "subject", "execution", "grant"} & set(approved)
    assert "extendedProperties" not in approved["body"]
    assert env.engine._session == "do-not-switch-active-session"
    assert approved["last_error_code"] is None
    await env.service._write(
        lambda tx: env.service._change(
            tx, "action", draft["id"], {"last_error_code": "raw secret=fixture-token"}
        )
    )
    safe = await env.service.get_action(draft["id"])
    assert safe["last_error_code"] == "action_error" and "fixture-token" not in str(safe)


@pytest.mark.asyncio
async def test_duplicate_prepare_and_approve_after_expiry_do_not_reissue(action_env):
    env = action_env
    payload = proposal(env)
    draft = await env.service.prepare_action(payload)
    approval = {
        "request_id": uuid.uuid4().hex,
        "expected_revision": draft["revision"],
        "expected_digest": draft["digest"],
    }
    first = await env.service.approve_action(draft["id"], approval, trusted_channel="manual_api")
    env.clock[0] += timedelta(days=2)
    assert (await env.service.prepare_action(payload))["id"] == draft["id"]
    assert (await env.service.approve_action(draft["id"], approval, trusted_channel="manual_api"))[
        "approval"
    ] == first["approval"]
    altered = copy.deepcopy(payload)
    altered["body"]["summary"] = "不同正文"
    with pytest.raises(PersonalAgentError) as caught:
        await env.service.prepare_action(altered)
    assert caught.value.code == "idempotency_conflict"


@pytest.mark.parametrize(
    "case",
    ["digest", "revision", "channel", "readonly", "source", "epoch", "actor_version", "grant"],
)
@pytest.mark.asyncio
async def test_approval_rejects_stale_identity_scope_source_and_human_version(action_env, case):
    env = action_env
    draft = await prepare(env, actor_id=ACTOR)
    payload = {
        "request_id": uuid.uuid4().hex,
        "expected_revision": draft["revision"],
        "expected_digest": draft["digest"],
    }
    channel = "manual_api"
    if case == "digest":
        payload["expected_digest"] = "0" * 64
    elif case == "revision":
        payload["expected_revision"] += 1
    elif case == "channel":
        channel = "agent_tool"
    elif case == "source":
        env.service.source_revoked = True
    elif case == "actor_version":
        env.versions[ACTOR] = 2
    else:

        def change(tx):
            value = tx.get("connection", CONNECTION)
            if case == "readonly":
                value["can_write"] = False
            elif case == "epoch":
                value["auth_epoch"] += 1
            else:
                value["grants"][env.service.workspace_key + ":" + ACTOR]["revision"] += 1
            tx.put("connection", value, expected_revision=value["revision"])

        await env.service._write(change)
    with pytest.raises(PersonalAgentError):
        await env.service.approve_action(draft["id"], payload, trusted_channel=channel)
    assert (await env.service._record("action", draft["id"]))["approval"] is None


@pytest.mark.asyncio
async def test_model_scope_main_is_not_manual_owner_and_bound_actor_default(action_env):
    env = action_env
    other = await prepare(env, actor_id=ACTOR)
    assert (await env.service.list_actions())["actions"][0]["id"] == other["id"]
    assert (await env.service.list_actions(caller_actor="main"))["actions"] == []
    with pytest.raises(PersonalAgentError):
        await env.service.get_action(other["id"], caller_actor="main")
    payload = proposal(env, source_refs=[])
    del payload["actor_id"]
    own = await env.service.prepare_action(payload, caller_actor=ACTOR)
    assert own["actor_id"] == ACTOR
    with pytest.raises(PersonalAgentError):
        await env.service.prepare_action(proposal(env, actor_id="main"), caller_actor=ACTOR)


@pytest.mark.asyncio
async def test_revise_rotates_marker_digest_and_locks_old_approval(action_env):
    env = action_env
    draft = await prepare(env)
    raw = proposal(env)
    raw["body"]["summary"] = "重新核对后的正文"
    revised = await env.service.revise_action(
        draft["id"],
        {
            "request_id": uuid.uuid4().hex,
            "expected_revision": draft["revision"],
            "body": raw["body"],
            "expires_at": raw["expires_at"],
        },
    )
    assert revised["digest"] != draft["digest"] and revised["event_id"] == draft["event_id"]
    with pytest.raises(PersonalAgentError):
        await approve(env, revised, expected_digest=draft["digest"])
    approved = await approve(env, revised)
    with pytest.raises(PersonalAgentError):
        await env.service.revise_action(
            approved["id"],
            {
                "request_id": uuid.uuid4().hex,
                "expected_revision": approved["revision"],
                "body": raw["body"],
                "expires_at": raw["expires_at"],
            },
        )


@pytest.mark.parametrize("hours", [-1, 25])
@pytest.mark.asyncio
async def test_expiry_boundaries_and_update_requires_exact_source(action_env, hours):
    env = action_env
    with pytest.raises(PersonalAgentError):
        await prepare(env, expires_at=(env.clock[0] + timedelta(hours=hours)).isoformat())
    with pytest.raises(PersonalAgentError):
        await prepare(env, operation="update", target_event_id="not_in_sources", base_etag='"old"')


@pytest.mark.parametrize(
    "bad_field", ["attendees", "attachments", "calendarId", "extendedProperties"]
)
@pytest.mark.asyncio
async def test_unapproved_fields_and_client_markers_rejected(action_env, bad_field):
    payload = proposal(action_env)
    payload["body"][bad_field] = [] if bad_field != "calendarId" else "other-calendar"
    with pytest.raises(PersonalAgentError):
        await action_env.service.prepare_action(payload)


def private_context(env):
    artifact = {
        "artifact_id": "art_" + "1" * 32,
        "version": 1,
        "sha256": "2" * 64,
        "grant_id": "",
        "grant_revision": 0,
    }
    memory = {
        "memory_id": "pm_" + "3" * 32,
        "revision": 1,
        "content_sha256": "4" * 64,
        "grant_id": "",
        "grant_revision": 0,
    }
    state = SimpleNamespace(
        artifact_revoked=False, memory_revoked=False, session_refs=[artifact], memory_calls=[]
    )

    class Artifacts:
        enabled = True

        async def session_refs(self, *_):
            return copy.deepcopy(state.session_refs)

        async def authorize_refs(self, *, actor_id, refs):
            if state.artifact_revoked and refs:
                raise PersonalAgentError("artifact_access_revoked", "原私有资料已撤销")
            return refs

    class Memories:
        async def resolve_ref(self, ref, actor, *, item_id=None):
            state.memory_calls.append((copy.deepcopy(ref), actor, item_id))
            if state.memory_revoked:
                raise PersonalAgentError("memory_access_revoked", "原记忆已纠正或删除")
            return {}, None

    env.engine.personal_artifacts = Artifacts()
    env.engine.personal_memory = Memories()
    env.engine.personal_memory_runtime = SimpleNamespace(item_id=lambda: None)
    env.engine._session = SimpleNamespace(id="private-session", personal_agent_id="")
    env.engine._personal_artifact_refs = [artifact]
    env.engine._personal_memory_artifact_refs = [artifact]
    env.engine._personal_memory_runtime_refs = [memory]
    return artifact, memory, state


@pytest.mark.asyncio
async def test_implicit_private_sources_cannot_be_omitted_from_model_action_digest(action_env):
    env = action_env
    artifact, memory, _ = private_context(env)
    action = await env.service.prepare_action(proposal(env, source_refs=[]), caller_actor="main")
    assert action["source_refs"] == []
    assert action["artifact_refs"] == [artifact] and action["memory_refs"] == [memory]
    await approve(env, action)
    internal = await env.service._record("action", action["id"])
    context = __import__("json").loads(internal["execution"]["payload"]["context"])
    assert context["artifact_refs"] == [artifact] and context["memory_refs"] == [memory]


@pytest.mark.parametrize("dependency", ["artifact", "memory"])
@pytest.mark.asyncio
async def test_implicit_private_source_revocation_after_approval_fences_external_write(
    action_env, dependency
):
    env = action_env
    _, _, state = private_context(env)
    draft = await env.service.prepare_action(proposal(env, source_refs=[]), caller_actor="main")
    await approve(env, draft)
    setattr(state, dependency + "_revoked", True)
    env.service._closing = False
    await settle(env)
    assert (await env.service.get_action(draft["id"]))["status"] == "cancelled"
    assert env.service.adapter.calls == []
    with pytest.raises(PersonalAgentError):
        await env.service.get_action(draft["id"], caller_actor="main")


@pytest.mark.asyncio
async def test_revise_cannot_drop_old_private_dependencies_when_context_query_misses(action_env):
    env = action_env
    artifact, memory, state = private_context(env)
    draft = await env.service.prepare_action(proposal(env, source_refs=[]), caller_actor="main")
    env.engine._personal_artifact_refs = []
    env.engine._personal_memory_artifact_refs = []
    env.engine._personal_memory_runtime_refs = []
    state.session_refs = []
    env.engine._session = SimpleNamespace(id="new-empty-session", personal_agent_id="")
    payload = proposal(env)
    revised = await env.service.revise_action(
        draft["id"],
        {
            "request_id": uuid.uuid4().hex,
            "expected_revision": draft["revision"],
            "body": payload["body"],
            "expires_at": payload["expires_at"],
        },
        caller_actor="main",
    )
    assert revised["artifact_refs"] == [artifact] and revised["memory_refs"] == [memory]
    state.memory_revoked = True
    with pytest.raises(PersonalAgentError):
        await approve(env, revised)


@pytest.mark.asyncio
async def test_manual_action_ignores_unrelated_active_model_private_context(action_env):
    env = action_env
    private_context(env)
    action = await env.service.prepare_action(proposal(env, source_refs=[]))
    assert action["artifact_refs"] == [] and action["memory_refs"] == []
    with pytest.raises(PersonalAgentError):
        await env.service.prepare_action(
            {**proposal(env), "artifact_refs": []}, caller_actor="main"
        )


@pytest.mark.asyncio
async def test_concurrent_identical_human_approvals_issue_one_exact_intent(action_env):
    env = action_env
    draft = await prepare(env)
    payload = {
        "request_id": uuid.uuid4().hex,
        "expected_revision": draft["revision"],
        "expected_digest": draft["digest"],
    }
    first, second = await asyncio.gather(
        *(
            env.service.approve_action(draft["id"], payload, trusted_channel="manual_api")
            for _ in range(2)
        )
    )
    assert first["approval"] == second["approval"]
    assert (await env.service._record("action", draft["id"]))["execution"][
        "admission"
    ] == "prepared"
