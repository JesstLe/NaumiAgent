"""上下文压缩 — 当上下文窗口接近限制时自动压缩历史消息."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import litellm

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.model.router import ModelRouter, ModelTier

logger = logging.getLogger(__name__)

_LARGE_TOOL_RESULT_CHARS = 12_000
_TOOL_RESULT_PREVIEW_CHARS = 1_200
_ARCHIVED_TOOL_RESULT_MARKER = "[大型工具结果已归档]"
_IMAGE_DATA_URL_RE = re.compile(
    r"data:image/(?P<format>[a-zA-Z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/=_-]+)"
)
_BARE_IMAGE_BASE64_RE = re.compile(
    r"(?P<data>(?P<prefix>iVBORw0KGgo|/9j/|UklGR)[A-Za-z0-9+/=_-]{1024,})"
)
_MULTIMODAL_IMAGE_TYPES = {"image_url", "input_image"}

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

EXTRACTION_PROMPT = """\
从以下对话中提取值得长期记住的关键信息。

只提取以下类型：
- fact: 客观事实（技术栈、项目结构、配置）
- preference: 用户偏好（风格、习惯、约定）
- decision: 重要决策（架构选型、方案确定）

不要提取：
- 临时性信息、中间过程
- 已被后续消息推翻的信息
- 文件内容片段

对话内容：
{history}

输出 JSON 数组，每个元素包含 content 和 category 字段：
```json
[
  {{"content": "...", "category": "fact"}},
  {{"content": "...", "category": "preference"}}
]
```

