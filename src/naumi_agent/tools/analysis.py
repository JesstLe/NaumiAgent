"""分析模式工具 — chaos/scale/state/vibe，可作为工具被 Agent 自主调用.

每个工具执行两阶段分析:
  1. 静态扫描阶段 — 读文件、grep 模式、统计指标，收集实打实的代码证据
  2. LLM 综合阶段 — 把扫描证据 + 专有 prompt 交给 LLM 做深度推理与建议
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from naumi_agent.tools import analysis_common
from naumi_agent.tools.analysis_support import autopsy as _autopsy_support
from naumi_agent.tools.analysis_support import consensus as _consensus_support
from naumi_agent.tools.analysis_support import cooe as _cooe_support
from naumi_agent.tools.analysis_support import cosmos as _cosmos_support
from naumi_agent.tools.analysis_support import dspy as _dspy_support
from naumi_agent.tools.analysis_support import entropy as _entropy_support
from naumi_agent.tools.analysis_support import eval as _eval_support
from naumi_agent.tools.analysis_support import fusion as _fusion_support
from naumi_agent.tools.analysis_support import genesis as _genesis_support
from naumi_agent.tools.analysis_support import graph as _graph_support
from naumi_agent.tools.analysis_support import heal as _heal_support
from naumi_agent.tools.analysis_support import hook as _hook_support
from naumi_agent.tools.analysis_support import jit as _jit_support
from naumi_agent.tools.analysis_support import macro as _macro_support
from naumi_agent.tools.analysis_support import mcts as _mcts_support
from naumi_agent.tools.analysis_support import ooda as _ooda_support
from naumi_agent.tools.analysis_support import page as _page_support
from naumi_agent.tools.analysis_support import pid as _pid_support
from naumi_agent.tools.analysis_support import pointer as _pointer_support
from naumi_agent.tools.analysis_support import probe as _probe_support
from naumi_agent.tools.analysis_support import route as _route_support
from naumi_agent.tools.analysis_support import self_review as _self_review_support
from naumi_agent.tools.analysis_support import sleep as _sleep_support
from naumi_agent.tools.analysis_support import spar as _spar_support
from naumi_agent.tools.analysis_support import speculate as _speculate_support
from naumi_agent.tools.analysis_support import static_modes as _static_modes_support
from naumi_agent.tools.analysis_support import supervisor as _supervisor_support
from naumi_agent.tools.analysis_support import vibe as _vibe_support
from naumi_agent.tools.analysis_support import vision as _vision_support
from naumi_agent.tools.analysis_support import watchdog as _watchdog_support

if TYPE_CHECKING:
    from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.tools.analysis_support import world as _world_support
from naumi_agent.tools.analysis_support import zkp as _zkp_support
from naumi_agent.tools.analysis_tools.autopsy import (
    AutopsyTool as _AutopsyTool,
)
from naumi_agent.tools.analysis_tools.chaos import (
    ChaosAnalysisTool as _ChaosAnalysisTool,
)
from naumi_agent.tools.analysis_tools.consensus import (
    ConsensusTool as _ConsensusTool,
)
from naumi_agent.tools.analysis_tools.cooe import (
    COOETool as _COOETool,
)
from naumi_agent.tools.analysis_tools.cosmos import (
    CosmosTool as _CosmosTool,
)
from naumi_agent.tools.analysis_tools.dspy import (
    DSPyTool as _DSPyTool,
)
from naumi_agent.tools.analysis_tools.entropy import (
    EntropyValveTool as _EntropyValveTool,
)
from naumi_agent.tools.analysis_tools.fusion import (
    FusionTool as _FusionTool,
)
from naumi_agent.tools.analysis_tools.genesis import (
    GenesisTool as _GenesisTool,
)
from naumi_agent.tools.analysis_tools.graph import (
    GraphRAGTool as _GraphRAGTool,
)
from naumi_agent.tools.analysis_tools.hook import (
    HookTool as _HookTool,
)
from naumi_agent.tools.analysis_tools.macro import (
    MacroTool as _MacroTool,
)
from naumi_agent.tools.analysis_tools.mcts import (
    MCTSTool as _MCTSTool,
)
from naumi_agent.tools.analysis_tools.ooda import (
    OODATool as _OODATool,
)
from naumi_agent.tools.analysis_tools.page import (
    MemoryPageTool as _MemoryPageTool,
)
from naumi_agent.tools.analysis_tools.pid import (
    PIDTool as _PIDTool,
)
from naumi_agent.tools.analysis_tools.pointer import (
    PointerTool as _PointerTool,
)
from naumi_agent.tools.analysis_tools.probe import (
    ProbeTool as _ProbeTool,
)
from naumi_agent.tools.analysis_tools.route import (
    MoERouteTool as _MoERouteTool,
)
from naumi_agent.tools.analysis_tools.scale import (
    ScaleAnalysisTool as _ScaleAnalysisTool,
)
from naumi_agent.tools.analysis_tools.self_review import (
    SelfReviewTool as _SelfReviewTool,
)
from naumi_agent.tools.analysis_tools.sleep import (
    SleepPruningTool as _SleepPruningTool,
)
from naumi_agent.tools.analysis_tools.spar import (
    SparTool as _SparTool,
)
from naumi_agent.tools.analysis_tools.speculate import (
    SpeculateTool as _SpeculateTool,
)
from naumi_agent.tools.analysis_tools.state import (
    StateAuditTool as _StateAuditTool,
)
from naumi_agent.tools.analysis_tools.supervisor import (
    SupervisorTool as _SupervisorTool,
)
from naumi_agent.tools.analysis_tools.vibe import (
    VibeModeTool as _VibeModeTool,
)
from naumi_agent.tools.analysis_tools.vision import (
    VisionTool as _VisionTool,
)
from naumi_agent.tools.analysis_tools.watchdog import (
    WatchdogTool as _WatchdogTool,
)
from naumi_agent.tools.analysis_tools.world import (
    WorldModelTool as _WorldModelTool,
)
from naumi_agent.tools.analysis_tools.zkp import (
    ZKPTool as _ZKPTool,
)
from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)

_SOURCE_EXTENSIONS = analysis_common.SOURCE_EXTENSIONS
_read_sources = analysis_common.read_sources
_resolve_target = analysis_common.resolve_target
_router_unavailable = analysis_common.router_unavailable
_run_analysis = analysis_common.run_analysis
_scan_probe = _probe_support.scan_probe
_build_probe_script = _probe_support.build_probe_script
_build_probe_report = _probe_support.build_probe_report
_scan_autopsy = _autopsy_support.scan_autopsy
_build_autopsy_inventory_script = _autopsy_support.build_autopsy_inventory_script
_build_autopsy_report = _autopsy_support.build_autopsy_report
_scan_hook = _hook_support.scan_hook
_build_hook_inventory_script = _hook_support.build_hook_inventory_script
_build_hook_report = _hook_support.build_hook_report
_scan_vision = _vision_support.scan_vision
_build_vision_inventory_script = _vision_support.build_vision_inventory_script
_build_vision_report = _vision_support.build_vision_report
_build_spar_harness_script = _spar_support.build_spar_harness_script
_build_spar_report = _spar_support.build_spar_report
_scan_spar = _spar_support.scan_spar
_build_world_inventory_script = _world_support.build_world_inventory_script
_build_world_report = _world_support.build_world_report
_scan_world = _world_support.scan_world
_build_fusion_inventory_script = _fusion_support.build_fusion_inventory_script
_build_fusion_report = _fusion_support.build_fusion_report
_scan_fusion = _fusion_support.scan_fusion
_build_consensus_inventory_script = _consensus_support.build_consensus_inventory_script
_build_consensus_report = _consensus_support.build_consensus_report
_scan_consensus = _consensus_support.scan_consensus
_build_cosmos_inventory_script = _cosmos_support.build_cosmos_inventory_script
_build_cosmos_report = _cosmos_support.build_cosmos_report
_scan_cosmos = _cosmos_support.scan_cosmos
_build_pid_inventory_script = _pid_support.build_pid_inventory_script
_build_pid_report = _pid_support.build_pid_report
_scan_pid = _pid_support.scan_pid
_build_zkp_trace_script = _zkp_support.build_zkp_trace_script
_build_zkp_report = _zkp_support.build_zkp_report
_scan_zkp = _zkp_support.scan_zkp
_build_genesis_inventory_script = _genesis_support.build_genesis_inventory_script
_build_genesis_report = _genesis_support.build_genesis_report
_scan_genesis = _genesis_support.scan_genesis
_build_macro_inventory_script = _macro_support.build_macro_inventory_script
_build_macro_report = _macro_support.build_macro_report
_scan_macro = _macro_support.scan_macro
_build_watchdog_inventory_script = _watchdog_support.build_watchdog_inventory_script
_build_watchdog_report = _watchdog_support.build_watchdog_report
_scan_watchdog = _watchdog_support.scan_watchdog
_build_supervisor_inventory_script = (
    _supervisor_support.build_supervisor_inventory_script
)
_build_supervisor_report = _supervisor_support.build_supervisor_report
_scan_supervisor = _supervisor_support.scan_supervisor
_build_self_review_inventory_script = (
    _self_review_support.build_self_review_inventory_script
)
_build_self_review_report = _self_review_support.build_self_review_report
_scan_self_review = _self_review_support.scan_self_review
_find_agent_source_dir = _self_review_support.find_agent_source_dir
_build_page_inventory_script = _page_support.build_page_inventory_script
_build_page_report = _page_support.build_page_report
_scan_page = _page_support.scan_page
_build_sleep_inventory_script = _sleep_support.build_sleep_inventory_script
_build_sleep_report = _sleep_support.build_sleep_report
_scan_sleep = _sleep_support.scan_sleep
_scan_entropy = _entropy_support.scan_entropy
_build_entropy_anchor = _entropy_support.build_entropy_anchor
_scan_jit = _jit_support.scan_jit
JITBaseline = _jit_support.JITBaseline
_build_jit_baseline = _jit_support.build_jit_baseline
_format_jit_baseline = _jit_support.format_jit_baseline
VibeScaffold = _vibe_support.VibeScaffold
_build_vibe_scaffold = _vibe_support.build_vibe_scaffold
_write_vibe_scaffold = _vibe_support.write_vibe_scaffold
_format_vibe_scaffold = _vibe_support.format_vibe_scaffold
_scan_vibe_request = _vibe_support.scan_vibe_request
_scan_dspy = _dspy_support.scan_dspy
_build_dspy_baseline_metric = _dspy_support.build_dspy_baseline_metric
_format_dspy_report = _dspy_support.format_dspy_report
_scan_heal = _heal_support.scan_heal
_build_heal_report = _heal_support.build_heal_report
_scan_eval = _eval_support.scan_eval
EvalFunctionTarget = _eval_support.EvalFunctionTarget
EvalClassTarget = _eval_support.EvalClassTarget
EvalBaseline = _eval_support.EvalBaseline
_build_eval_baseline = _eval_support.build_eval_baseline
_format_eval_baseline = _eval_support.format_eval_baseline
_read_sources_for_ast = _static_modes_support.read_sources_for_ast
_format_static_scan_result = _static_modes_support.format_static_scan_result
_scan_chaos = _static_modes_support.scan_chaos
_scan_scale = _static_modes_support.scan_scale
_scan_state = _static_modes_support.scan_state
_scan_graph = _graph_support.scan_graph
_format_graph_report = _graph_support.format_graph_report
_scan_mcts = _mcts_support.scan_mcts
_build_mcts_decision_report = _mcts_support.build_mcts_decision_report
_extract_mcts_complexity = _mcts_support.extract_mcts_complexity
RouteExpert = _route_support.RouteExpert
_scan_route = _route_support.scan_route
_build_route_report = _route_support.build_route_report
_select_route_experts = _route_support.select_route_experts
_route_domain_focus = _route_support.route_domain_focus
_route_domain_recommendation = _route_support.route_domain_recommendation
_route_domain_concern = _route_support.route_domain_concern
_route_conflicts = _route_support.route_conflicts
_route_complexity = _route_support.route_complexity
_scan_speculate = _speculate_support.scan_speculate
_build_speculate_report = _speculate_support.build_speculate_report
_speculate_risky_files = _speculate_support.speculate_risky_files
_scan_pointer = _pointer_support.scan_pointer
_build_pointer_report = _pointer_support.build_pointer_report
_infer_pointer_table = _pointer_support.infer_pointer_table
_scan_cooe = _cooe_support.scan_cooe
_build_cooe_report = _cooe_support.build_cooe_report
_cooe_subtasks = _cooe_support.cooe_subtasks
_cooe_dag_lines = _cooe_support.cooe_dag_lines
_scan_ooda = _ooda_support.scan_ooda
_build_ooda_report = _ooda_support.build_ooda_report
_extract_ooda_resilience_score = _ooda_support.extract_ooda_resilience_score

# --- 各模式专用的静态扫描函数 ---


# ---------------------------------------------------------------------------
#  LLM Prompt 模板
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  工具类
# ---------------------------------------------------------------------------

class ChaosAnalysisTool(_ChaosAnalysisTool):
    """Compatibility wrapper for the split Chaos analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class ScaleAnalysisTool(_ScaleAnalysisTool):
    """Compatibility wrapper for the split Scale analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class StateAuditTool(_StateAuditTool):
    """Compatibility wrapper for the split State analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class VibeModeTool(_VibeModeTool):
    """Compatibility wrapper for the split Vibe analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


# ===========================================================================
#  /eval — EDD 评测驱动开发
# ===========================================================================

_EVAL_SYSTEM = """\
You are a ruthless QA engineer implementing Eval-Driven Development (EDD).

