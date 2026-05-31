"""Git worktree manager for isolated agent execution."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from naumi_agent.tasks.store import TaskStore
from naumi_agent.worktree.models import WorktreeRecord, WorktreeStatus

_VALID_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAX_OUTPUT = 6000


class WorktreeManager:
    """Create, inspect, bind, keep, and remove isolated git worktrees."""

    def __init__(
        self,
        *,
        repo_root: str | Path,
        storage_dir: str | Path,
        task_store: TaskStore | None = None,
    ) -> None:
        self._repo_root = Path(repo_root).resolve()
        self._storage_dir = Path(storage_dir).resolve()
        self._task_store = task_store
        self._state_path = self._storage_dir / "worktrees.json"
        self._events_path = self._storage_dir / "events.jsonl"

    @property
    def storage_dir(self) -> Path:
        return self._storage_dir

    @property
    def repo_root(self) -> Path:
        return self._repo_root

    def validate_name(self, name: str) -> str | None:
        """Return a Chinese validation error, or None when the name is safe."""
        if not name:
            return "worktree 名称不能为空"
        if name in {".", ".."}:
            return f"worktree 名称不能是 {name!r}"
        if "/" in name or "\\" in name:
            return "worktree 名称不能包含路径分隔符"
        if not _VALID_NAME.match(name):
            return "worktree 名称只能包含字母、数字、点、下划线和连字符，长度 1-64"
        return None

    async def create(self, name: str, task_id: str = "") -> str:
        """Create a new git worktree and persist its metadata."""
        err = self.validate_name(name)
        if err:
            return f"错误：{err}"

        if task_id:
            task_error = await self._validate_task(task_id)
            if task_error:
                return task_error

        repo_error = self._ensure_git_repo()
        if repo_error:
            return repo_error

        records = self._load_records()
        if name in records:
            record = await self.status(name)
            return f"错误：worktree 已存在\n\n{self.format_status(record)}"

        path = self._worktree_path(name)
        if path.exists():
            return f"错误：目标目录已存在：{path}"

        branch = f"naumi/worktree-{name}"
        base_ref = self._git(["rev-parse", "HEAD"], cwd=self._repo_root).output.strip()
        if not base_ref:
            return "错误：无法读取当前 Git HEAD，不能创建隔离 worktree"

        result = self._git(
            ["worktree", "add", "-b", branch, str(path), "HEAD"],
            cwd=self._repo_root,
        )
        if not result.ok:
            return f"错误：创建 worktree 失败\n{result.output}"

        now = _now()
        record = WorktreeRecord(
            name=name,
            path=str(path),
            branch=branch,
            base_ref=base_ref,
            task_id=task_id,
            created_at=now,
            updated_at=now,
        )
        records[name] = record
        self._save_records(records)
        self._log_event("create", record, {"task_id": task_id})
        return "已创建隔离 worktree。\n\n" + self.format_status(record)

    async def bind_task(self, name: str, task_id: str) -> str:
        """Bind an existing worktree to an existing task."""
        if not task_id:
            return "错误：任务 ID 不能为空"
        err = self.validate_name(name)
        if err:
            return f"错误：{err}"
        task_error = await self._validate_task(task_id)
        if task_error:
            return task_error

        records = self._load_records()
        record = records.get(name)
        if record is None:
            return f"错误：worktree 不存在：{name}"

        record.task_id = task_id
        record.updated_at = _now()
        records[name] = record
        self._save_records(records)
        self._log_event("bind_task", record, {"task_id": task_id})
        return "已绑定任务。\n\n" + self.format_status(await self.status(name))

    async def status(self, name: str = "") -> WorktreeRecord | list[WorktreeRecord]:
        """Return one worktree status, or all tracked worktree statuses."""
        records = self._load_records()
        if name:
            err = self.validate_name(name)
            if err:
                raise ValueError(err)
            record = records.get(name)
            if record is None:
                raise KeyError(name)
            refreshed = self._refresh_status(record)
            records[name] = refreshed
            self._save_records(records)
            return refreshed

        refreshed_records = [self._refresh_status(r) for r in records.values()]
        if refreshed_records:
            self._save_records({r.name: r for r in refreshed_records})
        return sorted(refreshed_records, key=lambda r: r.name)

    async def keep(self, name: str, reason: str = "") -> str:
        """Mark a worktree as intentionally kept for review."""
        record = await self._get_existing(name)
        record = self._refresh_status(record)
        record.kept_reason = reason.strip()
        record.status = WorktreeStatus.KEPT
        record.updated_at = _now()
        records = self._load_records()
        records[name] = record
        self._save_records(records)
        self._log_event("keep", record, {"reason": record.kept_reason})
        return "已保留 worktree 供审查。\n\n" + self.format_status(record)

    async def remove(self, name: str, discard_changes: bool = False) -> str:
        """Remove a tracked worktree, refusing to discard work by default."""
        record = await self._get_existing(name)
        refreshed = self._refresh_status(record)

        if refreshed.status == WorktreeStatus.MISSING:
            records = self._load_records()
            records.pop(name, None)
            self._save_records(records)
            self._log_event("forget_missing", refreshed)
            return f"worktree 目录已不存在，已清理状态记录：{name}"

        if not discard_changes and not refreshed.removable:
            return (
                "拒绝删除：worktree 中仍有未保存或未审查的工作。\n"
                f"- 未提交文件数：{refreshed.dirty_files}\n"
                f"- 基于创建点的新提交数：{refreshed.commits_ahead}\n\n"
                "请先处理这些变更，或使用 worktree_keep 保留审查。"
            )

        args = ["worktree", "remove", refreshed.path]
        if discard_changes:
            args.append("--force")
        result = self._git(args, cwd=self._repo_root)
        if not result.ok:
            return f"错误：删除 worktree 失败\n{result.output}"

        branch_result = self._git(["branch", "-D", refreshed.branch], cwd=self._repo_root)
        records = self._load_records()
        records.pop(name, None)
        self._save_records(records)
        self._log_event(
            "remove",
            refreshed,
            {"discard_changes": str(discard_changes), "branch_output": branch_result.output},
        )
        suffix = "" if branch_result.ok else f"\n\n分支清理提示：{branch_result.output}"
        return f"已删除 worktree：{name}{suffix}"

    def format_status(self, record: WorktreeRecord | list[WorktreeRecord]) -> str:
        """Format worktree status for users and LLM tool results."""
        if isinstance(record, list):
            if not record:
                return "当前没有由 NaumiAgent 管理的 worktree。"
            return "\n\n".join(self.format_status(item) for item in record)

        task = f"\n- 绑定任务：#{record.task_id}" if record.task_id else ""
        reason = f"\n- 保留原因：{record.kept_reason}" if record.kept_reason else ""
        removable = "是" if record.removable else "否"
        return (
            f"### Worktree: {record.name}\n"
            f"- 状态：{_status_label(record.status)}\n"
            f"- 路径：`{record.path}`\n"
            f"- 分支：`{record.branch}`\n"
            f"- 未提交文件数：{record.dirty_files}\n"
            f"- 新提交数：{record.commits_ahead}\n"
            f"- 可安全删除：{removable}"
            f"{task}{reason}"
        )

    async def _get_existing(self, name: str) -> WorktreeRecord:
        err = self.validate_name(name)
        if err:
            raise ValueError(err)
        records = self._load_records()
        record = records.get(name)
        if record is None:
            raise KeyError(name)
        return record

    async def _validate_task(self, task_id: str) -> str | None:
        if self._task_store is None:
            return "错误：任务系统未初始化，不能绑定任务"
        if not self._task_store.session_id:
            return "错误：当前没有活动会话，不能校验任务 ID"
        task = await self._task_store.get_task(task_id)
        if task is None:
            return f"错误：依赖任务 #{task_id} 不存在"
        return None

    def _ensure_git_repo(self) -> str | None:
        result = self._git(["rev-parse", "--show-toplevel"], cwd=self._repo_root)
        if not result.ok:
            return f"错误：当前目录不是 Git 仓库，不能创建 worktree\n{result.output}"
        actual_root = Path(result.output.strip()).resolve()
        if actual_root != self._repo_root:
            self._repo_root = actual_root
        return None

    def _worktree_path(self, name: str) -> Path:
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        path = (self._storage_dir / name).resolve()
        if not path.is_relative_to(self._storage_dir):
            raise ValueError("worktree 路径逃逸存储目录")
        return path

    def _refresh_status(self, record: WorktreeRecord) -> WorktreeRecord:
        path = Path(record.path)
        if not path.exists():
            record.status = WorktreeStatus.MISSING
            record.dirty_files = 0
            record.commits_ahead = 0
            record.updated_at = _now()
            return record

        dirty_result = self._git(["status", "--porcelain"], cwd=path)
        dirty_files = len([line for line in dirty_result.output.splitlines() if line.strip()])
        commits_result = self._git(
            ["rev-list", "--count", "HEAD", f"^{record.base_ref}"],
            cwd=path,
        )
        commits_ahead = _parse_int(commits_result.output)

        branch_result = self._git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
        branch = branch_result.output.strip() or record.branch

        record.branch = branch
        record.dirty_files = dirty_files
        record.commits_ahead = commits_ahead
        if record.status == WorktreeStatus.KEPT:
            pass
        elif dirty_files or commits_ahead:
            record.status = WorktreeStatus.DIRTY
        else:
            record.status = WorktreeStatus.CLEAN
        record.updated_at = _now()
        return record

    def _load_records(self) -> dict[str, WorktreeRecord]:
        if not self._state_path.exists():
            return {}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        records: dict[str, WorktreeRecord] = {}
        for name, item in raw.items():
            if not isinstance(item, dict):
                continue
            try:
                if "status" in item:
                    item["status"] = WorktreeStatus(item["status"])
                records[name] = WorktreeRecord(**item)
            except (TypeError, ValueError):
                continue
        return records

    def _save_records(self, records: dict[str, WorktreeRecord]) -> None:
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        payload = {name: _record_to_dict(record) for name, record in records.items()}
        self._state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _log_event(
        self,
        event_type: str,
        record: WorktreeRecord,
        extra: dict[str, str] | None = None,
    ) -> None:
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        event = {
            "type": event_type,
            "name": record.name,
            "path": record.path,
            "branch": record.branch,
            "task_id": record.task_id,
            "timestamp": _now(),
            "extra": extra or {},
        }
        with self._events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _git(self, args: list[str], *, cwd: Path) -> _GitResult:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _GitResult(False, "Git 命令超时")
        except OSError as e:
            return _GitResult(False, f"Git 执行失败：{e}")
        output = (result.stdout + result.stderr).strip()
        if len(output) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + "\n...（输出已截断）"
        return _GitResult(result.returncode == 0, output)


class _GitResult:
    def __init__(self, ok: bool, output: str) -> None:
        self.ok = ok
        self.output = output


def _record_to_dict(record: WorktreeRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["status"] = record.status.value
    return payload


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_int(text: str) -> int:
    try:
        return int(text.strip())
    except (TypeError, ValueError):
        return 0


def _status_label(status: WorktreeStatus) -> str:
    return {
        WorktreeStatus.CLEAN: "干净",
        WorktreeStatus.DIRTY: "有变更",
        WorktreeStatus.MISSING: "目录缺失",
        WorktreeStatus.KEPT: "已保留",
    }[status]
