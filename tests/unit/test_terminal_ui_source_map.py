from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_CODE_ROOT = Path("/Users/lv/Workspace/claude-code")


def test_terminal_ui_source_map_points_to_existing_naumi_files() -> None:
    source_map_path = PROJECT_ROOT / "frontend" / "terminal-ui" / "cc-source-map.json"
    payload = json.loads(source_map_path.read_text(encoding="utf-8"))

    assert payload["source"]["name"] == "local-claude-code"
    assert payload["source"]["readme_claim"]
    assert payload["mapping"]

    valid_statuses = {"implemented", "partial", "planned"}
    areas = {entry["area"] for entry in payload["mapping"]}
    assert {
        "entrypoint",
        "terminal-renderer",
        "message-components",
        "state-and-resume",
        "permissions-and-modes",
        "tasks-and-progress",
        "debug-and-diagnostics",
    }.issubset(areas)

    for entry in payload["mapping"]:
        assert entry["phase_one_status"] in valid_statuses
        assert entry["claude_code"]
        assert entry["naumi_agent"]
        for relative_path in entry["naumi_agent"]:
            assert (PROJECT_ROOT / relative_path).exists(), relative_path


def test_terminal_ui_source_map_points_to_verified_claude_code_sources() -> None:
    source_map_path = PROJECT_ROOT / "frontend" / "terminal-ui" / "cc-source-map.json"
    payload = json.loads(source_map_path.read_text(encoding="utf-8"))
    readme = CLAUDE_CODE_ROOT / "README.md"

    assert Path(payload["source"]["workspace_path"]) == CLAUDE_CODE_ROOT
    assert readme.exists()
    readme_text = readme.read_text(encoding="utf-8")
    assert "React + [Ink]" in readme_text
    assert payload["source"]["readme_claim"] in readme_text

    for entry in payload["mapping"]:
        for relative_path in entry["claude_code"]:
            assert (CLAUDE_CODE_ROOT / relative_path).exists(), relative_path


def test_claude_code_audit_doc_references_machine_readable_source_map() -> None:
    doc_path = PROJECT_ROOT / "docs" / "14-claude-code-source-audit.md"
    text = doc_path.read_text(encoding="utf-8")

    assert "frontend/terminal-ui/cc-source-map.json" in text
    assert "frontend/terminal-ui/protocol-contract.json" in text
    assert "UI Event Protocol" in text
    assert "未完成的 CC 对齐点" in text
