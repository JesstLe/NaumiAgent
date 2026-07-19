from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from naumi_agent.harness.sandbox_batch import (
    HarnessSandboxBatchCheckpoint,
    HarnessSandboxBatchCoordinator,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class _SampleReceipt(_FrozenModel):
    sample_index: int


class _BatchReceipt(_FrozenModel):
    persisted_samples: int


class _PermissionStore:
    async def get(self, _receipt_id: str):
        return SimpleNamespace(
            authorizes_execution=True,
            run_id="run-batch",
            delegated_tool_names=("bash_run",),
        )


class _Store:
    def __init__(self) -> None:
        self.records = []
        self.released = []
        self.epoch = 0

    async def acquire_run_lease(self, **kwargs):
        self.epoch += 1
        return SimpleNamespace(epoch=self.epoch, owner_id=kwargs["owner_id"])

    async def release_run_lease(self, **kwargs):
        self.released.append(kwargs)
        return SimpleNamespace(state="released")


class _RunGrantAuthority:
    def __init__(self, workspace_root: Path, permission_store: _PermissionStore) -> None:
        self._workspace_root = workspace_root
        self._permission_store = permission_store
        self.issued = []
        self.revoked = []

    async def issue(self, request, **_kwargs):
        self.issued.append(request)
        index = len(self.issued)
        return SimpleNamespace(
            contract=SimpleNamespace(
                grant_id=f"grant-{index}",
                grant_sha256=f"{index:x}" * 64,
            )
        )

    async def revoke(self, **kwargs):
        self.revoked.append(kwargs)


def _record(index: int):
    return SimpleNamespace(sample_index=index, result_sha256=f"{index + 1:x}" * 64)


def _coordinator(
    tmp_path: Path,
    store: _Store,
    permissions: _PermissionStore,
    grants: _RunGrantAuthority,
    *,
    token: str,
) -> HarnessSandboxBatchCoordinator:
    return HarnessSandboxBatchCoordinator(
        workspace_root=tmp_path,
        store=store,  # type: ignore[arg-type]
        permission_store=permissions,  # type: ignore[arg-type]
        run_grant_authority=grants,  # type: ignore[arg-type]
        now=lambda: "2026-07-20T00:00:00+00:00",
        token=lambda: token,
    )


def test_sandbox_batch_rejects_cross_workspace_authority(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    permissions = _PermissionStore()
    grants = _RunGrantAuthority(other, permissions)
    with pytest.raises(ValueError, match="workspace 不一致"):
        HarnessSandboxBatchCoordinator(
            workspace_root=tmp_path,
            store=_Store(),  # type: ignore[arg-type]
            permission_store=permissions,  # type: ignore[arg-type]
            run_grant_authority=grants,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_sandbox_batch_emits_partial_checkpoint_and_resumes(
    tmp_path: Path,
) -> None:
    store = _Store()
    permissions = _PermissionStore()
    grants = _RunGrantAuthority(tmp_path, permissions)
    interrupted_progress: list[HarnessSandboxBatchCheckpoint] = []

    async def load_records():
        return tuple(store.records)

    async def validate_prefix(records):
        return [_SampleReceipt(sample_index=item.sample_index) for item in records]

    def validate_evidence(_records) -> None:
        return None

    async def interrupted_sample(index, authority):
        assert authority.run_id == "run-batch"
        if index == 1:
            raise RuntimeError("simulated interruption")
        store.records.append(_record(index))
        return _SampleReceipt(sample_index=index)

    def build_receipt(records, _receipts):
        return _BatchReceipt(persisted_samples=len(records))

    async def capture(checkpoint):
        if checkpoint.stage == "failed":
            assert grants.revoked
            assert store.released
        interrupted_progress.append(checkpoint)

    with pytest.raises(RuntimeError, match="simulated interruption"):
        await _coordinator(
            tmp_path,
            store,
            permissions,
            grants,
            token="a" * 32,
        ).execute(
            phase="adversarial",
            authority_key="a" * 64,
            parent_receipt_id="parent",
            requested_samples=5,
            max_total_duration_seconds=60,
            load_records=load_records,
            validate_existing_prefix=validate_prefix,
            validate_run_evidence=validate_evidence,
            execute_sample=interrupted_sample,
            build_receipt=build_receipt,
            on_progress=capture,
        )

    assert [item.stage for item in interrupted_progress] == [
        "recovering",
        "acquiring",
        "executing",
        "failed",
    ]
    partial = interrupted_progress[-1]
    assert partial.persisted_samples == 1
    assert partial.sample_result_sha256 == ("1" * 64,)
    assert partial.code == "sample_execution_interrupted"
    with pytest.raises(ValueError):
        HarnessSandboxBatchCheckpoint.model_validate(
            partial.model_copy(update={"persisted_samples": 2}).model_dump(mode="json")
        )
    assert grants.revoked[-1]["reason"] == "sandbox_batch_finished"
    assert len(store.released) == 1

    resumed_progress: list[HarnessSandboxBatchCheckpoint] = []

    async def remaining_sample(index, _authority):
        store.records.append(_record(index))
        return _SampleReceipt(sample_index=index)

    async def noisy_observer(checkpoint):
        resumed_progress.append(checkpoint)
        raise RuntimeError("observer unavailable")

    receipt = await _coordinator(
        tmp_path,
        store,
        permissions,
        grants,
        token="b" * 32,
    ).execute(
        phase="adversarial",
        authority_key="a" * 64,
        parent_receipt_id="parent",
        requested_samples=5,
        max_total_duration_seconds=60,
        load_records=load_records,
        validate_existing_prefix=validate_prefix,
        validate_run_evidence=validate_evidence,
        execute_sample=remaining_sample,
        build_receipt=build_receipt,
        on_progress=noisy_observer,
    )

    assert receipt.persisted_samples == 5
    assert [item.persisted_samples for item in resumed_progress] == [1, 1, 2, 3, 4, 5, 5]
    assert resumed_progress[-1].stage == "completed"
    assert len(store.released) == 2
    assert len(grants.revoked) == 2
