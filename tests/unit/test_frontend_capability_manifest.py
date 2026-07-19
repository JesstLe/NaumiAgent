"""UI-17.1 strict terminal capability manifest tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from naumi_agent.ui.capability_manifest import (
    REQUIRED_TERMINAL_CAPABILITIES,
    CapabilityManifestError,
    assert_required_terminal_parity,
    load_frontend_capability_manifest,
    missing_capability_evidence,
)
from naumi_agent.ui.protocol import (
    PROTOCOL_MAXIMUM_VERSION,
    PROTOCOL_MINIMUM_VERSION,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NEW_UI_MANIFEST = (
    PROJECT_ROOT / "frontend" / "terminal-ui" / "capability-manifest.json"
)
TUI_MANIFEST = (
    PROJECT_ROOT / "src" / "naumi_agent" / "tui" / "capability-manifest.json"
)


def test_shipped_terminal_manifests_have_exact_supported_parity_and_evidence() -> None:
    new_ui = load_frontend_capability_manifest("new_ui", NEW_UI_MANIFEST)
    tui = load_frontend_capability_manifest("tui", TUI_MANIFEST)

    assert tuple(sorted(new_ui.capabilities)) == tuple(
        sorted(REQUIRED_TERMINAL_CAPABILITIES)
    )
    assert tuple(sorted(tui.capabilities)) == tuple(
        sorted(REQUIRED_TERMINAL_CAPABILITIES)
    )
    assert new_ui.protocol.transport == "jsonl"
    assert new_ui.protocol.negotiated is True
    assert new_ui.protocol.minimum_version == PROTOCOL_MINIMUM_VERSION
    assert new_ui.protocol.maximum_version == PROTOCOL_MAXIMUM_VERSION
    assert tui.protocol.transport == "in_process"
    assert tui.protocol.negotiated is False
    assert_required_terminal_parity((new_ui, tui))
    assert missing_capability_evidence(new_ui, project_root=PROJECT_ROOT) == ()
    assert missing_capability_evidence(tui, project_root=PROJECT_ROOT) == ()


def test_default_manifest_resolver_loads_both_shipped_surfaces() -> None:
    assert load_frontend_capability_manifest("new_ui").surface == "new_ui"
    assert load_frontend_capability_manifest("tui").surface == "tui"


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("missing_capability", "覆盖不完整"),
        ("unknown_capability", "覆盖不完整"),
        ("unsafe_evidence", "受限的仓库相对路径"),
        ("degraded_without_note", "必须说明原因"),
        ("fake_in_process_negotiation", "不得伪造协议协商"),
    ],
)
def test_manifest_validation_fails_closed(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    document = json.loads(TUI_MANIFEST.read_text(encoding="utf-8"))
    if mutation == "missing_capability":
        document["capabilities"].pop("run_cancel")
    elif mutation == "unknown_capability":
        document["capabilities"]["imaginary_feature"] = {
            "state": "supported",
            "evidence": ["src/naumi_agent/tui/app.py", "tests/unit/test_tui.py"],
        }
    elif mutation == "unsafe_evidence":
        document["capabilities"]["run_cancel"]["evidence"][0] = "../secret"
    elif mutation == "degraded_without_note":
        document["capabilities"]["run_cancel"]["state"] = "degraded"
    elif mutation == "fake_in_process_negotiation":
        document["protocol"]["negotiated"] = True
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CapabilityManifestError, match=match):
        load_frontend_capability_manifest("tui", path)


def test_manifest_surface_and_parity_inputs_are_exact(tmp_path: Path) -> None:
    new_ui = load_frontend_capability_manifest("new_ui", NEW_UI_MANIFEST)
    document = json.loads(TUI_MANIFEST.read_text(encoding="utf-8"))
    document["surface"] = "new_ui"
    path = tmp_path / "wrong-surface.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CapabilityManifestError, match="surface 不匹配"):
        load_frontend_capability_manifest("tui", path)
    with pytest.raises(CapabilityManifestError, match="同时提供"):
        assert_required_terminal_parity((new_ui, new_ui))


def test_manifest_reports_missing_evidence_without_exposing_file_content(
    tmp_path: Path,
) -> None:
    manifest = load_frontend_capability_manifest("tui", TUI_MANIFEST)

    missing = missing_capability_evidence(manifest, project_root=tmp_path)

    assert missing
    assert all(path.startswith(("src/", "tests/")) for path in missing)
