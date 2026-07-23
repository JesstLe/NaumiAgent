"""Clipboard and transcript helpers for CLI/TUI diagnostics."""

from __future__ import annotations

import os
import platform
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class CopyResult:
    copied: bool
    path: Path
    message: str


def strip_ansi(text: str) -> str:
    """Return plain text with ANSI escape sequences removed."""
    return _ANSI_RE.sub("", text)


def save_transcript(text: str, *, base_dir: Path, prefix: str = "transcript") -> Path:
    """Persist transcript text to a unique timestamped UTF-8 file."""
    export_dir = base_dir / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", prefix).strip("._-")[:64]
    safe_prefix = safe_prefix or "transcript"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for suffix in range(100):
        unique_suffix = "" if suffix == 0 else f"-{suffix}"
        path = export_dir / f"{safe_prefix}-{stamp}{unique_suffix}.txt"
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            return path
        except FileExistsError:
            continue
    raise FileExistsError("无法为导出内容分配唯一文件名。")


def copy_text(text: str) -> bool:
    """Best-effort system clipboard copy without adding Python dependencies."""
    commands: list[list[str]] = []
    if os.name == "nt":
        commands.append(["clip"])
    else:
        system = platform.system().lower()
        if system == "darwin":
            commands.append(["pbcopy"])
        elif system == "linux":
            commands.extend([["wl-copy"], ["xclip", "-selection", "clipboard"]])

    for command in commands:
        try:
            subprocess.run(
                command,
                input=text,
                text=True,
                check=True,
                timeout=3.0,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except (
            FileNotFoundError,
            subprocess.SubprocessError,
            OSError,
            UnicodeError,
        ):
            continue
    return False


def copy_or_save_transcript(
    text: str,
    *,
    base_dir: Path,
    prefix: str = "transcript",
    content_label: str = "完整记录",
) -> CopyResult:
    """Save transcript and copy it to clipboard when possible."""
    plain = strip_ansi(text)
    path = save_transcript(plain, base_dir=base_dir, prefix=prefix)
    copied = copy_text(plain)
    if copied:
        message = f"已复制{content_label}，并保存到 {path}"
    else:
        message = f"无法访问系统剪贴板，已保存{content_label}到 {path}"
    return CopyResult(copied=copied, path=path, message=message)
