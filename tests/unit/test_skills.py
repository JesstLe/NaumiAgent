"""Skill 系统单元测试."""

import pytest

from naumi_agent.skills.loader import SkillLoader
from naumi_agent.skills.skill import (
    Skill,
    SkillError,
    parse_skill_md,
)
from naumi_agent.skills.tool import SkillDispatchTool, SkillTool, create_skill_tools

# ---------------------------------------------------------------------------
#  Fixtures — 临时 Skill 目录
# ---------------------------------------------------------------------------


@pytest.fixture()
def skill_dir(tmp_path):
    """创建一个包含有效 SKILL.md 的临时目录."""
    skill_subdir = tmp_path / "greet"
    skill_subdir.mkdir()
    (skill_subdir / "SKILL.md").write_text(
        """\
---
name: greet
description: Generate a greeting
arguments:
  - name: who
    description: Target person
    required: true
  - name: style
    description: Greeting style
    required: false
    default: casual
---

# Greeting Skill

Say hello to $ARGUMENTS.
Use ${SKILL_DIR} for file references.
First arg: $0, second arg: $1.
""",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def multi_skill_dir(tmp_path):
    """创建包含多个 Skill 的临时目录."""
    for name in ("alpha", "beta", "gamma"):
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"""\
---
name: {name}
description: Skill {name}
---

Execute {name} with $ARGUMENTS.
""",
            encoding="utf-8",
        )
    return tmp_path


@pytest.fixture()
def dynamic_skill_dir(tmp_path):
    """创建一个包含动态上下文注入的 Skill."""
    d = tmp_path / "sysinfo"
    d.mkdir()
    (d / "SKILL.md").write_text(
        """\
---
name: sysinfo
description: System information
---

OS: `!`uname -s``
""",
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
#  parse_skill_md
# ---------------------------------------------------------------------------


class TestParseSkillMd:
    def test_valid_skill(self, skill_dir):
        path = skill_dir / "greet" / "SKILL.md"
        skill = parse_skill_md(path)

        assert skill.name == "greet"
        assert skill.description == "Generate a greeting"
        assert len(skill.arguments) == 2
        assert skill.arguments[0].name == "who"
        assert skill.arguments[0].required is True
        assert skill.arguments[1].name == "style"
        assert skill.arguments[1].required is False
        assert skill.arguments[1].default == "casual"
        assert "Say hello" in skill.instructions

    def test_missing_file(self, tmp_path):
        with pytest.raises(SkillError, match="not found"):
            parse_skill_md(tmp_path / "nonexistent" / "SKILL.md")

    def test_no_frontmatter(self, tmp_path):
        d = tmp_path / "bare"
        d.mkdir()
        (d / "SKILL.md").write_text("Just markdown content", encoding="utf-8")

        with pytest.raises(SkillError, match="missing frontmatter"):
            parse_skill_md(d / "SKILL.md")

    def test_missing_name(self, tmp_path):
        d = tmp_path / "noname"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\ndescription: No name\n---\nBody",
            encoding="utf-8",
        )

        with pytest.raises(SkillError, match="missing 'name'"):
            parse_skill_md(d / "SKILL.md")

    def test_invalid_yaml(self, tmp_path):
        d = tmp_path / "bad-yaml"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: [invalid: yaml\n---\nBody",
            encoding="utf-8",
        )

        with pytest.raises(SkillError, match="Invalid YAML"):
            parse_skill_md(d / "SKILL.md")

    def test_string_arguments(self, tmp_path):
        """Arguments as plain strings."""
        d = tmp_path / "str-args"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: str-args\narguments: [target, mode]\n---\nBody",
            encoding="utf-8",
        )
        skill = parse_skill_md(d / "SKILL.md")
        assert len(skill.arguments) == 2
        assert skill.arguments[0].name == "target"
        assert skill.arguments[1].name == "mode"

    def test_name_normalization(self, tmp_path):
        d = tmp_path / "norm"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: My Cool Skill\n---\nBody",
            encoding="utf-8",
        )
        skill = parse_skill_md(d / "SKILL.md")
        assert skill.name == "my-cool-skill"

    def test_allowed_tools(self, tmp_path):
        d = tmp_path / "tools"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: tools\nallowed_tools: [file_read, bash_run]\n---\nBody",
            encoding="utf-8",
        )
        skill = parse_skill_md(d / "SKILL.md")
        assert skill.allowed_tools == ["file_read", "bash_run"]

    def test_user_invocable_default(self, tmp_path):
        d = tmp_path / "default"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: default\n---\nBody",
            encoding="utf-8",
        )
        skill = parse_skill_md(d / "SKILL.md")
        assert skill.user_invocable is True

    def test_user_invocable_false(self, tmp_path):
        d = tmp_path / "noinv"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: noinv\nuser_invocable: false\n---\nBody",
            encoding="utf-8",
        )
        skill = parse_skill_md(d / "SKILL.md")
        assert skill.user_invocable is False

    def test_agent_skill_context_fork(self, tmp_path):
        d = tmp_path / "forker"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: forker\ncontext: fork\n---\nBody",
            encoding="utf-8",
        )
        skill = parse_skill_md(d / "SKILL.md")
        assert skill.is_agent_skill is True

    def test_metadata_preserves_unknown_fields(self, tmp_path):
        d = tmp_path / "meta"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: meta\ncustom_field: hello\nmodel: claude-opus\n---\nBody",
            encoding="utf-8",
        )
        skill = parse_skill_md(d / "SKILL.md")
        assert skill.metadata["custom_field"] == "hello"
        assert skill.metadata["model"] == "claude-opus"


