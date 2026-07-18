"""Bounded, structured static facts for self-review evolution evidence."""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

_MAX_SELF_REVIEW_FILE_BYTES = 2_000_000
_SECRET_NAME_RE = re.compile(r"(?:api[_-]?key|password|secret|token)", re.IGNORECASE)
SELF_REVIEW_STATIC_RUNNER_VERSION = "self_review_static@1"


class SelfReviewFindingCode(StrEnum):
    BARE_EXCEPT = "bare_except"
    BROAD_EXCEPT = "broad_except"
    HARDCODED_SECRET = "hardcoded_secret"
    LONG_FUNCTION = "long_function"
    MUTABLE_GLOBAL = "mutable_global"
    SYNTAX_ERROR = "syntax_error"
    UNTYPED_PUBLIC_RETURN = "untyped_public_return"


@dataclass(frozen=True, slots=True)
class SelfReviewStaticFinding:
    code: SelfReviewFindingCode
    path: str
    line: int
    symbol: str
    file_sha256: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class SelfReviewScanError:
    code: str
    path: str


@dataclass(frozen=True, slots=True)
class SelfReviewStaticScan:
    workspace_root: str
    files_scanned: int
    findings: tuple[SelfReviewStaticFinding, ...]
    errors: tuple[SelfReviewScanError, ...]


@dataclass(frozen=True, slots=True)
class _ScannedFile:
    findings: tuple[SelfReviewStaticFinding, ...] = ()
    error: SelfReviewScanError | None = None


def scan_self_review_files(
    files: list[Path],
    *,
    workspace_root: str | Path,
) -> SelfReviewStaticScan:
    """Collect bounded AST facts without retaining source or secret values."""
    root = Path(workspace_root).expanduser().resolve(strict=True)
    findings: list[SelfReviewStaticFinding] = []
    errors: list[SelfReviewScanError] = []
    files_scanned = 0
    seen_resolved: set[Path] = set()
    for requested in sorted({Path(path) for path in files}):
        display_path = requested.name
        try:
            resolved = requested.expanduser().resolve(strict=True)
            relative = resolved.relative_to(root).as_posix()
            display_path = relative
        except (OSError, ValueError):
            errors.append(SelfReviewScanError(code="outside_or_missing", path=display_path))
            continue
        if resolved in seen_resolved:
            continue
        seen_resolved.add(resolved)
        scanned = _scan_self_review_file(resolved, relative)
        if scanned.error is not None:
            errors.append(scanned.error)
            continue
        files_scanned += 1
        findings.extend(scanned.findings)
    return SelfReviewStaticScan(
        workspace_root=str(root),
        files_scanned=files_scanned,
        findings=tuple(
            sorted(
                findings,
                key=lambda item: (item.path, item.line, item.code.value, item.symbol),
            )
        ),
        errors=tuple(sorted(errors, key=lambda item: (item.path, item.code))),
    )


def _scan_self_review_file(resolved: Path, relative: str) -> _ScannedFile:
    try:
        stat = resolved.stat()
        if stat.st_size > _MAX_SELF_REVIEW_FILE_BYTES:
            return _ScannedFile(
                error=SelfReviewScanError(code="file_too_large", path=relative)
            )
        raw = resolved.read_bytes()
        source = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return _ScannedFile(
            error=SelfReviewScanError(code="unreadable", path=relative)
        )
    digest = hashlib.sha256(raw).hexdigest()
    observed_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
    try:
        tree = ast.parse(source, filename=relative)
    except SyntaxError as exc:
        return _ScannedFile(
            findings=(
                SelfReviewStaticFinding(
                    code=SelfReviewFindingCode.SYNTAX_ERROR,
                    path=relative,
                    line=max(1, int(exc.lineno or 1)),
                    symbol="<module>",
                    file_sha256=digest,
                    observed_at=observed_at,
                ),
            )
        )
    return _ScannedFile(
        findings=tuple(
            _inspect_self_review_tree(
                tree,
                path=relative,
                file_sha256=digest,
                observed_at=observed_at,
            )
        )
    )


