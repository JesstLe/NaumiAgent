"""Exact item calculation boundaries, including actual Docker empty-workspace execution."""

import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from naumi_agent.personal.artifact_boundary import ArtifactToolProxy, guarded_tool
from naumi_agent.personal.artifact_process import execute_private_process
from naumi_agent.personal.models import PersonalAgentError


async def test_cancel_during_calculation_prevents_all_output_publication(tmp_path, monkeypatch):
    service = SimpleNamespace(
        authorize_engine=AsyncMock(),
        engine_actor=lambda: "main",
        publish=AsyncMock(),
        runtime_boundary_marker=tmp_path / "marker",
    )
    context = SimpleNamespace(
        process_workspace=tmp_path,
        force_private=True,
        authorize=AsyncMock(side_effect=PersonalAgentError("execution_fenced", "租约已失效")),
    )
    engine = SimpleNamespace(
        personal_artifacts=service,
        _session=SimpleNamespace(id="execution"),
        workspace_root=tmp_path,
        _personal_item_context=context,
        _personal_artifact_refs=[],
        personal_agents=SimpleNamespace(store=SimpleNamespace(base_dir=tmp_path / "private")),
        config=SimpleNamespace(
            memory=SimpleNamespace(session_db_path=str(tmp_path / "sessions.db"))
        ),
    )
    observed = {}

    async def calculation(**kwargs):
        observed.update(kwargs)
        return {
            "stdout": "done",
            "stderr": "",
            "exit_code": 0,
            "isolation": {},
            "files": [{"path": "report.md", "content": b"must-not-publish"}],
        }

    monkeypatch.setattr(
        "naumi_agent.personal.artifact_process.execute_private_process",
        calculation,
    )
    proxy = ArtifactToolProxy(engine, SimpleNamespace(name="code_execute"), "call-1")
    with pytest.raises(PersonalAgentError) as exc:
        await proxy._process("main", [], {"code": "print(1)"})
    assert exc.value.code == "execution_fenced"
    service.publish.assert_not_awaited()
    assert observed["empty_workspace"] is True
    assert observed["private"] is True


def test_empty_input_item_still_gets_guarded_without_existing_vault():
    tool = SimpleNamespace(name="code_execute")
    engine = SimpleNamespace(
        personal_artifacts=SimpleNamespace(enabled=False),
        _personal_item_context=object(),
    )
    assert isinstance(guarded_tool(engine, tool, "call"), ArtifactToolProxy)


async def test_empty_workspace_mode_rejects_writable_or_nonbool_arguments(tmp_path):
    for kwargs in ({"empty_workspace": 1}, {"empty_workspace": True, "private": False}):
        with pytest.raises(PersonalAgentError) as exc:
            await execute_private_process(workspace=tmp_path, command="echo test", **kwargs)
        assert exc.value.code == "invalid_input"


async def test_actual_docker_item_sees_64_inputs_and_zero_host_workspace_files(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("Docker executable is not present")
    ready = subprocess.run(
        ["docker", "image", "inspect", "python:3.12-slim", "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if ready.returncode:
        pytest.skip("Existing local Docker image is not ready; no image pull attempted")
    protected = tmp_path / "runtime" / "empty-input-workspace"
    protected.mkdir(parents=True)
    (protected / "host-secret.txt").write_text("HOST_SECRET_NOT_AUTHORIZED", encoding="utf-8")
    result = await execute_private_process(
        workspace=protected,
        protected_roots=[protected.parent],
        private=True,
        empty_workspace=True,
        language="python",
        inputs={f"input-{i}.txt": str(i).encode() for i in range(64)},
        command=(
            "from pathlib import Path\nimport json\n"
            "files=list(Path('/inputs').glob('*'))\n"
            "assert list(Path('/workspace').iterdir()) == []\n"
            "answer={'count':len(files),'sum':sum(int(p.read_text()) for p in files)}\n"
            "Path('/outputs/result.json').write_text(json.dumps(answer))\n"
            "print(json.dumps(answer))\n"
        ),
    )
    assert result["exit_code"] == 0
    assert json.loads(result["stdout"]) == {"count": 64, "sum": 2016}
    assert json.loads(result["files"][0]["content"]) == {"count": 64, "sum": 2016}
    assert result["isolation"]["container_removed"] is True
    assert (protected / "host-secret.txt").read_text("utf-8") == "HOST_SECRET_NOT_AUTHORIZED"
