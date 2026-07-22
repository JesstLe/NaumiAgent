"""Durable, session-scoped paging for oversized tool output."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ARTIFACT_ID = re.compile(r"^out_[a-f0-9]{32}$")


class ToolOutputArchiveError(RuntimeError):
    """Raised when an output artifact cannot be safely read or verified."""


@dataclass(frozen=True, slots=True)
class ToolOutputReference:
    artifact_id: str
    page_count: int
    page_chars: int
    content_chars: int
    content_bytes: int
    sha256: str

    def event_fields(self) -> dict[str, Any]:
        return {
            "output_artifact_id": self.artifact_id,
            "output_page_count": self.page_count,
            "output_page_chars": self.page_chars,
            "output_sha256": self.sha256,
            "content_length": self.content_chars,
            "content_bytes": self.content_bytes,
        }


@dataclass(frozen=True, slots=True)
class ToolOutputPage:
    artifact_id: str
    page: int
    page_count: int
    content: str
    content_chars: int
    content_bytes: int
    sha256: str


class ToolOutputArchive:
    """Write immutable character pages and verify every read against a manifest."""

    def __init__(self, root: Path, *, page_chars: int = 8_192) -> None:
        resolved = root.expanduser().resolve()
        if not resolved.is_absolute():
            raise TypeError("Tool Output 归档目录必须是绝对路径。")
        if not 1_024 <= page_chars <= 65_536:
            raise ValueError("Tool Output page_chars 必须在 1024..65536。")
        self.root = resolved
        self.page_chars = page_chars

    def archive(
        self,
        content: str,
        *,
        session_id: str,
        tool_call_id: str,
    ) -> ToolOutputReference:
        if not session_id.strip():
            raise ToolOutputArchiveError("缺少会话作用域，无法归档工具输出。")
        text = str(content)
        encoded = text.encode("utf-8", errors="strict")
        artifact_id = f"out_{uuid.uuid4().hex}"
        target = self.root / artifact_id
        temporary = self.root / f".{artifact_id}.tmp"
        pages = [
            text[offset : offset + self.page_chars]
            for offset in range(0, len(text), self.page_chars)
        ] or [""]
        page_digests = [
            hashlib.sha256(page.encode("utf-8")).hexdigest() for page in pages
        ]
        manifest = {
            "schema_version": 1,
            "artifact_id": artifact_id,
            "session_id": session_id,
            "tool_call_id": tool_call_id[:128],
            "page_count": len(pages),
            "page_chars": self.page_chars,
            "content_chars": len(text),
            "content_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "page_sha256": page_digests,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(self.root, 0o700)
        try:
            temporary.mkdir(mode=0o700)
            for index, page in enumerate(pages, start=1):
                page_path = temporary / f"{index:06d}.txt"
                page_path.write_text(page, encoding="utf-8", newline="")
                _restrict_permissions(page_path, 0o600)
            manifest_path = temporary / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            _restrict_permissions(manifest_path, 0o600)
            temporary.replace(target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return ToolOutputReference(
            artifact_id=artifact_id,
            page_count=len(pages),
            page_chars=self.page_chars,
            content_chars=len(text),
            content_bytes=len(encoded),
            sha256=manifest["sha256"],
        )

    def read_page(
        self,
        artifact_id: str,
        page: int,
        *,
        session_id: str,
    ) -> ToolOutputPage:
        if not _ARTIFACT_ID.fullmatch(artifact_id):
            raise ToolOutputArchiveError("Tool Output artifact id 无效。")
        manifest = self._read_manifest(artifact_id)
        if manifest.get("session_id") != session_id or not session_id:
            raise ToolOutputArchiveError("该工具输出不属于当前会话。")
        page_count = _positive_int(manifest.get("page_count"), "page_count")
        if not 1 <= page <= page_count:
            raise ToolOutputArchiveError(f"页码必须在 1..{page_count}。")
        page_path = self.root / artifact_id / f"{page:06d}.txt"
        try:
            content = page_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ToolOutputArchiveError("工具输出分页不可读。") from exc
        digests = manifest.get("page_sha256")
        if not isinstance(digests, list) or len(digests) != page_count:
            raise ToolOutputArchiveError("工具输出 manifest 已损坏。")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest != digests[page - 1]:
            raise ToolOutputArchiveError("工具输出分页摘要不一致。")
        return ToolOutputPage(
            artifact_id=artifact_id,
            page=page,
            page_count=page_count,
            content=content,
            content_chars=_nonnegative_int(manifest.get("content_chars"), "content_chars"),
            content_bytes=_nonnegative_int(manifest.get("content_bytes"), "content_bytes"),
            sha256=_sha256(manifest.get("sha256")),
        )

    def delete_session(self, session_id: str) -> int:
        """Delete only immutable artifacts bound to one exact session id."""
        if not session_id:
            return 0
        try:
            entries = tuple(self.root.iterdir())
        except OSError:
            return 0
        deleted = 0
        for entry in entries:
            if entry.is_symlink() or not _ARTIFACT_ID.fullmatch(entry.name):
                continue
            try:
                manifest = self._read_manifest(entry.name)
            except ToolOutputArchiveError:
                continue
            if manifest.get("session_id") != session_id:
                continue
            shutil.rmtree(entry)
            deleted += 1
        return deleted

    def _read_manifest(self, artifact_id: str) -> dict[str, Any]:
        manifest_path = self.root / artifact_id / "manifest.json"
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ToolOutputArchiveError("未找到可读取的工具输出归档。") from exc
        if not isinstance(value, dict) or value.get("artifact_id") != artifact_id:
            raise ToolOutputArchiveError("工具输出 manifest 已损坏。")
        if value.get("schema_version") != 1:
            raise ToolOutputArchiveError("工具输出 manifest 版本不兼容。")
        return value


def render_tool_output_page(value: ToolOutputPage) -> str:
    """Render one bounded page without allowing its content to escape the fence."""
    fence_size = max(3, _longest_run(value.content, "~") + 1)
    fence = "~" * fence_size
    return (
        f"## Tool Output · {value.artifact_id}\n\n"
        f"第 {value.page}/{value.page_count} 页 · 总计 {value.content_chars} 字符 / "
        f"{value.content_bytes} bytes · sha256 {value.sha256[:12]}\n\n"
        f"{fence}text\n{value.content}\n{fence}\n\n"
        f"下一页：`/tool-output {value.artifact_id} {min(value.page + 1, value.page_count)}`"
    )


def _longest_run(value: str, character: str) -> int:
    longest = current = 0
    for item in value:
        current = current + 1 if item == character else 0
        longest = max(longest, current)
    return longest


def _restrict_permissions(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _positive_int(value: Any, name: str) -> int:
    result = _nonnegative_int(value, name)
    if result < 1:
        raise ToolOutputArchiveError(f"工具输出 {name} 无效。")
    return result


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ToolOutputArchiveError(f"工具输出 {name} 无效。")
    return value


def _sha256(value: Any) -> str:
    text = str(value)
    if not re.fullmatch(r"[a-f0-9]{64}", text):
        raise ToolOutputArchiveError("工具输出 sha256 无效。")
    return text


__all__ = [
    "ToolOutputArchive",
    "ToolOutputArchiveError",
    "ToolOutputPage",
    "ToolOutputReference",
    "render_tool_output_page",
]
