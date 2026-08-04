"""Focused tests for CC-04.1a extension discovery authority."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.skills.loader import (
    SkillDiscoverySnapshot,
    SkillLoader,
    SkillSource,
    build_skill_sources,
)
from naumi_agent.tools.extensions import (
    ExtensionDiscoveryTool,
    execute_extension_discovery,
    render_skill_discovery,
)


def _write_skill(root: Path, directory: str, *, name: str, description: str) -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    manifest = skill_dir / "SKILL.md"
    manifest.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n执行。\n",
        encoding="utf-8",
    )
    return manifest


def test_real_loader_records_precedence_shadow_and_invalid_manifest(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    configured = tmp_path / "configured"
    workspace.mkdir()
    home.mkdir()
    configured.mkdir()
    workspace_manifest = _write_skill(
        workspace / ".naumi" / "skills",
        "review",
        name="review",
        description="workspace winner",
    )
    user_manifest = _write_skill(
        home / ".naumi" / "skills",
        "review-copy",
        name="review",
        description="user shadow",
    )
    invalid_dir = configured / "broken"
    invalid_dir.mkdir()
    invalid_manifest = invalid_dir / "SKILL.md"
    invalid_manifest.write_text("没有 frontmatter", encoding="utf-8")

    sources = build_skill_sources(
        workspace_root=workspace,
        configured_paths=[str(configured)],
        home=home,
    )
    loader = SkillLoader(sources=sources)

    loaded = loader.load_all()
    snapshot = loader.discovery_snapshot

    assert [skill.description for skill in loaded] == ["workspace winner"]
    assert [source.scope for source in snapshot.sources] == [
        "workspace",
        "user",
        "configured",
    ]
    assert snapshot.sources[0].requires_trust_gate is True
    assert snapshot.selected_count == 1
    assert snapshot.shadowed_count == 1
    assert snapshot.invalid_count == 1
    selected, shadowed, invalid = snapshot.candidates
    assert selected.manifest_path == workspace_manifest
    assert shadowed.manifest_path == user_manifest
    assert shadowed.selected_manifest_path == workspace_manifest
    assert invalid.manifest_path == invalid_manifest
    assert invalid.reason_code == "invalid_manifest"


def test_sources_preserve_missing_roots_and_deduplicate_canonical_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    duplicate = workspace / ".naumi" / "skills"

    sources = build_skill_sources(
        workspace_root=workspace,
        configured_paths=[str(duplicate), str(tmp_path / "missing-extra")],
        home=home,
    )

    assert len(sources) == 3
    assert [source.priority for source in sources] == [0, 1, 2]
    assert all(source.available is False for source in sources)
    assert sources[2].scope == "configured"


def test_relative_configured_source_is_bound_to_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()

    sources = build_skill_sources(
        workspace_root=workspace,
        configured_paths=["team-skills"],
        home=home,
    )

    assert sources[2].path == (workspace / "team-skills").resolve()


def test_source_availability_is_frozen_at_load_time(tmp_path: Path) -> None:
    source_path = tmp_path / "later"
    loader = SkillLoader(
        sources=(
            SkillSource(
                scope="configured",
                path=source_path,
                priority=0,
                available=False,
            ),
        )
    )
    source_path.mkdir()
    _write_skill(source_path, "live", name="live", description="live")

    loader.load_all()
    snapshot = loader.discovery_snapshot
    source_path.rename(tmp_path / "moved-after-load")

    assert snapshot.sources[0].available is True
    assert snapshot.selected_count == 1


def test_reloading_replaces_discovery_snapshot_instead_of_retaining_stale_skill(
    tmp_path: Path,
) -> None:
    manifest = _write_skill(
        tmp_path,
        "once",
        name="once",
        description="temporary",
    )
    loader = SkillLoader(search_paths=[str(tmp_path)])
    assert [skill.name for skill in loader.load_all()] == ["once"]
    manifest.unlink()

    assert loader.load_all() == []
    assert loader.names == []
    assert loader.discovery_snapshot.candidates == ()


@pytest.mark.asyncio
async def test_slash_and_agent_helper_render_the_same_loader_snapshot(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, "audit", name="audit", description="audit source")
    loader = SkillLoader(search_paths=[str(tmp_path)])
    loader.load_all()
    engine = SimpleNamespace(skill_loader=loader)
    tool = ExtensionDiscoveryTool(engine)

    shared = await execute_extension_discovery(engine, kind="skills")
    agent = await tool.execute(kind="skills")

    assert shared == agent == render_skill_discovery(loader.discovery_snapshot)
    assert "扩展发现 · Skills" in shared
    assert "**audit**" in shared
    assert "不执行动态命令" in shared
    assert tool.metadata.read_only is True
    assert tool.metadata.concurrency_safe is True


@pytest.mark.asyncio
async def test_discovery_rejects_unimplemented_extension_kind() -> None:
    engine = SimpleNamespace(skill_loader=SkillLoader())

    with pytest.raises(ValueError, match="Plugin 与 MCP"):
        await execute_extension_discovery(engine, kind="plugins")


@pytest.mark.asyncio
async def test_shared_slash_route_is_available_to_new_ui_and_tui(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, "visible", name="visible", description="visible")
    loader = SkillLoader(search_paths=[str(tmp_path)])
    loader.load_all()
    engine = SimpleNamespace(skill_loader=loader)

    output = await execute_slash_command(engine, "/extensions skills")

    assert "扩展发现 · Skills" in output
    assert "visible" in output


def test_bridge_command_registry_advertises_extensions() -> None:
    from naumi_agent.ui.bridge import _slash_command_payload

    commands = {item["command"] for item in _slash_command_payload()}
    assert "/extensions" in commands


def test_renderer_neutralizes_control_characters_and_backticks_in_paths() -> None:
    snapshot = SkillDiscoverySnapshot(
        sources=(
            SkillSource(
                scope="configured",
                path=Path("/tmp/team\n`spoof`"),
                priority=0,
            ),
        ),
        candidates=(),
    )

    rendered = render_skill_discovery(snapshot)

    assert "/tmp/team\\n`spoof`" in rendered
    assert "/tmp/team\n`spoof`" not in rendered
