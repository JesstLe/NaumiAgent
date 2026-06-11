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

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from naumi_agent.tools.base import ToolCall, ToolResult

if TYPE_CHECKING:
    from naumi_agent.model.router import ModelRouter
    from naumi_agent.orchestrator.pursuit_store import PursuitStore
    from naumi_agent.orchestrator.subagent_manager import SubAgentManager
    from naumi_agent.tools.base import ToolRegistry

logger = logging.getLogger(__name__)

ToolExecutor = Callable[[ToolCall], Awaitable[ToolResult]]

_EXIT_CODE_RE = re.compile(r"\[exit code:\s*(-?\d+)\]", re.IGNORECASE)
_LEGACY_FAILURE_RE = re.compile(
    r"(^|\n)\s*(error:|traceback\b|fail\b)|\bnot found\b",
    re.IGNORECASE,
)


def _verification_command_passed(output: Any) -> bool:
    """Return whether a shell verification output represents success."""
    text = str(output)
    matches = _EXIT_CODE_RE.findall(text)
    if matches:
        return int(matches[-1]) == 0
    return _LEGACY_FAILURE_RE.search(text) is None

# ---------------------------------------------------------------------------
#  Data structures
# ---------------------------------------------------------------------------


class GoalStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    ACHIEVED = "achieved"
    FAILED = "failed"
    WAITING = "waiting"
    BUDGET_EXCEEDED = "budget_exceeded"
    STUCK = "stuck"
    CANCELLED = "cancelled"


class CriterionStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    VERIFIED = "verified"
    FAILED = "failed"


class PursuitRunStatus(StrEnum):
    """Durable status for one pursuit run."""

    RUNNING = "running"
    WAITING = "waiting"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"


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
class PursuitEvidence:
    """One concrete evidence item collected during pursuit."""

    kind: str
    source: str
    summary: str
    is_hard: bool
    timestamp: float = 0.0


@dataclass
class PursuitBackgroundWait:
    """One background task the pursuit loop is waiting on."""

    task_id: str
    action_id: str
    command: str
    created_at: float


@dataclass
class PursuitStopDecision:
    """Programmatic stop decision for a pursuit run."""

    status: PursuitRunStatus
    reason: str
    evidence: list[PursuitEvidence]


@dataclass
class PursuitRun:
    """Live state snapshot for a pursuit execution."""

    id: str
    goal: str
    status: PursuitRunStatus
    phase: str
    started_at: float
    updated_at: float
    iteration: int = 0
    criteria_total: int = 0
    criteria_verified: int = 0
    failure_count: int = 0
    blocked_reason: str = ""
    next_action: str = ""
    worktree_name: str = ""
    worktree_path: str = ""
    waiting_on: list[PursuitBackgroundWait] | None = None
    evidence: list[PursuitEvidence] | None = None

    def add_evidence(self, item: PursuitEvidence) -> None:
        if self.evidence is None:
            self.evidence = []
        self.evidence.append(item)
        self.updated_at = time.time()


@dataclass
class PursuitConfig:
    """Configuration for the pursuit loop."""

    max_iterations: int = 1000
    max_budget_usd: float = float("inf")
    max_time_seconds: float = float("inf")
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

## Available Tools (use these exact names)
- **file_write** — Create or overwrite a file with complete content.
  Params: path, content. BEST for creating new files.
- **file_edit** — Edit existing file with search/replace.
  Params: path, old_text, new_text. BEST for modifying existing files.
- **file_read** — Read a file's content (params: path)
- **bash_run** — Shell commands ONLY for: tests, verification,
  installing packages, reading files

## Rules
- Each action must be SPECIFIC (which file, what content, which command)
- The description MUST contain the target file path (e.g. config.yaml, src/main.py)
- Use **file_write** to CREATE new files — the system generates complete content
- Use **file_edit** to MODIFY existing files — the system generates search/replace
- Use **bash_run** ONLY for: pytest, ruff check, pip install, cat/grep/ls, verification commands
- NEVER use bash_run for creating or editing source files — use file_write/file_edit instead
- Do NOT plan actions that are already done
- Focus on the BIGGEST gaps first

## Output Format
ACTION|<id>|<description>|<tool_name>|<expected_result>

