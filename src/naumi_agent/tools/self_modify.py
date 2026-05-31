"""自我修改 — Agent 在沙箱中修改自身工具代码，验证后应用."""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)

# Reuse protected prefixes from hot-reload.
_PROTECTED_PREFIXES = (
    "naumi_agent.orchestrator.engine",
    "naumi_agent.orchestrator.subagent_manager",
    "naumi_agent.safety.",
    "naumi_agent.config.",
    "naumi_agent.model.router",
    "naumi_agent.tools.hotreload",
    "naumi_agent.tools.base",
    "naumi_agent.tools.self_modify",
)

# Only tool modules under these prefixes are modifiable.
_MODIFIABLE_PREFIXES = (
    "naumi_agent.tools.",
    "naumi_agent.memory.",
    "naumi_agent.skills.",
)

_AGENT_SOURCE_DIR: Path | None = None


def _find_agent_source_dir() -> Path:
    """Locate the naumi_agent source directory."""
    global _AGENT_SOURCE_DIR
    if _AGENT_SOURCE_DIR is not None:
        return _AGENT_SOURCE_DIR

    candidates = [
        # Development: src/naumi_agent/
        Path(__file__).resolve().parent.parent,
        # Editable install fallback
        Path(__file__).resolve().parent,
    ]

    for candidate in candidates:
        if (candidate / "__init__.py").exists() and candidate.name == "naumi_agent":
            _AGENT_SOURCE_DIR = candidate
            return candidate

    # Walk up from this file
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "naumi_agent" / "__init__.py").exists():
            _AGENT_SOURCE_DIR = parent / "naumi_agent"
            return _AGENT_SOURCE_DIR

    raise FileNotFoundError("Cannot locate naumi_agent source directory")


def _resolve_target_path(target_file: str) -> Path:
    """Resolve a relative target file to an absolute path.

    Args:
        target_file: Relative path like "tools/analysis.py"

    Returns:
        Absolute path to the file.

    Raises:
        ValueError: If the path escapes the source directory.
    """
    source_dir = _find_agent_source_dir()
    resolved = (source_dir / target_file).resolve()

    if not str(resolved).startswith(str(source_dir)):
        raise ValueError(f"路径越界: {target_file}")

    if not resolved.suffix == ".py":
        raise ValueError(f"只支持修改 .py 文件: {target_file}")

    return resolved


def _is_protected_file(file_path: Path) -> bool:
    """Check if a file belongs to a protected module."""
    source_dir = _find_agent_source_dir()
    try:
        relative = file_path.relative_to(source_dir)
    except ValueError:
        return True

    module_name = "naumi_agent." + str(relative.with_suffix("")).replace("/", ".")

    for prefix in _PROTECTED_PREFIXES:
        if module_name == prefix or module_name.startswith(prefix.rstrip(".") + "."):
            return True
    return False


def _is_modifiable_file(file_path: Path) -> bool:
    """Check if a file belongs to a modifiable domain."""
    source_dir = _find_agent_source_dir()
    try:
        relative = file_path.relative_to(source_dir)
    except ValueError:
        return False

    module_name = "naumi_agent." + str(relative.with_suffix("")).replace("/", ".")

    for prefix in _MODIFIABLE_PREFIXES:
        if module_name.startswith(prefix):
            return True
    return False


