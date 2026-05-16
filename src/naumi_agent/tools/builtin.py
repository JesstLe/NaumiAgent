"""内置工具：文件读写、编辑、命令执行."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from naumi_agent.tools.base import Tool


class FileReadTool(Tool):
    """读取文件内容."""

    @property
    def name(self) -> str:
        return "file_read"

    @property
    def description(self) -> str:
        return "读取指定路径的文件内容。支持 offset 和 limit 参数读取部分内容。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要读取的文件路径",
                },
                "offset": {
                    "type": "integer",
                    "description": "起始行号（从 0 开始），默认 0",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "最多读取的行数，默认读取全部",
                    "default": -1,
                },
            },
            "required": ["path"],
        }

    async def execute(self, *, path: str, offset: int = 0, limit: int = -1, **kwargs: Any) -> str:
        resolved = os.path.expanduser(path)
        if not os.path.isfile(resolved):
            return f"Error: File not found: {path}"

        try:
            with open(resolved, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            total = len(lines)
            end = total if limit < 0 else min(offset + limit, total)
            selected = lines[offset:end]

            result = "".join(selected)
            header = f"📄 {path} ({total} 行"
            if offset > 0 or limit > 0:
                header += f"，显示第 {offset + 1}-{end} 行"
            header += ")\n"

            lang = FileWriteTool._guess_lang(path)
            return f"{header}\n```{lang}\n{result}```"
        except Exception as e:
            return f"Error reading file: {type(e).__name__}: {e}"


class FileWriteTool(Tool):
    """写入文件."""

    @property
    def name(self) -> str:
        return "file_write"

    @property
    def description(self) -> str:
        return "将内容写入指定文件。如果文件不存在则创建，存在则覆盖。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要写入的文件路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的内容",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, *, path: str, content: str, **kwargs: Any) -> str:
        resolved = os.path.expanduser(path)
        is_new = not os.path.isfile(resolved)

        try:
            old_content = ""
            if not is_new:
                with open(resolved, encoding="utf-8") as f:
                    old_content = f.read()

            os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(content)

            lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

            if is_new:
                preview = self._preview_content(content, max_lines=15)
                return (
                    f"✅ 已创建 {path} ({lines} 行, {len(content)} 字符)\n\n"
                    f"```{self._guess_lang(path)}\n{preview}\n```"
                )

            if old_content == content:
                return f"ℹ️ {path} 内容未变化"

            diff = self._make_diff(old_content, content, path)
            return (
                f"✅ 已覆写 {path} ({lines} 行, {len(content)} 字符)\n\n"
                f"{diff}"
            )
        except Exception as e:
            return f"Error writing file: {type(e).__name__}: {e}"

    @staticmethod
    def _preview_content(content: str, max_lines: int = 15) -> str:
        lines = content.splitlines()
        if len(lines) <= max_lines:
            return content
        shown = "\n".join(lines[:max_lines])
        return f"{shown}\n... ({len(lines) - max_lines} more lines)"

    @staticmethod
    def _guess_lang(path: str) -> str:
        ext = path.rsplit(".", 1)[-1] if "." in path else ""
        return {
            "py": "python", "js": "javascript", "ts": "typescript",
            "yaml": "yaml", "yml": "yaml", "json": "json",
            "md": "markdown", "toml": "toml", "rs": "rust",
            "go": "go", "sh": "bash", "sql": "sql",
        }.get(ext, "")

    @staticmethod
    def _make_diff(old: str, new: str, path: str) -> str:
        import difflib

        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"{path} (before)",
            tofile=f"{path} (after)",
            lineterm="",
        )
        diff_lines = list(diff)
        if not diff_lines:
            return ""
        if len(diff_lines) > 60:
            total_count = len(
                list(difflib.unified_diff(
                    old_lines, new_lines, lineterm="",
                ))
            )
            diff_lines = diff_lines[:60]
            diff_lines.append(
                f"... ({total_count} total diff lines)\n"
            )
        return "```diff\n" + "\n".join(diff_lines) + "\n```"


class FileEditTool(Tool):
    """编辑文件 — 搜索替换."""

    @property
    def name(self) -> str:
        return "file_edit"

    @property
    def description(self) -> str:
        return (
            "对文件进行精确的搜索替换编辑。"
            "提供 old_text 和 new_text，将文件中首次出现的 old_text 替换为 new_text。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要编辑的文件路径",
                },
                "old_text": {
                    "type": "string",
                    "description": "要被替换的原始文本",
                },
                "new_text": {
                    "type": "string",
                    "description": "替换后的新文本",
                },
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(self, *, path: str, old_text: str, new_text: str, **kwargs: Any) -> str:
        resolved = os.path.expanduser(path)

        if not os.path.isfile(resolved):
            return f"Error: File not found: {path}"

        try:
            with open(resolved, encoding="utf-8") as f:
                content = f.read()

            count = content.count(old_text)
            if count == 0:
                return f"Error: old_text not found in {path}"
            if count > 1:
                return (
                    f"Error: old_text appears {count} times in {path}."
                    " Please provide more context to make it unique."
                )

            new_content = content.replace(old_text, new_text, 1)
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(new_content)

            diff = FileWriteTool._make_diff(content, new_content, path)
            return (
                f"✅ 已编辑 {path} (替换 1 处)\n\n{diff}"
            )
        except Exception as e:
            return f"Error editing file: {type(e).__name__}: {e}"


class YamlMicroVerifyTool(Tool):
    @property
    def name(self): return 'yaml_micro_verify'
    @property
    def description(self): return '语法级微验证：绕过可能超时的 yaml_validate，使用最小化 Python 3 命令仅做加载测试并输出极简标记，杜绝长输出与 Lint 干扰；若环境 Python 仍异常，则自动降级为系统常备的 `ruby -ryaml` 做等价验证'
    @property
    def parameters_schema(self): return {'type': 'object', 'properties': {'file_path': {'type': 'string', 'description': 'Path to YAML file'}}, 'required': ['file_path']}
    async def execute(self, *, file_path, **kwargs):
        cmd = f"python3 -c \"import yaml,sys; yaml.safe_load(open(sys.argv[1])); print('OK')\" \"{file_path}\" 2>/dev/null || ruby -ryaml -e \"YAML.load_file(ARGV[0]); puts 'OK'\" \"{file_path}\" 2>/dev/null || echo FAIL"
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        return 'YAML_SYNTAX_OK' if stdout.decode().strip() == 'OK' else 'YAML_SYNTAX_FAIL'

class BashRunTool(Tool):
    """执行 shell 命令."""

    @property
    def name(self) -> str:
        return "bash_run"

    @property
    def description(self) -> str:
        return "在 shell 中执行命令并返回输出。支持超时设置。工作目录默认为项目根目录。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时时间（秒），默认 30",
                    "default": 30,
                },
                "cwd": {
                    "type": "string",
                    "description": "工作目录，默认为当前目录",
                },
            },
            "required": ["command"],
        }

    async def execute(
        self, *, command: str, timeout: int = 30, cwd: str | None = None, **kwargs: Any
    ) -> str:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

            output_parts = []
            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))
            if stderr:
                output_parts.append(f"[stderr]\n{stderr.decode('utf-8', errors='replace')}")

            output = "\n".join(output_parts) if output_parts else "(no output)"

            if proc.returncode != 0:
                output += f"\n[exit code: {proc.returncode}]"

            # 截断过长输出
            if len(output) > 50000:
                output = output[:50000] + f"\n... (truncated, {len(output)} total chars)"

            return output
        except TimeoutError:
            return f"Error: Command timed out after {timeout}s"
        except Exception as e:
            return f"Error executing command: {type(e).__name__}: {e}"


def create_builtin_tools() -> list[Tool]:
    """创建所有内置工具实例."""
    return [FileReadTool(), FileWriteTool(), FileEditTool(), YamlMicroVerifyTool(), BashRunTool(), YamlValidateTool()]

class YamlValidateTool(Tool):
    @property
    def name(self):
        return 'yaml_validate'

    @property
    def description(self):
        return '使用 Python 仅做只读 YAML 语法校验，确保插入注释后文件仍能正常解析'

    @property
    def parameters_schema(self):
        return {
            'type': 'object',
            'properties': {
                'file_path': {
                    'type': 'string',
                    'description': '待校验的 YAML 文件路径'
                },
                'encoding': {
                    'type': 'string',
                    'description': '文件编码，默认 utf-8',
                    'default': 'utf-8'
                }
            },
            'required': ['file_path']
        }

    async def execute(self, *, file_path, encoding='utf-8', **kwargs):
        import os
        if not os.path.isfile(file_path):
            return f'错误：文件不存在 {file_path}'
        try:
            import yaml
            with open(file_path, 'r', encoding=encoding) as f:
                yaml.safe_load(f)
            return f'YAML 语法校验通过：{file_path}'
        except ImportError:
            return '错误：未安装 PyYAML，请执行 pip install pyyaml'
        except Exception as e:
            return f'YAML 语法校验失败：{type(e).__name__}: {e}'

