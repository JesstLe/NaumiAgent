"""Deterministic JIT script generation helpers."""

from __future__ import annotations

import ast
import operator
import re
from dataclasses import dataclass

COMPUTATION_TASKS = {
    "math": [
        r"(?:计算|求值|积分|微分|矩阵|向量|概率|统计|回归)",
        r"(?:calculate|compute|integral|derivative|matrix|vector)",
        r"(?:fibonacci|prime|factorial|permutation|combination)",
        r"(?:方程|不等式|最优化|线性规划)",
    ],
    "string": [
        r"(?:字符串|正则|匹配|替换|编码|解码|哈希)",
        r"(?:regex|parse|transform|encode|decode|hash|base64)",
        r"(?:格式化|提取|分割|拼接|转义)",
    ],
    "data": [
        r"(?:排序|过滤|聚合|去重|分组|透视|统计)",
        r"(?:sort|filter|aggregate|dedup|group|pivot)",
        r"(?:csv|json|yaml|xml|excel|pandas|dataframe)",
    ],
    "algo": [
        r"(?:算法|图|树|路径|搜索|动态规划|贪心|回溯)",
        r"(?:graph|tree|bfs|dfs|dijkstra|dp|greedy|backtrack)",
        r"(?:排序算法|查找|时间复杂度|空间复杂度)",
    ],
    "network": [
        r"(?:爬虫|抓取|请求|http|api|websocket|socket)",
        r"(?:scrape|fetch|request|crawl|download)",
        r"(?:dns|tcp|udp|ip|端口|代理)",
    ],
}


@dataclass(frozen=True)
class JITBaseline:
    script: str
    execution_output: str
    verified: bool


def scan_jit(task: str) -> str:
    """Classify a task and decide whether deterministic code verification is needed."""
    findings: list[str] = []
    task_lower = task.lower()

    matched_types: list[tuple[str, list[str]]] = []
    for comp_type, patterns in COMPUTATION_TASKS.items():
        hits = []
        for pattern in patterns:
            hits.extend(re.findall(pattern, task_lower))
        if hits:
            matched_types.append((comp_type, hits))

    if matched_types:
        findings.append("- 检测到计算需求:")
        for comp_type, keywords in matched_types:
            unique_kw = list(set(keywords))[:5]
            findings.append(f"  - {comp_type}: {', '.join(unique_kw)}")
    else:
        findings.append("- 计算需求: 未匹配到明确模式（将由 LLM 判断）")

    if any(item[0] == "math" for item in matched_types):
        findings.append("- 推荐语言: Python (numpy/scipy) 或 C++ (高性能)")
    elif any(item[0] == "string" for item in matched_types):
        findings.append("- 推荐语言: Python (re/字符串操作)")
    elif any(item[0] == "data" for item in matched_types):
        findings.append("- 推荐语言: Python (pandas/csv/json)")
    elif any(item[0] == "algo" for item in matched_types):
        findings.append("- 推荐语言: Python (快速验证) 或 C++ (生产级)")
    elif any(item[0] == "network" for item in matched_types):
        findings.append("- 推荐语言: Python (httpx/requests)")
    else:
        findings.append("- 推荐语言: Python (通用) — 最适合即时生成与执行")

    constraints: list[str] = []
    if re.search(r"\d+\s*(?:ms|毫秒|秒|second)", task_lower):
        constraints.append("时间限制")
    if re.search(r"\d+\s*(?:MB|GB|KB|字节)", task_lower):
        constraints.append("内存限制")
    if re.search(r"(?:精确|精确到|小数点|精度|float|double|decimal)", task_lower):
        constraints.append("精度要求")
    if re.search(r"(?:并发|并行|多线程|multi)", task_lower):
        constraints.append("并发要求")
    if re.search(r"(?:大数|10\^\d+|万|亿|million|billion)", task_lower):
        constraints.append("大数据量")
    if constraints:
        findings.append(f"- 约束条件: {', '.join(constraints)}")

    verification_needed = any(item[0] in ("math", "algo") for item in matched_types)
    if verification_needed:
        findings.append("- ✅ 需要计算验证 — LLM 推理不可靠，必须运行代码")
    else:
        findings.append("- ℹ️ 可选计算验证 — LLM 推理可能够用，但代码更可靠")

    return "\n".join(findings)