You have REAL static analysis evidence and the actual source code. Your task:

## Task
Generate a COMPLETE, RUNNABLE pytest test file that covers ALL edge cases.

## Rules
1. **Every function** must have at least one test.
2. **Every if/elif branch** must be tested (true AND false paths).
3. **Every raise/exception** must be tested with a `pytest.raises` block.
4. **External inputs** must be tested with: empty, None, wrong type, \
oversized, malicious (SQL injection, path traversal, etc.).
5. Tests must be INDEPENDENT (no shared mutable state between tests).
6. Import the target module correctly. Use proper fixtures if needed.
7. Output ONLY valid Python code — no markdown fences, no explanations \
outside the code.

## Output Format
- First line: `import pytest` and the target import
- Then the test functions
- Add a comment `# EDD: N test cases generated` at the top with the count
"""


class EvalDrivenTool(Tool):
    """EDD 评测驱动开发 — 静态扫描代码结构 + 生成可执行的 pytest 测试."""

    @property
    def name(self) -> str:
        return "analysis_eval"

    @property
    def description(self) -> str:
        return (
            "评测驱动开发(EDD)：分析目标代码的函数签名、条件分支、异常路径、"
            "外部输入点，自动生成覆盖所有边界情况的 pytest 测试代码。"
            "生成的测试可直接运行。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要生成测试的文件路径或目录路径",
                },
                "context": {
                    "type": "string",
                    "description": "补充上下文（功能描述、业务规则等）",
                    "default": "",
                },
            },
            "required": ["target"],
        }

    async def execute(self, *, target: str, context: str = "", **kwargs: Any) -> str:
        files = _resolve_target(target)
        if not files:
            return f"无法解析目标: {target} (请提供文件或目录路径)"

        source_text = _read_sources(files)
        scan_evidence = _scan_eval(files, source_text)
        baseline = _build_eval_baseline(files)
        deterministic = _format_eval_baseline(scan_evidence, baseline)

        router = _global_router
        if router is None:
            return deterministic + "\n\n模型路由未初始化，已返回可运行的 baseline pytest。"

        user_msg = (
            f"## 静态扫描证据\n{scan_evidence}\n\n"
            f"## Baseline Pytest\n{baseline.test_code}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if context:
            user_msg += f"\n## 业务规则/上下文\n{context}\n"

        enhanced = await _run_analysis(router, _EVAL_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM 边界测试增强\n" + enhanced


class MemoryPageTool(_MemoryPageTool):
    """Compatibility wrapper for the split memory page analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


