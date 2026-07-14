"""Shared read-only Harness status, doctor, and user-only trust facade."""

from __future__ import annotations

import asyncio
import shlex
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from naumi_agent.harness.context import (
    HarnessKnowledgeContextComposer,
    KnowledgeContextBundle,
    safe_markdown_fence,
)
from naumi_agent.harness.knowledge import (
    KnowledgeIndexSnapshot,
    KnowledgeReadResult,
    RepositoryKnowledgeIndex,
)
from naumi_agent.harness.models import (
    HarnessProfile,
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


class HarnessKnowledgeStatusCode(StrEnum):
    MISSING = "missing"
    INVALID = "invalid"
    UNTRUSTED = "untrusted"
    READY = "ready"
    ERROR = "error"


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


@dataclass(frozen=True)
class HarnessKnowledgeContextResult:
    code: HarnessKnowledgeStatusCode
    bundle: KnowledgeContextBundle | None
    message: str
    cache_hit: bool = False
    selection_cache_hit: bool = False
    index_fingerprint: str | None = None
    elapsed_ms: int = 0


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
        self._knowledge_index = RepositoryKnowledgeIndex(self.workspace_root)
        self._knowledge_composer = HarnessKnowledgeContextComposer(
            self._knowledge_index
        )
        self._knowledge_cache: KnowledgeIndexSnapshot | None = None
        self._knowledge_lock = asyncio.Lock()
        self._knowledge_build: tuple[
            str,
            asyncio.Task[KnowledgeIndexSnapshot],
        ] | None = None
        self._selection_cache: OrderedDict[
            tuple[str, str, int | None],
            KnowledgeContextBundle,
        ] = OrderedDict()
        self._last_git_audit_at = 0.0
        self._git_audit_interval_seconds = 30.0

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
                message=(
                    "H2 只提供 Profile 诊断与只读仓库知识，"
                    "不会执行其中的任何命令。"
                ),
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

    async def knowledge_context(
        self,
        task: str,
        *,
        model_window: int | None,
    ) -> HarnessKnowledgeContextResult:
        """Compose trusted repository knowledge without persisting its body."""
        started = time.perf_counter()
        status = await self.status()
        unavailable = _knowledge_unavailable(status)
        if unavailable is not None:
            return _with_elapsed(unavailable, started)

        profile = status.snapshot.profile
        digest = status.snapshot.digest
        assert profile is not None and digest is not None
        try:
            snapshot, cache_hit = await self._get_knowledge_snapshot(
                profile,
                digest,
            )
            if not await self._profile_trust_is_current(digest):
                return _with_elapsed(
                    HarnessKnowledgeContextResult(
                        code=HarnessKnowledgeStatusCode.UNTRUSTED,
                        bundle=None,
                        message=(
                            "Harness Profile 在知识组装期间发生变化；"
                            "请重新运行 /harness trust 预览并确认。"
                        ),
                    ),
                    started,
                )
            selection_key = (snapshot.fingerprint, task, model_window)
            bundle = self._selection_cache.get(selection_key)
            selection_cache_hit = bundle is not None
            if (
                bundle is not None
                and not await asyncio.to_thread(
                    self._knowledge_index.sources_are_current,
                    snapshot,
                    bundle.source_paths,
                )
            ):
                await self.invalidate_knowledge_cache()
                snapshot, cache_hit = await self._get_knowledge_snapshot(
                    profile,
                    digest,
                )
                if not await self._profile_trust_is_current(digest):
                    return _with_elapsed(
                        HarnessKnowledgeContextResult(
                            code=HarnessKnowledgeStatusCode.UNTRUSTED,
                            bundle=None,
                            message=(
                                "Harness Profile 在知识重建期间发生变化；"
                                "请重新运行 /harness trust 预览并确认。"
                            ),
                        ),
                        started,
                    )
                selection_key = (snapshot.fingerprint, task, model_window)
                bundle = None
                selection_cache_hit = False
            if bundle is None:
                bundle = await asyncio.to_thread(
                    self._knowledge_composer.compose,
                    task,
                    snapshot,
                    profile,
                    model_window=model_window,
                )
                self._selection_cache[selection_key] = bundle
                self._selection_cache.move_to_end(selection_key)
                while len(self._selection_cache) > 16:
                    self._selection_cache.popitem(last=False)
            else:
                self._selection_cache.move_to_end(selection_key)
        except Exception:
            return _with_elapsed(
                HarnessKnowledgeContextResult(
                    code=HarnessKnowledgeStatusCode.ERROR,
                    bundle=None,
                    message=(
                        "仓库知识索引暂时不可用；主任务可以继续。"
                        "下一步：运行 /harness doctor 检查路径与权限。"
                    ),
                ),
                started,
            )
        return _with_elapsed(
            HarnessKnowledgeContextResult(
                code=HarnessKnowledgeStatusCode.READY,
                bundle=bundle,
                message="受信任仓库知识已按当前任务和模型窗口组装。",
                cache_hit=cache_hit,
                selection_cache_hit=selection_cache_hit,
                index_fingerprint=snapshot.fingerprint,
            ),
            started,
        )

    async def read_knowledge(
        self,
        *,
        query: str | None = None,
        path: str | None = None,
        max_tokens: int = 4_000,
    ) -> KnowledgeReadResult:
        """Read one trusted L2 knowledge item through the current cache."""
        status = await self.status()
        if status.code is HarnessStatusCode.MISSING:
            return _unavailable_read(
                "missing",
                max_tokens,
                "Harness Profile 不存在；先创建 .naumi/harness.yaml。",
            )
        if status.code is HarnessStatusCode.INVALID:
            return _unavailable_read(
                "invalid",
                max_tokens,
                "Harness Profile 无效；先运行 /harness doctor 修复。",
            )
        if status.code is not HarnessStatusCode.TRUSTED:
            return _unavailable_read(
                "untrusted",
                max_tokens,
                "仓库知识尚未受信任；先运行 /harness trust 预览并确认。",
            )
        profile = status.snapshot.profile
        digest = status.snapshot.digest
        assert profile is not None and digest is not None
        try:
            snapshot, _ = await self._get_knowledge_snapshot(profile, digest)
            current_status = await self.status()
            if (
                current_status.code is not HarnessStatusCode.TRUSTED
                or current_status.snapshot.digest != digest
            ):
                return _unavailable_read(
                    "untrusted",
                    max_tokens,
                    "Profile 已变化；重新运行 /harness trust 后再读取知识。",
                )
            return await asyncio.to_thread(
                self._knowledge_index.read,
                snapshot,
                query=query,
                path=path,
                max_tokens=max_tokens,
            )
        except ValueError:
            raise
        except Exception:
            return _unavailable_read(
                "invalid",
                max_tokens,
                "知识索引读取失败；运行 /harness doctor 检查路径与权限。",
            )

    async def _get_knowledge_snapshot(
        self,
        profile: HarnessProfile,
        digest: str,
    ) -> tuple[KnowledgeIndexSnapshot, bool]:
        cached = self._knowledge_cache
        if (
            cached is not None
            and cached.profile_digest == digest
            and await self._cached_snapshot_is_current(cached)
        ):
            return cached, True

        async with self._knowledge_lock:
            current_cache = self._knowledge_cache
            if (
                current_cache is not None
                and current_cache is not cached
                and current_cache.profile_digest == digest
            ):
                return current_cache, True
            if self._knowledge_build is not None and self._knowledge_build[0] == digest:
                build_task = self._knowledge_build[1]
            else:
                build_task = asyncio.create_task(asyncio.to_thread(
                    self._knowledge_index.build,
                    profile,
                    profile_digest=digest,
                ))
                self._knowledge_build = (digest, build_task)

        try:
            built = await build_task
        except BaseException:
            async with self._knowledge_lock:
                if (
                    self._knowledge_build is not None
                    and self._knowledge_build[1] is build_task
                ):
                    self._knowledge_build = None
            raise
        async with self._knowledge_lock:
            existing = self._knowledge_cache
            if (
                existing is not None
                and existing.profile_digest == digest
                and existing is not cached
            ):
                return existing, True
            if (
                self._knowledge_build is not None
                and self._knowledge_build[1] is not build_task
            ):
                return built, False
            self._knowledge_cache = built
            self._selection_cache.clear()
            self._last_git_audit_at = time.monotonic()
            if (
                self._knowledge_build is not None
                and self._knowledge_build[1] is build_task
            ):
                self._knowledge_build = None
        return built, False

    async def _cached_snapshot_is_current(
        self,
        snapshot: KnowledgeIndexSnapshot,
    ) -> bool:
        metadata_current = await asyncio.to_thread(
            self._knowledge_index.metadata_is_current,
            snapshot,
        )
        if not metadata_current:
            return False
        now = time.monotonic()
        if now - self._last_git_audit_at < self._git_audit_interval_seconds:
            return True
        current = await asyncio.to_thread(self._knowledge_index.is_current, snapshot)
        self._last_git_audit_at = now
        return current

    async def invalidate_knowledge_cache(self) -> None:
        """Invalidate repository knowledge after an in-process write operation."""
        async with self._knowledge_lock:
            self._knowledge_cache = None
            self._selection_cache.clear()
            self._last_git_audit_at = 0.0

    async def _profile_trust_is_current(self, digest: str) -> bool:
        status = await self.status()
        return (
            status.code is HarnessStatusCode.TRUSTED
            and status.snapshot.digest == digest
        )

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
        next_step = (
            "已启用受信任的只读仓库知识；Profile 中的检查命令仍不会被执行。"
        )
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


def render_harness_knowledge(result: KnowledgeReadResult) -> str:
    """Render one L2 result consistently for slash commands and Agent tools."""
    if result.status == "ok":
        assert result.source is not None
        fence = safe_markdown_fence(result.content)
        truncated = "是" if result.truncated else "否"
        return (
            "## Harness 仓库知识\n\n"
            f"- 来源：`{result.source.path}`\n"
            f"- Knowledge ID：`{result.source.id}`\n"
            f"- Digest：`{result.source.digest}`\n"
            f"- 估算：{result.estimated_tokens}/{result.budget_tokens} tokens\n"
            f"- 已裁剪：{truncated}\n\n"
            f"{fence}\n{result.content}\n{fence}"
        )
    if result.status == "ambiguous":
        candidates = "\n".join(f"- `{path}`" for path in result.candidates)
        return (
            "## Harness 知识查询不唯一\n\n"
            f"{result.message}\n\n候选：\n{candidates}"
        )
    return (
        "## Harness 知识暂不可用\n\n"
        f"状态：`{result.status}`\n\n"
        f"{result.message or '请提供更精确的知识路径或查询。'}"
    )


def _knowledge_unavailable(
    status: HarnessStatus,
) -> HarnessKnowledgeContextResult | None:
    if status.code is HarnessStatusCode.MISSING:
        return HarnessKnowledgeContextResult(
            code=HarnessKnowledgeStatusCode.MISSING,
            bundle=None,
            message="当前工作区没有 Harness Profile；不会注入仓库知识。",
        )
    if status.code is HarnessStatusCode.INVALID:
        return HarnessKnowledgeContextResult(
            code=HarnessKnowledgeStatusCode.INVALID,
            bundle=None,
            message="Harness Profile 无效；不会注入仓库知识。",
        )
    if status.code is HarnessStatusCode.UNTRUSTED:
        return HarnessKnowledgeContextResult(
            code=HarnessKnowledgeStatusCode.UNTRUSTED,
            bundle=None,
            message="Harness Profile 未受信任；不会注入仓库知识。",
        )
    return None


def _with_elapsed(
    result: HarnessKnowledgeContextResult,
    started: float,
) -> HarnessKnowledgeContextResult:
    return HarnessKnowledgeContextResult(
        code=result.code,
        bundle=result.bundle,
        message=result.message,
        cache_hit=result.cache_hit,
        selection_cache_hit=result.selection_cache_hit,
        index_fingerprint=result.index_fingerprint,
        elapsed_ms=max(0, int((time.perf_counter() - started) * 1_000)),
    )


def _unavailable_read(
    status: Literal["missing", "invalid", "untrusted"],
    max_tokens: int,
    message: str,
) -> KnowledgeReadResult:
    return KnowledgeReadResult(
        status=status,
        content="",
        source=None,
        estimated_tokens=0,
        budget_tokens=max_tokens,
        truncated=False,
        message=message,
    )
