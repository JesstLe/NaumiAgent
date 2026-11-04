"""Artifact service/API/Tool boundaries over real Engines, SQLite and file bytes.

Model transport is controlled only in tests explicitly exercising a Run. Docker
and HTTP sockets remain real where marked; no provider calls are made here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from naumi_agent.api.app import create_app
from naumi_agent.config.settings import AppConfig
from naumi_agent.daemons.agent_jobs import AgentJobState
from naumi_agent.model.router import ModelResponse, ModelRouter, ModelTier, StreamChunk, TokenUsage
from naumi_agent.orchestrator.engine import AgentResult
from naumi_agent.personal import runtime
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tools.base import ToolCall
from naumi_agent.tools.output_publish import MAX_ASSET_BYTES

pytestmark = pytest.mark.usefixtures("runtime_payload_key")
_KEY = "artifact-fixture-key"
_CSV = b"income,expense\n1200,375\n"


@dataclass
class Context:
    engine: object
    service: object
    app: FastAPI
    client: httpx.AsyncClient
    profiles: list
    sessions: list[str]
    workspace: Path
    config: AppConfig


@pytest.fixture
async def artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state-home"))

    async def no_provider(self, **kwargs):
        raise AssertionError("Artifact fixture must not contact an external model provider")

    async def no_stream(self, **kwargs):
        raise AssertionError("Artifact fixture must not contact an external streaming provider")
        yield  # pragma: no cover

    monkeypatch.setattr(ModelRouter, "call", no_provider)
    monkeypatch.setattr(ModelRouter, "stream", no_stream)
    work, state = tmp_path / "work", tmp_path / "data"
    work.mkdir()
    (work / "public.csv").write_bytes(_CSV)
    (work / ".gitignore").write_text(".naumi/\n", "utf-8")
    for args in (
        ["init", "-q"],
        ["add", "public.csv", ".gitignore"],
        [
            "-c",
            "user.name=Artifact Fixture",
            "-c",
            "user.email=artifact@example.invalid",
            "commit",
            "-qm",
            "Record artifact input",
        ],
    ):
        subprocess.run(["git", "-C", str(work), *args], check=True, capture_output=True)
    config = AppConfig(
        workspace_root=str(work),
        api={"api_keys": [_KEY]},
        personal={"data_dir": str(state / "personal")},
        memory={
            "session_db_path": str(state / "sessions.db"),
            "vector_db_path": str(state / "vectors"),
            "long_term_enabled": False,
        },
        safety={"permission_mode": "bypass", "allowed_dirs": [str(work)], "max_turns": 6},
    )
    engine = create_agent_engine(config)
    await engine.get_or_create_session()
    engine.personal_teams.start = lambda: None
    engine.personal_collaborations.start = lambda: None
    tools = [
        "personal_artifacts",
        "file_read",
        "read",
        "file_write",
        "file_edit",
        "glob",
        "grep",
        "bash_run",
        "code_execute",
        "web_fetch",
        "output_publish",
    ]
    profiles = [
        engine.personal_agents.create(
            request_id="artifact-agent-" + str(index),
            name=name,
            instructions="按用户明确委托处理资料，保留确切版本与来源。",
            tool_names=tools,
            model_tier="fast",
        )
        for index, name in enumerate(("Mira", "Lambda-7", "山海"))
    ]
    sessions = [
        (await engine.personal_agents.open_session(profile.id))["session_id"]
        for profile in profiles
    ]
    app = create_app()
    app.state.engine, app.state.config = engine, config
    app.state.chat_run_store = engine.chat_run_store
    app.state.engine_lock = asyncio.Lock()
    app.state.active_chat_run_tasks = {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://artifact.test/api/v1",
        headers={"X-API-Key": _KEY},
    ) as client:
        try:
            yield Context(
                engine, engine.personal_artifacts, app, client, profiles, sessions, work, config
            )
        finally:
            await engine.shutdown()


async def _publish(
    context,
    *,
    owner=0,
    request="input-v1",
    content=_CSV,
    title="预算",
    filename="budget.csv",
    kind="material",
    **extra,
):
    return await context.service.publish(
        actor_id=context.profiles[owner].id if type(owner) is int else owner,
        request_id=request,
        title=title,
        kind=kind,
        filename=filename,
        content=content,
        **extra,
    )


async def _share(context, snapshot, *, recipient=1, request="share-v1"):
    return await context.service.share(
        actor_id=snapshot["owner_id"],
        artifact_id=snapshot["artifact_id"],
        version=snapshot["version"],
        recipient_id=context.profiles[recipient].id if type(recipient) is int else recipient,
        request_id=request,
    )


async def _tool(context, name, arguments, *, call_id="artifact-call", actor=1):
    await context.engine.load_session(context.sessions[actor])
    return await context.engine.execute_tool(
        ToolCall(
            id=call_id,
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        )
    )


def _body(context, **extra):
    return {
        "actor_id": context.profiles[0].id,
        "request_id": "http-publish",
        "title": "预算",
        "kind": "material",
        "filename": "budget.csv",
        "content": _CSV.decode(),
        **extra,
    }


async def test_user_names_are_kept_separate_from_responsibilities(artifacts):
    assert [profile.name for profile in artifacts.profiles] == ["Mira", "Lambda-7", "山海"]
    assert [artifacts.engine.personal_agents.get(p.id).name for p in artifacts.profiles] == [
        "Mira",
        "Lambda-7",
        "山海",
    ]


async def test_private_material_exact_read_has_no_raw_blob_or_public_hash_url(artifacts):
    value = await _publish(artifacts)
    assert value["sha256"] == hashlib.sha256(_CSV).hexdigest() and value["version"] == 1
    assert value["owner_id"] == artifacts.profiles[0].id
    assert value["grant_id"] == "" and value["grant_revision"] == 0
    assert not any(key in value for key in ("path", "blob_path", "data_path", "storage_key"))
    read, content = await artifacts.service.read(
        actor_id=value["owner_id"],
        artifact_id=value["artifact_id"],
        version=1,
    )
    assert content == _CSV and read["sha256"] == value["sha256"]
    denied = await artifacts.client.get(
        "/personal/artifacts/" + value["artifact_id"],
        params={"actor_id": artifacts.profiles[1].id, "version": 1},
    )
    assert denied.status_code == 404 and "预算" not in denied.text
    public = await artifacts.client.get("/output-assets/out-" + value["sha256"] + ".csv")
    assert public.status_code == 404


async def test_exact_v1_share_never_leaks_unshared_v2_or_latest_metadata(artifacts):
    first = await _publish(artifacts)
    grant = await _share(artifacts, first)
    revised = await _publish(
        artifacts,
        request="input-v2",
        content=b"income,expense\n9000,1\n",
        artifact_id=first["artifact_id"],
        expected_version=1,
    )
    assert revised["version"] == 2
    value, content = await artifacts.service.read(
        actor_id=artifacts.profiles[1].id,
        artifact_id=first["artifact_id"],
    )
    assert value["version"] == 1 and content == _CSV
    assert value["grant_id"] == grant["grant_id"]
    versions = await artifacts.service.versions(
        actor_id=artifacts.profiles[1].id,
        artifact_id=first["artifact_id"],
    )
    assert [v["version"] for v in versions] == [1]
    with pytest.raises(PersonalAgentError) as caught:
        await artifacts.service.get(
            actor_id=artifacts.profiles[1].id,
            artifact_id=first["artifact_id"],
            version=2,
        )
    assert caught.value.code == "not_found"


async def test_same_request_concurrent_publish_is_one_version_and_changed_payload_conflicts(
    artifacts,
):
    values = await asyncio.gather(*[_publish(artifacts) for _ in range(8)])
    assert all(value == values[0] for value in values)
    listing = await artifacts.service.list(actor_id=artifacts.profiles[0].id)
    assert len(listing) == 1 and listing[0]["version"] == 1
    with pytest.raises(PersonalAgentError) as caught:
        await _publish(artifacts, content=b"changed\ncontent\n")
    assert caught.value.code == "idempotency_conflict"


async def test_concurrent_expected_version_cas_keeps_original_and_one_winner(artifacts):
    first = await _publish(artifacts)

    async def update(index):
        try:
            return await _publish(
                artifacts,
                request="cas-" + str(index),
                artifact_id=first["artifact_id"],
                expected_version=1,
                content=f"income,expense\n{1000 + index},1\n".encode(),
            )
        except PersonalAgentError as exc:
            return exc

    results = await asyncio.gather(*[update(index) for index in range(6)])
    winners = [item for item in results if isinstance(item, dict)]
    assert len(winners) == 1 and winners[0]["version"] == 2
    assert all(isinstance(item, dict) or item.code == "version_conflict" for item in results)
    _, original = await artifacts.service.read(
        actor_id=first["owner_id"],
        artifact_id=first["artifact_id"],
        version=1,
    )
    assert original == _CSV
    assert (
        len(
            await artifacts.service.versions(
                actor_id=first["owner_id"],
                artifact_id=first["artifact_id"],
            )
        )
        == 2
    )


async def test_revoke_blocks_source_derived_and_old_session_even_after_new_grant(artifacts):
    first = await _publish(artifacts)
    grant = await _share(artifacts, first)
    read = await _tool(
        artifacts,
        "personal_artifacts",
        {
            "operation": "read",
            "artifact_id": first["artifact_id"],
            "version": 1,
        },
    )
    assert read.status == "success", read.content
    derived = await _publish(
        artifacts,
        owner=1,
        request="derived-output",
        kind="output",
        filename="result.txt",
        content=b"balance=825",
        source_refs=[artifacts.service.reference(json.loads(read.content)["artifact"])],
        session_id=artifacts.sessions[1],
    )
    with pytest.raises(PersonalAgentError) as no_laundering:
        await _share(artifacts, derived, recipient=2, request="derived-to-third")
    assert no_laundering.value.code == "not_found"
    revoked = await artifacts.service.revoke(
        actor_id=first["owner_id"],
        grant_id=grant["grant_id"],
        expected_revision=1,
        request_id="revoke-v1",
    )
    assert revoked["revision"] == 2 and revoked["status"] == "revoked"
    for identity, version in ((first["artifact_id"], 1), (derived["artifact_id"], 1)):
        with pytest.raises(PersonalAgentError) as denied:
            await artifacts.service.read(
                actor_id=artifacts.profiles[1].id,
                artifact_id=identity,
                version=version,
            )
        assert denied.value.code == "not_found"
    new = await _share(artifacts, first, request="regrant-v1")
    assert new["grant_id"] != grant["grant_id"]
    assert (
        await artifacts.service.read(
            actor_id=artifacts.profiles[1].id,
            artifact_id=first["artifact_id"],
            version=1,
        )
    )[1] == _CSV
    with pytest.raises(PersonalAgentError) as old_session:
        await artifacts.service.authorize_engine()
    assert old_session.value.code == "artifact_access_revoked"
    with pytest.raises(PersonalAgentError):
        await artifacts.service.get(
            actor_id=artifacts.profiles[1].id,
            artifact_id=derived["artifact_id"],
            version=1,
        )


@pytest.mark.parametrize("changed", ["outer", "inner"])
async def test_team_share_freezes_nested_membership_and_version(artifacts, changed):
    inner = artifacts.engine.personal_agents.create(
        name="周末",
        instructions="用户指定成员。",
        request_id="scope-inner",
        kind="team",
        member_ids=[artifacts.profiles[1].id],
        tool_names=[],
        model_tier="fast",
    )
    outer = artifacts.engine.personal_agents.create(
        name="一起看账",
        instructions="用户明确创建的组合。",
        request_id="scope-outer",
        kind="team",
        member_ids=[inner.id],
        tool_names=["personal_team"],
        model_tier="fast",
    )
    first = await _publish(artifacts)
    grant = await _share(artifacts, first, recipient=outer.id)
    assert grant["recipient_scope"]["member_ids"] == [artifacts.profiles[1].id]
    assert len(grant["recipient_scope"]["teams"]) == 2
    await artifacts.service.get(
        actor_id=artifacts.profiles[1].id,
        artifact_id=first["artifact_id"],
        version=1,
    )
    target = outer if changed == "outer" else inner
    artifacts.engine.personal_agents.update(
        target.id,
        target.version,
        member_ids=[artifacts.profiles[2].id],
    )
    with pytest.raises(PersonalAgentError) as denied:
        await artifacts.service.get(
            actor_id=artifacts.profiles[1].id,
            artifact_id=first["artifact_id"],
            version=1,
        )
    assert denied.value.code == "version_conflict"
    with pytest.raises(PersonalAgentError):
        await artifacts.service.get(
            actor_id=artifacts.profiles[2].id,
            artifact_id=first["artifact_id"],
            version=1,
        )
    assert await artifacts.service.list(actor_id=artifacts.profiles[1].id) == []


def _page(ref):
    return {
        "version": 1,
        "type": "page",
        "title": "本次结余",
        "sections": [
            {
                "id": "calculation",
                "title": "核算",
                "evidence_status": "fact",
                "markdown": "1200 - 375 = 825 元",
                "source_refs": [ref],
                "widget": {
                    "version": 1,
                    "type": "table",
                    "title": "实际数值",
                    "columns": [{"key": "amount", "label": "结余"}],
                    "rows": [{"amount": 825}],
                },
            },
            {
                "id": "unknown",
                "title": "待确认",
                "evidence_status": "unknown",
                "markdown": "原文件没有逐笔明细，不推断来源构成。",
                "source_refs": [],
            },
        ],
    }


async def test_page_real_source_refs_values_and_evidence_status_are_preserved(artifacts):
    first = await _publish(artifacts)
    page = _page(artifacts.service.reference(first))
    value = await _publish(
        artifacts,
        request="page-output",
        kind="output",
        title="本次结余",
        filename="budget.json",
        content=json.dumps(page, ensure_ascii=False).encode(),
    )
    assert value["source_refs"] == [artifacts.service.reference(first)]
    rendered = await artifacts.service.preview(
        actor_id=first["owner_id"],
        artifact_id=value["artifact_id"],
        version=1,
    )
    assert rendered == page
    assert rendered["sections"][0]["widget"]["rows"][0]["amount"] == 825
    assert rendered["sections"][1]["evidence_status"] == "unknown"


async def test_csv_preview_is_bounded_but_download_keeps_all_real_rows(artifacts):
    data = ("id,amount\n" + "".join(f"{index},{index * 2}\n" for index in range(2001))).encode()
    value = await _publish(artifacts, content=data)
    preview = await artifacts.service.preview(
        actor_id=value["owner_id"],
        artifact_id=value["artifact_id"],
        version=1,
    )
    assert preview["total_rows"] == 2001 and len(preview["rows"]) == 2000
    assert preview["rows"][100] == {"c0": "100", "c1": "200"}
    response = await artifacts.client.get(
        f"/personal/artifacts/{value['artifact_id']}/content",
        params={"actor_id": value["owner_id"], "version": 1},
    )
    assert response.status_code == 200 and response.content == data
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-SHA256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    "update",
    [
        {"actor_id": "forged"},
        {"unknown": True},
        {"title": ""},
        {"filename": "../hidden.txt"},
        {"source_refs": [{"artifact_id": "art_" + "a" * 32}]},
        {"content": 42},
    ],
)
async def test_http_strict_body_rejects_invalid_fields_without_new_artifacts(artifacts, update):
    result = await artifacts.client.post("/personal/artifacts", json=_body(artifacts, **update))
    assert result.status_code == 422, result.text
    assert await artifacts.service.list(actor_id=artifacts.profiles[0].id) == []


async def test_all_library_http_routes_require_configured_authentication(artifacts):
    value = await _publish(artifacts)
    base = "/personal/artifacts/" + value["artifact_id"]
    actor = value["owner_id"]
    for method, url, body in [
        ("GET", "/personal/artifacts?actor_id=" + actor, None),
        ("POST", "/personal/artifacts", _body(artifacts)),
        ("GET", base + "?actor_id=" + actor, None),
        ("GET", base + "/versions?actor_id=" + actor, None),
        ("GET", base + "/content?version=1&actor_id=" + actor, None),
        ("GET", base + "/preview?version=1&actor_id=" + actor, None),
        ("GET", base + "/versions/1/grants?actor_id=" + actor, None),
        (
            "POST",
            base + "/versions/1/grants",
            {"actor_id": actor, "recipient_id": artifacts.profiles[1].id, "request_id": "unauth"},
        ),
        (
            "POST",
            "/personal/artifact-grants/asg_" + "a" * 32 + "/revoke",
            {"actor_id": actor, "request_id": "unauth", "expected_revision": 1},
        ),
    ]:
        response = await artifacts.client.request(
            method, url, json=body, headers={"X-API-Key": "wrong"}
        )
        assert response.status_code == 401, (method, url, response.text)
    assert (
        await artifacts.service.grants(actor_id=actor, artifact_id=value["artifact_id"], version=1)
        == []
    )


async def test_http_share_revoke_replay_and_strict_expected_revision(artifacts):
    value = await _publish(artifacts)
    url = f"/personal/artifacts/{value['artifact_id']}/versions/1/grants"
    body = {
        "actor_id": value["owner_id"],
        "recipient_id": artifacts.profiles[1].id,
        "request_id": "http-share",
    }
    first = await artifacts.client.post(url, json=body)
    assert first.status_code == 201, first.text
    replay = await artifacts.client.post(url, json=body)
    assert replay.status_code == 201 and replay.json() == first.json()
    gid = first.json()["grant_id"]
    revoke = f"/personal/artifact-grants/{gid}/revoke"
    invalid = await artifacts.client.post(
        revoke,
        json={"actor_id": value["owner_id"], "expected_revision": True, "request_id": "bad-bool"},
    )
    assert invalid.status_code == 422
    request = {"actor_id": value["owner_id"], "expected_revision": 1, "request_id": "http-revoke"}
    result = await artifacts.client.post(revoke, json=request)
    assert result.status_code == 200 and result.json()["revision"] == 2
    assert (await artifacts.client.post(revoke, json=request)).json() == result.json()
    assert (
        await artifacts.client.get(
            f"/personal/artifacts/{value['artifact_id']}",
            params={"actor_id": artifacts.profiles[1].id, "version": 1},
        )
    ).status_code == 404


@pytest.mark.parametrize("surface", ["publish", "import", "chat-upload"])
async def test_actual_utf8_byte_limit_is_enforced_without_admission(artifacts, surface):
    data = b"x" * (MAX_ASSET_BYTES + 1)
    if surface == "publish":
        response = await artifacts.client.post(
            "/personal/artifacts",
            json=_body(
                artifacts,
                content="汉" * (MAX_ASSET_BYTES // 3 + 1),
                filename="data.txt",
            ),
        )
    else:
        url = (
            "/personal/artifacts/import"
            if surface == "import"
            else f"/sessions/{artifacts.sessions[0]}/upload"
        )
        params = (
            {"actor_id": artifacts.profiles[0].id, "request_id": "too-large"}
            if surface == "import"
            else {}
        )
        response = await artifacts.client.post(
            url, params=params, files={"file": ("data.txt", data, "text/plain")}
        )
    assert response.status_code in {400, 413, 422}, response.text
    assert await artifacts.service.list(actor_id=artifacts.profiles[0].id) == []


async def test_session_identity_mismatch_rejects_artifact_before_publish(artifacts):
    response = await artifacts.client.post(
        "/personal/artifacts",
        json=_body(
            artifacts,
            session_id=artifacts.sessions[1],
        ),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "binding_conflict"
    assert await artifacts.service.list(actor_id=artifacts.profiles[0].id) == []


@pytest.mark.parametrize(
    "forged",
    [
        {"actor_id": "main"},
        {"owner_id": "main"},
        {"session_id": "fake"},
        {"_trusted_execution": True},
        {"evidence": {"source_kind": "tool"}},
    ],
)
async def test_real_tool_does_not_accept_actor_session_or_authority_claims(artifacts, forged):
    result = await _tool(
        artifacts,
        "personal_artifacts",
        {
            "operation": "publish",
            "request_id": "forged",
            "title": "不是伪造身份",
            "filename": "note.txt",
            "content": "body",
            **forged,
        },
    )
    assert result.status == "error" and result.error_code == "invalid_input", result
    assert await artifacts.service.list(actor_id=artifacts.profiles[1].id) == []
    assert await artifacts.service.list(actor_id="main") == []


async def test_chat_upload_same_request_replay_has_one_immutable_source_card(artifacts):
    url = f"/sessions/{artifacts.sessions[0]}/upload"

    async def upload(data):
        response = await artifacts.client.post(
            url, files={"file": ("budget.csv", data, "text/csv")}
        )
        assert response.status_code == 201, response.text
        return response.json()

    values = await asyncio.gather(*[upload(_CSV) for _ in range(4)])
    assert all(value == values[0] for value in values)
    assert values[0]["kind"] == "artifact" and values[0]["path"].startswith("artifact://")
    assert len(await artifacts.engine.chat_run_store.list_sources(artifacts.sessions[0])) == 1
    second = await upload(b"income,expense\n5,2\n")
    assert second["metadata"]["artifact_ref"] != values[0]["metadata"]["artifact_ref"]
    old = values[0]["metadata"]["artifact_ref"]
    assert (
        await artifacts.service.read(
            actor_id=artifacts.profiles[0].id,
            artifact_id=old["artifact_id"],
            version=old["version"],
        )
    )[1] == _CSV
    assert not (artifacts.workspace / ".naumi/uploads").exists()


async def test_workspace_source_snapshot_does_not_reread_later_mutable_bytes(artifacts):
    url = f"/sessions/{artifacts.sessions[0]}/sources"
    response = await artifacts.client.post(url, json={"path": "public.csv", "title": "原预算"})
    assert response.status_code == 201, response.text
    first = response.json()
    (artifacts.workspace / "public.csv").write_bytes(b"income,expense\n5,2\n")
    ref = first["metadata"]["artifact_ref"]
    assert (
        await artifacts.service.read(
            actor_id=artifacts.profiles[0].id,
            artifact_id=ref["artifact_id"],
            version=ref["version"],
        )
    )[1] == _CSV
    assert len(await artifacts.engine.chat_run_store.list_sources(artifacts.sessions[0])) == 1


async def test_restarting_engine_reopens_same_exact_version_and_grant(artifacts):
    first = await _publish(artifacts)
    grant = await _share(artifacts, first)
    second = create_agent_engine(artifacts.config.model_copy(deep=True))
    try:
        snapshot, content = await second.personal_artifacts.read(
            actor_id=artifacts.profiles[1].id,
            artifact_id=first["artifact_id"],
            version=1,
        )
        assert snapshot["grant_id"] == grant["grant_id"] and snapshot["sha256"] == first["sha256"]
        assert content == _CSV
        assert (await second.personal_agents.open_session(artifacts.profiles[1].id))[
            "session_id"
        ] == artifacts.sessions[1]
    finally:
        await second.shutdown()


async def test_other_workspace_cannot_read_shared_physical_store_as_main(artifacts, tmp_path):
    first = await _publish(artifacts, owner="main")
    other = tmp_path / "other-workspace"
    other.mkdir()
    config = artifacts.config.model_copy(deep=True)
    config.workspace_root = str(other)
    config.memory.session_db_path = str(tmp_path / "other-runtime/sessions.db")
    second = create_agent_engine(config)
    try:
        assert second.personal_artifacts.store.db_path == artifacts.service.store.db_path
        with pytest.raises(PersonalAgentError) as denied:
            await second.personal_artifacts.get(
                actor_id="main", artifact_id=first["artifact_id"], version=1
            )
        assert denied.value.code == "not_found"
    finally:
        await second.shutdown()


async def test_missing_or_corrupted_blob_is_storage_error_not_success_or_identity_leak(artifacts):
    value = await _publish(artifacts)
    path = artifacts.service.store._blob(value["sha256"], value["suffix"])
    path.write_bytes(b"x" * len(_CSV))
    response = await artifacts.client.get(
        f"/personal/artifacts/{value['artifact_id']}/content",
        params={"actor_id": value["owner_id"], "version": 1},
    )
    assert response.status_code == 503 and response.json()["code"] == "storage_error"
    denied = await artifacts.client.get(
        f"/personal/artifacts/{value['artifact_id']}/content",
        params={"actor_id": artifacts.profiles[1].id, "version": 1},
    )
    assert denied.status_code == 404


@pytest.mark.parametrize(
    "name,args",
    [
        ("file_read", {"path": "private"}),
        ("read", {"path": "private"}),
        ("output_publish", {"path": "private"}),
    ],
)
async def test_host_file_and_publish_tools_cannot_read_private_broker_path(artifacts, name, args):
    value = await _publish(artifacts)
    path = artifacts.service.store._blob(value["sha256"], value["suffix"])
    result = await _tool(artifacts, name, {**args, "path": str(path)})
    assert result.status == "error" and result.error_code == "artifact_path_denied", result
    assert "1200" not in result.content


async def test_revoke_stops_actual_model_and_tool_boundaries_before_any_side_effect(
    artifacts, monkeypatch
):
    value = await _publish(artifacts)
    grant = await _share(artifacts, value)
    read = await _tool(
        artifacts,
        "personal_artifacts",
        {
            "operation": "read",
            "artifact_id": value["artifact_id"],
            "version": 1,
        },
    )
    assert read.status == "success", read
    await artifacts.service.revoke(
        actor_id=value["owner_id"],
        grant_id=grant["grant_id"],
        expected_revision=1,
        request_id="revoke",
    )
    calls = []

    async def provider(self, **kwargs):
        calls.append(kwargs)
        raise AssertionError("Revoked data must stop before model transport")

    monkeypatch.setattr(ModelRouter, "call", provider)
    with pytest.raises(PersonalAgentError) as model:
        await artifacts.engine._call_model_with_recovery(
            messages=[{"role": "user", "content": "read revoked"}],
            tier=ModelTier.FAST,
            tools=None,
        )
    assert model.value.code == "artifact_access_revoked" and calls == []
    with pytest.raises(PersonalAgentError) as tool:
        await artifacts.engine.execute_tool(
            ToolCall(
                id="after-revoke",
                name="file_write",
                arguments=json.dumps({"path": "forbidden.txt", "content": "no"}),
            )
        )
    assert tool.value.code == "artifact_access_revoked"
    assert not (artifacts.workspace / "forbidden.txt").exists()


@pytest.mark.integration
async def test_real_socket_library_roundtrip_and_host_fetch_ssrf_boundary(artifacts):
    value = await _publish(artifacts)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(artifacts.app, lifespan="off", log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        url = f"http://127.0.0.1:{port}/api/v1/personal/artifacts/{value['artifact_id']}/content"
        async with httpx.AsyncClient(headers={"X-API-Key": _KEY}) as client:
            response = await client.get(url, params={"actor_id": value["owner_id"], "version": 1})
            assert response.status_code == 200 and response.content == _CSV
        result = await _tool(artifacts, "web_fetch", {"url": url})
        assert "1200" not in result.content and (
            "SSRF" in result.content or result.status == "error"
        )
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        sock.close()


def _call(name, arguments, identity):
    return StreamChunk(
        tool_call={
            0: {
                "id": identity,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        },
        finish_reason="tool_calls",
        usage=TokenUsage(input_tokens=11, output_tokens=7, cost_usd=0.002),
    )


@pytest.mark.integration
async def test_controlled_model_real_read_docker_output_broker_and_run_lineage(
    artifacts, monkeypatch
):
    if (
        shutil.which("docker") is None
        or subprocess.run(
            ["docker", "image", "inspect", "python:3.12-slim"],
            capture_output=True,
            timeout=10,
        ).returncode
        != 0
    ):
        pytest.skip("Actual Docker daemon and existing Python image are required")
    value = await _publish(artifacts)
    grant = await _share(artifacts, value)
    await artifacts.engine.load_session(artifacts.sessions[1])
    calls = []

    async def stream(self, **kwargs):
        tools = [message for message in kwargs["messages"] if message["role"] == "tool"]
        calls.append(len(tools))
        if not tools:
            yield _call(
                "personal_artifacts",
                {"operation": "read", "artifact_id": value["artifact_id"], "version": 1},
                "read-private",
            )
        elif len(tools) == 1:
            read = json.loads(tools[-1]["content"])
            assert "1200" in read["content"] and read["artifact"]["grant_id"] == grant["grant_id"]
            code = (
                "import csv;from pathlib import Path;"
                + f"r=next(csv.DictReader(Path({read['input_path']!r}).open()));"
                + "n=int(r['income'])-int(r['expense']);"
                + "Path('/outputs/balance.csv').write_text('balance\\n'+str(n)+'\\n');print(n)"
            )
            yield _call(
                "code_execute",
                {"code": code, "language": "python", "timeout": 20},
                "calculate-private",
            )
        else:
            actual = json.loads(tools[-1]["content"])
            assert actual["exit_code"] == 0 and actual["stdout"].strip() == "825"
            assert actual["isolation"]["container_removed"] is True
            assert len(actual["artifacts"]) == 1
            output = actual["artifacts"][0]
            assert output["url"] == (
                f"/web2?artifact={output['artifact_id']}&version=1&actor={artifacts.profiles[1].id}"
            )
            assert output["markdown"] == f"[balance.csv]({output['url']})"
            yield StreamChunk(token="实际核算结余825元，产物为balance.csv，来源为已共享的预算v1。")
            yield StreamChunk(
                finish_reason="stop",
                usage=TokenUsage(input_tokens=11, output_tokens=7, cost_usd=0.002),
            )

    monkeypatch.setattr(ModelRouter, "stream", stream)

    async def events(_event, _data):
        pass

    result = await artifacts.engine.run_streaming("实际读取预算并核算，保存结果。", events)
    assert result.status == "completed", result
    assert calls == [0, 1, 2]
    outputs = await artifacts.service.list(actor_id=artifacts.profiles[1].id, kind="output")
    assert len(outputs) == 1 and outputs[0]["source_refs"][0]["grant_id"] == grant["grant_id"]
    assert outputs[0]["evidence"]["source_kind"] == "isolated_process"
    assert (
        await artifacts.service.read(
            actor_id=artifacts.profiles[1].id,
            artifact_id=outputs[0]["artifact_id"],
            version=1,
        )
    )[1] == b"balance\n825\n"
    saved = await artifacts.engine.session_store.load(artifacts.sessions[1])
    assert (
        saved.messages[-1]["metadata"]["artifact_refs"][0]["artifact_id"]
        == outputs[0]["artifact_id"]
    )
    runs = await artifacts.engine.chat_run_store.list_runs(artifacts.sessions[1], limit=10)
    assert len(runs) == 1 and runs[0].status == "completed" and runs[0].receipt is not None
    assert not (artifacts.workspace / "balance.csv").exists()


async def test_missing_catalog_does_not_reenable_raw_host_private_file_reads(artifacts):
    value = await _publish(artifacts)
    blob = artifacts.service.store._blob(value["sha256"], value["suffix"])
    original = artifacts.service.store.db_path
    backup = original.with_suffix(".missing-fixture")
    original.rename(backup)
    try:
        try:
            result = await _tool(artifacts, "file_read", {"path": str(blob)})
        except PersonalAgentError as error:
            assert error.code == "storage_error"
        else:
            assert result.status == "error", result
            assert result.error_code in {"storage_error", "artifact_path_denied"}
        assert not original.exists(), (
            "A read must not replace lost metadata with a new empty catalog"
        )
    finally:
        backup.rename(original)


async def test_session_propagation_requires_recipient_share_and_rebinds_exact_grant(artifacts):
    value = await _publish(artifacts)
    await artifacts.service.bind_refs(
        artifacts.sessions[0],
        artifacts.profiles[0].id,
        [artifacts.service.reference(value)],
    )
    with pytest.raises(PersonalAgentError) as denied:
        await artifacts.service.propagate(
            artifacts.sessions[0],
            artifacts.sessions[1],
            artifacts.profiles[1].id,
        )
    assert denied.value.code == "not_found"
    assert (
        await artifacts.service.session_refs(artifacts.sessions[1], artifacts.profiles[1].id) == []
    )
    grant = await _share(artifacts, value)
    await artifacts.service.propagate(
        artifacts.sessions[0],
        artifacts.sessions[1],
        artifacts.profiles[1].id,
    )
    refs = await artifacts.service.session_refs(artifacts.sessions[1], artifacts.profiles[1].id)
    assert len(refs) == 1 and refs[0]["grant_id"] == grant["grant_id"]
    assert refs[0]["sha256"] == value["sha256"] and refs[0]["version"] == 1
    assert refs[0]["grant_revision"] == 1


@pytest.mark.parametrize(
    "filename,content",
    [
        ("broken.pdf", b"%PDF-1.7\nnot an actual PDF structure\n%%EOF"),
        ("broken.json", b'{"duplicate":1,"duplicate":2}'),
    ],
)
async def test_invalid_first_document_does_not_lock_out_later_valid_publication(
    artifacts,
    filename,
    content,
):
    with pytest.raises(PersonalAgentError) as bad:
        await _publish(artifacts, filename=filename, content=content, request="first-invalid")
    assert bad.value.code == "invalid_input"
    assert not artifacts.service.store.db_path.exists()
    assert not artifacts.service.boundary_marker.exists()
    good = await _publish(artifacts, request="valid-after-invalid")
    assert good["version"] == 1
    assert artifacts.service.boundary_marker.exists()
    assert (
        await artifacts.service.read(
            actor_id=good["owner_id"],
            artifact_id=good["artifact_id"],
            version=1,
        )
    )[1] == _CSV


async def test_missing_entire_vault_keeps_independent_boundary_and_blocks_old_session_db(
    artifacts,
):
    await _publish(artifacts)
    base = artifacts.service.store.base_dir.resolve()
    backup = base.with_name(base.name + "-missing-fixture")
    fixture_root = artifacts.workspace.parent.resolve()
    assert base.is_relative_to(fixture_root) and backup.is_relative_to(fixture_root)
    assert artifacts.service.boundary_marker.exists() and not backup.exists()
    base.rename(backup)
    try:
        assert artifacts.service.enabled is True
        with pytest.raises(PersonalAgentError) as denied:
            await _tool(
                artifacts,
                "file_read",
                {
                    "path": artifacts.engine.config.memory.session_db_path,
                },
            )
        assert denied.value.code == "storage_error"
        assert not base.exists(), "A lost vault must not be replaced with fresh empty metadata"
    finally:
        assert not base.exists()
        backup.rename(base)


async def test_first_private_read_immediately_disables_compactor_global_memory(
    artifacts, monkeypatch
):
    value = await _publish(artifacts, owner="main")
    artifacts.engine.config.memory.long_term_enabled = True
    runtime.begin_turn(artifacts.engine)
    assert artifacts.engine._personal_artifact_refs == []
    assert artifacts.engine._compactor._long_term_memory is artifacts.engine.long_term_memory
    read = await artifacts.engine.execute_tool(
        ToolCall(
            id="main-first-read",
            name="personal_artifacts",
            arguments=json.dumps(
                {"operation": "read", "artifact_id": value["artifact_id"], "version": 1}
            ),
        )
    )
    assert read.status == "success", read
    assert artifacts.engine._personal_artifact_refs
    calls, stores = [], []

    async def extract(self, **kwargs):
        calls.append(kwargs)
        return ModelResponse(
            content='[{"content":"PRIVATE_FIXTURE_825","category":"fact"}]',
            tool_calls=[],
            model="controlled-extraction",
            usage=TokenUsage(),
        )

    async def store(entry):
        stores.append(entry)

    monkeypatch.setattr(ModelRouter, "call", extract)
    monkeypatch.setattr(artifacts.engine.long_term_memory, "store", store)
    await artifacts.engine._compactor._extract_memories(
        [
            {"role": "user", "content": "PRIVATE_FIXTURE_825 " * 30},
        ]
    )
    assert calls == [] and stores == [], "Private bytes must not enter global compaction memory"
    assert artifacts.engine._compactor._long_term_memory is None


async def test_main_private_session_auto_extraction_is_blocked_with_public_positive_control(
    artifacts,
    monkeypatch,
):
    value = await _publish(artifacts, owner="main")
    session = artifacts.engine._session.id
    await artifacts.service.bind_refs(session, "main", [artifacts.service.reference(value)])
    artifacts.engine.config.memory.long_term_enabled = True
    runtime.begin_turn(artifacts.engine)
    assert artifacts.engine._personal_artifact_refs
    assert artifacts.engine._compactor._long_term_memory is None
    from naumi_agent.memory.auto_extract import MemoryCandidate

    monkeypatch.setattr(
        "naumi_agent.personal.memory_runtime.extract_memory_candidates",
        lambda task, *_: [
            MemoryCandidate(
                content="CONTROLLED_CANDIDATE",
                category="preference",
                reason="guard probe",
                source_role="user",
                source_quote=task,
            )
        ],
    )
    completed = AgentResult(status="completed", response="controlled response")
    await artifacts.engine._auto_extract_memories("private source", completed)
    assert await artifacts.engine.personal_memory._records(actor_id="main") == []
    artifacts.engine.reset()
    await artifacts.engine.get_or_create_session()
    runtime.begin_turn(artifacts.engine)
    assert artifacts.engine._personal_artifact_refs == []
    task = "public positive control"
    artifacts.engine._append_message({"role": "user", "content": task})
    await artifacts.engine._save_session()
    with runtime.run_context(
        artifacts.engine, artifacts.engine._session.id, "extraction-guard", None
    ):
        await artifacts.engine._auto_extract_memories(task, completed)
    records = await artifacts.engine.personal_memory._records(actor_id="main")
    assert len(records) == 1 and records[0]["content"] == "CONTROLLED_CANDIDATE"
    assert records[0]["state"] == "proposed" and records[0]["confirmation"] is None
    assert artifacts.engine._legacy_long_term_memory._client is None


async def test_private_collaboration_rejects_legacy_child_without_artifact_adapter(artifacts):
    from tests.unit.test_personal_collaboration import ControlledChild, finish

    value = await _publish(artifacts, owner="main")
    grant = await _share(artifacts, value)
    source = artifacts.engine._session.id
    refs = [artifacts.service.reference(value)]
    await artifacts.service.bind_refs(source, "main", refs)
    assert artifacts.service.enabled
    recipient = artifacts.profiles[1]
    allowed = await artifacts.service.get(
        actor_id=recipient.id,
        artifact_id=value["artifact_id"],
        version=1,
    )
    assert allowed["grant_id"] == grant["grant_id"], "Failure must not be a missing share"
    records, children = [], []

    def legacy(step, job):
        child = ControlledChild(artifacts.config, step, records)
        assert not hasattr(child, "personal_artifacts")
        children.append(child)
        return child

    collaboration = artifacts.engine.personal_collaborations
    collaboration._child_factory = legacy
    created = await collaboration.create(
        source_session_id=source,
        request_id="private-legacy-adapter",
        title="确切资料交接",
        context="EXPLICIT_TASK_ONLY_NO_PRIVATE_BYTES",
        steps=[
            {
                "id": "read",
                "agent_id": recipient.id,
                "agent_version": recipient.version,
                "task": "读取已分享的确切预算版本",
                "depends_on": [],
            }
        ],
    )
    result = await finish(collaboration, created["id"])
    assert result["status"] == "error"
    step = result["steps"][0]
    assert step["status"] == "error" and "缺少私有资料访问边界" in step["error"]
    assert len(children) == 1 and records == [], "Legacy model/executor must never start"
    job = await collaboration.jobs.get(step["job_id"])
    assert job.state is AgentJobState.ERROR and job.result.total_tokens == 0
    assert step["response"] == "" and "1200,375" not in str(result)
    execution = await artifacts.engine.session_store.load(step["session_id"])
    assert execution is not None and execution.messages == []
    assert await artifacts.service.session_refs(step["session_id"], recipient.id) == []
    assert await artifacts.service.session_refs(source, "main") == refs
