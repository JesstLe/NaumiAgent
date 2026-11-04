"""Private calculations use real Docker isolation, not a native-host substitute."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid

import pytest

from naumi_agent.personal import artifact_process as process
from naumi_agent.personal.models import PersonalAgentError


@pytest.fixture
def workspace(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "budget.csv").write_text("income,expense\n1200,375\n", "utf-8")
    for directory in (".naumi", ".git", "vault", "node_modules"):
        (work / directory).mkdir()
        (work / directory / "private.txt").write_text("PRIVATE_SENTINEL_" + directory, "utf-8")
    return work


@pytest.fixture
def docker_ready():
    if shutil.which("docker") is None:
        pytest.skip("Docker executable is not present")
    ready = subprocess.run(
        ["docker", "image", "inspect", "python:3.12-slim", "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if ready.returncode != 0:
        pytest.skip("Existing Docker daemon/python image is not ready; no pull attempted")
    return ready.stdout.strip()


def _absent(identity):
    result = subprocess.run(
        ["docker", "container", "inspect", identity],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and "No such" in result.stderr


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "../private",
        "/absolute",
        "a/../b",
        "a//b",
        "a/",
        "a\\b",
        "C:/private",
        "a\0b",
        "a\nb",
        "a\x7fb",
        "file.",
        "file ",
        "NUL",
        "CON.txt",
        "aux.json",
        "LPT1.txt",
        "com3",
        "x/" * 17 + "a",
        "x" * 241,
    ],
)
def test_input_file_names_reject_alias_and_traversal(name):
    with pytest.raises(PersonalAgentError) as caught:
        process._relative(name)
    assert caught.value.code == "invalid_input"


@pytest.mark.parametrize("name", ["budget.csv", "研究/材料.csv", "docs/report-v1.txt"])
def test_valid_relative_names_are_preserved(name):
    assert process._relative(name) == name


@pytest.mark.parametrize(
    "arguments",
    [
        {"command": ""},
        {"command": "  "},
        {"command": "x\0y"},
        {"command": "x" * 100001},
        {"language": "powershell"},
        {"timeout_s": 0},
        {"timeout_s": True},
        {"timeout_s": 301},
        {"timeout_s": float("nan")},
        {"private": 1},
        {"max_output_bytes": 0},
        {"max_output_bytes": True},
        {"max_output_bytes": 8388609},
        {"image": "--privileged"},
        {"image": "python:3.12-slim -v /:/host"},
        {"inputs": {"a": "not-bytes"}},
        {"inputs": {"a": b"", "A": b""}},
        {"inputs": {"a": b"", "a/b": b""}},
        {"inputs": {"../outside": b""}},
        {"protected_roots": "not-a-list"},
    ],
)
async def test_invalid_calls_fail_before_any_process(workspace, arguments, monkeypatch):
    async def denied(*args, **kwargs):
        raise AssertionError("Invalid inputs must not spawn a process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", denied)
    with pytest.raises(PersonalAgentError) as caught:
        await process.execute_private_process(
            workspace=workspace, **{"command": "echo ok", **arguments}
        )
    assert caught.value.code == "invalid_input"


async def test_missing_docker_never_runs_on_host(workspace, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(PersonalAgentError) as caught:
        await process.execute_private_process(workspace=workspace, command="echo not executed")
    assert caught.value.code == "isolation_unavailable"


def test_snapshot_excludes_state_dependencies_and_private_roots(workspace, tmp_path):
    snapshot = tmp_path / "snapshot"
    info, _ = process._prepare_workspace(workspace, snapshot, True, [workspace / "vault"])
    assert sorted(path.name for path in snapshot.iterdir()) == ["budget.csv"]
    assert set(info["excluded_dirs"]) == {".naumi", ".git", "node_modules", "vault"}
    assert (snapshot / "budget.csv").read_bytes() == (workspace / "budget.csv").read_bytes()


def test_snapshot_rejects_workspace_inside_private_root(workspace, tmp_path):
    with pytest.raises(PersonalAgentError) as caught:
        process._prepare_workspace(workspace, tmp_path / "snapshot", True, [workspace.parent])
    assert caught.value.code == "isolation_denied"


def test_snapshot_limit_rejects_instead_of_truncating(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(process, "_SNAPSHOT_BYTES", 1)
    with pytest.raises(PersonalAgentError) as caught:
        process._prepare_workspace(workspace, tmp_path / "snapshot", True, [workspace / "vault"])
    assert caught.value.code == "workspace_limit"


@pytest.mark.parametrize("target_kind", ["outside", "private", "cycle"])
def test_snapshot_rejects_link_escape_and_cycles(workspace, tmp_path, target_kind):
    target = {"outside": tmp_path, "private": workspace / "vault", "cycle": workspace}[target_kind]
    link = workspace / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    with pytest.raises(PersonalAgentError) as caught:
        process._prepare_workspace(workspace, tmp_path / "snapshot", True, [workspace / "vault"])
    assert caught.value.code == "isolation_denied"


def test_outputs_reject_hardlink_even_within_output_directory(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "original").write_bytes(b"a")
    os.link(output / "original", output / "linked")
    with pytest.raises(PersonalAgentError) as caught:
        process._collect_outputs(output)
    assert caught.value.code == "invalid_output"


async def test_cancelling_disk_copy_waits_for_owned_operation_before_cleanup(monkeypatch):
    import threading

    entered, release, completed = threading.Event(), threading.Event(), threading.Event()

    def operation():
        entered.set()
        release.wait(5)
        completed.set()

    task = asyncio.create_task(process._disk(operation))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()


@pytest.mark.integration
async def test_real_container_reads_exact_data_masks_private_state_and_captures_output(
    workspace,
    docker_ready,
    monkeypatch,
):
    monkeypatch.setenv("PA08_PRIVATE_PARENT_SENTINEL", "MUST_NOT_ENTER_CONTAINER")
    before = (workspace / "budget.csv").read_bytes()
    code = """
