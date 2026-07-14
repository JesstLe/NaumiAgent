"""Deterministic repository knowledge contracts and budget primitives."""

from __future__ import annotations

import hashlib
import math
import re
import stat
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal

from naumi_agent.harness.models import HarnessProfile

_L0_TOKEN_LIMIT = 1_000
_TOTAL_TOKEN_LIMIT = 12_000
_MODEL_WINDOW_FRACTION = 0.15
_TRUNCATION_MARKER = "…（内容已按知识预算截断）"
_BUILD_MANIFEST_NAMES = frozenset({"pyproject.toml", "package.json", "Package.swift"})
_DEFAULT_EXCLUDED_PARTS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".cache",
    ".next",
    "build",
    "dist",
})
_UNSUPPORTED_ARTIFACT_SUFFIXES = frozenset({
    ".7z",
    ".avi",
    ".bmp",
    ".diff",
    ".dmg",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".log",
    ".mov",
    ".mp3",
    ".mp4",
    ".patch",
    ".pdf",
    ".png",
    ".tar",
    ".tiff",
    ".webp",
    ".zip",
})
_SENSITIVE_NAMES = frozenset({
    ".npmrc",
    ".pypirc",
    "auth.json",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
    "token.json",
})
_SENSITIVE_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
_BASE64_LINE_RE = re.compile(r"[A-Za-z0-9+/=_-]+")
_TASK_TOKEN_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+[/\\])+[A-Za-z0-9_.-]+"
    r"|[A-Za-z_][A-Za-z0-9_-]{1,}"
    r"|[\u3400-\u9fff]{2,}"
)
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([.A-Za-z0-9_]+)", re.MULTILINE)
_RANK_STOP_TOKENS = frozenset({
    "src",
    "test",
    "tests",
    "file",
    "code",
    "修改",
    "优化",
    "调整",
    "继续",
})


class KnowledgeKind(StrEnum):
    """Repository source categories used by deterministic ranking."""

    INSTRUCTION = "instruction"
    ENTRYPOINT = "entrypoint"
    BUILD = "build"
    SOURCE = "source"
    TEST = "test"
    DOCUMENT = "document"


class KnowledgeLevel(StrEnum):
    """Progressive disclosure levels."""

    L0 = "l0"
    L1 = "l1"
    L2 = "l2"


@dataclass(frozen=True)
class KnowledgeWarning:
    code: str
    message: str
    hint: str = ""
    path: str = ""


@dataclass(frozen=True)
class KnowledgeCandidate:
    id: str
    path: str
    kind: KnowledgeKind
    digest: str
    size_bytes: int
    modified_ns: int
    scope: str = ""
    changed: bool = False
    content: str = field(default="", repr=False, compare=False)


@dataclass(frozen=True)
class KnowledgeIndexSnapshot:
    workspace_root: Path
    profile_digest: str
    fingerprint: str
    git_head: str | None
    changed_paths: tuple[str, ...]
    candidates: tuple[KnowledgeCandidate, ...]
    warnings: tuple[KnowledgeWarning, ...] = ()

    def instructions_for(self, target_path: str) -> tuple[KnowledgeCandidate, ...]:
        """Return broad-to-specific instruction files applicable to a target."""
        normalized = _normalize_relative_text(target_path)
        target = PurePosixPath(normalized)
        applicable: list[KnowledgeCandidate] = []
        for candidate in self.candidates:
            if candidate.kind is not KnowledgeKind.INSTRUCTION:
                continue
            if not candidate.scope:
                applicable.append(candidate)
                continue
            scope = PurePosixPath(candidate.scope)
            if target == scope or target.is_relative_to(scope):
                applicable.append(candidate)
        return tuple(sorted(applicable, key=lambda item: (_path_depth(item.scope), item.path)))


@dataclass(frozen=True)
class KnowledgeSelection:
    level: KnowledgeLevel
    content: str
    source_ids: tuple[str, ...]
    source_paths: tuple[str, ...]
    reasons: tuple[tuple[str, tuple[str, ...]], ...]
    estimated_tokens: int
    budget_tokens: int
    truncated: bool


