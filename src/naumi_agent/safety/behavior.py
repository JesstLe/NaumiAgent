"""运行时行为监控 — 循环检测、异常频率."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class ToolCallRecord:
    tool_name: str
    timestamp: float


class BehaviorMonitor:
    """运行时行为异常检测."""

    def __init__(
        self,
        *,
        loop_window: int = 10,
        high_freq_window: int = 5,
        high_freq_seconds: float = 5.0,
        error_rate_threshold: float = 0.5,
    ) -> None:
        self._loop_window = loop_window
        self._high_freq_window = high_freq_window
        self._high_freq_seconds = high_freq_seconds
        self._error_rate_threshold = error_rate_threshold
        self._call_history: list[ToolCallRecord] = []
        self._error_count = 0

    def record_tool_call(self, tool_name: str, is_error: bool = False) -> None:
        self._call_history.append(ToolCallRecord(tool_name=tool_name, timestamp=time.time()))
        if is_error:
            self._error_count += 1

    def check_anomalous_behavior(self) -> list[str]:
        warnings: list[str] = []

        recent = self._call_history[-self._loop_window :]
        if len(recent) >= self._loop_window:
            names = [r.tool_name for r in recent]
            if len(set(names)) <= 2:
                warnings.append(f"检测到工具调用循环：重复调用 {set(names)}")

        recent_freq = self._call_history[-self._high_freq_window :]
        if len(recent_freq) >= self._high_freq_window:
            time_span = recent_freq[-1].timestamp - recent_freq[0].timestamp
            if time_span < self._high_freq_seconds:
                warnings.append(
                    f"工具调用频率异常高：{self._high_freq_window} 次在 {time_span:.1f}s 内"
                )

        total = len(self._call_history)
        if total > 5 and self._error_count / total > self._error_rate_threshold:
            warnings.append(
                f"工具调用失败率过高：{self._error_count}/{total} ({self._error_count / total:.0%})"
            )

        return warnings

    def reset(self) -> None:
        self._call_history.clear()
        self._error_count = 0