import csv,json,os,pathlib,socket
row=next(csv.DictReader(pathlib.Path('/inputs/finance/budget.csv').open()))
assert int(row['income'])-int(row['expense'])==825
assert pathlib.Path('/workspace/budget.csv').read_text().startswith('income,expense')
for name in ['.naumi','.git','vault','node_modules']:
 assert not (pathlib.Path('/workspace')/name).exists(),name
assert 'PA08_PRIVATE_PARENT_SENTINEL' not in os.environ
assert os.getuid()==65534
status=pathlib.Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\\t1' in status
for target in ['/workspace/budget.csv','/inputs/finance/budget.csv','/root-write']:
 try:pathlib.Path(target).write_text('forbidden')
 except OSError:pass
 else:raise AssertionError(target)
assert not pathlib.Path('/var/run/docker.sock').exists()
try:socket.create_connection(('host.docker.internal',18768),timeout=.25)
except OSError:pass
else:raise AssertionError('host API connection')
pathlib.Path('/outputs/report.txt').write_text('余额825元',encoding='utf-8')
print(json.dumps({'result':825,'private':'denied','parent_env':'absent'}))
"""
    result = await process.execute_private_process(
        workspace=workspace,
        command=code,
        language="python",
        inputs={"finance/budget.csv": before},
        protected_roots=[workspace / "vault"],
    )
    assert result["exit_code"] == 0, result
    assert json.loads(result["stdout"])["result"] == 825
    assert result["files"] == [{"path": "report.txt", "content": "余额825元".encode()}]
    assert result["isolation"]["image_id"] == docker_ready
    assert result["isolation"]["workspace_snapshot"] is True
    assert result["isolation"]["container_removed"] is True
    assert (workspace / "budget.csv").read_bytes() == before
    assert not (workspace / "report.txt").exists()
    _absent(result["isolation"]["container_id"])


@pytest.mark.integration
async def test_real_shell_and_nonzero_exit_are_not_reported_as_success(workspace, docker_ready):
    result = await process.execute_private_process(
        workspace=workspace,
        command="printf 'actual stdout'; printf 'actual stderr' >&2; exit 7",
        protected_roots=[workspace / "vault"],
    )
    assert result["stdout"] == "actual stdout" and result["stderr"] == "actual stderr"
    assert result["exit_code"] == 7
    assert result["files"] == []
    _absent(result["isolation"]["container_id"])


@pytest.mark.integration
async def test_real_stdout_flood_is_bounded_and_container_is_still_cleaned(workspace, docker_ready):
    result = await process.execute_private_process(
        workspace=workspace,
        command="print('x'*100000)",
        language="python",
        max_output_bytes=128,
        protected_roots=[workspace / "vault"],
    )
    assert result["exit_code"] == 0 and len(result["stdout"].encode()) == 128
    assert result["stdout_truncated"] is True
    _absent(result["isolation"]["container_id"])


@pytest.mark.integration
@pytest.mark.parametrize(
    "code",
    [
        "from pathlib import Path; [(Path('/outputs')/str(i)).write_text('x') for i in range(33)]",
        "from pathlib import Path; f=Path('/outputs/large').open('wb'); "
        "f.truncate(21*1024*1024); f.close()",
    ],
)
async def test_real_excess_output_is_rejected_after_container_cleanup(
    workspace,
    docker_ready,
    monkeypatch,
    code,
):
    identity = []
    original = process._command

    async def observe(docker, args, **kwargs):
        if args[0] == "start":
            identity.append(args[-1])
        return await original(docker, args, **kwargs)

    monkeypatch.setattr(process, "_command", observe)
    with pytest.raises(PersonalAgentError) as caught:
        await process.execute_private_process(
            workspace=workspace,
            command=code,
            language="python",
            protected_roots=[workspace / "vault"],
        )
    assert caught.value.code == "output_limit"
    assert len(identity) == 1
    _absent(identity[0])


@pytest.mark.integration
async def test_real_timeout_removes_only_its_own_container(workspace, docker_ready, monkeypatch):
    identity = []
    original = process._command

    async def observe(docker, args, **kwargs):
        if args[0] == "start":
            identity.append(args[-1])
        return await original(docker, args, **kwargs)

    monkeypatch.setattr(process, "_command", observe)
    with pytest.raises(PersonalAgentError) as caught:
        await process.execute_private_process(
            workspace=workspace,
            command="import time;time.sleep(30)",
            language="python",
            timeout_s=0.25,
            protected_roots=[workspace / "vault"],
        )
    assert caught.value.code == "execution_timeout"
    assert caught.value.isolation["container_removed"] is True
    assert len(identity) == 1
    _absent(identity[0])


@pytest.mark.integration
async def test_real_running_container_cancellation_is_cleaned_before_return(
    workspace,
    docker_ready,
    monkeypatch,
):
    created = asyncio.Event()
    identity = []
    original = process._command

    async def observe(docker, args, **kwargs):
        if args[0] == "start":
            identity.append(args[-1])
            created.set()
        return await original(docker, args, **kwargs)

    monkeypatch.setattr(process, "_command", observe)
    task = asyncio.create_task(
        process.execute_private_process(
            workspace=workspace,
            command="import time;time.sleep(30)",
            language="python",
            protected_roots=[workspace / "vault"],
        )
    )
    try:
        await asyncio.wait_for(created.wait(), timeout=10)
        for _ in range(50):
            running = await original(
                shutil.which("docker"),
                ["inspect", identity[0], "--format", "{{.State.Running}}"],
            )
            if running[0] == 0 and running[1].strip() == "true":
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("Expected actual running container before cancellation")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        _absent(identity[0])
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.integration
async def test_missing_local_image_has_no_native_fallback(workspace, docker_ready):
    with pytest.raises(PersonalAgentError) as caught:
        await process.execute_private_process(
            workspace=workspace,
            command="echo must-not-run",
            image="naumi-pa08-missing:" + uuid.uuid4().hex,
            protected_roots=[workspace / "vault"],
        )
    assert caught.value.code == "isolation_unavailable"


def test_writable_mode_requires_existing_mask_points(workspace, tmp_path):
    (workspace / ".git" / "private.txt").unlink()
    (workspace / ".git").rmdir()
    with pytest.raises(PersonalAgentError) as caught:
        process._prepare_workspace(workspace, tmp_path / "logical", False, [workspace / "vault"])
    assert caught.value.code == "isolation_denied"


@pytest.mark.integration
async def test_real_writable_workspace_masks_existing_private_directories(workspace, docker_ready):
    workspace.chmod(0o777)
    code = """
