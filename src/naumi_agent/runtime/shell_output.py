"""Bounded, recoverable storage for foreground shell output."""

from __future__ import annotations

import os
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

_MANAGED_PREFIX = "shell-"
_MANAGED_SUFFIX = ".log"


@dataclass(frozen=True)
class ShellOutputSummary:
    """A bounded view of one complete shell output artifact."""

    size_bytes: int
    content: str = ""
    head: str = ""
    tail: str = ""
    omitted_bytes: int = 0
    path: Path | None = None

    @property
    def is_large(self) -> bool:
        return self.path is not None


@dataclass
class ShellOutputArtifact:
    """An active output file and its writable subprocess stream."""

    path: Path
    stream: BinaryIO


@dataclass(frozen=True)
class ShellOutputPruneResult:
    deleted: int = 0
    refused: int = 0
    errors: tuple[str, ...] = ()


class ShellOutputStore:
    """Own foreground shell logs without retaining unbounded model output."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        inline_limit_bytes: int = 50_000,
        head_bytes: int = 24_000,
        tail_bytes: int = 24_000,
        retention_days: int = 7,
        max_artifacts: int = 100,
    ) -> None:
        if inline_limit_bytes < 0:
            raise ValueError("inline_limit_bytes 不能小于 0")
        if head_bytes < 0 or tail_bytes < 0:
            raise ValueError("head_bytes 和 tail_bytes 不能小于 0")
        if retention_days < 0:
            raise ValueError("retention_days 不能小于 0")
        if max_artifacts < 0:
            raise ValueError("max_artifacts 不能小于 0")
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.inline_limit_bytes = inline_limit_bytes
        self.head_bytes = head_bytes
        self.tail_bytes = tail_bytes
        self.retention_days = retention_days
        self.max_artifacts = max_artifacts
        self._active: set[Path] = set()

    def allocate(self) -> ShellOutputArtifact:
        """Create an exclusive, private output file for a subprocess."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(10):
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            name = f"{_MANAGED_PREFIX}{stamp}-{uuid.uuid4().hex[:12]}{_MANAGED_SUFFIX}"
            path = self.output_dir / name
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                continue
            stream = os.fdopen(descriptor, "wb")
            resolved = path.resolve()
            self._active.add(resolved)
            return ShellOutputArtifact(path=resolved, stream=stream)
        raise OSError("无法分配唯一的 Shell 输出日志")

    def summarize(self, artifact: ShellOutputArtifact) -> ShellOutputSummary:
        """Close an artifact and return either full content or a head/tail view."""
        self._close_stream(artifact)
        path = artifact.path.resolve()
        try:
            size = path.stat().st_size
            if size <= self.inline_limit_bytes:
                content = path.read_bytes().decode("utf-8", errors="replace")
                path.unlink()
                return ShellOutputSummary(size_bytes=size, content=content)

            with path.open("rb") as handle:
                head_raw = handle.read(min(self.head_bytes, size))
                tail_start = max(len(head_raw), size - self.tail_bytes)
                handle.seek(tail_start)
                tail_raw = handle.read()
            return ShellOutputSummary(
                size_bytes=size,
                head=head_raw.decode("utf-8", errors="replace"),
                tail=tail_raw.decode("utf-8", errors="replace"),
                omitted_bytes=max(0, size - len(head_raw) - len(tail_raw)),
                path=path,
            )
        finally:
            self._active.discard(path)

    def discard(self, artifact: ShellOutputArtifact) -> None:
        """Close and remove an output artifact after a launch failure."""
        self._close_stream(artifact)
        path = artifact.path.resolve()
        self._active.discard(path)
        path.unlink(missing_ok=True)

    def preserve(self, artifact: ShellOutputArtifact) -> None:
        """Close a failed-to-read artifact but keep its evidence recoverable."""
        self._close_stream(artifact)
        self._active.discard(artifact.path.resolve())

    def prune(self, *, now: datetime | None = None) -> ShellOutputPruneResult:
        """Delete expired or excess completed artifacts inside the managed directory."""
        if not self.output_dir.exists():
            return ShellOutputPruneResult()

        refused = 0
        errors: list[str] = []
        candidates: list[tuple[Path, float]] = []
        for path in self.output_dir.glob(f"{_MANAGED_PREFIX}*{_MANAGED_SUFFIX}"):
            try:
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    refused += 1
                    continue
                resolved = path.resolve(strict=True)
                if resolved.parent != self.output_dir or resolved in self._active:
                    if resolved.parent != self.output_dir:
                        refused += 1
                    continue
                candidates.append((resolved, metadata.st_mtime))
            except OSError as exc:
                errors.append(f"无法检查 {path.name}：{type(exc).__name__}")

        candidates.sort(key=lambda item: (item[1], item[0].name), reverse=True)
        retained_by_count = {path for path, _ in candidates[: self.max_artifacts]}
        cutoff = (now or datetime.now()) - timedelta(days=self.retention_days)
        cutoff_timestamp = cutoff.timestamp()
        to_delete = [
            path
            for path, modified_at in candidates
            if path not in retained_by_count or modified_at < cutoff_timestamp
        ]

        deleted = 0
        for path in to_delete:
            try:
                path.unlink()
                deleted += 1
            except OSError as exc:
                errors.append(f"无法删除 {path.name}：{type(exc).__name__}")
        return ShellOutputPruneResult(
            deleted=deleted,
            refused=refused,
            errors=tuple(errors),
        )

    @staticmethod
    def _close_stream(artifact: ShellOutputArtifact) -> None:
        if artifact.stream.closed:
            return
        artifact.stream.flush()
        artifact.stream.close()
