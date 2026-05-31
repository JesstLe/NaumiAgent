"""Clipboard and transcript export tests."""

from __future__ import annotations

from pathlib import Path

from naumi_agent import clipboard


def test_strip_ansi_removes_terminal_escape_sequences() -> None:
    text = "\x1b[31m错误\x1b[0m: file_write 失败"

    assert clipboard.strip_ansi(text) == "错误: file_write 失败"


def test_save_transcript_writes_utf8_export(tmp_path: Path) -> None:
    path = clipboard.save_transcript("完整记录", base_dir=tmp_path, prefix="cli")

    assert path.parent == tmp_path / "exports"
    assert path.name.startswith("cli-")
    assert path.read_text(encoding="utf-8") == "完整记录"


def test_copy_or_save_transcript_copies_and_saves_plain_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    copied: list[str] = []

    def fake_copy(text: str) -> bool:
        copied.append(text)
        return True

    monkeypatch.setattr(clipboard, "copy_text", fake_copy)

    result = clipboard.copy_or_save_transcript(
        "\x1b[32m成功\x1b[0m",
        base_dir=tmp_path,
        prefix="tui",
    )

    assert result.copied is True
    assert copied == ["成功"]
    assert result.path.read_text(encoding="utf-8") == "成功"
    assert "已复制完整记录" in result.message


def test_copy_or_save_transcript_reports_export_when_clipboard_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(clipboard, "copy_text", lambda _text: False)

    result = clipboard.copy_or_save_transcript("诊断记录", base_dir=tmp_path)

    assert result.copied is False
    assert result.path.read_text(encoding="utf-8") == "诊断记录"
    assert "已保存完整记录" in result.message
