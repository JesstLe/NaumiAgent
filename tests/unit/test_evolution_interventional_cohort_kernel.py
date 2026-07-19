from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.evolution.interventional_cohort_kernel import (
    EvolutionInterventionalCohortKernel,
    EvolutionInterventionalCohortKernelError,
)


class _ParentStore:
    async def get(self, _receipt_id: str):
        return SimpleNamespace(
            authorizes_execution=True,
            run_id="run-kernel",
            delegated_tool_names=("bash_run",),
        )


class _NeverAcquireStore:
    async def acquire_run_lease(self, **_kwargs):
        raise AssertionError("invalid authority must fail before Runtime lease")


def _kernel(tmp_path: Path, *, now, token=lambda: "a" * 32):
    return EvolutionInterventionalCohortKernel(
        workspace_root=tmp_path,
        store=_NeverAcquireStore(),  # type: ignore[arg-type]
        permission_store=_ParentStore(),  # type: ignore[arg-type]
        run_grant_authority=None,  # type: ignore[arg-type]
        now=now,
        token=token,
    )


async def _empty_records():
    return ()


async def _empty_receipts(_records):
    return []


async def _never_sample(_sample_index, _authority):
    raise AssertionError("sample execution must not start")


def _no_evidence(_records) -> None:
    return None


def _never_receipt(_records, _receipts):
    raise AssertionError("cohort receipt must not be built")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("now", "token", "expected_code"),
    [
        (lambda: "2026-07-19T00:00:00", lambda: "a" * 32, "cohort_clock_invalid"),
        (
            lambda: datetime(2026, 7, 19, tzinfo=UTC).isoformat(),
            lambda: "bad token",
            "cohort_owner_token_invalid",
        ),
    ],
)
async def test_cohort_kernel_rejects_invalid_runtime_authority_before_lease(
    tmp_path: Path,
    now,
    token,
    expected_code: str,
) -> None:
    kernel = _kernel(tmp_path, now=now, token=token)

    with pytest.raises(EvolutionInterventionalCohortKernelError) as captured:
        await kernel.execute(
            phase="green",
            authority_key="a" * 64,
            parent_receipt_id="parent",
            requested_samples=5,
            max_total_duration_seconds=60,
            load_records=_empty_records,
            validate_existing_prefix=_empty_receipts,
            validate_run_evidence=_no_evidence,
            execute_sample=_never_sample,
            build_receipt=_never_receipt,
        )

    assert captured.value.code == expected_code


@pytest.mark.asyncio
async def test_cohort_kernel_rejects_misaligned_receipt_prefix_before_permission(
    tmp_path: Path,
) -> None:
    kernel = _kernel(
        tmp_path,
        now=lambda: datetime(2026, 7, 19, tzinfo=UTC).isoformat(),
    )
    records = (SimpleNamespace(sample_index=0),)

    async def load_records():
        return records

    async def wrong_receipts(_records):
        return [SimpleNamespace(sample_index=1)]

    with pytest.raises(EvolutionInterventionalCohortKernelError) as captured:
        await kernel.execute(
            phase="red",
            authority_key="b" * 64,
            parent_receipt_id="must-not-read",
            requested_samples=5,
            max_total_duration_seconds=60,
            load_records=load_records,  # type: ignore[arg-type]
            validate_existing_prefix=wrong_receipts,  # type: ignore[arg-type]
            validate_run_evidence=_no_evidence,
            execute_sample=_never_sample,
            build_receipt=_never_receipt,
        )

    assert captured.value.code == "cohort_receipt_prefix_invalid"