# ===========================================================================
#  /heal — 自愈代码修复
# ===========================================================================

_HEAL_SYSTEM = """\
You are an immune cell in a self-healing code system.

You have:
1. A bug report / error log from the user
2. REAL static analysis evidence about error handling patterns
3. The actual source code

## Your Tasks

### 1. Diagnosis
- Identify the ROOT CAUSE (not just the symptom)
- Map the failure chain: what called what, where it broke
- Classify: logic bug / missing validation / race condition / \
external dependency failure / resource leak

### 2. Hotfix Code
- Provide the MINIMAL surgical fix (not a rewrite)
- The fix must be a drop-in replacement — show exact old_text → new_text
- Include defensive guards to prevent this class of bug from recurring

### 3. Immune Boost (Defensive Programming)
- Add validation at the boundary where bad data entered
- Add logging that would make this bug instantly diagnosable next time
- Add a regression test that would have caught this bug

### 4. Prevention Checklist
- What monitoring alert should be set up?
- What would a canary check look like?

Be surgical. The fix should change as few lines as possible while \
being bulletproof.
"""


class SelfHealTool(Tool):
    """自愈代码修复 — 分析错误日志 + 扫描代码防御模式 + 生成热修复代码."""

    @property
    def name(self) -> str:
        return "analysis_heal"

    @property
    def description(self) -> str:
        return (
            "自愈代码修复：分析错误日志，定位根因，生成最小化的热修复代码，"
            "并加入防御性编程逻辑防止同类错误再次发生。"
            "需要提供错误日志和对应的代码路径。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "error_log": {
                    "type": "string",
                    "description": "错误日志、异常堆栈或 Bug 描述",
                },
                "target": {
                    "type": "string",
                    "description": "相关代码的文件路径或目录路径",
                    "default": "",
                },
                "context": {
                    "type": "string",
                    "description": "补充上下文（发生场景、预期行为等）",
                    "default": "",
                },
            },
            "required": ["error_log"],
        }

    async def execute(
        self,
        *,
        error_log: str,
        target: str = "",
        context: str = "",
        **kwargs: Any,
    ) -> str:
        # 尝试从错误日志提取文件路径
        scan_evidence = ""
        files: list[Path] = []
        if target:
            files = _resolve_target(target)
        if not files:
            # 从错误栈中提取文件路径
            stack_paths = re.findall(r'File "([^"]+)"', error_log)
            for sp in stack_paths:
                p = Path(sp)
                if p.exists() and p.suffix in _SOURCE_EXTENSIONS:
                    files.append(p)

        source_text = ""
        if files:
            source_text = _read_sources(files)
            scan_evidence = _scan_heal(files, source_text, error_log)

        deterministic = _build_heal_report(error_log, scan_evidence, files)

        router = _global_router
        if router is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性自愈诊断。"

        user_msg = f"## 错误日志\n```\n{error_log}\n```\n"
        user_msg += f"\n## 确定性诊断\n{deterministic}\n"
        if scan_evidence:
            user_msg += f"\n## 静态扫描证据\n{scan_evidence}\n"
        if source_text:
            user_msg += f"\n## 相关源代码\n{source_text[:40000]}\n"
        if context:
            user_msg += f"\n## 补充上下文\n{context}\n"

        enhanced = await _run_analysis(router, _HEAL_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM 热修复增强\n" + enhanced


class DSPyTool(_DSPyTool):
    """Compatibility wrapper for the split DSPy analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
            resolve_target=_resolve_target,
            read_sources=_read_sources,
            cwd_getter=Path.cwd,
        )


class GraphRAGTool(_GraphRAGTool):
    """Compatibility wrapper for the split GraphRAG analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
            resolve_target=_resolve_target,
            read_sources=_read_sources,
            cwd_getter=Path.cwd,
        )


