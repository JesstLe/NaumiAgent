"""Real ten-process acceptance for Terminal UI session isolation."""

from __future__ import annotations

import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROCESS_COUNT = 10
START_TIMEOUT_SECONDS = 300
EVENT_TIMEOUT_SECONDS = 20


@dataclass
class BridgeProcess:
    slot: int
    process: subprocess.Popen[str]
    records: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    stderr: list[str] = field(default_factory=list)

    def send(self, event_type: str, payload: dict[str, Any], request_id: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps(
                {"type": event_type, "version": 1, "id": request_id, "payload": payload},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        self.process.stdin.flush()

    def wait_for(self, event_type: str, *, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        observed: list[str] = []
        while time.monotonic() < deadline:
            if self.process.poll() is not None and self.records.empty():
                raise RuntimeError(
                    f"Bridge {self.slot} 提前退出 ({self.process.returncode})："
                    + "".join(self.stderr[-20:])
                )
            try:
                record = self.records.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            observed.append(str(record.get("type") or ""))
            if record.get("type") == event_type:
                return record
        raise TimeoutError(f"Bridge {self.slot} 未收到 {event_type}，已看到：{observed[-20:]}")


def main() -> int:
    bridges: list[BridgeProcess] = []
    root = Path(tempfile.mkdtemp(prefix="naumi-parallel-acceptance-"))
    try:
        workspace = root / "workspace"
        workspace.mkdir()
        database = root / "sessions.db"
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "models": {
                        "provider": "openai",
                        "default_model": "openai/acceptance-placeholder",
                        "fast_model": "openai/acceptance-placeholder",
                        "reasoning_model": "openai/acceptance-placeholder",
                    },
                    "memory": {
                        "session_db_path": str(database),
                        "vector_db_path": str(root / "vectors"),
                        "long_term_enabled": False,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        for slot in range(1, PROCESS_COUNT + 1):
            bridges.append(start_bridge(slot, workspace=workspace, config=config))
        try:
            wait_for_all_ready(bridges, timeout=START_TIMEOUT_SECONDS)
            for bridge in bridges:
                bridge.send(
                    "hello",
                    {
                        "client": f"parallel-acceptance-{bridge.slot}",
                        "minimum_version": 1,
                        "maximum_version": 1,
                        "capabilities": ["typed_ui_messages"],
                    },
                    f"hello-{bridge.slot}",
                )
                bridge.wait_for("ack", timeout=EVENT_TIMEOUT_SECONDS)

            session_ids: list[str] = []
            for bridge in bridges:
                bridge.send("submit", {"text": "/new"}, f"new-{bridge.slot}")
            for bridge in bridges:
                replay = bridge.wait_for("session/replayed", timeout=EVENT_TIMEOUT_SECONDS)
                session_ids.append(str(replay["payload"]["session_id"]))

            if len(set(session_ids)) != PROCESS_COUNT:
                raise AssertionError(f"会话 ID 不唯一：{session_ids}")

            stopped = bridges[0]
            stopped.process.terminate()
            stopped.process.wait(timeout=20)
            for bridge in bridges[1:]:
                if bridge.process.poll() is not None:
                    raise AssertionError(f"停止 Bridge 1 后 Bridge {bridge.slot} 也退出了")
                bridge.send("ping", {}, f"ping-{bridge.slot}")
                bridge.wait_for("pong", timeout=EVENT_TIMEOUT_SECONDS)

            with sqlite3.connect(database, timeout=10) as connection:
                row_count = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if row_count != PROCESS_COUNT:
                raise AssertionError(f"SQLite 会话数应为 {PROCESS_COUNT}，实际为 {row_count}")
            if str(journal_mode).lower() != "wal":
                raise AssertionError(f"SQLite journal_mode 应为 wal，实际为 {journal_mode}")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "process_count": PROCESS_COUNT,
                        "unique_session_count": len(set(session_ids)),
                        "sqlite_session_count": row_count,
                        "journal_mode": journal_mode,
                        "survivors_after_one_exit": PROCESS_COUNT - 1,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        finally:
            stop_bridges(bridges)
    finally:
        remove_tree_with_retry(root)


def start_bridge(slot: int, *, workspace: Path, config: Path) -> BridgeProcess:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-m", "naumi_agent.ui.bridge", "--config", str(config)],
        cwd=str(workspace),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    bridge = BridgeProcess(slot=slot, process=process)
    threading.Thread(target=read_stdout, args=(bridge,), daemon=True).start()
    threading.Thread(target=read_stderr, args=(bridge,), daemon=True).start()
    return bridge


def wait_for_all_ready(bridges: list[BridgeProcess], *, timeout: float) -> None:
    pending = {bridge.slot: bridge for bridge in bridges}
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        for slot, bridge in list(pending.items()):
            if bridge.process.poll() is not None:
                raise RuntimeError(
                    f"Bridge {slot} 提前退出 ({bridge.process.returncode})："
                    + "".join(bridge.stderr[-20:])
                )
            try:
                record = bridge.records.get_nowait()
            except queue.Empty:
                continue
            if record.get("type") == "ready":
                pending.pop(slot)
        if pending:
            time.sleep(0.05)
    if pending:
        diagnostics = {
            slot: "".join(bridge.stderr[-10:])
            for slot, bridge in pending.items()
        }
        raise TimeoutError(f"Bridge 未全部就绪：{sorted(pending)}；诊断：{diagnostics}")


def read_stdout(bridge: BridgeProcess) -> None:
    assert bridge.process.stdout is not None
    for line in bridge.process.stdout:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            bridge.stderr.append(f"stdout 非 JSONL：{line}")
            continue
        if isinstance(record, dict):
            bridge.records.put(record)


def read_stderr(bridge: BridgeProcess) -> None:
    assert bridge.process.stderr is not None
    bridge.stderr.extend(bridge.process.stderr)


def stop_bridges(bridges: list[BridgeProcess]) -> None:
    for bridge in bridges:
        if bridge.process.poll() is None:
            try:
                bridge.send("shutdown", {}, f"shutdown-{bridge.slot}")
            except (BrokenPipeError, OSError):
                pass
    for bridge in bridges:
        if bridge.process.poll() is not None:
            continue
        try:
            bridge.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            bridge.process.terminate()
            try:
                bridge.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bridge.process.kill()


def remove_tree_with_retry(root: Path) -> None:
    for attempt in range(50):
        try:
            shutil.rmtree(root)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == 49:
                shutil.rmtree(root, ignore_errors=True)
                return
            time.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(main())
