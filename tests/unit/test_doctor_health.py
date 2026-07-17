from __future__ import annotations

import pytest

from naumi_agent.ui.doctor import DoctorCheck, DoctorReport
from naumi_agent.ui.doctor_health import build_doctor_health_snapshot


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