class MCTSTool(_MCTSTool):
    """Compatibility wrapper for the split MCTS analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
            resolve_target=_resolve_target,
            read_sources=_read_sources,
        )


class MoERouteTool(_MoERouteTool):
    """Compatibility wrapper for the split MoE route analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
            resolve_target=_resolve_target,
            read_sources=_read_sources,
            subagent_manager_getter=_get_analysis_subagent_manager,
        )


class SpeculateTool(_SpeculateTool):
    """Compatibility wrapper for the split speculate analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
            resolve_target=_resolve_target,
            read_sources=_read_sources,
        )


# ===========================================================================
#  /jit — JIT 即时沙盒工具生成
# ===========================================================================

_JIT_SYSTEM = """\
You are a JIT (Just-In-Time) Tool Generator. When pure LLM reasoning \
cannot guarantee correctness, you generate and present actual runnable \
code as your "external brain computation."

## Core Principle
**STOP guessing.** If the answer involves:
- Mathematical computation → write and trace through the code
- String manipulation with precise rules → write and test the code
- Data transformation → write and run the pipeline
- Algorithm correctness → implement and verify with test cases

## Your Tasks

### 1. Task Analysis
- State whether LLM reasoning alone is sufficient (confidence < 90% → use code)
- Identify the exact computation needed
- Declare the input/output contract

