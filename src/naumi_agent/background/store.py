"""Persistent JSON store for background task metadata."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from naumi_agent.background.models import BackgroundStatus, BackgroundTask


class BackgroundTaskStore:
    """Small durable store for background task records and output artifacts."""

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir).resolve()
        self._records_path = self._base_dir / "tasks.json"
        self._artifacts_dir = self._base_dir / "artifacts"

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def artifacts_dir(self) -> Path:
        return self._artifacts_dir

    def next_id(self) -> str:
        records = self.list_tasks()
        numbers: list[int] = []
        for task in records:
            prefix, _, suffix = task.id.partition("_")
            if prefix == "bg" and suffix.isdigit():
                numbers.append(int(suffix))
        return f"bg_{(max(numbers) if numbers else 0) + 1:04d}"

    def save(self, task: BackgroundTask) -> None:
        records = {item.id: item for item in self.list_tasks()}
        records[task.id] = task
        self._write_records(records)

    def get(self, task_id: str) -> BackgroundTask | None:
        return {task.id: task for task in self.list_tasks()}.get(task_id)

    def list_tasks(self) -> list[BackgroundTask]:
        if not self._records_path.exists():
            return []
        try:
            raw = json.loads(self._records_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        tasks: list[BackgroundTask] = []
        for item in raw.values() if isinstance(raw, dict) else []:
            if not isinstance(item, dict):
                continue
            try:
                item["status"] = BackgroundStatus(item["status"])
                tasks.append(BackgroundTask(**item))
            except (TypeError, ValueError):
                continue
        return sorted(tasks, key=lambda task: task.started_at or task.id, reverse=True)

    def mark_notified(self, task_id: str) -> None:
        task = self.get(task_id)
        if task is None:
            return
        task.notified = True
        self.save(task)

    def read_output(self, task_id: str, max_chars: int = 20000) -> str:
        task = self.get(task_id)
        if task is None:
            return f"错误：后台任务不存在：{task_id}"
        path = Path(task.output_path)
        if not path.exists():
            return f"错误：后台任务输出文件不存在：{path}"
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            return text[:max_chars] + f"\n...（输出已截断，完整内容见 {path}）"
        return text or "（无输出）"

    def _write_records(self, records: dict[str, BackgroundTask]) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        payload = {task_id: _task_to_dict(task) for task_id, task in records.items()}
        self._records_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _task_to_dict(task: BackgroundTask) -> dict[str, Any]:
    payload = asdict(task)
    payload["status"] = task.status.value
    return payload
