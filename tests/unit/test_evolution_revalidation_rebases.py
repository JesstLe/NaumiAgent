from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.revalidation_execution import (
    EvolutionRevalidationExecutionService,
)
from naumi_agent.evolution.revalidation_rebases import (
    EvolutionRevalidationRebaseError,
    EvolutionRevalidationRebaseExecutor,
    EvolutionRevalidationRebaseStatus,
    EvolutionRevalidationRebaseStore,
    _merge_file,
    _path_is_unsafe,
)
from naumi_agent.tools.evolution_review import EvolutionRevalidationReplayTool
from tests.unit.test_evolution_promotion_packages import _git
from tests.unit.test_evolution_revalidation_replays import _fixture


class _StaticRequestService:
    def __init__(self, view) -> None:
        self._view = view

    async def inspect(self, **_kwargs):
        return self._view


class _StaticStore:
    def __init__(self, value) -> None:
        self._value = value

    async def get(self, _key):
        return self._value


class _UnexpectedExactExecutor:
    async def execute(self, **_kwargs):
        raise AssertionError("advanced target must not use exact replay")


def _advance_view(root: Path, view):
    current = _git(root, "rev-parse", "main")
    return view.model_copy(
        update={
            "decision_current": False,
            "decision_rebase_eligible": True,
            "target_current": False,
            "current_target_head": current,
            "current_target_relation": "advanced",
            "current_status": "stale",
            "execution_eligible": True,
        }
    )


@pytest.mark.asyncio
async def test_linear_target_replays_candidate_in_detached_worktree(tmp_path: Path) -> None:
    now, storage, package, lease, view, path, before, after = _fixture(tmp_path)
    (tmp_path / "target-only.txt").write_text("target advanced\n", encoding="utf-8")
    _git(tmp_path, "add", "target-only.txt")
    _git(tmp_path, "commit", "-m", "advance target independently")
    advanced = _advance_view(tmp_path, view)
    executor = EvolutionRevalidationRebaseExecutor(
        store=EvolutionRevalidationRebaseStore(tmp_path / ".naumi" / "state.db"),
        worktree_storage_dir=storage,
        clock=lambda: now,
    )

    outcome = await executor.execute(
        request_view=advanced,
        package_input=package,
        lease=lease,
    )
    repeated = await executor.execute(
        request_view=advanced,
        package_input=package,
        lease=lease,
    )

    assert outcome == repeated
    assert outcome.status is EvolutionRevalidationRebaseStatus.SUCCEEDED
    assert outcome.files[0].merge_strategy == "candidate_on_unchanged_target"
    assert outcome.result_tree_sha256
    assert not outcome.conflict_paths
    assert outcome.detached_worktree_removed
    assert not outcome.validation_executed
    assert not outcome.promotion_authority
    assert (tmp_path / path).read_bytes() == before
    assert (Path(lease.worktree_path) / path).read_bytes() == after
    assert _git(tmp_path, "rev-parse", "main") == advanced.current_target_head
    assert not tuple(storage.glob(f"rebase-{view.request.request_id[-12:]}-*"))
    with sqlite3.connect(tmp_path / ".naumi" / "state.db") as db:
        persisted_path = db.execute(
            "SELECT worktree_path FROM evolution_revalidation_rebase_attempts "
            "WHERE request_id = ? AND target_head = ?",
            (view.request.request_id, advanced.current_target_head),
        ).fetchone()
    assert persisted_path is not None
    assert persisted_path[0].endswith("-1")
    assert not persisted_path[0].endswith("-pending")

    service = EvolutionRevalidationExecutionService(
        request_service=_StaticRequestService(advanced),
        package_input_store=_StaticStore(
            SimpleNamespace(
                promotion_review_eligible=True,
                package_input=package,
            )
        ),
        lease_store=_StaticStore(lease),
        exact_executor=_UnexpectedExactExecutor(),
        rebase_executor=executor,
    )
    dispatched = await service.execute(
        workspace_root=tmp_path,
        request_id=view.request.request_id,
    )
    assert dispatched == outcome
    engine = SimpleNamespace(
        workspace_root=tmp_path,
        evolution_revalidation_replay_service=service,
    )
    tool_output = await EvolutionRevalidationReplayTool(engine).execute(
        view.request.request_id
    )
    slash_output = await execute_slash_command(
        engine,
        f"/evolution revalidation-replay {view.request.request_id}",
    )
    assert outcome.outcome_id in tool_output
    assert outcome.outcome_id in slash_output


@pytest.mark.asyncio
async def test_same_line_change_creates_durable_conflict_outcome(tmp_path: Path) -> None:
    now, storage, package, lease, view, path, _before, _after = _fixture(tmp_path)
    (tmp_path / path).write_text("VERSION = 99\n", encoding="utf-8")
    _git(tmp_path, "add", path)
    _git(tmp_path, "commit", "-m", "conflict with candidate")
    advanced = _advance_view(tmp_path, view)
    executor = EvolutionRevalidationRebaseExecutor(
        store=EvolutionRevalidationRebaseStore(tmp_path / ".naumi" / "state.db"),
        worktree_storage_dir=storage,
        clock=lambda: now,
    )

    outcome = await executor.execute(
        request_view=advanced,
        package_input=package,
        lease=lease,
    )

    assert outcome.status is EvolutionRevalidationRebaseStatus.CONFLICTED
    assert outcome.conflict_paths == (path,)
    assert outcome.files[0].merge_strategy == "conflict"
    assert outcome.result_tree_sha256 is None
    assert outcome.detached_worktree_removed
    assert (tmp_path / path).read_text(encoding="utf-8") == "VERSION = 99\n"


