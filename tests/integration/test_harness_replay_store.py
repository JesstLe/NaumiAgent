from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_python(source: str, *, workspace: Path, db_path: Path) -> str:
    env = os.environ.copy()
    source_root = Path(__file__).parents[2] / "src"
    env["PYTHONPATH"] = str(source_root)
    completed = subprocess.run(
        [sys.executable, "-c", source, str(workspace), str(db_path)],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return completed.stdout.strip()


CREATE_RUN = r"""
import asyncio
import hashlib
import json
import sys
from pathlib import Path

from naumi_agent.harness.checks import HarnessCheckResult, HarnessCheckStatus
from naumi_agent.harness.completion import HarnessCompletionReceipt, HarnessEvidenceRef
from naumi_agent.harness.models import HarnessCompletionContract, HarnessTaskKind
from naumi_agent.harness.store import HarnessStore

workspace = Path(sys.argv[1])
db_path = Path(sys.argv[2])
artifact = workspace / "artifacts" / "unit.txt"
artifact.parent.mkdir(parents=True, exist_ok=True)
artifact.write_bytes(b"verified integration artifact")
artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
tool_summary = {
    "tool_name": "dangerous_canary",
    "status": "success",
    "destructive": True,
    "start_missing": False,
    "permission_status": "bypass",
}
tool_sha = hashlib.sha256(json.dumps(
    tool_summary,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).encode()).hexdigest()

async def main():
    store = HarnessStore(db_path)
    contract = HarnessCompletionContract(
        run_id="cross-process-run",
        session_id="cross-process-session",
        task_kind=HarnessTaskKind.CHANGE,
        objective="跨进程验证安全 Replay",
    )
    await store.start_run(
        workspace_root=workspace,
        contract=contract,
        tree_fingerprint_before="a" * 64,
        started_at="2026-07-15T10:00:00+00:00",
    )
    await store.record_check(
        result=HarnessCheckResult(
            check_id="unit",
            run_id=contract.run_id,
            status=HarnessCheckStatus.PASSED,
            tree_fingerprint="b" * 64,
            profile_digest="c" * 64,
            message="真实检查已在原运行完成",
            exit_code=0,
            duration_ms=10,
        ),
        argv=("python3", "-m", "pytest", "tests/unit/test_small.py"),
        cwd=workspace,
        started_at="2026-07-15T10:00:01+00:00",
        completed_at="2026-07-15T10:00:02+00:00",
        artifact_path="artifacts/unit.txt",
    )
    await store.record_evidence(
        run_id=contract.run_id,
        evidence=HarnessEvidenceRef(
            id="artifact-evidence",
            kind="test_report",
            summary="真实 artifact digest",
        ),
        uri="artifact://artifacts/unit.txt",
        sha256=artifact_sha,
        summary={"status": "passed"},
        producer="integration_test",
        created_at="2026-07-15T10:00:03+00:00",
    )
    await store.record_evidence(
        run_id=contract.run_id,
        evidence=HarnessEvidenceRef(
            id="tool-evidence",
            kind="tool_execution",
            summary="仅保存破坏性工具的规范化历史事实",
        ),
        uri="chat-run://cross-process-run/tool/tool-evidence",
        sha256=tool_sha,
        summary=tool_summary,
        producer="integration_test",
        created_at="2026-07-15T10:00:04+00:00",
    )
    await store.finish_run(
        run_id=contract.run_id,
        receipt=HarnessCompletionReceipt(
            run_id=contract.run_id,
            status="completed_verified",
            task_kind=HarnessTaskKind.CHANGE,
            changed_files=("source.py",),
            checks=(),
            criteria=(),
            warnings=(),
            tree_fingerprint="b" * 64,
        ),
        completed_at="2026-07-15T10:00:05+00:00",
    )
    print("created")

asyncio.run(main())
"""


REPLAY_RUN = r"""
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from naumi_agent.harness.checks import HarnessCheckRunner
from naumi_agent.harness.service import HarnessService, render_harness_replay
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore

workspace = Path(sys.argv[1])
db_path = Path(sys.argv[2])

async def forbidden(*args, **kwargs):
    raise AssertionError("Replay must not execute a Harness check")

HarnessCheckRunner.run = forbidden

async def main():
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(db_path.with_name("trust.db")),
        store=HarnessStore(db_path),
    )
    lookup = await service.replay_run("cross-process-run")
    print(json.dumps({
        "lookup": lookup.status,
        "result": asdict(lookup.result) if lookup.result else None,
        "receipt": render_harness_replay(lookup),
    }, ensure_ascii=False, sort_keys=True))

asyncio.run(main())
"""


def test_real_git_sqlite_run_replays_across_processes_without_execution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "replay@example.com"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Replay Test"],
        cwd=workspace,
        check=True,
    )
    (workspace / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=workspace, check=True)
    db_path = tmp_path / "state" / "harness.db"

    assert _run_python(CREATE_RUN, workspace=workspace, db_path=db_path) == "created"
    first = json.loads(_run_python(REPLAY_RUN, workspace=workspace, db_path=db_path))
    second = json.loads(_run_python(REPLAY_RUN, workspace=workspace, db_path=db_path))

    assert first == second
    assert first["lookup"] == "ok"
    assert first["result"]["status"] == "reproduced"
    assert "Harness 安全回放" in first["receipt"]
    assert "已复现" in first["receipt"]
    assert not (workspace / "dangerous-canary-ran").exists()

    artifact = workspace / "artifacts" / "unit.txt"
    artifact.unlink()
    missing = json.loads(_run_python(REPLAY_RUN, workspace=workspace, db_path=db_path))
    assert missing["result"]["status"] == "partial"
    assert "verified integration artifact" not in missing["receipt"]

    artifact.write_bytes(b"tampered")
    tampered = json.loads(_run_python(REPLAY_RUN, workspace=workspace, db_path=db_path))
    assert tampered["result"]["status"] == "corrupt"
    assert "tampered" not in tampered["receipt"]
