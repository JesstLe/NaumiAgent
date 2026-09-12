"""Workspace-scoped environment data for the Workbench chat inspector."""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from naumi_agent.background.store import BackgroundTaskStore
from naumi_agent.runs.store import ChatRunStore

_SENSITIVE_NAMES = ("token", "secret", "password", "passwd", "api_key", "apikey")
_SOURCE_KINDS = {"source", "file", "screenshot"}


@dataclass(frozen=True, slots=True)
class GitEnvironment:
    available: bool = False
    branch: str = ""
    changed_files: int = 0
    additions: int = 0
    deletions: int = 0
    ahead: int = 0
    behind: int = 0
    dirty: bool = False


@dataclass(frozen=True, slots=True)
class GitDiffFile:
    path: str
    status: str
    stage: str
    additions: int = 0
    deletions: int = 0
    patch: str = ""


@dataclass(frozen=True, slots=True)
class GitDiff:
    available: bool = False
    branch: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    error: str = ""
    files: list[GitDiffFile] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BackgroundProcessEnvironment:
    id: str
    command: str
    pid: int | None
    status: str
    started_at: str
    cwd: str


@dataclass(frozen=True, slots=True)
class SourceEnvironment:
    id: str
    kind: str
    title: str
    path: str
    run_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ChatEnvironmentSnapshot:
    session_id: str
    workspace_root: str
    workspace_name: str
    git: GitEnvironment
    processes: list[BackgroundProcessEnvironment] = field(default_factory=list)
    sources: list[SourceEnvironment] = field(default_factory=list)


