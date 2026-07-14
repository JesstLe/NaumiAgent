"""Repository-level invariants for the canonical NaumiAgent logo."""

from __future__ import annotations

import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_canonical_logo_is_the_selected_square_png() -> None:
    logo = ROOT / "assets" / "logo.png"
    header = logo.read_bytes()[:24]

    assert header[:8] == b"\x89PNG\r\n\x1a\n"
    assert header[12:16] == b"IHDR"
    assert struct.unpack(">II", header[16:24]) == (1254, 1254)


def test_old_logo_variants_are_absent_and_platform_icon_remains() -> None:
    removed = [
        ROOT / "assets" / "logo.svg",
        ROOT / "docs" / "assets" / "mac-agent-workbench" / "logo-variant-a-minimal.png",
        ROOT / "docs" / "assets" / "mac-agent-workbench" / "logo-variant-b-macos-depth.png",
        ROOT / "docs" / "assets" / "mac-agent-workbench" / "logo-variant-c-brand-mark.png",
        ROOT / "docs" / "assets" / "mac-agent-workbench" / "naumiagent-workbench-logo-selected.png",
    ]

    assert not any(path.exists() for path in removed)
    assert (
        ROOT / "apps" / "macos" / "NaumiAgentWorkbench" / "Resources" / "AppIcon.icns"
    ).is_file()


def test_current_documentation_references_only_the_canonical_logo() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    docs_index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")

    assert 'src="assets/logo.png"' in readme
    assert "assets/logo.svg" not in readme
    assert "../assets/logo.png" in docs_index