### 2. Code Generation
Generate a COMPLETE, RUNNABLE script:
- Language: Python (preferred for JIT) or C++ (if performance critical)
- Include all imports and setup
- Include test cases that verify correctness
- Include print statements that show the computation trace
- The code must be copy-paste-runnable (no missing dependencies)

### 3. Execution Trace
Simulate running the code mentally (or for straightforward cases, show \
output):
- Show the step-by-step computation
- Show intermediate values at key checkpoints
- Show the final result

### 4. Verification
- Provide at least 2 test cases with known correct answers
- Show that the code produces the expected output
- If any test fails, fix the code and re-run

### 5. Result
State the answer clearly, derived from the code's deterministic output, \
not from LLM reasoning.

Format:
```
## JIT Script
```python
# ... complete runnable code ...
```

## Execution Result
```
# ... actual or simulated output ...
```

## Verified Answer
Based on the code output: [clear answer]
```
"""


class JITTool(Tool):
    """JIT 即时沙盒工具生成 — 停止玄学推理，用代码保证确定性."""

    @property
    def name(self) -> str:
        return "analysis_jit"

    @property
    def description(self) -> str:
        return (
            "JIT 即时工具生成：当 LLM 推理无法保证准确性时，"
            "立即生成可运行的 Python/C++ 脚本，"
            "展示代码作为\"外置大脑计算过程\"，"
            "基于代码的确定性结果回答问题。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "需要计算验证的任务描述",
                },
                "context": {
                    "type": "string",
                    "description": "补充上下文（已知条件、约束等）",
                    "default": "",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self,
        *,
        task: str,
        context: str = "",
        **kwargs: Any,
    ) -> str:
        scan_evidence = _scan_jit(task)
        baseline = _build_jit_baseline(task, context)
        deterministic = _format_jit_baseline(scan_evidence, baseline)

        router = _global_router
        if router is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 JIT 脚本。"

        user_msg = f"## 任务\n{task}\n"
        user_msg += f"\n## JIT 扫描分析\n{scan_evidence}\n"
        user_msg += f"\n## Baseline Script\n{baseline.script}\n"
        user_msg += f"\n## Baseline Execution\n{baseline.execution_output}\n"
        if context:
            user_msg += f"\n## 补充上下文\n{context}\n"

        enhanced = await _run_analysis(router, _JIT_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM JIT 增强\n" + enhanced


class PointerTool(_PointerTool):
    """Compatibility wrapper for the split pointer analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
            resolve_target=_resolve_target,
            read_sources=_read_sources,
        )


