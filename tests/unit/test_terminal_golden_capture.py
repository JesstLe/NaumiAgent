from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from naumi_agent.ui.terminal_capture import (
    TerminalCaptureError,
    capture_terminal_goldens,
)

FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "ui17"
    / "terminal-run-lifecycle-golden.json"
)


@pytest.mark.asyncio
async def test_dual_surface_capture_is_deterministic_and_semantically_aligned(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js 不可用")
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    first = await capture_terminal_goldens(FIXTURE, first_dir)
    second = await capture_terminal_goldens(FIXTURE, second_dir)

    assert first == second
    assert first["schema"] == "naumi.terminal-golden-capture.v1"
    assert first["semantic_parity"]["passed"] is True
    assert first["viewport"] == {"width": 100, "height": 40}
    for surface, prefix in (("new_ui", "new-ui"), ("tui", "tui")):
        capture = first["captures"][surface]
        ansi = (first_dir / f"{prefix}.ansi").read_text(encoding="utf-8")
        text = (first_dir / f"{prefix}.txt").read_text(encoding="utf-8")
        assert "\x1b[" in ansi
        assert "\x1b[" not in text
        assert text.endswith("\n")
        assert len(text.splitlines()) == 40
        assert capture["missing_anchors"] == []
        assert capture["ansi_sha256"] == _digest(ansi)
        assert capture["text_sha256"] == _digest(text)
        for anchor in first["semantic_parity"]["required_anchors"]:
            assert anchor in text
        assert (first_dir / f"{prefix}.ansi").read_bytes() == (
            second_dir / f"{prefix}.ansi"
        ).read_bytes()
        assert (first_dir / f"{prefix}.txt").read_bytes() == (
            second_dir / f"{prefix}.txt"
        ).read_bytes()
    assert json.loads((first_dir / "manifest.json").read_text(encoding="utf-8")) == first


@pytest.mark.asyncio
async def test_capture_fails_closed_before_publishing_on_missing_anchor(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js 不可用")
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["capture"]["required_anchors"].append("不存在的双端锚点")
    fixture_path = tmp_path / "missing-anchor.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    output_dir = tmp_path / "capture"

    with pytest.raises(TerminalCaptureError, match="缺少语义锚点"):
        await capture_terminal_goldens(fixture_path, output_dir)

    assert not output_dir.exists()


@pytest.mark.asyncio
async def test_capture_rejects_invalid_viewport_without_publishing(tmp_path: Path) -> None:
    output_dir = tmp_path / "capture"

    with pytest.raises(TerminalCaptureError, match="width 必须是 40-400"):
        await capture_terminal_goldens(FIXTURE, output_dir, width=39)

    assert not output_dir.exists()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
