"""Goal Pursuit Loop — autonomous long-running goal execution.

The core idea: given a goal, the agent runs a persistent loop that:
1. Parses the goal into measurable success criteria
2. Assesses current state against criteria
3. Plans concrete actions to close gaps
4. Executes actions using available tools and sub-agents
5. Objectively verifies results (tests compile, tests pass, files exist)
6. Repeats until ALL criteria are met, budget exhausted, or stuck

This is NOT a demo generator. The loop runs until true success or honest failure.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from naumi_agent.model.router import ModelRouter
    from naumi_agent.orchestrator.subagent_manager import SubAgentManager
    from naumi_agent.tools.base import ToolRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Data structures
# ---------------------------------------------------------------------------


class GoalStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    ACHIEVED = "achieved"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    STUCK = "stuck"
    CANCELLED = "cancelled"


class CriterionStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    VERIFIED = "verified"
    FAILED = "failed"


@dataclass
class SuccessCriterion:
    """One measurable success criterion."""

    id: str
    description: str
    verification_command: str  # shell command or tool call to verify
    status: CriterionStatus = CriterionStatus.NOT_STARTED
    evidence: str = ""
    last_checked: float = 0.0


@dataclass
class GoalSpec:
    """Parsed goal specification."""

    original_goal: str
    description: str
    success_criteria: list[SuccessCriterion]
    constraints: dict[str, Any]
    estimated_complexity: str = "M"  # S/M/L/XL


@dataclass
class IterationCheckpoint:
    """Snapshot of one iteration."""

    iteration: int
    timestamp: float
    assessment: str
    gaps_found: list[str]
    actions_planned: list[str]
    actions_taken: list[str]
    verification_results: list[dict[str, Any]]
    criteria_status: dict[str, str]
    convergence_score: float  # 0.0-1.0
    tokens_used: int = 0
    cost_usd: float = 0.0


@dataclass
class PursuitConfig:
    """Configuration for the pursuit loop."""

    max_iterations: int = 30
    max_budget_usd: float = 5.0
    max_time_seconds: float = 1800.0  # 30 min
    stagnation_threshold: int = 3  # consecutive iterations with no progress
    verify_interval: int = 1  # verify every N iterations
    plan_depth: int = 3  # how many steps to plan ahead
    replan_on_stagnation: bool = True


# ---------------------------------------------------------------------------
#  LLM prompts
# ---------------------------------------------------------------------------

_GOAL_PARSER_SYSTEM = """\
You are a goal decomposition specialist. Given a natural language goal, you produce:

1. A clear, precise description of what "done" looks like
2. 3-7 measurable success criteria, each with a verification command
3. Constraints (scope, time, resources)

## Output Format (STRICT — follow exactly)

### Description
<one paragraph describing the end state>

### Criteria
CRITERION|<id>|<description>|<verification_command>
CRITERION|<id>|<description>|<verification_command>
...

### Constraints
- <constraint>
...

### Complexity
<S/M/L/XL>

Only output the sections above. No extra commentary.
"""

_ASSESSOR_SYSTEM = """\
You are a progress assessor. Given a goal, success criteria, and the current state,
you objectively evaluate what has been achieved and what gaps remain.

## Rules
- Be brutally honest. Do NOT mark something as done if it's only partially done.
- A criterion is VERIFIED only if you have hard evidence (test output, file content, etc.)
- A criterion is FAILED if the last attempt clearly didn't work
- Otherwise it's IN_PROGRESS or NOT_STARTED

## Output Format

### Current State
<what actually exists right now>

### Criteria Assessment
ASSESS|<criterion_id>|<status: verified|in_progress|failed|not_started>|<evidence>

### Gaps
GAP|<description of what's missing>

### Convergence
CONVERGENCE|<0.0 to 1.0>

Be precise. "It looks like..." is not evidence. Show actual output or file contents.
"""

_PLANNER_SYSTEM = """\
You are an action planner. Given a goal, success criteria, current gaps, and available
tools, you produce a concrete plan of 1-5 actions to close the gaps.

