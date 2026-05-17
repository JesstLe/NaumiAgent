"""工具锻造 — Agent 自主设计、生成、注册新工具."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)

_GENERATED_DIR = Path(__file__).resolve().parent / "generated"

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

        return True, tool_instance.name
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
    generated_dir = get_generated_dir()
    file_name = f"{tool_name.replace('-', '_')}.py"
    file_path = generated_dir / file_name
    file_path.write_text(code, encoding="utf-8")
    return file_path


def load_generated_tool(tool_name: str) -> Tool | None:
    """Load a generated tool by name.

    Args:
        tool_name: Tool name (snake_case).

    Returns:
        Tool instance or None if not found.
    """
    file_name = f"{tool_name.replace('-', '_')}.py"
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
            f"naumi_agent.tools.generated.{tool_name}",
            str(file_path),
        )
        if spec is None or spec.loader is None:
            return None

        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        tool_class = getattr(mod, class_name, None)
        if tool_class is None:
            return None

        return tool_class()
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
    file_name = f"{tool_name.replace('-', '_')}.py"
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
        llm_output: Pre-generated code (if None, caller provides via LLM).

    Returns:
        Dict with status, tool_name, validation results.
    """
    if llm_output is None:
        return {
            "status": "needs_llm",
            "description": description,
            "system_prompt": _TOOL_GENERATION_SYSTEM,
            "message": "需要 LLM 生成代码。请用 system_prompt 调用 LLM 后将输出传入 llm_output。",
        }

    # 1. Extract code
    code = _extract_python_code(llm_output)

    # 2. Determine tool name
    if tool_name is None:
        # Try to extract from code
        name_match = re.search(r'return\s+"(\w+)"', code)
        if not name_match:
            name_match = re.search(r"return\s+f?'(\w+)'", code)
        if name_match:
            tool_name = name_match.group(1)
        else:
            # Generate from description
            words = re.findall(r"[a-zA-Z一-鿿]+", description)
            tool_name = (
                "_".join(w.lower() for w in words[:3] if w.isascii())
                or "custom_tool"
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
    file_path = save_tool(tool_name, code)

    logger.info("Forged new tool: %s at %s", tool_name, file_path)
    return {
        "status": "forged",
        "tool_name": tool_name,
        "class_name": class_name,
        "file_path": str(file_path),
        "import_name": import_msg,  # actual tool name from instance
        "code": code,
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

        if status == "needs_llm":
            parts.append("**状态**: 📝 需要 LLM 生成代码")
            parts.append(f"**描述**: {description}")
            parts.append("")
            parts.append("请使用以下系统提示词调用 LLM 生成工具代码，然后将输出传入:")
            parts.append("```")
            parts.append(result["system_prompt"])
            parts.append("```")

        elif status == "forged":
            parts.append("**状态**: ✅ 工具已锻造成功")
            parts.append(f"**名称**: `{result['import_name']}`")
            parts.append(f"**文件**: `{result['file_path']}`")
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
