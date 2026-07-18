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

from naumi_agent.workbench.store import WorkbenchStore


@dataclass(frozen=True)
class DiffHunk:
    path: str
    patch: str


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str  # e.g. "modified", "added", "deleted", "untracked"


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
        worktree_status = "missing"
        if worktree_path is not None and worktree_path.exists():
            worktree_status = "present"
            changed_files, diff_hunks = await self._collect_git_diff(worktree_path)
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
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Returns (changed_files, diff_hunks) from the worktree's git state."""
        changed_files = await self._changed_files(worktree_path)
        diff_hunks = await self._diff_hunks(worktree_path)
        return changed_files, diff_hunks

    async def _changed_files(self, worktree_path: Path) -> list[dict[str, Any]]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(worktree_path),
                "status",
                "--porcelain=v1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return []
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return []
        files: list[dict[str, Any]] = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            if len(line) < 3:
                continue
            code = line[:2]
            path = line[3:].strip().strip('"')
            files.append({"path": path, "status": _git_status_label(code)})
        return files[:200]

    async def _diff_hunks(self, worktree_path: Path) -> list[dict[str, Any]]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(worktree_path),
                "diff",
                "HEAD",
                "--no-color",
                "--no-ext-diff",
                "--unified=3",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return []
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return []
        return _parse_diff_hunks(stdout.decode("utf-8", errors="replace"))

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
    hunks: list[dict[str, Any]] = []
    current_path = ""
    current_lines: list[str] = []
    max_hunks = 30
    max_patch_chars = 4000

    def flush() -> None:
        nonlocal current_path, current_lines
        if current_path and current_lines:
            patch = "\n".join(current_lines)[:max_patch_chars]
            hunks.append({"path": current_path, "patch": patch})
        current_path = ""
        current_lines = []

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            if len(hunks) >= max_hunks:
                break
            flush()
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
    return hunks


__all__ = [
    "ChangedFile",
    "DiffHunk",
    "ReviewEvidenceCollector",
]
