from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from naumi_agent.harness.service import (
    HarnessKnowledgeStatusCode,
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


@pytest.mark.asyncio
async def test_knowledge_context_requires_current_exact_profile_trust(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    profile_path = _profile_path(service.workspace_root)
    (service.workspace_root / "AGENTS.md").write_text(
        "TRUSTED_REPOSITORY_RULE",
        encoding="utf-8",
    )

    untrusted = await service.knowledge_context(
        "读取仓库规则",
        model_window=124_000,
    )
    await service.trust(source="user_slash")
    trusted = await service.knowledge_context(
        "读取仓库规则",
        model_window=124_000,
    )
    profile_path.write_text(PROFILE + "\n", encoding="utf-8")
    changed = await service.knowledge_context(
        "读取仓库规则",
        model_window=124_000,
    )

    assert untrusted.code is HarnessKnowledgeStatusCode.UNTRUSTED
    assert untrusted.bundle is None
    assert trusted.code is HarnessKnowledgeStatusCode.READY
    assert trusted.bundle is not None
    assert "TRUSTED_REPOSITORY_RULE" in trusted.bundle.rendered
    assert changed.code is HarnessKnowledgeStatusCode.UNTRUSTED
    assert changed.bundle is None


@pytest.mark.asyncio
async def test_knowledge_cache_hits_and_invalidates_when_source_changes(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _profile_path(service.workspace_root)
    agents = service.workspace_root / "AGENTS.md"
    agents.write_text("RULE_VERSION_ONE", encoding="utf-8")
    await service.trust(source="user_slash")

    first = await service.knowledge_context("规则", model_window=124_000)
    second = await service.knowledge_context("规则", model_window=124_000)
    agents.write_text("RULE_VERSION_TWO", encoding="utf-8")
    third = await service.knowledge_context("规则", model_window=124_000)

    assert not first.cache_hit
    assert second.cache_hit
    assert first.index_fingerprint == second.index_fingerprint
    assert not third.cache_hit
    assert third.index_fingerprint != second.index_fingerprint
    assert third.bundle is not None
    assert "RULE_VERSION_TWO" in third.bundle.rendered
    assert "RULE_VERSION_ONE" not in third.bundle.rendered


@pytest.mark.asyncio
async def test_warm_knowledge_context_skips_git_and_reselection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    _profile_path(service.workspace_root)
    (service.workspace_root / "AGENTS.md").write_text("RULE", encoding="utf-8")
    await service.trust(source="user_slash")
    first = await service.knowledge_context("读取规则", model_window=124_000)

    def fail_git() -> object:
        raise AssertionError("暖缓存不应每轮重新运行 Git")

    def fail_compose(*args: object, **kwargs: object) -> object:
        raise AssertionError("相同任务不应重新选择或渲染知识")

    monkeypatch.setattr(service._knowledge_index, "_read_git_state", fail_git)
    monkeypatch.setattr(service._knowledge_composer, "compose", fail_compose)

    second = await service.knowledge_context("读取规则", model_window=124_000)

    assert first.bundle == second.bundle
    assert second.cache_hit
    assert second.selection_cache_hit


@pytest.mark.asyncio
async def test_cached_bundle_revalidates_selected_digest_even_with_same_metadata(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _profile_path(service.workspace_root)
    agents = service.workspace_root / "AGENTS.md"
    agents.write_text("RULE_A", encoding="utf-8")
    await service.trust(source="user_slash")
    first = await service.knowledge_context("读取规则", model_window=124_000)
    original_stat = agents.stat()

    agents.write_text("RULE_B", encoding="utf-8")
    os.utime(
        agents,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    second = await service.knowledge_context("读取规则", model_window=124_000)

    assert first.bundle is not None and "RULE_A" in first.bundle.rendered
    assert second.bundle is not None and "RULE_B" in second.bundle.rendered
    assert "RULE_A" not in second.bundle.rendered
    assert not second.selection_cache_hit


@pytest.mark.asyncio
async def test_profile_change_during_digest_rebuild_revokes_knowledge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    profile_path = _profile_path(service.workspace_root)
    agents = service.workspace_root / "AGENTS.md"
    agents.write_text("RULE_A", encoding="utf-8")
    await service.trust(source="user_slash")
    first = await service.knowledge_context("读取规则", model_window=124_000)
    original_stat = agents.stat()

    agents.write_text("RULE_B", encoding="utf-8")
    os.utime(agents, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    original_build = service._knowledge_index.build

    def build_then_change_profile(*args: object, **kwargs: object) -> object:
        snapshot = original_build(*args, **kwargs)
        profile_path.write_text(PROFILE + "\n", encoding="utf-8")
        return snapshot

    monkeypatch.setattr(service._knowledge_index, "build", build_then_change_profile)

    second = await service.knowledge_context("读取规则", model_window=124_000)

    assert first.code is HarnessKnowledgeStatusCode.READY
    assert second.code is HarnessKnowledgeStatusCode.UNTRUSTED
    assert second.bundle is None


@pytest.mark.asyncio
async def test_knowledge_context_coalesces_concurrent_readers(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _profile_path(service.workspace_root)
    (service.workspace_root / "AGENTS.md").write_text("rules", encoding="utf-8")
    await service.trust(source="user_slash")

    results = await asyncio.gather(*(
        service.knowledge_context(f"任务 {index}", model_window=124_000)
        for index in range(50)
    ))

    assert all(result.code is HarnessKnowledgeStatusCode.READY for result in results)
    assert len({result.index_fingerprint for result in results}) == 1
    assert all(result.bundle is not None for result in results)


@pytest.mark.asyncio
async def test_knowledge_read_uses_same_trust_gate_and_current_cache(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _profile_path(service.workspace_root)
    (service.workspace_root / "AGENTS.md").write_text("READABLE_RULE", encoding="utf-8")

    untrusted = await service.read_knowledge(path="AGENTS.md", max_tokens=100)
    await service.trust(source="user_slash")
    trusted = await service.read_knowledge(path="AGENTS.md", max_tokens=100)

    assert untrusted.status == "untrusted"
    assert "先运行 /harness trust" in untrusted.message
    assert trusted.status == "ok"
    assert trusted.content == "READABLE_RULE"


@pytest.mark.asyncio
async def test_unavailable_trust_store_never_injects_knowledge(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "trust.db"
    db_path.write_text("not sqlite", encoding="utf-8")
    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(db_path),
    )
    _profile_path(workspace)
    (workspace / "AGENTS.md").write_text("MUST_NOT_LEAK", encoding="utf-8")

    result = await service.knowledge_context("读取规则", model_window=124_000)

    assert result.code is HarnessKnowledgeStatusCode.UNTRUSTED
    assert result.bundle is None
    assert "MUST_NOT_LEAK" not in result.message
