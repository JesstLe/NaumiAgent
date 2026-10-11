"""Review evidence collection for the Workbench reviews page.

Gathers real, persisted evidence for a pending approval so the Mac app can
render approval cards from live data instead of fixtures: the approval itself,
the linked issue, the worktree, validation runs, changed files, diff hunks,
agent notes (derived from audit events), and the relevant event timeline.

Diff data is collected from the local git worktree via ``git``; when no
worktree path is available the evidence still loads with empty diff fields so
the UI can show the approval without fabricating a diff.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from naumi_agent.runtime.async_process import BoundedProcessOutput, read_bounded_stdout
from naumi_agent.workbench.store import WorkbenchStore

_STATUS_MAX_BYTES = 1024 * 1024
_DIFF_MAX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class DiffHunk:
    path: str
    patch: str


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str  # e.g. "modified", "added", "deleted", "untracked"


@dataclass(frozen=True)
class _ParsedDiff:
    hunks: list[dict[str, Any]]
    truncated: bool = False


class ReviewEvidenceCollector:
    """Collects approval review evidence from the store + local git worktree."""

    def __init__(
        self,
        *,
        store: WorkbenchStore,
        task_store: Any,
        worktree_storage_dir: str | Path | None = None,
    ) -> None:
        self._store = store
        self._task_store = task_store
        self._worktree_storage_dir = (
            Path(worktree_storage_dir).resolve() if worktree_storage_dir else None
        )

    async def collect(
        self,
        *,
        session_id: str,
        approval_id: str,
    ) -> dict[str, Any] | None:
        approval = await self._store.get_approval(session_id, approval_id)
        if approval is None:
            return None

        task_id = approval.task_id
        issue = await self._store.get_issue(session_id, task_id)
        worktree_name = (issue.related_worktree if issue else "") or ""
        worktree_path = self._worktree_path(worktree_name)

        validation_runs = await self._store.list_validation_runs(
            session_id, task_id=task_id, limit=20
        )
        events = await self._store.list_events(
            session_id, subject_id=task_id, limit=50
        )
        agent_notes = self._derive_agent_notes(events)

        changed_files: list[dict[str, Any]] = []
        diff_hunks: list[dict[str, Any]] = []
        git_warnings: list[str] = []
        worktree_status = "missing"
        if worktree_path is not None and worktree_path.exists():
            worktree_status = "present"
            changed_files, diff_hunks, git_warnings = await self._collect_git_diff(
                worktree_path
            )
        elif worktree_name:
            worktree_status = "missing"
        else:
            worktree_status = "unbound"

        return {
            "approval": _approval_to_dict(approval),
            "issue": _issue_to_dict(issue) if issue else None,
            "worktree": {
                "name": worktree_name,
                "path": str(worktree_path) if worktree_path else "",
                "status": worktree_status,
            },
            "validation_runs": validation_runs,
            "changed_files": changed_files,
            "diff_hunks": diff_hunks,
            "warnings": git_warnings,
            "agent_notes": agent_notes,
            "events": [_event_to_dict(event) for event in events],
        }

    def _worktree_path(self, worktree_name: str) -> Path | None:
        if not worktree_name or self._worktree_storage_dir is None:
            return None
        candidate = (self._worktree_storage_dir / worktree_name).resolve()
        try:
            candidate.relative_to(self._worktree_storage_dir)
        except ValueError:
            return None
        return candidate

    async def _collect_git_diff(
        self, worktree_path: Path
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        """Returns (changed_files, diff_hunks) from the worktree's git state."""
        (changed_files, status_warning), (diff_hunks, diff_warning) = await asyncio.gather(
            self._changed_files(worktree_path),
            self._diff_hunks(worktree_path),
        )
        warnings = [warning for warning in (status_warning, diff_warning) if warning]
        return changed_files, diff_hunks, warnings

    async def _changed_files(
        self, worktree_path: Path
    ) -> tuple[list[dict[str, Any]], str]:
        result = await self._git(
            worktree_path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "-z",
            max_bytes=_STATUS_MAX_BYTES,
        )
        if result.returncode != 0 and not result.truncated:
            return [], "Git 状态读取失败，变更文件列表不可用。"
        files: list[dict[str, Any]] = []
        entries = result.stdout.decode("utf-8", errors="ignore").split("\0")
        if result.truncated and entries and entries[-1]:
            entries.pop()
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            code = entry[:2]
            path = entry[3:]
            files.append({"path": path, "status": _git_status_label(code)})
            if "R" in code or "C" in code:
                index += 1
        warning = ""
        if result.truncated:
            warning = "Git 状态超过 1 MiB，仅显示已读取的文件。"
        elif len(files) > 200:
            warning = "变更文件超过 200 个，仅显示前 200 个。"
        return files[:200], warning

    async def _diff_hunks(
        self, worktree_path: Path
    ) -> tuple[list[dict[str, Any]], str]:
        result = await self._git(
            worktree_path,
            "diff",
            "HEAD",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=3",
            max_bytes=_DIFF_MAX_BYTES,
        )
        if result.returncode != 0 and not result.truncated:
            return [], "Git 差异读取失败，补丁证据不可用。"
        parsed = _parse_diff_hunks_with_limits(
            result.stdout.decode("utf-8", errors="ignore")
        )
        warnings: list[str] = []
        if result.truncated:
            warnings.append("Git 差异超过 4 MiB，仅显示已读取的补丁证据。")
        if parsed.truncated:
            warnings.append("审查预览超过 30 个文件或单文件 4000 字符，仅显示摘要。")
        return parsed.hunks, " ".join(warnings)

    async def _git(
        self,
        worktree_path: Path,
        *args: str,
        max_bytes: int,
    ) -> BoundedProcessOutput:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(worktree_path),
                "--no-pager",
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return BoundedProcessOutput(stdout=b"", returncode=127)
        return await read_bounded_stdout(proc, max_bytes=max_bytes)

    def _derive_agent_notes(self, events: list[Any]) -> list[dict[str, Any]]:
        """Derives agent notes from review/agent audit events.

        Until a dedicated agent-notes model exists (planned for M14), review
        evidence surfaces notes from audit events whose type mentions an agent
        or review action. Real data only — never fabricated.
        """
        notes: list[dict[str, Any]] = []
        for event in events:
            event_type = getattr(event, "type", "")
            lowered = event_type.lower()
            if "agent" not in lowered and "review" not in lowered and "note" not in lowered:
                continue
            notes.append(
                {
                    "actor": getattr(event, "actor", ""),
                    "note": str(getattr(event, "payload", {}).get("note", "")),
                    "type": event_type,
                    "timestamp": getattr(event, "timestamp", ""),
                }
            )
        return notes


