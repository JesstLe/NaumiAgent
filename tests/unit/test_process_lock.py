from __future__ import annotations

from pathlib import Path

from naumi_agent.runtime.process_lock import InterprocessDirectoryLock


def test_process_lock_recovers_dead_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "startup.lock"
    lock_path.mkdir()
    (lock_path / "owner").write_text("2147483647\n0\n", encoding="utf-8")

    with InterprocessDirectoryLock(lock_path, timeout_seconds=0.2, poll_seconds=0.01):
        assert lock_path.is_dir()

    assert not lock_path.exists()
