"""Real research/approval stores and Tool boundaries; only outbound transports are fixtures.

No model or credential backend is used. These tests exercise exact human approval,
live source authority, one-shot dispatch and original-result recovery independently
of semantic research quality and independently of live public-provider acceptance.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import keyring
import pytest

from naumi_agent.config.settings import AppConfig, ModelConfig
from naumi_agent.model.router import ModelRouter
from naumi_agent.personal.artifact_boundary import ArtifactToolProxy
from naumi_agent.personal.memory_models import reference as memory_reference
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.research_tools import (
    execute_research_operation,
    validate_research_arguments,
)
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tools.base import ToolExecutionError

pytestmark = pytest.mark.usefixtures("runtime_payload_key")
PRIVATE = "PRIVATE_EGRESS_CANARY_7482"
BODY = "收入 USD 100；成本 USD 25。PUBLIC_CAPTURE_RESULT_51"
URL = "https://research.example.test/exact?purpose=approved"
WAIT = 60


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 6, 10, tzinfo=UTC)

    def __call__(self):
        return self.value


class OutboundFixture:
    """Observe only production service transport arguments and actual send count."""

    def __init__(self, clock):
        self.clock = clock
        self.captures, self.searches = [], []
        self.capture_entered, self.search_entered = asyncio.Event(), asyncio.Event()
        self.capture_release = self.search_release = None
        self.error = None
        self.body = BODY

    async def capture(self, url, **options):
        self.captures.append({"url": url, "options": copy.deepcopy(options)})
        self.capture_entered.set()
        if self.capture_release is not None:
            await self.capture_release.wait()
        if self.error is not None:
            raise self.error
        raw = self.body.encode("utf-8")
        checksum = hashlib.sha256(raw).hexdigest()
        return {
            "requested_url": url,
            "final_url": url,
            "title": "真实服务传输夹具原文",
            "body": self.body,
            "raw_sha256": checksum,
            "body_sha256": checksum,
            "http_status": 200,
            "captured_at": self.clock().isoformat(),
            "truncated": False,
            "content_type": "text/plain; charset=utf-8",
            "extractor": "explicit-transport-fixture-v1",
            "redirects": [],
        }

    async def search(self, tool, query, max_results=5, *, plan):
        self.searches.append(
            {
                "tool": tool.name,
                "query": query,
                "max_results": max_results,
                "plan": copy.deepcopy(plan),
            }
        )
        self.search_entered.set()
        if self.search_release is not None:
            await self.search_release.wait()
        if self.error is not None:
            raise self.error
        return {
            "query": query,
            "provider": plan["provider"],
            "status": "success",
            "code": "",
            "items": [{"title": "发现的候选", "url": URL, "snippet": "检索摘要并非正文"}],
            "observed_at": self.clock().isoformat(),
            "attempts": [{"provider": plan["provider"], "status": "success", "code": ""}],
            "destination_plan": copy.deepcopy(plan),
        }


def configuration(root):
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        workspace_root=str(workspace),
        personal={"data_dir": str(root / "personal")},
        memory={
            "session_db_path": str(root / "runtime/sessions.db"),
            "vector_db_path": str(root / "runtime/vectors"),
            "long_term_enabled": False,
        },
        models=ModelConfig(
            default_model="openai/gpt-4o-mini",
            fast_model="openai/gpt-4o-mini",
            reasoning_model="openai/gpt-4o-mini",
            api_key="egress-fixture-only",
            api_base="http://127.0.0.1:1/v1",
            max_tokens=64,
        ),
        search={
            "provider_order": ("duckduckgo", "brave"),
            "brave": {"enabled": True, "api_key_ref": "{env:NAUMI_EGRESS_FIXTURE_BRAVE_KEY}"},
        },
        safety={"permission_mode": "strict", "allowed_dirs": [str(workspace)]},
        skills={"search_paths": []},
    )


@pytest.fixture
async def egress(tmp_path, monkeypatch):
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("NAUMI_EGRESS_FIXTURE_BRAVE_KEY", "local-transport-fixture-key")
    provider_calls, credential_calls, engines = [], [], []

    async def no_provider(*_args, **_kwargs):
        provider_calls.append("call")
        raise AssertionError("Egress regression fixtures never call a model")

    async def no_stream(*_args, **_kwargs):
        provider_calls.append("stream")
        raise AssertionError("Egress regression fixtures never stream a model")
        yield

    def no_credential(*_args, **_kwargs):
        credential_calls.append("native credential")
        raise AssertionError("Egress regression fixtures never access native credentials")

    monkeypatch.setattr(ModelRouter, "call", no_provider)
    monkeypatch.setattr(ModelRouter, "stream", no_stream)
    for method in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, method, no_credential)
    clock = Clock()
    transport = OutboundFixture(clock)
    config = configuration(tmp_path)

    async def make(*, session_id=None, other_root=None):
        engine = create_agent_engine(
            configuration(other_root) if other_root else config.model_copy(deep=True)
        )
        engines.append(engine)
        engine.personal_research.clock = clock
        engine.personal_research.capture_transport = transport.capture
        engine.personal_research.approved_discovery_transport = transport.search
        if session_id is None:
            await engine.get_or_create_session(title="确切研究外发审批")
        else:
            assert await engine.load_session(session_id)
        return engine

    engine = await make()
    try:
        yield SimpleNamespace(
            engine=engine,
            service=engine.personal_research,
            root=tmp_path,
            config=config,
            clock=clock,
            transport=transport,
            make=make,
            monkeypatch=monkeypatch,
        )
    finally:
        for engine in reversed(engines):
            await engine.shutdown()
        assert provider_calls == [] and credential_calls == []


async def manual(context, operation, *, engine=None, **fields):
    current = engine or context.engine
    return await execute_research_operation(
        current.personal_research,
        {"operation": operation, **fields},
        trusted_channel="web",
    )


async def model(context, operation, *, engine=None, **fields):
    current = engine or context.engine
    tool = current.tool_registry.get("personal_research")
    proxy = ArtifactToolProxy(current, tool, "egress-fixture-" + operation)
    return json.loads(await proxy.execute(operation=operation, **fields))


async def notebook(context, *, engine=None, request_id="egress-notebook", title="原研究"):
    return await model(
        context,
        "create",
        engine=engine,
        request_id=request_id,
        title=title,
        question="按用户确切批准的内容获取外部来源",
        domain="general",
    )


async def private_material(
    context, *, owner="main", recipient=None, request_id="private-egress-input"
):
    artifacts = context.engine.personal_artifacts
    snapshot = await artifacts.publish(
        actor_id=owner,
        request_id=request_id,
        title=PRIVATE,
        kind="material",
        filename="private.txt",
        content=PRIVATE.encode(),
    )
    grant = None
    if recipient is not None:
        grant = await artifacts.share(
            actor_id=owner,
            artifact_id=snapshot["artifact_id"],
            version=snapshot["version"],
            recipient_id=recipient,
            request_id=request_id + "-share",
        )
        snapshot = await artifacts.get(
            actor_id=recipient, artifact_id=snapshot["artifact_id"], version=snapshot["version"]
        )
    ref = artifacts.reference(snapshot)
    await artifacts.bind_refs(context.engine._session.id, recipient or owner, [ref])
    return snapshot, ref, grant


def proposal(value):
    assert value["availability"] == "available"
    result = value["proposal"]
    assert result["id"].startswith("rx_") and len(result["id"]) == 35
    return result


async def propose(
    context, view, *, action="capture", request_id="original-egress-proposal", engine=None, **fields
):
    arguments = {
        "notebook_id": view["notebook"]["id"],
        "expected_notebook_revision": view["notebook"]["revision"],
        "request_id": request_id,
        "action": action,
        "reason": "获取与当前研究相关的确切外部资料",
        "expires_in_minutes": 30,
    }
    if action == "search":
        arguments.update(provider="duckduckgo", query=PRIVATE, max_results=2)
    elif action == "capture":
        arguments.update(url=URL, max_age_days=1)
    arguments.update(fields)
    return await model(context, "egress_propose", engine=engine, **arguments)


def control(value, request_id):
    frozen = proposal(value)
    return {
        "id": frozen["id"],
        "request_id": request_id,
        "expected_revision": frozen["revision"],
        "expected_digest": frozen["digest"],
    }


async def decide(context, value, decision="approve", *, request_id="original-approve", engine=None):
    return await manual(
        context, "egress_decide", engine=engine, decision=decision, **control(value, request_id)
    )


async def execute(context, value, *, request_id="original-execute", engine=None, via_model=True):
    call = model if via_model else manual
    return await call(context, "egress_execute", engine=engine, **control(value, request_id))


async def request(context, request_id, *, engine=None):
    return await manual(context, "egress_request", engine=engine, request_id=request_id)


async def capture_source(context, view, request_id="manual-original-source"):
    return await manual(
        context,
        "capture",
        id=view["notebook"]["id"],
        request_id=request_id,
        url=URL,
        max_age_days=1,
    )


def stored_artifact_version(engine, snapshot):
    """Inspect the internal protocol marker without adding it to public DTOs."""
    path = engine.personal_artifacts.store.db_path.resolve()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT record_json FROM artifact_versions WHERE artifact_id=? AND version=?",
            (snapshot["artifact_id"], snapshot["version"]),
        ).fetchone()
    assert row is not None
    value = json.loads(row[0])
    assert value["sha256"] == snapshot["sha256"]
    return value


async def test_private_proposal_and_human_approval_are_local_until_exact_execute(egress):
    view = await notebook(egress)
    _, ref, _ = await private_material(egress)
    offered = await propose(egress, view)
    frozen = proposal(offered)
    assert frozen["status"] == "proposed"
    assert frozen["payload"]["url"] == URL
    original_record = await egress.service._record("egress", frozen["id"])
    assert ref in original_record["artifact_refs"]
    assert frozen["dependencies"]["artifact_count"] >= 1
    assert egress.transport.captures == egress.transport.searches == []
    approved = await decide(egress, offered)
    assert proposal(approved)["status"] == "approved"
    assert proposal(approved)["approval"]["digest"] == frozen["digest"]
    assert egress.transport.captures == []
    completed = await execute(egress, approved)
    assert proposal(completed)["status"] == "completed"
    assert egress.transport.captures == [{"url": URL, "options": {"max_redirects": 0}}]
    result = completed["result"]
    assert result["notebook"]["id"] == view["notebook"]["id"]
    source = result["sources"][0]
    stored, content = await egress.engine.personal_artifacts.read(
        actor_id="main",
        artifact_id=source["artifact_ref"]["artifact_id"],
        version=source["artifact_ref"]["version"],
    )
    assert content.decode() == BODY
    assert stored_artifact_version(egress.engine, stored)["authority_version"] == 7
    assert ref in stored["source_refs"]
    await egress.engine.personal_artifacts.authorize_engine()


async def test_approved_search_freezes_one_provider_and_keeps_discovery_distinct(egress):
    view = await notebook(egress)
    await private_material(egress)
    offered = await propose(egress, view, action="search")
    frozen = proposal(offered)
    assert frozen["payload"]["query"] == PRIVATE
    assert frozen["destination_plan"]["provider"] == "duckduckgo"
    assert egress.transport.searches == []
    approved = await decide(egress, offered)
    completed = await execute(egress, approved)
    assert len(egress.transport.searches) == 1 and egress.transport.captures == []
    sent = egress.transport.searches[0]
    assert sent["query"] == PRIVATE and sent["max_results"] == 2
    assert sent["plan"] == frozen["destination_plan"]
    assert completed["result"]["discovery"]["evidence_kind"] == "discovery_only"
    assert completed["result"]["view"]["sources"] == []


async def test_definite_failed_capture_consumes_approval_and_replays_the_original_observation(
    egress,
):
    approved = await decide(egress, await propose(egress, await notebook(egress)))
    egress.transport.error = PersonalAgentError("source_unavailable", "原网页已返回明确不可用状态")
    completed = await execute(egress, approved, via_model=False)
    assert proposal(completed)["status"] == "completed"
    source = completed["result"]["sources"][0]
    assert source["status"] == "unavailable" and source["artifact_ref"] is None
    observed = await manual(egress, "source", id=source["id"])
    assert observed["availability"] == "available" and observed["body"] is None
    egress.transport.error = None
    replay = await execute(
        egress, approved, request_id="other-definite-result-read", via_model=False
    )
    assert proposal(replay)["execution"] == proposal(completed)["execution"]
    assert len(egress.transport.captures) == 1


@pytest.mark.parametrize("action", ["search", "capture", "recheck"])
async def test_private_direct_network_stays_blocked_even_after_other_proposal_approved(
    egress, action
):
    view = await capture_source(egress, await notebook(egress))
    source = view["sources"][0]
    await private_material(egress)
    approved = await decide(egress, await propose(egress, view))
    assert proposal(approved)["status"] == "approved"
    arguments = {"request_id": "unapproved-direct-" + action}
    if action == "search":
        arguments.update(id=view["notebook"]["id"], query=PRIVATE, max_results=1)
    elif action == "capture":
        arguments.update(id=view["notebook"]["id"], url=URL, max_age_days=1)
    else:
        arguments.update(source_id=source["id"], expected_revision=1)
    with pytest.raises(ToolExecutionError):
        await model(egress, action, **arguments)
    assert len(egress.transport.captures) == 1 and egress.transport.searches == []


@pytest.mark.parametrize("decision", ["approve", "reject", "revoke"])
async def test_model_has_no_human_decision_power_in_schema_or_service(egress, decision):
    offered = await propose(egress, await notebook(egress))
    args = {
        "operation": "egress_decide",
        "decision": decision,
        **control(offered, "forged-decision"),
    }
    with pytest.raises(PersonalAgentError):
        validate_research_arguments(args)
    with pytest.raises(ToolExecutionError):
        await model(egress, **args)
    with pytest.raises(PersonalAgentError):
        await egress.service.execute_operation(args, caller_actor="main")
    assert (
        proposal(await manual(egress, "egress_get", id=proposal(offered)["id"]))["status"]
        == "proposed"
    )
    assert egress.transport.captures == egress.transport.searches == []


@pytest.mark.parametrize(
    "forged",
    [
        "actor_id",
        "caller_actor",
        "trusted_channel",
        "session_id",
        "approval",
        "dependencies",
        "destination_plan",
        "allow_network",
    ],
)
async def test_model_cannot_inject_identity_dependencies_or_network_authority(egress, forged):
    view = await notebook(egress)
    with pytest.raises(ToolExecutionError) as caught:
        await propose(egress, view, **{forged: PRIVATE})
    assert caught.value.code == "invalid_input"
    assert egress.transport.captures == egress.transport.searches == []


@pytest.mark.parametrize("changed", ["url", "query", "provider", "max_results", "max_age_days"])
async def test_execute_never_accepts_new_network_arguments(egress, changed):
    approved = await decide(egress, await propose(egress, await notebook(egress)))
    args = control(approved, "execute-mutated-" + changed)
    args[changed] = (
        2
        if changed in {"max_results", "max_age_days"}
        else "https://other.example/"
        if changed == "url"
        else PRIVATE
    )
    with pytest.raises(PersonalAgentError) as caught:
        await manual(egress, "egress_execute", **args)
    assert caught.value.code == "invalid_input"
    assert egress.transport.captures == egress.transport.searches == []


@pytest.mark.parametrize("changed", ["digest", "revision"])
async def test_approval_requires_the_exact_human_seen_revision_and_digest(egress, changed):
    offered = await propose(egress, await notebook(egress))
    args = control(offered, "stale-human-approval")
    if changed == "digest":
        args["expected_digest"] = "0" * 64
    else:
        args["expected_revision"] += 1
    with pytest.raises(PersonalAgentError) as caught:
        await manual(egress, "egress_decide", decision="approve", **args)
    assert caught.value.code == "version_conflict"
    assert (
        proposal(await manual(egress, "egress_get", id=proposal(offered)["id"]))["status"]
        == "proposed"
    )
    assert egress.transport.captures == []


async def test_rejected_and_revoked_proposals_remain_unsent_on_replay(egress):
    view = await notebook(egress)
    rejected = await decide(
        egress, await propose(egress, view), "reject", request_id="reject-original"
    )
    assert proposal(rejected)["status"] == "rejected"
    with pytest.raises((PersonalAgentError, ToolExecutionError)):
        await execute(egress, rejected)
    offered = await propose(egress, view, request_id="other-proposal")
    approved = await decide(egress, offered, request_id="approve-other")
    revoked = await decide(egress, approved, "revoke", request_id="revoke-original")
    assert proposal(revoked)["status"] == "revoked"
    with pytest.raises((PersonalAgentError, ToolExecutionError)):
        await execute(egress, revoked, request_id="execute-revoked")
    replay = await decide(egress, offered, request_id="approve-other")
    assert proposal(replay)["status"] == "revoked"
    assert egress.transport.captures == egress.transport.searches == []


async def test_propose_and_control_request_ids_are_idempotent_not_new_capabilities(egress):
    view = await notebook(egress)
    first = await propose(egress, view)
    assert proposal(await propose(egress, view))["id"] == proposal(first)["id"]
    with pytest.raises(ToolExecutionError) as caught:
        await propose(egress, view, url=URL + "&changed=1")
    assert caught.value.code == "idempotency_conflict"
    approved = await decide(egress, first)
    replay = await decide(egress, first)
    assert proposal(replay)["approval"] == proposal(approved)["approval"]
    with pytest.raises(PersonalAgentError):
        await decide(egress, first, "reject")
    assert egress.transport.captures == []


async def test_two_identical_human_approvals_issue_one_original_receipt(egress):
    offered = await propose(egress, await notebook(egress))
    first, second = await asyncio.gather(decide(egress, offered), decide(egress, offered))
    assert proposal(first)["approval"] == proposal(second)["approval"]
    assert egress.transport.captures == []


async def test_two_distinct_human_decisions_have_one_cas_winner(egress):
    offered = await propose(egress, await notebook(egress))
    values = await asyncio.gather(
        decide(egress, offered, request_id="approve-race"),
        decide(egress, offered, "reject", request_id="reject-race"),
        return_exceptions=True,
    )
    successful = [value for value in values if isinstance(value, dict)]
    failed = [value for value in values if isinstance(value, PersonalAgentError)]
    assert len(successful) == len(failed) == 1
    assert failed[0].code == "version_conflict"
    assert proposal(successful[0])["status"] in {"approved", "rejected"}
    assert egress.transport.captures == []


async def test_same_proposal_two_real_engines_dispatch_exactly_once(egress):
    view = await notebook(egress)
    await private_material(egress)
    approved = await decide(egress, await propose(egress, view))
    other = await egress.make(session_id=egress.engine._session.id)
    egress.transport.capture_release = asyncio.Event()
    first = asyncio.create_task(execute(egress, approved, via_model=False))
    await asyncio.wait_for(egress.transport.capture_entered.wait(), timeout=WAIT)
    try:
        second = await asyncio.gather(
            execute(
                egress,
                approved,
                engine=other,
                request_id="different-second-execute",
                via_model=False,
            ),
            return_exceptions=True,
        )
        assert len(egress.transport.captures) == 1
        assert not any(isinstance(value, dict) and value.get("result") for value in second)
    finally:
        egress.transport.capture_release.set()
    finished = await asyncio.wait_for(first, timeout=WAIT)
    assert proposal(finished)["status"] == "completed"
    replay = await execute(
        egress,
        approved,
        engine=other,
        request_id="different-second-execute",
        via_model=False,
    )
    assert proposal(replay)["execution"] == proposal(finished)["execution"]
    assert len(egress.transport.captures) == 1


async def test_cancel_after_started_is_unknown_and_cold_engine_never_resends(egress):
    approved = await decide(egress, await propose(egress, await notebook(egress)))
    session_id = egress.engine._session.id
    egress.transport.capture_release = asyncio.Event()
    active = asyncio.create_task(execute(egress, approved, via_model=False))
    await asyncio.wait_for(egress.transport.capture_entered.wait(), timeout=WAIT)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert len(egress.transport.captures) == 1
    status = await request(egress, "original-execute")
    assert status["status"] == "unknown"
    assert "resume" not in status
    current = await manual(egress, "egress_get", id=proposal(approved)["id"])
    assert proposal(current)["status"] == "started"
    other = await egress.make(session_id=session_id)
    cold_status = await request(egress, "original-execute", engine=other)
    assert cold_status["status"] == "unknown" and "resume" not in cold_status
    with pytest.raises((PersonalAgentError, ToolExecutionError)):
        await execute(egress, approved, engine=other, via_model=False)
    assert len(egress.transport.captures) == 1


@pytest.mark.parametrize("stage", ["before_approval", "before_execution"])
async def test_expiry_boundary_stops_before_any_outbound_transport(egress, stage):
    offered = await propose(egress, await notebook(egress))
    current = offered if stage == "before_approval" else await decide(egress, offered)
    egress.clock.value = datetime.fromisoformat(proposal(offered)["expires_at"])
    with pytest.raises((PersonalAgentError, ToolExecutionError)):
        if stage == "before_approval":
            await decide(egress, current)
        else:
            await execute(egress, current)
    assert egress.transport.captures == egress.transport.searches == []


@pytest.mark.parametrize("stage", ["before_approval", "before_execution"])
async def test_notebook_revision_change_invalidates_the_original_egress_intent(egress, stage):
    view = await notebook(egress)
    offered = await propose(egress, view)
    current = offered if stage == "before_approval" else await decide(egress, offered)
    original = view["notebook"]
    await manual(
        egress,
        "revise",
        id=original["id"],
        request_id="changed-notebook",
        expected_revision=1,
        title="修改后的问题",
        question=original["question"],
        domain=original["domain"],
        claims=[],
        conclusion=original["conclusion"],
    )
    with pytest.raises((PersonalAgentError, ToolExecutionError)):
        if stage == "before_approval":
            await decide(egress, current)
        else:
            await execute(egress, current)
    assert egress.transport.captures == []


async def test_recheck_proposal_binds_the_exact_source_revision(egress):
    view = await capture_source(egress, await notebook(egress))
    source = view["sources"][0]
    offered = await propose(
        egress,
        view,
        action="recheck",
        source_id=source["id"],
        expected_source_revision=source["revision"],
    )
    approved = await decide(egress, offered)
    await manual(
        egress,
        "recheck",
        source_id=source["id"],
        expected_revision=1,
        request_id="different-human-source-observation",
    )
    with pytest.raises((PersonalAgentError, ToolExecutionError)):
        await execute(egress, approved)
    assert len(egress.transport.captures) == 2


async def test_provider_configuration_change_never_silently_broadens_approved_plan(egress):
    offered = await propose(egress, await notebook(egress), action="search", provider="brave")
    approved = await decide(egress, offered)
    search = egress.engine.tool_registry.get("web_search")
    search._search_config.brave.safesearch = "strict"
    with pytest.raises((PersonalAgentError, ToolExecutionError)) as caught:
        await execute(egress, approved)
    assert caught.value.code in {"source_stale", "approval_invalidated"}
    assert egress.transport.searches == []


async def test_private_memory_is_implicit_and_erasure_fences_an_already_approved_query(egress):
    engine = egress.engine
    view = await notebook(egress)
    memory = await engine.personal_memory.dispatch(
        "create",
        {
            "request_id": "private-memory",
            "topic": "研究背景",
            "category": "fact",
            "content": PRIVATE,
            "scope": "workspace",
        },
        actor_id="main",
        trusted_channel="management_web",
    )
    engine.config.memory.long_term_enabled = True
    engine._personal_memory_runtime_refs = [memory_reference(memory)]
    offered = await propose(egress, view, action="search")
    original_record = await egress.service._record("egress", proposal(offered)["id"])
    assert memory_reference(memory) in original_record["memory_refs"]
    assert proposal(offered)["dependencies"]["memory_count"] == 1
    approved = await decide(egress, offered)
    await engine.personal_memory.dispatch(
        "delete",
        {
            "request_id": "erase-original-memory",
            "memory_id": memory["memory_id"],
            "expected_revision": memory["revision"],
        },
        actor_id="main",
        trusted_channel="management_web",
    )
    with pytest.raises((PersonalAgentError, ToolExecutionError)):
        await execute(egress, approved)
    assert egress.transport.searches == []


async def test_private_memory_without_artifact_pins_still_blocks_direct_model_network(egress):
    engine = egress.engine
    # An explicitly created local notebook does not itself populate model material.
    view = await manual(
        egress,
        "create",
        request_id="memory-only-notebook",
        title="仅记忆上下文",
        question="私有记忆中的词不应直接外发",
        domain="general",
    )
    memory = await engine.personal_memory.dispatch(
        "create",
        {
            "request_id": "only-private-memory",
            "topic": "私有研究背景",
            "category": "fact",
            "content": PRIVATE,
            "scope": "workspace",
        },
        actor_id="main",
        trusted_channel="management_web",
    )
    engine.config.memory.long_term_enabled = True
    engine._personal_memory_runtime_refs = [memory_reference(memory)]
    assert await engine.personal_artifacts.session_refs(engine._session.id, "main") == []
    assert not getattr(engine, "_personal_artifact_refs", [])
    assert not getattr(engine, "_personal_memory_artifact_refs", [])
    with pytest.raises(ToolExecutionError) as caught:
        await model(
            egress,
            "search",
            id=view["notebook"]["id"],
            request_id="memory-only-unapproved-search",
            query=PRIVATE,
            max_results=1,
        )
    assert caught.value.code == "artifact_network_denied"
    assert egress.transport.captures == egress.transport.searches == []


async def test_owner_queue_sees_child_proposal_but_other_model_cannot_read_or_approve_it(egress):
    engine = egress.engine
    main_session = engine._session.id
    child = engine.personal_agents.create(
        request_id="explicit-egress-agent",
        name="Mira",
        instructions="按用户要求研究",
        tool_names=["personal_research"],
    )
    opened = await engine.personal_agents.open_session(child.id)
    assert await engine.load_session(opened["session_id"])
    view = await notebook(egress, request_id="child-notebook")
    offered = await propose(egress, view)
    assert proposal(offered)["actor_id"] == child.id
    owner_page = await manual(egress, "egress_list", limit=20)
    assert any(
        item["proposal"]["id"] == proposal(offered)["id"] for item in owner_page["proposals"]
    )
    approved = await decide(egress, offered)
    assert proposal(approved)["status"] == "approved"
    assert await engine.load_session(main_session)
    own_page = await model(egress, "egress_list", limit=20)
    assert all(item["proposal"]["actor_id"] == "main" for item in own_page["proposals"])
    with pytest.raises(ToolExecutionError):
        await model(egress, "egress_get", id=proposal(offered)["id"])
    with pytest.raises(ToolExecutionError):
        await execute(egress, approved)
    assert await engine.load_session(opened["session_id"])
    complete = await execute(egress, approved)
    assert proposal(complete)["status"] == "completed" and len(egress.transport.captures) == 1


async def test_agent_configuration_change_invalidates_approval_without_changing_identity(egress):
    engine = egress.engine
    child = engine.personal_agents.create(
        request_id="explicit-config-agent",
        name="Atlas",
        instructions="原研究要求",
        tool_names=["personal_research"],
    )
    opened = await engine.personal_agents.open_session(child.id)
    assert await engine.load_session(opened["session_id"])
    approved = await decide(egress, await propose(egress, await notebook(egress)))
    engine.personal_agents.update(child.id, child.version, instructions="新的职责版本")
    with pytest.raises((PersonalAgentError, ToolExecutionError)):
        await execute(egress, approved)
    assert egress.transport.captures == []


async def test_revoking_original_artifact_share_redacts_proposal_and_fences_unsent_approval(egress):
    engine = egress.engine
    owner = engine.personal_agents.create(
        request_id="explicit-input-owner", name="Echo", instructions="保留原资料", tool_names=[]
    )
    view = await notebook(egress, title=PRIVATE)
    _, _, grant = await private_material(egress, owner=owner.id, recipient="main")
    offered = await propose(egress, view, action="search")
    approved = await decide(egress, offered)
    await engine.personal_artifacts.revoke(
        actor_id=owner.id,
        grant_id=grant["grant_id"],
        expected_revision=grant["revision"],
        request_id="revoke-original-egress-input",
    )
    with pytest.raises((PersonalAgentError, ToolExecutionError)):
        await execute(egress, approved, via_model=False)
    values = [
        await manual(egress, "egress_get", id=proposal(offered)["id"]),
        await manual(egress, "egress_list", limit=20),
        await request(egress, "original-egress-proposal"),
    ]
    assert PRIVATE not in json.dumps(values, ensure_ascii=False)
    assert egress.transport.captures == egress.transport.searches == []


async def test_captured_response_can_finish_after_local_failure_without_a_second_send(egress):
    view = await notebook(egress)
    await private_material(egress)
    approved = await decide(egress, await propose(egress, view))
    artifacts = egress.engine.personal_artifacts
    original_publish = artifacts.publish
    fault_count = 0

    async def fail_source_once(**arguments):
        nonlocal fault_count
        if (
            arguments.get("evidence", {}).get("source_kind") == "research_capture"
            and not fault_count
        ):
            fault_count += 1
            raise PersonalAgentError("storage_error", "明确注入的本地存储失败")
        return await original_publish(**arguments)

    egress.monkeypatch.setattr(artifacts, "publish", fail_source_once)
    with pytest.raises(PersonalAgentError):
        await execute(egress, approved, via_model=False)
    assert fault_count == 1 and len(egress.transport.captures) == 1
    pending = await request(egress, "original-execute")
    assert pending["status"] == "pending"
    assert proposal(pending["view"])["status"] == "started"
    assert proposal(pending["view"])["execution"]["phase"] == "captured"
    assert proposal(pending["view"])["execution"]["saved_stage"] is True
    resume = pending["resume"]
    assert resume == {
        "operation": "egress_execute",
        "arguments": control(approved, "original-execute"),
    }
    assert resume["arguments"]["expected_revision"] < proposal(pending["view"])["revision"]
    assert not {"query", "url", "provider", "context", "dependencies"} & set(resume["arguments"])
    other = await egress.make(session_id=egress.engine._session.id)
    reopened = await request(egress, "original-execute", engine=other)
    assert reopened["status"] == "pending" and reopened["resume"] == resume
    completed = await manual(
        egress,
        resume["operation"],
        engine=other,
        **resume["arguments"],
    )
    assert proposal(completed)["status"] == "completed"
    assert completed["result"]["sources"] and len(egress.transport.captures) == 1


async def test_completed_replay_keeps_original_source_receipt_after_later_source_observation(
    egress,
):
    view = await notebook(egress)
    approved = await decide(egress, await propose(egress, view))
    completed = await execute(egress, approved, via_model=False)
    original_execution = copy.deepcopy(proposal(completed)["execution"])
    original_source = completed["result"]["sources"][0]
    egress.transport.body = "收入 USD 100；成本 USD 30。A_LATER_SOURCE_REVISION"
    await manual(
        egress,
        "recheck",
        source_id=original_source["id"],
        expected_revision=1,
        request_id="later-manual-source-observation",
    )
    replay = await execute(egress, approved, via_model=False)
    assert proposal(replay)["execution"] == original_execution
    assert len(egress.transport.captures) == 2
    result = proposal(replay)["execution"]["result"]
    assert result["source_id"] == original_source["id"]
    assert result["source_revision"] == 1
    observed = await manual(egress, "source", id=original_source["id"], revision=1)
    assert observed["body"] == BODY


async def test_completed_output_and_derivative_keep_original_private_acl_after_share_revocation(
    egress,
):
    engine = egress.engine
    owner = engine.personal_agents.create(
        request_id="explicit-completed-input-owner",
        name="Iris",
        instructions="保留源资料授权",
        tool_names=[],
    )
    view = await notebook(egress)
    _, original_ref, grant = await private_material(
        egress, owner=owner.id, recipient="main", request_id="completed-private-input"
    )
    offered = await propose(egress, view)
    approved = await decide(egress, offered)
    completed = await execute(egress, approved)
    source_ref = completed["result"]["sources"][0]["artifact_ref"]
    snapshot, body = await engine.personal_artifacts.read(
        actor_id="main", artifact_id=source_ref["artifact_id"], version=source_ref["version"]
    )
    assert body.decode() == BODY and original_ref in snapshot["source_refs"]
    derivative = await engine.personal_artifacts.publish(
        actor_id="main",
        request_id="egress-derived-output",
        title="来源摘要",
        kind="output",
        filename="summary.md",
        content=b"# Derived approved research source\n",
        source_refs=[source_ref],
    )
    await engine.personal_artifacts.authorize_engine()
    await engine.personal_artifacts.revoke(
        actor_id=owner.id,
        grant_id=grant["grant_id"],
        expected_revision=grant["revision"],
        request_id="revoke-completed-private-input",
    )
    for ref in (source_ref, engine.personal_artifacts.reference(derivative)):
        with pytest.raises(PersonalAgentError):
            await engine.personal_artifacts.read(
                actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
            )
    with pytest.raises(PersonalAgentError):
        await engine.personal_artifacts.authorize_engine()
    redacted = await manual(egress, "egress_get", id=proposal(offered)["id"])
    assert PRIVATE not in json.dumps(redacted, ensure_ascii=False)
    assert len(egress.transport.captures) == 1


async def test_completed_approval_does_not_authorize_a_different_workspace(egress):
    approved = await decide(egress, await propose(egress, await notebook(egress)))
    other = await egress.make(other_root=egress.root / "other-workspace")
    with pytest.raises(PersonalAgentError) as caught:
        await manual(egress, "egress_get", engine=other, id=proposal(approved)["id"])
    assert caught.value.code == "not_found"
    with pytest.raises(PersonalAgentError):
        await execute(egress, approved, engine=other, via_model=False)
    assert egress.transport.captures == []


async def test_revocation_race_before_dispatch_has_no_outbound_transport(egress):
    offered = await propose(egress, await notebook(egress))
    approved = await decide(egress, offered)
    original_start = egress.service._start_approved
    entered, release = asyncio.Event(), asyncio.Event()

    async def gate_before_admission(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_start(*args, **kwargs)

    egress.monkeypatch.setattr(egress.service, "_start_approved", gate_before_admission)
    running = asyncio.create_task(execute(egress, approved, via_model=False))
    await asyncio.wait_for(entered.wait(), timeout=WAIT)
    try:
        revoked = await decide(egress, approved, "revoke", request_id="race-revoke")
        assert proposal(revoked)["status"] == "revoked"
    finally:
        release.set()
    with pytest.raises(PersonalAgentError) as caught:
        await asyncio.wait_for(running, timeout=WAIT)
    assert caught.value.code in {"approval_invalidated", "version_conflict"}
    assert egress.transport.captures == []


async def test_revocation_after_started_never_claims_the_request_was_unsent(egress):
    approved = await decide(egress, await propose(egress, await notebook(egress)))
    egress.transport.capture_release = asyncio.Event()
    running = asyncio.create_task(execute(egress, approved, via_model=False))
    await asyncio.wait_for(egress.transport.capture_entered.wait(), timeout=WAIT)
    started = await manual(egress, "egress_get", id=proposal(approved)["id"])
    assert proposal(started)["status"] == "started"
    try:
        # Revocation cannot retract data that already crossed the send boundary.
        with pytest.raises(PersonalAgentError) as caught:
            await decide(egress, started, "revoke", request_id="late-revoke")
        assert caught.value.code == "action_locked"
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert (await request(egress, "original-execute"))["status"] == "unknown"
    with pytest.raises(PersonalAgentError):
        await execute(egress, approved, request_id="other-attempt-after-send", via_model=False)
    assert len(egress.transport.captures) == 1