def build_jit_baseline(task: str, context: str = "") -> JITBaseline:
    """Build a runnable deterministic verification script for a JIT request."""
    expression = extract_arithmetic_expression(task)
    if expression:
        try:
            result = safe_eval_arithmetic(expression)
            script = render_jit_arithmetic_script(task, context, expression, result)
            output = f"EXPRESSION={expression}\nRESULT={result}\nSTATUS=verified"
            return JITBaseline(script=script, execution_output=output, verified=True)
        except Exception as exc:
            script = render_jit_triage_script(task, context, f"算术表达式拒绝执行：{exc}")
            output = f"STATUS=needs_manual_verification\nREASON={exc}"
            return JITBaseline(script=script, execution_output=output, verified=False)

    script = render_jit_triage_script(
        task,
        context,
        "未检测到可安全直接求值的算术表达式，已生成可运行验证脚手架。",
    )
    output = "STATUS=needs_manual_verification\nREASON=no_safe_arithmetic_expression"
    return JITBaseline(script=script, execution_output=output, verified=False)


def extract_arithmetic_expression(task: str) -> str:
    """Extract a safe-looking arithmetic expression from natural language."""
    candidates = re.findall(r"(?<![\w.])[\d\s+\-*/().%]+(?![\w.])", task)
    candidates = [
        candidate.strip()
        for candidate in candidates
        if re.search(r"\d", candidate) and re.search(r"[+\-*/%]", candidate)
    ]
    if not candidates:
        return ""
    return max(candidates, key=len)


def safe_eval_arithmetic(expression: str) -> int | float:
    """Evaluate arithmetic using an AST allowlist instead of Python eval."""
    operators = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def eval_node(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return eval_node(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in operators:
            left = eval_node(node.left)
            right = eval_node(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10:
                raise ValueError("幂指数过大")
            return operators[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in operators:
            return operators[type(node.op)](eval_node(node.operand))
        raise ValueError(f"不支持的表达式节点：{type(node).__name__}")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("表达式语法无效") from exc
    return eval_node(tree)


def render_jit_arithmetic_script(
    task: str,
    context: str,
    expression: str,
    result: int | float,
) -> str:
    """Render an executable arithmetic verification script."""
    return f'''\
"""JIT verification script generated by analysis_jit."""

TASK = {task!r}
CONTEXT = {context!r}
EXPRESSION = {expression!r}
EXPECTED_RESULT = {result!r}


def compute() -> int | float:
    return {expression}


def main() -> None:
    actual = compute()
    print(f"task={{TASK}}")
    if CONTEXT:
        print(f"context={{CONTEXT}}")
    print(f"expression={{EXPRESSION}}")
    print(f"result={{actual}}")
    assert actual == EXPECTED_RESULT
    print("status=verified")


if __name__ == "__main__":
    main()
'''


def render_jit_triage_script(task: str, context: str, reason: str) -> str:
    """Render an executable scaffold for non-trivial JIT requests."""
    return f'''\
"""JIT verification scaffold generated by analysis_jit."""

TASK = {task!r}
CONTEXT = {context!r}
REASON = {reason!r}


def classify_task(task: str) -> str:
    lowered = task.lower()
    if any(token in lowered for token in ("json", "csv", "排序", "过滤", "聚合")):
        return "data"
    if any(token in lowered for token in ("regex", "正则", "字符串", "parse")):
        return "string"
    if any(token in lowered for token in ("graph", "tree", "算法", "路径")):
        return "algo"
    return "general"


def main() -> None:
    print(f"task={{TASK}}")
    if CONTEXT:
        print(f"context={{CONTEXT}}")
    print(f"classification={{classify_task(TASK)}}")
    print(f"reason={{REASON}}")
    print("status=needs_manual_contract")


if __name__ == "__main__":
    main()
'''


def format_jit_baseline(scan_evidence: str, baseline: JITBaseline) -> str:
    """Format deterministic JIT evidence for the tool result."""
    status = "verified" if baseline.verified else "needs_manual_verification"
    return (
        "## JIT 确定性脚本\n"
        f"{scan_evidence}\n\n"
        f"- 执行状态：{status}\n\n"
        "```python\n"
        f"{baseline.script}"
        "```\n\n"
        "## Execution Result\n"
        "```text\n"
        f"{baseline.execution_output}\n"
        "```"
    )
