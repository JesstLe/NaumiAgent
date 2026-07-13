from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta

import pytest

from naumi_agent.runtime.shell_output import ShellOutputStore


def _write_artifact(store: ShellOutputStore, content: bytes):
    artifact = store.allocate()
    artifact.stream.write(content)
    return artifact, store.summarize(artifact)


def test_allocate_creates_unique_private_active_artifacts(tmp_path) -> None:
    store = ShellOutputStore(tmp_path)

    first = store.allocate()
    second = store.allocate()

    try:
        assert first.path != second.path
        assert first.path.parent == tmp_path.resolve()
        if os.name != "nt":
            assert stat.S_IMODE(first.path.stat().st_mode) == 0o600
            assert stat.S_IMODE(second.path.stat().st_mode) == 0o600
    finally:
        store.discard(first)
        store.discard(second)


def test_small_output_is_returned_in_full_and_temporary_file_is_removed(tmp_path) -> None:
    store = ShellOutputStore(tmp_path, inline_limit_bytes=20)

    artifact, summary = _write_artifact(store, b"hello\n")

    assert summary.content == "hello\n"
    assert summary.size_bytes == 6
    assert not summary.is_large
    assert summary.path is None
    assert not artifact.path.exists()


def test_large_output_keeps_recoverable_file_and_returns_head_and_tail(tmp_path) -> None:
    store = ShellOutputStore(
        tmp_path,
        inline_limit_bytes=10,
        head_bytes=4,
        tail_bytes=3,
    )

    artifact, summary = _write_artifact(store, b"HEAD-middle-TAIL")

    assert summary.content == ""
    assert summary.head == "HEAD"
    assert summary.tail == "AIL"
    assert summary.omitted_bytes == 9
    assert summary.size_bytes == 16
    assert summary.is_large
    assert summary.path == artifact.path
    assert artifact.path.read_bytes() == b"HEAD-middle-TAIL"


def test_invalid_utf8_is_replaced_without_changing_saved_bytes(tmp_path) -> None:
    store = ShellOutputStore(
        tmp_path,
        inline_limit_bytes=2,
        head_bytes=2,
        tail_bytes=1,
    )

    artifact, summary = _write_artifact(store, b"A\xffZ")

    assert summary.head == "A�"
    assert summary.tail == "Z"
    assert artifact.path.read_bytes() == b"A\xffZ"


def test_prune_removes_expired_completed_artifact(tmp_path) -> None:
    store = ShellOutputStore(tmp_path, inline_limit_bytes=1, retention_days=1)
    artifact, summary = _write_artifact(store, b"large")
    assert summary.path == artifact.path
    old = (datetime.now() - timedelta(days=2)).timestamp()
    os.utime(artifact.path, (old, old))

    result = store.prune()

    assert result.deleted == 1
    assert result.refused == 0
    assert not artifact.path.exists()


def test_prune_keeps_only_newest_completed_artifacts(tmp_path) -> None:
    store = ShellOutputStore(tmp_path, inline_limit_bytes=1, max_artifacts=2)
    artifacts = []
    for index in range(3):
        artifact, _ = _write_artifact(store, f"log-{index}".encode())
        timestamp = datetime.now().timestamp() + index
        os.utime(artifact.path, (timestamp, timestamp))
        artifacts.append(artifact)

    result = store.prune()

    assert result.deleted == 1
    assert not artifacts[0].path.exists()
    assert artifacts[1].path.exists()
    assert artifacts[2].path.exists()


def test_prune_refuses_managed_name_symlink(tmp_path) -> None:
    external = tmp_path.parent / "external-shell-output.log"
    external.write_text("keep", encoding="utf-8")
    link = tmp_path / "shell-20000101T000000-deadbeef.log"
    tmp_path.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("当前平台不允许创建测试符号链接")
    old = (datetime.now() - timedelta(days=30)).timestamp()
    os.utime(link, (old, old), follow_symlinks=False)
    store = ShellOutputStore(tmp_path, retention_days=1, max_artifacts=0)

    result = store.prune()

    assert result.deleted == 0
    assert result.refused == 1
    assert link.is_symlink()
    assert external.read_text(encoding="utf-8") == "keep"


def test_prune_never_removes_active_artifact(tmp_path) -> None:
    store = ShellOutputStore(tmp_path, retention_days=0, max_artifacts=0)
    artifact = store.allocate()
    artifact.stream.write(b"still running")

    result = store.prune()

    try:
        assert result.deleted == 0
        assert artifact.path.exists()
    finally:
        store.discard(artifact)


def test_preserve_releases_failed_summary_without_deleting_evidence(tmp_path) -> None:
    store = ShellOutputStore(tmp_path, retention_days=0, max_artifacts=0)
    artifact = store.allocate()
    artifact.stream.write(b"diagnostic evidence")

    store.preserve(artifact)

    assert artifact.path.read_bytes() == b"diagnostic evidence"
    result = store.prune()
    assert result.deleted == 1
