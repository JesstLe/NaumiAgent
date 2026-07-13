from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import naumi_agent.main as main_module
from naumi_agent.ui.doctor import DoctorCheck, DoctorReport

runner = CliRunner()


def test_doctor_command_forwards_live_flag(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("workspace_root: .\n", encoding="utf-8")
    calls: list[bool] = []

    async def fake_run_doctor(_config, *, workspace_root, mcp_manager=None, live=False):
        calls.append(live)
        return DoctorReport((DoctorCheck("模型实时连接", "pass", "连接成功"),))

    monkeypatch.setattr(main_module, "run_doctor", fake_run_doctor)

    result = runner.invoke(
        main_module.app,
        ["doctor", "--live", "--config", str(config_path)],
    )

    assert result.exit_code == 0
    assert calls == [True]
    assert "连接成功" in result.output
