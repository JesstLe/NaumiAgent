from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import naumi_agent.main as main_module
from naumi_agent.ui.doctor import DoctorCheck, DoctorReport
from naumi_agent.ui.doctor_probe import DoctorLiveProbeResult

runner = CliRunner()


def test_doctor_command_forwards_live_flag(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("workspace_root: .\n", encoding="utf-8")
    calls: list[int] = []

    async def fake_run_doctor_probe(
        _config,
        *,
        workspace_root,
        timeout_ms,
        model_router=None,
        model_router_error=None,
    ):
        calls.append(timeout_ms)
        return DoctorLiveProbeResult(
            report=DoctorReport((
                DoctorCheck("模型实时连接", "pass", "连接成功"),
            )),
            status="passed",
            diagnostic_code="",
            message="连接成功",
            suggestion="",
            request_count=1,
            duration_ms=10,
            timeout_ms=timeout_ms,
        )

    monkeypatch.setattr(
        main_module,
        "run_bounded_doctor_live_probe",
        fake_run_doctor_probe,
    )

    result = runner.invoke(
        main_module.app,
        ["doctor", "--live", "--config", str(config_path)],
    )

    assert result.exit_code == 0
    assert calls == [15_000]
    assert "连接成功" in result.output
