"""运行时行为监控 — 循环检测、振荡检测、主动干预."""

from __future__ import annotations

import time
from dataclasses import dataclass

_TOOL_WORKFLOW_GROUPS: dict[str, str] = {
    # These tools are consecutive stages of one multi-agent workflow, not
    # competing strategies. Treating them as unrelated causes false convergence.
    "spawn_agent": "subagent_workflow",
    "delegate_task": "subagent_workflow",
    "list_agents": "subagent_workflow",
    "destroy_agent": "subagent_workflow",
    "task_create": "task_workflow",
    "task_update": "task_workflow",
    "task_list": "task_workflow",
    "task_delete": "task_workflow",
}


def _workflow_key(tool_name: str) -> str:
    return _TOOL_WORKFLOW_GROUPS.get(tool_name, tool_name)


@dataclass
class ToolCallRecord:
    tool_name: str
    timestamp: float


@dataclass
class TurnRecord:
    """Per-turn tool usage record for oscillation detection."""
    turn: int
    tools: list[str]
    timestamp: float


@dataclass
class InterventionAction:
    """Directive from the monitor for the engine to act on."""
    action: str  # "inject_message" | "force_converge"
    message: str
    severity: int  # 1=gentle nudge, 2=strong nudge, 3=force converge


class BehaviorMonitor:
    """运行时行为异常检测 + 方案振荡检测 + 主动干预."""

    def __init__(
        self,
        *,
        loop_window: int = 10,
        high_freq_window: int = 5,
        high_freq_seconds: float = 5.0,
        error_rate_threshold: float = 0.5,
        max_interventions: int = 2,
        approach_diversity_window: int = 6,
        approach_overlap_threshold: float = 0.3,
    ) -> None:
        self._loop_window = loop_window
        self._high_freq_window = high_freq_window
        self._high_freq_seconds = high_freq_seconds
        self._error_rate_threshold = error_rate_threshold
        self._max_interventions = max_interventions
        self._approach_diversity_window = approach_diversity_window
        self._approach_overlap_threshold = approach_overlap_threshold

        self._call_history: list[ToolCallRecord] = []
        self._error_count = 0

        # Turn-level tracking for approach oscillation
        self._turn_records: list[TurnRecord] = []
        self._current_turn_tools: list[str] = []
        self._current_turn: int = 0
        self._intervention_count: int = 0

    def begin_turn(self, turn: int) -> None:
        """Finalize previous turn's tools and start new turn tracking."""
        if self._current_turn_tools:
            self._turn_records.append(TurnRecord(
                turn=self._current_turn,
                tools=list(self._current_turn_tools),
                timestamp=time.time(),
            ))
        self._current_turn = turn
        self._current_turn_tools = []

    def record_tool_call(self, tool_name: str, is_error: bool = False) -> None:
        self._call_history.append(
            ToolCallRecord(tool_name=tool_name, timestamp=time.time())
        )
        self._current_turn_tools.append(tool_name)
        if is_error:
            self._error_count += 1

    def check_anomalous_behavior(self) -> list[str]:
        """Detect tool-level anomalies (existing logic, unchanged)."""
        warnings: list[str] = []

        recent = self._call_history[-self._loop_window:]
        if len(recent) >= self._loop_window:
            names = [r.tool_name for r in recent]
            if len(set(names)) <= 2:
                warnings.append(f"检测到工具调用循环：重复调用 {set(names)}")

        recent_freq = self._call_history[-self._high_freq_window:]
        if len(recent_freq) >= self._high_freq_window:
            time_span = recent_freq[-1].timestamp - recent_freq[0].timestamp
            if time_span < self._high_freq_seconds:
                warnings.append(
                    f"工具调用频率异常高：{self._high_freq_window} 次在 {time_span:.1f}s 内"
                )

        total = len(self._call_history)
        if total > 5 and self._error_count / total > self._error_rate_threshold:
            warnings.append(
                f"工具调用失败率过高：{self._error_count}/{total} "
                f"({self._error_count / total:.0%})"
            )

        return warnings

    def check_intervention(self) -> InterventionAction | None:
        """Detect approach-level oscillation and return intervention directive."""
        all_turns = list(self._turn_records)
        if self._current_turn_tools:
            all_turns.append(TurnRecord(
                turn=self._current_turn,
                tools=list(self._current_turn_tools),
                timestamp=time.time(),
            ))

        non_empty = [t for t in all_turns if t.tools]
        if len(non_empty) < 3:
            return None

        recent = non_empty[-self._approach_diversity_window:]

        # Count approach changes: low tool overlap between consecutive turns
        approach_changes = 0
        for i in range(1, len(recent)):
            prev_tools = {_workflow_key(tool) for tool in recent[i - 1].tools}
            curr_tools = {_workflow_key(tool) for tool in recent[i].tools}
            if not prev_tools or not curr_tools:
                continue
            overlap = len(prev_tools & curr_tools) / len(prev_tools | curr_tools)
            if overlap < self._approach_overlap_threshold:
                approach_changes += 1

        # High tool diversity ratio = agent never commits to a toolset
        all_tools_in_window: set[str] = set()
        total_calls_in_window = 0
        for t in recent:
            all_tools_in_window.update(_workflow_key(tool) for tool in t.tools)
            total_calls_in_window += len(t.tools)
        unique_ratio = len(all_tools_in_window) / max(total_calls_in_window, 1)

        is_oscillating = approach_changes >= 2 or (
            len(recent) >= 4 and unique_ratio > 0.8
        )

        if not is_oscillating:
            return None

        self._intervention_count += 1

        if self._intervention_count >= self._max_interventions:
            return InterventionAction(
                action="force_converge",
                message=(
                    "⚠️ 系统检测到你已多次切换执行方案。"
                    "请立即停止尝试新方案，选择当前最成熟的方案直接输出最终结果。"
                    "不要再调用任何工具。立即用纯文本给出你的最终回答。"
                ),
                severity=3,
            )

        return InterventionAction(
            action="inject_message",
            message=(
                "⚠️ 系统检测到你频繁切换执行方案。"
                "建议坚持当前方案完成执行，不要推翻重来。"
                "如果当前方案有小问题，在原有基础上微调，不要完全更换方案。"
                "记住：一个完成的方案胜过三个半成品。"
            ),
            severity=2,
        )

    def reset(self) -> None:
        self._call_history.clear()
        self._error_count = 0
        self._turn_records.clear()
        self._current_turn_tools.clear()
        self._current_turn = 0
        self._intervention_count = 0
