from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from naumi_agent.claude_source.governance import (
    SourceIdentityManifest,
    capture_source_identity,
    write_source_identity,
)
from naumi_agent.claude_source.refresh import SourceRefreshStore
from naumi_agent.claude_source.symbol_diff import (
    SourceSymbolDiff,
    build_source_symbol_diff,
)

CLAIM = "Symbol diff fixture license claim"
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, SourceRefreshStore, SourceIdentityManifest]:
    project = tmp_path / "project"
    source = tmp_path / "claude-code"
    mapping = project / "frontend" / "terminal-ui" / "cc-source-map.json"
    manifest_path = mapping.with_name("cc-source-map.v2.json")
    mapping.parent.mkdir(parents=True)
    mapping.write_text(
        json.dumps(
            {
                "source": {"name": "local-claude-code"},
                "mapping": [
                    {
                        "area": "entrypoint",
                        "claude_code": ["src/main.tsx"],
                        "naumi_agent": ["frontend/terminal-ui/src/index.js"],
                    },
                    {
                        "area": "permissions",
                        "claude_code": ["src/keybindings/defaultBindings.ts"],
                        "naumi_agent": ["src/naumi_agent/ui/keybindings.py"],
                    },
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "source@example.invalid")
    _git(source, "config", "user.name", "Source Fixture")
    _git(source, "remote", "add", "origin", "https://example.invalid/source.git")
    (source / "README.md").write_text(f"# source\n\n{CLAIM}\n", encoding="utf-8")
    (source / "src" / "keybindings").mkdir(parents=True)
    (source / "src" / "main.tsx").write_text(
        """
// export const FalsePositive = true;
export function App(props: { mode: string }) { return null; }
export type RuntimeEvent = "run/start" | "run/end";
dispatch("run/start");
logEvent("tengu_run_started");
const quotePattern = /["']/;
const ReexportedPanel = () => null;
export { ReexportedPanel };
const InternalOnly = true;
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (source / "src" / "keybindings" / "defaultBindings.ts").write_text(
        """
export const defaultBindings = {
  open: "ctrl+p",
  close: "escape",
};
""".strip()
        + "\n",
        encoding="utf-8",
    )
    _git(source, "add", ".")
    _git(source, "commit", "-m", "approved symbol baseline")
    manifest = capture_source_identity(
        source,
        mapping,
        source_name="local-claude-code",
        checkout_hint="../claude-code",
        license_path="README.md",
        license_claim=CLAIM,
        observed_at=NOW,
    )
    write_source_identity(manifest_path, manifest)
    store = SourceRefreshStore(tmp_path / "state" / "claude-source.db")
    store.bootstrap(
        manifest,
        source_root=source,
        project_root=project,
        reviewed_by="maintainer",
        review_reason="Approve symbol diff baseline.",
        reviewed_at=NOW,
    )
    return project, source, mapping, manifest_path, store, manifest


def test_unchanged_symbol_diff_is_deterministic_read_only_and_cli_usable(
    tmp_path: Path,
) -> None:
    project, source, _mapping, manifest_path, store, manifest = _fixture(tmp_path)
    before_mtime = store.db_path.stat().st_mtime_ns

    first = build_source_symbol_diff(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )
    second = build_source_symbol_diff(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )

    assert first == second
    assert first.status == "unchanged"
    assert first.baseline_commit == manifest.git.commit
    assert first.changes == ()
    assert first.analyzed_file_count == 4
    assert first.baseline_symbol_count == first.current_symbol_count
    assert first.baseline_symbol_count >= 7
    assert store.db_path.stat().st_mtime_ns == before_mtime

    command = subprocess.run(
        [
            str(Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"),
            "-m",
            "naumi_agent.claude_source.symbol_diff",
            "--manifest",
            str(manifest_path),
            "--source",
            str(source),
            "--project-root",
            str(project),
            "--store",
            str(store.db_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(command.stdout)["symbol_diff_id"] == first.symbol_diff_id


def test_dirty_symbol_diff_detects_exports_components_events_and_keys(
    tmp_path: Path,
) -> None:
    project, source, _mapping, manifest_path, store, _manifest = _fixture(tmp_path)
    (source / "src" / "main.tsx").write_text(
        """
export function App(props: { mode: number }) { return null; }
export const NewPanel = () => null;
export type RuntimeEvent = "run/stop";
dispatch("run/stop");
logEvent("tengu_run_stopped");
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (source / "src" / "keybindings" / "defaultBindings.ts").write_text(
        """
export const defaultBindings = {
  open: "ctrl+k",
  close: "escape",
};
""".strip()
        + "\n",
        encoding="utf-8",
    )

    receipt = build_source_symbol_diff(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )

    assert receipt.status == "change_detected"
    assert receipt.includes_worktree is True
    assert {
        (item.change_kind, item.symbol_kind, item.name)
        for item in receipt.changes
    } >= {
        ("modified", "component", "App"),
        ("modified", "export", "App"),
        ("added", "component", "NewPanel"),
        ("modified", "event", "RuntimeEvent"),
        ("removed", "component", "ReexportedPanel"),
        ("removed", "event", "run/start"),
        ("added", "event", "run/stop"),
        ("removed", "event", "tengu_run_started"),
        ("added", "event", "tengu_run_stopped"),
        ("removed", "keybinding", "ctrl+p"),
        ("added", "keybinding", "ctrl+k"),
    }
    assert set(receipt.affected_areas) == {"entrypoint", "permissions"}
    assert set(receipt.risk_flags) >= {
        "event_contract_changed",
        "keybinding_changed",
    }


def test_exact_git_rename_is_reported_as_symbol_move(tmp_path: Path) -> None:
    project, source, mapping, manifest_path, store, _manifest = _fixture(tmp_path)
    (source / "src" / "main.tsx").rename(source / "src" / "app.tsx")
    with (source / "src" / "app.tsx").open("a", encoding="utf-8") as stream:
        stream.write("export const RenamedOnly = () => null;\n")
    mapping_payload = json.loads(mapping.read_text(encoding="utf-8"))
    # The approved map intentionally remains on the old path. Rename identity
    # comes from Git structural evidence, not an unapproved mapping rewrite.
    assert mapping_payload["mapping"][0]["claude_code"] == ["src/main.tsx"]
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "rename mapped entrypoint")

    receipt = build_source_symbol_diff(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )

    assert receipt.status == "change_detected"
    moved = {
        (item.symbol_kind, item.name, item.old_path, item.new_path)
        for item in receipt.changes
        if item.change_kind == "moved"
    }
    assert ("component", "App", "src/main.tsx", "src/app.tsx") in moved
    assert ("export", "App", "src/main.tsx", "src/app.tsx") in moved
    added = next(
        item
        for item in receipt.changes
        if item.change_kind == "added"
        and item.symbol_kind == "component"
        and item.name == "RenamedOnly"
    )
    assert added.areas == ("entrypoint",)


def test_mapping_drift_and_unterminated_source_fail_closed(tmp_path: Path) -> None:
    project, source, mapping, manifest_path, store, _manifest = _fixture(tmp_path)
    mapping.write_text('{"mapping":[]}\n', encoding="utf-8")
    stale = build_source_symbol_diff(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )

    assert stale.status == "invalid"
    assert stale.analyzed_file_count == 0
    assert "mapping" in stale.errors[0]

    project, source, _mapping, manifest_path, store, _manifest = _fixture(
        tmp_path / "syntax"
    )
    (source / "src" / "main.tsx").write_text(
        'export const broken = "unterminated\n',
        encoding="utf-8",
    )
    invalid = build_source_symbol_diff(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )

    assert invalid.status == "invalid"
    assert "未终止字符串" in invalid.errors[0]


def test_symbol_diff_receipt_rejects_tampering(tmp_path: Path) -> None:
    project, source, _mapping, manifest_path, store, _manifest = _fixture(tmp_path)
    receipt = build_source_symbol_diff(
        store,
        manifest_path=manifest_path,
        source_root=source,
        project_root=project,
    )
    payload = receipt.model_dump(mode="json")
    payload["analyzed_file_count"] += 1

    with pytest.raises(ValueError, match="digest 不一致"):
        SourceSymbolDiff.model_validate(payload)
