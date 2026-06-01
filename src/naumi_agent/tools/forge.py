"""工具锻造 — Agent 自主设计、生成、注册新工具."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import textwrap
from hashlib import sha1
from pathlib import Path
from typing import Any

from naumi_agent.tools.base import Tool, ToolMetadata

logger = logging.getLogger(__name__)

_GENERATED_DIR = Path(__file__).resolve().parent / "generated"
MAX_FORGE_DESCRIPTION_CHARS = 4_000
MAX_FORGE_CODE_CHARS = 200_000
_SAFE_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_TOOL_GENERATION_SYSTEM = textwrap.dedent("""\
    你是 Agent 工具锻造系统。根据用户描述生成完整的 Python 工具代码。

    工具必须继承 naumi_agent.tools.base.Tool 基类并实现以下接口:
    - name (property) -> str: 工具名称，小写+下划线
    - description (property) -> str: 工具描述
    - parameters_schema (property) -> dict: JSON Schema 格式参数定义
    - async execute(**kwargs) -> str: 执行逻辑，返回字符串

    要求:
    1. 必须继承 Tool 类
    2. execute 方法必须用 **kwargs 接收参数，用命名参数解包
    3. 包含所有必要的 import 语句
    4. 使用中文描述和错误提示
    5. 代码必须自包含、可直接运行
    6. 只输出 Python 代码，不要 markdown 代码块标记或其他说明
    7. 错误处理要完善，不要抛出未捕获的异常
""")


def get_generated_dir() -> Path:
    """Return the directory for generated tools."""
    _GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    return _GENERATED_DIR


def _to_class_name(tool_name: str) -> str:
    """Convert snake_case tool name to PascalCase class name."""
    parts = tool_name.replace("-", "_").split("_")
    return "".join(p.capitalize() for p in parts) + "Tool"


def _sanitize_tool_name(raw: str, fallback: str = "custom_tool") -> str:
    """Convert user text into a safe snake_case tool name."""
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9]*", raw.replace("-", "_"))
    if not words:
        digest = sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:8]
        return f"{fallback}_{digest}"
    name = "_".join(word.lower() for word in words[:4])
    name = re.sub(r"_+", "_", name).strip("_")
    return name or fallback


def _normalize_tool_name(raw: Any, *, field_name: str = "tool_name") -> str:
    """Normalize a public tool name into a safe generated file stem."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field_name} 不能为空，且必须是字符串。")

    normalized = raw.strip().replace("-", "_")
    if not _SAFE_TOOL_NAME_RE.fullmatch(normalized):
        raise ValueError(
            f"{field_name} 只能包含小写字母、数字和下划线，"
            "长度最多 64 个字符，且必须以小写字母开头。"
        )
    return normalized


def _normalize_forge_inputs(
    description: Any,
    tool_name: Any | None,
    llm_output: Any | None,
) -> tuple[str, str | None, str | None]:
    """Validate public forge inputs before any code generation or file write."""
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description 不能为空，且必须是字符串。")
    description = description.strip()
    if len(description) > MAX_FORGE_DESCRIPTION_CHARS:
        raise ValueError(
            "description 过长，当前上限为 "
            f"{MAX_FORGE_DESCRIPTION_CHARS} 个字符。"
        )

    safe_tool_name = (
        _normalize_tool_name(tool_name)
        if tool_name is not None
        else None
    )

    if llm_output is None:
        return description, safe_tool_name, None
    if not isinstance(llm_output, str) or not llm_output.strip():
        raise ValueError("llm_output 必须是非空字符串，或直接省略以使用确定性脚手架。")
    if len(llm_output) > MAX_FORGE_CODE_CHARS:
        raise ValueError(
            "llm_output 过大，当前上限为 "
            f"{MAX_FORGE_CODE_CHARS} 个字符。"
        )
    return description, safe_tool_name, llm_output


def _description_keywords(description: str, limit: int = 12) -> list[str]:
    """Extract stable keywords used by the deterministic scaffold."""
    words = re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", description)
    seen: set[str] = set()
    keywords: list[str] = []
    for word in words:
        normalized = word.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        keywords.append(word[:40])
        if len(keywords) >= limit:
            break
    return keywords