def test_three_way_merge_preserves_independent_edits_and_detects_overlap() -> None:
    baseline = b"alpha\nbeta\ngamma\n"
    current = b"alpha\nbeta\ngamma-target\n"
    candidate = b"alpha-candidate\nbeta\ngamma\n"
    strategy, merged = _merge_file(
        operation="modify",
        baseline=baseline,
        current=current,
        candidate=candidate,
        mode_compatible=True,
    )
    assert strategy == "three_way_clean"
    assert merged == b"alpha-candidate\nbeta\ngamma-target\n"

    conflict_strategy, conflict = _merge_file(
        operation="modify",
        baseline=baseline,
        current=b"alpha-target\nbeta\ngamma\n",
        candidate=candidate,
        mode_compatible=True,
    )
    assert conflict_strategy == "conflict"
    assert conflict is None


def test_rebase_path_rejects_symlink_and_non_directory_ancestors(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("platform does not permit symlink creation")
    assert _path_is_unsafe(tmp_path, PurePosixPath("linked/result.py"))

    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    assert _path_is_unsafe(tmp_path, PurePosixPath("blocked/result.py"))
    assert not _path_is_unsafe(tmp_path, PurePosixPath("new/result.py"))


@pytest.mark.asyncio
async def test_expired_claim_is_fenced_and_stale_worktree_is_recovered(
    tmp_path: Path,
) -> None:
    now, storage, package, lease, view, _path, _before, _after = _fixture(tmp_path)
    (tmp_path / "advance.txt").write_text("advanced\n", encoding="utf-8")
    _git(tmp_path, "add", "advance.txt")
    _git(tmp_path, "commit", "-m", "advance for recovery")
    advanced = _advance_view(tmp_path, view)
    assert advanced.current_target_head is not None
    store = EvolutionRevalidationRebaseStore(tmp_path / ".naumi" / "state.db")
    prefix = f"rebase-{view.request.request_id[-12:]}-{advanced.current_target_head[:8]}-"
    stale_path = storage / f"{prefix}1"
    old_claim, _ = await store.claim(
        request_id=view.request.request_id,
        request_sha256=view.request.request_sha256,
        target_head=advanced.current_target_head,
        worktree_path=str(stale_path),
        now=now,
    )
    assert old_claim is not None
    with pytest.raises(EvolutionRevalidationRebaseError) as busy:
        await store.claim(
            request_id=view.request.request_id,
            request_sha256=view.request.request_sha256,
            target_head=advanced.current_target_head,
            worktree_path=str(stale_path),
            now=now + timedelta(seconds=1),
        )
    assert busy.value.code == "revalidation_rebase_claim_busy"
    _git(tmp_path, "worktree", "add", "--detach", str(stale_path), advanced.current_target_head)
    executor = EvolutionRevalidationRebaseExecutor(
        store=store,
        worktree_storage_dir=storage,
        clock=lambda: now + timedelta(seconds=121),
    )

    outcome = await executor.execute(
        request_view=advanced,
        package_input=package,
        lease=lease,
    )

    assert outcome.epoch == 2
    assert outcome.status is EvolutionRevalidationRebaseStatus.SUCCEEDED
    assert not stale_path.exists()
    with pytest.raises(EvolutionRevalidationRebaseError) as fenced:
        await store.finish(claim=old_claim, outcome=outcome, now=now + timedelta(seconds=122))
    assert fenced.value.code == "revalidation_rebase_fenced"


@pytest.mark.asyncio
async def test_missing_candidate_source_creates_durable_failed_outcome(
    tmp_path: Path,
) -> None:
    now, storage, package, lease, view, _path, _before, _after = _fixture(tmp_path)
    (tmp_path / "advance.txt").write_text("advanced\n", encoding="utf-8")
    _git(tmp_path, "add", "advance.txt")
    _git(tmp_path, "commit", "-m", "advance before source loss")
    advanced = _advance_view(tmp_path, view)
    _git(tmp_path, "worktree", "remove", "--force", lease.worktree_path)
    executor = EvolutionRevalidationRebaseExecutor(
        store=EvolutionRevalidationRebaseStore(tmp_path / ".naumi" / "state.db"),
        worktree_storage_dir=storage,
        clock=lambda: now,
    )

    outcome = await executor.execute(
        request_view=advanced,
        package_input=package,
        lease=lease,
    )
    repeated = await executor.execute(
        request_view=advanced,
        package_input=package,
        lease=lease,
    )

    assert outcome == repeated
    assert outcome.status is EvolutionRevalidationRebaseStatus.FAILED
    assert outcome.failure_code == "source_changed_during_rebase"
    assert not outcome.source_worktree_unchanged
    assert outcome.main_worktree_unchanged
    assert outcome.target_branch_unchanged
