"""Bounded HAR coverage readers shared by release-bound execution outcomes."""

from __future__ import annotations

from datetime import UTC, datetime

from naumi_agent.harness.run_lease import HarnessRunKind
from naumi_agent.harness.store import HarnessStore
from naumi_agent.runs.store import ChatRunRecord

_MAX_SAMPLES = 5_000
_PAGE_LIMIT = 500


class ReleaseBoundExecutionCoverageError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def read_current_execution_coverage(
    *,
    harness_store: HarnessStore,
    run: ChatRunRecord,
):
    if run.release_provenance is None or run.receipt is None:
        raise ReleaseBoundExecutionCoverageError(
            "run_evidence_incomplete",
            "Chat run 缺少 release provenance 或 completion receipt。",
        )
    binding = run.release_provenance.binding
    heartbeat = await harness_store.get_heartbeat(
        workspace_root=run.release_provenance.workspace_root,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id=binding.subject_id,
    )
    if heartbeat is None:
        raise ReleaseBoundExecutionCoverageError(
            "heartbeat_missing",
            "缺少 Runtime heartbeat head。",
        )
    origin_page = await harness_store.list_runtime_release_observations(
        workspace_root=run.release_provenance.workspace_root,
        subject_id=binding.subject_id,
        after_sequence=0,
        limit=1,
    )
    if origin_page is None or not origin_page.items:
        raise ReleaseBoundExecutionCoverageError(
            "coverage_missing",
            "缺少 Runtime Release Observation samples。",
        )
    if not (
        origin_page.binding_id == binding.binding_id
        and origin_page.binding_sha256 == binding.binding_sha256
    ):
        raise ReleaseBoundExecutionCoverageError(
            "ledger_binding_mismatch",
            "Runtime observation origin 未绑定该 chat run release。",
        )
    origin = origin_page.items[0].chain_origin_sequence
    first_sequence = max(origin, heartbeat.sequence - _MAX_SAMPLES + 1)
    after_sequence = 0 if first_sequence == origin else first_sequence - 1
    samples = []
    while after_sequence < heartbeat.sequence:
        page = await harness_store.list_runtime_release_observations(
            workspace_root=run.release_provenance.workspace_root,
            subject_id=binding.subject_id,
            after_sequence=after_sequence,
            limit=min(_PAGE_LIMIT, heartbeat.sequence - after_sequence),
        )
        if page is None or not (
            page.binding_id == binding.binding_id
            and page.binding_sha256 == binding.binding_sha256
        ):
            raise ReleaseBoundExecutionCoverageError(
                "ledger_binding_mismatch",
                "Runtime observation ledger 未绑定该 chat run release。",
            )
        samples.extend(page.items)
        if page.items and page.items[-1].heartbeat_sequence == heartbeat.sequence:
            break
        if not page.items or page.items[-1].heartbeat_sequence <= after_sequence:
            raise ReleaseBoundExecutionCoverageError(
                "ledger_cursor_invalid",
                "Runtime observation ledger cursor 未前进。",
            )
        after_sequence = page.items[-1].heartbeat_sequence
    heartbeat_after = await harness_store.get_heartbeat(
        workspace_root=run.release_provenance.workspace_root,
        subject_kind=HarnessRunKind.RUNTIME,
        subject_id=binding.subject_id,
    )
    binding_after = await harness_store.get_runtime_release_binding(
        workspace_root=run.release_provenance.workspace_root,
        subject_id=binding.subject_id,
    )
    if (
        heartbeat_after != heartbeat
        or binding_after != binding
        or not samples
        or not _heartbeat_matches_sample(heartbeat, samples[-1])
    ):
        raise ReleaseBoundExecutionCoverageError(
            "ledger_head_changed",
            "Runtime observation ledger 在读取期间发生变化。",
        )
    started = _aware(run.receipt.started_at)
    completed = _aware(run.receipt.completed_at)
    predecessor = None
    successor = None
    for index, sample in enumerate(samples):
        observed = _aware(sample.observed_at)
        if observed <= started:
            predecessor = index
        if predecessor is not None and observed >= completed:
            successor = index
            break
    if predecessor is None or successor is None or successor <= predecessor:
        raise ReleaseBoundExecutionCoverageError(
            "coverage_pending",
            "Heartbeat 尚未从运行开始前连续覆盖到运行完成后。",
        )
    return tuple(samples[predecessor : successor + 1])


async def read_exact_execution_coverage(
    *,
    harness_store: HarnessStore,
    workspace_root: str,
    subject_id: str,
    binding_id: str,
    binding_sha256: str,
    expected: tuple,
):
    if not 2 <= len(expected) <= _MAX_SAMPLES:
        raise ReleaseBoundExecutionCoverageError(
            "coverage_count_invalid",
            "Execution Outcome coverage 样本数无效。",
        )
    first = expected[0]
    after_sequence = first.heartbeat_sequence - 1
    actual = []
    while len(actual) < len(expected):
        page = await harness_store.list_runtime_release_observations(
            workspace_root=workspace_root,
            subject_id=subject_id,
            after_sequence=after_sequence,
            limit=min(_PAGE_LIMIT, len(expected) - len(actual)),
        )
        if page is None or not page.items:
            break
        if not (
            page.binding_id == binding_id
            and page.binding_sha256 == binding_sha256
        ):
            raise ReleaseBoundExecutionCoverageError(
                "ledger_binding_mismatch",
                "Execution Outcome coverage page 未绑定 exact release。",
            )
        actual.extend(page.items)
        after_sequence = page.items[-1].heartbeat_sequence
    return tuple(actual)


def _heartbeat_matches_sample(heartbeat, sample) -> bool:
    return bool(
        heartbeat.workspace_root == sample.workspace_root
        and heartbeat.subject_kind is HarnessRunKind.RUNTIME
        and heartbeat.subject_id == sample.subject_id
        and heartbeat.instance_id == sample.instance_id
        and heartbeat.epoch == sample.epoch
        and heartbeat.sequence == sample.heartbeat_sequence
        and heartbeat.phase is sample.phase
        and _aware(heartbeat.observed_at) == _aware(sample.observed_at)
        and heartbeat.timeout_seconds == sample.timeout_seconds
        and heartbeat.detail_code == sample.detail_code
    )


def _aware(value) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Execution Outcome coverage timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


__all__ = [
    "ReleaseBoundExecutionCoverageError",
    "read_current_execution_coverage",
    "read_exact_execution_coverage",
]
