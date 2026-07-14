from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from naumi_agent.harness.service import (
    HarnessService,
    HarnessStatusCode,
    render_harness_doctor,
    render_harness_status,
)
from naumi_agent.harness.trust import HarnessTrustStore

PROFILE = """\
schema_version: 1
knowledge:
  entrypoints: [AGENTS.md, docs/missing.md]
checks:
  - id: unit
    label: 单元测试
    argv: [uv, run, pytest, -q]
    timeout_seconds: 60
evals:
  suites: [docs/evals/core.yaml]
"""


def _profile_path(workspace: Path) -> Path:
    path = workspace / ".naumi" / "harness.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PROFILE, encoding="utf-8")
    return path


def _service(tmp_path: Path) -> HarnessService:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "trust.db"),
    )


@pytest.mark.asyncio
async def test_status_distinguishes_missing_invalid_untrusted_and_trusted(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    missing = await service.status()
    assert missing.code is HarnessStatusCode.MISSING

    path = _profile_path(service.workspace_root)
    path.write_text("schema_version: 2\n", encoding="utf-8")
    invalid = await service.status()
    assert invalid.code is HarnessStatusCode.INVALID

    path.write_text(PROFILE, encoding="utf-8")
    untrusted = await service.status()
    assert untrusted.code is HarnessStatusCode.UNTRUSTED
    assert untrusted.profile_digest

    await service.trust(source="user_slash")
    trusted = await service.status()
    assert trusted.code is HarnessStatusCode.TRUSTED
    assert trusted.trusted


@pytest.mark.asyncio
async def test_profile_byte_change_invalidates_service_trust(tmp_path: Path) -> None:
    service = _service(tmp_path)
    path = _profile_path(service.workspace_root)
    await service.trust(source="user_slash")

    path.write_text(PROFILE + "\n", encoding="utf-8")

    status = await service.status()
    assert status.code is HarnessStatusCode.UNTRUSTED
    assert not status.trusted


@pytest.mark.asyncio
async def test_doctor_reports_paths_commands_and_execution_disabled_without_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    _profile_path(service.workspace_root)
    (service.workspace_root / "AGENTS.md").write_text("rules", encoding="utf-8")

    def fail_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"H1 不得执行 profile 命令: {args!r} {kwargs!r}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    report = await service.doctor()

    assert report.status.code is HarnessStatusCode.UNTRUSTED
    assert report.command_summaries == ("unit: uv run pytest -q",)
    findings = {finding.code: finding for finding in report.findings}
    assert findings["entrypoint_ok"].level == "ok"
    assert findings["entrypoint_missing"].level == "warning"
    assert findings["eval_suite_missing"].level == "warning"
    assert findings["execution_disabled"].level == "info"
    assert "不会执行" in findings["execution_disabled"].message


@pytest.mark.asyncio
async def test_trust_rejects_missing_or_invalid_profile(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="不存在"):
        await service.trust(source="user_slash")

    path = _profile_path(service.workspace_root)
    path.write_text("schema_version: 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="无效"):
        await service.trust(source="user_slash")


@pytest.mark.asyncio
async def test_untrust_reports_whether_record_existed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _profile_path(service.workspace_root)
    await service.trust(source="user_slash")

    assert await service.untrust()
    assert not await service.untrust()


@pytest.mark.asyncio
async def test_renderers_are_chinese_actionable_and_hide_raw_errors(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _profile_path(service.workspace_root)

    status_text = render_harness_status(await service.status())
    doctor_text = render_harness_doctor(await service.doctor())

    assert "Harness 配置未受信任" in status_text
    assert "下一步" in status_text
    assert "诊断" in doctor_text
    assert "uv run pytest -q" in doctor_text
    assert "Traceback" not in doctor_text


@pytest.mark.asyncio
async def test_corrupted_trust_database_degrades_to_actionable_doctor_warning(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "trust.db"
    db_path.write_text("not a sqlite database", encoding="utf-8")
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(db_path),
    )
    _profile_path(workspace)

    report = await service.doctor()
    text = render_harness_doctor(report)

    assert report.status.code is HarnessStatusCode.UNTRUSTED
    assert not report.status.trust_store_available
    assert any(finding.code == "trust_store_unavailable" for finding in report.findings)
    assert "信任状态暂时不可用" in text
    assert "file is not a database" not in text
