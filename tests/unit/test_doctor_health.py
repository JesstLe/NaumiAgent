from __future__ import annotations

import pytest

from naumi_agent.ui.doctor import DoctorCheck, DoctorReport
from naumi_agent.ui.doctor_health import (
    build_doctor_health_snapshot,
    pursuit_recovery_health_item,
    render_doctor_health_item_markdown,
    runtime_heartbeat_retention_health_item,
)
from naumi_agent.ui.pursuit_recovery import PursuitRecoverySnapshot


def test_doctor_health_snapshot_maps_domains_severity_and_responsibility() -> None:
    report = DoctorReport(
        checks=(
            DoctorCheck("Node.js", "pass", "v22.0.0"),
            DoctorCheck("API key", "error", "未检测到凭据", "运行 naumi configure。"),
            DoctorCheck("状态存储目录", "warn", "存在未版本化 Store", "运行迁移预检。"),
            DoctorCheck("browser daemon", "warn", "不可访问"),
            DoctorCheck("terminal capability", "pass", "TERM=xterm width=120"),
        )
    )

    first = build_doctor_health_snapshot(
        report,
        generated_at="2026-07-18T10:00:00+00:00",
    )
    second = build_doctor_health_snapshot(
        report,
        generated_at="2026-07-18T11:00:00+00:00",
    )
    inserted = build_doctor_health_snapshot(
        DoctorReport(
            checks=(DoctorCheck("Python 环境", "pass", "3.14"),) + report.checks
        )
    )

    assert first.status == "error"
    assert first.snapshot_sha256 == second.snapshot_sha256
    assert first.items[0].id == second.items[0].id
    assert first.items[0].id == inserted.items[1].id
    assert first.items[0].id.startswith("node-")
    assert [item.domain for item in first.items] == [
        "node",
        "provider",
        "store",
        "browser",
        "terminal",
    ]
    assert first.items[1].responsibility == "user_config"
    assert first.items[2].responsibility == "product_runtime"
    assert first.items[3].responsibility == "external_service"


def test_doctor_health_snapshot_bounds_public_text_and_item_count() -> None:
    report = DoctorReport(
        checks=tuple(
            DoctorCheck(f"自定义检查 {index}", "warn", "d" * 800, "s" * 800)
            for index in range(64)
        )
    )
    snapshot = build_doctor_health_snapshot(report)

    assert len(snapshot.items) == 64
    assert len(snapshot.items[0].detail) == 500
    assert len(snapshot.items[0].suggestion) == 500

    with pytest.raises(ValueError, match="64"):
        build_doctor_health_snapshot(
            DoctorReport(
                checks=report.checks + (DoctorCheck("额外检查", "pass", "ok"),)
            )
        )


def test_doctor_health_snapshot_redacts_secret_shaped_text() -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    snapshot = build_doctor_health_snapshot(
        DoctorReport(
            checks=(DoctorCheck("模型契约 fast", "error", f"provider rejected {secret}"),)
        )
    )

    assert secret not in snapshot.items[0].detail
    assert "REDACTED" in snapshot.items[0].detail


def test_pursuit_recovery_item_uses_shared_state_and_raises_overall_severity() -> None:
    recovery = PursuitRecoverySnapshot.model_validate({
        "schema_version": 1,
        "run_id": "pursuit-1",
        "generated_at": "2026-07-18T00:00:00+00:00",
        "recovery_state": "orphaned",
        "heartbeat": {
            "health": "offline", "phase": "running", "instance_id": "worker-a",
            "epoch": 1, "sequence": 2, "observed_at": "2026-07-17T23:00:00+00:00",
            "timeout_seconds": 30, "age_seconds": 3600, "detail_code": "lease_active",
        },
        "lease": {
            "status": "released", "owner_id": "worker-a", "epoch": 1,
            "expires_at": "2026-07-17T23:30:00+00:00",
            "updated_at": "2026-07-17T23:30:00+00:00", "expired": True,
        },
        "checkpoint": {
            "status": "ready", "checkpoint_id": "pchk_1", "sequence": 3,
            "phase": "assess", "iteration": 1,
            "created_at": "2026-07-17T23:20:00+00:00",
        },
        "reconcile_required": False,
        "reconcile_reason": "",
        "alerts": ["运行中但没有 live lease"],
    })
    item = pursuit_recovery_health_item(recovery)
    snapshot = build_doctor_health_snapshot(
        DoctorReport(checks=(DoctorCheck("Node.js", "pass", "v22"),)),
        additional_items=(item,),
    )

    assert item.id == "runtime-pursuit-recovery"
    assert item.severity == "error"
    assert "疑似孤立" in item.detail
    assert snapshot.status == "error"
    assert snapshot.items[-1] == item


def test_runtime_retention_health_distinguishes_live_failed_and_unavailable() -> None:
    live = runtime_heartbeat_retention_health_item({
        "configured_enabled": True,
        "state": "waiting",
        "cycle_count": 4,
        "deleted_count": 12,
        "failure_count": 1,
    })
    failed = runtime_heartbeat_retention_health_item({
        "configured_enabled": True,
        "state": "failed",
        "failure_count": 2,
        "last_error_code": "lease_lost",
    })
    unavailable = runtime_heartbeat_retention_health_item({
        "configured_enabled": True,
        "state": "unavailable",
    })

    assert live.severity == "ok"
    assert "成功周期 4" in live.detail
    assert "累计清理 12" in live.detail
    assert failed.severity == "degraded"
    assert "lease_lost" in failed.detail
    assert unavailable.severity == "unknown"
    assert "TUI fallback" in unavailable.suggestion
    assert "UNKNOWN 运行时心跳清理" in render_doctor_health_item_markdown(
        unavailable
    )


def test_runtime_retention_health_sanitizes_untrusted_status() -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    item = runtime_heartbeat_retention_health_item({
        "configured_enabled": True,
        "state": "broken",
        "cycle_count": secret,
        "last_error_code": secret,
    })

    assert item.severity == "unknown"
    assert secret not in item.detail
    assert "status_invalid" in item.detail