如果没有值得提取的信息，输出空数组：[]
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
        long_term_memory: Any | None = None,
    ) -> None:
        self._threshold = threshold
        self._max_messages = max_messages
        self._config = config
        self._router = router
        self._long_term_memory = long_term_memory

    def offload_large_tool_results(
        self,
        messages: list[dict[str, Any]],
        *,
        min_chars: int = _LARGE_TOOL_RESULT_CHARS,
    ) -> tuple[list[dict[str, Any]], list[ToolResultArchive]]:
        """Persist oversized tool results and leave compact placeholders."""
        archived: list[ToolResultArchive] = []
        updated: list[dict[str, Any]] = []

        for message in messages:
            if message.get("role") != "tool":
                updated.append(message)
                continue

            content = message.get("content", "")
            if not isinstance(content, str):
                updated.append(message)
                continue
            if len(content) < min_chars or _ARCHIVED_TOOL_RESULT_MARKER in content:
                updated.append(message)
                continue

            artifact = self._write_tool_result_artifact(message, content)
            archived.append(artifact)
            replacement = dict(message)
            replacement["content"] = _format_archived_tool_result_placeholder(
                artifact,
                content,
            )
            updated.append(replacement)

        return updated, archived

    def sanitize_visual_payloads(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        """Replace inline image payloads with compact placeholders."""
        sanitized_messages: list[dict[str, Any]] = []
        replacements = 0

        for message in messages:
            sanitized, count = _sanitize_visual_value(message)
            replacements += count
            sanitized_messages.append(
                sanitized if isinstance(sanitized, dict) else message
            )

        return sanitized_messages, replacements

    def should_compact(self, messages: list[dict[str, Any]], max_tokens: int) -> bool:
        """判断是否需要压缩."""
        estimated = self._estimate_tokens(messages)
        if estimated > max_tokens * self._threshold:
            return True
        if len(messages) > self._max_messages:
            return True
        return False

    async def compact(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
        *,
        runtime_snapshot: str = "",
    ) -> list[dict[str, Any]]:
        """压缩消息列表.

        保留 system prompt 和最近几轮，压缩中间历史。
        """
        messages, _ = self.sanitize_visual_payloads(messages)
        messages, _ = self.offload_large_tool_results(messages)
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

        # Extract memories from messages being compacted
        await self._extract_memories(to_compact)

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
                messages=[
                    {"role": "user", "content": COMPACTION_PROMPT.format(history=history_text)}
                ],
                tier=ModelTier.FAST,
                max_tokens=1000,
            )
            summary = response.content
        except Exception as e:
            logger.warning("Compaction failed, keeping original: %s", e)
            return messages

        # 构建压缩后的消息列表
        summary_content = f"## 之前的对话摘要\n\n{summary}"
        if runtime_snapshot.strip():
            summary_content += (
                "\n\n## 压缩时保留的运行时状态\n\n"
                f"{runtime_snapshot.strip()}"
            )

        compacted = [
            *system_msgs,
            {
                "role": "system",
                "content": summary_content,
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
                        parts.append(
                            f"[Assistant called {func.get('name', '?')}]:"
                            f" {func.get('arguments', '')[:200]}"
                        )
                if content:
                    parts.append(f"[Assistant]: {content[:500]}")
            elif role == "user":
                parts.append(f"[User]: {content[:500]}")

        return "\n\n".join(parts)

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """使用 litellm token_counter 估算 token 数."""
        sanitized_messages, _ = self.sanitize_visual_payloads(messages)
        try:
            model = self._router.resolve_model(ModelTier.CAPABLE)
            return litellm.token_counter(model=model, messages=sanitized_messages)
        except Exception:
            # Fallback: 1 token ≈ 4 chars
            total_chars = sum(
                _fallback_token_chars(m.get("content", "")) for m in sanitized_messages
            )
            for m in sanitized_messages:
                for tc in m.get("tool_calls", []):
                    func = tc.get("function", {})
                    sanitized_args, _ = _sanitize_visual_value(func.get("arguments", ""))
                    total_chars += _fallback_token_chars(sanitized_args)
            return total_chars // 4

    async def _extract_memories(self, messages: list[dict[str, Any]]) -> None:
        """从即将被压缩的消息中提取关键信息存入长期记忆."""
        if self._long_term_memory is None:
            return

        history_text = self._messages_to_text(messages)
        if len(history_text) < 100:
            return

        try:
            response = await self._router.call(
                messages=[
                    {"role": "user", "content": EXTRACTION_PROMPT.format(history=history_text)}
                ],
                tier=ModelTier.FAST,
                max_tokens=500,
            )
            extracted = _parse_extraction_response(response.content)
        except Exception as e:
            logger.debug("Memory extraction failed: %s", e)
            return

        if not extracted:
            return

        now = datetime.now().isoformat()
        stored = 0
        for item in extracted:
            content = item.get("content", "").strip()
            category = item.get("category", "fact")
            if not content:
                continue
            try:
                from naumi_agent.memory.long_term import MemoryEntry

                entry = MemoryEntry(
                    id="",
                    content=content,
                    category=category,
                    created_at=now,
                    updated_at=now,
                    metadata={"source": "compaction"},
                )
                await self._long_term_memory.store(entry)
                stored += 1
            except Exception as e:
                logger.debug("Failed to store extracted memory: %s", e)

        if stored:
            logger.info("Auto-extracted %d memories during compaction", stored)

    def _write_tool_result_artifact(
        self,
        message: dict[str, Any],
        content: str,
    ) -> ToolResultArchive:
        digest = sha256(content.encode("utf-8")).hexdigest()
        tool_call_id = str(message.get("tool_call_id") or "unknown")
        safe_tool_call_id = _safe_artifact_segment(tool_call_id)
        artifacts_dir = (
            Path(self._config.session_db_path).expanduser().resolve().parent
            / "compaction-artifacts"
            / "tool-results"
        )
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = artifacts_dir / f"{safe_tool_call_id}-{digest[:12]}.txt"
        if not path.exists():
            path.write_text(content, encoding="utf-8")
        return ToolResultArchive(
            tool_call_id=tool_call_id,
            path=str(path),
            chars=len(content),
            sha256=digest,
        )


def _sanitize_visual_value(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        return _sanitize_visual_string(value)

    if isinstance(value, list):
        changed = 0
        items: list[Any] = []
        for item in value:
            sanitized, count = _sanitize_visual_value(item)
            changed += count
            items.append(sanitized)
        return (items, changed) if changed else (value, 0)

    if not isinstance(value, dict):
        return value, 0

    message_type = str(value.get("type") or "")
    if message_type in _MULTIMODAL_IMAGE_TYPES:
        return _visual_payload_placeholder(value), 1

    changed = 0
    sanitized_dict: dict[str, Any] = {}
    for key, child in value.items():
        if key in {"image_url", "input_image"} and child:
            sanitized_dict[key] = _visual_payload_placeholder(child)
            changed += 1
            continue
        sanitized, count = _sanitize_visual_value(child)
        changed += count
        sanitized_dict[key] = sanitized

    return (sanitized_dict, changed) if changed else (value, 0)


def _sanitize_visual_string(value: str) -> tuple[str, int]:
    replacements = 0

    def replace_data_url(match: re.Match[str]) -> str:
        nonlocal replacements
        replacements += 1
        return (
            "[图片内容已省略: "
            f"data:image/{match.group('format')};base64, "
            f"base64_chars={len(match.group('data'))}]"
        )

    sanitized = _IMAGE_DATA_URL_RE.sub(replace_data_url, value)

    def replace_bare_image(match: re.Match[str]) -> str:
        nonlocal replacements
        data = match.group("data")
        replacements += 1
        return (
            "[图片内容已省略: "
            f"{_guess_image_base64_format(match.group('prefix'))}_base64, "
            f"base64_chars={len(data)}]"
        )

    sanitized = _BARE_IMAGE_BASE64_RE.sub(replace_bare_image, sanitized)
    return sanitized, replacements


def _visual_payload_placeholder(value: Any) -> dict[str, str]:
    text = str(value)
    data_url_match = _IMAGE_DATA_URL_RE.search(text)
    if data_url_match:
        description = (
            f"data:image/{data_url_match.group('format')};base64, "
            f"base64_chars={len(data_url_match.group('data'))}"
        )
    elif bare_match := _BARE_IMAGE_BASE64_RE.search(text):
        description = (
            f"{_guess_image_base64_format(bare_match.group('prefix'))}_base64, "
            f"base64_chars={len(bare_match.group('data'))}"
        )
    else:
        description = f"image_payload_chars={len(text)}"
    return {
        "type": "text",
        "text": f"[图片内容已省略: {description}]",
    }


def _guess_image_base64_format(prefix: str) -> str:
    if prefix == "iVBORw0KGgo":
        return "png"
    if prefix == "/9j/":
        return "jpeg"
    if prefix == "UklGR":
        return "webp"
    return "image"


def _fallback_token_chars(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except TypeError:
        return len(str(value))


@dataclass(frozen=True)
class ToolResultArchive:
    """Persisted oversized tool result metadata."""

    tool_call_id: str
    path: str
    chars: int
    sha256: str


def _parse_extraction_response(text: str) -> list[dict[str, str]]:
    """Parse JSON array from LLM extraction response."""
    # Try to extract JSON from markdown code block
    import re

    json_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if json_match:
        text = json_match.group(1).strip()

    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [
                item for item in data
                if isinstance(item, dict) and "content" in item
            ]
    except json.JSONDecodeError:
        pass

    return []


def _format_archived_tool_result_placeholder(
    archive: ToolResultArchive,
    content: str,
) -> str:
    preview = content[:_TOOL_RESULT_PREVIEW_CHARS]
    if len(content) > _TOOL_RESULT_PREVIEW_CHARS:
        preview += "\n...（完整结果已归档，请按路径读取）"
    return (
        f"{_ARCHIVED_TOOL_RESULT_MARKER}\n"
        f"tool_call_id: {archive.tool_call_id}\n"
        f"chars: {archive.chars}\n"
        f"sha256: {archive.sha256}\n"
        f"artifact: {archive.path}\n\n"
        "预览：\n"
        f"{preview}"
    )


def _safe_artifact_segment(value: str) -> str:
    import re

    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._-") or "tool_result"
