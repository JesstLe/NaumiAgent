"""Original deliveries, downgrade fences, late revocation and immutable model material."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess

import pytest

from naumi_agent.model.router import ModelRouter, StreamChunk, TokenUsage
from naumi_agent.personal import runtime
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.workspace_vault import WorkspaceVault
from naumi_agent.personal.workspaces import PersonalWorkspaceService
from tests.unit.test_personal_today_authority import make_item, prepare_intent, settle
from tests.unit.test_personal_workspace_service import authority_system as _authority_system
from tests.unit.test_personal_workspace_service import federation_system as _federation_system
from tests.unit.test_personal_workspace_service import human, pair

federation_system = _federation_system
authority_system = _authority_system
pytestmark = pytest.mark.usefixtures("runtime_payload_key")


async def test_original_delivered_answer_and_bytes_read_without_ack_or_repeat(
    federation_system, monkeypatch
):
    env = federation_system

    async def local_stream(self, **_):
        yield StreamChunk(
            token="原工作区报告：本地核对完成。",
            finish_reason="stop",
            usage=TokenUsage(input_tokens=9, output_tokens=8, cost_usd=0.001),
        )

    monkeypatch.setattr(ModelRouter, "stream", local_stream)
    item = await make_item(env.source)
    intent = await prepare_intent(env.source, item, "pio_" + "1" * 32)
    original = await settle(env.source.items.executor, intent)
    assert original["status"] == "completed"
    _, connected, _, _ = await pair(env)
    args = {
        "operation": "result",
        "connection_id": connected["connection"]["id"],
        "item_id": item["item_id"],
    }
    value = await env.service.model_result("result", {"forged": "NOT EVIDENCE"}, "main", args)
    assert value["availability"] == "available"
    assert value["result"]["answer"] == original["answer"]
    assert value["result"]["run_id"] == original["run_id"]
    assert value["result"]["job_id"] == original["job_id"]
    assert value["result"]["read_receipt"] is None
    assert value["result"]["artifacts"]
    for artifact in value["result"]["artifacts"]:
        assert hashlib.sha256(artifact["content_text"].encode()).hexdigest() == artifact["sha256"]
    current = await env.source.items.result(intent["occurrence_id"])
    assert current["read_receipt"] is None and current["delivery_id"] == original["delivery_id"]
    refs = await env.hub.personal_artifacts.session_refs(env.hub._session.id, "main")
    assert len(refs) == 1
    _, content = await env.hub.personal_artifacts.read(
        actor_id="main", artifact_id=refs[0]["artifact_id"], version=1
    )
    assert json.loads(content) == value
    await runtime.authorize_execution(env.hub)


async def test_old_broker_rejects_version4_and_revision_cannot_launder_read(
    federation_system, tmp_path, monkeypatch
):
    env = federation_system
    await make_item(env.source)
    granted, _, _, _ = await pair(env)
    await env.service.model_result("overview", {}, "main", {"operation": "overview"})
    artifacts = env.hub.personal_artifacts
    ref = (await artifacts.session_refs(env.hub._session.id, "main"))[0]
    store = artifacts.store
    with store._connection() as db:
        value = json.loads(
            db.execute(
                "SELECT record_json FROM artifact_versions WHERE artifact_id=?",
                (ref["artifact_id"],),
            ).fetchone()[0]
        )
    assert value["authority_version"] == 4
    baseline = subprocess.check_output(
        ["git", "show", "1b22de6d:src/naumi_agent/personal/artifact_store.py"]
    )
    namespace = {
        "__name__": "workspace_baseline_artifact_store",
        "__file__": str(tmp_path / "baseline.py"),
    }
    exec(compile(baseline, str(tmp_path / "baseline.py"), "exec"), namespace)
    old = namespace["ArtifactStore"](store.base_dir)
    with pytest.raises(PersonalAgentError):
        old.get(
            workspace_key=env.service.workspace_key,
            actor_id="main",
            artifact_id=ref["artifact_id"],
            version=1,
        )
    revised = await artifacts.publish(
        actor_id="main",
        request_id="revise-workspace-read",
        title="修订标题",
        kind="material",
        filename="workspaces.txt",
        content=b"Revised content",
        source_refs=[],
        evidence={},
        artifact_id=ref["artifact_id"],
        expected_version=1,
    )
    assert any(
        r["artifact_id"] == ref["artifact_id"] and r["version"] == 1 for r in revised["source_refs"]
    )
    await human(
        env.source_service,
        "revoke",
        payload={
            "request_id": "late-revoke",
            "grant_id": granted["grant"]["id"],
            "expected_revision": 1,
        },
    )
    with pytest.raises(PersonalAgentError):
        await artifacts.get(actor_id="main", artifact_id=revised["artifact_id"], version=2)


async def test_postcommit_projection_failure_preserves_accepted_identity(
    federation_system, monkeypatch
):
    env = federation_system
    recipient = (await human(env.service, "recipient", actor_id="main"))["recipient"]
    from datetime import timedelta

    payload = {
        "request_id": "issue-late",
        "recipient": recipient,
        "source_actor_id": "main",
        "source_actor_version": 1,
        "scopes": ["items"],
        "expires_at": (env.source.clock[0] + timedelta(days=1)).isoformat(),
    }
    original = env.source_service._ticket

    async def missing_ticket(grant):
        raise PersonalAgentError("credential_error", "late fixture failure")

    monkeypatch.setattr(env.source_service, "_ticket", missing_ticket)
    issued = await human(env.source_service, "issue", payload=payload)
    assert issued["accepted"] and issued["projection_available"] is False and "ticket" not in issued
    assert (await env.source_service.request_status("issue-late"))["identity"] == issued["identity"]
    monkeypatch.setattr(env.source_service, "_ticket", original)
    replay = await human(env.source_service, "issue", payload=payload)
    connect = {
        "request_id": "connect-late",
        "endpoint": "http://127.0.0.1:54321",
        "label": "新资料",
        "ticket": replay["ticket"],
    }

    async def offline_after_commit(*args, **kwargs):
        raise PersonalAgentError("source_unavailable", "late fixture failure")

    monkeypatch.setattr(env.service, "_remote", offline_after_commit)
    connected = await human(env.service, "connect", payload=connect)
    assert (
        connected["accepted"]
        and connected["projection_available"] is False
        and "connection" not in connected
    )
    assert (await env.service.request_status("connect-late"))["identity"] == connected["identity"]
    assert len((await env.service.list())["connections"]) == 1


async def test_native_catalog_replacement_and_source_store_replacement_do_not_rebind(
    federation_system,
):
    env = federation_system
    await make_item(env.source)
    await pair(env)
    path = env.source.engine.personal_agents.store.db_path
    replacement = path.with_suffix(".replacement")
    shutil.copy2(path, replacement)
    try:
        os.replace(replacement, path)
    except PermissionError:
        pytest.skip("Windows keeps this fixture SQLite handle open")
    value = await env.service.overview()
    assert value["partial"] and value["sources"][0]["entries"] == []
    assert value["sources"][0]["code"] == "source_stale"


async def test_revoke_between_projection_and_return_has_no_body(federation_system, monkeypatch):
    env = federation_system
    await make_item(env.source)
    granted, _, _, _ = await pair(env)
    original = env.source_service._save_source_read

    async def revoke_before_final(body, manifest, grant):
        result = await original(body, manifest, grant)
        await human(
            env.source_service,
            "revoke",
            payload={
                "request_id": "raced-revoke",
                "grant_id": granted["grant"]["id"],
                "expected_revision": 1,
            },
        )
        return result

    monkeypatch.setattr(env.source_service, "_save_source_read", revoke_before_final)
    value = await env.service.overview()
    assert value["partial"] and value["sources"][0]["entries"] == []


async def test_cross_workspace_request_lookup_does_not_expose_foreign_identity(federation_system):
    env = federation_system
    await pair(env)
    # Same owner, different workspace route: no federation records or original receipt leak.
    original = env.hub.personal_agents.workspace_key
    env.hub.personal_agents.workspace_key = "f" * 64
    try:
        foreign = PersonalWorkspaceService(
            env.hub, vault=WorkspaceVault(env.backend), transport=env.transport
        )
        assert (await foreign.list())["connections"] == []
        assert (await foreign.request_status("pair-connect")) == {
            "status": "missing",
            "request_id": "pair-connect",
        }
    finally:
        env.hub.personal_agents.workspace_key = original


async def test_source_deadline_covers_semaphore_wait_and_handler(federation_system, monkeypatch):
    from naumi_agent.personal import workspace_source

    env = federation_system
    monkeypatch.setattr(workspace_source, "FEDERATION_TIMEOUT_SECONDS", 0.01)

    async def stalled(*args, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(env.source_service, "_federation", stalled)
    with pytest.raises(PersonalAgentError) as exc:
        await env.source_service.federation({}, token="not-used")
    assert exc.value.code == "source_unavailable"


async def test_lease_release_cleanup_does_not_mask_a_committed_issue(
    federation_system, monkeypatch
):
    env = federation_system
    await env.source_service.ensure()
    original = env.source_service._mutate
    write = env.source_service._write

    async def committed_then_cleanup_fails(*args, **kwargs):
        receipt = await original(*args, **kwargs)

        async def missing_release(callback):
            raise PersonalAgentError("storage_error", "release fixture failure")

        monkeypatch.setattr(env.source_service, "_write", missing_release)
        return receipt

    monkeypatch.setattr(env.source_service, "_mutate", committed_then_cleanup_fails)
    from datetime import timedelta

    recipient = (await human(env.service, "recipient", actor_id="main"))["recipient"]
    payload = {
        "request_id": "release-issue",
        "recipient": recipient,
        "source_actor_id": "main",
        "source_actor_version": 1,
        "scopes": ["items"],
        "expires_at": (env.source.clock[0] + timedelta(days=1)).isoformat(),
    }
    granted = await human(env.source_service, "issue", payload=payload)
    assert granted["accepted"] is True
    assert env.source_service._last_lease_cleanup_error == "workspace_unknown"
    monkeypatch.setattr(env.source_service, "_write", write)
    assert (await env.source_service.request_status("release-issue"))["identity"] == granted[
        "identity"
    ]


async def test_observed_expiration_survives_clock_rollback_and_reopen(federation_system):
    from datetime import timedelta

    env = federation_system
    await make_item(env.source)
    await pair(env)
    before = env.source.clock[0]
    env.source.clock[0] += timedelta(days=2)
    assert (await env.service.overview())["sources"][0]["availability"] == "unavailable"
    env.source.clock[0] = before
    env.source.engine.personal_workspaces = env.source_service = PersonalWorkspaceService(
        env.source.engine, vault=WorkspaceVault(env.backend), clock=lambda: env.source.clock[0]
    )
    env.transport.source = env.source_service
    value = await env.service.overview()
    assert value["sources"][0]["availability"] == "unavailable"
    assert value["sources"][0]["code"] == "grant_revoked"
