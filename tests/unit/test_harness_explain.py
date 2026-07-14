from __future__ import annotations

from pathlib import Path

import pytest

from naumi_agent.harness.completion import HarnessCompletionReceipt
from naumi_agent.harness.explain import HarnessExplainer, HarnessFailureClass
from naumi_agent.harness.models import HarnessCompletionContract, HarnessTaskKind
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import (
    HarnessStore,
    HarnessStoredCheck,
    HarnessStoredEvidence,
    HarnessStoredRun,
)
from naumi_agent.harness.trust import HarnessTrustStore


def _receipt(
    *,
    status: str = "completed_verified",
    warnings: tuple[str, ...] = (),
) -> HarnessCompletionReceipt:
    return HarnessCompletionReceipt.model_validate(
        {
            "run_id": "explain-run",
            "status": status,
            "task_kind": "change",
            "changed_files": ["source.py"],
            "checks": [],
            "criteria": [],
            "warnings": warnings,
            "tree_fingerprint": "b" * 64,
        }
    )


def _run(
    *,
    status: str = "completed_verified",
    receipt: HarnessCompletionReceipt | None = None,
    checks: tuple[HarnessStoredCheck, ...] = (),
    evidence: tuple[HarnessStoredEvidence, ...] = (),
) -> HarnessStoredRun:
    contract = HarnessCompletionContract(
        run_id="explain-run",
        session_id="session-1",
        task_kind=HarnessTaskKind.CHANGE,
        objective="解释一次真实运行",
    )
    return HarnessStoredRun(
        id="explain-run",
        workspace_root="/workspace/project",
        session_id="session-1",
        task_id=None,
        issue_id=None,
        task_kind="change",
        objective=contract.objective,
        status=status,
        profile_digest="a" * 64,
        tree_fingerprint_before="a" * 64,
        tree_fingerprint_after="b" * 64 if receipt is not None else "",
        started_at="2026-07-15T10:00:00+00:00",
        completed_at=("2026-07-15T10:01:00+00:00" if receipt is not None else ""),
        contract=contract,
        receipt=receipt,
        criteria=(),
        checks=checks,
        evidence=evidence,
    )


def _check(status: str) -> HarnessStoredCheck:
    return HarnessStoredCheck(
        id="check-record",
        check_key="unit",
        argv=("python3", "-m", "pytest", "tests/unit/test_small.py"),
        cwd="/workspace/project",
        status=status,
        exit_code=1 if status == "failed" else None,
        duration_ms=25,
        started_at="2026-07-15T10:00:10+00:00",
        completed_at="2026-07-15T10:00:11+00:00",
        tree_fingerprint="b" * 64,
        profile_digest="a" * 64,
        artifact_path="",
    )


def _tool_evidence(**summary: object) -> HarnessStoredEvidence:
    return HarnessStoredEvidence(
        id="tool-evidence",
        kind="tool_execution",
        uri="chat-run://explain-run/tool/tool-evidence",
        sha256="c" * 64,
        description="工具执行事实",
        summary={
            "tool_name": "read",
            "status": "success",
            "start_missing": False,
            "permission_status": "not_observed",
            **summary,
        },
        producer="harness_evidence_collector",
        created_at="2026-07-15T10:00:20+00:00",
        criterion_ids=(),
    )


def test_verified_run_has_no_failure_class() -> None:
    result = HarnessExplainer().explain(
        _run(receipt=_receipt())
    )

    assert result.verified is True
    assert result.running is False
    assert result.failure_classes == ()
    assert result.findings == ()


def test_running_run_is_not_misclassified_as_failure() -> None:
    result = HarnessExplainer().explain(_run(status="running"))

    assert result.running is True
    assert result.verified is False
    assert result.failure_classes == ()
    assert result.summary == "运行仍在进行，尚未形成完成回执。"


def test_failed_check_is_verification_failure_with_action() -> None:
    result = HarnessExplainer().explain(
        _run(
            status="completed_unverified",
            receipt=_receipt(
                status="completed_unverified",
                warnings=("必需检查 unit 状态为 failed，不能作为通过证据。",),
            ),
            checks=(_check("failed"),),
        )
    )

    assert result.failure_classes == (HarnessFailureClass.VERIFICATION_FAILURE,)
    assert result.findings[0].source.split(",") == ["check:unit", "receipt"]
    assert "重新运行" in result.findings[0].next_step


def test_permission_denial_and_blocked_tool_are_permission_block() -> None:
    result = HarnessExplainer().explain(
        _run(
            status="blocked",
            receipt=_receipt(status="blocked"),
            evidence=(
                _tool_evidence(
                    status="aborted",
                    permission_status="denied",
                    permission_risk_level="high",
                ),
            ),
        )
    )

    assert result.failure_classes == (HarnessFailureClass.PERMISSION_BLOCK,)
    assert result.findings[0].evidence_ids == ("tool-evidence",)
    assert "权限" in result.findings[0].message