## Rules
- Each action must be SPECIFIC (which file to edit, what test to write, etc.)
- Each action must be VERIFIABLE (you can check if it worked)
- Do NOT plan actions that are already done
- Focus on the BIGGEST gaps first
- If stuck, try a COMPLETELY DIFFERENT approach

## Output Format
ACTION|<id>|<description>|<tool_to_use>|<expected_result>

Example:
ACTION|a1|Create src/utils.py with parse_config|file_write|function exists
ACTION|a2|Write tests in tests/test_utils.py|file_write|tests pass
ACTION|a3|Run pytest to verify|bash_run|All tests pass
"""

_STAGNATION_RECOVERY_SYSTEM = """\
You are a stagnation recovery specialist. The pursuit loop has detected that progress
has stalled — the same gaps keep appearing despite multiple attempts.

## Your Task
1. Analyze WHY previous attempts failed
2. Identify the ROOT CAUSE (wrong approach? missing dependency? wrong abstraction?)
3. Propose a COMPLETELY DIFFERENT strategy

## Rules
- Do NOT repeat the same approach that failed
- Consider: breaking the problem into smaller pieces, using a different tool,
  consulting external documentation, starting from a working example
- Be creative but practical

## Output Format
RECOVERY|<root_cause_analysis>
STRATEGY|<new_approach_description>
ACTION|<id>|<description>|<tool_to_use>|<expected_result>
"""

_FINAL_REPORT_SYSTEM = """\
You are a final report generator. Given the full pursuit history, produce an honest,
comprehensive report in Chinese (中文).

## Report must include:
1. 目标回顾 — original goal
2. 最终状态 — achieved / partially achieved / failed
3. 成功标准达成情况 — each criterion and its final status
4. 关键行动回顾 — major actions taken
5. 遇到的障碍与应对 — obstacles encountered and how they were handled
6. 残余问题 — what's still not perfect (be honest)
7. 总消耗 — tokens, cost, iterations, time