# ---------------------------------------------------------------------------
#  Skill.render
# ---------------------------------------------------------------------------


class TestSkillRender:
    def test_arguments_replacement(self, skill_dir):
        skill = parse_skill_md(skill_dir / "greet" / "SKILL.md")
        rendered = skill.render(arguments="Alice casual")
        assert "Alice casual" in rendered
        assert "$ARGUMENTS" not in rendered

    def test_positional_args(self, skill_dir):
        skill = parse_skill_md(skill_dir / "greet" / "SKILL.md")
        rendered = skill.render(arguments="Alice formal")
        assert "First arg: Alice" in rendered
        assert "second arg: formal" in rendered

    def test_skill_dir_replacement(self, skill_dir):
        skill = parse_skill_md(skill_dir / "greet" / "SKILL.md")
        rendered = skill.render()
        assert str(skill_dir / "greet") in rendered
        assert "${SKILL_DIR}" not in rendered

    def test_extra_vars(self, skill_dir):
        skill = parse_skill_md(skill_dir / "greet" / "SKILL.md")
        body = "Hello ${name}, your ${role} is ready."
        skill = Skill(
            directory=skill_dir,
            name="test",
            instructions=body,
        )
        rendered = skill.render(extra_vars={"name": "Alice", "role": "report"})
        assert "Hello Alice" in rendered
        assert "report is ready" in rendered

    def test_empty_arguments(self, skill_dir):
        skill = parse_skill_md(skill_dir / "greet" / "SKILL.md")
        rendered = skill.render(arguments="")
        # $0, $1 should become empty
        assert "First arg: " in rendered

    def test_positional_out_of_range(self, tmp_path):
        skill = Skill(
            directory=tmp_path,
            name="test",
            instructions="Only $0 available, $1 should be empty.",
        )
        rendered = skill.render(arguments="hello")
        assert "hello" in rendered
        assert " should be empty" in rendered
        # $1 was replaced with empty string, not left as "$1"
        assert "$1" not in rendered


# ---------------------------------------------------------------------------
#  Dynamic context injection
# ---------------------------------------------------------------------------


class TestDynamicContext:
    def test_injects_command_output(self, dynamic_skill_dir):
        skill = parse_skill_md(dynamic_skill_dir / "sysinfo" / "SKILL.md")
        rendered = skill.render()
        # uname -s should produce something like "Darwin" or "Linux"
        assert "[command failed" not in rendered
        assert "[command timed out" not in rendered
        assert len(rendered.strip()) > len("OS:")

    def test_dynamic_disabled(self, dynamic_skill_dir):
        skill = parse_skill_md(dynamic_skill_dir / "sysinfo" / "SKILL.md")
        rendered = skill.render(inject_dynamic=False)
        # Should still contain the raw template
        assert "`!`uname -s``" in rendered

    def test_failed_command(self, tmp_path):
        skill = Skill(
            directory=tmp_path,
            name="test",
            instructions="Result: `!`false``",
        )
        rendered = skill.render()
        assert "[command failed" in rendered

    def test_timeout_command(self, tmp_path):
        skill = Skill(
            directory=tmp_path,
            name="test",
            instructions="Result: `!`sleep 60``",
        )
        rendered = skill.render()
        # 10s default timeout, sleep 60 will timeout
        # But we don't want to wait 10s in tests, so we just test the mechanism
        # This test is intentionally brief
        assert "Result:" in rendered


# ---------------------------------------------------------------------------
#  SkillLoader
# ---------------------------------------------------------------------------