from pathlib import Path
for name in ['.naumi','.git','vault']:
 assert list((Path('/workspace')/name).iterdir())==[],name
Path('/workspace/public-result.txt').write_text('actual public write')
try:Path('/workspace/vault/should-not-write').write_text('denied')
except OSError:pass
else:raise AssertionError('private mask write')
"""
    result = await process.execute_private_process(
        workspace=workspace,
        private=False,
        command=code,
        language="python",
        protected_roots=[workspace / "vault"],
    )
    assert result["exit_code"] == 0, result
    assert (workspace / "public-result.txt").read_text() == "actual public write"
    assert (workspace / "vault/private.txt").read_text() == "PRIVATE_SENTINEL_vault"
    assert not (workspace / "vault/should-not-write").exists()
    _absent(result["isolation"]["container_id"])


async def test_cleanup_transport_failure_is_unknown_and_does_not_remove_others(monkeypatch):
    calls = []

    async def broken(docker, args, **kwargs):
        calls.append(args)
        raise OSError("controlled unavailable daemon")

    monkeypatch.setattr(process, "_command", broken)
    with pytest.raises(PersonalAgentError) as caught:
        await process._cleanup("docker", "naumi-artifact-fixture", "fixture")
    assert caught.value.code == "cleanup_unknown"
    assert len(calls) == 1 and calls[0][:2] == ["container", "ls"]


async def test_cleanup_rejects_foreign_label_before_any_remove(monkeypatch):
    calls = []

    async def records(docker, args, **kwargs):
        calls.append(args)
        if args[:2] == ["container", "ls"]:
            return 0, "a" * 64, "", False, False
        assert args[0] == "inspect"
        return 0, json.dumps({process._LABEL: "foreign"}), "", False, False

    monkeypatch.setattr(process, "_command", records)
    with pytest.raises(PersonalAgentError) as caught:
        await process._cleanup("docker", "naumi-artifact-fixture", "fixture")
    assert caught.value.code == "cleanup_unknown"
    assert len(calls) == 2 and not any(args[0] == "rm" for args in calls)


@pytest.mark.parametrize(
    "endpoint",
    [
        "tcp://127.0.0.1:2375",
        "tcp://remote:2376",
        "ssh://remote",
        "npipe:////remote/pipe/docker",
    ],
)
async def test_remote_docker_host_is_rejected_without_sending_private_paths(monkeypatch, endpoint):
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", endpoint)

    async def denied(*args, **kwargs):
        raise AssertionError("A remote endpoint must be rejected before any command")

    monkeypatch.setattr(process, "_command", denied)
    with pytest.raises(PersonalAgentError) as caught:
        await process._require_local_engine("docker")
    assert caught.value.code == "isolation_unavailable"


async def test_explicit_docker_context_overrides_host_environment(monkeypatch):
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")

    async def context(docker, args, **kwargs):
        assert args[:3] == ["context", "inspect", "desktop-linux"]
        return 0, json.dumps("npipe:////./pipe/dockerDesktopLinuxEngine"), "", False, False

    monkeypatch.setattr(process, "_command", context)
    assert await process._require_local_engine("docker") == "local_pipe"


@pytest.mark.integration
async def test_real_javascript_uses_explicit_existing_node_image(workspace, docker_ready):
    available = subprocess.run(
        ["docker", "image", "inspect", "node:24-bookworm-slim", "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if available.returncode != 0:
        pytest.skip("Existing Node image is not ready; no pull attempted")
    result = await process.execute_private_process(
        workspace=workspace,
        language="javascript",
        image="node:24-bookworm-slim",
        command="const fs=require('node:fs');fs.writeFileSync('/outputs/js.txt','825');"
        "console.log(1200-375);",
        protected_roots=[workspace / "vault"],
    )
    assert result["exit_code"] == 0, result
    assert result["stdout"].strip() == "825"
    assert result["files"] == [{"path": "js.txt", "content": b"825"}]
    assert result["isolation"]["image_id"] == available.stdout.strip()
    _absent(result["isolation"]["container_id"])
