"""Tool forge tests."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from naumi_agent.tools.forge import (
    MAX_FORGE_CODE_CHARS,
    ForgeTool,
    _extract_python_code,
    _import_test,
    _to_class_name,
    _validate_tool_code,
    build_deterministic_tool_code,
    forge_tool,
    list_generated_tools,
    load_all_generated_tools,
    load_generated_tool,
    remove_generated_tool,
    save_tool,
)

VALID_TOOL_CODE = textwrap.dedent("""\
    from __future__ import annotations

    from typing import Any

    from naumi_agent.tools.base import Tool

    class CommentCounterTool(Tool):
        @property
        def name(self) -> str:
            return "comment_counter"

        @property
        def description(self) -> str:
            return "统计代码中的注释行数和注释率"

        @property
        def parameters_schema(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "源代码文本",
                    },
                },
                "required": ["source"],
            }

        async def execute(self, *, source: str, **kwargs: Any) -> str:
            lines = source.split("\\n")
            total = len(lines)
            comment_lines = sum(
                1 for ln in lines if ln.strip().startswith("#")
            )
            ratio = comment_lines / total if total > 0 else 0.0
            return f"注释行: {comment_lines}/{total} ({ratio:.1%})"
    """)


class TestToClassName:
    def test_snake_case(self):
        assert _to_class_name("comment_counter") == "CommentCounterTool"

    def test_hyphenated(self):
        assert _to_class_name("my-tool") == "MyToolTool"

    def test_single_word(self):
        assert _to_class_name("calculator") == "CalculatorTool"


class TestDeterministicName:
    def test_chinese_description_gets_stable_non_colliding_name(self):
        code = build_deterministic_tool_code("统计代码注释率的工具")
        assert "custom_tool_71d3ab07" in code


class TestExtractPythonCode:
    def test_extracts_from_markdown_fence(self):
        raw = 'Some text\n```python\nx = 1\n```\nMore text'
        result = _extract_python_code(raw)
        assert result == "x = 1"

    def test_extracts_plain_fence(self):
        raw = '```\nx = 1\n```'
        result = _extract_python_code(raw)
        assert result == "x = 1"

    def test_returns_raw_if_no_fence(self):
        raw = "x = 1\ny = 2"
        result = _extract_python_code(raw)
        assert "x = 1" in result

    def test_handles_empty_input(self):
        result = _extract_python_code("")
        assert result == ""


class TestValidateToolCode:
    def test_validates_correct_code(self):
        ok, msg = _validate_tool_code(VALID_TOOL_CODE)
        assert ok
        assert msg == "CommentCounterTool"

    def test_rejects_syntax_error(self):
        ok, msg = _validate_tool_code("def broken(\n")
        assert not ok
        assert "语法错误" in msg

    def test_rejects_no_tool_inheritance(self):
        ok, msg = _validate_tool_code("class Foo:\n    pass\n")
        assert not ok
        assert "Tool" in msg

    def test_rejects_missing_name(self):
        code = textwrap.dedent("""\
            from naumi_agent.tools.base import Tool

            class BadTool(Tool):
                @property
                def description(self) -> str:
                    return "no name"

                @property
                def parameters_schema(self) -> dict:
                    return {}

                async def execute(self, **kwargs) -> str:
                    return "ok"
        """)
        ok, msg = _validate_tool_code(code)
        assert not ok
        assert "name property" in msg

    def test_rejects_missing_execute(self):
        code = textwrap.dedent("""\
            from naumi_agent.tools.base import Tool

            class BadTool(Tool):
                @property
                def name(self) -> str:
                    return "bad"

                @property
                def description(self) -> str:
                    return "no execute"

                @property
                def parameters_schema(self) -> dict:
                    return {}
        """)
        ok, msg = _validate_tool_code(code)
        assert not ok
        assert "execute method" in msg


class TestDeterministicToolCode:
    def test_builds_valid_runnable_tool_code(self):
        code = build_deterministic_tool_code(
            "count comments in source text",
            "comment_counter_scaffold",
        )

        ok, class_name = _validate_tool_code(code)
        assert ok
        assert class_name == "CommentCounterScaffoldTool"

        import_ok, tool_name = _import_test(code, class_name)
        assert import_ok
        assert tool_name == "comment_counter_scaffold"


class TestImportTest:
    def test_passes_on_valid_code(self):
        ok, msg = _import_test(VALID_TOOL_CODE, "CommentCounterTool")
        assert ok
        assert msg == "comment_counter"

    def test_fails_on_wrong_class_name(self):
        ok, msg = _import_test(VALID_TOOL_CODE, "NonexistentTool")
        assert not ok

    def test_fails_on_import_error(self):
        bad_code = "from nonexistent_module import Foo\n"
        ok, msg = _import_test(bad_code, "Foo")
        assert not ok

    def test_rejects_unsafe_instance_name(self):
        bad_name_code = VALID_TOOL_CODE.replace(
            'return "comment_counter"',
            'return "../escape"',
        )
        ok, msg = _import_test(bad_name_code, "CommentCounterTool")
        assert not ok
        assert "生成工具实例名" in msg


class TestSaveTool:
    def test_saves_to_generated_dir(self, tmp_path: Path):
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            path = save_tool("test_tool", "x = 1\n")
            assert path.name == "test_tool.py"
            assert path.read_text() == "x = 1\n"

    def test_sanitizes_name(self, tmp_path: Path):
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            path = save_tool("my-cool-tool", "x = 1\n")
            assert path.name == "my_cool_tool.py"

    def test_rejects_path_like_name(self, tmp_path: Path):
        with (
            patch(
                "naumi_agent.tools.forge.get_generated_dir",
                return_value=tmp_path,
            ),
            pytest.raises(ValueError, match="只能包含小写字母"),
        ):
            save_tool("../escape", "x = 1\n")


class TestLoadGeneratedTool:
    def test_loads_existing_tool(self, tmp_path: Path):
        (tmp_path / "comment_counter.py").write_text(
            VALID_TOOL_CODE, encoding="utf-8"
        )
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            tool = load_generated_tool("comment_counter")
            assert tool is not None
            assert tool.name == "comment_counter"

    def test_returns_none_for_missing(self, tmp_path: Path):
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            tool = load_generated_tool("nonexistent")
            assert tool is None

    def test_rejects_path_like_name(self, tmp_path: Path):
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            assert load_generated_tool("../escape") is None

    def test_rejects_generated_tool_with_unsafe_instance_name(self, tmp_path: Path):
        unsafe_code = VALID_TOOL_CODE.replace(
            'return "comment_counter"',
            'return "../escape"',
        )
        (tmp_path / "comment_counter.py").write_text(
            unsafe_code,
            encoding="utf-8",
        )

        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            assert load_generated_tool("comment_counter") is None


class TestListGeneratedTools:
    def test_lists_existing_tools(self, tmp_path: Path):
        (tmp_path / "comment_counter.py").write_text(
            VALID_TOOL_CODE, encoding="utf-8"
        )
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            tools = list_generated_tools()
            assert len(tools) == 1
            assert tools[0]["name"] == "comment_counter"
            assert "注释" in tools[0]["description"]

    def test_empty_dir(self, tmp_path: Path):
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            tools = list_generated_tools()
            assert tools == []

    def test_skips_init(self, tmp_path: Path):
        (tmp_path / "__init__.py").write_text("", encoding="utf-8")
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            tools = list_generated_tools()
            assert tools == []


class TestRemoveGeneratedTool:
    def test_removes_existing(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text("x = 1", encoding="utf-8")

        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            assert remove_generated_tool("test")
            assert not f.exists()

    def test_returns_false_for_missing(self, tmp_path: Path):
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            assert not remove_generated_tool("nonexistent")

    def test_rejects_path_like_name(self, tmp_path: Path):
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            assert not remove_generated_tool("../escape")


class TestLoadAllGeneratedTools:
    def test_loads_all(self, tmp_path: Path):
        (tmp_path / "comment_counter.py").write_text(
            VALID_TOOL_CODE, encoding="utf-8"
        )

        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            tools = load_all_generated_tools()
            assert len(tools) == 1
            assert tools[0].name == "comment_counter"

    def test_skips_invalid(self, tmp_path: Path):
        (tmp_path / "broken.py").write_text(
            "not valid tool code", encoding="utf-8"
        )

        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            tools = load_all_generated_tools()
            assert tools == []


class TestForgeTool:
    def test_forges_deterministic_tool_without_code(self, tmp_path: Path):
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            result = forge_tool(
                "count comments in source text",
                tool_name="comment_counter_scaffold",
            )
        assert result["status"] == "forged"
        assert result["generation_mode"] == "deterministic"
        assert result["tool_name"] == "comment_counter_scaffold"
        assert Path(result["file_path"]).exists()

    def test_forges_with_valid_code(self, tmp_path: Path):
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            result = forge_tool(
                description="统计代码注释率的工具",
                llm_output=VALID_TOOL_CODE,
            )
        assert result["status"] == "forged"
        assert result["tool_name"] == "comment_counter"
        assert result["class_name"] == "CommentCounterTool"
        assert Path(result["file_path"]).exists()

    def test_rejects_invalid_code(self):
        result = forge_tool(
            description="bad tool",
            llm_output="class NotATool:\n    pass",
        )
        assert result["status"] == "rejected"
        assert "验证失败" in result["error"]

    def test_uses_explicit_tool_name(self, tmp_path: Path):
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            result = forge_tool(
                description="统计代码注释率的工具",
                tool_name="my_counter",
                llm_output=VALID_TOOL_CODE,
            )
        assert result["status"] == "forged"
        assert result["tool_name"] == "my_counter"

    def test_rejects_invalid_explicit_tool_name_before_write(self, tmp_path: Path):
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            result = forge_tool(
                description="统计代码注释率的工具",
                tool_name="../escape",
                llm_output=VALID_TOOL_CODE,
            )

        assert result["status"] == "rejected"
        assert "只能包含小写字母" in result["error"]
        assert list(tmp_path.glob("*.py")) == []

    def test_rejects_oversized_llm_output_before_validation(self):
        result = forge_tool(
            description="bad",
            llm_output="x" * (MAX_FORGE_CODE_CHARS + 1),
        )

        assert result["status"] == "rejected"
        assert "llm_output 过大" in result["error"]


class TestForgeToolClass:
    def test_tool_name(self):
        assert ForgeTool().name == "forge_tool"

    def test_tool_description(self):
        desc = ForgeTool().description
        assert "锻造" in desc or "工具" in desc

    def test_tool_schema(self):
        schema = ForgeTool().parameters_schema
        assert "description" in schema["properties"]
        assert "tool_name" in schema["properties"]
        assert "llm_output" in schema["properties"]
        assert schema["required"] == ["description"]

    def test_metadata_marks_forge_as_confirmed_state_change(self):
        metadata = ForgeTool().metadata
        assert metadata.destructive is True
        assert metadata.requires_confirmation is True
        assert metadata.user_facing_name == "工具锻造"

    @pytest.mark.asyncio
    async def test_execute_rejects_empty_description_before_forging(self):
        tool = ForgeTool()

        result = await tool.execute(description="")

        assert "验证未通过" in result
        assert "description 不能为空" in result

    @pytest.mark.asyncio
    async def test_execute_forges_deterministic_tool(self, tmp_path: Path):
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            tool = ForgeTool()
            result = await tool.execute(
                description="a tool that counts lines",
                tool_name="line_counter_scaffold",
            )
        assert "锻造成功" in result
        assert "deterministic" in result
        assert (tmp_path / "line_counter_scaffold.py").exists()

    @pytest.mark.asyncio
    async def test_execute_forges_tool(self, tmp_path: Path):
        with patch(
            "naumi_agent.tools.forge.get_generated_dir",
            return_value=tmp_path,
        ):
            tool = ForgeTool()
            result = await tool.execute(
                description="统计注释",
                llm_output=VALID_TOOL_CODE,
            )
        assert "锻造成功" in result

    @pytest.mark.asyncio
    async def test_execute_rejects_invalid(self):
        tool = ForgeTool()
        result = await tool.execute(
            description="bad",
            llm_output="class X:\n    pass",
        )
        assert "验证未通过" in result
