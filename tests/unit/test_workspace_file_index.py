from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from naumi_agent.ui.workspace_file_index import (
    WorkspaceFileIndex,
    _WorkspaceFileBuild,
    workspace_file_search_payload,
    workspace_file_template,
)


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(workspace), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_workspace_file_index_uses_git_ignore_and_safe_relative_payload(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("ignored/\n*.secret\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "文档 note.md").write_text("# note\n", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "hidden.py").write_text("", encoding="utf-8")
    (tmp_path / "token.secret").write_text("private", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore")

    index = WorkspaceFileIndex(tmp_path)
    result = await index.search("main")
    payload = workspace_file_search_payload(result)

    assert result.source == "git"
    assert [item.path for item in result.items] == ["src/main.py"]
    assert payload["items"] == [
        {
            "path": "src/main.py",
            "name": "main.py",
            "directory": "src",
            "extension": ".py",
            "template": "/read src/main.py",
        }
    ]
    all_files = await index.search("")
    assert "ignored/hidden.py" not in {item.path for item in all_files.items}
    assert "token.secret" not in {item.path for item in all_files.items}
    assert all(not item.path.startswith(str(tmp_path)) for item in all_files.items)
    note = next(item for item in all_files.items if item.path == "文档 note.md")
    assert workspace_file_template(note) == "/read '文档 note.md'"


@pytest.mark.asyncio
async def test_workspace_file_index_fallback_is_bounded_and_excludes_symlinks(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    for index in range(5):
        (tmp_path / "src" / f"file-{index}.py").write_text("", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "package.js").write_text("", encoding="utf-8")
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("private", encoding="utf-8")
    (tmp_path / "outside-link").symlink_to(outside)

    index = WorkspaceFileIndex(tmp_path, max_files=3, max_path_bytes=1_024)
    result = await index.search("")

    assert result.source == "filesystem"
    assert result.truncated
    assert result.total_indexed == 3
    assert all("node_modules" not in item.path for item in result.items)
    assert all(item.path != "outside-link" for item in result.items)


@pytest.mark.asyncio
async def test_workspace_file_index_cancels_build_and_can_recover(
    tmp_path: Path,
) -> None:
    class SlowIndex(WorkspaceFileIndex):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def _build_index(self) -> _WorkspaceFileBuild:
            self.started.set()
            await self.release.wait()
            return _WorkspaceFileBuild(
                paths=("README.md",),
                index_sha256="a" * 64,
                truncated=False,
                source="filesystem",
                built_at="2026-07-23T00:00:00+00:00",
            )

    index = SlowIndex(tmp_path)
    request = asyncio.create_task(index.search(""))
    await index.started.wait()
    cancelled = await index.cancel()

    assert cancelled.status == "cancelled"
    assert cancelled.revision == 0
    with pytest.raises(asyncio.CancelledError):
        await request

    index.release.set()
    recovered = await index.search("")
    assert recovered.status == "ready"
    assert recovered.revision == 1
    assert recovered.items[0].path == "README.md"


@pytest.mark.asyncio
async def test_workspace_file_indexes_never_cross_workspace_boundaries(
    tmp_path: Path,
) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "left.py").write_text("", encoding="utf-8")
    (right / "right.py").write_text("", encoding="utf-8")

    left_result, right_result = await asyncio.gather(
        WorkspaceFileIndex(left).search(""),
        WorkspaceFileIndex(right).search(""),
    )

    assert {item.path for item in left_result.items} == {"left.py"}
    assert {item.path for item in right_result.items} == {"right.py"}