def test_aborted_or_error_tool_without_cause_requires_human_judgment() -> None:
    for status in ("aborted", "error"):
        result = HarnessExplainer().explain(
            _run(
                status="completed_unverified",
                receipt=_receipt(status="completed_unverified"),
                evidence=(_tool_evidence(status=status),),
            )
        )

        assert result.failure_classes == (
            HarnessFailureClass.HUMAN_JUDGMENT_REQUIRED,
        )
        assert "ChatRun" in result.findings[0].next_step


def test_skipped_tool_is_agent_repetition() -> None:
    result = HarnessExplainer().explain(
        _run(
            status="completed_unverified",
            receipt=_receipt(status="completed_unverified"),
            evidence=(_tool_evidence(status="skipped"),),
        )
    )

    assert result.failure_classes == (HarnessFailureClass.AGENT_REPETITION,)
    assert "重复" in result.findings[0].message


def test_missing_start_and_invalid_tool_are_tool_contract_error() -> None:
    result = HarnessExplainer().explain(
        _run(
            status="completed_unverified",
            receipt=_receipt(status="completed_unverified"),
            evidence=(
                _tool_evidence(
                    tool_name="invalid_tool_call",
                    status="error",
                    start_missing=True,
                ),
            ),
        )
    )

    assert result.failure_classes == (HarnessFailureClass.TOOL_CONTRACT_ERROR,)
    assert "事件不完整" in result.findings[0].message


def test_missing_evidence_warning_is_premature_finish() -> None:
    result = HarnessExplainer().explain(
        _run(
            status="completed_unverified",
            receipt=_receipt(
                status="completed_unverified",
                warnings=("缺少必需证据类型 test_report。",),
            ),
        )
    )

    assert result.failure_classes == (HarnessFailureClass.AGENT_PREMATURE_FINISH,)
    assert result.findings[0].source == "receipt"


def test_infrastructure_warning_precedes_other_classes() -> None:
    result = HarnessExplainer().explain(
        _run(
            status="blocked",
            receipt=_receipt(
                status="blocked",
                warnings=(
                    "infrastructure_error: Harness 状态库不可写。",
                    "缺少必需检查 unit。",
                ),
            ),
        )
    )

    assert result.failure_classes == (
        HarnessFailureClass.ENVIRONMENT_ERROR,
        HarnessFailureClass.AGENT_PREMATURE_FINISH,
    )


def test_unclassified_blocked_run_requires_human_judgment() -> None:
    result = HarnessExplainer().explain(
        _run(
            status="blocked",
            receipt=_receipt(status="blocked", warnings=("需要产品负责人判断。",)),
        )
    )

    assert result.failure_classes == (
        HarnessFailureClass.HUMAN_JUDGMENT_REQUIRED,
    )
    assert "人工" in result.findings[0].next_step


async def _start_stored_run(
    store: HarnessStore,
    workspace: Path,
    *,
    run_id: str,
    started_at: str,
) -> None:
    await store.start_run(
        workspace_root=workspace,
        contract=HarnessCompletionContract(
            run_id=run_id,
            session_id="lookup-session",
            task_kind=HarnessTaskKind.ANALYSIS,
            objective=f"解释 {run_id}",
        ),
        tree_fingerprint_before="d" * 64,
        started_at=started_at,
    )


@pytest.mark.asyncio
async def test_service_explain_latest_is_workspace_isolated(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    other_workspace.mkdir()
    store = HarnessStore(tmp_path / "harness.db")
    await _start_stored_run(
        store,
        workspace,
        run_id="workspace-run",
        started_at="2026-07-15T10:00:00+00:00",
    )
    await _start_stored_run(
        store,
        other_workspace,
        run_id="other-run",
        started_at="2026-07-15T11:00:00+00:00",
    )
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
        store=store,
    )

    latest = await service.explain_run()
    explicit = await service.explain_run("workspace-run")
    foreign = await service.explain_run("other-run")

    assert latest.status == "ok"
    assert latest.explanation is not None
    assert latest.explanation.run_id == "workspace-run"
    assert explicit.status == "ok"
    assert foreign.status == "not_found"
    assert foreign.explanation is None
    assert "other-run" not in foreign.message


@pytest.mark.asyncio
async def test_service_explain_reports_unavailable_store_safely(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    without_store = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
    )
    damaged_path = tmp_path / "damaged.db"
    damaged_path.write_text("not sqlite", encoding="utf-8")
    damaged_store = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "damaged-trust.db"),
        store=HarnessStore(damaged_path),
    )

    missing = await without_store.explain_run()
    damaged = await damaged_store.explain_run("run-id")

    assert missing.status == "unavailable"
    assert damaged.status == "unavailable"
    assert "sqlite" not in damaged.message.casefold()
    assert str(damaged_path) not in damaged.message