@dataclass(frozen=True)
class RankedKnowledgeCandidate:
    candidate: KnowledgeCandidate
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeReadResult:
    status: Literal[
        "ok",
        "missing",
        "ambiguous",
        "unsafe",
        "untrusted",
        "invalid",
    ]
    content: str
    source: KnowledgeCandidate | None
    estimated_tokens: int
    budget_tokens: int
    truncated: bool
    message: str = ""
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClippedKnowledgeText:
    text: str
    truncated: bool
    estimated_tokens: int


@dataclass(frozen=True)
class KnowledgeBudget:
    """Effective L0/L1 allocation after all configured ceilings."""

    l0_tokens: int
    l1_tokens: int
    total_tokens: int
    profile_l1_tokens: int
    model_window: int | None

    @classmethod
    def for_model(
        cls,
        *,
        profile_l1: int,
        model_window: int | None,
    ) -> KnowledgeBudget:
        if not 1 <= profile_l1 <= _TOTAL_TOKEN_LIMIT:
            raise ValueError("Profile L1 知识预算必须在 1 到 12000 tokens 之间。")
        if model_window is not None and model_window <= 0:
            raise ValueError("模型上下文窗口必须是正整数。")

        requested_total = _L0_TOKEN_LIMIT + profile_l1
        if model_window is None:
            available_total = _TOTAL_TOKEN_LIMIT
        else:
            available_total = math.floor(model_window * _MODEL_WINDOW_FRACTION)
        total = max(0, min(requested_total, _TOTAL_TOKEN_LIMIT, available_total))
        l0 = min(_L0_TOKEN_LIMIT, total)
        l1 = min(profile_l1, max(0, total - l0))
        return cls(
            l0_tokens=l0,
            l1_tokens=l1,
            total_tokens=l0 + l1,
            profile_l1_tokens=profile_l1,
            model_window=model_window,
        )


def estimate_knowledge_tokens(text: str) -> int:
    """Return a deterministic conservative cross-provider token estimate."""
    if not text:
        return 0
    return math.ceil(len(text.encode("utf-8")) / 3)


def clip_text_to_token_budget(text: str, budget: int) -> ClippedKnowledgeText:
    """Clip text without splitting Unicode characters and stay within budget."""
    if budget < 0:
        raise ValueError("知识预算不能为负数。")
    estimated = estimate_knowledge_tokens(text)
    if estimated <= budget:
        return ClippedKnowledgeText(text, False, estimated)
    if budget == 0:
        return ClippedKnowledgeText("", bool(text), 0)

    marker = _fit_character_prefix(_TRUNCATION_MARKER, budget)
    marker_tokens = estimate_knowledge_tokens(marker)
    if marker != _TRUNCATION_MARKER:
        return ClippedKnowledgeText(marker, True, marker_tokens)

    prefix = ""
    for line in text.splitlines(keepends=True):
        candidate = f"{prefix}{line}{_TRUNCATION_MARKER}"
        if estimate_knowledge_tokens(candidate) > budget:
            break
        prefix += line

    if not prefix:
        prefix_budget = max(0, budget - marker_tokens)
        prefix = _fit_character_prefix(text, prefix_budget)

    prefix = prefix.rstrip("\n")
    clipped = f"{prefix}\n{_TRUNCATION_MARKER}" if prefix else _TRUNCATION_MARKER
    while clipped and estimate_knowledge_tokens(clipped) > budget:
        prefix = prefix[:-1]
        clipped = f"{prefix}\n{_TRUNCATION_MARKER}" if prefix else _TRUNCATION_MARKER
    return ClippedKnowledgeText(
        clipped,
        True,
        estimate_knowledge_tokens(clipped),
    )


def knowledge_id_for_path(path: str) -> str:
    """Build a stable ID from one workspace-relative POSIX path."""
    normalized = str(PurePosixPath(path.replace("\\", "/")))
    if normalized.startswith("./"):
        normalized = normalized[2:]
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"kn_{digest[:16]}"


def knowledge_digest(data: bytes) -> str:
    """Hash exact repository bytes for cache and evidence identity."""
    return hashlib.sha256(data).hexdigest()