class ChatEnvironmentCollector:
    """Collects real state without crossing the configured workspace boundary."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        background_store: BackgroundTaskStore,
        chat_run_store: ChatRunStore,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._background_store = background_store
        self._chat_run_store = chat_run_store

    async def collect(self, *, session_id: str) -> ChatEnvironmentSnapshot:
        return ChatEnvironmentSnapshot(
            session_id=session_id,
            workspace_root=str(self._workspace_root),
            workspace_name=self._workspace_root.name,
            git=await self._collect_git(),
            processes=self._collect_processes(),
            sources=await self._collect_sources(session_id),
        )

    async def collect_diff(self) -> GitDiff:
        """Return a structured diff/status view for the workspace."""
        inside = await self._git("rev-parse", "--is-inside-work-tree")
        if inside != "true":
            return GitDiff(available=False)

        branch = await self._git("branch", "--show-current") or "HEAD"
        upstream = await self._git("rev-parse", "--abbrev-ref", "@{upstream}") or ""
        ahead, behind = await self._ahead_behind()

        try:
            files = await self._collect_diff_files()
        except OSError as exc:
            return GitDiff(
                available=True,
                branch=branch,
                upstream=upstream,
                ahead=ahead,
                behind=behind,
                error=f"Git diff failed: {exc}",
            )
        return GitDiff(
            available=True,
            branch=branch,
            upstream=upstream,
            ahead=ahead,
            behind=behind,
            files=files,
        )

    async def _collect_git(self) -> GitEnvironment:
        inside = await self._git("rev-parse", "--is-inside-work-tree")
        if inside != "true":
            return GitEnvironment()

        branch = await self._git("branch", "--show-current") or "HEAD"
        status = await self._git("status", "--porcelain=v1")
        changed_files = len(status.splitlines()) if status else 0
        additions, deletions = await self._diff_totals()
        ahead, behind = await self._ahead_behind()
        return GitEnvironment(
            available=True,
            branch=branch,
            changed_files=changed_files,
            additions=additions,
            deletions=deletions,
            ahead=ahead,
            behind=behind,
            dirty=changed_files > 0,
        )

    async def _diff_totals(self) -> tuple[int, int]:
        additions = 0
        deletions = 0
        for args in (("diff", "--numstat"), ("diff", "--cached", "--numstat")):
            output = await self._git(*args)
            for line in output.splitlines():
                parts = line.split("\t", 2)
                if len(parts) < 2:
                    continue
                if parts[0].isdigit():
                    additions += int(parts[0])
                if parts[1].isdigit():
                    deletions += int(parts[1])
        return additions, deletions

    async def _collect_diff_files(self) -> list[GitDiffFile]:
        """Parse porcelain status and fetch per-file patches."""
        output, staged_stats, unstaged_stats = await asyncio.gather(
            self._git("status", "--porcelain=v1", "--untracked-files=all", "-z"),
            self._git("diff", "--cached", "--numstat", "-z"),
            self._git("diff", "--numstat", "-z"),
        )
        descriptors: list[tuple[str, str, str, bool]] = []
        entries = output.split("\0")
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            index_status = entry[0]
            worktree_status = entry[1]
            path = entry[3:]
            # In porcelain -z output, rename/copy destinations come first and
            # the following NUL-delimited field contains the original path.
            if index_status in {"R", "C"} or worktree_status in {"R", "C"}:
                index += 1
            if index_status not in (" ", "?"):
                descriptors.append((path, index_status, "staged", False))
            if worktree_status not in (" ", "?"):
                descriptors.append((path, worktree_status, "unstaged", False))
            if index_status == "?" and worktree_status == "?":
                descriptors.append((path, "A", "unstaged", True))

        stats = {
            "staged": _parse_numstat(staged_stats),
            "unstaged": _parse_numstat(unstaged_stats),
        }
        semaphore = asyncio.Semaphore(8)

        async def collect(
            descriptor: tuple[str, str, str, bool],
        ) -> GitDiffFile:
            path, status, stage, untracked = descriptor
            async with semaphore:
                return await self._collect_file_diff(
                    path,
                    status,
                    stage,
                    untracked=untracked,
                    stats=stats[stage].get(path, (0, 0)),
                )

        files = list(await asyncio.gather(*(collect(item) for item in descriptors)))
        # Preserve the order Git reports.
        files.sort(key=lambda f: (f.stage, f.path))
        return files

    async def _collect_file_diff(
        self,
        path: str,
        status: str,
        stage: str,
        *,
        untracked: bool = False,
        stats: tuple[int, int] = (0, 0),
    ) -> GitDiffFile:
        if untracked:
            patch, additions, deletions = await self._untracked_patch_and_stats(path)
        elif stage == "staged":
            patch = await self._git("diff", "--cached", "--", path)
            additions, deletions = stats
        else:
            patch = await self._git("diff", "--", path)
            additions, deletions = stats
        return GitDiffFile(
            path=path,
            status=status,
            stage=stage,
            additions=additions,
            deletions=deletions,
            patch=patch,
        )

    async def _untracked_patch_and_stats(self, path: str) -> tuple[str, int, int]:
        """Read an untracked text file once for both patch content and stats."""
        target = self._workspace_root / path
        try:
            if target.is_file() and target.stat().st_size <= 1_000_000:
                text = target.read_text(encoding="utf-8", errors="replace")
                lines = text.splitlines()
                patch = "".join(f"+{line}\n" for line in lines) + "\n"
                return patch, len(lines), 0
        except (OSError, UnicodeError):
            pass
        return "", 0, 0

    async def _ahead_behind(self) -> tuple[int, int]:
        output = await self._git(
            "rev-list", "--left-right", "--count", "@{upstream}...HEAD"
        )
        parts = output.split()
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            return 0, 0
        return int(parts[1]), int(parts[0])

    async def _git(self, *args: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(self._workspace_root),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, NotADirectoryError):
            return ""
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            return ""
        # Only strip trailing newlines: leading spaces matter for status and diff output.
        return stdout.decode("utf-8", errors="replace").rstrip("\n")

    def _collect_processes(self) -> list[BackgroundProcessEnvironment]:
        processes: list[BackgroundProcessEnvironment] = []
        for task in self._background_store.list_tasks():
            cwd = Path(task.cwd).expanduser().resolve()
            relative_cwd = self._relative_path(cwd)
            if relative_cwd is None:
                continue
            processes.append(
                BackgroundProcessEnvironment(
                    id=task.id,
                    command=_safe_command_summary(task.command),
                    pid=task.pid,
                    status=task.status.value,
                    started_at=task.started_at,
                    cwd=relative_cwd or ".",
                )
            )
        return processes

    async def _collect_sources(self, session_id: str) -> list[SourceEnvironment]:
        sources = [
            SourceEnvironment(
                id=source.id,
                kind=source.kind,
                title=source.title,
                path=source.path,
                run_id="",
                created_at=source.created_at,
            )
            for source in await self._chat_run_store.list_sources(session_id)
        ]
        source_ids = {source.id for source in sources}
        source_paths = {source.path for source in sources}
        for run in await self._chat_run_store.list_runs(session_id, limit=50):
            for artifact in run.artifacts:
                if artifact.kind not in _SOURCE_KINDS or artifact.id in source_ids:
                    continue
                raw_path = artifact.summary.get("path") or artifact.metadata.get("path")
                if not isinstance(raw_path, str) or not raw_path:
                    continue
                path = Path(raw_path).expanduser()
                resolved = (
                    path.resolve()
                    if path.is_absolute()
                    else (self._workspace_root / path).resolve()
                )
                relative_path = self._relative_path(resolved)
                if relative_path is None or relative_path in source_paths:
                    continue
                sources.append(
                    SourceEnvironment(
                        id=artifact.id,
                        kind=artifact.kind,
                        title=artifact.title,
                        path=relative_path,
                        run_id=run.id,
                        created_at=artifact.created_at,
                    )
                )
                source_paths.add(relative_path)
        return sources

    def _relative_path(self, path: Path) -> str | None:
        try:
            return path.relative_to(self._workspace_root).as_posix()
        except ValueError:
            return None


def _safe_command_summary(command: str, *, max_chars: int = 200) -> str:
    """Returns a shell-safe display summary with common secret values removed."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "<unavailable>"
    sanitized: list[str] = []
    redact_next = False
    for token in tokens:
        if redact_next:
            sanitized.append("<redacted>")
            redact_next = False
            continue
        lowered = token.lower().lstrip("-").replace("-", "_")
        if any(lowered == name for name in _SENSITIVE_NAMES):
            sanitized.append(token)
            redact_next = True
            continue
        if "=" in token:
            name, _, _ = token.partition("=")
            if any(secret in name.lower() for secret in _SENSITIVE_NAMES):
                sanitized.append(f"{name}=<redacted>")
                continue
        sanitized.append(token)
    summary = " ".join(sanitized)
    return summary if len(summary) <= max_chars else summary[: max_chars - 1] + "…"


def _parse_numstat(output: str) -> dict[str, tuple[int, int]]:
    """Parse NUL-delimited git numstat output, including rename records."""
    stats: dict[str, tuple[int, int]] = {}
    entries = output.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        parts = entry.split("\t", 2)
        if len(parts) != 3:
            continue
        additions_raw, deletions_raw, path = parts
        additions = int(additions_raw) if additions_raw.isdigit() else 0
        deletions = int(deletions_raw) if deletions_raw.isdigit() else 0
        if not path:
            # Rename/copy records encode old and new paths in the next fields.
            if index + 1 >= len(entries):
                break
            index += 1
            path = entries[index]
            index += 1
        if path:
            stats[path] = (additions, deletions)
    return stats
