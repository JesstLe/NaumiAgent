"""Deterministic eval-driven development helpers."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


def scan_eval(files: list[Path], source_text: str) -> str:
    """Extract signatures, branch counts, exceptions, and test hints."""
    findings: list[str] = []

    func_sigs: list[str] = []
    class_defs: list[str] = []
    for file in files:
        try:
            tree = ast.parse(file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                args = [arg.arg for arg in node.args.args if arg.arg != "self"]
                returns = ast.unparse(node.returns) if node.returns else ""
                func_sigs.append(
                    f"  - {file.name}:{node.lineno} {node.name}"
                    f"({', '.join(args)}) -> {returns}"
                )
            elif isinstance(node, ast.ClassDef):
                class_defs.append(f"  - {file.name}:{node.lineno} class {node.name}")

    findings.append(f"- 函数定义: {len(func_sigs)} 个")
    for sig in func_sigs[:15]:
        findings.append(sig)
    if len(func_sigs) > 15:
        findings.append(f"  ... 还有 {len(func_sigs) - 15} 个")

    if class_defs:
        findings.append(f"- 类定义: {len(class_defs)} 个")
        for class_def in class_defs[:10]:
            findings.append(class_def)

    if_count = len(re.findall(r"\bif\s+|\belif\s+", source_text))
    findings.append(f"- 条件分支 (if/elif): {if_count} 个 (每个分支至少需要 1 个测试)")

    raises = re.findall(r"raise\s+\w+", source_text)
    if raises:
        findings.append(f"- 异常抛出点: {len(raises)} 个")
        for raise_expr in raises[:8]:
            findings.append(f"  - `{raise_expr}`")

    existing_tests = re.findall(r"\bdef\s+test_\w+", source_text)
    if existing_tests:
        findings.append(f"- 已有测试: {len(existing_tests)} 个")
        for test_name in existing_tests[:8]:
            findings.append(f"  - `{test_name}`")
    else:
        findings.append("- ⚠️ 未发现任何 test_ 开头的测试函数")

    annotated_params = len(re.findall(r"def\s+\w+\([^)]*:\s*\w+", source_text))
    total_params = len(re.findall(r"def\s+\w+\([^)]*\)", source_text))
    if total_params > 0:
        pct = annotated_params * 100 // total_params
        findings.append(f"- 类型标注覆盖: {annotated_params}/{total_params} 参数 ({pct}%)")

    input_points = re.findall(
        r"(?:request\.\w+|input\(|sys\.argv|os\.environ|json\.loads\()",
        source_text,
    )
    if input_points:
        findings.append(f"- 外部输入点: {len(input_points)} 个 (必须用异常输入测试)")

    findings.append(f"\n- 预估最低测试数: {max(if_count + len(raises), len(func_sigs))} 个")

    return "\n".join(findings)


@dataclass(frozen=True)
class EvalFunctionTarget:
    file_path: str
    module_name: str
    qualname: str
    parameters: tuple[str, ...]


@dataclass(frozen=True)
class EvalClassTarget:
    file_path: str
    module_name: str
    class_name: str


@dataclass(frozen=True)
class EvalBaseline:
    test_code: str
    function_count: int
    class_count: int


def build_eval_baseline(files: list[Path]) -> EvalBaseline:
    """Generate a deterministic runnable pytest baseline from Python AST."""
    functions: list[EvalFunctionTarget] = []
    classes: list[EvalClassTarget] = []

    for file in files:
        if file.suffix != ".py":
            continue
        try:
            tree = ast.parse(file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        module_name = file.stem
        file_path = str(file.resolve())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if node.name.startswith("_"):
                    continue
                functions.append(
                    EvalFunctionTarget(
                        file_path=file_path,
                        module_name=module_name,
                        qualname=node.name,
                        parameters=tuple(arg.arg for arg in node.args.args),
                    )
                )
            elif isinstance(node, ast.ClassDef):
                if node.name.startswith("_"):
                    continue
                classes.append(
                    EvalClassTarget(
                        file_path=file_path,
                        module_name=module_name,
                        class_name=node.name,
                    )
                )
                for child in node.body:
                    if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                        continue
                    if child.name.startswith("_") and child.name != "__init__":
                        continue
                    functions.append(
                        EvalFunctionTarget(
                            file_path=file_path,
                            module_name=module_name,
                            qualname=f"{node.name}.{child.name}",
                            parameters=tuple(arg.arg for arg in child.args.args),
                        )
                    )

    target_files = tuple(str(file.resolve()) for file in files if file.suffix == ".py")
    test_code = render_eval_baseline(functions, classes, target_files)
    return EvalBaseline(
        test_code=test_code,
        function_count=len(functions),
        class_count=len(classes),
    )


def render_eval_baseline(
    functions: list[EvalFunctionTarget],
    classes: list[EvalClassTarget],
    target_files: tuple[str, ...],
) -> str:
    """Render a pytest file that imports targets and validates public signatures."""
    function_rows = [
        (fn.file_path, fn.module_name, fn.qualname, list(fn.parameters))
        for fn in functions
    ]
    class_rows = [(cls.file_path, cls.module_name, cls.class_name) for cls in classes]
    import_files = sorted(set(target_files))
    lines = [
        "# EDD baseline: generated by analysis_eval.",
        "from __future__ import annotations",
        "",
        "import importlib.util",
        "import inspect",
        "from pathlib import Path",
        "",
        "import pytest",
        "",
        f"TARGET_FILES = {import_files!r}",
        f"FUNCTION_TARGETS = {function_rows!r}",
        f"CLASS_TARGETS = {class_rows!r}",
        "",
        "",
        "def _load_module(path: str, module_name: str):",
        "    spec = importlib.util.spec_from_file_location(module_name, path)",
        "    assert spec is not None",
        "    assert spec.loader is not None",
        "    module = importlib.util.module_from_spec(spec)",
        "    spec.loader.exec_module(module)",
        "    return module",
        "",
        "",
        "@pytest.mark.parametrize('path', TARGET_FILES)",
        "def test_target_module_imports(path: str) -> None:",
        "    _load_module(path, Path(path).stem)",
        "",
        "",
        "@pytest.mark.parametrize('path,module_name,qualname,parameters', FUNCTION_TARGETS)",
        "def test_public_function_signature(",
        "    path: str, module_name: str, qualname: str, parameters: list[str]",
        ") -> None:",
        "    module = _load_module(path, module_name)",
        "    owner = module",
        "    function_name = qualname",
        "    if '.' in qualname:",
        "        class_name, function_name = qualname.split('.', 1)",
        "        owner = getattr(module, class_name)",
        "        member = owner.__dict__[function_name]",
        "        target = member.__func__ if hasattr(member, '__func__') else member",
        "    else:",
        "        target = getattr(owner, function_name)",
        "    assert callable(target)",
        "    signature = inspect.signature(target)",
        "    assert list(signature.parameters) == parameters",
        "",
        "",
        "@pytest.mark.parametrize('path,module_name,class_name', CLASS_TARGETS)",
        "def test_public_class_exists(path: str, module_name: str, class_name: str) -> None:",
        "    module = _load_module(path, module_name)",
        "    cls = getattr(module, class_name)",
        "    assert inspect.isclass(cls)",
        "",
    ]
    return "\n".join(lines)


def format_eval_baseline(scan_evidence: str, baseline: EvalBaseline) -> str:
    """Format eval scan evidence and generated pytest for tool output."""
    return (
        "## Eval 静态扫描\n"
        f"{scan_evidence}\n\n"
        "## EDD Baseline Pytest\n"
        f"- 函数/方法目标：{baseline.function_count}\n"
        f"- 类目标：{baseline.class_count}\n\n"
        "```python\n"
        f"{baseline.test_code}\n"
        "```"
    )
