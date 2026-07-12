"""Workspace-scoped environment data for the Workbench chat inspector."""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from naumi_agent.api.chat_runs import ChatRunStore
from naumi_agent.background.store import BackgroundTaskStore

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
        return stdout.decode("utf-8", errors="replace").strip()

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
        sources: list[SourceEnvironment] = []
        for run in await self._chat_run_store.list_runs(session_id, limit=50):
            for artifact in run.artifacts:
                if artifact.kind not in _SOURCE_KINDS:
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
                if relative_path is None:
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
        return sources

    def _relative_path(self, path: Path) -> str | None:
        try:
            return str(path.relative_to(self._workspace_root))
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