class COOETool(_COOETool):
    """Compatibility wrapper for the split COOE analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
            resolve_target=_resolve_target,
            read_sources=_read_sources,
        )


class SleepPruningTool(_SleepPruningTool):
    """Compatibility wrapper for the split sleep analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
            resolve_target=_resolve_target,
            read_sources=_read_sources,
        )


class EntropyValveTool(_EntropyValveTool):
    """Compatibility wrapper for the split entropy analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class OODATool(_OODATool):
    """Compatibility wrapper for the split OODA analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
            resolve_target=_resolve_target,
            read_sources=_read_sources,
        )


class ProbeTool(_ProbeTool):
    """Compatibility wrapper for the split probe analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class HookTool(_HookTool):
    """Compatibility wrapper for the split hook analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class VisionTool(_VisionTool):
    """Compatibility wrapper for the split vision analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class SparTool(_SparTool):
    """Compatibility wrapper for the split SPAR analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
            subagent_manager_getter=_get_analysis_subagent_manager,
        )


class WorldModelTool(_WorldModelTool):
    """Compatibility wrapper for the split world-model analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class FusionTool(_FusionTool):
    """Compatibility wrapper for the split fusion analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class ConsensusTool(_ConsensusTool):
    """Compatibility wrapper for the split consensus analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class PIDTool(_PIDTool):
    """Compatibility wrapper for the split PID analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class ZKPTool(_ZKPTool):
    """Compatibility wrapper for the split ZKP analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class GenesisTool(_GenesisTool):
    """Compatibility wrapper for the split Genesis analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class MacroTool(_MacroTool):
    """Compatibility wrapper for the split Macro analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class CosmosTool(_CosmosTool):
    """Compatibility wrapper for the split Cosmos analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class WatchdogTool(_WatchdogTool):
    """Compatibility wrapper for the split Watchdog analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


class SupervisorTool(_SupervisorTool):
    """Compatibility wrapper for the split Supervisor analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
            subagent_manager_getter=_get_analysis_subagent_manager,
        )