def build_deterministic_tool_code(description: str, tool_name: str | None = None) -> str:
    """Build a runnable generated tool without requiring an LLM."""
    safe_name = _sanitize_tool_name(tool_name or description)
    class_name = _to_class_name(safe_name)
    keywords = _description_keywords(description)
    return textwrap.dedent(f'''\
        """Generated deterministic tool: {safe_name}."""

        from __future__ import annotations

        import json
        import re
        from typing import Any

        from naumi_agent.tools.base import Tool


        class {class_name}(Tool):
            """Deterministic generated tool scaffold."""

            @property
            def name(self) -> str:
                return {safe_name!r}

            @property
            def description(self) -> str:
                return {description!r}

            @property
            def parameters_schema(self) -> dict[str, Any]:
                return {{
                    "type": "object",
                    "properties": {{
                        "input_text": {{
                            "type": "string",
                            "description": "要分析的文本或任务材料",
                            "default": "",
                        }},
                        "mode": {{
                            "type": "string",
                            "description": "输出模式：summary 或 json",
                            "default": "summary",
                        }},
                    }},
                    "required": [],
                }}

            async def execute(
                self,
                *,
                input_text: str = "",
                mode: str = "summary",
                **kwargs: Any,
            ) -> str:
                text = input_text or "\\n".join(
                    str(value) for value in kwargs.values() if value is not None
                )
                keywords = {keywords!r}
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                words = re.findall(r"[\\w\\u4e00-\\u9fff]+", text)
                hits = {{
                    keyword: len(re.findall(re.escape(keyword), text, re.IGNORECASE))
                    for keyword in keywords
                }}
                payload = {{
                    "tool": self.name,
                    "description": self.description,
                    "chars": len(text),
                    "lines": len(lines),
                    "words": len(words),
                    "keyword_hits": hits,
                    "empty": not bool(text.strip()),
                }}
                if mode == "json":
                    return json.dumps(payload, ensure_ascii=False, indent=2)
                hit_lines = [
                    f"- {{keyword}}: {{count}}"
                    for keyword, count in hits.items()
                    if count
                ] or ["- 未命中描述关键词"]
                return "\\n".join([
                    f"## {{self.name}} 执行结果",
                    f"- 字符数: {{payload['chars']}}",
                    f"- 非空行数: {{payload['lines']}}",
                    f"- 词元数: {{payload['words']}}",
                    "## 关键词命中",
                    *hit_lines,
                ])
        ''')


def _extract_python_code(llm_output: str) -> str:
    """Extract Python code from LLM output, stripping markdown fences."""
    # Try to find ```python ... ``` block
    match = re.search(
        r"```(?:python)?\s*\n(.*?)\n\s*```",
        llm_output,
        re.DOTALL,
    )
    if match:
        return match.group(1).strip()

    # If no fences, assume the whole output is code
    lines = llm_output.strip().split("\n")
    # Remove leading/trailing empty lines
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines)


def _validate_tool_code(code: str) -> tuple[bool, str]:
    """Validate generated tool code.

    Checks:
    1. Syntax (compile)
    2. Contains a class inheriting from Tool
    3. Has required methods (name, description, parameters_schema, execute)

    Returns:
        (is_valid, error_message)
    """
    # 1. Syntax check
    try:
        compile(code, "<generated>", "exec")
    except SyntaxError as e:
        return False, f"语法错误: 第 {e.lineno} 行: {e.msg}"

    # 2. Check class definition
    class_match = re.search(
        r"class\s+(\w+)\s*\(\s*Tool\s*\)", code
    )
    if not class_match:
        return False, "代码未包含继承自 Tool 的类定义"

    class_name = class_match.group(1)

    # 3. Check required interface elements
    required_patterns = {
        "name property": r"@property\s+def\s+name\s*\(",
        "description property": r"@property\s+def\s+description\s*\(",
        "parameters_schema property": r"@property\s+def\s+parameters_schema\s*\(",
        "execute method": r"async\s+def\s+execute\s*\(",
    }

    missing = []
    for label, pattern in required_patterns.items():
        if not re.search(pattern, code):
            missing.append(label)

    if missing:
        return False, f"缺少必要接口: {', '.join(missing)}"

    # 4. Import check in subprocess
    try:
        result = subprocess.run(
            [
                sys.executable, "-c",
                f"compile({code!r}, '<generated>', 'exec')",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, f"编译检查失败: {result.stderr[:500]}"
    except subprocess.TimeoutExpired:
        return False, "编译检查超时"

    return True, class_name


def _import_test(code: str, class_name: str) -> tuple[bool, str]:
    """Test that the tool can be imported and instantiated.

    Returns:
        (success, error_message)
    """
    import tempfile

    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", encoding="utf-8", delete=False,
    ) as tmp:
        tmp.write(code)
        tmp_path = tmp.name

    try:
        # Import the module from temp file
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_forge_test_module", tmp_path
        )
        if spec is None or spec.loader is None:
            return False, "无法创建模块规格"

        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Find the Tool subclass
        tool_class = getattr(mod, class_name, None)
        if tool_class is None:
            return False, f"找不到类 {class_name}"

        # Try to instantiate
        tool_instance = tool_class()

        # Verify interface
        if not hasattr(tool_instance, "name"):
            return False, "实例缺少 name 属性"
        if not hasattr(tool_instance, "description"):
            return False, "实例缺少 description 属性"
        if not hasattr(tool_instance, "parameters_schema"):
            return False, "实例缺少 parameters_schema 属性"
        if not hasattr(tool_instance, "execute"):
            return False, "实例缺少 execute 方法"

        try:
            safe_name = _normalize_tool_name(
                tool_instance.name,
                field_name="生成工具实例名",
            )
        except ValueError as e:
            return False, str(e)

        return True, safe_name
    except Exception as e:
        return False, str(e)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def save_tool(tool_name: str, code: str) -> Path:
    """Save generated tool code to disk.

    Args:
        tool_name: Tool name (snake_case).
        code: Complete Python source code.

    Returns:
        Path to the saved file.
    """
    safe_name = _normalize_tool_name(tool_name)
    generated_dir = get_generated_dir().resolve()
    file_name = f"{safe_name}.py"
    file_path = (generated_dir / file_name).resolve()
    if file_path.parent != generated_dir:
        raise ValueError("工具文件路径越界，已拒绝写入。")
    file_path.write_text(code, encoding="utf-8")
    return file_path


