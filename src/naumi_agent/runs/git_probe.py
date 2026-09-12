"""Bounded Git snapshots for attributing net workspace changes to one run."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from naumi_agent.runs.models import ReceiptChange, ReceiptGitState
from naumi_agent.runtime.async_process import read_bounded_stdout

_GIT_TIMEOUT_SECONDS = 3.0
_MAX_STATUS_PATHS = 500
_MAX_FINGERPRINT_BYTES = 8 * 1024 * 1024
_MAX_GIT_TEXT_BYTES = 64 * 1024
_MAX_GIT_STATUS_BYTES = 1024 * 1024
_MAX_GIT_NUMSTAT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class GitPathState:
    status: str
    fingerprint: str
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True, slots=True)
class GitWorkspaceSnapshot:
    available: bool
    branch: str = ""
    commit: str = ""
    dirty: bool = False
    ahead: int = 0
    behind: int = 0
    paths: dict[str, GitPathState] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GitRunDelta:
    changes: tuple[ReceiptChange, ...]
    git_state: ReceiptGitState
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes = b""
    truncated: bool = False


class GitWorkspaceProbe:
    """Capture the observable Git state without invoking a shell."""

    def __init__(self, workspace_root: str | Path) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()

    async def capture(self) -> GitWorkspaceSnapshot:
        root_result = await self._git("rev-parse", "--show-toplevel")
        if root_result is None or root_result.returncode != 0:
            detail = _safe_error(root_result)
            return GitWorkspaceSnapshot(
                available=False,
                warnings=(f"Git 仓库不可用{detail}",),
            )

        root_text = root_result.stdout.decode("utf-8", errors="replace").strip()
        if not root_text:
            return GitWorkspaceSnapshot(
                available=False,
                warnings=("Git 仓库不可用：未返回仓库根目录",),
            )
        repository_root = Path(root_text).expanduser().resolve()

        status_result = await self._git(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            max_bytes=_MAX_GIT_STATUS_BYTES,
        )
        if status_result is None or (
            status_result.returncode != 0 and not status_result.truncated
        ):
            return GitWorkspaceSnapshot(
                available=False,
                warnings=(f"Git 状态读取失败{_safe_error(status_result)}",),
            )

        warnings: list[str] = []
        if status_result.truncated:
            warnings.append(
                "Git 状态输出超过 1 MiB，仅保留已完整读取的路径；回执可能不完整"
            )
        raw_statuses = _parse_porcelain_v1_z(status_result.stdout)
        if len(raw_statuses) > _MAX_STATUS_PATHS:
            warnings.append(
                f"Git 状态路径超过 {_MAX_STATUS_PATHS} 条，仅保留前 {_MAX_STATUS_PATHS} 条"
            )
            raw_statuses = raw_statuses[:_MAX_STATUS_PATHS]

        stats, numstat_truncated = await self._numstat()
        if numstat_truncated:
            warnings.append(
                "Git numstat 输出超过 2 MiB，部分文件的增删行统计未计入回执"
            )
        paths: dict[str, GitPathState] = {}
        for path, status in raw_statuses:
            fingerprint, fingerprint_warning = await asyncio.to_thread(
                _fingerprint_path,
                repository_root,
                path,
            )
            if fingerprint_warning:
                warnings.append(fingerprint_warning)
            additions, deletions = stats.get(path, (0, 0))
            if status == "untracked" and fingerprint.startswith("sha256:"):
                additions = await asyncio.to_thread(
                    _count_file_lines,
                    repository_root,
                    path,
                )
            paths[path] = GitPathState(
                status=status,
                fingerprint=fingerprint,
                additions=additions,
                deletions=deletions,
            )

        branch = await self._git_text("symbolic-ref", "--short", "-q", "HEAD")
        commit = await self._git_text("rev-parse", "--verify", "HEAD")
        ahead, behind = await self._ahead_behind()
        return GitWorkspaceSnapshot(
            available=True,
            branch=branch,
            commit=commit,
            dirty=bool(paths),
            ahead=ahead,
            behind=behind,
            paths=paths,
            warnings=_unique(warnings),
        )

    async def _numstat(self) -> tuple[dict[str, tuple[int, int]], bool]:
        result = await self._git(
            "diff",
            "--numstat",
            "HEAD",
            "--",
            max_bytes=_MAX_GIT_NUMSTAT_BYTES,
        )
        if result is None or (result.returncode != 0 and not result.truncated):
            return {}, False
        stats: dict[str, tuple[int, int]] = {}
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            additions, deletions, path = parts
            if additions == "-" or deletions == "-":
                stats[path] = (0, 0)
                continue
            try:
                stats[path] = (max(int(additions), 0), max(int(deletions), 0))
            except ValueError:
                continue
        return stats, result.truncated

    async def _ahead_behind(self) -> tuple[int, int]:
        result = await self._git(
            "rev-list",
            "--left-right",
            "--count",
            "@{upstream}...HEAD",
        )
        if result is None or result.returncode != 0:
            return 0, 0
        parts = result.stdout.decode("ascii", errors="ignore").split()
        if len(parts) != 2:
            return 0, 0
        try:
            behind, ahead = (max(int(part), 0) for part in parts)
        except ValueError:
            return 0, 0
        return ahead, behind

    async def _git_text(self, *args: str) -> str:
        result = await self._git(*args)
        if result is None or result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", errors="replace").strip()[:500]

    async def _git(
        self,
        *args: str,
        max_bytes: int = _MAX_GIT_TEXT_BYTES,
    ) -> _GitResult | None:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(self._workspace_root),
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, NotADirectoryError, OSError):
            return None
        try:
            output, stderr = await asyncio.wait_for(
                asyncio.gather(
                    read_bounded_stdout(process, max_bytes=max_bytes),
                    _read_bounded_stderr(process, max_bytes=4096),
                ),
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.wait()
            return None
        return _GitResult(
            returncode=output.returncode,
            stdout=output.stdout,
            stderr=stderr,
            truncated=output.truncated,
        )


def diff_run_changes(
    before: GitWorkspaceSnapshot,
    after: GitWorkspaceSnapshot,
) -> GitRunDelta:
    """Return only net path-state changes after the captured baseline."""
    warnings = _unique([*before.warnings, *after.warnings])
    git_state = ReceiptGitState(
        available=after.available,
        branch=after.branch,
        dirty=after.dirty,
        commit=after.commit,
        ahead=after.ahead,
        behind=after.behind,
    )
    if not before.available or not after.available:
        if before.available != after.available:
            warnings = _unique([*warnings, "Git 运行前后状态不连续，无法归因文件改动"])
        return GitRunDelta(changes=(), git_state=git_state, warnings=warnings)

    changes: list[ReceiptChange] = []
    for path in sorted(set(before.paths) | set(after.paths)):
        previous = before.paths.get(path)
        current = after.paths.get(path)
        if previous == current:
            continue
        if current is None:
            status = (
                "removed_untracked"
                if previous is not None and previous.status == "untracked"
                else "restored"
            )
            changes.append(ReceiptChange(path=path, status=status))
            continue
        changes.append(
            ReceiptChange(
                path=path,
                status=current.status,
                additions=current.additions,
                deletions=current.deletions,
            )
        )
    return GitRunDelta(
        changes=tuple(changes[:_MAX_STATUS_PATHS]),
        git_state=git_state,
        warnings=warnings,
    )


async def _read_bounded_stderr(
    process: asyncio.subprocess.Process,
    *,
    max_bytes: int,
) -> bytes:
    if process.stderr is None:
        return b""
    captured = bytearray()
    while chunk := await process.stderr.read(64 * 1024):
        remaining = max_bytes - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return bytes(captured)


def _parse_porcelain_v1_z(payload: bytes) -> list[tuple[str, str]]:
    tokens = payload.split(b"\0")
    if payload and not payload.endswith(b"\0"):
        tokens = tokens[:-1]
    result: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        record = tokens[index]
        index += 1
        if not record or len(record) < 4:
            continue
        code = record[:2].decode("ascii", errors="replace")
        path = record[3:].decode("utf-8", errors="replace")
        result.append((path, _status_label(code)))
        if "R" in code or "C" in code:
            index += 1
    return result


def _status_label(code: str) -> str:
    if code == "??":
        return "untracked"
    if "U" in code:
        return "conflicted"
    if "R" in code:
        return "renamed"
    if "C" in code:
        return "copied"
    if "A" in code:
        return "added"
    if "D" in code:
        return "deleted"
    return "modified"


def _fingerprint_path(repository_root: Path, relative_path: str) -> tuple[str, str]:
    candidate = repository_root / relative_path
    try:
        if candidate.is_symlink():
            return f"symlink:{candidate.readlink()}", ""
        if not candidate.exists():
            return "missing", ""
        if not candidate.is_file():
            return "other", ""
        stat = candidate.stat()
        if stat.st_size > _MAX_FINGERPRINT_BYTES:
            return (
                f"large:{stat.st_size}:{stat.st_mtime_ns}",
                f"Git 文件过大，使用有限指纹: {relative_path}",
            )
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        return f"sha256:{digest}", ""
    except OSError as exc:
        return "unreadable", f"Git 文件无法读取: {relative_path} ({type(exc).__name__})"


def _count_file_lines(repository_root: Path, relative_path: str) -> int:
    candidate = repository_root / relative_path
    try:
        if not candidate.is_file() or candidate.stat().st_size > _MAX_FINGERPRINT_BYTES:
            return 0
        content = candidate.read_bytes()
    except OSError:
        return 0
    return len(content.splitlines())


def _safe_error(result: _GitResult | None) -> str:
    if result is None:
        return "：git 命令不可用或超时"
    if result.truncated:
        return "：git 输出超过读取上限"
    detail = result.stderr.decode("utf-8", errors="replace").strip()
    return f"：{detail[:200]}" if detail else ""


def _unique(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in items if item))


__all__ = [
    "GitPathState",
    "GitRunDelta",
    "GitWorkspaceProbe",
    "GitWorkspaceSnapshot",
    "diff_run_changes",
]
