"""工具系统单元测试."""

import pytest

from naumi_agent.tools.base import ToolRegistry
from naumi_agent.tools.builtin import (
    BashRunTool,
    FileEditTool,
    FileReadTool,
    FileWriteTool,
    create_builtin_tools,
)


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool in create_builtin_tools():
        reg.register(tool)
    return reg


class TestToolRegistry:
    def test_register_and_get(self, registry: ToolRegistry) -> None:
        assert "file_read" in registry
        assert "file_write" in registry
        assert "file_edit" in registry
        assert "bash_run" in registry
        assert len(registry) >= 4

    def test_get_nonexistent(self, registry: ToolRegistry) -> None:
        assert registry.get("nonexistent") is None

    def test_openai_tools_format(self, registry: ToolRegistry) -> None:
        tools = registry.get_openai_tools()
        assert len(tools) >= 4
        for t in tools:
            assert t["type"] == "function"
            assert "function" in t
            assert "name" in t["function"]
            assert "parameters" in t["function"]

    def test_parse_arguments_accepts_decoded_object(self, registry: ToolRegistry) -> None:
        tool = registry.get("file_read")
        assert tool is not None

        assert tool.parse_arguments({"path": "pyproject.toml"}) == {
            "path": "pyproject.toml",
        }

    def test_parse_arguments_rejects_non_object_json(self, registry: ToolRegistry) -> None:
        tool = registry.get("file_read")
        assert tool is not None

        with pytest.raises(ValueError, match="expected object"):
            tool.parse_arguments('["pyproject.toml"]')

    def test_parse_arguments_rejects_non_json_value(self, registry: ToolRegistry) -> None:
        tool = registry.get("file_read")
        assert tool is not None

        with pytest.raises(ValueError, match="Invalid JSON arguments"):
            tool.parse_arguments(None)


class TestFileReadTool:
    @pytest.fixture
    def read_tool(self) -> FileReadTool:
        return FileReadTool()

    async def test_read_existing_file(self, read_tool: FileReadTool, tmp_path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello\nworld\n")

        result = await read_tool.execute(path=str(f))
        assert "hello" in result
        assert "world" in result

    async def test_read_nonexistent_file(self, read_tool: FileReadTool) -> None:
        result = await read_tool.execute(path="/nonexistent/file.txt")
        assert "Error" in result

    async def test_read_with_offset_and_limit(self, read_tool: FileReadTool, tmp_path) -> None:
        f = tmp_path / "lines.txt"
        f.write_text("\n".join(f"line {i}" for i in range(10)))

        result = await read_tool.execute(path=str(f), offset=2, limit=3)
        assert "line 2" in result
        assert "line 4" in result


class TestFileWriteTool:
    @pytest.fixture
    def write_tool(self) -> FileWriteTool:
        return FileWriteTool()

    async def test_write_new_file(self, write_tool: FileWriteTool, tmp_path) -> None:
        f = tmp_path / "new.txt"
        result = await write_tool.execute(path=str(f), content="hello world")
        assert "已创建" in result or "Successfully" in result
        assert f.read_text() == "hello world"

    async def test_write_creates_dirs(self, write_tool: FileWriteTool, tmp_path) -> None:
        f = tmp_path / "sub" / "dir" / "file.txt"
        result = await write_tool.execute(path=str(f), content="nested")
        assert "已创建" in result or "Successfully" in result
        assert f.read_text() == "nested"

    async def test_relative_write_uses_workspace_root(self, tmp_path) -> None:
        tool = FileWriteTool(workspace_root=tmp_path)

        result = await tool.execute(path="workspace/showcase/index.html", content="<h1>Hi</h1>")

        target = tmp_path / "workspace" / "showcase" / "index.html"
        assert target.read_text() == "<h1>Hi</h1>"
        assert str(target) in result


class TestFileEditTool:
    @pytest.fixture
    def edit_tool(self) -> FileEditTool:
        return FileEditTool()

    async def test_edit_replace(self, edit_tool: FileEditTool, tmp_path) -> None:
        f = tmp_path / "edit.txt"
        f.write_text("foo bar baz")

        result = await edit_tool.execute(
            path=str(f),
            old_text="bar",
            new_text="BAR",
        )
        assert "已编辑" in result or "Successfully" in result
        assert f.read_text() == "foo BAR baz"

    async def test_relative_edit_uses_workspace_root(self, tmp_path) -> None:
        f = tmp_path / "edit.txt"
        f.write_text("foo bar baz")
        tool = FileEditTool(workspace_root=tmp_path)

        result = await tool.execute(path="edit.txt", old_text="bar", new_text="BAR")

        assert f.read_text() == "foo BAR baz"
        assert str(f) in result

    async def test_edit_not_found(self, edit_tool: FileEditTool, tmp_path) -> None:
        f = tmp_path / "edit.txt"
        f.write_text("hello")

        result = await edit_tool.execute(
            path=str(f),
            old_text="not here",
            new_text="replacement",
        )
        assert "not found" in result

    async def test_edit_ambiguous(self, edit_tool: FileEditTool, tmp_path) -> None:
        f = tmp_path / "dup.txt"
        f.write_text("aaa aaa")

        result = await edit_tool.execute(
            path=str(f),
            old_text="aaa",
            new_text="bbb",
        )
        assert "appears 2 times" in result


class TestBashRunTool:
    @pytest.fixture
    def bash_tool(self) -> BashRunTool:
        return BashRunTool()

    async def test_simple_command(self, bash_tool: BashRunTool) -> None:
        result = await bash_tool.execute(command="echo hello")
        assert "hello" in result

    async def test_command_timeout(self, bash_tool: BashRunTool) -> None:
        result = await bash_tool.execute(command="sleep 10", timeout=1)
        assert "timed out" in result

    async def test_command_failure(self, bash_tool: BashRunTool) -> None:
        result = await bash_tool.execute(command="exit 1")
        assert "exit code: 1" in result

    async def test_default_cwd_uses_workspace_root(self, tmp_path) -> None:
        tool = BashRunTool(workspace_root=tmp_path)

        result = await tool.execute(command="pwd")

        assert f"工作目录: {tmp_path}" in result
        assert str(tmp_path) in result
