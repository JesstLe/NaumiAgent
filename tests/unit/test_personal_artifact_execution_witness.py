"""Independent privacy witnesses and exact Run bindings over real persistence."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from naumi_agent.model.router import ModelResponse, ModelRouter, TokenUsage
from naumi_agent.personal import runtime
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tools.base import ToolCall
from tests.unit.test_personal_artifacts import artifacts as artifacts

pytestmark = pytest.mark.usefixtures("runtime_payload_key")


@pytest.mark.parametrize("replacement", [False, True])
async def test_main_private_boundary_survives_entire_personal_directory_loss(
    artifacts, tmp_path, replacement
):
    engine = artifacts.engine
    sid = engine._session.id
    assert runtime.current_agent(engine) is None
    value = await artifacts.service.publish(
        actor_id="main", request_id="main-private-input", title="私有资料",
        kind="material", filename="private.txt", content=b"PRIVATE_MAIN_825",
    )
    result = await engine.execute_tool(ToolCall(
        id="main-read", name="personal_artifacts",
        arguments={"operation": "read", "artifact_id": value["artifact_id"], "version": 1},
    ))
    assert result.status == "success"
    engine._append_message({
        "role": "assistant", "content": "PRIVATE_MAIN_825",
        "metadata": {"artifact_refs": [artifacts.service.reference(value)]},
    })
    await engine._save_session()
    marker = Path(artifacts.config.memory.session_db_path).resolve().with_name(
        f".artifact-boundary-{artifacts.service.workspace_key}-v1"
    )
    assert not marker.is_relative_to(engine.personal_agents.store.base_dir)
    original = engine.personal_agents.store.base_dir.resolve()
    backup = original.with_name("personal-lost-fixture")
    assert original.is_relative_to(tmp_path) and backup.is_relative_to(tmp_path)
    original.rename(backup)
    fresh = None
    try:
        config = artifacts.config.model_copy(deep=True)
        if replacement:
            config.personal.data_dir = str(original.with_name("personal-new-fixture"))
        fresh = create_agent_engine(config)
        assert await fresh.load_session(sid)
        assert "PRIVATE_MAIN_825" in json.dumps(fresh._messages)
        assert fresh.personal_artifacts.enabled
        with pytest.raises(PersonalAgentError) as error:
            runtime.begin_turn(fresh)
        assert error.value.code == "storage_error"
        with pytest.raises(PersonalAgentError) as error:
            await fresh.personal_artifacts.authorize_engine()
        assert error.value.code == "storage_error"
        assert not fresh.personal_artifacts.store.db_path.exists()
    finally:
        if fresh is not None:
            await fresh.shutdown()
        if original.exists():
            recreated = original.with_name("personal-reinitialized-fixture")
            assert recreated.is_relative_to(tmp_path) and not recreated.exists()
            original.rename(recreated)
        backup.rename(original)
    assert marker.read_bytes() == b"naumi-private-artifact-boundary-v1\n"


def call(name, arguments, identity):
    return {
        "id": identity, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


@pytest.mark.parametrize("operation", ["publish", "process"])
async def test_nonstreamed_run_owns_artifacts_and_never_borrows_stale_running_run(
    artifacts, monkeypatch, operation
):
    if operation == "process" and (
        shutil.which("docker") is None
        or subprocess.run(
            ["docker", "image", "inspect", "python:3.12-slim"],
            capture_output=True, timeout=10,
        ).returncode != 0
    ):
        pytest.skip("Actual Docker daemon and existing Python image are required")
    engine, service = artifacts.engine, artifacts.service
    actor, sid = artifacts.profiles[0].id, artifacts.sessions[0]
    value = await service.publish(
        actor_id=actor, request_id="run-private-input", title="预算输入",
        kind="material", filename="budget.csv", content=b"income,expense\n1200,375\n",
    )
    assert await engine.load_session(sid)
    stale = await engine.chat_run_store.start_run(
        session_id=sid, user_message_id="stale-request-no-executor",
    )

    async def model(self, **kwargs):
        tools = [message for message in kwargs["messages"] if message["role"] == "tool"]
        if not tools:
            actions = [call("personal_artifacts", {
                "operation": "read", "artifact_id": value["artifact_id"], "version": 1,
            }, "read-private")]
        elif len(tools) == 1:
            read = json.loads(tools[0]["content"])
            if operation == "publish":
                actions = [call("personal_artifacts", {
                    "operation": "publish", "request_id": "nonstream-result",
                    "title": "真实资料引用", "filename": "report.md",
                    "content": "原始收入1200、支出375，结余825。",
                }, "publish-private")]
            else:
                code = (
                    "import csv;from pathlib import Path;"
                    f"r=next(csv.DictReader(Path({read['input_path']!r}).open()));"
                    "n=int(r['income'])-int(r['expense']);"
                    "Path('/outputs/report.txt').write_text(str(n));print(n)"
                )
                actions = [call("code_execute", {
                    "language": "python", "code": code, "timeout": 20,
                }, "calculate-private")]
        else:
            return ModelResponse(
                content="实际资料处理完成，结果825已保存。", finish_reason="stop",
                usage=TokenUsage(input_tokens=3, output_tokens=3),
            )
        return ModelResponse(content="", tool_calls=actions, finish_reason="tool_calls")

    monkeypatch.setattr(ModelRouter, "call", model)
    response = await artifacts.client.post(
        f"/sessions/{sid}/messages",
        json={"content": "实际读取并保存预算产物。", "stream": False},
    )
    assert response.status_code == 200, response.text
    runs = await engine.chat_run_store.list_runs(sid, limit=10)
    current = [row for row in runs if row.id != stale.id]
    assert len(current) == 1 and current[0].status == "completed"
    assert current[0].receipt.outcome == "completed"
    outputs = await service.list(actor_id=actor, kind="output")
    assert len(outputs) == 1
    output = outputs[0]
    assert output["evidence"]["run_id"] == current[0].id
    assert output["evidence"]["session_id"] == sid
    assert output["source_refs"] == [service.reference(value)]
    untouched = await engine.chat_run_store.get_run(sid, stale.id)
    assert untouched.status == "running" and untouched.artifacts == []
    if operation == "process":
        assert len(current[0].artifacts) == 1
        evidence = current[0].artifacts[0].summary
        assert evidence["exit_code"] == 0 and evidence["stdout"].strip() == "825"
    assert runtime.current_run_id(engine, sid) == ""


async def test_manual_publish_does_not_claim_a_stale_running_run(artifacts):
    engine, service = artifacts.engine, artifacts.service
    actor, sid = artifacts.profiles[0].id, artifacts.sessions[0]
    assert await engine.load_session(sid)
    stale = await engine.chat_run_store.start_run(session_id=sid, user_message_id="old-request")
    result = await engine.execute_tool(ToolCall(
        id="manual-publish", name="personal_artifacts", arguments={
            "operation": "publish", "request_id": "manual-result", "title": "手动结果",
            "filename": "manual.txt", "content": "真实手动输入",
        },
    ), agent_name="cli")
    assert result.status == "success", result
    output = (await service.list(actor_id=actor))[0]
    assert "run_id" not in output["evidence"]
    assert (await engine.chat_run_store.get_run(sid, stale.id)).status == "running"


async def test_manual_http_command_binds_its_explicit_run_context(artifacts):
    engine, service = artifacts.engine, artifacts.service
    actor, sid = artifacts.profiles[0].id, artifacts.sessions[0]
    payload = {
        "request_id": "http-command-result", "title": "手动命令结果",
        "filename": "command.txt", "content": "持久化实际命令输入",
    }
    response = await artifacts.client.post(
        f"/sessions/{sid}/commands",
        json={"command": "/library publish-json '" + json.dumps(payload, ensure_ascii=False) + "'"},
    )
    assert response.status_code == 200, response.text
    outputs = await service.list(actor_id=actor)
    assert len(outputs) == 1, response.text
    runs = await engine.chat_run_store.list_runs(sid)
    assert len(runs) == 1 and runs[0].status == "completed"
    assert outputs[0]["evidence"]["run_id"] == runs[0].id
    assert outputs[0]["evidence"]["session_id"] == sid
    assert runtime.current_run_id(engine, sid) == ""
