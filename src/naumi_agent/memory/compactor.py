"""上下文压缩 — 当上下文窗口接近限制时自动压缩历史消息."""

from __future__ import annotations

import logging
from typing import Any

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.model.router import ModelRouter, ModelTier

logger = logging.getLogger(__name__)

COMPACTION_PROMPT = """\
请将以下对话历史压缩为简洁的摘要。保留关键信息：
1. 用户的核心需求和目标
2. 已完成的关键操作和结果
3. 重要的上下文（文件路径、配置、决策）
4. 待解决的剩余问题

丢弃：
- 已被替代的旧方案
- 冗余的中间过程
- 超长的文件内容引用

对话历史：
{history}

输出格式：
## 任务目标
（一句话）

## 已完成
- （关键操作和结果列表）

## 关键上下文
- （重要文件路径、配置、决策）

## 待处理
- （剩余问题）
"""


class ContextCompactor:
    """上下文压缩器."""

    def __init__(
        self,
        config: MemoryConfig,
        router: ModelRouter,
        *,
        threshold: float = 0.75,
        max_messages: int = 50,
    ) -> None:
        self._threshold = threshold
        self._max_messages = max_messages
        self._config = config
        self._router = router

    def should_compact(self, messages: list[dict[str, Any]], max_tokens: int) -> bool:
        """判断是否需要压缩."""
        estimated = self._estimate_tokens(messages)
        if estimated > max_tokens * self._threshold:
            return True
        if len(messages) > self._max_messages:
            return True
        return False

    async def compact(
        self, messages: list[dict[str, Any]], max_tokens: int
    ) -> list[dict[str, Any]]:
        """压缩消息列表.

        保留 system prompt 和最近几轮，压缩中间历史。
        """
        if not self.should_compact(messages, max_tokens):
            return messages

        # 分离 system prompt
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        if len(non_system) <= 6:
            return messages

        # 保留最近 4 条消息（2 轮交互）
        recent = non_system[-4:]
        to_compact = non_system[:-4]

        if not to_compact:
            return messages

        # 将中间历史转为文本
        history_text = self._messages_to_text(to_compact)

        logger.info(
            "Compacting %d messages (%d chars) → summary",
            len(to_compact),
            len(history_text),
        )

        # 用 fast model 生成摘要
        try:
            response = await self._router.call(
                messages=[{"role": "user", "content": COMPACTION_PROMPT.format(history=history_text)}],
                tier=ModelTier.FAST,
                max_tokens=1000,
            )
            summary = response.content
        except Exception as e:
            logger.warning("Compaction failed, keeping original: %s", e)
            return messages

        # 构建压缩后的消息列表
        compacted = [
            *system_msgs,
            {
                "role": "system",
                "content": f"## 之前的对话摘要\n\n{summary}",
            },
            *recent,
        ]

        logger.info(
            "Compacted: %d → %d messages",
            len(messages),
            len(compacted),
        )

        return compacted

    def _messages_to_text(self, messages: list[dict[str, Any]]) -> str:
        """将消息列表转为可读文本."""
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if role == "tool":
                tool_id = msg.get("tool_call_id", "")
                preview = content[:500] if len(content) > 500 else content
                parts.append(f"[Tool Result {tool_id}]: {preview}")
            elif role == "assistant":
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    for tc in tool_calls:
                        func = tc.get("function", {})
                        parts.append(f"[Assistant called {func.get('name', '?')}]: {func.get('arguments', '')[:200]}")
                if content:
                    parts.append(f"[Assistant]: {content[:500]}")
            elif role == "user":
                parts.append(f"[User]: {content[:500]}")

        return "\n\n".join(parts)

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """粗略估算 token 数（1 token ≈ 4 chars）."""
        total_chars = sum(len(m.get("content", "")) for m in messages)
        # 工具调用参数也算
        for m in messages:
            for tc in m.get("tool_calls", []):
                func = tc.get("function", {})
                total_chars += len(func.get("arguments", ""))
        return total_chars // 4