class AutopsyTool(_AutopsyTool):
    """Compatibility wrapper for the split Autopsy analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
        )


# ---------------------------------------------------------------------------
#  内部基础设施



_global_router: ModelPort | None = None
_global_subagent_manager: Any = None


def set_analysis_router(router: ModelPort) -> None:
    """注入 ModelPort 实例，供工具内部调用 LLM."""
    global _global_router
    _global_router = router


def set_analysis_subagent_manager(manager: Any) -> None:
    """注入 SubAgentManager 实例，供工具内部调度子 Agent."""
    global _global_subagent_manager
    _global_subagent_manager = manager


def clear_analysis_subagent_manager(manager: Any | None = None) -> None:
    """Clear the global SubAgentManager when the owning engine shuts down."""
    global _global_subagent_manager
    if manager is None or _global_subagent_manager is manager:
        _global_subagent_manager = None


def _get_analysis_subagent_manager(router: ModelPort) -> Any | None:
    """Return a subagent manager only when it belongs to the active router."""
    manager = _global_subagent_manager
    if manager is None:
        return None
    engine = getattr(manager, "_engine", None)
    if engine is not None and getattr(engine, "_router", None) is not router:
        return None
    return manager


class SelfReviewTool(_SelfReviewTool):
    """Compatibility wrapper for the split Self-Review analysis tool."""

    def __init__(self) -> None:
        super().__init__(
            router_getter=lambda: _global_router,
            run_analysis=_run_analysis,
            source_dir_getter=_find_agent_source_dir,
        )


def create_analysis_tools() -> list[Tool]:
    """创建所有分析模式工具."""
    return [
        ChaosAnalysisTool(),
        ScaleAnalysisTool(),
        StateAuditTool(),
        VibeModeTool(),
        EvalDrivenTool(),
        MemoryPageTool(),
        SelfHealTool(),
        DSPyTool(),
        GraphRAGTool(),
        MCTSTool(),
        MoERouteTool(),
        SpeculateTool(),
        JITTool(),
        PointerTool(),
        COOETool(),
        SleepPruningTool(),
        EntropyValveTool(),
        OODATool(),
        ProbeTool(),
        HookTool(),
        VisionTool(),
        SparTool(),
        WorldModelTool(),
        FusionTool(),
        ConsensusTool(),
        PIDTool(),
        ZKPTool(),
        GenesisTool(),
        MacroTool(),
        CosmosTool(),
        WatchdogTool(),
        SupervisorTool(),
        AutopsyTool(),
        SelfReviewTool(),
    ]
