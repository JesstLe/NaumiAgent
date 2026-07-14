"""Shared read-only Harness status, doctor, and user-only trust facade."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from naumi_agent.harness.models import (
    HarnessProfileSnapshot,
    HarnessProfileStatus,
)
from naumi_agent.harness.profile import load_harness_profile
from naumi_agent.harness.trust import (
    HarnessTrustRecord,
    HarnessTrustStore,
    HarnessTrustStoreError,
)


class HarnessStatusCode(StrEnum):
    MISSING = "missing"
    INVALID = "invalid"
    UNTRUSTED = "untrusted"
    TRUSTED = "trusted"


@dataclass(frozen=True)
class HarnessStatus:
    code: HarnessStatusCode
    snapshot: HarnessProfileSnapshot
    trusted: bool
    stored_trust: HarnessTrustRecord | None = None
    trust_store_available: bool = True

    @property
    def profile_digest(self) -> str | None:
        return self.snapshot.digest


@dataclass(frozen=True)
class HarnessDoctorFinding:
    code: str
    level: Literal["ok", "info", "warning", "error"]
    message: str
    hint: str = ""


@dataclass(frozen=True)
class HarnessDoctorReport:
    status: HarnessStatus
    findings: tuple[HarnessDoctorFinding, ...]
    command_summaries: tuple[str, ...]


class HarnessService:
    """Facade shared by manual commands and read-only Agent tools."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        trust_store: HarnessTrustStore,
        profile_path: str | Path | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self._trust_store = trust_store
        self._profile_path = profile_path

    async def status(self) -> HarnessStatus:
        snapshot = self._load()
        if snapshot.status is HarnessProfileStatus.MISSING:
            return HarnessStatus(
                code=HarnessStatusCode.MISSING,
                snapshot=snapshot,
                trusted=False,
            )
        if snapshot.status is HarnessProfileStatus.INVALID:
            stored, available = await self._read_trust()
            return HarnessStatus(
                code=HarnessStatusCode.INVALID,
                snapshot=snapshot,
                trusted=False,
                stored_trust=stored,
                trust_store_available=available,
            )

        stored, available = await self._read_trust()
        trusted = stored is not None and stored.profile_digest == snapshot.digest
        return HarnessStatus(
            code=(HarnessStatusCode.TRUSTED if trusted else HarnessStatusCode.UNTRUSTED),
            snapshot=snapshot,
            trusted=trusted,
            stored_trust=stored,
            trust_store_available=available,
        )

    async def doctor(self) -> HarnessDoctorReport:
        status = await self.status()
        findings: list[HarnessDoctorFinding] = []
        commands: list[str] = []

        if status.code is HarnessStatusCode.MISSING:
            findings.append(
                HarnessDoctorFinding(
                    code="profile_missing",
                    level="info",
                    message="当前工作区尚未配置 .naumi/harness.yaml。",
                    hint="创建 schema_version: 1 的配置后重新运行 /harness doctor。",
                )
            )
            return HarnessDoctorReport(status, tuple(findings), ())

        if status.code is HarnessStatusCode.INVALID:
            findings.extend(
                HarnessDoctorFinding(
                    code=error.code,
                    level="error",
                    message=error.message,
                    hint=error.hint,
                )
                for error in status.snapshot.errors
            )
            return HarnessDoctorReport(status, tuple(findings), ())

        findings.append(
            HarnessDoctorFinding(
                code="profile_valid",
                level="ok",
                message="Harness Profile schema version 1 解析通过。",
            )
        )
        if not status.trust_store_available:
            findings.append(
                HarnessDoctorFinding(
                    code="trust_store_unavailable",
                    level="warning",
                    message="用户级 Harness 信任状态暂时不可用。",
                    hint="检查用户状态目录权限；Profile 命令仍不会执行。",
                )
            )
        elif status.trusted:
            findings.append(
                HarnessDoctorFinding(
                    code="profile_trusted",
                    level="ok",
                    message="当前 Profile digest 已由用户信任。",
                )
            )
        else:
            findings.append(
                HarnessDoctorFinding(
                    code="profile_untrusted",
                    level="warning",
                    message="当前 Profile digest 尚未受信任。",
                    hint="运行 /harness trust 预览，再运行 /harness trust --confirm。",
                )
            )

        profile = status.snapshot.profile
        assert profile is not None
        for path_text in profile.knowledge.entrypoints:
            path = (self.workspace_root / path_text).resolve(strict=False)
            exists = path.is_file()
            findings.append(
                HarnessDoctorFinding(
                    code="entrypoint_ok" if exists else "entrypoint_missing",
                    level="ok" if exists else "warning",
                    message=(
                        f"知识入口可读取：{path_text}"
                        if exists
                        else f"知识入口不存在：{path_text}"
                    ),
                    hint="" if exists else "创建文件或从 knowledge.entrypoints 中移除。",
                )
            )
        for path_text in profile.evals.suites:
            path = (self.workspace_root / path_text).resolve(strict=False)
            exists = path.is_file()
            findings.append(
                HarnessDoctorFinding(
                    code="eval_suite_ok" if exists else "eval_suite_missing",
                    level="ok" if exists else "warning",
                    message=(
                        f"Eval Suite 可读取：{path_text}"
                        if exists
                        else f"Eval Suite 尚不存在：{path_text}"
                    ),
                    hint="" if exists else "H5 前可以保留为待建设入口。",
                )
            )
        for check in profile.checks:
            command = shlex.join(check.argv)
            commands.append(f"{check.id}: {command}")
            findings.append(
                HarnessDoctorFinding(
                    code="check_declared",
                    level="info",
                    message=f"已声明检查 {check.id}：{command}",
                )
            )

        findings.append(
            HarnessDoctorFinding(
                code="execution_disabled",
                level="info",
                message="H1 只验证并展示 Profile，不会执行其中的任何命令。",
                hint="命令执行将在 H3 Check Runner 完成后启用。",
            )
        )
        return HarnessDoctorReport(status, tuple(findings), tuple(commands))

    async def trust(self, *, source: str) -> HarnessTrustRecord:
        snapshot = self._load()
        if snapshot.status is HarnessProfileStatus.MISSING:
            raise ValueError("Harness 配置不存在，无法建立信任。")
        if snapshot.status is HarnessProfileStatus.INVALID or snapshot.digest is None:
            raise ValueError("Harness 配置无效，修复后才能建立信任。")
        return await self._trust_store.trust(
            self.workspace_root,
            snapshot.digest,
            source=source,
        )

    async def untrust(self) -> bool:
        return await self._trust_store.untrust(self.workspace_root)

    def _load(self) -> HarnessProfileSnapshot:
        return load_harness_profile(self.workspace_root, self._profile_path)

    async def _read_trust(self) -> tuple[HarnessTrustRecord | None, bool]:
        try:
            return await self._trust_store.get(self.workspace_root), True
        except HarnessTrustStoreError:
            return None, False


