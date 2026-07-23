"""Bounded, cancellable workspace file index for terminal QuickOpen surfaces."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import stat
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

WORKSPACE_FILE_INDEX_LIMIT = 100_000
WORKSPACE_FILE_INDEX_PATH_BYTES_LIMIT = 16 * 1024 * 1024
WORKSPACE_FILE_QUERY_LIMIT = 200
WORKSPACE_FILE_RESULT_LIMIT = 200
WORKSPACE_FILE_PATH_LIMIT = 4_096

_IGNORED_DIRECTORY_NAMES = frozenset({
    ".git",
    ".hg",
    ".mypy_cache",
    ".naumi",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
})
_SAFE_RELATIVE_PATH = re.compile(r"^[^\x00-\x1f\x7f]{1,4096}$")


@dataclass(frozen=True, slots=True)
class WorkspaceFileItem:
    """One public relative file identity."""

    path: str
    name: str
    directory: str
    extension: str


@dataclass(frozen=True, slots=True)
class WorkspaceFileSearchResult:
    """One bounded query result over an immutable index revision."""

    status: str
    revision: int
    index_sha256: str
    query: str
    items: tuple[WorkspaceFileItem, ...]
    total_indexed: int
    truncated: bool
    source: str
    built_at: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class _WorkspaceFileBuild:
    paths: tuple[str, ...]
    index_sha256: str
    truncated: bool
    source: str
    built_at: str


class WorkspaceFileIndex:
    """Build and query one workspace-isolated file index without blocking input."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_files: int = WORKSPACE_FILE_INDEX_LIMIT,
        max_path_bytes: int = WORKSPACE_FILE_INDEX_PATH_BYTES_LIMIT,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Workspace File Index 根目录无效。")
        if isinstance(max_files, bool) or not 1 <= max_files <= WORKSPACE_FILE_INDEX_LIMIT:
            raise ValueError("Workspace File Index max_files 必须在 1..100000。")
        if (
            isinstance(max_path_bytes, bool)
            or not 1_024 <= max_path_bytes <= WORKSPACE_FILE_INDEX_PATH_BYTES_LIMIT
        ):
            raise ValueError("Workspace File Index max_path_bytes 无效。")
        self.workspace_root = root
        self.max_files = max_files
        self.max_path_bytes = max_path_bytes
        self._revision = 0
        self._build: _WorkspaceFileBuild | None = None
        self._build_task: asyncio.Task[_WorkspaceFileBuild] | None = None
        self._lock = asyncio.Lock()

    @property
    def building(self) -> bool:
        return self._build_task is not None and not self._build_task.done()

    async def search(
        self,
        query: str = "",
        *,
        limit: int = WORKSPACE_FILE_RESULT_LIMIT,
        refresh: bool = False,
    ) -> WorkspaceFileSearchResult:
        """Ensure one index revision and return only bounded public matches."""
        term = _normalize_query(query)
        if isinstance(limit, bool) or not 1 <= limit <= WORKSPACE_FILE_RESULT_LIMIT:
            raise ValueError("Workspace File QuickOpen limit 必须在 1..200。")
        build = await self._ensure_build(refresh=refresh)
        items = await asyncio.to_thread(_search_paths, build.paths, term, limit)
        return WorkspaceFileSearchResult(
            status="ready",
            revision=self._revision,
            index_sha256=build.index_sha256,
            query=term,
            items=items,
            total_indexed=len(build.paths),
            truncated=build.truncated,
            source=build.source,
            built_at=build.built_at,
        )

    async def cancel(self) -> WorkspaceFileSearchResult:
        """Cancel an in-flight build and leave the last complete revision intact."""
        async with self._lock:
            task = self._build_task
            if task is not None and not task.done():
                task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        build = self._build
        return WorkspaceFileSearchResult(
            status="cancelled",
            revision=self._revision,
            index_sha256=build.index_sha256 if build is not None else "",
            query="",
            items=(),
            total_indexed=len(build.paths) if build is not None else 0,
            truncated=build.truncated if build is not None else False,
            source=build.source if build is not None else "",
            built_at=build.built_at if build is not None else "",
            message="Workspace 文件索引构建已取消。",
        )

    async def _ensure_build(self, *, refresh: bool) -> _WorkspaceFileBuild:
        async with self._lock:
            if self._build is not None and not refresh:
                return self._build
            if self._build_task is None or self._build_task.done():
                self._build_task = asyncio.create_task(
                    self._build_index(),
                    name=f"workspace-file-index-{self.workspace_root.name}",
                )
            task = self._build_task
        try:
            build = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        async with self._lock:
            if self._build_task is task:
                self._build_task = None
            if self._build is not build:
                self._build = build
                self._revision += 1
            return build

    async def _build_index(self) -> _WorkspaceFileBuild:
        try:
            paths, truncated = await self._git_paths()
            source = "git"
        except (FileNotFoundError, NotADirectoryError, RuntimeError):
            paths, truncated = await asyncio.to_thread(self._scan_paths)
            source = "filesystem"
        ordered = tuple(sorted(set(paths), key=lambda value: (value.casefold(), value)))
        canonical = json.dumps(
            {"paths": ordered, "source": source, "truncated": truncated},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return _WorkspaceFileBuild(
            paths=ordered,
            index_sha256=hashlib.sha256(canonical).hexdigest(),
            truncated=truncated,
            source=source,
            built_at=datetime.now(UTC).isoformat(),
        )

    async def _git_paths(self) -> tuple[list[str], bool]:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.workspace_root),
            "-c",
            "core.quotepath=false",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        paths: list[str] = []
        path_bytes = 0
        truncated = False
        terminated_for_limit = False
        try:
            while True:
                raw = await process.stdout.readuntil(b"\0")
                if not raw:
                    break
                value = raw[:-1].decode("utf-8", errors="strict")
                normalized = _safe_relative_file(self.workspace_root, value)
                if normalized is None:
                    continue
                encoded_size = len(normalized.encode("utf-8"))
                if len(paths) >= self.max_files or path_bytes + encoded_size > self.max_path_bytes:
                    truncated = True
                    terminated_for_limit = True
                    process.terminate()
                    break
                paths.append(normalized)
                path_bytes += encoded_size
        except asyncio.IncompleteReadError:
            pass
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        finally:
            if process.returncode is None:
                await process.wait()
        stderr = await process.stderr.read() if process.stderr is not None else b""
        if process.returncode != 0 and not terminated_for_limit:
            raise RuntimeError(
                stderr.decode("utf-8", errors="replace")[:500]
                or "git ls-files 不可用"
            )
        return paths, truncated

    def _scan_paths(self) -> tuple[list[str], bool]:
        paths: list[str] = []
        path_bytes = 0
        stack = [self.workspace_root]
        truncated = False
        while stack:
            directory = stack.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
            except (OSError, PermissionError):
                continue
            for entry in entries:
                if entry.name in _IGNORED_DIRECTORY_NAMES:
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                relative = Path(entry.path).relative_to(self.workspace_root).as_posix()
                normalized = _normalize_relative_path(relative)
                if normalized is None:
                    continue
                encoded_size = len(normalized.encode("utf-8"))
                if len(paths) >= self.max_files or path_bytes + encoded_size > self.max_path_bytes:
                    truncated = True
                    return paths, truncated
                paths.append(normalized)
                path_bytes += encoded_size
        return paths, truncated


