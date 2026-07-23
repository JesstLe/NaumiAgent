from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from naumi_agent.harness.models import (
    HarnessCheckSpec,
    HarnessEvalSpec,
    HarnessProfile,
)
from naumi_agent.harness.sandbox_request import (
    HarnessSandboxEvalRequest,
    HarnessSandboxEvalRequestBuilder,
    HarnessSandboxEvalRequestError,
    validate_request_checks,
)
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoreError,
)


def _git(workspace: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
    ).stdout


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "harness@example.invalid")
    _git(workspace, "config", "user.name", "Harness Test")
    (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(workspace, "add", "module.py")
    _git(workspace, "commit", "-qm", "initial")
    return workspace


def _profile(
    *,
    check: HarnessCheckSpec | None = None,
    max_duration_seconds: int = 600,
) -> HarnessProfile:
    return HarnessProfile(
        schema_version=1,
        checks=(
            check
            or HarnessCheckSpec(
                id="unit",
                label="定向单测",
                argv=("uv", "run", "pytest", "-q", "tests/unit/test_one.py"),
                timeout_seconds=30,
                provides=("unit",),
            ),
        ),
        evals=HarnessEvalSpec(max_duration_seconds=max_duration_seconds),
    )


def _build(
    workspace: Path,
    *,
    profile: HarnessProfile | None = None,
    profile_digest: str = "a" * 64,
    batch_id: str = "sandbox-batch-1",
    requested_samples: int = 5,
) -> HarnessSandboxEvalRequest:
    return HarnessSandboxEvalRequestBuilder().build(
        workspace_root=workspace,
        profile=profile or _profile(),
        profile_digest=profile_digest,
        profile_trusted=True,
        check_ids=("unit",),
        batch_id=batch_id,
        requested_samples=requested_samples,
    )


def test_sandbox_request_compiles_clean_git_and_profile_authority(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    profile = _profile()

    first = _build(workspace, profile=profile)
    repeated = _build(workspace, profile=profile)
    second_batch = _build(workspace, profile=profile, batch_id="sandbox-batch-2")

    revision = _git(workspace, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    tree_listing = _git(
        workspace,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        revision,
    )
    assert first == repeated
    assert first.request_id.startswith("hseval_")
    assert first.authority_key == first.request_sha256
    assert first.workspace_root == str(workspace.resolve())
    assert first.lane == "sandbox"
    assert first.source_revision == revision
    assert first.source_tree_sha256 == hashlib.sha256(tree_listing).hexdigest()
    assert first.checks[0].check_id == "unit"
    assert first.check_timeout_seconds_per_sample == 30
    assert first.max_total_duration_seconds == 600
    assert first.suite_id == second_batch.suite_id
    assert first.request_sha256 != second_batch.request_sha256
    assert validate_request_checks(first, profile) == profile.checks

    with pytest.raises(ValidationError, match="摘要不一致"):
        HarnessSandboxEvalRequest.model_validate(
            first.model_copy(update={"batch_id": "tampered"}).model_dump(mode="json")
        )


@pytest.mark.asyncio
async def test_request_manifest_survives_restart_and_fences_batch_identity(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    db_path = tmp_path / "harness.db"
    request = _build(workspace)
    first = await HarnessStore(db_path).record_sandbox_eval_request(
        request,
        created_at="2026-07-23T01:00:00+00:00",
    )
    repeated = await HarnessStore(db_path).record_sandbox_eval_request(
        request,
        created_at="2026-07-23T01:01:00+00:00",
    )
    restored = await HarnessStore(db_path).get_sandbox_eval_request(
        workspace,
        request.request_sha256,
    )

    assert repeated == first
    assert restored == first
    assert restored is not None
    assert restored.created_at == "2026-07-23T01:00:00+00:00"
    assert restored.request == request

    drifted = _build(workspace, profile_digest="b" * 64)
    with pytest.raises(HarnessStoreConflictError, match="batch"):
        await HarnessStore(db_path).record_sandbox_eval_request(
            drifted,
            created_at="2026-07-23T01:02:00+00:00",
        )


@pytest.mark.asyncio
async def test_request_manifest_detects_persisted_content_tampering(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    db_path = tmp_path / "harness.db"
    request = _build(workspace)
    store = HarnessStore(db_path)
    await store.record_sandbox_eval_request(
        request,
        created_at="2026-07-23T01:00:00+00:00",
    )
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            UPDATE harness_sandbox_eval_requests
            SET request_json = replace(request_json, ?, ?)
            WHERE request_sha256 = ?
            """,
            ("sandbox-batch-1", "sandbox-batch-X", request.request_sha256),
        )
        db.commit()

    with pytest.raises(HarnessStoreError, match="损坏"):
        await HarnessStore(db_path).get_sandbox_eval_request(
            workspace,
            request.request_sha256,
        )


@pytest.mark.asyncio
async def test_request_manifest_concurrent_process_facades_converge(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    db_path = tmp_path / "harness.db"
    request = _build(workspace)

    first, second = await asyncio.gather(
        HarnessStore(db_path).record_sandbox_eval_request(
            request,
            created_at="2026-07-23T01:00:00+00:00",
        ),
        HarnessStore(db_path).record_sandbox_eval_request(
            request,
            created_at="2026-07-23T01:00:01+00:00",
        ),
    )

    assert first == second
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM harness_sandbox_eval_requests"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"profile_trusted": False}, "sandbox_request_profile_untrusted"),
        ({"profile_digest": "bad"}, "sandbox_request_profile_digest_invalid"),
        ({"check_ids": ("missing",)}, "sandbox_request_profile_check_missing"),
        ({"check_ids": ("unit", "unit")}, "sandbox_request_check_ids_duplicated"),
        ({"batch_id": "../escape"}, "sandbox_request_batch_id_invalid"),
        ({"requested_samples": True}, "sandbox_request_sample_count_invalid"),
    ],
)
def test_sandbox_request_rejects_invalid_authority_before_git(
    tmp_path: Path,
    kwargs: dict[str, object],
    code: str,
) -> None:
    values = {
        "workspace_root": tmp_path / "not-created",
        "profile": _profile(),
        "profile_digest": "a" * 64,
        "profile_trusted": True,
        "check_ids": ("unit",),
        "batch_id": "batch",
        "requested_samples": 5,
        **kwargs,
    }

    with pytest.raises(HarnessSandboxEvalRequestError) as captured:
        HarnessSandboxEvalRequestBuilder().build(**values)  # type: ignore[arg-type]

    assert captured.value.code == code


def test_sandbox_request_rejects_dirty_or_nested_git_workspace(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "untracked.txt").write_text("not committed\n", encoding="utf-8")

    with pytest.raises(HarnessSandboxEvalRequestError) as dirty:
        _build(workspace)
    assert dirty.value.code == "sandbox_request_worktree_dirty"

    (workspace / "untracked.txt").unlink()
    nested = workspace / "nested"
    nested.mkdir()
    with pytest.raises(HarnessSandboxEvalRequestError) as wrong_root:
        _build(nested)
    assert wrong_root.value.code == "sandbox_request_git_root_mismatch"


def test_sandbox_request_enforces_worst_case_duration_budget(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    profile = _profile(
        check=HarnessCheckSpec(
            id="unit",
            argv=("uv", "run", "pytest"),
            timeout_seconds=121,
        ),
        max_duration_seconds=600,
    )

    with pytest.raises(HarnessSandboxEvalRequestError) as captured:
        _build(workspace, profile=profile)

    assert captured.value.code == "sandbox_request_duration_budget_exceeded"


def test_sandbox_request_detects_profile_check_drift_after_compilation(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    request = _build(workspace)
    drifted = _profile(
        check=HarnessCheckSpec(
            id="unit",
            argv=("uv", "run", "pytest", "-q", "tests/unit/test_other.py"),
            timeout_seconds=30,
        )
    )

    with pytest.raises(HarnessSandboxEvalRequestError) as captured:
        validate_request_checks(request, drifted)

    assert captured.value.code == "sandbox_request_profile_check_drifted"