def render_harness_status(status: HarnessStatus) -> str:
    snapshot = status.snapshot
    if status.code is HarnessStatusCode.MISSING:
        return (
            "## Harness 尚未配置\n\n"
            f"配置路径：`{snapshot.profile_path}`\n\n"
            "下一步：创建 `.naumi/harness.yaml`，然后运行 `/harness doctor`。"
        )
    if status.code is HarnessStatusCode.INVALID:
        errors = "\n".join(
            f"- {error.message} {error.hint}" for error in snapshot.errors
        )
        return (
            "## Harness 配置无效\n\n"
            f"配置路径：`{snapshot.profile_path}`\n\n{errors}"
        )

    assert snapshot.profile is not None
    digest = snapshot.digest or "-"
    if status.code is HarnessStatusCode.UNTRUSTED:
        title = "## Harness 配置未受信任"
        if status.trust_store_available:
            next_step = (
                "下一步：运行 `/harness trust` 查看 digest 与命令摘要，"
                "再运行 `/harness trust --confirm`。"
            )
        else:
            next_step = (
                "用户级信任状态暂时不可用。下一步：检查 NaumiAgent 状态目录权限；"
                "在修复前不会执行 Profile 命令。"
            )
    else:
        title = "## Harness 已就绪"
        next_step = "当前 H1 仅提供只读诊断；Profile 命令不会被执行。"
    return (
        f"{title}\n\n"
        f"配置路径：`{snapshot.profile_path}`\n"
        f"Profile digest：`{digest}`\n"
        f"检查定义：{len(snapshot.profile.checks)} 条\n\n"
        f"{next_step}"
    )


def render_harness_doctor(report: HarnessDoctorReport) -> str:
    lines = ["## Harness 诊断", "", render_harness_status(report.status), "", "### 检查结果"]
    icons = {"ok": "✅", "info": "ℹ️", "warning": "⚠️", "error": "❌"}
    for finding in report.findings:
        suffix = f"；{finding.hint}" if finding.hint else ""
        lines.append(f"- {icons[finding.level]} {finding.message}{suffix}")
    if report.command_summaries:
        lines.extend(("", "### 配置中的命令（仅展示，不执行）"))
        lines.extend(f"- `{command}`" for command in report.command_summaries)
    return "\n".join(lines)
