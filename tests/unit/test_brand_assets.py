"""Contracts for the approved black-tile, white-N brand and every derivative."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "assets/brand-manifest.json").read_text(encoding="utf-8"))
SOURCE_SHA256 = "53229b3fe0fcd8108111aa859c606fe99e8839e78759c8d86db977a426f4fe5e"
spec = importlib.util.spec_from_file_location(
    "brand_generator", ROOT / "scripts/generate_brand_assets.py"
)
assert spec and spec.loader
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


def test_approved_source_is_pinned_and_self_contained() -> None:
    source = (ROOT / MANIFEST["source"]).read_bytes()
    assert hashlib.sha256(source).hexdigest() == SOURCE_SHA256 == MANIFEST["source_sha256"]
    svg = ET.fromstring(source)
    tags = {element.tag.rsplit("}", 1)[-1] for element in svg.iter()}
    assert tags == {"svg", "title", "rect", "path"}
    assert svg.attrib["viewBox"] == "0 0 128 128"
    assert b"#181B23" in source and b"#FFFFFF" in source


@pytest.mark.parametrize("relative,expected", MANIFEST["outputs"].items())
def test_derived_asset_matches_manifest(relative: str, expected: str) -> None:
    asset = ROOT / relative
    assert asset.is_file()
    assert hashlib.sha256(asset.read_bytes()).hexdigest() == expected


def test_canonical_png_is_transparent_with_approved_colors() -> None:
    with Image.open(ROOT / "assets/logo.png") as image:
        image.load()
        assert image.size == (1024, 1024)
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((512, 128)) == (24, 27, 35, 255)
        assert image.getpixel((304, 688)) == (255, 255, 255, 255)


@pytest.mark.parametrize("name,size", generator.PNG_SIZES.items())
def test_windows_png_pixels_derive_from_canonical(name: str, size: int) -> None:
    with Image.open(ROOT / "assets/logo.png") as source:
        expected = source.resize((size, size), Image.Resampling.LANCZOS)
    with Image.open(ROOT / generator.WINDOWS / name) as actual:
        assert actual.size == (size, size)
        assert ImageChops.difference(actual, expected).getbbox(alpha_only=False) is None


def test_multisize_containers_decode_and_macos_matches_windows() -> None:
    icons = ROOT / generator.WINDOWS
    with Image.open(icons / "icon.ico") as ico:
        assert ico.ico.sizes() == {(s, s) for s in (16, 24, 32, 48, 64, 128, 256)}
        for size in ico.ico.sizes():
            ico.ico.getimage(size).load()
    with Image.open(icons / "icon.icns") as icns:
        icns.load()
        assert icns.size == (1024, 1024)
    assert (icons / "icon.icns").read_bytes() == (
        ROOT / generator.MACOS / "Resources/AppIcon.icns"
    ).read_bytes()


def test_native_mark_is_compiled_from_source_without_resource_bundle() -> None:
    assert (ROOT / generator.SWIFT).read_text(encoding="utf-8") == generator.native_mark(
        ROOT / "assets/logo.svg"
    )


def test_native_compiler_rejects_unsupported_geometry(tmp_path: Path) -> None:
    source = (ROOT / "assets/logo.svg").read_text(encoding="utf-8")
    path = tmp_path / "invalid.svg"
    path.write_text(source.replace("M38 86", "M38 86Q20 20 10 10"), encoding="utf-8")
    with pytest.raises(ValueError, match="绝对 M/L"):
        generator.native_mark(path)


def test_browser_icons_and_current_consumers_use_the_canonical_source() -> None:
    public = ROOT / generator.PUBLIC
    assert (public / "brand-mark.svg").read_bytes() == (ROOT / "assets/logo.svg").read_bytes()
    assert (public / "favicon.ico").read_bytes() == (
        ROOT / generator.WINDOWS / "icon.ico"
    ).read_bytes()
    with Image.open(public / "apple-touch-icon.png") as touch:
        assert touch.size == (180, 180) and touch.mode == "RGB"
    index = (ROOT / "frontend/web/index.html").read_text(encoding="utf-8")
    for filename in ("brand-mark.svg", "favicon.ico", "apple-touch-icon.png"):
        assert f"%BASE_URL%{filename}" in index
    for relative in (
        "frontend/web2/src/Web2.tsx",
        "frontend/web/src/components/Shell/MainLayout.tsx",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "assets/logo.svg'" in source and "src={brandLogo}" in source
    assert 'src="assets/logo.png"' in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "../assets/logo.png" in (ROOT / "docs/README.md").read_text(encoding="utf-8")


def test_discarded_logo_candidates_remain_absent() -> None:
    directory = ROOT / "docs/assets/mac-agent-workbench"
    assert not list(directory.glob("logo-variant-*.png"))
    assert not (directory / "naumiagent-workbench-logo-selected.png").exists()