def workspace_file_template(item: WorkspaceFileItem | str) -> str:
    """Return a read-only slash template; explicit submit still controls execution."""
    path = item.path if isinstance(item, WorkspaceFileItem) else str(item)
    normalized = _normalize_relative_path(path)
    if normalized is None:
        raise ValueError("Workspace 文件路径无法安全填入 QuickOpen。")
    return f"/read {shlex.quote(normalized)}"


def workspace_file_search_payload(result: WorkspaceFileSearchResult) -> dict[str, object]:
    """Serialize one bounded result without exposing the absolute workspace path."""
    return {
        "schema_version": 1,
        "status": result.status,
        "revision": result.revision,
        "index_sha256": result.index_sha256,
        "query": result.query,
        "items": [
            {
                "path": item.path,
                "name": item.name,
                "directory": item.directory,
                "extension": item.extension,
                "template": workspace_file_template(item),
            }
            for item in result.items
        ],
        "total_indexed": result.total_indexed,
        "truncated": result.truncated,
        "source": result.source,
        "built_at": result.built_at,
        "message": result.message,
    }


def _search_paths(
    paths: tuple[str, ...],
    term: str,
    limit: int,
) -> tuple[WorkspaceFileItem, ...]:
    ranked = [
        (score, path.count("/"), len(path), path.casefold(), path)
        for path in paths
        if (score := _file_search_score(path, term)) is not None
    ]
    ranked.sort(key=lambda value: value[:4])
    return tuple(_file_item(value[4]) for value in ranked[:limit])


def _safe_relative_file(root: Path, value: str) -> str | None:
    normalized = _normalize_relative_path(value)
    if normalized is None:
        return None
    candidate = root / Path(*PurePosixPath(normalized).parts)
    try:
        metadata = candidate.stat(follow_symlinks=False)
    except OSError:
        return None
    return normalized if stat.S_ISREG(metadata.st_mode) else None


def _normalize_relative_path(value: str) -> str | None:
    raw = unicodedata.normalize("NFC", str(value or "")).replace("\\", "/")
    if not _SAFE_RELATIVE_PATH.fullmatch(raw):
        return None
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return None
    normalized = path.as_posix()
    return normalized if len(normalized.encode("utf-8")) <= WORKSPACE_FILE_PATH_LIMIT else None


def _normalize_query(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()[
        :WORKSPACE_FILE_QUERY_LIMIT
    ]


def _file_search_score(path: str, term: str) -> int | None:
    if not term:
        return 0
    folded = unicodedata.normalize("NFKC", path).casefold()
    name = folded.rsplit("/", 1)[-1]
    if name == term:
        return 0
    if folded == term:
        return 1
    if name.startswith(term):
        return 10_000 + len(name) - len(term)
    if folded.startswith(term):
        return 20_000 + len(folded) - len(term)
    if term in name:
        return 30_000 + name.index(term)
    if term in folded:
        return 40_000 + folded.index(term)
    gap = _subsequence_gap(term, folded)
    return None if gap is None else 100_000 + gap


def _subsequence_gap(term: str, target: str) -> int | None:
    position = -1
    score = 0
    for character in term:
        next_position = target.find(character, position + 1)
        if next_position < 0:
            return None
        score += next_position if position < 0 else next_position - position - 1
        position = next_position
    return score


def _file_item(path: str) -> WorkspaceFileItem:
    pure = PurePosixPath(path)
    directory = pure.parent.as_posix()
    return WorkspaceFileItem(
        path=path,
        name=pure.name,
        directory="" if directory == "." else directory,
        extension=pure.suffix.lower(),
    )


__all__ = [
    "WORKSPACE_FILE_INDEX_LIMIT",
    "WORKSPACE_FILE_QUERY_LIMIT",
    "WORKSPACE_FILE_RESULT_LIMIT",
    "WorkspaceFileIndex",
    "WorkspaceFileItem",
    "WorkspaceFileSearchResult",
    "workspace_file_search_payload",
    "workspace_file_template",
]
