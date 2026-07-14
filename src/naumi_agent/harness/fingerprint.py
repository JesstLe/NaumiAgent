"""Deterministic Git worktree fingerprints for Harness verification evidence."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class TreeFingerprintError(RuntimeError):
    """Raised when the current repository state cannot be fingerprinted safely."""


@dataclass(frozen=True)
class TreeFingerprint:
    digest: str
    head: str
    dirty_paths: tuple[str, ...]


class _Digest(Protocol):
    def update(self, data: bytes) -> None: ...


def compute_tree_fingerprint(workspace_root: str | Path) -> TreeFingerprint:
    """Hash HEAD, index state, worktree status, and dirty/untracked bytes."""
    root = Path(workspace_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise TreeFingerprintError("Harness 工作区不是目录。")
    head = _git(root, "rev-parse", "--verify", "HEAD").strip().decode("ascii")
    index = _git(root, "ls-files", "-s", "-z")
    status_bytes = _git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    dirty_paths = _parse_dirty_paths(status_bytes)

    digest = hashlib.sha256()
    _update_field(digest, b"head", head.encode("ascii"))
    _update_field(digest, b"index", index)
    _update_field(digest, b"status", status_bytes)
    for relative in dirty_paths:
        encoded = relative.encode("utf-8", errors="surrogateescape")
        _update_field(digest, b"path", encoded)
        _hash_worktree_path(digest, root, relative)
    return TreeFingerprint(
        digest=f"sha256:{digest.hexdigest()}",
        head=head,
        dirty_paths=dirty_paths,
    )


def _git(root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TreeFingerprintError(
            "无法读取 Git 状态。下一步：确认 git 可用且工作区可读取。"
        ) from exc
    if completed.returncode != 0:
        raise TreeFingerprintError(
            "当前工作区不是可验证的 Git 仓库，或 Git 状态不可读取。"
        )
    return completed.stdout


def _parse_dirty_paths(status_bytes: bytes) -> tuple[str, ...]:
    records = status_bytes.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise TreeFingerprintError("Git 状态输出格式无法识别，已停止验证。")
        path_bytes = record[3:]
        paths.add(path_bytes.decode("utf-8", errors="surrogateescape"))
        if b"R" in record[:2] or b"C" in record[:2]:
            if index >= len(records) or not records[index]:
                raise TreeFingerprintError("Git rename 状态不完整，已停止验证。")
            paths.add(records[index].decode("utf-8", errors="surrogateescape"))
            index += 1
    return tuple(sorted(paths))


def _hash_worktree_path(digest: _Digest, root: Path, relative: str) -> None:
    path = root / relative
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _update_field(digest, b"kind", b"missing")
        return
    mode = metadata.st_mode
    _update_field(digest, b"mode", f"{stat.S_IMODE(mode):o}".encode("ascii"))
    if stat.S_ISLNK(mode):
        _update_field(digest, b"kind", b"symlink")
        _update_field(
            digest,
            b"target",
            os.readlink(path).encode("utf-8", errors="surrogateescape"),
        )
        return
    if stat.S_ISREG(mode):
        _update_field(digest, b"kind", b"file")
        content_digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                content_digest.update(chunk)
        _update_field(digest, b"size", str(metadata.st_size).encode("ascii"))
        _update_field(digest, b"content_sha256", content_digest.digest())
        return
    if stat.S_ISDIR(mode):
        _update_field(digest, b"kind", b"directory")
        return
    _update_field(digest, b"kind", f"mode:{mode:o}".encode("ascii"))


def _update_field(digest: _Digest, name: bytes, value: bytes) -> None:
    digest.update(name)
    digest.update(b"\0")
    digest.update(str(len(value)).encode("ascii"))
    digest.update(b"\0")
    digest.update(value)
    digest.update(b"\0")