def load_generated_tool(tool_name: str) -> Tool | None:
    """Load a generated tool by name.

    Args:
        tool_name: Tool name (snake_case).

    Returns:
        Tool instance or None if not found.
    """
    try:
        safe_name = _normalize_tool_name(tool_name)
    except ValueError:
        return None
    file_name = f"{safe_name}.py"
    file_path = get_generated_dir() / file_name

    if not file_path.exists():
        return None

    try:
        # Find the Tool subclass in the file
        code = file_path.read_text(encoding="utf-8")
        class_match = re.search(
            r"class\s+(\w+)\s*\(\s*Tool\s*\)", code
        )
        if not class_match:
            return None

        class_name = class_match.group(1)

        import importlib.util

        spec = importlib.util.spec_from_file_location(
            f"naumi_agent.tools.generated.{safe_name}",
            str(file_path),
        )
        if spec is None or spec.loader is None:
            return None

        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        tool_class = getattr(mod, class_name, None)
        if tool_class is None:
            return None

        tool = tool_class()
        _normalize_tool_name(tool.name, field_name="生成工具实例名")
        return tool
    except Exception as e:
        logger.warning("Failed to load generated tool %s: %s", tool_name, e)
        return None


def list_generated_tools() -> list[dict[str, str]]:
    """List all generated tools with metadata.

    Returns:
        List of dicts with name, description, file_path.
    """
    generated_dir = get_generated_dir()
    tools = []

    for py_file in sorted(generated_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue

        tool_name = py_file.stem
        try:
            code = py_file.read_text(encoding="utf-8")

            # Extract description from the class
            desc_match = re.search(
                r'@property\s+def\s+description\s*\([^)]*\)\s*->\s*str:\s*return\s+"([^"]*)"',
                code,
            )
            if not desc_match:
                desc_match = re.search(
                    r'@property\s+def\s+description\s*\([^)]*\)\s*->\s*str:\s*return\s+f?"([^"]*)"',
                    code,
                )

            description = desc_match.group(1) if desc_match else "(无描述)"
            tools.append({
                "name": tool_name,
                "description": description[:100],
                "path": str(py_file),
            })
        except Exception:
            tools.append({
                "name": tool_name,
                "description": "(读取失败)",
                "path": str(py_file),
            })

    return tools


def remove_generated_tool(tool_name: str) -> bool:
    """Remove a generated tool file.

    Returns:
        True if the file was removed.
    """
    try:
        safe_name = _normalize_tool_name(tool_name)
    except ValueError:
        return False
    file_name = f"{safe_name}.py"
    file_path = get_generated_dir() / file_name

    if not file_path.exists():
        return False

    file_path.unlink()
    return True


def load_all_generated_tools() -> list[Tool]:
    """Load all generated tools from the generated/ directory.

    Returns:
        List of Tool instances.
    """
    tools = []
    generated_dir = get_generated_dir()

    for py_file in sorted(generated_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue

        tool_name = py_file.stem
        tool = load_generated_tool(tool_name)
        if tool is not None:
            tools.append(tool)

    return tools


def forge_tool(
    description: str,
    tool_name: str | None = None,
    llm_output: str | None = None,
) -> dict[str, Any]:
    """Forge a new tool from a description.

    Pipeline:
    1. Extract/generate tool code
    2. Validate syntax and interface
    3. Import test
    4. Save to disk
    5. Return result

    Args:
        description: What the tool should do.
        tool_name: Optional explicit tool name.
        llm_output: Pre-generated code. When omitted, a deterministic scaffold
            is generated locally.

    Returns:
        Dict with status, tool_name, validation results.
    """
    try:
        description, tool_name, llm_output = _normalize_forge_inputs(
            description,
            tool_name,
            llm_output,
        )
    except ValueError as e:
        return {
            "status": "rejected",
            "tool_name": tool_name if isinstance(tool_name, str) else None,
            "error": str(e),
            "code": "",
        }

    # 1. Extract code
    deterministic = llm_output is None
    code = (
        build_deterministic_tool_code(description, tool_name)
        if deterministic
        else _extract_python_code(llm_output)
    )

    # 2. Determine tool name
    if tool_name is None:
        # Try to extract from code
        name_match = re.search(r'return\s+"(\w+)"', code)
        if not name_match:
            name_match = re.search(r"return\s+f?'(\w+)'", code)
        if name_match:
            try:
                tool_name = _normalize_tool_name(
                    name_match.group(1),
                    field_name="生成代码中的工具名",
                )
            except ValueError as e:
                return {
                    "status": "rejected",
                    "tool_name": None,
                    "error": str(e),
                    "code": code,
                }
        else:
            tool_name = _normalize_tool_name(
                _sanitize_tool_name(description),
                field_name="自动生成的工具名",
            )

    # 3. Validate
    is_valid, validation_msg = _validate_tool_code(code)
    if not is_valid:
        return {
            "status": "rejected",
            "tool_name": tool_name,
            "error": f"代码验证失败: {validation_msg}",
            "code": code,
        }

    class_name = validation_msg  # _validate_tool_code returns class_name on success

    # 4. Import test
    import_ok, import_msg = _import_test(code, class_name)
    if not import_ok:
        return {
            "status": "rejected",
            "tool_name": tool_name,
            "error": f"实例化测试失败: {import_msg}",
            "code": code,
        }

    # 5. Check name collision
    existing = list_generated_tools()
    if any(t["name"] == tool_name for t in existing):
        # Overwrite existing
        logger.info("Overwriting existing generated tool: %s", tool_name)

    # 6. Save
    try:
        file_path = save_tool(tool_name, code)
    except ValueError as e:
        return {
            "status": "rejected",
            "tool_name": tool_name,
            "error": str(e),
            "code": code,
        }

    logger.info("Forged new tool: %s at %s", tool_name, file_path)
    return {
        "status": "forged",
        "tool_name": tool_name,
        "class_name": class_name,
        "file_path": str(file_path),
        "import_name": import_msg,  # actual tool name from instance
        "code": code,
        "generation_mode": "deterministic" if deterministic else "llm_output",
    }


class ForgeTool(Tool):
    """工具锻造 — 自主设计并注册新工具."""

    @property
    def name(self) -> str:
        return "forge_tool"

    @property
    def description(self) -> str:
        return (
            "工具锻造 — 根据描述自动生成新的工具并注册。"
            "Agent 可以自主扩展自身能力，无需人工编码。"
            "生成的工具经过语法检查、接口验证、实例化测试后保存到磁盘并立即生效。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            user_facing_name="工具锻造",
            search_hint="生成工具 写入磁盘 验证 Python Tool hot reload",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "工具功能描述（用自然语言描述需要什么工具）",
                },
                "tool_name": {
                    "type": "string",
                    "description": "可选的工具名称（小写+下划线），不提供则自动生成",
                },
                "llm_output": {
                    "type": "string",
                    "description": "LLM 生成的工具代码（如未提供则返回生成提示）",
                },
            },
            "required": ["description"],
        }

    async def execute(
        self,
        *,
        description: str,
        tool_name: str | None = None,
        llm_output: str | None = None,
        **kwargs: Any,
    ) -> str:
        result = forge_tool(description, tool_name, llm_output)

        parts: list[str] = ["## 🔨 工具锻造结果"]
        status = result["status"]

        if status == "forged":
            parts.append("**状态**: ✅ 工具已锻造成功")
            parts.append(f"**名称**: `{result['import_name']}`")
            parts.append(f"**文件**: `{result['file_path']}`")
            mode = result.get("generation_mode", "llm_output")
            parts.append(f"**生成方式**: `{mode}`")
            parts.append("")
            parts.append("### 工具代码")
            parts.append("```python")
            parts.append(result["code"][:3000])
            parts.append("```")
            parts.append("")
            parts.append(
                "💡 工具已保存到磁盘。"
                "使用 `hot_reload` 或重启 Agent 即可使用新工具。"
            )

        elif status == "rejected":
            parts.append("**状态**: ❌ 验证未通过")
            parts.append(f"**名称**: `{result.get('tool_name', 'unknown')}`")
            parts.append(f"**原因**: {result.get('error', '未知')}")
            parts.append("")
            parts.append("### 生成的代码")
            parts.append("```python")
            parts.append(result.get("code", "")[:2000])
            parts.append("```")

        return "\n".join(parts)
