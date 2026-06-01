"""Structured git diff viewer shared by CLI and TUI."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from naumi_agent.ui.theme import UIStyleConfig, build_ui_style_config

_MAX_FILE_PREVIEW_LINES = 32


@dataclass(frozen=True)
class DiffFileSummary:
    """Parsed summary for one file in a unified diff."""

    old_path: str
    new_path: str
    status: str
    additions: int
    deletions: int
    hunk_count: int
    preview_lines: tuple[str, ...]
    hidden_lines: int = 0

    @property
    def display_path(self) -> str:
        if self.new_path and self.new_path != "/dev/null":
            return self.new_path
        return self.old_path


@dataclass(frozen=True)
class DiffSnapshot:
    """Complete structured diff snapshot for a git worktree."""

    cwd: Path
    scope: str
    files: tuple[DiffFileSummary, ...]
    untracked_files: tuple[str, ...] = ()
    raw_diff: str = ""
    error: str = ""

    @property
    def additions(self) -> int:
        return sum(file.additions for file in self.files)

    @property
    def deletions(self) -> int:
        return sum(file.deletions for file in self.files)

    @property
    def hunk_count(self) -> int:
        return sum(file.hunk_count for file in self.files)


def collect_git_diff_snapshot(cwd: str | Path, *, scope: str = "all") -> DiffSnapshot:
    """Collect staged/unstaged git diff plus untracked-file status."""
    root = Path(cwd).expanduser().resolve()
    normalized = scope.strip().lower() or "all"
    if normalized in {"cached", "stage"}:
        normalized = "staged"
    if normalized not in {"all", "worktree", "staged"}:
        return DiffSnapshot(
            cwd=root,
            scope=normalized,
            files=(),
            error="用法: /diff [all|worktree|staged]",
        )

    if not _run_git(root, "rev-parse", "--is-inside-work-tree").ok:
        return DiffSnapshot(
            cwd=root,
            scope=normalized,
            files=(),
            error=f"当前目录不是 git 仓库: {root}",
        )

    diff_parts: list[str] = []
    if normalized in {"all", "staged"}:
        staged = _run_git(root, "diff", "--cached", "--no-ext-diff")
        if not staged.ok:
            return DiffSnapshot(cwd=root, scope=normalized, files=(), error=staged.text)
        if staged.text:
            diff_parts.append(staged.text)
    if normalized in {"all", "worktree"}:
        worktree = _run_git(root, "diff", "--no-ext-diff")
        if not worktree.ok:
            return DiffSnapshot(cwd=root, scope=normalized, files=(), error=worktree.text)
        if worktree.text:
            diff_parts.append(worktree.text)

    status = _run_git(root, "status", "--short", "--untracked-files=normal")
    untracked = _parse_untracked_files(status.text if status.ok else "")
    raw = "\n".join(part.rstrip("\n") for part in diff_parts if part).strip("\n")
    return DiffSnapshot(
        cwd=root,
        scope=normalized,
        files=parse_unified_diff(raw),
        untracked_files=tuple(untracked),
        raw_diff=raw,
    )


def parse_unified_diff(
    diff_text: str,
    *,
    max_preview_lines: int = _MAX_FILE_PREVIEW_LINES,
) -> tuple[DiffFileSummary, ...]:
    """Parse unified diff into file-level summaries."""
    if not diff_text.strip():
        return ()

    files: list[DiffFileSummary] = []
    current_header: str | None = None
    current_lines: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current_header is not None:
                files.append(_summarize_file(current_header, current_lines, max_preview_lines))
            current_header = line
            current_lines = [line]
        elif current_header is not None:
            current_lines.append(line)

    if current_header is not None:
        files.append(_summarize_file(current_header, current_lines, max_preview_lines))
    return tuple(files)


def render_diff_snapshot(
    snapshot: DiffSnapshot,
    *,
    max_files: int | None = None,
    style_config: UIStyleConfig | None = None,
) -> str:
    """Render a structured diff snapshot as ANSI text."""
    style = style_config or build_ui_style_config()
    file_limit = max_files if max_files is not None else style.output_style.diff_max_files
    if snapshot.error:
        return style.ansi("danger", f"Diff 查看失败: {snapshot.error}") + "\n"

    if not snapshot.files and not snapshot.untracked_files:
        return (
            style.ansi(
                "muted",
                "当前没有 git diff。工作区没有已暂存/未暂存改动，也没有未跟踪文件。",
            )
            + "\n"
        )

    title = {
        "all": "全部改动",
        "worktree": "未暂存改动",
        "staged": "已暂存改动",
    }.get(snapshot.scope, snapshot.scope)
    lines = [
        style.ansi("title", "结构化 Diff Viewer"),
        style.ansi("muted", f"范围: {title} · 仓库: {snapshot.cwd}"),
        (
            style.ansi("muted", f"文件 {len(snapshot.files)} · hunk {snapshot.hunk_count} · ")
            + style.ansi("diff_add", f"+{snapshot.additions}")
            + style.ansi("muted", " / ")
            + style.ansi("diff_delete", f"-{snapshot.deletions}")
        ),
        "",
    ]
    if style.output_style.show_debug_metadata:
        lines.append(style.ansi("muted", f"raw diff: {len(snapshot.raw_diff)} chars"))
        lines.append("")

    shown_files = snapshot.files[:file_limit]
    for file in shown_files:
        lines.extend(_render_file_summary(file, style_config=style))
        lines.append("")
    hidden_files = len(snapshot.files) - len(shown_files)
    if hidden_files > 0:
        lines.append(style.ansi("muted", f"... 还有 {hidden_files} 个文件未展示"))

    if snapshot.untracked_files:
        lines.append(style.ansi("warning", "未跟踪文件"))
        for path in snapshot.untracked_files[:20]:
            lines.append(f"  ? {path}")
        hidden = len(snapshot.untracked_files) - 20
        if hidden > 0:
            lines.append("  " + style.ansi("muted", f"... 还有 {hidden} 个未跟踪文件"))

    return "\n".join(lines).rstrip() + "\n"


def render_git_diff_viewer(
    cwd: str | Path,
    *,
    scope: str = "all",
    style_config: UIStyleConfig | None = None,
) -> str:
    """Collect and render git diff for a worktree."""
    return render_diff_snapshot(
        collect_git_diff_snapshot(cwd, scope=scope),
        style_config=style_config,
    )


@dataclass(frozen=True)
class _GitResult:
    ok: bool
    text: str


def _run_git(cwd: Path, *args: str) -> _GitResult:
    try:
        proc = subprocess.run(
            ("git", *args),
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return _GitResult(False, str(exc))
    text = proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)
    return _GitResult(proc.returncode == 0, text.strip("\n"))


def _summarize_file(
    header: str,
    lines: list[str],
    max_preview_lines: int,
) -> DiffFileSummary:
    old_path, new_path = _paths_from_header(header)
    status = "modified"
    additions = 0
    deletions = 0
    hunks = 0
    preview: list[str] = []
    in_hunk = False

    for line in lines[1:]:
        if line.startswith("new file mode"):
            status = "added"
        elif line.startswith("deleted file mode"):
            status = "deleted"
        elif line.startswith("rename from "):
            status = "renamed"
            old_path = line.removeprefix("rename from ").strip()
        elif line.startswith("rename to "):
            status = "renamed"
            new_path = line.removeprefix("rename to ").strip()
        elif line.startswith("--- "):
            old_path = _normalize_diff_path(line.removeprefix("--- ").strip())
        elif line.startswith("+++ "):
            new_path = _normalize_diff_path(line.removeprefix("+++ ").strip())

        if line.startswith("@@"):
            hunks += 1
            in_hunk = True

        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1

        if in_hunk and len(preview) < max_preview_lines:
            preview.append(line)

    hunk_lines = [line for line in lines if line.startswith("@@") or _is_hunk_body_line(line)]
    hidden = max(0, len(hunk_lines) - len(preview))
    return DiffFileSummary(
        old_path=old_path,
        new_path=new_path,
        status=status,
        additions=additions,
        deletions=deletions,
        hunk_count=hunks,
        preview_lines=tuple(preview),
        hidden_lines=hidden,
    )


def _render_file_summary(
    file: DiffFileSummary,
    *,
    style_config: UIStyleConfig,
) -> list[str]:
    status_label = {
        "added": "新增",
        "deleted": "删除",
        "renamed": "重命名",
        "modified": "修改",
    }.get(file.status, file.status)
    lines = [
        (
            style_config.ansi("title", file.display_path)
            + " "
            + style_config.ansi("muted", f"[{status_label}] hunk {file.hunk_count} · ")
            + style_config.ansi("diff_add", f"+{file.additions}")
            + style_config.ansi("muted", " / ")
            + style_config.ansi("diff_delete", f"-{file.deletions}")
        )
    ]
    if style_config.output_style.show_diff_preview:
        for line in file.preview_lines:
            lines.append("  " + _color_diff_line(line, style_config=style_config))
    if file.hidden_lines:
        lines.append(
            "  " + style_config.ansi("muted", f"... 还有 {file.hidden_lines} 行 diff 已折叠")
        )
    return lines


def _paths_from_header(header: str) -> tuple[str, str]:
    parts = header.split()
    if len(parts) >= 4:
        return _normalize_diff_path(parts[2]), _normalize_diff_path(parts[3])
    return "", ""


def _normalize_diff_path(path: str) -> str:
    if path == "/dev/null":
        return path
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path.strip('"')


def _is_hunk_body_line(line: str) -> bool:
    if not line:
        return False
    if line.startswith(("+++", "---", "diff --git", "index ")):
        return False
    return line[0] in {" ", "+", "-"}


def _color_diff_line(line: str, *, style_config: UIStyleConfig) -> str:
    if line.startswith("@@"):
        return style_config.ansi("diff_hunk", line)
    if line.startswith("+") and not line.startswith("+++"):
        return style_config.ansi("diff_add", line)
    if line.startswith("-") and not line.startswith("---"):
        return style_config.ansi("diff_delete", line)
    return style_config.ansi("muted", line)


def _parse_untracked_files(status_text: str) -> list[str]:
    paths: list[str] = []
    for line in status_text.splitlines():
        if line.startswith("?? "):
            paths.append(line[3:].strip())
    return paths
