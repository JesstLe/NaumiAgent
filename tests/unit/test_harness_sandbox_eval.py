from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.harness.models import HarnessCheckSpec
from naumi_agent.harness.sandbox_eval import (
    HarnessSandboxEvalExecutionError,
    HarnessSandboxEvalExecutionKernel,
    HarnessSandboxEvalRunAuthority,
    HarnessSandboxEvalSource,
    _check_run_id,
)


def _kernel(tmp_path: Path) -> HarnessSandboxEvalExecutionKernel:
    permission_store = object()
    run_grant_authority = SimpleNamespace(
        _workspace_root=tmp_path,
        _permission_store=permission_store,
    )
    return HarnessSandboxEvalExecutionKernel(
        workspace_root=tmp_path,
        permission_store=permission_store,  # type: ignore[arg-type]
        run_grant_authority=run_grant_authority,  # type: ignore[arg-type]
        sandbox_runner=SimpleNamespace(workspace_root=tmp_path),  # type: ignore[arg-type]
        shell_admission_composer=SimpleNamespace(  # type: ignore[arg-type]
            _permission_store=permission_store,
            _run_delegation_grant_authority=run_grant_authority,
        ),
        now=lambda: "2026-07-20T00:00:00+00:00",
    )


def _authority() -> HarnessSandboxEvalRunAuthority:
    return HarnessSandboxEvalRunAuthority(
        parent_receipt_id="parent",
        run_id="run",
        grant_id="grant",
        grant_sha256="a" * 64,
    )


async def _current() -> bool:
    return True


def test_adversarial_run_id_binds_lane_authority_without_changing_red_identity() -> None:
    red = _check_run_id(
        "run",
        "red",
        0,
        "unit",
        authority_key="a" * 64,
    )
    first = _check_run_id(
        "run",
        "adversarial",
        0,
        "unit",
        authority_key="a" * 64,
    )
    second = _check_run_id(
        "run",
        "adversarial",
        0,
        "unit",
        authority_key="b" * 64,
    )

    assert red == "evored-0dcb65bb9911b66ff2a9e2529ee810f9"
    assert first.startswith("hevaladversarial-")
    assert first != second


def test_sandbox_eval_rejects_cross_workspace_runner(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    permission_store = object()
    run_grant_authority = SimpleNamespace(
        _workspace_root=tmp_path,
        _permission_store=permission_store,
    )
    with pytest.raises(ValueError, match="workspace 不一致"):
        HarnessSandboxEvalExecutionKernel(
            workspace_root=tmp_path,
            permission_store=permission_store,  # type: ignore[arg-type]
            run_grant_authority=run_grant_authority,  # type: ignore[arg-type]
            sandbox_runner=SimpleNamespace(workspace_root=other),  # type: ignore[arg-type]
            shell_admission_composer=SimpleNamespace(  # type: ignore[arg-type]
                _permission_store=permission_store,
                _run_delegation_grant_authority=run_grant_authority,
            ),
            now=lambda: "2026-07-20T00:00:00+00:00",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authority_key", "sample_index", "code"),
    [
        ("invalid", 0, "sandbox_eval_authority_key_invalid"),
        ("a" * 64, 100, "sandbox_eval_sample_index_invalid"),
    ],
)
async def test_sandbox_eval_rejects_invalid_identity_before_authority_read(
    tmp_path: Path,
    authority_key: str,
    sample_index: int,
    code: str,
) -> None:
    with pytest.raises(HarnessSandboxEvalExecutionError) as captured:
        await _kernel(tmp_path).execute(
            lane="adversarial",
            authority_key=authority_key,
            parent_receipt_id="parent",
            sample_index=sample_index,
            checks=(HarnessCheckSpec(id="unit", argv=("true",)),),
            profile_digest="b" * 64,
            profile_is_current=_current,
            source=HarnessSandboxEvalSource(
                revision="c" * 40,
                revision_tree_sha256="d" * 64,
            ),
            run_authority=_authority(),
        )
    assert captured.value.code == code


@pytest.mark.asyncio
async def test_sandbox_eval_rejects_duplicate_checks_before_authority_read(
    tmp_path: Path,
) -> None:
    check = HarnessCheckSpec(id="unit", argv=("true",))
    with pytest.raises(HarnessSandboxEvalExecutionError) as captured:
        await _kernel(tmp_path).execute(
            lane="red",
            authority_key="a" * 64,
            parent_receipt_id="parent",
            sample_index=0,
            checks=(check, check),
            profile_digest="b" * 64,
            profile_is_current=_current,
            source=HarnessSandboxEvalSource(
                revision="c" * 40,
                revision_tree_sha256="d" * 64,
            ),
            run_authority=_authority(),
        )
    assert captured.value.code == "sandbox_eval_checks_duplicated"
