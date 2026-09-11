#!/usr/bin/env python3
"""Run an offline lesson against the real task store, without invoking a model."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.reconciliation import reconcile_todos
from naumi_agent.tasks.store import TaskStore

FIXTURE = Path(__file__).resolve().parents[1] / "docs/interview/labs/weekly-updates.json"
CASES = ("complete", "pending", "stale", "missing-artifact", "tampered-artifact")
MAX_FIXTURE_BYTES = 65_536


def parse_updates(raw: bytes) -> list[dict[str, str]]:
    """Validate the bounded, synthetic lesson dataset before rendering it."""
    if len(raw) > MAX_FIXTURE_BYTES:
        raise ValueError("教学数据超过 64 KiB，请使用随教材提供的小型样例。")
    try:
        rows = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("教学数据不是有效的 UTF-8 JSON。") from exc
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise ValueError("教学数据必须包含 1–64 条更新。")
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "date", "status", "summary"}:
            raise ValueError("更新字段应且仅应为 id、date、status、summary。")
        if any(not isinstance(value, str) for value in row.values()):
            raise ValueError("更新字段必须是文本。")
        if not re.fullmatch(r"UPD-[0-9]{3}", row["id"]) or row["id"] in ids:
            raise ValueError("更新编号应为唯一的 UPD-三位数字。")
        ids.add(row["id"])
        try:
            date.fromisoformat(row["date"])
        except ValueError as exc:
            raise ValueError("更新日期无效，请使用 YYYY-MM-DD。") from exc
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"]):
            raise ValueError("更新日期应使用 YYYY-MM-DD。")
        if row["status"] not in {"completed", "blocked"}:
            raise ValueError("教学更新状态只能为 completed 或 blocked。")
        summary = row["summary"]
        if not summary.strip() or len(summary) > 500 or any(ord(c) < 32 for c in summary):
            raise ValueError("更新摘要应为 1–500 字的单行非空文本。")
    return rows


def render_report(rows: list[dict[str, str]]) -> str:
    """Render exact source text, not a generated or semantically graded answer."""
    lines = ["# 周报草稿（虚构教学数据，未发送）", "", "本报告由确定性程序整理，不由模型生成。", ""]
    for row in rows:
        summary = row["summary"].replace("&", "&amp;").replace("<", "&lt;")
        summary = re.sub(r"([\\`*_[\]()>#!|])", r"\\\1", summary)
        status = "已完成" if row["status"] == "completed" else "受阻"
        lines.append(f"- {row['date']}｜{status}｜{summary}（来源：{row['id']}）")
    return "\n".join(lines) + "\n"


async def run_case(case: str, parent: Path) -> dict[str, object]:
    """Create a new private run directory; never open a user's existing database."""
    if case not in CASES:
        raise ValueError("未知实验场景。")
    with FIXTURE.open("rb") as stream:
        raw = stream.read(MAX_FIXTURE_BYTES + 1)
    expected = render_report(parse_updates(raw))
    run_dir = Path(tempfile.mkdtemp(prefix=f"{case}-", dir=parent))
    database = run_dir / "tasks.db"
    store = TaskStore(str(database)).scoped("lesson-weekly-report")
    collected = await store.create_task("收集教学更新")
    draft = await store.create_task("保存并核验周报草稿", blocked_by=[collected.id])
    await store.update_task(collected.id, status=TaskStatus.COMPLETED)
    await store.update_task(draft.id, status=TaskStatus.IN_PROGRESS)
    artifact = run_dir / "weekly-report.md"
    if case in {"complete", "pending", "tampered-artifact"}:
        body = expected if case != "tampered-artifact" else "# 已完成所有工作\n"
        with artifact.open("x", encoding="utf-8") as stream:
            stream.write(body)
    if case == "pending":
        await store.update_task(draft.id, status=TaskStatus.PENDING)
    elif case != "stale":
        await store.update_task(draft.id, status=TaskStatus.COMPLETED)

    first = await reconcile_todos(store, attempted=False)
    second = await reconcile_todos(store, attempted=True) if case == "stale" else None
    # Reopen storage to distinguish persisted records from in-memory objects.
    reopened = TaskStore(str(database)).scoped("lesson-weekly-report")
    tasks = await reopened.list_tasks()
    checks = {
        "all_tasks_completed": bool(tasks) and all(t.status == TaskStatus.COMPLETED for t in tasks),
        "artifact_exists": artifact.is_file(),
        "artifact_matches_expected": (
            artifact.is_file() and artifact.read_text(encoding="utf-8") == expected
        ),
        "reconciliation_without_warning": not first.warning and not (second and second.warning),
    }
    expected_status = (
        "blocked" if case == "stale" else "pending" if case == "pending" else "completed"
    )
    expected_first = "retry" if case == "stale" else "none"
    goal_met = all(checks.values())
    lesson_passed = (
        goal_met == (case == "complete")
        and tasks[-1].status == expected_status
        and first.action == expected_first
        and (second is None or second.action == "blocked")
    )
    receipt: dict[str, object] = {
        "case": case,
        "lesson_assertions_passed": lesson_passed,
        "business_goal_met": goal_met,
        "mode": "offline-deterministic-no-model",
        "synthetic_dataset": True,
        "fixture_sha256": hashlib.sha256(raw).hexdigest(),
        "source_ids": [row["id"] for row in parse_updates(raw)],
        "task_statuses": {t.id: t.status.value for t in tasks},
        "first_reconciliation": first.action.value,
        "second_reconciliation": second.action.value if second else None,
        "checks": checks,
        "warnings": [
            warning for warning in (first.warning, second.warning if second else "") if warning
        ],
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()
        if artifact.is_file()
        else None,
        "run_directory": str(run_dir),
    }
    with (run_dir / "receipt.json").open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return receipt


async def run_lesson(case: str) -> dict[str, object]:
    if case not in (*CASES, "all"):
        raise ValueError("未知实验场景。")
    root = Path(tempfile.mkdtemp(prefix="naumi-interview-"))
    results = [await run_case(item, root) for item in (CASES if case == "all" else (case,))]
    return {
        "说明": "退出码表示教学断言是否符合预期，不表示每个业务任务成功。",
        "model_requests": 0,
        "lesson_assertions_passed": all(result["lesson_assertions_passed"] for result in results),
        "artifact_root": str(root),
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="离线复现任务账本与产物核验，不调用模型、不修改现有数据。"
    )
    parser.add_argument("--case", choices=(*CASES, "all"), default="all", help="选择实验场景")
    args = parser.parse_args()
    try:
        report = asyncio.run(run_lesson(args.case))
    except Exception as exc:
        print(
            f"实验未完成（{type(exc).__name__}）。请核对开发依赖、教学数据与临时目录写权限。",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["lesson_assertions_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