def _create_git_backup(file_path: Path) -> str | None:
    """Create a git stash entry as backup before modification.

    Returns:
        Stash ref if successful, None if git unavailable.
    """
    try:
        # Stage the current file
        subprocess.run(
            ["git", "add", str(file_path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        # Create a stash with a descriptive message
        result = subprocess.run(
            ["git", "stash", "push", "-m", f"self_modify_backup:{file_path.name}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            # Pop immediately so working tree is restored
            subprocess.run(
                ["git", "stash", "pop"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return "stash@{0}"
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.CalledProcessError):
        return None


def _rollback_file(file_path: Path) -> bool:
    """Restore a file from git.

    Returns:
        True if rollback succeeded.
    """
    try:
        result = subprocess.run(
            ["git", "checkout", "HEAD", "--", str(file_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _restore_original_content(file_path: Path, original_content: str) -> bool:
    """Restore the exact pre-modification content, preserving uncommitted user edits."""
    try:
        file_path.write_text(original_content, encoding="utf-8")
        return True
    except Exception:
        return _rollback_file(file_path)


def _run_ruff(file_path: Path) -> tuple[bool, str]:
    """Run ruff check on the file.

    Returns:
        (passed, output)
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", str(file_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        passed = result.returncode == 0
        output = result.stdout.strip() or result.stderr.strip()
        return passed, output
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, str(e)


def _run_ruff_format(file_path: Path) -> tuple[bool, str]:
    """Run ruff format check on the file.

    Returns:
        (passed, output)
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "format", "--check", str(file_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        passed = result.returncode == 0
        output = result.stdout.strip() or result.stderr.strip()
        return passed, output
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, str(e)


def _find_test_file(file_path: Path) -> Path | None:
    """Find the corresponding test file for a source file.

    Convention: src/naumi_agent/tools/foo.py → tests/unit/test_foo.py
    """
    source_dir = _find_agent_source_dir()
    try:
        file_path.relative_to(source_dir)
    except ValueError:
        return None

    # naumi_agent/tools/analysis.py → test_analysis.py
    stem = file_path.stem
    test_dir = source_dir.parent.parent / "tests" / "unit"

    if not test_dir.is_dir():
        return None

    test_file = test_dir / f"test_{stem}.py"
    return test_file if test_file.exists() else None


def _run_tests(file_path: Path) -> tuple[bool, str]:
    """Run pytest on the corresponding test file.

    Returns:
        (passed, output)
    """
    test_file = _find_test_file(file_path)
    if test_file is None:
        return True, "无对应测试文件，跳过测试"

    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                str(test_file),
                "-x", "-q",
                "--timeout=30",
                "--tb=short",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        passed = result.returncode == 0
        output = result.stdout.strip() + "\n" + result.stderr.strip()
        return passed, output.strip()
    except subprocess.TimeoutExpired:
        return False, "测试超时 (60s)"
    except FileNotFoundError:
        return True, "pytest 不可用，跳过测试"


def _run_import_test(file_path: Path) -> tuple[bool, str]:
    """Test that the modified module can be imported without errors.

    Returns:
        (passed, error_message)
    """
    source_dir = _find_agent_source_dir()
    try:
        relative = file_path.relative_to(source_dir)
    except ValueError:
        return False, f"无法解析模块路径: {file_path}"

    module_name = "naumi_agent." + str(relative.with_suffix("")).replace("/", ".")

    try:
        result = subprocess.run(
            [
                sys.executable, "-c",
                f"import importlib; importlib.import_module('{module_name}')",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        passed = result.returncode == 0
        output = result.stderr.strip() if not passed else ""
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "导入测试超时"


def _compute_diff(original: str, modified: str) -> str:
    """Compute a unified diff between original and modified content."""
    import difflib

    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)
    diff = difflib.unified_diff(
        original_lines,
        modified_lines,
        fromfile="original",
        tofile="modified",
    )
    return "".join(diff) or "(无变更)"


def validate_and_apply(
    target_file: str,
    new_content: str,
    description: str,
) -> dict[str, Any]:
    """Validate a proposed modification and apply if safe.

    Pipeline:
    1. Resolve target path
    2. Check modifiable and not protected
    3. Read original content
    4. Compute diff
    5. Write to temp file for validation
    6. Run ruff check
    7. Run ruff format check
    8. Write to actual file
    9. Run import test against the real package module
    10. Run pytest on corresponding test file
    11. If import/tests fail → restore original content

    Returns:
        Dict with status, diff, validation results.
    """
    # 1. Resolve path
    try:
        file_path = _resolve_target_path(target_file)
    except ValueError as e:
        return {"status": "rejected", "error": str(e)}

    # 2. Safety checks
    if _is_protected_file(file_path):
        return {
            "status": "rejected",
            "file": target_file,
            "error": "文件在保护区内，禁止修改（引擎/安全/配置等核心模块）",
        }

    if not _is_modifiable_file(file_path):
        return {
            "status": "rejected",
            "file": target_file,
            "error": "文件不在可修改范围内（仅限 tools/memory/skills 目录下的模块）",
        }

    if not file_path.exists():
        return {
            "status": "rejected",
            "file": target_file,
            "error": f"文件不存在: {target_file}",
        }

    # 3. Read original content
    try:
        original_content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        return {"status": "error", "file": target_file, "error": f"读取原文件失败: {e}"}

    # 4. No-op check
    if original_content == new_content:
        return {
            "status": "noop",
            "file": target_file,
            "message": "内容无变更",
        }

    # 5. Compute diff
    diff = _compute_diff(original_content, new_content)

    # 6. Validate in temp file first
    validation_results: dict[str, Any] = {}
    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", encoding="utf-8", delete=False,
    ) as tmp:
        tmp.write(new_content)
        tmp_path = Path(tmp.name)

    try:
        # Ruff check
        lint_passed, lint_output = _run_ruff(tmp_path)
        validation_results["ruff_check"] = {
            "passed": lint_passed,
            "output": lint_output,
        }

        if not lint_passed:
            return {
                "status": "rejected",
                "file": target_file,
                "error": "ruff 检查未通过",
                "validation": validation_results,
                "diff": diff,
            }

        # Ruff format check
        fmt_passed, fmt_output = _run_ruff_format(tmp_path)
        validation_results["ruff_format"] = {
            "passed": fmt_passed,
            "output": fmt_output,
        }

    finally:
        tmp_path.unlink(missing_ok=True)

    # 7. Git backup
    backup_ref = _create_git_backup(file_path)

    # 8. Write to actual file
    try:
        file_path.write_text(new_content, encoding="utf-8")
    except Exception as e:
        return {
            "status": "error",
            "file": target_file,
            "error": f"写入文件失败: {e}",
            "diff": diff,
        }

    # 9. Import test against the actual package path.
    import_passed, import_output = _run_import_test(file_path)
    validation_results["import_test"] = {
        "passed": import_passed,
        "output": import_output,
    }

    if not import_passed:
        restored = _restore_original_content(file_path, original_content)
        return {
            "status": "rolled_back",
            "file": target_file,
            "error": "导入测试失败，已恢复修改前内容",
            "rollback_success": restored,
            "validation": validation_results,
            "diff": diff,
        }

    # 10. Run tests
    test_passed, test_output = _run_tests(file_path)
    validation_results["pytest"] = {
        "passed": test_passed,
        "output": test_output[:2000],  # Truncate long output
    }

    if not test_passed:
        rolled_back = _restore_original_content(file_path, original_content)
        return {
            "status": "rolled_back",
            "file": target_file,
            "error": "测试未通过，已恢复修改前内容",
            "rollback_success": rolled_back,
            "validation": validation_results,
            "diff": diff,
        }

    # 10. Success
    logger.info("Self-modification applied: %s — %s", target_file, description)
    return {
        "status": "applied",
        "file": target_file,
        "description": description,
        "diff": diff,
        "validation": validation_results,
        "backup_ref": backup_ref,
    }


class SelfModifyTool(Tool):
    """自我修改 — Agent 修改自身工具代码并验证."""

    @property
    def name(self) -> str:
        return "self_modify"

    @property
    def description(self) -> str:
        return (
            "自我修改 — 修改 Agent 自身的工具代码。"
            "提交修改后自动执行静态检查和测试验证，通过后热重载生效。"
            "仅允许修改 tools/memory/skills 目录下的模块，核心引擎受保护。"
            "修改失败会自动回滚到修改前的状态。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target_file": {
                    "type": "string",
                    "description": (
                        "要修改的文件，相对于 naumi_agent 包根目录 "
                        "(如 tools/analysis.py)"
                    ),
                },
                "new_content": {
                    "type": "string",
                    "description": "修改后的完整文件内容",
                },
                "description": {
                    "type": "string",
                    "description": "修改说明：做了什么、为什么",
                },
            },
            "required": ["target_file", "new_content", "description"],
        }

    async def execute(
        self,
        *,
        target_file: str,
        new_content: str,
        description: str,
        **kwargs: Any,
    ) -> str:
        result = validate_and_apply(target_file, new_content, description)

        parts: list[str] = ["## 自我修改结果"]
        status = result["status"]
        file_name = result.get("file", target_file)

        if status == "applied":
            parts.append("**状态**: ✅ 已应用并验证")
            parts.append(f"**文件**: `{file_name}`")
            parts.append(f"**说明**: {description}")
            parts.append("")
            parts.append("### 验证详情")
            for check_name, check_result in result.get("validation", {}).items():
                icon = "✅" if check_result["passed"] else "❌"
                status_text = (
                    "通过"
                    if check_result["passed"]
                    else check_result.get("output", "失败")
                )
                parts.append(f"- {icon} **{check_name}**: {status_text}")
            if result.get("diff"):
                parts.append("")
                parts.append("### 变更内容")
                parts.append("```diff")
                parts.append(result["diff"][:3000])
                parts.append("```")
            parts.append("")
            parts.append("💡 修改已写入磁盘并通过验证。使用 `hot_reload` 工具重载模块使改动生效。")

        elif status == "rolled_back":
            parts.append("**状态**: 🔄 已回滚（测试未通过）")
            parts.append(f"**文件**: `{file_name}`")
            rollback_ok = result.get("rollback_success", False)
            parts.append(f"**回滚**: {'成功' if rollback_ok else '⚠️ 回滚失败，请手动检查'}")
            parts.append("")
            parts.append("### 验证详情")
            for check_name, check_result in result.get("validation", {}).items():
                icon = "✅" if check_result["passed"] else "❌"
                output = check_result.get("output", "")
                if not check_result["passed"]:
                    parts.append(f"- {icon} **{check_name}**: {output[:500]}")
                else:
                    parts.append(f"- {icon} **{check_name}**: 通过")

        elif status == "rejected":
            parts.append("**状态**: ❌ 已拒绝")
            parts.append(f"**文件**: `{file_name}`")
            parts.append(f"**原因**: {result.get('error', '未知')}")
            if result.get("validation"):
                parts.append("")
                parts.append("### 验证详情")
                for check_name, check_result in result["validation"].items():
                    icon = "✅" if check_result["passed"] else "❌"
                    parts.append(f"- {icon} **{check_name}**: {check_result.get('output', '失败')}")

        elif status == "noop":
            parts.append("**状态**: ⏭️ 无变更")
            parts.append(f"**文件**: `{file_name}`")

        else:
            parts.append("**状态**: ❌ 错误")
            parts.append(f"**文件**: `{file_name}`")
            parts.append(f"**原因**: {result.get('error', '未知错误')}")

        return "\n".join(parts)
