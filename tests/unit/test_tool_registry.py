"""工具系统单元测试."""

import shlex
import sys
from pathlib import Path

import pytest

from naumi_agent.tools.base import ToolRegistry
from naumi_agent.tools.builtin import (
    BashRunTool,
    FileEditTool,
    FileReadTool,
    FileWriteTool,
    GlobTool,
    GrepTool,
    ReadTool,
    YamlValidateTool,
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
        assert "glob" in registry
        assert "grep" in registry
        assert "read" in registry
        assert "file_read" in registry
        assert "file_write" in registry
        assert "file_edit" in registry
        assert "bash_run" in registry
        assert len(registry) >= 7

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

    def test_builtin_tool_metadata_exposes_execution_traits(
        self,
        registry: ToolRegistry,
    ) -> None:
        glob_tool = registry.get("glob")
        grep_tool = registry.get("grep")
        read_alias = registry.get("read")
        read_tool = registry.get("file_read")
        write_tool = registry.get("file_write")
        bash_tool = registry.get("bash_run")
        yaml_tool = registry.get("yaml_validate")

        assert glob_tool is not None
        assert grep_tool is not None
        assert read_alias is not None
        assert read_tool is not None
        assert write_tool is not None
        assert bash_tool is not None
        assert yaml_tool is not None

        assert glob_tool.is_read_only
        assert glob_tool.is_concurrency_safe
        assert glob_tool.metadata.path_argument_names == ("directory",)
        assert grep_tool.is_read_only
        assert grep_tool.is_concurrency_safe
        assert grep_tool.metadata.path_argument_names == ("path",)
        assert read_alias.is_read_only
        assert read_alias.is_concurrency_safe
        assert read_tool.is_read_only
        assert read_tool.is_concurrency_safe
        assert write_tool.is_destructive
        assert bash_tool.metadata.requires_confirmation is True
        assert bash_tool.metadata.command_argument_names == ("command",)
        assert yaml_tool.metadata.path_argument_names == ("file_path",)
        assert yaml_tool.user_facing_name == "YAML 语法校验"

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


class TestGlobGrepReadTools:
    async def test_glob_finds_multiple_paths_by_pattern(self, tmp_path) -> None:
        (tmp_path / "site").mkdir()
        (tmp_path / "site" / "index.html").write_text("<h1>Site</h1>")
        (tmp_path / "demo").mkdir()
        (tmp_path / "demo" / "index.html").write_text("<h1>Demo</h1>")
        (tmp_path / "notes.txt").write_text("skip")
        tool = GlobTool(workspace_root=tmp_path)

        result = await tool.execute(pattern="**/*.html", limit=10)

        assert "匹配总数: 2" in result
        assert "`demo/index.html`" in result
        assert "`site/index.html`" in result
        assert "notes.txt" not in result

    async def test_glob_rejects_parent_escape_pattern(self, tmp_path) -> None:
        tool = GlobTool(workspace_root=tmp_path)

        result = await tool.execute(pattern="../*.html")

        assert "pattern 必须是工作区内的相对 glob 模式" in result

    async def test_grep_searches_content_with_file_type_filter(self, tmp_path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("def run():\n    return 'Showcase'\n")
        (tmp_path / "src" / "app.txt").write_text("Showcase in text\n")
        tool = GrepTool(workspace_root=tmp_path)

        result = await tool.execute(pattern="showcase", path="src", file_type="py")

        assert "已搜索文件数: 1" in result
        assert "返回匹配数: 1" in result
        assert "`src/app.py`:2:" in result
        assert "app.txt" not in result

    async def test_grep_reports_invalid_regex(self, tmp_path) -> None:
        tool = GrepTool(workspace_root=tmp_path)

        result = await tool.execute(pattern="[", path=".")

        assert "正则表达式无效" in result

    async def test_read_alias_reads_complete_file_content(self, tmp_path) -> None:
        target = tmp_path / "README.md"
        target.write_text("hello\nworld\n")
        tool = ReadTool(workspace_root=tmp_path)

        result = await tool.execute(path="README.md")

        assert "hello" in result
        assert "world" in result
        assert str(target) in result


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
        assert "超过 1 秒未完成" in result

    async def test_command_failure(self, bash_tool: BashRunTool) -> None:
        result = await bash_tool.execute(command="exit 1")
        assert "exit code: 1" in result

    async def test_large_output_keeps_full_log_and_tail_marker(self, tmp_path) -> None:
        output_dir = tmp_path / "shell-output"
        tool = BashRunTool(workspace_root=tmp_path, output_dir=output_dir)
        script = "import sys; sys.stdout.write('H' * 60000 + 'TAIL_MARKER')"

        result = await tool.execute(command=_python_command(script))

        assert "TAIL_MARKER" in result
        assert "完整输出:" in result
        log_path = _output_path_from_result(result)
        assert log_path.read_text(encoding="utf-8").endswith("TAIL_MARKER")
        assert log_path.stat().st_size == 60011

    async def test_timeout_preserves_output_written_before_termination(self, tmp_path) -> None:
        tool = BashRunTool(workspace_root=tmp_path, output_dir=tmp_path / "shell-output")
        script = (
            "import sys,time; "
            "sys.stdout.write('BEFORE_TIMEOUT\\n'); sys.stdout.flush(); time.sleep(10)"
        )

        result = await tool.execute(command=_python_command(script), timeout=1)

        assert "BEFORE_TIMEOUT" in result
        assert result.startswith("Shell 命令已超时并终止。")
        assert "超过 1 秒未完成" in result
        assert "exit code" not in result

    async def test_explicit_cwd_can_run_outside_workspace_after_permission(self, tmp_path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        tool = BashRunTool(workspace_root=workspace, output_dir=tmp_path / "shell-output")

        result = await tool.execute(command="pwd", cwd=str(tmp_path))

        assert f"工作目录: {tmp_path}" in result
        assert "工作目录必须位于工作区内" not in result

    async def test_rejects_non_positive_timeout(self, tmp_path) -> None:
        tool = BashRunTool(workspace_root=tmp_path, output_dir=tmp_path / "shell-output")

        result = await tool.execute(command="printf should-not-run", timeout=0)

        assert "超时时间必须是正整数" in result
        assert "should-not-run" not in result

    async def test_reports_output_log_allocation_failure(self, tmp_path) -> None:
        output_dir = tmp_path / "not-a-directory"
        output_dir.write_text("occupied", encoding="utf-8")
        tool = BashRunTool(workspace_root=tmp_path, output_dir=output_dir)

        result = await tool.execute(command="printf should-not-run")

        assert "无法创建 Shell 输出日志" in result
        assert "should-not-run" not in result

    async def test_summary_failure_preserves_log_path(self, tmp_path, monkeypatch) -> None:
        output_dir = tmp_path / "shell-output"
        tool = BashRunTool(workspace_root=tmp_path, output_dir=output_dir)

        def fail_to_summarize(artifact) -> None:
            artifact.stream.flush()
            artifact.stream.close()
            raise OSError("simulated read failure")

        monkeypatch.setattr(tool._output_store, "summarize", fail_to_summarize)

        result = await tool.execute(command="printf diagnostic-evidence")

        logs = list(output_dir.glob("shell-*.log"))
        assert "输出日志读取失败" in result
        assert len(logs) == 1
        assert str(logs[0]) in result
        assert logs[0].read_text(encoding="utf-8") == "diagnostic-evidence"

    async def test_nonzero_exit_keeps_command_output(self, tmp_path) -> None:
        tool = BashRunTool(workspace_root=tmp_path, output_dir=tmp_path / "shell-output")

        result = await tool.execute(command="printf failure-detail; exit 7")

        assert "failure-detail" in result
        assert "exit code: 7" in result

    async def test_stdout_and_stderr_keep_emission_order(self, tmp_path) -> None:
        tool = BashRunTool(workspace_root=tmp_path, output_dir=tmp_path / "shell-output")
        script = (
            "import sys; "
            "sys.stdout.write('OUT_ONE\\n'); sys.stdout.flush(); "
            "sys.stderr.write('ERR_TWO\\n'); sys.stderr.flush(); "
            "sys.stdout.write('OUT_THREE\\n'); sys.stdout.flush()"
        )

        result = await tool.execute(command=_python_command(script))

        assert result.index("OUT_ONE") < result.index("ERR_TWO") < result.index("OUT_THREE")

    async def test_default_cwd_uses_workspace_root(self, tmp_path) -> None:
        tool = BashRunTool(workspace_root=tmp_path)

        result = await tool.execute(command="pwd")

        assert f"工作目录: {tmp_path}" in result
        assert str(tmp_path) in result

    @pytest.mark.parametrize(
        "command",
        [
            "python -m http.server 8080 &",
            "nohup python -m http.server 8080",
            "python -m http.server 8080; disown",
        ],
    )
    async def test_background_shell_forms_use_background_runner(
        self,
        bash_tool: BashRunTool,
        command: str,
    ) -> None:
        result = await bash_tool.execute(command=command)

        assert "后台 shell 写法" in result
        assert "background_run" in result


class TestYamlValidateTool:
    async def test_metadata_marks_file_path_as_sandboxed_path_arg(self) -> None:
        tool = YamlValidateTool()

        assert tool.metadata.read_only
        assert tool.metadata.path_argument_names == ("file_path",)


def _python_command(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _output_path_from_result(result: str) -> Path:
    prefix = "- 完整输出: "
    line = next(item for item in result.splitlines() if item.startswith(prefix))
    return Path(line.removeprefix(prefix).strip())
