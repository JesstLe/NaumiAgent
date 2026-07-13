"""内置工具：文件读写、编辑、命令执行."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from naumi_agent.runtime.shell import create_shell_process, terminate_process_tree
from naumi_agent.runtime.shell_output import (
    ShellOutputArtifact,
    ShellOutputStore,
    ShellOutputSummary,
)
from naumi_agent.tools.base import Tool, ToolMetadata


def _resolve_workspace_path(path: str, workspace_root: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    return candidate.resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _relative_workspace_path(path: Path, workspace_root: Path) -> str:
    return path.relative_to(workspace_root).as_posix()


def _has_ignored_path_part(path: Path, workspace_root: Path, *, include_hidden: bool) -> bool:
    try:
        parts = path.relative_to(workspace_root).parts
    except ValueError:
        return True
    for part in parts[:-1]:
        if part in _FIND_EXCLUDED_DIRS:
            return True
        if not include_hidden and part.startswith("."):
            return True
    if parts and not include_hidden and parts[-1].startswith("."):
        return True
    return False


def _looks_like_background_shell(command: str) -> bool:
    """Detect shell backgrounding that would bypass BackgroundRunner tracking."""
    stripped = command.strip()
    if not stripped:
        return False
    if re.search(r"(^|[;&|]\s*)nohup\s+", stripped):
        return True
    if re.search(r"(^|[;&|]\s*)disown(\s|$)", stripped):
        return True
    return stripped.endswith("&")


_FIND_EXCLUDED_DIRS = {
    ".cache",
    ".git",
    ".hg",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "venv",
}

def _normalize_extensions(extensions: Any) -> set[str]:
    if extensions is None:
        return set()
    if isinstance(extensions, str):
        raw_items = re.split(r"[\s,]+", extensions)
    elif isinstance(extensions, list):
        raw_items = [str(item) for item in extensions]
    else:
        raw_items = [str(extensions)]
    normalized = set()
    for item in raw_items:
        value = item.strip().lower()
        if not value:
            continue
        normalized.add(value if value.startswith(".") else f".{value}")
    return normalized


class GlobTool(Tool):
    """Find files by glob pattern under the workspace."""

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        root = Path.cwd() if workspace_root is None else Path(workspace_root)
        self._workspace_root = root.expanduser().resolve()

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return "按 glob 模式搜索工作区文件路径，例如 `src/**/*.py` 或 `**/*.html`。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            path_argument_names=("directory",),
            user_facing_name="Glob 文件路径搜索",
            search_hint="glob files paths pattern workspace find list",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "glob 模式，例如 `src/**/*.py`、`**/*.html`",
                },
                "directory": {
                    "type": "string",
                    "description": "工作区内搜索目录，默认整个工作区",
                    "default": ".",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少条路径，默认 100，最大 500",
                    "default": 100,
                },
                "include_hidden": {
                    "type": "boolean",
                    "description": "是否包含隐藏文件/目录，默认 false",
                    "default": False,
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        *,
        pattern: str,
        directory: str = ".",
        limit: int = 100,
        include_hidden: bool = False,
        **kwargs: Any,
    ) -> str:
        normalized_pattern = str(pattern or "").strip()
        if not normalized_pattern:
            return "Error: pattern 不能为空。"
        if Path(normalized_pattern).is_absolute() or ".." in Path(normalized_pattern).parts:
            return "Error: pattern 必须是工作区内的相对 glob 模式。"
        search_root = _resolve_workspace_path(directory or ".", self._workspace_root)
        if not search_root.is_dir():
            return f"Error: 搜索目录不存在: {directory} (resolved: {search_root})"
        if not _is_relative_to(search_root, self._workspace_root):
            return f"Error: 搜索目录必须位于工作区内: {search_root}"

        safe_limit = max(1, min(int(limit or 100), 500))
        matches: list[str] = []
        for path in search_root.glob(normalized_pattern):
            resolved = path.resolve()
            if not resolved.is_file():
                continue
            if not _is_relative_to(resolved, self._workspace_root):
                continue
            if _has_ignored_path_part(
                resolved, self._workspace_root, include_hidden=include_hidden,
            ):
                continue
            matches.append(_relative_workspace_path(resolved, self._workspace_root))

        matches = sorted(set(matches), key=str.lower)
        shown = matches[:safe_limit]
        lines = [
            "Glob 文件路径搜索结果",
            f"- 工作区: {self._workspace_root}",
            f"- 搜索目录: {search_root}",
            f"- 模式: {normalized_pattern}",
            f"- 匹配总数: {len(matches)}",
            f"- 显示数量: {len(shown)}",
        ]
        if len(matches) > len(shown):
            lines.append(f"- 还有 {len(matches) - len(shown)} 个匹配未显示，请提高 limit。")
        if not shown:
            lines.append("未找到匹配文件。")
            return "\n".join(lines)
        lines.append("")
        lines.append("候选文件:")
        lines.extend(f"{idx}. `{path}`" for idx, path in enumerate(shown, start=1))
        return "\n".join(lines)


class GrepTool(Tool):
    """Search file contents with regex or literal matching."""

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        root = Path.cwd() if workspace_root is None else Path(workspace_root)
        self._workspace_root = root.expanduser().resolve()

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return "按内容搜索文件，支持正则/字面量、指定目录、glob 和文件类型过滤。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            path_argument_names=("path",),
            user_facing_name="Grep 内容搜索",
            search_hint="grep search content regex literal file type workspace",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "要搜索的正则或字面量文本",
                },
                "path": {
                    "type": "string",
                    "description": "工作区内文件或目录，默认整个工作区",
                    "default": ".",
                },
                "glob": {
                    "type": "string",
                    "description": "可选文件路径 glob 过滤，例如 `**/*.py`",
                },
                "file_type": {
                    "type": "string",
                    "description": "可选扩展名过滤，例如 py、html、md",
                },
                "literal": {
                    "type": "boolean",
                    "description": "按字面量搜索而不是正则，默认 false",
                    "default": False,
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "是否大小写敏感，默认 false",
                    "default": False,
                },
                "max_matches": {
                    "type": "integer",
                    "description": "最多返回多少条匹配，默认 50，最大 200",
                    "default": 50,
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        *,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        file_type: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_matches: int = 50,
        **kwargs: Any,
    ) -> str:
        raw_pattern = str(pattern or "")
        if not raw_pattern:
            return "Error: pattern 不能为空。"
        target = _resolve_workspace_path(path or ".", self._workspace_root)
        if not _is_relative_to(target, self._workspace_root):
            return f"Error: 搜索路径必须位于工作区内: {target}"
        if not target.exists():
            return f"Error: 搜索路径不存在: {path} (resolved: {target})"

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(re.escape(raw_pattern) if literal else raw_pattern, flags)
        except re.error as exc:
            return f"Error: 正则表达式无效: {exc}"

        extension_filter = _normalize_extensions(file_type)
        path_glob = str(glob or "").strip()
        safe_limit = max(1, min(int(max_matches or 50), 200))
        searched_files = 0
        skipped_large = 0
        matches: list[str] = []

        for candidate in self._iter_search_files(target, path_glob, extension_filter):
            if candidate.stat().st_size > 2_000_000:
                skipped_large += 1
                continue
            searched_files += 1
            try:
                lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel = _relative_workspace_path(candidate, self._workspace_root)
            for line_no, line in enumerate(lines, start=1):
                if regex.search(line):
                    matches.append(f"`{rel}`:{line_no}: {line.strip()}")
                    if len(matches) >= safe_limit:
                        break
            if len(matches) >= safe_limit:
                break

        lines = [
            "Grep 内容搜索结果",
            f"- 工作区: {self._workspace_root}",
            f"- 搜索路径: {target}",
            f"- pattern: {raw_pattern}",
            f"- 模式: {'字面量' if literal else '正则'}",
            f"- glob: {path_glob or '(不限)'}",
            f"- 文件类型: {', '.join(sorted(extension_filter)) if extension_filter else '(不限)'}",
            f"- 已搜索文件数: {searched_files}",
            f"- 跳过大文件数: {skipped_large}",
            f"- 返回匹配数: {len(matches)}",
        ]
        if len(matches) >= safe_limit:
            lines.append("- 结果达到上限，请缩小 path/glob/file_type 或提高 max_matches。")
        if not matches:
            lines.append("未找到内容匹配。")
            return "\n".join(lines)
        lines.append("")
        lines.append("匹配:")
        lines.extend(matches)
        return "\n".join(lines)

    def _iter_search_files(
        self,
        target: Path,
        path_glob: str,
        extension_filter: set[str],
    ) -> list[Path]:
        if target.is_file():
            candidates = [target]
        else:
            candidates = []
            for root, dirs, files in os.walk(target):
                root_path = Path(root)
                dirs[:] = [
                    dirname
                    for dirname in dirs
                    if dirname not in _FIND_EXCLUDED_DIRS and not dirname.startswith(".")
                ]
                candidates.extend(root_path / filename for filename in files)

        filtered = []
        for candidate in candidates:
            resolved = candidate.resolve()
            if not resolved.is_file():
                continue
            if not _is_relative_to(resolved, self._workspace_root):
                continue
            if _has_ignored_path_part(resolved, self._workspace_root, include_hidden=False):
                continue
            if extension_filter and resolved.suffix.lower() not in extension_filter:
                continue
            rel = _relative_workspace_path(resolved, self._workspace_root)
            if path_glob and not fnmatch.fnmatch(rel, path_glob):
                continue
            filtered.append(resolved)
        return sorted(
            filtered,
            key=lambda item: _relative_workspace_path(item, self._workspace_root),
        )


class FileReadTool(Tool):
    """读取文件内容."""

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        root = Path.cwd() if workspace_root is None else Path(workspace_root)
        self._workspace_root = root.expanduser().resolve()

    @property
    def name(self) -> str:
        return "file_read"

    @property
    def description(self) -> str:
        return "读取指定路径的文件内容。支持 offset 和 limit 参数读取部分内容。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="读取文件",
        )

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
        resolved = _resolve_workspace_path(path, self._workspace_root)
        if not resolved.is_file():
            return f"Error: File not found: {path} (resolved: {resolved})"

        try:
            with resolved.open(encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            total = len(lines)
            end = total if limit < 0 else min(offset + limit, total)
            selected = lines[offset:end]

            result = "".join(selected)
            header = f"📄 {resolved} ({total} 行"
            if offset > 0 or limit > 0:
                header += f"，显示第 {offset + 1}-{end} 行"
            header += ")\n"

            lang = FileWriteTool._guess_lang(path)
            return f"{header}\n```{lang}\n{result}```"
        except Exception as e:
            return f"Error reading file: {type(e).__name__}: {e}"


class ReadTool(FileReadTool):
    """Claude Code-style alias for reading a file."""

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "读取文件完整内容；也支持 offset 和 limit 读取局部内容。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="Read 读取文件",
            search_hint="read file content full offset limit",
        )


class FileWriteTool(Tool):
    """写入文件."""

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        root = Path.cwd() if workspace_root is None else Path(workspace_root)
        self._workspace_root = root.expanduser().resolve()

    @property
    def name(self) -> str:
        return "file_write"

    @property
    def description(self) -> str:
        return "将内容写入指定文件。如果文件不存在则创建，存在则覆盖。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            user_facing_name="写入文件",
        )

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
        resolved = _resolve_workspace_path(path, self._workspace_root)
        is_new = not resolved.is_file()

        try:
            old_content = ""
            if not is_new:
                with resolved.open(encoding="utf-8") as f:
                    old_content = f.read()

            resolved.parent.mkdir(parents=True, exist_ok=True)
            with resolved.open("w", encoding="utf-8") as f:
                f.write(content)

            lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

            if is_new:
                preview = self._preview_content(content, max_lines=15)
                return (
                    f"✅ 已创建 {resolved} ({lines} 行, {len(content)} 字符)\n\n"
                    f"```{self._guess_lang(path)}\n{preview}\n```"
                )

            if old_content == content:
                return f"ℹ️ {resolved} 内容未变化"

            diff = self._make_diff(old_content, content, str(resolved))
            return (
                f"✅ 已覆写 {resolved} ({lines} 行, {len(content)} 字符)\n\n"
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

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        root = Path.cwd() if workspace_root is None else Path(workspace_root)
        self._workspace_root = root.expanduser().resolve()

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
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            user_facing_name="编辑文件",
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
        resolved = _resolve_workspace_path(path, self._workspace_root)

        if not resolved.is_file():
            return f"Error: File not found: {path} (resolved: {resolved})"

        try:
            with resolved.open(encoding="utf-8") as f:
                content = f.read()

            count = content.count(old_text)
            if count == 0:
                return f"Error: old_text not found in {resolved}"
            if count > 1:
                return (
                    f"Error: old_text appears {count} times in {resolved}."
                    " Please provide more context to make it unique."
                )

            new_content = content.replace(old_text, new_text, 1)
            with resolved.open("w", encoding="utf-8") as f:
                f.write(new_content)

            diff = FileWriteTool._make_diff(content, new_content, str(resolved))
            return (
                f"✅ 已编辑 {resolved} (替换 1 处)\n\n{diff}"
            )
        except Exception as e:
            return f"Error editing file: {type(e).__name__}: {e}"


class YamlMicroVerifyTool(Tool):
    @property
    def name(self) -> str:
        return "yaml_micro_verify"

    @property
    def description(self) -> str:
        return (
            "语法级微验证：使用最小化 Python 3 命令做 YAML 加载测试，"
            "仅输出极简标记；若 Python 环境异常，则降级为 ruby -ryaml 验证"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            path_argument_names=("file_path",),
            user_facing_name="YAML 微验证",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to YAML file",
                }
            },
            "required": ["file_path"],
        }

    async def execute(self, *, file_path: str, **kwargs: Any) -> str:
        python_code = (
            "import sys, yaml; "
            "yaml.safe_load(open(sys.argv[1], encoding='utf-8')); "
            "print('OK')"
        )
        proc = await asyncio.create_subprocess_exec(
            "python3",
            "-c",
            python_code,
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if stdout.decode().strip() == "OK":
            return "YAML_SYNTAX_OK"

        proc = await asyncio.create_subprocess_exec(
            "ruby",
            "-ryaml",
            "-e",
            "YAML.load_file(ARGV[0]); puts 'OK'",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return "YAML_SYNTAX_OK" if stdout.decode().strip() == "OK" else "YAML_SYNTAX_FAIL"


class BashRunTool(Tool):
    """执行 shell 命令."""

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        *,
        output_dir: str | Path | None = None,
    ) -> None:
        root = Path.cwd() if workspace_root is None else Path(workspace_root)
        self._workspace_root = root.expanduser().resolve()
        resolved_output_dir = (
            Path(output_dir)
            if output_dir is not None
            else Path(tempfile.gettempdir()) / "naumi-agent-shell-output"
        )
        self._output_store = ShellOutputStore(resolved_output_dir)

    @property
    def output_dir(self) -> Path:
        return self._output_store.output_dir

    @property
    def name(self) -> str:
        return "bash_run"

    @property
    def description(self) -> str:
        return "在 shell 中执行命令并返回可恢复输出。支持超时设置，工作目录默认为工作区根目录。"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            requires_confirmation=True,
            path_argument_names=("cwd",),
            command_argument_names=("command",),
            user_facing_name="执行命令",
        )

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
                    "description": "工作目录，默认为工作区根目录",
                },
            },
            "required": ["command"],
        }

    async def execute(
        self, *, command: str, timeout: int = 30, cwd: str | None = None, **kwargs: Any
    ) -> str:
        proc: asyncio.subprocess.Process | None = None
        artifact: ShellOutputArtifact | None = None
        try:
            if not isinstance(command, str) or not command.strip():
                return "错误：Shell 命令不能为空"
            if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
                return "错误：超时时间必须是正整数（秒）"
            if _looks_like_background_shell(command):
                return (
                    "错误：检测到后台 shell 写法（如 `&`/`nohup`/`disown`）。"
                    "请改用 background_run，这样系统可以记录 PID、输出文件并在 cleanup 时回收进程。"
                )
            workdir = (
                _resolve_workspace_path(cwd, self._workspace_root)
                if cwd
                else self._workspace_root
            )
            if not workdir.is_dir():
                return f"错误：工作目录不存在: {workdir}"

            self._output_store.prune()
            try:
                artifact = self._output_store.allocate()
            except OSError as exc:
                return f"错误：无法创建 Shell 输出日志: {type(exc).__name__}: {exc}"

            proc = await create_shell_process(
                command,
                stdout=artifact.stream,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(workdir),
            )
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            try:
                summary = self._output_store.summarize(artifact)
            except OSError as exc:
                self._output_store.preserve(artifact)
                output_path = artifact.path
                artifact = None
                return (
                    "错误：Shell 命令已结束，但输出日志读取失败: "
                    f"{type(exc).__name__}: {exc}\n- 完整输出: {output_path}"
                )
            artifact = None
            self._output_store.prune()
            return _format_shell_result(
                summary,
                workdir=workdir,
                exit_code=proc.returncode,
            )
        except TimeoutError:
            if proc is not None:
                await terminate_process_tree(proc)
            if artifact is None:
                return f"错误：命令超过 {timeout} 秒未完成，已终止"
            try:
                summary = self._output_store.summarize(artifact)
                artifact = None
            except OSError as exc:
                self._output_store.preserve(artifact)
                output_path = artifact.path
                artifact = None
                return (
                    f"错误：命令超过 {timeout} 秒未完成，已终止；"
                    f"输出日志读取失败: {type(exc).__name__}: {exc}"
                    f"\n- 完整输出: {output_path}"
                )
            self._output_store.prune()
            return _format_shell_result(
                summary,
                workdir=workdir,
                exit_code=proc.returncode if proc is not None else None,
                timed_out_after=timeout,
            )
        except asyncio.CancelledError:
            if proc is not None:
                await terminate_process_tree(proc)
            if artifact is not None:
                self._output_store.discard(artifact)
            raise
        except Exception as e:
            if proc is not None and proc.returncode is None:
                await terminate_process_tree(proc)
            if artifact is not None:
                self._output_store.discard(artifact)
            return f"错误：Shell 命令执行失败: {type(e).__name__}: {e}"


def _format_shell_result(
    summary: ShellOutputSummary,
    *,
    workdir: Path,
    exit_code: int | None,
    timed_out_after: int | None = None,
) -> str:
    title = (
        "Shell 命令已超时并终止。"
        if timed_out_after is not None
        else "Shell 命令执行完成。"
    )
    lines = [title, f"工作目录: {workdir}"]
    if timed_out_after is not None:
        lines.append(f"状态: 命令超过 {timed_out_after} 秒未完成，已终止")
    if timed_out_after is None and exit_code is not None:
        lines.append(f"退出码: {exit_code}")
    lines.append(f"输出: {summary.size_bytes} 字节")

    if summary.is_large:
        lines.append(f"- 完整输出: {summary.path}")
        lines.extend(
            [
                "",
                "输出摘要（开头）:",
                summary.head,
                "",
                f"...（中间省略 {summary.omitted_bytes} 字节，完整内容见上方日志）...",
                "",
                "输出摘要（结尾）:",
                summary.tail,
            ]
        )
    else:
        lines.extend(["", summary.content or "（无输出）"])
    if timed_out_after is None and exit_code not in (None, 0):
        lines.append(f"[exit code: {exit_code}]")
    return "\n".join(lines)


def create_builtin_tools(
    workspace_root: str | Path | None = None,
    *,
    shell_output_dir: str | Path | None = None,
) -> list[Tool]:
    """创建所有内置工具实例."""
    return [
        GlobTool(workspace_root),
        GrepTool(workspace_root),
        ReadTool(workspace_root),
        FileReadTool(workspace_root),
        FileWriteTool(workspace_root),
        FileEditTool(workspace_root),
        YamlMicroVerifyTool(),
        BashRunTool(workspace_root, output_dir=shell_output_dir),
        YamlValidateTool(),
    ]


class YamlValidateTool(Tool):
    @property
    def name(self):
        return 'yaml_validate'

    @property
    def description(self):
        return '使用 Python 仅做只读 YAML 语法校验，确保插入注释后文件仍能正常解析'

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            path_argument_names=("file_path",),
            user_facing_name="YAML 语法校验",
        )

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
            with open(file_path, encoding=encoding) as f:
                yaml.safe_load(f)
            return f'YAML 语法校验通过：{file_path}'
        except ImportError:
            return '错误：未安装 PyYAML，请执行 pip install pyyaml'
        except Exception as e:
            return f'YAML 语法校验失败：{type(e).__name__}: {e}'
