"""Structured diff viewer tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from naumi_agent.ui.diff_viewer import (
    collect_git_diff_snapshot,
    parse_unified_diff,
    render_diff_snapshot,
    render_git_diff_viewer,
)
from naumi_agent.ui.theme import build_ui_style_config

SAMPLE_DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,4 +1,5 @@
 import os
+import sys
 keep
-print("old")
+print("new")
diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-gone
"""


def test_parse_unified_diff_collects_file_stats() -> None:
    files = parse_unified_diff(SAMPLE_DIFF)

    assert len(files) == 2
    assert files[0].display_path == "app.py"
    assert files[0].hunk_count == 1
    assert files[0].additions == 2
    assert files[0].deletions == 1
    assert files[1].status == "deleted"
    assert files[1].deletions == 1


def test_render_diff_snapshot_summarizes_and_colors() -> None:
    snapshot = collect_snapshot_from_text(SAMPLE_DIFF)

    text = render_diff_snapshot(snapshot)

    assert "结构化 Diff Viewer" in text
    assert "app.py" in text
    assert "old.txt" in text
    assert "+2" in text
    assert "-2" in text
    assert "\033[32m+import sys" in text
    assert "\033[31m-print(\"old\")" in text


def test_long_diff_is_folded_per_file() -> None:
    body = "\n".join(f"+line {i}" for i in range(80))
    diff = f"""diff --git a/big.txt b/big.txt
--- a/big.txt
+++ b/big.txt
@@ -0,0 +1,80 @@
{body}
"""
    snapshot = collect_snapshot_from_text(diff)
    text = render_diff_snapshot(snapshot)

    assert "big.txt" in text
    assert "已折叠" in text
    assert "+line 0" in text
    assert "+line 79" not in text


def test_render_diff_snapshot_uses_theme_and_output_style() -> None:
    snapshot = collect_snapshot_from_text(SAMPLE_DIFF)
    high_contrast = build_ui_style_config(theme="high_contrast", output_style="debug")
    silent = build_ui_style_config(theme="dark", output_style="silent_tools")

    high_contrast_text = render_diff_snapshot(snapshot, style_config=high_contrast)
    silent_text = render_diff_snapshot(snapshot, style_config=silent)

    assert "\033[92;1m+import sys" in high_contrast_text
    assert "raw diff:" in high_contrast_text
    assert "+import sys" not in silent_text
    assert "app.py" in silent_text


def test_collect_git_diff_snapshot_reads_real_repo(tmp_path: Path) -> None:
    run(tmp_path, "git", "init")
    run(tmp_path, "git", "config", "user.email", "test@example.com")
    run(tmp_path, "git", "config", "user.name", "Test User")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8")
    run(tmp_path, "git", "add", "tracked.txt")
    run(tmp_path, "git", "commit", "-m", "init")

    tracked.write_text("one\ntwo\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")

    snapshot = collect_git_diff_snapshot(tmp_path)
    rendered = render_git_diff_viewer(tmp_path)

    assert snapshot.files[0].display_path == "tracked.txt"
    assert snapshot.files[0].additions == 1
    assert snapshot.untracked_files == ("new.txt",)
    assert "tracked.txt" in rendered
    assert "未跟踪文件" in rendered
    assert "new.txt" in rendered


def test_collect_git_diff_snapshot_reports_non_repo(tmp_path: Path) -> None:
    snapshot = collect_git_diff_snapshot(tmp_path)

    assert snapshot.error
    assert "不是 git 仓库" in render_diff_snapshot(snapshot)


def collect_snapshot_from_text(text: str):
    from naumi_agent.ui.diff_viewer import DiffSnapshot

    return DiffSnapshot(
        cwd=Path("/repo"),
        scope="all",
        files=parse_unified_diff(text),
        raw_diff=text,
    )


def run(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True)