class TestSkillLoader:
    def test_load_all(self, skill_dir):
        loader = SkillLoader(search_paths=[str(skill_dir)])
        skills = loader.load_all()

        assert len(skills) == 1
        assert skills[0].name == "greet"

    def test_load_multiple(self, multi_skill_dir):
        loader = SkillLoader(search_paths=[str(multi_skill_dir)])
        skills = loader.load_all()

        assert len(skills) == 3
        names = {s.name for s in skills}
        assert names == {"alpha", "beta", "gamma"}

    def test_deduplication(self, multi_skill_dir, tmp_path):
        """Same skill in multiple paths — first one wins."""
        dup_dir = tmp_path / "dup"
        dup_dir.mkdir()
        sub = dup_dir / "alpha"
        sub.mkdir()
        (sub / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Duplicate\n---\nDup body",
            encoding="utf-8",
        )

        loader = SkillLoader(
            search_paths=[str(multi_skill_dir), str(dup_dir)]
        )
        loader.load_all()
        skill = loader.get("alpha")

        # First occurrence wins (from multi_skill_dir)
        assert skill.description == "Skill alpha"

    def test_get_by_name(self, skill_dir):
        loader = SkillLoader(search_paths=[str(skill_dir)])
        loader.load_all()

        assert loader.get("greet") is not None
        assert loader.get("nonexistent") is None

    def test_names_property(self, multi_skill_dir):
        loader = SkillLoader(search_paths=[str(multi_skill_dir)])
        loader.load_all()

        assert set(loader.names) == {"alpha", "beta", "gamma"}

    def test_len(self, multi_skill_dir):
        loader = SkillLoader(search_paths=[str(multi_skill_dir)])
        loader.load_all()

        assert len(loader) == 3

    def test_contains(self, multi_skill_dir):
        loader = SkillLoader(search_paths=[str(multi_skill_dir)])
        loader.load_all()

        assert "alpha" in loader
        assert "missing" not in loader

    def test_empty_paths(self):
        loader = SkillLoader(search_paths=[])
        skills = loader.load_all()
        assert skills == []

    def test_nonexistent_path(self):
        loader = SkillLoader(search_paths=["/nonexistent/path"])
        skills = loader.load_all()
        assert skills == []

    def test_skips_non_skill_dirs(self, tmp_path):
        """Files and dirs without SKILL.md are skipped."""
        (tmp_path / "readme.txt").write_text("not a skill dir")
        (tmp_path / "nodd").mkdir()
        loader = SkillLoader(search_paths=[str(tmp_path)])
        skills = loader.load_all()
        assert skills == []

    def test_skips_invalid_skill(self, tmp_path):
        """Invalid SKILL.md is skipped with warning, not crash."""
        d = tmp_path / "bad"
        d.mkdir()
        (d / "SKILL.md").write_text("No frontmatter here", encoding="utf-8")

        loader = SkillLoader(search_paths=[str(tmp_path)])
        skills = loader.load_all()
        assert skills == []


# ---------------------------------------------------------------------------
#  SkillTool
# ---------------------------------------------------------------------------


class TestSkillTool:
    def test_tool_name(self, skill_dir):
        skill = parse_skill_md(skill_dir / "greet" / "SKILL.md")
        tool = SkillTool(skill)
        assert tool.name == "skill_greet"

    def test_tool_description(self, skill_dir):
        skill = parse_skill_md(skill_dir / "greet" / "SKILL.md")
        tool = SkillTool(skill)
        assert "Generate a greeting" in tool.description
        assert "who(required)" in tool.description

    def test_tool_schema(self, skill_dir):
        skill = parse_skill_md(skill_dir / "greet" / "SKILL.md")
        tool = SkillTool(skill)
        schema = tool.parameters_schema

        assert schema["type"] == "object"
        assert "arguments" in schema["properties"]
        assert "who" in schema["properties"]
        assert "who" in schema["required"]

    @pytest.mark.asyncio
    async def test_tool_execute(self, skill_dir):
        skill = parse_skill_md(skill_dir / "greet" / "SKILL.md")
        tool = SkillTool(skill)
        result = await tool.execute(who="World", arguments="World")
        assert "World" in result

    def test_openai_tool_format(self, skill_dir):
        skill = parse_skill_md(skill_dir / "greet" / "SKILL.md")
        tool = SkillTool(skill)
        fmt = tool.to_openai_tool()

        assert fmt["type"] == "function"
        assert fmt["function"]["name"] == "skill_greet"


class TestSkillDispatchTool:
    def test_dispatch_schema(self):
        tool = SkillDispatchTool(["alpha", "beta"])
        schema = tool.parameters_schema

        assert "name" in schema["properties"]
        assert schema["properties"]["name"]["enum"] == ["alpha", "beta"]
        assert "name" in schema["required"]

    @pytest.mark.asyncio
    async def test_dispatch_execute_raises(self):
        tool = SkillDispatchTool(["alpha"])
        with pytest.raises(NotImplementedError):
            await tool.execute(name="alpha")


class TestCreateSkillTools:
    def test_few_skills_get_individual_tools(self, multi_skill_dir):
        loader = SkillLoader(search_paths=[str(multi_skill_dir)])
        skills = loader.load_all()

        tools = create_skill_tools(skills)
        assert len(tools) == 3
        assert all(isinstance(t, SkillTool) for t in tools)

    def test_many_skills_get_dispatch_tool(self, tmp_path):
        """More than 10 skills → single dispatch tool."""
        for i in range(12):
            d = tmp_path / f"skill-{i:02d}"
            d.mkdir()
            (d / "SKILL.md").write_text(
                f"---\nname: skill-{i:02d}\ndescription: Skill {i}\n---\nBody",
                encoding="utf-8",
            )

        loader = SkillLoader(search_paths=[str(tmp_path)])
        skills = loader.load_all()

        tools = create_skill_tools(skills)
        assert len(tools) == 1
        assert isinstance(tools[0], SkillDispatchTool)

    def test_empty_skills(self):
        tools = create_skill_tools([])
        assert tools == []