Example:
ACTION|a1|Create src/utils.py with parse_config|file_write|file exists
ACTION|a2|Add import to src/main.py|file_edit|import added
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
        store: PursuitStore | None = None,
        config: PursuitConfig | None = None,
        execute_tool_call: ToolExecutor | None = None,
    ) -> None:
        self._router = router
        self._tools = tool_registry
        self._manager = subagent_manager
        self._store = store
        self._config = config or PursuitConfig()
        self._execute_tool_call = execute_tool_call
        self._history: list[IterationCheckpoint] = []
        self._start_time = 0.0
        self._total_tokens = 0
        self._total_cost = 0.0
        self._cancelled = False
        self._run: PursuitRun | None = None
        self._last_stop_decision: PursuitStopDecision | None = None
        self._pending_background: list[PursuitBackgroundWait] = []

    def cancel(self) -> None:
        """Request cancellation of the running loop."""
        self._cancelled = True
        if self._run is not None:
            self._run.status = PursuitRunStatus.CANCELLED
            self._run.updated_at = time.time()
            self._persist_run()

    def _persist_run(self) -> None:
        """Persist current run state if a store is configured."""
        if self._store is None or self._run is None:
            return
        self._store.save_run(self._run)

    def list_persisted_runs(self, *, include_finished: bool = True) -> list[PursuitRun]:
        """List persisted pursuit runs."""
        if self._store is None:
            return []
        return self._store.list_runs(include_finished=include_finished)

    def get_persisted_run(self, run_id: str) -> PursuitRun | None:
        """Return one persisted pursuit run."""
        if self._store is None:
            return None
        return self._store.get_run(run_id)

    async def resume_persisted(self, run_id: str) -> str:
        """Restore a persisted run and collect finished background results."""
        if self._store is None:
            return "错误：目标追踪持久化存储未初始化。"
        run = self._store.get_run(run_id)
        if run is None:
            return f"错误：目标追踪运行不存在：{run_id}"

        self._run = run
        self._pending_background = list(run.waiting_on or [])
        if self._pending_background:
            await self._collect_background_results()

        if self._pending_background:
            self._record_waiting("目标追踪仍在等待后台任务完成。")
        elif self._run.status == PursuitRunStatus.WAITING:
            self._run.status = PursuitRunStatus.RUNNING
            self._run.phase = "assess"
            self._run.waiting_on = []
            self._persist_run()

        from naumi_agent.orchestrator.pursuit_store import format_run

        return "目标追踪状态已恢复。\n\n" + format_run(self._run)

    def _update_run(
        self,
        *,
        phase: str,
        iteration: int | None = None,
        spec: GoalSpec | None = None,
        blocked_reason: str = "",
        next_action: str = "",
    ) -> None:
        """Update the live pursuit state snapshot."""
        if self._run is None:
            return
        self._run.phase = phase
        self._run.updated_at = time.time()
        if iteration is not None:
            self._run.iteration = iteration
        if spec is not None:
            self._run.criteria_total = len(spec.success_criteria)
            self._run.criteria_verified = sum(
                1 for c in spec.success_criteria
                if c.status == CriterionStatus.VERIFIED
                and self._criterion_has_hard_evidence(c)
            )
        if blocked_reason:
            self._run.blocked_reason = blocked_reason
        if next_action:
            self._run.next_action = next_action
        self._persist_run()

    def _record_stop(
        self,
        status: PursuitRunStatus,
        reason: str,
        evidence: list[PursuitEvidence] | None = None,
    ) -> None:
        """Persist a stop decision into the live run snapshot."""
        decision = PursuitStopDecision(
            status=status,
            reason=reason,
            evidence=evidence or [],
        )
        self._last_stop_decision = decision
        self._apply_stop_decision(decision)

    def _record_waiting(self, reason: str) -> None:
        """Mark the run as waiting for asynchronous work."""
        if self._run is None:
            return
        self._run.status = PursuitRunStatus.WAITING
        self._run.phase = "waiting"
        self._run.blocked_reason = ""
        self._run.waiting_on = list(self._pending_background)
        self._run.updated_at = time.time()
        self._run.add_evidence(PursuitEvidence(
            kind="waiting",
            source="background",
            summary=reason,
            is_hard=False,
            timestamp=time.time(),
        ))
        self._persist_run()

    def _apply_stop_decision(self, decision: PursuitStopDecision) -> None:
        if self._run is None:
            return
        self._run.status = decision.status
        self._run.blocked_reason = (
            decision.reason
            if decision.status == PursuitRunStatus.BLOCKED
            else ""
        )
        self._run.updated_at = time.time()
        for item in decision.evidence:
            self._run.add_evidence(item)
        self._persist_run()

    def _record_checkpoint_evidence(self, checkpoint: IterationCheckpoint) -> None:
        """Record concrete assessment facts in the live state."""
        if self._run is None:
            return
        summary = (
            f"收敛度 {checkpoint.convergence_score:.2f}，"
            f"差距 {len(checkpoint.gaps_found)} 个"
        )
        self._run.add_evidence(PursuitEvidence(
            kind="assessment",
            source=f"iteration:{checkpoint.iteration}",
            summary=summary,
            is_hard=False,
            timestamp=time.time(),
        ))
        self._persist_run()

    def _record_action_evidence(self, results: list[dict[str, Any]]) -> None:
        """Record action execution results in the live state."""
        if self._run is None:
            return
        for result in results:
            status = str(result.get("status", ""))
            self._run.add_evidence(PursuitEvidence(
                kind="action",
                source=str(result.get("action_id", "?")),
                summary=f"[{status}] {str(result.get('output', ''))[:300]}",
                is_hard=status == "completed",
                timestamp=time.time(),
            ))
            if status not in {"completed", "waiting"}:
                self._run.failure_count += 1
        self._persist_run()

    async def _ensure_worktree_for_code_goal(self, spec: GoalSpec) -> None:
        """Create an isolated worktree for code-heavy pursuit goals when possible."""
        if self._run is None or self._run.worktree_name:
            return
        if not self._looks_like_code_goal(spec):
            return
        tool = self._tools.get("worktree_create")
        if tool is None:
            return

        name = self._make_worktree_name(spec.original_goal)
        try:
            output = await tool.execute(name=name)
        except Exception as e:
            self._run.add_evidence(PursuitEvidence(
                kind="worktree",
                source=name,
                summary=f"创建隔离 worktree 失败：{type(e).__name__}: {e}",
                is_hard=False,
                timestamp=time.time(),
            ))
            self._persist_run()
            return

        path = self._extract_markdown_field(str(output), "路径")
        self._run.worktree_name = name
        self._run.worktree_path = path
        self._run.add_evidence(PursuitEvidence(
            kind="worktree",
            source=name,
            summary=str(output)[:500],
            is_hard="已创建隔离 worktree" in str(output) or "Worktree:" in str(output),
            timestamp=time.time(),
        ))
        self._persist_run()

    async def _collect_background_results(self) -> None:
        """Collect finished background task results as hard pursuit evidence."""
        if not self._pending_background or self._run is None:
            return
        status_tool = self._tools.get("background_status")
        output_tool = self._tools.get("background_read_output")
        if status_tool is None:
            return

        still_waiting: list[PursuitBackgroundWait] = []
        for pending in self._pending_background:
            try:
                status_text = str(await status_tool.execute(task_id=pending.task_id))
            except Exception as e:
                self._run.add_evidence(PursuitEvidence(
                    kind="background",
                    source=pending.task_id,
                    summary=f"读取后台任务状态失败：{type(e).__name__}: {e}",
                    is_hard=False,
                    timestamp=time.time(),
                ))
                still_waiting.append(pending)
                continue

            if "运行中" in status_text:
                still_waiting.append(pending)
                continue

            output_text = ""
            if output_tool is not None:
                try:
                    output_text = str(await output_tool.execute(task_id=pending.task_id))
                except Exception as e:
                    output_text = f"读取输出失败：{type(e).__name__}: {e}"

            self._run.add_evidence(PursuitEvidence(
                kind="background",
                source=pending.task_id,
                summary=(status_text + "\n" + output_text)[:1000],
                is_hard=True,
                timestamp=time.time(),
            ))

        self._pending_background = still_waiting
        self._run.waiting_on = list(still_waiting)
        if not still_waiting and self._run.status == PursuitRunStatus.WAITING:
            self._run.status = PursuitRunStatus.RUNNING
            self._run.phase = "assess"
        self._persist_run()

    async def _completion_decision(self, spec: GoalSpec) -> PursuitStopDecision:
        """Decide whether the goal is objectively complete."""
        hard_evidence = self._collect_hard_evidence(spec)
        all_verified = all(
            c.status == CriterionStatus.VERIFIED
            for c in spec.success_criteria
        )
        all_hard = all(
            self._criterion_has_hard_evidence(c)
            for c in spec.success_criteria
        )
        if all_verified and all_hard:
            return PursuitStopDecision(
                status=PursuitRunStatus.COMPLETED,
                reason="所有成功标准都有强证据",
                evidence=hard_evidence,
            )
        return PursuitStopDecision(
            status=PursuitRunStatus.RUNNING,
            reason="仍有成功标准未通过强证据验证",
            evidence=hard_evidence,
        )

    def _collect_hard_evidence(self, spec: GoalSpec) -> list[PursuitEvidence]:
        evidence: list[PursuitEvidence] = []
        for criterion in spec.success_criteria:
            if not self._criterion_has_hard_evidence(criterion):
                continue
            evidence.append(PursuitEvidence(
                kind="criterion",
                source=criterion.id,
                summary=criterion.evidence[:500],
                is_hard=True,
                timestamp=criterion.last_checked or time.time(),
            ))
        return evidence

    @staticmethod
    def _criterion_has_hard_evidence(criterion: SuccessCriterion) -> bool:
        """Return true when a verified criterion has tool/command evidence."""
        if criterion.status != CriterionStatus.VERIFIED:
            return False
        evidence = criterion.evidence.strip()
        if not evidence:
            return False
        hard_prefixes = (
            "Command output:",
            "Tool output:",
            "Verification output:",
        )
        return evidence.startswith(hard_prefixes)

    async def pursue(self, goal: str) -> str:
        """Execute the full goal pursuit loop.

        Returns a final report string in Chinese.
        """
        self._start_time = time.time()
        self._history.clear()
        self._total_tokens = 0
        self._total_cost = 0.0
        self._cancelled = False
        self._last_stop_decision = None
        self._pending_background = []
        self._run = PursuitRun(
            id=f"pursuit_{int(self._start_time)}",
            goal=goal,
            status=PursuitRunStatus.RUNNING,
            phase="parse_goal",
            started_at=self._start_time,
            updated_at=self._start_time,
        )
        self._persist_run()

        # Phase 0: Parse the goal
        spec = await self._parse_goal(goal)
        self._update_run(phase="assess", spec=spec)
        await self._ensure_worktree_for_code_goal(spec)
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
                self._record_stop(PursuitRunStatus.CANCELLED, "用户取消了目标追踪")
                break

            elapsed = time.time() - self._start_time
            if elapsed > self._config.max_time_seconds:
                status = GoalStatus.BUDGET_EXCEEDED
                self._record_stop(PursuitRunStatus.BUDGET_EXCEEDED, "目标追踪超过最大运行时间")
                break

            if self._total_cost >= self._config.max_budget_usd:
                status = GoalStatus.BUDGET_EXCEEDED
                self._record_stop(PursuitRunStatus.BUDGET_EXCEEDED, "目标追踪超过预算上限")
                break

            if iteration > self._config.max_iterations:
                status = GoalStatus.BUDGET_EXCEEDED
                self._record_stop(PursuitRunStatus.BUDGET_EXCEEDED, "目标追踪超过最大迭代次数")
                break

            await self._collect_background_results()

            # Phase 1: Assess current state
            self._update_run(phase="assess", iteration=iteration, spec=spec)
            assessment = await self._assess(spec)
            checkpoint = assessment["checkpoint"]
            self._history.append(checkpoint)
            self._record_checkpoint_evidence(checkpoint)

            logger.info(
                "Iteration %d: convergence=%.2f, gaps=%d, tokens=%d",
                iteration, checkpoint.convergence_score,
                len(checkpoint.gaps_found), self._total_tokens,
            )

            # Check if all criteria are verified with hard evidence.
            stop_decision = await self._completion_decision(spec)
            if stop_decision.status == PursuitRunStatus.COMPLETED:
                self._last_stop_decision = stop_decision
                self._apply_stop_decision(stop_decision)
                if await self._final_verification(spec):
                    self._record_stop(
                        PursuitRunStatus.COMPLETED,
                        "所有成功标准已通过强制验证",
                        self._collect_hard_evidence(spec),
                    )
                    status = GoalStatus.ACHIEVED
                    break

            # Check convergence (are we making progress?)
            if self._is_stagnant():
                if self._config.replan_on_stagnation:
                    self._update_run(
                        phase="recover",
                        iteration=iteration,
                        spec=spec,
                        blocked_reason="连续多轮没有可观测进展，正在切换恢复策略",
                    )
                    logger.warning(
                        "Stagnation detected at iteration %d", iteration,
                    )
                    recovery = await self._recover_from_stagnation(
                        spec, checkpoint,
                    )
                    if not recovery:
                        status = GoalStatus.STUCK
                        self._record_stop(
                            PursuitRunStatus.BLOCKED,
                            "检测到停滞，但没有生成可执行的恢复行动",
                        )
                        break
                    await self._execute_actions(spec, recovery)
                    continue
                else:
                    status = GoalStatus.STUCK
                    self._record_stop(PursuitRunStatus.BLOCKED, "连续多轮没有可观测进展")
                    break

            # Phase 2: Plan next actions
            self._update_run(phase="plan", iteration=iteration, spec=spec)
            actions = await self._plan(spec, checkpoint)
            if not actions:
                status = GoalStatus.STUCK
                self._record_stop(
                    PursuitRunStatus.BLOCKED,
                    "规划器没有给出下一步可执行行动",
                )
                break

            # Store plan in checkpoint
            checkpoint.actions_planned = [
                f"{a['id']}: {a['description']}" for a in actions
            ]
            self._update_run(
                phase="execute",
                iteration=iteration,
                spec=spec,
                next_action=checkpoint.actions_planned[0],
            )

            # Phase 3: Execute actions
            results = await self._execute_actions(spec, actions)

            # Store results for next iteration's evidence
            checkpoint.actions_taken = [
                f"[{r.get('status', '?')}] {r.get('action_id', '?')}: "
                f"{str(r.get('output', ''))[:200]}"
                for r in results
            ]
            self._record_action_evidence(results)
            if any(result.get("status") == "waiting" for result in results):
                status = GoalStatus.WAITING
                self._record_waiting("后台任务仍在运行，已安排后续复查")
                break

            # Phase 4: Verify (if interval matches)
            if iteration % self._config.verify_interval == 0:
                self._update_run(phase="verify", iteration=iteration, spec=spec)
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

        logger.debug("Assessor evidence length: %d chars", len(state_evidence))
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

        # Programmatic convergence floor: if the LLM says 0 but there is
        # hard evidence of progress, boost convergence so stagnation
        # detection doesn't fire unnecessarily.
        if convergence < 0.1 and self._history:
            last = self._history[-1]
            has_completed = any(
                a.startswith("[completed]") for a in last.actions_taken
            )
            has_diff = False
            if "Git 变更:" in state_evidence:
                diff_section = state_evidence.split("Git 变更:")[-1]
                has_diff = any(
                    line.startswith("+") and not line.startswith("+++")
                    for line in diff_section.splitlines()
                )
            if has_completed and has_diff:
                convergence = max(convergence, 0.5)
                logger.info(
                    "Boosted convergence %.2f→%.2f (completed actions + git diff)",
                    0.0, convergence,
                )

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
        """Execute planned actions using real tool calls."""
        results: list[dict[str, Any]] = []

        for action in actions:
            if self._cancelled:
                break

            action_id = action["id"]
            description = action["description"]
            tool_name = action["tool"]

            # Inject file path from goal when action description lacks it
            if tool_name in ("file_write", "file_edit", "file_read"):
                if not self._extract_target_path(description):
                    goal_paths = self._extract_file_paths(spec.original_goal)
                    if goal_paths:
                        description = f"Target: {goal_paths[0]}. {description}"

            # Normalize ambiguous tool names (LLM sometimes outputs "file_write 或 file_edit")
            if "file_edit" in tool_name:
                tool_name = "file_edit"
            elif "file_write" in tool_name:
                tool_name = "file_write"
            elif "file_read" in tool_name:
                tool_name = "file_read"
            elif "bash" in tool_name:
                tool_name = "bash_run"

            logger.info("Executing action %s: %s via %s", action_id, description, tool_name)

            result: dict[str, Any] | None = None

            # Route to the appropriate executor based on planned tool
            if tool_name in ("file_write", "file_edit", "file_read"):
                tool = self._tools.get(tool_name)
                if tool:
                    result = await self._execute_tool_action(
                        tool, tool_name, description, action_id,
                    )
            elif tool_name == "bash_run":
                bash_tool = self._tools.get("bash_run")
                if bash_tool:
                    result = await self._execute_via_bash(
                        bash_tool, description, action_id,
                    )
            else:
                # Unknown tool or generic action — try bash first, then tool
                bash_tool = self._tools.get("bash_run")
                if bash_tool:
                    result = await self._execute_via_bash(
                        bash_tool, description, action_id,
                    )
                    if result["status"] != "completed":
                        tool = self._tools.get(tool_name)
                        if tool:
                            result = await self._execute_tool_action(
                                tool, tool_name, description, action_id,
                            )

            # Fallback to sub-agent
            if result is None:
                result = await self._execute_via_agent(
                    description, action_id,
                )

            results.append(result)

        return results

    async def _execute_tool_action(
        self,
        tool: Any,
        tool_name: str,
        description: str,
        action_id: str,
    ) -> dict[str, Any]:
        """Execute a file tool action by generating content via LLM."""
        try:
            if tool_name == "file_write":
                return await self._execute_file_write(
                    tool, description, action_id,
                )
            elif tool_name == "file_edit":
                return await self._execute_file_edit(
                    tool, description, action_id,
                )
            else:
                return await self._execute_generic_tool(
                    tool, tool_name, description, action_id,
                )
        except Exception as e:
            logger.warning("Tool action %s failed: %s", action_id, e)
            return {
                "action_id": action_id,
                "status": "error",
                "output": f"{type(e).__name__}: {e}",
            }

    async def _execute_file_write(
        self, tool: Any, description: str, action_id: str,
    ) -> dict[str, Any]:
        """Generate file content via LLM and write it.

        If the file already exists, delegate to file_edit instead
        to avoid losing existing content.
        """
        import os
        path = self._extract_target_path(description)

        # If file already exists, use edit instead of overwrite
        if path:
            resolved = os.path.expanduser(path)
            if os.path.isfile(resolved):
                edit_tool = self._tools.get("file_edit")
                if edit_tool:
                    return await self._execute_file_edit(
                        edit_tool, description, action_id,
                    )

        # File doesn't exist yet — generate from scratch
        existing = ""
        if path:
            resolved = os.path.expanduser(path)
            try:
                if os.path.isfile(resolved):
                    with open(resolved, encoding="utf-8") as f:
                        existing = f.read()
            except Exception:
                pass

        context = self._build_codebase_context()
        prompt = (
            f"Action: {description}\n\n"
            f"## Codebase Context\n{context}\n\n"
        )
        if existing and "not found" not in existing.lower():
            prompt += f"## Current file content ({path})\n{existing}\n\n"
            prompt += (
                "Generate the COMPLETE updated file content. "
                "Output ONLY the raw file content, no markdown fences, no explanation."
            )
        else:
            prompt += (
                "Generate the COMPLETE file content. "
                "Output ONLY the raw file content, no markdown fences, no explanation."
            )

        content = await self._llm_call(
            "You generate complete source files. "
            "Output only the raw file content, no markdown.",
            prompt,
        )
        # Strip markdown fences if present
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        if not path:
            return {
                "action_id": action_id,
                "status": "error",
                "output": "Could not determine file path from description",
            }

        if self._execute_tool_call is not None:
            tool_result = await self._execute_tool_call(
                ToolCall(
                    id=f"pursuit-{action_id}",
                    name="file_write",
                    arguments=json.dumps(
                        {"path": path, "content": text},
                        ensure_ascii=False,
                    ),
                )
            )
            return {
                "action_id": action_id,
                "status": "completed" if tool_result.status == "success" else "error",
                "output": tool_result.content[:3000],
            }

        output = await tool.execute(path=path, content=text)
        return {
            "action_id": action_id,
            "status": "completed",
            "output": str(output)[:3000],
        }

    async def _execute_file_edit(
        self, tool: Any, description: str, action_id: str,
    ) -> dict[str, Any]:
        """Edit a file using LLM-generated search/replace.

        Both small and large files use search/replace blocks.
        Large files first locate the relevant region to keep context small.
        """
        import os
        path = self._extract_target_path(description)
        if not path:
            return {
                "action_id": action_id,
                "status": "error",
                "output": "Could not determine file path",
            }

        # Read raw file content
        resolved = os.path.expanduser(path)
        existing = ""
        try:
            if os.path.isfile(resolved):
                with open(resolved, encoding="utf-8") as f:
                    existing = f.read()
        except Exception:
            pass

        if not existing:
            return {
                "action_id": action_id,
                "status": "error",
                "output": f"File {path} not found or empty",
            }

        line_count = existing.count("\n") + 1
        context = self._build_codebase_context()

        if line_count <= 200:
            return await self._edit_small_file(
                tool, path, existing, description, action_id, context,
            )
        else:
            return await self._edit_large_file(
                tool, path, existing, description, action_id, context,
            )

    async def _edit_small_file(
        self,
        tool: Any,
        path: str,
        existing: str,
        description: str,
        action_id: str,
        context: str,
    ) -> dict[str, Any]:
        """Edit a file using search/replace pairs from LLM."""
        lines = existing.split("\n")
        # Build a numbered listing for precise reference
        numbered = "\n".join(f"{i+1:4d} | {line}" for i, line in enumerate(lines))

        prompt = (
            f"Action: {description}\n\n"
            f"## File: {path} ({len(lines)} lines)\n"
            f"```\n{numbered}\n```\n\n"
            "Apply the described change using EXACT search/replace.\n"
            "Copy the original lines VERBATIM from the listing above.\n\n"
            "Output ONE block per change, using this EXACT format:\n"
            "[SEARCH]\n"
            "<exact original lines copied verbatim>\n"
            "[REPLACE]\n"
            "<replacement lines>\n"
            "[END]\n\n"
            "Rules:\n"
            "- The text after [SEARCH] must be an EXACT verbatim copy from the file\n"
            "- Preserve exact indentation, spacing, and special characters\n"
            "- For insertions: copy the line BEFORE insertion as SEARCH, "
            "then repeat it + the new line as REPLACE\n"
            "- Multiple changes: output multiple [SEARCH]...[END] blocks\n"
            "- Output ONLY [SEARCH]/[REPLACE]/[END] blocks\n\n"
            "Example — add a comment above temperature:\n"
            "[SEARCH]\n"
            "  max_tokens: 4096\n"
            "  temperature: 0.7\n"
            "[REPLACE]\n"
            "  max_tokens: 4096\n"
            "  # kimi-k2.6 requires exactly 1.0\n"
            "  temperature: 1.0\n"
            "[END]\n\n"
            "Example — change a function signature:\n"
            "[SEARCH]\n"
            "async def handle(self, request):\n"
            "[REPLACE]\n"
            "async def handle(self, request: Request) -> Response:\n"
            "[END]"
        )

        content = await self._llm_call(
            "You edit files using exact search/replace. "
            "CRITICAL: output ONLY [SEARCH]/[REPLACE]/[END] blocks. "
            "No explanation, no commentary, no markdown.",
            prompt,
        )

        logger.debug(
            "file_edit LLM response for %s (first 800 chars): %s",
            path, content[:800],
        )

        replacements = self._parse_replacements(content)
        if not replacements:
            return {
                "action_id": action_id,
                "status": "error",
                "output": f"No valid search/replace blocks found in LLM response: {content[:500]}",
            }

        updated = existing
        applied = 0
        errors: list[str] = []
        for old_text, new_text in replacements:
            if old_text in updated:
                output = await tool.execute(
                    path=path,
                    old_text=old_text,
                    new_text=new_text,
                )
                if str(output).startswith("Error"):
                    errors.append(str(output)[:200])
                    continue
                updated = updated.replace(old_text, new_text, 1)
                applied += 1
                continue
            errors.append(f"OLD_TEXT not found: {old_text[:80]}...")

        if applied == 0:
            return {
                "action_id": action_id,
                "status": "error",
                "output": f"All replacements failed: {'; '.join(errors)}",
            }

        msg = f"Applied {applied}/{len(replacements)} replacements"
        if errors:
            msg += f" (errors: {'; '.join(errors)})"
        return {
            "action_id": action_id,
            "status": "completed",
            "output": msg,
        }

    @staticmethod
    def _parse_replacements(llm_output: str) -> list[tuple[str, str]]:
        """Parse [SEARCH]...[REPLACE]...[END] blocks from LLM output."""
        import re as _re
        # Primary format: [SEARCH]...[REPLACE]...[END]
        pattern = _re.compile(
            r"\[SEARCH\]\s*\n(.*?)\[REPLACE\]\s*\n(.*?)\[END\]",
            _re.DOTALL,
        )
        results: list[tuple[str, str]] = []
        for m in pattern.finditer(llm_output):
            old_text = m.group(1).rstrip("\n")
            new_text = m.group(2).rstrip("\n")
            if old_text:
                results.append((old_text, new_text))
        if results:
            return results

        # Fallback: <<<OLD/===NEW/>>> format
        fallback = _re.compile(
            r"<<<OLD\s*\n(.*?)===NEW\s*\n(.*?)>>>",
            _re.DOTALL,
        )
        for m in fallback.finditer(llm_output):
            old_text = m.group(1).rstrip("\n")
            new_text = m.group(2).rstrip("\n")
            if old_text:
                results.append((old_text, new_text))
        return results

    async def _edit_large_file(
        self,
        tool: Any,
        path: str,
        existing: str,
        description: str,
        action_id: str,
        context: str,
    ) -> dict[str, Any]:
        """Edit a large file using search/replace on a targeted section."""
        lines = existing.split("\n")

        # Identify the relevant region based on the action description
        region_start, region_end = self._locate_region(
            description, lines, context,
        )

        # Extract the target region + surrounding context
        ctx_lines = 5
        show_start = max(0, region_start - ctx_lines)
        show_end = min(len(lines), region_end + ctx_lines)

        region_numbered = "\n".join(
            f"{i+1:4d} | {lines[i]}"
            for i in range(show_start, show_end)
        )

        prompt = (
            f"Action: {description}\n\n"
            f"## File: {path} (region, lines {show_start+1}-{show_end})\n"
            f"```\n{region_numbered}\n```\n\n"
            "Apply the described change using EXACT search/replace.\n"
            "Copy the original lines VERBATIM from the listing above.\n\n"
            "Output ONE block per change, using this EXACT format:\n"
            "[SEARCH]\n"
            "<exact original lines copied verbatim>\n"
            "[REPLACE]\n"
            "<replacement lines>\n"
            "[END]\n\n"
            "Rules:\n"
            "- The text after [SEARCH] must be an EXACT verbatim copy from the file\n"
            "- Preserve exact indentation and spacing\n"
            "- Output ONLY [SEARCH]/[REPLACE]/[END] blocks\n\n"
            "Example:\n"
            "[SEARCH]\n"
            "    async def process(self, data):\n"
            "        return data\n"
            "[REPLACE]\n"
            "    async def process(self, data: dict) -> dict:\n"
            "        validated = self._validate(data)\n"
            "        return validated\n"
            "[END]"
        )

        content = await self._llm_call(
            "You edit files using exact search/replace. "
            "CRITICAL: output ONLY [SEARCH]/[REPLACE]/[END] blocks. "
            "No explanation, no commentary, no markdown.",
            prompt,
        )

        replacements = self._parse_replacements(content)
        if not replacements:
            return {
                "action_id": action_id,
                "status": "error",
                "output": f"No valid search/replace blocks found: {content[:500]}",
            }

        updated = existing
        applied = 0
        errors: list[str] = []
        for old_text, new_text in replacements:
            if old_text in updated:
                output = await tool.execute(
                    path=path,
                    old_text=old_text,
                    new_text=new_text,
                )
                if str(output).startswith("Error"):
                    errors.append(str(output)[:200])
                    continue
                updated = updated.replace(old_text, new_text, 1)
                applied += 1
            else:
                errors.append(f"OLD_TEXT not found: {old_text[:80]}...")

        if applied == 0:
            return {
                "action_id": action_id,
                "status": "error",
                "output": f"All replacements failed: {'; '.join(errors)}",
            }

        msg = (
            f"Applied {applied}/{len(replacements)} replacements "
            f"in region L{show_start + 1}-L{show_end}"
        )
        if errors:
            msg += f" (errors: {'; '.join(errors)})"
        return {
            "action_id": action_id,
            "status": "completed",
            "output": msg,
        }

    def _locate_region(
        self, description: str, lines: list[str], context: str,
    ) -> tuple[int, int]:
        """Heuristic: locate the relevant region in a large file."""
        import re as _re
        # Extract likely identifiers from the description
        keywords = _re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b', description)

        best_start = 0
        best_end = min(len(lines), 50)
        best_score = 0

        window = 40
        step = 20
        for start in range(0, len(lines), step):
            end = min(start + window, len(lines))
            chunk = "\n".join(lines[start:end])
            score = sum(1 for kw in keywords if kw in chunk)
            if score > best_score:
                best_score = score
                best_start = start
                best_end = end

        if best_score == 0:
            # Fallback: look for def/class/import near the top
            for i, line in enumerate(lines):
                stripped = line.strip()
                if any(kw in stripped for kw in keywords):
                    best_start = max(0, i - 2)
                    best_end = min(len(lines), i + 20)
                    break

        return best_start, best_end

    async def _execute_generic_tool(
        self, tool: Any, tool_name: str, description: str, action_id: str,
    ) -> dict[str, Any]:
        """Execute generic tool by translating description to JSON params."""
        import json

        schema = tool.parameters_schema
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        param_desc = "\n".join(
            f"  - {k}: {v.get('description', v.get('type', 'string'))}"
            for k, v in properties.items()
        )

        translation_prompt = (
            f"Given this action description: \"{description}\"\n\n"
            f"Generate parameters for tool '{tool_name}' with these fields:\n"
            f"{param_desc}\n\n"
            f"Required fields: {', '.join(required)}\n\n"
            f"Output a single JSON object with the parameter values. "
            f"No explanation, just the JSON."
        )

        response = await self._llm_call(
            "You translate action descriptions into tool parameters. "
            "Output only valid JSON.",
            translation_prompt,
        )

        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        params = json.loads(text)

        output = await tool.execute(**params)
        return {
            "action_id": action_id,
            "status": "completed",
            "output": str(output)[:3000],
        }

    async def _execute_via_bash(
        self,
        bash_tool: Any,
        description: str,
        action_id: str,
    ) -> dict[str, Any]:
        """Execute an action by asking LLM to generate a bash command."""
        # Gather codebase context for accurate command generation
        context = self._build_codebase_context()

        command_prompt = (
            f'Given this action: "{description}"\n\n'
            f"## Codebase Context\n{context}\n\n"
            "Generate a bash command or pipeline. Rules:\n"
            "- To CREATE a file: use heredoc: "
            "cat > path/file.py << 'PYEOF'\\n<content>\\nPYEOF\n"
            "- To EDIT a file: prefer python3 -c to read lines and modify\n"
            "- NEVER use sed for multi-line edits\n"
            "- Use python3 not python\n"
            "- Output ONLY the command, no explanation, no markdown fences\n"
        )

        try:
            response = await self._llm_call(
                "You generate precise bash commands. "
                "Output only the command, no markdown.",
                command_prompt,
            )
            command = response.strip()
            # Strip markdown code fences
            if command.startswith("```"):
                command = command.split("\n", 1)[-1].rsplit("```", 1)[0]
            command = command.strip()

            if self._should_run_in_background(command, description):
                background = await self._start_background_action(
                    command=command,
                    description=description,
                    action_id=action_id,
                )
                if background is not None:
                    return background

            if self._execute_tool_call is not None:
                tool_result = await self._execute_tool_call(
                    ToolCall(
                        id=f"pursuit-{action_id}",
                        name="bash_run",
                        arguments=json.dumps(
                            {"command": command},
                            ensure_ascii=False,
                        ),
                    )
                )
                output = tool_result.content
                passed = (
                    tool_result.status == "success"
                    and _verification_command_passed(output)
                )
            else:
                output = await bash_tool.execute(command=command)
                passed = _verification_command_passed(output)

            status = "completed" if passed else "error"
            return {
                "action_id": action_id,
                "status": status,
                "output": str(output)[:3000],
            }
        except Exception as e:
            return {
                "action_id": action_id,
                "status": "error",
                "output": f"{type(e).__name__}: {e}",
            }

    async def _start_background_action(
        self,
        *,
        command: str,
        description: str,
        action_id: str,
    ) -> dict[str, Any] | None:
        """Start a long-running command through background tools."""
        background_tool = self._tools.get("background_run")
        if background_tool is None:
            return None

        cwd = self._run.worktree_path if self._run and self._run.worktree_path else ""
        try:
            output = str(await background_tool.execute(
                command=command,
                cwd=cwd,
                timeout_seconds=1800,
            ))
        except Exception as e:
            return {
                "action_id": action_id,
                "status": "error",
                "output": f"启动后台任务失败：{type(e).__name__}: {e}",
            }

        task_id = self._extract_background_task_id(output)
        if not task_id:
            return {
                "action_id": action_id,
                "status": "error",
                "output": f"后台任务启动后未返回任务 ID：{output[:500]}",
            }

        pending = PursuitBackgroundWait(
            task_id=task_id,
            action_id=action_id,
            command=command,
            created_at=time.time(),
        )
        self._pending_background.append(pending)
        if self._run is not None:
            self._run.waiting_on = list(self._pending_background)
            self._run.add_evidence(PursuitEvidence(
                kind="background",
                source=task_id,
                summary=f"后台执行：{description}\n命令：{command}",
                is_hard=True,
                timestamp=time.time(),
            ))
            self._persist_run()

        await self._schedule_background_followup(task_id)
        return {
            "action_id": action_id,
            "status": "waiting",
            "background_task_id": task_id,
            "output": output[:1000],
        }

    async def _schedule_background_followup(self, task_id: str) -> None:
        """Schedule a reminder to revisit a pending background task."""
        schedule_tool = self._tools.get("schedule_create")
        if schedule_tool is None:
            return
        from datetime import datetime, timedelta

        when = (datetime.now().astimezone() + timedelta(minutes=2)).replace(microsecond=0)
        prompt = (
            f"后台任务 {task_id} 可能已完成。"
            f"请继续 `/pursue resume {self._run.id if self._run else ''}`，"
            "读取 background_status 和 background_read_output 后判断下一步。"
        )
        try:
            output = str(await schedule_tool.execute(
                kind="once",
                expression=when.isoformat(),
                prompt=prompt,
            ))
        except Exception as e:
            output = f"创建复查提醒失败：{type(e).__name__}: {e}"
        if self._run is not None:
            self._run.add_evidence(PursuitEvidence(
                kind="schedule",
                source=task_id,
                summary=output[:500],
                is_hard="调度任务已创建" in output,
                timestamp=time.time(),
            ))
            self._persist_run()

    async def _execute_via_agent(
        self,
        description: str,
        action_id: str,
    ) -> dict[str, Any]:
        """Delegate action to a sub-agent as last resort."""
        from naumi_agent.orchestrator.subagent_manager import SubTask

        agent_name = f"pursuit_{action_id}"
        self._manager.spawn_for_task(
            name=agent_name,
            task_description=description,
            max_turns=5,
            max_budget_usd=float("inf"),
        )
        try:
            subtask = SubTask(
                id=action_id,
                description=description,
                agent_name=agent_name,
            )
            agent_result = await self._manager.delegate(subtask)
            self._total_tokens += agent_result.total_tokens
            self._total_cost += agent_result.total_cost_usd
            return {
                "action_id": action_id,
                "status": agent_result.status,
                "output": (agent_result.response or "")[:3000],
                "tokens": agent_result.total_tokens,
                "cost": agent_result.total_cost_usd,
            }
        except Exception as e:
            return {
                "action_id": action_id,
                "status": "error",
                "output": f"{type(e).__name__}: {e}",
            }
        finally:
            self._manager.destroy(agent_name)

    # ------------------------------------------------------------------
    #  Phase 4: Verification
    # ------------------------------------------------------------------

    async def _verify_criteria(self, spec: GoalSpec, *, force: bool = False) -> None:
        """Run verification commands for each criterion."""
        for criterion in spec.success_criteria:
            if (
                not force
                and criterion.status == CriterionStatus.VERIFIED
                and self._criterion_has_hard_evidence(criterion)
            ):
                continue

            cmd = criterion.verification_command
            if not cmd:
                continue

            # Try to run the verification command
            try:
                if self._execute_tool_call is not None:
                    tool_result = await self._execute_tool_call(
                        ToolCall(
                            id=f"pursuit-verify-{criterion.id}",
                            name="bash_run",
                            arguments=json.dumps(
                                {"command": cmd},
                                ensure_ascii=False,
                            ),
                        )
                    )
                    output = tool_result.content
                    passed = (
                        tool_result.status == "success"
                        and _verification_command_passed(output)
                    )
                else:
                    bash_tool = self._tools.get("bash_run")
                    if not bash_tool:
                        continue
                    output = await bash_tool.execute(command=cmd)
                    passed = _verification_command_passed(output)

                if passed:
                    criterion.status = CriterionStatus.VERIFIED
                    criterion.evidence = f"Command output: {str(output)[:500]}"
                else:
                    criterion.status = CriterionStatus.FAILED
                    criterion.evidence = f"Failed: {str(output)[:500]}"
                criterion.last_checked = time.time()
            except Exception as e:
                criterion.status = CriterionStatus.FAILED
                criterion.evidence = f"Verification error: {e}"

    async def _final_verification(self, spec: GoalSpec) -> bool:
        """Double-check all criteria are truly met."""
        await self._verify_criteria(spec, force=True)
        return all(
            c.status == CriterionStatus.VERIFIED
            and self._criterion_has_hard_evidence(c)
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

    @staticmethod
    def _looks_like_code_goal(spec: GoalSpec) -> bool:
        """Detect goals that should get an isolated code workspace."""
        text = " ".join([
            spec.original_goal,
            spec.description,
            " ".join(c.description for c in spec.success_criteria),
            " ".join(c.verification_command for c in spec.success_criteria),
        ]).lower()
        code_markers = (
            "src/",
            "tests/",
            ".py",
            ".ts",
            ".js",
            ".tsx",
            "pytest",
            "ruff",
            "实现",
            "修改",
            "代码",
            "测试",
            "工具",
        )
        return any(marker in text for marker in code_markers)

    @staticmethod
    def _make_worktree_name(goal: str) -> str:
        """Create a stable-ish safe worktree name for a pursuit goal."""
        import hashlib
        import re

        ascii_goal = re.sub(r"[^a-zA-Z0-9]+", "-", goal.lower()).strip("-")
        prefix = ascii_goal[:32].strip("-") or "goal"
        digest = hashlib.sha1(goal.encode("utf-8")).hexdigest()[:8]
        return f"pursue-{prefix}-{digest}"[:64].strip("-")

    @staticmethod
    def _extract_markdown_field(text: str, label: str) -> str:
        """Extract a markdown bullet field such as '- 路径：`...`'."""
        import re

        pattern = rf"- {re.escape(label)}：`?([^`\n]+)`?"
        match = re.search(pattern, text)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _extract_background_task_id(text: str) -> str:
        """Extract bg_XXXX from background tool output."""
        import re

        match = re.search(r"\b(bg_\d{4,})\b", text)
        return match.group(1) if match else ""

    @staticmethod
    def _should_run_in_background(command: str, description: str) -> bool:
        """Return true for commands that are likely to be slow or long-running."""
        text = f"{command}\n{description}".lower()
        long_markers = (
            "pytest tests/",
            "pytest tests ",
            "pytest .",
            "ruff check src/ tests",
            "npm run build",
            "npm test",
            "pnpm test",
            "pnpm build",
            "yarn test",
            "yarn build",
            "cargo test",
            "go test ./...",
            "mvn test",
            "gradle test",
            "sleep ",
        )
        return any(marker in text for marker in long_markers)

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

        bash_tool = self._tools.get("bash_run")
        if bash_tool:
            # Collect git diff since pursuit started
            # Use both working-tree diff and cached diff to catch
            # staged changes that git diff HEAD might miss
            if self._start_time > 0:
                try:
                    diff_result = await bash_tool.execute(
                        command=(
                            "git diff --stat 2>/dev/null; "
                            "git diff --cached --stat 2>/dev/null; "
                            "echo '---'; "
                            "git diff 2>/dev/null | head -80; "
                            "git diff --cached 2>/dev/null | head -80"
                        ),
                    )
                    if diff_result and "fatal" not in diff_result:
                        evidence_parts.append(
                            f"Git 变更:\n{diff_result}"
                        )
                except Exception:
                    pass

            # Read files mentioned in the goal
            for path in self._extract_file_paths(spec.original_goal):
                try:
                    file_result = await bash_tool.execute(
                        command=f"cat {path} 2>/dev/null | head -80",
                    )
                    if file_result and "No such file" not in file_result:
                        evidence_parts.append(f"文件 {path}:\n{file_result}")
                except Exception:
                    pass

            # Read files mentioned in success criteria verification commands
            for c in spec.success_criteria:
                for path in self._extract_file_paths(c.verification_command):
                    if path not in self._extract_file_paths(spec.original_goal):
                        try:
                            file_result = await bash_tool.execute(
                                command=f"cat {path} 2>/dev/null | head -80",
                            )
                            if file_result and "No such file" not in file_result:
                                evidence_parts.append(f"文件 {path}:\n{file_result}")
                        except Exception:
                            pass

            try:
                test_result = await bash_tool.execute(
                    command="python3 -m pytest tests/ -q --tb=no 2>&1 | tail -5",
                )
                evidence_parts.append(f"测试状态:\n{test_result}")
            except Exception:
                pass

            try:
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

    @staticmethod
    def _extract_target_path(description: str) -> str:
        """Extract the target file path from an action description.

        Uses a strict regex for known extensions first, then falls back
        to a broad match. Avoids matching version numbers like k2.6.
        """
        import re
        # Strict: known file extensions (avoids matching 1.0, k2.6, etc.)
        strict = re.search(
            r'((?:src/[\w/.-]+/)?[\w.-]+\.(?:py|yaml|yml|toml|json|md|txt|cfg|ini|sh|rs|go|ts|js|tsx|jsx|css|html|xml|sql))',
            description,
        )
        if strict:
            return strict.group(1)

        # Broad: any path with a dot extension (but skip obvious non-paths)
        broad = re.search(r'([\w/.][\w/.-]*\.[a-zA-Z]{2,4})\b', description)
        if broad:
            candidate = broad.group(1)
            # Skip version-like matches (e.g. 1.0, k2.6, v1.2)
            if not re.match(r'^[kv]?\d+\.\d+$', candidate):
                return candidate

        # Last resort: use _extract_file_paths
        paths = __class__._extract_file_paths(description)
        return paths[0] if paths else ""

    @staticmethod
    def _extract_file_paths(text: str) -> list[str]:
        """Extract file paths mentioned in text."""
        import re
        return list(set(re.findall(
            r'(?:src/[\w/.-]+|[\w/.-]+\.(?:py|yaml|yml|toml|json|md|txt|cfg|ini|sh))',
            text,
        )))

    def _build_codebase_context(self) -> str:
        """Build a concise context string with key codebase interfaces."""
        parts: list[str] = []

        # Tool base class signature (from source we know)
        parts.append(
            "Tool base class (src/naumi_agent/tools/base.py):\n"
            "  class Tool(ABC):\n"
            "      @property name -> str (abstract)\n"
            "      @property description -> str (abstract)\n"
            "      @property parameters_schema -> dict (abstract)\n"
            "      async def execute(self, **kwargs) -> str (abstract)\n"
        )

        # Existing tool pattern
        parts.append(
            "Existing tool pattern (from builtin.py):\n"
            "  class BashRunTool(Tool):\n"
            "      @property def name -> 'bash_run'\n"
            "      @property def description -> str\n"
            "      @property def parameters_schema -> dict\n"
            "      async def execute(self, *, command, timeout=30, ...) -> str\n"
        )

        # Registration pattern
        parts.append(
            "Registration in create_builtin_tools():\n"
            "  return [FileReadTool(), FileWriteTool(), FileEditTool(), BashRunTool()]\n"
        )

        # Project structure
        parts.append(
            "Project structure:\n"
            "  src/naumi_agent/tools/base.py    — Tool base class + ToolRegistry\n"
            "  src/naumi_agent/tools/builtin.py  — Built-in tools + create_builtin_tools()\n"
            "  src/naumi_agent/tools/pursuit.py  — PursueTool\n"
            "  src/naumi_agent/main.py           — CLI entry, slash commands in _handle_command()\n"
            "  src/naumi_agent/tui/app.py        — TUI, slash commands in _handle_command()\n"
        )

        return "\n".join(parts)

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
            max_tokens=16384,
            temperature=1.0,
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