def render_self_review_static_scan(scan: SelfReviewStaticScan) -> str:
    """Render structured facts without exposing matched source values."""
    counts = {
        code: sum(1 for finding in scan.findings if finding.code is code)
        for code in SelfReviewFindingCode
    }
    lines = [
        f"- 源文件: {scan.files_scanned} 个",
        f"- 裸 except: {counts[SelfReviewFindingCode.BARE_EXCEPT]} 处",
        f"- 宽泛 Exception: {counts[SelfReviewFindingCode.BROAD_EXCEPT]} 处",
        f"- 疑似硬编码密钥: {counts[SelfReviewFindingCode.HARDCODED_SECRET]} 处（值已隐藏）",
        f"- 缺少返回类型注解的公开函数: {counts[SelfReviewFindingCode.UNTYPED_PUBLIC_RETURN]} 个",
        f"- 超过 60 行的函数: {counts[SelfReviewFindingCode.LONG_FUNCTION]} 个",
        f"- 模块级可变状态: {counts[SelfReviewFindingCode.MUTABLE_GLOBAL]} 处",
        f"- 语法错误: {counts[SelfReviewFindingCode.SYNTAX_ERROR]} 处",
    ]
    if scan.errors:
        lines.append(f"- 扫描错误: {len(scan.errors)} 个")
        lines.extend(f"  - `{error.path}`: `{error.code}`" for error in scan.errors[:20])
    return "\n".join(lines)


def _inspect_self_review_tree(
    tree: ast.AST,
    *,
    path: str,
    file_sha256: str,
    observed_at: str,
) -> list[SelfReviewStaticFinding]:
    findings: list[SelfReviewStaticFinding] = []

    def add(code: SelfReviewFindingCode, node: ast.AST, symbol: str) -> None:
        findings.append(
            SelfReviewStaticFinding(
                code=code,
                path=path,
                line=max(1, int(getattr(node, "lineno", 1))),
                symbol=symbol,
                file_sha256=file_sha256,
                observed_at=observed_at,
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                add(SelfReviewFindingCode.BARE_EXCEPT, node, "<except>")
            elif _is_broad_exception(node.type):
                add(SelfReviewFindingCode.BROAD_EXCEPT, node, "<except>")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_") and node.returns is None:
                add(SelfReviewFindingCode.UNTYPED_PUBLIC_RETURN, node, node.name)
            end = int(getattr(node, "end_lineno", node.lineno))
            if end - node.lineno + 1 > 60:
                add(SelfReviewFindingCode.LONG_FUNCTION, node, node.name)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if (
                isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and len(value.value) >= 8
            ):
                for name in _assignment_names(node):
                    if (
                        _SECRET_NAME_RE.search(name)
                        and value.value.casefold() != name.casefold()
                    ):
                        add(SelfReviewFindingCode.HARDCODED_SECRET, node, name)

    if isinstance(tree, ast.Module):
        for statement in tree.body:
            assignment = statement if isinstance(statement, (ast.Assign, ast.AnnAssign)) else None
            if assignment is None:
                continue
            if isinstance(assignment.value, (ast.Dict, ast.List, ast.Set)):
                for name in _assignment_names(assignment):
                    if not (name.startswith("__") and name.endswith("__")):
                        add(SelfReviewFindingCode.MUTABLE_GLOBAL, assignment, name)
    return findings


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return tuple(target.id for target in targets if isinstance(target, ast.Name))


def _is_broad_exception(node: ast.expr | None) -> bool:
    candidates = node.elts if isinstance(node, ast.Tuple) else (node,)
    return any(
        isinstance(candidate, ast.Name)
        and candidate.id in {"Exception", "BaseException"}
        for candidate in candidates
    )


__all__ = [
    "SELF_REVIEW_STATIC_RUNNER_VERSION",
    "SelfReviewFindingCode",
    "SelfReviewScanError",
    "SelfReviewStaticFinding",
    "SelfReviewStaticScan",
    "render_self_review_static_scan",
    "scan_self_review_files",
]
