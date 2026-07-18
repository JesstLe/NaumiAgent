"""Canonical, immutable filesystem paths owned by one runtime instance."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Absolute paths resolved once by the composition root."""

    workspace_root: Path
    session_db_path: Path
    runtime_data_dir: Path
    chat_run_db_path: Path
    worktree_storage_dir: Path
    goal_storage_dir: Path
    pursuit_storage_dir: Path
    harness_db_path: Path
    harness_trust_db_path: Path
    evolution_db_path: Path
    browser_data_dir: Path
    browser_daemon_log_dir: Path

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if not isinstance(value, Path) or not value.is_absolute():
                raise TypeError(f"{item.name} 必须是绝对 Path。")
            if value != value.resolve(strict=False):
                raise ValueError(f"{item.name} 必须是已规范化的绝对路径。")
        for name in (
            "session_db_path",
            "chat_run_db_path",
            "worktree_storage_dir",
            "goal_storage_dir",
            "pursuit_storage_dir",
            "browser_data_dir",
            "browser_daemon_log_dir",
        ):
            try:
                getattr(self, name).relative_to(self.runtime_data_dir)
            except ValueError as exc:
                raise ValueError(f"{name} 必须位于 runtime_data_dir 内。") from exc


__all__ = ["RuntimePaths"]