Be factual. Do not inflate accomplishments. If something is only partially done, say so.
"""


# ---------------------------------------------------------------------------
#  Pursuit Loop
# ---------------------------------------------------------------------------


class GoalPursuitLoop:
    """Autonomous goal pursuit with verification-driven iteration."""

    def __init__(
        self,
        router: ModelRouter,
        tool_registry: ToolRegistry,
        subagent_manager: SubAgentManager,
        config: PursuitConfig | None = None,
    ) -> None:
        self._router = router
        self._tools = tool_registry
        self._manager = subagent_manager
        self._config = config or PursuitConfig()
        self._history: list[IterationCheckpoint] = []
        self._start_time = 0.0
        self._total_tokens = 0
        self._total_cost = 0.0
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation of the running loop."""
        self._cancelled = True

    async def pursue(self, goal: str) -> str:
        """Execute the full goal pursuit loop.

        Returns a final report string in Chinese.
        """
        self._start_time = time.time()
        self._history.clear()
        self._total_tokens = 0
        self._total_cost = 0.0
        self._cancelled = False

        # Phase 0: Parse the goal
        spec = await self._parse_goal(goal)
        logger.info(
            "Goal parsed: %d criteria, complexity=%s",
            len(spec.success_criteria), spec.estimated_complexity,
        )

        iteration = 0
        status = GoalStatus.IN_PROGRESS

        while status == GoalStatus.IN_PROGRESS:
            iteration += 1

            # Safety checks
            if self._cancelled:
                status = GoalStatus.CANCELLED
                break

            elapsed = time.time() - self._start_time
            if elapsed > self._config.max_time_seconds:
                status = GoalStatus.BUDGET_EXCEEDED
                break

            if self._total_cost >= self._config.max_budget_usd:
                status = GoalStatus.BUDGET_EXCEEDED
                break

            if iteration > self._config.max_iterations:
                status = GoalStatus.BUDGET_EXCEEDED
                break

            # Phase 1: Assess current state
            assessment = await self._assess(spec)
            checkpoint = assessment["checkpoint"]
            self._history.append(checkpoint)

            logger.info(
                "Iteration %d: convergence=%.2f, gaps=%d, tokens=%d",
                iteration, checkpoint.convergence_score,
                len(checkpoint.gaps_found), self._total_tokens,
            )

            # Check if all criteria are verified
            all_verified = all(
                c.status == CriterionStatus.VERIFIED
                for c in spec.success_criteria
            )
            if all_verified:
                # Final verification pass — double check
                if await self._final_verification(spec):
                    status = GoalStatus.ACHIEVED
                    break

            # Check convergence (are we making progress?)
            if self._is_stagnant():
                if self._config.replan_on_stagnation:
                    logger.warning(
                        "Stagnation detected at iteration %d", iteration,
                    )
                    recovery = await self._recover_from_stagnation(
                        spec, checkpoint,
                    )
                    await self._execute_actions(spec, recovery)
                    continue
                else:
                    status = GoalStatus.STUCK
                    break

            # Phase 2: Plan next actions
            actions = await self._plan(spec, checkpoint)
            if not actions:
                status = GoalStatus.STUCK
                break

            # Phase 3: Execute actions
            await self._execute_actions(spec, actions)

            # Phase 4: Verify (if interval matches)
            if iteration % self._config.verify_interval == 0:
                await self._verify_criteria(spec)

        # Generate final report
        report = await self._generate_report(spec, status)
        return report

    # ------------------------------------------------------------------
    #  Phase 0: Goal parsing
    # ------------------------------------------------------------------

    async def _parse_goal(self, goal: str) -> GoalSpec:
        """Use LLM to parse a natural language goal into GoalSpec."""
        response = await self._llm_call(
            _GOAL_PARSER_SYSTEM, f"目标: {goal}",
        )

        # Parse criteria
        criteria: list[SuccessCriterion] = []
        for line in response.splitlines():
            if line.startswith("CRITERION|"):
                parts = line.split("|")
                if len(parts) >= 4:
                    criteria.append(SuccessCriterion(
                        id=parts[1].strip(),
                        description=parts[2].strip(),
                        verification_command=parts[3].strip(),
                    ))

        if not criteria:
            # Fallback: create generic criteria
            criteria = [
                SuccessCriterion(
                    id="c1",
                    description=goal[:200],
                    verification_command=f"验证目标 '{goal[:100]}' 已完成",
                ),
            ]

        # Parse constraints
        constraints: dict[str, Any] = {}
        for line in response.splitlines():
            if line.startswith("- "):
                constraints[line[2:].strip()] = True

        # Parse complexity
        complexity = "M"
        for line in response.splitlines():
            if "Complexity" in line:
                for level in ("S", "M", "L", "XL"):
                    if level in line.split()[-1] if line.split() else "":
                        complexity = level

        # Parse description
        desc_lines: list[str] = []
        in_desc = False
        for line in response.splitlines():
            if "### Description" in line:
                in_desc = True
                continue
            if line.startswith("###") and in_desc:
                break
            if in_desc and line.strip():
                desc_lines.append(line.strip())

        return GoalSpec(
            original_goal=goal,
            description="\n".join(desc_lines) if desc_lines else goal,
            success_criteria=criteria,
            constraints=constraints,
            estimated_complexity=complexity,
        )

    # ------------------------------------------------------------------
    #  Phase 1: Assessment
    # ------------------------------------------------------------------

    async def _assess(self, spec: GoalSpec) -> dict[str, Any]:
        """Assess current state against success criteria."""
        # Gather current state evidence
        state_evidence = await self._gather_state_evidence(spec)

        criteria_text = "\n".join(
            f"- [{c.status.value}] {c.id}: {c.description}"
            for c in spec.success_criteria
        )

        history_summary = ""
        if self._history:
            last = self._history[-1]
            history_summary = (
                f"上一轮 ({last.iteration}) 状态:\n"
                f"- 评估: {last.assessment[:500]}\n"
                f"- 差距: {', '.join(last.gaps_found[:5])}\n"
                f"- 收敛度: {last.convergence_score:.2f}\n"
            )

        user_msg = (
            f"## 目标\n{spec.description}\n\n"
            f"## 成功标准\n{criteria_text}\n\n"
            f"## 当前状态证据\n{state_evidence}\n\n"
        )
        if history_summary:
            user_msg += f"{history_summary}\n"
        user_msg += "请客观评估当前进度。"

        response = await self._llm_call(_ASSESSOR_SYSTEM, user_msg)

        # Parse assessment
        gaps: list[str] = []
        convergence = 0.0
        criteria_updates: dict[str, tuple[CriterionStatus, str]] = {}

        for line in response.splitlines():
            if line.startswith("ASSESS|"):
                parts = line.split("|")
                if len(parts) >= 4:
                    cid = parts[1].strip()
                    status_str = parts[2].strip().lower()
                    evidence = parts[3].strip()
                    status_map = {
                        "verified": CriterionStatus.VERIFIED,
                        "in_progress": CriterionStatus.IN_PROGRESS,
                        "failed": CriterionStatus.FAILED,
                        "not_started": CriterionStatus.NOT_STARTED,
                    }
                    if status_str in status_map:
                        criteria_updates[cid] = (
                            status_map[status_str], evidence,
                        )

            elif line.startswith("GAP|"):
                gaps.append(line[4:].strip())

            elif line.startswith("CONVERGENCE|"):
                try:
                    convergence = float(line.split("|")[1].strip())
                except (ValueError, IndexError):
                    convergence = 0.0

        # Apply updates
        for c in spec.success_criteria:
            if c.id in criteria_updates:
                c.status, c.evidence = criteria_updates[c.id]
                c.last_checked = time.time()

        checkpoint = IterationCheckpoint(
            iteration=len(self._history) + 1,
            timestamp=time.time(),
            assessment=response[:2000],
            gaps_found=gaps,
            actions_planned=[],
            actions_taken=[],
            verification_results=[],
            criteria_status={
                c.id: c.status.value for c in spec.success_criteria
            },
            convergence_score=max(0.0, min(1.0, convergence)),
            tokens_used=self._total_tokens,
            cost_usd=self._total_cost,
        )

        return {"checkpoint": checkpoint, "gaps": gaps}

    # ------------------------------------------------------------------
    #  Phase 2: Planning
    # ------------------------------------------------------------------

    async def _plan(
        self, spec: GoalSpec, checkpoint: IterationCheckpoint,
    ) -> list[dict[str, str]]:
        """Plan next actions to close gaps."""
        gaps_text = "\n".join(f"- {g}" for g in checkpoint.gaps_found)
        criteria_text = "\n".join(
            f"- [{c.status.value}] {c.id}: {c.description}"
            for c in spec.success_criteria
        )

        available_tools = ", ".join(sorted(self._tools.names))

        user_msg = (
            f"## 目标\n{spec.description}\n\n"
            f"## 成功标准\n{criteria_text}\n\n"
            f"## 待解决差距\n{gaps_text}\n\n"
            f"## 可用工具\n{available_tools}\n\n"
            f"## 上轮收敛建度\n{checkpoint.convergence_score:.2f}\n\n"
            "请规划 1-5 个具体行动来缩小差距。"
        )

        response = await self._llm_call(_PLANNER_SYSTEM, user_msg)

        actions: list[dict[str, str]] = []
        for line in response.splitlines():
            if line.startswith("ACTION|"):
                parts = line.split("|")
                if len(parts) >= 5:
                    actions.append({
                        "id": parts[1].strip(),
                        "description": parts[2].strip(),
                        "tool": parts[3].strip(),
                        "expected": parts[4].strip(),
                    })

        return actions

    # ------------------------------------------------------------------
    #  Phase 3: Execution
    # ------------------------------------------------------------------

    async def _execute_actions(
        self, spec: GoalSpec, actions: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Execute planned actions using sub-agents."""
        results: list[dict[str, Any]] = []

        for action in actions:
            if self._cancelled:
                break

            tool_name = action["tool"]
            description = action["description"]

            logger.info("Executing action %s: %s", action["id"], description)

            # Try to use the tool directly from registry
            tool = self._tools.get(tool_name)
            if tool:
                try:
                    result = await tool.execute(
                        task=description, target=description,
                    )
                    results.append({
                        "action_id": action["id"],
                        "status": "completed",
                        "output": str(result)[:3000],
                    })
                except Exception as e:
                    results.append({
                        "action_id": action["id"],
                        "status": "error",
                        "output": f"{type(e).__name__}: {e}",
                    })
            else:
                # Delegate to sub-agent
                agent_name = f"pursuit_{action['id']}"
                self._manager.spawn_for_task(
                    name=agent_name,
                    task_description=description,
                    max_turns=5,
                    max_budget_usd=0.3,
                )
                try:
                    from naumi_agent.orchestrator.subagent_manager import SubTask

                    subtask = SubTask(
                        id=action["id"],
                        description=description,
                        agent_name=agent_name,
                    )
                    agent_result = await self._manager.delegate(subtask)
                    results.append({
                        "action_id": action["id"],
                        "status": agent_result.status,
                        "output": (agent_result.response or "")[:3000],
                        "tokens": agent_result.total_tokens,
                        "cost": agent_result.total_cost_usd,
                    })
                    self._total_tokens += agent_result.total_tokens
                    self._total_cost += agent_result.total_cost_usd
                except Exception as e:
                    results.append({
                        "action_id": action["id"],
                        "status": "error",
                        "output": f"{type(e).__name__}: {e}",
                    })
                finally:
                    self._manager.destroy(agent_name)

        return results

    # ------------------------------------------------------------------
    #  Phase 4: Verification
    # ------------------------------------------------------------------

    async def _verify_criteria(self, spec: GoalSpec) -> None:
        """Run verification commands for each criterion."""
        for criterion in spec.success_criteria:
            if criterion.status == CriterionStatus.VERIFIED:
                continue

            cmd = criterion.verification_command
            if not cmd:
                continue

            # Try to run the verification command
            try:
                bash_tool = self._tools.get("bash_run")
                if bash_tool:
                    output = await bash_tool.execute(command=cmd)
                    passed = (
                        "error" not in output.lower()
                        and "fail" not in output.lower()
                        and "traceback" not in output.lower()
                    )
                    if passed:
                        criterion.status = CriterionStatus.VERIFIED
                        criterion.evidence = f"Command output: {str(output)[:500]}"
                    else:
                        criterion.evidence = f"Failed: {str(output)[:500]}"
                    criterion.last_checked = time.time()
            except Exception as e:
                criterion.evidence = f"Verification error: {e}"

    async def _final_verification(self, spec: GoalSpec) -> bool:
        """Double-check all criteria are truly met."""
        await self._verify_criteria(spec)
        return all(
            c.status == CriterionStatus.VERIFIED
            for c in spec.success_criteria
        )

    # ------------------------------------------------------------------
    #  Stagnation detection & recovery
    # ------------------------------------------------------------------

    def _is_stagnant(self) -> bool:
        """Check if recent iterations show no progress."""
        if len(self._history) < self._config.stagnation_threshold:
            return False

        recent = self._history[-self._config.stagnation_threshold:]
        scores = [cp.convergence_score for cp in recent]

        # Stagnant if convergence hasn't improved at all
        if all(s == scores[0] for s in scores) and scores[0] < 1.0:
            return True

        # Stagnant if convergence decreased
        if recent[-1].convergence_score <= recent[0].convergence_score:
            return True

        return False

    async def _recover_from_stagnation(
        self, spec: GoalSpec, checkpoint: IterationCheckpoint,
    ) -> list[dict[str, str]]:
        """Generate a recovery plan when stuck."""
        recent_actions: list[str] = []
        for cp in self._history[-3:]:
            recent_actions.extend(cp.actions_planned[:3])

        user_msg = (
            f"## 目标\n{spec.description}\n\n"
            f"## 当前差距\n"
            + "\n".join(f"- {g}" for g in checkpoint.gaps_found)
            + "\n\n## 最近尝试过的行动\n"
            + "\n".join(f"- {a}" for a in recent_actions[-10:])
            + "\n\n这些行动都没有奏效。请分析根因并提出全新的策略。"
        )

        response = await self._llm_call(_STAGNATION_RECOVERY_SYSTEM, user_msg)

        actions: list[dict[str, str]] = []
        for line in response.splitlines():
            if line.startswith("ACTION|"):
                parts = line.split("|")
                if len(parts) >= 5:
                    actions.append({
                        "id": parts[1].strip(),
                        "description": parts[2].strip(),
                        "tool": parts[3].strip(),
                        "expected": parts[4].strip(),
                    })

        return actions

    # ------------------------------------------------------------------
    #  Helpers
    # ------------------------------------------------------------------

    async def _gather_state_evidence(self, spec: GoalSpec) -> str:
        """Gather real evidence about current state."""
        evidence_parts: list[str] = []

        # Check existing files related to the goal
        bash_tool = self._tools.get("bash_run")
        if bash_tool:
            try:
                # List relevant files
                ls_result = await bash_tool.execute(
                    command="find . -name '*.py' -newer . -not -path './.*' "
                    "-not -path './__pycache__/*' | head -30",
                )
                evidence_parts.append(f"最近修改的 Python 文件:\n{ls_result}")
            except Exception:
                pass

            try:
                # Check test status
                test_result = await bash_tool.execute(
                    command="python -m pytest tests/ -q --tb=no 2>&1 | tail -5",
                )
                evidence_parts.append(f"测试状态:\n{test_result}")
            except Exception:
                pass

            try:
                # Check lint status
                lint_result = await bash_tool.execute(
                    command="ruff check src/ 2>&1 | tail -5",
                )
                evidence_parts.append(f"Lint 状态:\n{lint_result}")
            except Exception:
                pass

        # Include previous action results
        if self._history:
            last = self._history[-1]
            if last.actions_taken:
                evidence_parts.append(
                    "上轮执行结果:\n" + "\n".join(last.actions_taken[:5])
                )

        return "\n\n".join(evidence_parts) if evidence_parts else "暂无状态证据"

    async def _llm_call(
        self, system_prompt: str, user_msg: str,
    ) -> str:
        """Make an LLM call and track usage."""
        from naumi_agent.model.router import ModelTier

        response = await self._router.call(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            tier=ModelTier.CAPABLE,
            max_tokens=4096,
            temperature=0.3,
        )
        self._total_tokens += response.usage.total_tokens
        self._total_cost += response.usage.cost_usd
        return response.content

    async def _generate_report(
        self, spec: GoalSpec, status: GoalStatus,
    ) -> str:
        """Generate the final pursuit report."""
        elapsed = time.time() - self._start_time
        iterations = len(self._history)

        criteria_summary = "\n".join(
            f"- [{c.status.value}] {c.id}: {c.description}"
            + (f"\n  证据: {c.evidence[:200]}" if c.evidence else "")
            for c in spec.success_criteria
        )

        history_summary = ""
        for cp in self._history[-5:]:
            history_summary += (
                f"### 第 {cp.iteration} 轮\n"
                f"- 收敛度: {cp.convergence_score:.2f}\n"
                f"- 差距: {', '.join(cp.gaps_found[:3])}\n"
                f"- Token: {cp.tokens_used}, 成本: ${cp.cost_usd:.4f}\n\n"
            )

        user_msg = (
            f"## 原始目标\n{spec.original_goal}\n\n"
            f"## 最终状态: {status.value}\n\n"
            f"## 成功标准\n{criteria_summary}\n\n"
            f"## 执行统计\n"
            f"- 总轮次: {iterations}\n"
            f"- 总 Token: {self._total_tokens}\n"
            f"- 总成本: ${self._total_cost:.4f}\n"
            f"- 总耗时: {elapsed:.1f} 秒\n\n"
            f"## 最近轮次历史\n{history_summary}\n"
            "请生成最终的完整报告。"
        )

        return await self._llm_call(_FINAL_REPORT_SYSTEM, user_msg)