def _approval_to_dict(approval: Any) -> dict[str, Any]:
    from dataclasses import asdict

    data = asdict(approval)
    data["state"] = data["state"].value if hasattr(data["state"], "value") else data["state"]
    return data


def _issue_to_dict(issue: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(issue)


def _event_to_dict(event: Any) -> dict[str, Any]:
    return {
        "id": getattr(event, "id", ""),
        "session_id": getattr(event, "session_id", ""),
        "type": getattr(event, "type", ""),
        "actor": getattr(event, "actor", ""),
        "subject_id": getattr(event, "subject_id", ""),
        "timestamp": getattr(event, "timestamp", ""),
        "payload": getattr(event, "payload", {}),
    }


def _git_status_label(code: str) -> str:
    """Maps a porcelain status code to a human-readable label."""
    code = code.strip()
    if not code:
        return "modified"
    if code.startswith("??"):
        return "untracked"
    if code.startswith("A"):
        return "added"
    if code.startswith("D"):
        return "deleted"
    if code.startswith("R"):
        return "renamed"
    if code.startswith("M"):
        return "modified"
    return "modified"


def _parse_diff_hunks(diff_text: str) -> list[dict[str, Any]]:
    """Splits a unified diff into per-file hunk dicts, capped for UI use."""
    return _parse_diff_hunks_with_limits(diff_text).hunks


def _parse_diff_hunks_with_limits(diff_text: str) -> _ParsedDiff:
    hunks: list[dict[str, Any]] = []
    current_path = ""
    current_lines: list[str] = []
    max_hunks = 30
    max_patch_chars = 4000
    truncated = False

    def flush() -> None:
        nonlocal current_path, current_lines, truncated
        if current_path and current_lines and len(hunks) < max_hunks:
            raw_patch = "\n".join(current_lines)
            if len(raw_patch) > max_patch_chars:
                truncated = True
            patch = raw_patch[:max_patch_chars]
            hunks.append({"path": current_path, "patch": patch})
        current_path = ""
        current_lines = []

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            flush()
            if len(hunks) >= max_hunks:
                truncated = True
                break
            # diff --git a/path b/path
            try:
                parts = shlex.split(line)
            except ValueError:
                parts = line.split(" ")
            if len(parts) >= 4:
                path = parts[-1].strip()
                current_path = path[2:] if path.startswith("b/") else path
        elif current_path:
            current_lines.append(line)
    flush()
    return _ParsedDiff(hunks=hunks, truncated=truncated)


__all__ = [
    "ChangedFile",
    "DiffHunk",
    "ReviewEvidenceCollector",
]