class RepositoryKnowledgeIndex:
    """Build immutable repository knowledge metadata without model or network use."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        git_executable: str = "git",
        git_timeout_seconds: float = 2.0,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        if not self.workspace_root.is_dir():
            raise ValueError("知识工作区必须是存在的目录。")
        if not git_executable.strip():
            raise ValueError("Git 可执行文件名不能为空。")
        if git_timeout_seconds <= 0:
            raise ValueError("Git 超时必须大于 0 秒。")
        self._git_executable = git_executable
        self._git_timeout_seconds = git_timeout_seconds

    def build(
        self,
        profile: HarnessProfile,
        *,
        profile_digest: str,
    ) -> KnowledgeIndexSnapshot:
        if not re.fullmatch(r"[0-9a-f]{64}", profile_digest):
            raise ValueError("Profile digest 必须是 64 位小写 SHA-256。")

        warnings: list[KnowledgeWarning] = []
        git_head, changed_paths, git_warnings = self._read_git_state()
        warnings.extend(git_warnings)
        proposed = self._discover_paths(profile, warnings)
        candidates: list[KnowledgeCandidate] = []
        changed = set(changed_paths)
        for relative_path, kind in sorted(proposed.items()):
            candidate = self._inspect_candidate(
                relative_path,
                kind,
                profile,
                changed=relative_path in changed,
                warnings=warnings,
            )
            if candidate is not None:
                candidates.append(candidate)

        ordered_candidates = tuple(sorted(candidates, key=lambda item: item.path))
        ordered_warnings = _deduplicate_warnings(warnings)
        fingerprint = _index_fingerprint(
            profile_digest=profile_digest,
            git_head=git_head,
            changed_paths=changed_paths,
            candidates=ordered_candidates,
        )
        return KnowledgeIndexSnapshot(
            workspace_root=self.workspace_root,
            profile_digest=profile_digest,
            fingerprint=fingerprint,
            git_head=git_head,
            changed_paths=changed_paths,
            candidates=ordered_candidates,
            warnings=ordered_warnings,
        )

    def rank(
        self,
        snapshot: KnowledgeIndexSnapshot,
        task: str,
        *,
        limit: int = 8,
    ) -> tuple[RankedKnowledgeCandidate, ...]:
        """Rank indexed candidates using deterministic repository evidence."""
        self._validate_snapshot(snapshot)
        if limit < 1:
            raise ValueError("知识候选数量上限必须大于 0。")
        normalized_task = task.replace("\\", "/").lower()
        task_tokens = _task_tokens(normalized_task)
        explicit: list[KnowledgeCandidate] = []
        text_by_id: dict[str, str] = {}
        for candidate in snapshot.candidates:
            if candidate.kind is KnowledgeKind.INSTRUCTION:
                continue
            if (
                candidate.path.lower() in normalized_task
                or PurePosixPath(candidate.path).name.lower() in task_tokens
            ):
                explicit.append(candidate)
            text_by_id[candidate.id] = candidate.content

        related_stems: set[str] = set()
        imported_stems: set[str] = set()
        for candidate in explicit:
            stem = PurePosixPath(candidate.path).stem.lower()
            related_stems.update({stem, f"test_{stem}", f"{stem}_test"})
            content = text_by_id.get(candidate.id, "")
            for module in _IMPORT_RE.findall(content):
                imported_stems.add(module.lstrip(".").split(".")[-1].lower())

        ranked: list[RankedKnowledgeCandidate] = []
        for candidate in snapshot.candidates:
            if candidate.kind is KnowledgeKind.INSTRUCTION:
                continue
            path = candidate.path.lower()
            path_obj = PurePosixPath(path)
            name = path_obj.name
            stem = path_obj.stem
            content = text_by_id.get(candidate.id, "").lower()
            reasons: list[str] = []
            score = 0
            if path in normalized_task:
                score += 600
                reasons.append("explicit_path")
            elif name in task_tokens:
                score += 320
                reasons.append("filename")
            if stem in task_tokens:
                score += 180
                reasons.append("stem")
            path_tokens = _path_tokens(path)
            matched_path_tokens = sorted(path_tokens.intersection(task_tokens))
            if matched_path_tokens:
                score += min(180, 30 * len(matched_path_tokens))
                reasons.append(f"path_tokens:{','.join(matched_path_tokens)}")
            substring_tokens = sorted(
                token
                for token in task_tokens
                if len(token) >= 3 and token in path and token not in path_tokens
            )
            if substring_tokens:
                score += min(100, 25 * len(substring_tokens))
                reasons.append(f"path_substrings:{','.join(substring_tokens[:4])}")
            content_matches = sorted(
                token
                for token in task_tokens
                if len(token) >= 3 and token in content
            )
            if content_matches:
                score += min(120, 12 * len(content_matches))
                reasons.append(f"text:{','.join(content_matches[:5])}")
            symbol_definitions = sorted(
                token
                for token in task_tokens
                if _content_defines_symbol(content, token)
            )
            if symbol_definitions:
                score += min(360, 240 * len(symbol_definitions))
                reasons.append(
                    f"symbol_definition:{','.join(symbol_definitions[:3])}"
                )
            if candidate.changed:
                score += 40
                reasons.append("git_changed")
            if stem in related_stems:
                score += 140
                reasons.append("source_test_pair")
            if stem in imported_stems:
                score += 130
                reasons.append("import_relation")
            if candidate.kind is KnowledgeKind.ENTRYPOINT:
                score += 20
                reasons.append("entrypoint")
            elif candidate.kind is KnowledgeKind.BUILD:
                score += 10
                reasons.append("build_manifest")
            if score > 0:
                ranked.append(RankedKnowledgeCandidate(
                    candidate=candidate,
                    score=score,
                    reasons=tuple(reasons),
                ))
        ranked.sort(
            key=lambda item: (-item.score, item.candidate.path),
        )
        return tuple(ranked[:limit])

    def is_current(self, snapshot: KnowledgeIndexSnapshot) -> bool:
        """Check cached metadata plus Git state during periodic full audits."""
        if not self.metadata_is_current(snapshot):
            return False
        git_head, changed_paths, warnings = self._read_git_state()
        warning_codes = {item.code for item in warnings}
        snapshot_warning_codes = {item.code for item in snapshot.warnings}
        if git_head != snapshot.git_head or changed_paths != snapshot.changed_paths:
            return False
        if warning_codes.intersection({"git_timeout", "git_unavailable"}):
            return False
        if warning_codes != snapshot_warning_codes.intersection({
            "git_not_repository",
            "git_status_unavailable",
            "git_timeout",
            "git_unavailable",
        }):
            return False
        return True

    def metadata_is_current(self, snapshot: KnowledgeIndexSnapshot) -> bool:
        """Cheaply check known candidate identity without rereading file bodies."""
        if snapshot.workspace_root != self.workspace_root:
            return False
        for candidate in snapshot.candidates:
            path = self.workspace_root / candidate.path
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(self.workspace_root)
                file_stat = resolved.stat()
            except (OSError, ValueError):
                return False
            if (
                file_stat.st_size != candidate.size_bytes
                or file_stat.st_mtime_ns != candidate.modified_ns
            ):
                return False
        return True

    def read(
        self,
        snapshot: KnowledgeIndexSnapshot,
        *,
        query: str | None = None,
        path: str | None = None,
        max_tokens: int = 4_000,
    ) -> KnowledgeReadResult:
        """Read one current indexed file by exact path or deterministic query."""
        self._validate_snapshot(snapshot)
        if not 1 <= max_tokens <= 4_000:
            raise ValueError("L2 知识读取预算必须在 1 到 4000 tokens 之间。")
        normalized_query = query.strip() if query is not None else ""
        normalized_path = path.strip() if path is not None else ""
        if bool(normalized_query) == bool(normalized_path):
            raise ValueError("必须且只能提供 query 或 path 之一。")

        candidate: KnowledgeCandidate | None
        if normalized_path:
            try:
                safe_path = _normalize_relative_text(normalized_path)
            except ValueError:
                return _empty_read_result(
                    status="unsafe",
                    max_tokens=max_tokens,
                    message="知识路径越过工作区边界；请使用安全的相对路径。",
                )
            candidate = next(
                (item for item in snapshot.candidates if item.path == safe_path),
                None,
            )
            if candidate is None:
                return _empty_read_result(
                    status="missing",
                    max_tokens=max_tokens,
                    message="知识路径未进入当前索引；请检查 Profile include/exclude。",
                )
        else:
            matches = self._query_candidates(snapshot, normalized_query)
            if not matches:
                return _empty_read_result(
                    status="missing",
                    max_tokens=max_tokens,
                    message="没有找到匹配知识；请提供更精确的符号或相对路径。",
                )
            top_score = matches[0][0]
            top = [item for score, item in matches if score == top_score]
            if len(top) > 1:
                return KnowledgeReadResult(
                    status="ambiguous",
                    content="",
                    source=None,
                    estimated_tokens=0,
                    budget_tokens=max_tokens,
                    truncated=False,
                    message="知识查询命中多个同等候选；请改用相对路径。",
                    candidates=tuple(sorted(item.path for item in top)),
                )
            candidate = top[0]

        text = self._read_current_text(snapshot, candidate)
        if text is None:
            return _empty_read_result(
                status="invalid",
                max_tokens=max_tokens,
                message="知识文件已变化或不可读取；请重新建立知识索引。",
            )
        clipped = clip_text_to_token_budget(text, max_tokens)
        return KnowledgeReadResult(
            status="ok",
            content=clipped.text,
            source=candidate,
            estimated_tokens=clipped.estimated_tokens,
            budget_tokens=max_tokens,
            truncated=clipped.truncated,
        )

    def sources_are_current(
        self,
        snapshot: KnowledgeIndexSnapshot,
        source_paths: tuple[str, ...],
    ) -> bool:
        """Verify exact digests for the small set embedded in a cached bundle."""
        self._validate_snapshot(snapshot)
        by_path = {item.path: item for item in snapshot.candidates}
        for path in source_paths:
            candidate = by_path.get(path)
            if candidate is None or self._read_current_text(snapshot, candidate) is None:
                return False
        return True

    def _query_candidates(
        self,
        snapshot: KnowledgeIndexSnapshot,
        query: str,
    ) -> list[tuple[int, KnowledgeCandidate]]:
        lowered = query.lower()
        matches: list[tuple[int, KnowledgeCandidate]] = []
        for candidate in snapshot.candidates:
            path = candidate.path.lower()
            path_obj = PurePosixPath(path)
            score = 0
            if lowered in {candidate.id.lower(), path}:
                score = 1_000
            elif lowered in {path_obj.name, path_obj.stem}:
                score = 800
            elif lowered in path:
                score = 500
            else:
                if lowered in candidate.content.lower():
                    score = 100
            if score:
                matches.append((score, candidate))
        matches.sort(key=lambda item: (-item[0], item[1].path))
        return matches

    def _read_current_text(
        self,
        snapshot: KnowledgeIndexSnapshot,
        candidate: KnowledgeCandidate,
    ) -> str | None:
        path = self.workspace_root / candidate.path
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.workspace_root)
            raw = _read_bounded_bytes(resolved, candidate.size_bytes)
        except (OSError, ValueError):
            return None
        if raw is None:
            return None
        if knowledge_digest(raw) != candidate.digest or b"\x00" in raw:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def _validate_snapshot(self, snapshot: KnowledgeIndexSnapshot) -> None:
        if snapshot.workspace_root != self.workspace_root:
            raise ValueError("知识索引不属于当前工作区。")

    def _discover_paths(
        self,
        profile: HarnessProfile,
        warnings: list[KnowledgeWarning],
    ) -> dict[str, KnowledgeKind]:
        proposed: dict[str, KnowledgeKind] = {}

        self._add_glob_matches(
            "**/AGENTS.md",
            KnowledgeKind.INSTRUCTION,
            profile,
            proposed,
            warnings,
        )
        for entrypoint in profile.knowledge.entrypoints:
            self._propose_path(
                entrypoint,
                KnowledgeKind.ENTRYPOINT,
                profile,
                proposed,
                warnings,
                explicit=True,
            )
        for manifest_name in sorted(_BUILD_MANIFEST_NAMES):
            self._add_glob_matches(
                f"**/{manifest_name}",
                KnowledgeKind.BUILD,
                profile,
                proposed,
                warnings,
            )
        for pattern in profile.knowledge.include:
            self._add_glob_matches(
                pattern,
                KnowledgeKind.SOURCE,
                profile,
                proposed,
                warnings,
            )
        return proposed

    def _add_glob_matches(
        self,
        pattern: str,
        kind: KnowledgeKind,
        profile: HarnessProfile,
        proposed: dict[str, KnowledgeKind],
        warnings: list[KnowledgeWarning],
    ) -> None:
        try:
            matches = self.workspace_root.glob(pattern)
            for path in matches:
                try:
                    relative = path.relative_to(self.workspace_root).as_posix()
                except ValueError:
                    continue
                self._propose_path(
                    relative,
                    kind,
                    profile,
                    proposed,
                    warnings,
                )
        except (OSError, ValueError) as exc:
            warnings.append(KnowledgeWarning(
                code="glob_unavailable",
                message=f"知识路径模式暂时无法扫描：{pattern}",
                hint="检查路径权限或从 knowledge.include 中移除该模式。",
                path=str(exc)[:160],
            ))

    def _propose_path(
        self,
        path_text: str,
        kind: KnowledgeKind,
        profile: HarnessProfile,
        proposed: dict[str, KnowledgeKind],
        warnings: list[KnowledgeWarning],
        *,
        explicit: bool = False,
    ) -> None:
        try:
            relative = _normalize_relative_text(path_text)
        except ValueError:
            warnings.append(KnowledgeWarning(
                code="path_escape",
                message=f"知识路径不是安全的工作区相对路径：{path_text}",
                hint="改用工作区内、不含 .. 的相对路径。",
                path=path_text,
            ))
            return
        if _is_default_excluded(relative) or _matches_any_glob(
            relative,
            profile.knowledge.exclude,
        ):
            return
        lexical = self.workspace_root / relative
        if explicit and not lexical.exists():
            warnings.append(KnowledgeWarning(
                code="file_missing",
                message=f"知识入口不存在：{relative}",
                hint="创建文件或从 knowledge.entrypoints 中移除。",
                path=relative,
            ))
            return
        inferred = _infer_kind(relative, kind)
        current = proposed.get(relative)
        if current is None or _kind_priority(inferred) > _kind_priority(current):
            proposed[relative] = inferred

    def _inspect_candidate(
        self,
        relative: str,
        kind: KnowledgeKind,
        profile: HarnessProfile,
        *,
        changed: bool,
        warnings: list[KnowledgeWarning],
    ) -> KnowledgeCandidate | None:
        lexical = self.workspace_root / relative
        try:
            resolved = lexical.resolve(strict=True)
            resolved.relative_to(self.workspace_root)
        except (FileNotFoundError, OSError):
            warnings.append(KnowledgeWarning(
                code="file_unreadable",
                message=f"知识文件无法读取：{relative}",
                hint="检查文件是否存在及其权限。",
                path=relative,
            ))
            return None
        except ValueError:
            warnings.append(KnowledgeWarning(
                code="path_escape",
                message=f"知识文件越过工作区边界：{relative}",
                hint="移除该 symlink 或从知识配置中排除。",
                path=relative,
            ))
            return None
        try:
            file_stat = resolved.stat()
        except OSError:
            warnings.append(KnowledgeWarning(
                code="file_unreadable",
                message=f"知识文件状态不可读取：{relative}",
                hint="检查文件权限后重试。",
                path=relative,
            ))
            return None
        if not stat.S_ISREG(file_stat.st_mode):
            return None
        if file_stat.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH) == 0:
            warnings.append(KnowledgeWarning(
                code="file_unreadable",
                message=f"知识文件没有读取权限：{relative}",
                hint="恢复最小读取权限或从知识配置中排除。",
                path=relative,
            ))
            return None
        if _is_sensitive_path(relative):
            warnings.append(KnowledgeWarning(
                code="sensitive_path",
                message=f"敏感文件不会进入知识索引：{relative}",
                hint="只在安全的外部凭据存储中管理密钥。",
                path=relative,
            ))
            return None
        if resolved.suffix.lower() in _UNSUPPORTED_ARTIFACT_SUFFIXES:
            warnings.append(KnowledgeWarning(
                code="unsupported_artifact",
                message=f"原始制品不会进入模型上下文：{relative}",
                hint="提供文本摘要、路径和 digest，而不是原始制品。",
                path=relative,
            ))
            return None
        if file_stat.st_size > profile.knowledge.max_file_bytes:
            warnings.append(KnowledgeWarning(
                code="file_too_large",
                message=f"知识文件超过大小上限：{relative}",
                hint=(
                    "拆分文档，或在确认内容安全后调整 "
                    "knowledge.max_file_bytes。"
                ),
                path=relative,
            ))
            return None
        try:
            raw = _read_bounded_bytes(
                resolved,
                profile.knowledge.max_file_bytes,
            )
        except OSError:
            warnings.append(KnowledgeWarning(
                code="file_unreadable",
                message=f"知识文件读取失败：{relative}",
                hint="检查文件权限后重试。",
                path=relative,
            ))
            return None
        if raw is None:
            warnings.append(KnowledgeWarning(
                code="file_too_large",
                message=f"知识文件在读取期间超过大小上限：{relative}",
                hint="等待写入完成、拆分文档，或从知识配置中排除。",
                path=relative,
            ))
            return None
        if b"\x00" in raw:
            warnings.append(KnowledgeWarning(
                code="binary_file",
                message=f"二进制文件不会进入知识索引：{relative}",
                hint="提供文本摘要或源码侧引用。",
                path=relative,
            ))
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append(KnowledgeWarning(
                code="binary_file",
                message=f"非 UTF-8 文件不会进入知识索引：{relative}",
                hint="转换为 UTF-8 文本或提供摘要。",
                path=relative,
            ))
            return None
        if _looks_like_base64_payload(text):
            warnings.append(KnowledgeWarning(
                code="base64_payload",
                message=f"Base64 载荷不会进入知识索引：{relative}",
                hint="保存为制品，并在文档中引用路径和 digest。",
                path=relative,
            ))
            return None
        scope = ""
        if kind is KnowledgeKind.INSTRUCTION:
            parent = PurePosixPath(relative).parent
            scope = "" if str(parent) == "." else str(parent)
        return KnowledgeCandidate(
            id=knowledge_id_for_path(relative),
            path=relative,
            kind=kind,
            digest=knowledge_digest(raw),
            size_bytes=file_stat.st_size,
            modified_ns=file_stat.st_mtime_ns,
            scope=scope,
            changed=changed,
            content=text,
        )

    def _read_git_state(
        self,
    ) -> tuple[str | None, tuple[str, ...], tuple[KnowledgeWarning, ...]]:
        try:
            inside = self._run_git("rev-parse", "--is-inside-work-tree")
            if inside.returncode != 0 or inside.stdout.strip() != "true":
                return None, (), (_git_not_repository_warning(),)
            head_result = self._run_git("rev-parse", "HEAD")
            head = head_result.stdout.strip() if head_result.returncode == 0 else None
            status_result = self._run_git(
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            )
            if status_result.returncode != 0:
                return head, (), (KnowledgeWarning(
                    code="git_status_unavailable",
                    message="Git 改动状态暂时不可用。",
                    hint="确认仓库未被其他进程锁定后重试。",
                ),)
            paths = _parse_git_porcelain_z(status_result.stdout)
            return head, paths, ()
        except subprocess.TimeoutExpired:
            return None, (), (KnowledgeWarning(
                code="git_timeout",
                message="读取 Git 状态超时，知识索引将忽略改动加权。",
                hint="检查仓库锁或 Git 性能后重试。",
            ),)
        except (FileNotFoundError, OSError):
            return None, (), (KnowledgeWarning(
                code="git_unavailable",
                message="系统中未找到可用 Git，知识索引将忽略改动加权。",
                hint="安装 Git 或确认 PATH 后重试。",
            ),)

    def _run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self._git_executable, *args],
            cwd=self.workspace_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=self._git_timeout_seconds,
        )


def _fit_character_prefix(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    accepted: list[str] = []
    for character in text:
        candidate = "".join((*accepted, character))
        if estimate_knowledge_tokens(candidate) > budget:
            break
        accepted.append(character)
    return "".join(accepted)


def _read_bounded_bytes(path: Path, max_bytes: int) -> bytes | None:
    """Read at most max_bytes and detect concurrent growth without unbounded IO."""
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    return raw if len(raw) <= max_bytes else None


def _normalize_relative_text(path_text: str) -> str:
    normalized = path_text.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError("path must stay inside workspace")
    cleaned = str(path)
    if cleaned in {"", "."}:
        raise ValueError("path must identify a file")
    return cleaned.removeprefix("./")


def _path_depth(path_text: str) -> int:
    if not path_text:
        return 0
    return len(PurePosixPath(path_text).parts)


def _kind_priority(kind: KnowledgeKind) -> int:
    return {
        KnowledgeKind.SOURCE: 0,
        KnowledgeKind.DOCUMENT: 1,
        KnowledgeKind.TEST: 2,
        KnowledgeKind.BUILD: 3,
        KnowledgeKind.ENTRYPOINT: 4,
        KnowledgeKind.INSTRUCTION: 5,
    }[kind]


def _infer_kind(relative: str, proposed: KnowledgeKind) -> KnowledgeKind:
    path = PurePosixPath(relative)
    if path.name == "AGENTS.md":
        return KnowledgeKind.INSTRUCTION
    if proposed is KnowledgeKind.ENTRYPOINT:
        return proposed
    if path.name in _BUILD_MANIFEST_NAMES:
        return KnowledgeKind.BUILD
    lowered_parts = {part.lower() for part in path.parts}
    if (
        "tests" in lowered_parts
        or "test" in lowered_parts
        or path.name.lower().startswith("test_")
        or path.name.lower().endswith("_test.py")
    ):
        return KnowledgeKind.TEST
    if path.suffix.lower() in {".md", ".mdx", ".rst", ".txt"}:
        return KnowledgeKind.DOCUMENT
    return proposed


def _is_default_excluded(relative: str) -> bool:
    return bool(_DEFAULT_EXCLUDED_PARTS.intersection(PurePosixPath(relative).parts))


def _matches_any_glob(relative: str, patterns: tuple[str, ...]) -> bool:
    path = PurePosixPath(relative)
    return any(path.match(pattern) or _glob_prefix_match(relative, pattern) for pattern in patterns)


def _glob_prefix_match(relative: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return relative == prefix or relative.startswith(f"{prefix}/")
    return False


def _is_sensitive_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    name = path.name.lower()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in _SENSITIVE_NAMES
        or path.suffix.lower() in _SENSITIVE_SUFFIXES
    )


def _looks_like_base64_payload(text: str) -> bool:
    stripped = "".join(line.strip() for line in text.splitlines())
    if len(stripped) < 256:
        return False
    match = _BASE64_LINE_RE.fullmatch(stripped)
    return match is not None


def _git_not_repository_warning() -> KnowledgeWarning:
    return KnowledgeWarning(
        code="git_not_repository",
        message="当前工作区不是 Git 仓库，知识索引将忽略改动加权。",
        hint="如需改动相关性，请在 Git 工作区中运行。",
    )


def _parse_git_porcelain_z(output: str) -> tuple[str, ...]:
    changed: set[str] = set()
    records = output.split("\x00")
    skip_next = False
    for record in records:
        if not record:
            continue
        if skip_next:
            skip_next = False
            continue
        if len(record) < 4:
            continue
        status = record[:2]
        path_text = record[3:]
        try:
            changed.add(_normalize_relative_text(path_text))
        except ValueError:
            continue
        if "R" in status or "C" in status:
            skip_next = True
    return tuple(sorted(changed))


def _index_fingerprint(
    *,
    profile_digest: str,
    git_head: str | None,
    changed_paths: tuple[str, ...],
    candidates: tuple[KnowledgeCandidate, ...],
) -> str:
    identity = [profile_digest, git_head or "-"]
    identity.extend(changed_paths)
    identity.extend(
        f"{item.path}\0{item.digest}\0{item.modified_ns}\0{int(item.changed)}"
        for item in candidates
    )
    return hashlib.sha256("\n".join(identity).encode("utf-8")).hexdigest()


def _deduplicate_warnings(
    warnings: list[KnowledgeWarning],
) -> tuple[KnowledgeWarning, ...]:
    unique = {
        (item.code, item.path, item.message, item.hint): item
        for item in warnings
    }
    return tuple(unique[key] for key in sorted(unique))


def _task_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in _TASK_TOKEN_RE.findall(text):
        normalized = match.lower().replace("\\", "/")
        if "/" in normalized:
            tokens.update(_path_tokens(normalized))
            tokens.add(normalized)
        elif normalized not in _RANK_STOP_TOKENS:
            tokens.add(normalized)
    return tokens


def _path_tokens(path_text: str) -> set[str]:
    return {
        item
        for item in re.split(r"[^a-z0-9_]+", path_text.lower())
        if len(item) >= 2 and item not in _RANK_STOP_TOKENS
    }


def _content_defines_symbol(content: str, token: str) -> bool:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{2,}", token):
        return False
    definition = re.compile(
        rf"\b(?:class|def|enum|function|interface|protocol|struct)\s+"
        rf"{re.escape(token)}\b",
        re.IGNORECASE,
    )
    return definition.search(content) is not None


def _empty_read_result(
    *,
    status: Literal["missing", "unsafe", "invalid"],
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
