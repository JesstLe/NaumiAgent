# 第十部分：流式输出设计

## 1. 流式架构总览

Agent 的响应必须实时流式输出——用户不应等待整个生成完成才看到内容。核心挑战：将 LLM token 流、工具调用进度、规划状态统一为一套事件流。

```
┌─────────────────────────────────────────────────────────────┐
│                    事件源（Event Sources）                    │
│                                                             │
│  LLM Token Stream   Tool Execution   Planner   Memory      │
│       │                  │              │          │         │
└───────┼──────────────────┼──────────────┼──────────┼─────────┘
        │                  │              │          │
        ▼                  ▼              ▼          ▼
┌─────────────────────────────────────────────────────────────┐
│                    事件总线（Event Bus）                      │
│                                                             │
│  EventEmitter — asyncio.Queue + 多播分发                     │
│  - 背压控制（slow consumer 隔离）                             │
│  - 事件过滤（消费者只订阅关心的类型）                          │
│  - 生命周期管理（创建/销毁随 Session）                        │
└─────────────────────┬───────────────────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        │             │             │
        ▼             ▼             ▼
   TUI Consumer  WS Consumer   Log Consumer
   (Textual)     (FastAPI)     (OTel/LangSmith)
```

## 2. 事件模型

### 2.1 事件类型

```python
# src/naumi_agent/streaming/events.py

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
import uuid


class EventType(str, Enum):
    # --- LLM 响应流 ---
    TOKEN_DELTA = "token_delta"            # 单个 token 增量
    THINKING_DELTA = "thinking_delta"      # 思维链增量
    THINKING_START = "thinking_start"      # 开始思考
    THINKING_END = "thinking_end"          # 思考结束

    # --- 工具调用 ---
    TOOL_CALL_START = "tool_call_start"    # 开始调用工具
    TOOL_CALL_DELTA = "tool_call_delta"    # 工具参数增量（流式参数解析）
    TOOL_CALL_END = "tool_call_end"        # 工具调用完成（含结果）
    TOOL_CALL_ERROR = "tool_call_error"    # 工具调用失败

    # --- 规划 ---
    PLAN_CREATED = "plan_created"          # 规划生成完成
    PLAN_STEP_START = "plan_step_start"    # 步骤开始执行
    PLAN_STEP_UPDATE = "plan_step_update"  # 步骤进度更新
    PLAN_STEP_END = "plan_step_end"        # 步骤完成

    # --- 记忆 ---
    MEMORY_STORED = "memory_stored"        # 记忆已存储
    MEMORY_RECALLED = "memory_recalled"    # 记忆已召回
    CONTEXT_COMPACTED = "context_compacted"  # 上下文已压缩

    # --- 生命周期 ---
    AGENT_START = "agent_start"            # Agent 开始处理
    AGENT_END = "agent_end"                # Agent 完成处理
    AGENT_ERROR = "agent_error"            # Agent 出错
    TURN_START = "turn_start"              # 新一轮开始
    TURN_END = "turn_end"                  # 当前轮结束

    # --- 资源 ---
    BUDGET_UPDATE = "budget_update"        # 预算消耗更新
    TOKEN_COUNT = "token_count"            # Token 计数更新


@dataclass(frozen=True)
class StreamEvent:
    """统一事件模型"""
    type: EventType
    data: dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    session_id: str = ""
    turn: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "data": self.data,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "turn": self.turn,
        }

    def to_sse(self) -> str:
        """转为 Server-Sent Events 格式"""
        import json
        return f"data: {json.dumps(self.to_dict())}\n\n"

    def to_ws(self) -> str:
        """转为 WebSocket JSON 文本帧"""
        import json
        return json.dumps(self.to_dict())
```

### 2.2 事件数据结构

```python
# 各事件类型对应的 data 字段结构

# TOKEN_DELTA
{
    "token": "你",              # 增量文本
    "model": "claude-sonnet-4-6",
    "index": 0,                 # 响应中的位置（多响应时区分）
}

# THINKING_DELTA
{
    "token": "让我分析一下...",   # 思维链增量
    "model": "claude-sonnet-4-6",
}

# TOOL_CALL_START
{
    "call_id": "call_abc123",
    "tool_name": "file_read",
    "args": {"path": "/workspace/main.py"},
    "step_id": "step_2",        # 关联的规划步骤（如果有）
}

# TOOL_CALL_END
{
    "call_id": "call_abc123",
    "tool_name": "file_read",
    "status": "success",        # "success" | "error" | "timeout"
    "result_preview": "def hello():...",  # 结果预览（截断）
    "duration_ms": 120,
}

# PLAN_CREATED
{
    "plan_id": "plan_xyz",
    "steps": [
        {"id": "step_1", "description": "读取文件", "status": "pending"},
        {"id": "step_2", "description": "修改代码", "status": "pending"},
    ],
    "mode": "prompt_chain",
}

# PLAN_STEP_END
{
    "step_id": "step_1",
    "status": "completed",      # "completed" | "failed" | "skipped"
    "summary": "已读取 main.py，共 120 行",
}

# BUDGET_UPDATE
{
    "total_input_tokens": 15000,
    "total_output_tokens": 3000,
    "total_cost_usd": 0.045,
    "remaining_usd": 4.955,
    "model": "claude-sonnet-4-6",
}

# AGENT_END
{
    "status": "completed",      # "completed" | "max_turns" | "budget_exceeded" | "error"
    "total_turns": 5,
    "total_tokens": 18000,
    "total_cost_usd": 0.045,
    "duration_seconds": 12.3,
}
```

## 3. 事件总线

### 3.1 EventEmitter

```python
# src/naumi_agent/streaming/event_bus.py

import asyncio
from collections import defaultdict
from typing import AsyncIterator, Callable


class EventEmitter:
    """异步事件总线 — 发布/订阅模式"""

    def __init__(self, max_queue_size: int = 1000):
        self._subscribers: dict[str, asyncio.Queue[StreamEvent]] = {}
        self._filters: dict[str, set[EventType]] = {}
        self._max_queue_size = max_queue_size
        self._history: list[StreamEvent] = []
        self._history_limit = 500

    def subscribe(
        self,
        subscriber_id: str,
        event_types: set[EventType] | None = None,
    ) -> asyncio.Queue[StreamEvent]:
        """订阅事件流，返回异步队列"""
        queue: asyncio.Queue[StreamEvent] = asyncio.Queue(
            maxsize=self._max_queue_size
        )
        self._subscribers[subscriber_id] = queue
        if event_types:
            self._filters[subscriber_id] = event_types
        return queue

    def unsubscribe(self, subscriber_id: str) -> None:
        """取消订阅"""
        self._subscribers.pop(subscriber_id, None)
        self._filters.pop(subscriber_id, None)

    async def emit(self, event: StreamEvent) -> None:
        """发布事件到所有订阅者"""
        self._history.append(event)
        if len(self._history) > self._history_limit:
            self._history = self._history[-self._history_limit:]

        for sub_id, queue in self._subscribers.items():
            # 事件类型过滤
            allowed = self._filters.get(sub_id)
            if allowed and event.type not in allowed:
                continue

            # 背压：队列满时丢弃最旧的事件
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

            await queue.put(event)

    def emit_sync(self, event: StreamEvent) -> None:
        """同步发布（用于在同步上下文中调用）"""
        asyncio.get_event_loop().create_task(self.emit(event))

    async def replay(self, subscriber_id: str, since: str = "") -> None:
        """回放历史事件给新订阅者（从指定事件 ID 之后）"""
        if subscriber_id not in self._subscribers:
            return

        queue = self._subscribers[subscriber_id]
        replaying = since == ""

        for event in self._history:
            if not replaying:
                if event.id == since:
                    replaying = True
                continue

            allowed = self._filters.get(subscriber_id)
            if allowed and event.type not in allowed:
                continue

            if not queue.full():
                await queue.put(event)

    def get_history(
        self, event_types: set[EventType] | None = None, limit: int = 50
    ) -> list[StreamEvent]:
        """获取历史事件"""
        events = self._history
        if event_types:
            events = [e for e in events if e.type in event_types]
        return events[-limit:]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
```

### 3.2 便捷方法

```python
# src/naumi_agent/streaming/helpers.py

class EventHelper:
    """为 AgentEngine 提供便捷的事件发射方法"""

    def __init__(self, emitter: EventEmitter, session_id: str):
        self.emitter = emitter
        self.session_id = session_id
        self._turn = 0

    def _make_event(self, type: EventType, data: dict) -> StreamEvent:
        return StreamEvent(
            type=type,
            data=data,
            session_id=self.session_id,
            turn=self._turn,
        )

    async def agent_start(self, task: str) -> None:
        await self.emitter.emit(self._make_event(
            EventType.AGENT_START,
            {"task": task[:200]},
        ))

    async def agent_end(self, status: str, turns: int, tokens: int,
                        cost: float, duration: float) -> None:
        await self.emitter.emit(self._make_event(
            EventType.AGENT_END,
            {
                "status": status,
                "total_turns": turns,
                "total_tokens": tokens,
                "total_cost_usd": round(cost, 4),
                "duration_seconds": round(duration, 2),
            },
        ))

    async def token_delta(self, token: str, model: str) -> None:
        await self.emitter.emit(self._make_event(
            EventType.TOKEN_DELTA,
            {"token": token, "model": model},
        ))

    async def thinking_delta(self, token: str, model: str) -> None:
        await self.emitter.emit(self._make_event(
            EventType.THINKING_DELTA,
            {"token": token, "model": model},
        ))

    async def tool_call_start(self, call_id: str, tool_name: str,
                               args: dict, step_id: str = "") -> None:
        await self.emitter.emit(self._make_event(
            EventType.TOOL_CALL_START,
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "args": args,
                "step_id": step_id,
            },
        ))

    async def tool_call_end(self, call_id: str, tool_name: str,
                             status: str, result_preview: str,
                             duration_ms: int) -> None:
        await self.emitter.emit(self._make_event(
            EventType.TOOL_CALL_END,
            {
                "call_id": call_id,
                "tool_name": tool_name,
                "status": status,
                "result_preview": result_preview[:500],
                "duration_ms": duration_ms,
            },
        ))

    async def plan_created(self, plan_id: str, steps: list[dict],
                            mode: str) -> None:
        await self.emitter.emit(self._make_event(
            EventType.PLAN_CREATED,
            {
                "plan_id": plan_id,
                "steps": steps,
                "mode": mode,
            },
        ))

    async def plan_step_end(self, step_id: str, status: str,
                             summary: str) -> None:
        await self.emitter.emit(self._make_event(
            EventType.PLAN_STEP_END,
            {
                "step_id": step_id,
                "status": status,
                "summary": summary[:300],
            },
        ))

    async def budget_update(self, input_tokens: int, output_tokens: int,
                             cost: float, remaining: float, model: str) -> None:
        await self.emitter.emit(self._make_event(
            EventType.BUDGET_UPDATE,
            {
                "total_input_tokens": input_tokens,
                "total_output_tokens": output_tokens,
                "total_cost_usd": round(cost, 4),
                "remaining_usd": round(remaining, 4),
                "model": model,
            },
        ))

    def next_turn(self) -> None:
        self._turn += 1
```

## 4. LLM 流式处理

### 4.1 流式模型调用

```python
# src/naumi_agent/streaming/model_stream.py

from litellm import completion
import time


class ModelStreamHandler:
    """将 LiteLLM 流式响应当转为 StreamEvent 流"""

    def __init__(self, events: EventHelper):
        self.events = events

    async def stream_completion(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamEvent]:
        """流式调用 LLM，yield 每个 token 事件"""
        start_time = time.time()
        collected_content = []
        collected_tool_calls: dict[int, dict] = {}
        input_tokens = 0

        response = await completion(
            model=model,
            messages=messages,
            tools=tools,
            stream=True,
            stream_options={"include_usage": True},
            **kwargs,
        )

        async for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None

            # 处理 usage 信息（最后一个 chunk）
            if hasattr(chunk, "usage") and chunk.usage:
                input_tokens = chunk.usage.prompt_tokens or 0
                output_tokens = chunk.usage.completion_tokens or 0
                continue

            if not delta:
                continue

            # --- 思维链内容 ---
            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                await self.events.thinking_delta(delta.reasoning_content, model)

            # --- 文本内容 ---
            if delta.content:
                await self.events.token_delta(delta.content, model)
                collected_content.append(delta.content)

            # --- 工具调用 ---
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in collected_tool_calls:
                        collected_tool_calls[idx] = {
                            "id": tc.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    if tc.id:
                        collected_tool_calls[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            collected_tool_calls[idx]["name"] = tc.function.name
                        if tc.function.arguments:
                            collected_tool_calls[idx]["arguments"] += tc.function.arguments

        elapsed = int((time.time() - start_time) * 1000)

        return StreamResult(
            content="".join(collected_content),
            tool_calls=list(collected_tool_calls.values()),
            input_tokens=input_tokens,
            output_tokens=sum(len(c) for c in collected_content) // 4,  # 近似
            duration_ms=elapsed,
            model=model,
        )


@dataclass
class StreamResult:
    content: str
    tool_calls: list[dict]
    input_tokens: int
    output_tokens: int
    duration_ms: int
    model: str
```

### 4.2 工具调用流式执行

```python
# src/naumi_agent/streaming/tool_stream.py

class ToolStreamExecutor:
    """流式执行工具调用，实时报告进度"""

    def __init__(self, events: EventHelper, tool_registry: ToolRegistry):
        self.events = events
        self.tool_registry = tool_registry

    async def execute_with_streaming(
        self, tool_calls: list[dict]
    ) -> list[ToolResult]:
        """流式执行多个工具调用"""
        # 并行执行（对独立调用）
        tasks = [
            self._execute_single(tc)
            for tc in tool_calls
        ]

        if len(tasks) == 1:
            return [await tasks[0]]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [
            r if isinstance(r, ToolResult) else ToolResult(
                call_id=tc["id"],
                status="error",
                content=str(r),
            )
            for r, tc in zip(results, tool_calls)
        ]

    async def _execute_single(self, tool_call: dict) -> ToolResult:
        call_id = tool_call["id"]
        tool_name = tool_call["name"]
        args = json.loads(tool_call["arguments"])

        await self.events.tool_call_start(
            call_id=call_id,
            tool_name=tool_name,
            args=args,
        )

        start = time.time()
        try:
            tool = self.tool_registry.get(tool_name)
            result = await tool.execute(**args)
            duration = int((time.time() - start) * 1000)

            await self.events.tool_call_end(
                call_id=call_id,
                tool_name=tool_name,
                status="success",
                result_preview=result.content[:500],
                duration_ms=duration,
            )

            return ToolResult(
                call_id=call_id,
                status="success",
                content=result.content,
                duration_ms=duration,
            )
        except Exception as e:
            duration = int((time.time() - start) * 1000)

            await self.events.emitter.emit(StreamEvent(
                type=EventType.TOOL_CALL_ERROR,
                data={
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "error": str(e),
                    "duration_ms": duration,
                },
                session_id=self.events.session_id,
                turn=self.events._turn,
            ))

            return ToolResult(
                call_id=call_id,
                status="error",
                content=f"Error: {type(e).__name__}: {e}",
                duration_ms=duration,
            )
```

## 5. 集成到 AgentEngine

### 5.1 流式主循环

```python
# src/naumi_agent/orchestrator/engine.py（流式版本关键修改）

class AgentEngine:
    def __init__(self, config: AppConfig):
        self.config = config
        self.emitter = EventEmitter()
        self.model_stream = ModelStreamHandler(...)
        self.tool_stream = ToolStreamExecutor(...)

    async def run_stream(self, task: str, session: Session) -> AgentResult:
        """流式执行 — 与 run() 逻辑相同，但通过事件总线推送实时状态"""
        events = EventHelper(self.emitter, session.id)
        events.next_turn()

        await events.agent_start(task)

        start_time = time.time()
        session.messages.append(UserMessage(content=task))

        for turn in range(self.config.safety.max_turns):
            events.next_turn()
            await events.emitter.emit(StreamEvent(
                type=EventType.TURN_START,
                data={"turn": events._turn},
                session_id=session.id,
                turn=events._turn,
            ))

            # 1. LLM 调用（流式）
            result = await self.model_stream.stream_completion(
                model=self._select_model(session),
                messages=session.messages,
                tools=self.tool_registry.get_schemas(),
            )

            # 更新预算
            await events.budget_update(
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost=self.budget_tracker.total_cost,
                remaining=self.budget_tracker.remaining,
                model=result.model,
            )

            # 2. 处理工具调用
            if result.tool_calls:
                session.messages.append(AssistantMessage(
                    content=result.content,
                    tool_calls=result.tool_calls,
                ))

                tool_results = await self.tool_stream.execute_with_streaming(
                    result.tool_calls
                )

                session.messages.append(ToolResultMessage(tool_results))
                continue

            # 3. 没有工具调用 — 最终回答
            session.messages.append(AssistantMessage(content=result.content))
            break

        duration = time.time() - start_time
        await events.agent_end(
            status="completed",
            turns=events._turn,
            tokens=self.budget_tracker.total_input_tokens,
            cost=self.budget_tracker.total_cost,
            duration=duration,
        )

        return AgentResult(
            status="completed",
            response=result.content,
            usage=self.budget_tracker.get_summary(),
        )
```

### 5.2 上下文压缩事件

```python
# src/naumi_agent/memory/compactor.py（流式通知）

class ContextCompactor:
    async def compact(self, messages: list[Message]) -> list[Message]:
        if not self._should_compact(messages):
            return messages

        await self.events.emitter.emit(StreamEvent(
            type=EventType.CONTEXT_COMPACTED,
            data={
                "before_count": len(messages),
                "before_tokens": self._estimate_tokens(messages),
            },
            session_id=self.events.session_id,
        ))

        compacted = await self._do_compaction(messages)

        await self.events.emitter.emit(StreamEvent(
            type=EventType.CONTEXT_COMPACTED,
            data={
                "after_count": len(compacted),
                "after_tokens": self._estimate_tokens(compacted),
                "saved_tokens": self._estimate_tokens(messages) - self._estimate_tokens(compacted),
            },
            session_id=self.events.session_id,
        ))

        return compacted
```

## 6. 消费者端

### 6.1 TUI 消费者（Textual）

```python
# src/naumi_agent/tui/consumers.py

from textual.message import Message


class StreamConsumer:
    """将 EventEmitter 队列桥接到 Textual 消息系统"""

    def __init__(self, app: "NaumiApp", emitter: EventEmitter):
        self.app = app
        self.queue = emitter.subscribe("tui")
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._consume_loop())

    async def stop(self) -> None:
        self._running = False
        self._task.cancel()

    async def _consume_loop(self) -> None:
        while self._running:
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue

            # 将 StreamEvent 转为 Textual Message 并投递到主线程
            match event.type:
                case EventType.TOKEN_DELTA:
                    self.app.post_message(
                        AgentTokenMessage(token=event.data["token"])
                    )
                case EventType.THINKING_DELTA:
                    self.app.post_message(
                        AgentThinkingMessage(token=event.data["token"])
                    )
                case EventType.TOOL_CALL_START:
                    self.app.post_message(
                        ToolCallStartMessage(
                            tool_name=event.data["tool_name"],
                            args=event.data["args"],
                        )
                    )
                case EventType.TOOL_CALL_END:
                    self.app.post_message(
                        ToolCallEndMessage(
                            tool_name=event.data["tool_name"],
                            status=event.data["status"],
                            preview=event.data.get("result_preview", ""),
                            duration_ms=event.data.get("duration_ms", 0),
                        )
                    )
                case EventType.PLAN_CREATED:
                    self.app.post_message(
                        PlanCreatedMessage(steps=event.data["steps"])
                    )
                case EventType.PLAN_STEP_END:
                    self.app.post_message(
                        PlanStepEndMessage(
                            step_id=event.data["step_id"],
                            status=event.data["status"],
                            summary=event.data["summary"],
                        )
                    )
                case EventType.BUDGET_UPDATE:
                    self.app.post_message(
                        BudgetUpdateMessage(
                            cost=event.data["total_cost_usd"],
                            remaining=event.data["remaining_usd"],
                        )
                    )
                case EventType.AGENT_END:
                    self.app.post_message(
                        AgentEndMessage(
                            status=event.data["status"],
                            turns=event.data["total_turns"],
                            cost=event.data["total_cost_usd"],
                        )
                    )


# Textual 自定义消息定义
class AgentTokenMessage(Message):
    """LLM 输出 token"""
    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

class AgentThinkingMessage(Message):
    """思维链 token"""
    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

class ToolCallStartMessage(Message):
    def __init__(self, tool_name: str, args: dict) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.args = args

class ToolCallEndMessage(Message):
    def __init__(self, tool_name: str, status: str,
                 preview: str, duration_ms: int) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.status = status
        self.preview = preview
        self.duration_ms = duration_ms

class PlanCreatedMessage(Message):
    def __init__(self, steps: list[dict]) -> None:
        super().__init__()
        self.steps = steps

class PlanStepEndMessage(Message):
    def __init__(self, step_id: str, status: str, summary: str) -> None:
        super().__init__()
        self.step_id = step_id
        self.status = status
        self.summary = summary

class BudgetUpdateMessage(Message):
    def __init__(self, cost: float, remaining: float) -> None:
        super().__init__()
        self.cost = cost
        self.remaining = remaining

class AgentEndMessage(Message):
    def __init__(self, status: str, turns: int, cost: float) -> None:
        super().__init__()
        self.status = status
        self.turns = turns
        self.cost = cost
```

### 6.2 TUI 中处理流式消息

```python
# src/naumi_agent/tui/widgets/chat_panel.py（关键修改）

class ChatPanel(Widget):
    """聊天面板 — 处理流式渲染"""

    def __init__(self) -> None:
        super().__init__()
        self._current_agent_msg: AgentMessage | None = None
        self._current_thinking: str = ""

    def on_agent_token_message(self, msg: AgentTokenMessage) -> None:
        """逐 token 追加到当前 Agent 消息"""
        if self._current_agent_msg is None:
            self._current_agent_msg = AgentMessage()
            self.mount(self._current_agent_msg)
            self._current_agent_msg.scroll_visible()

        self._current_agent_msg.append_token(msg.token)

    def on_agent_thinking_message(self, msg: AgentThinkingMessage) -> None:
        """思维链渲染"""
        self._current_thinking += msg.token
        if self._current_agent_msg:
            self._current_agent_msg.update_thinking(self._current_thinking)

    def on_tool_call_start_message(self, msg: ToolCallStartMessage) -> None:
        """工具调用开始 — 在聊天中插入工具卡片"""
        if self._current_agent_msg:
            self._current_agent_msg.add_tool_call(
                tool_name=msg.tool_name,
                args=msg.args,
                status="running",
            )

    def on_tool_call_end_message(self, msg: ToolCallEndMessage) -> None:
        """工具调用完成 — 更新工具卡片状态"""
        if self._current_agent_msg:
            self._current_agent_msg.update_tool_call(
                tool_name=msg.tool_name,
                status=msg.status,
                preview=msg.preview,
                duration_ms=msg.duration_ms,
            )

    def on_agent_end_message(self, msg: AgentEndMessage) -> None:
        """Agent 回合结束 — 收尾当前消息"""
        if self._current_agent_msg:
            self._current_agent_msg.finalize(
                turns=msg.turns,
                cost=msg.cost,
            )
        self._current_agent_msg = None
        self._current_thinking = ""
```

## 7. 背压与性能

### 7.1 背压策略

```
生产速率（LLM tokens ~50-100/s）
          │
          ▼
    ┌───────────┐
    │  Queue     │ ← maxsize=1000
    │  (每个消费者│
    │   独立队列) │
    └─────┬─────┘
          │
    队列满？
     ├── 否 → 正常入队
     └── 是 → 丢弃最旧事件（滑动窗口）
              避免生产者阻塞
```

### 7.2 事件聚合

高频事件需要聚合以降低 UI 渲染压力：

```python
class EventAggregator:
    """将高频 token 事件聚合为批量更新"""

    def __init__(self, flush_interval: float = 0.03):  # ~33fps
        self._buffer: list[str] = []
        self._flush_interval = flush_interval
        self._last_flush = 0.0

    def add_token(self, token: str) -> str | None:
        """添加 token，达到刷新间隔时返回聚合文本"""
        self._buffer.append(token)
        now = time.time()

        if now - self._last_flush >= self._flush_interval:
            aggregated = "".join(self._buffer)
            self._buffer.clear()
            self._last_flush = now
            return aggregated

        return None

    def flush(self) -> str | None:
        """强制刷新剩余 buffer"""
        if not self._buffer:
            return None
        aggregated = "".join(self._buffer)
        self._buffer.clear()
        return aggregated
```

### 7.3 渲染节流

```python
class ThrottledRenderer:
    """Textual 渲染节流 — 避免 Markdown 重新解析导致的卡顿"""

    def __init__(self, widget: AgentMessage, interval: float = 0.05):
        self._widget = widget
        self._interval = interval
        self._content = ""
        self._dirty = False
        self._last_render = 0.0

    def append(self, token: str) -> None:
        self._content += token
        self._dirty = True
        now = time.time()
        if now - self._last_render >= self._interval:
            self._render()

    def finalize(self) -> None:
        if self._dirty:
            self._render()

    def _render(self) -> None:
        self._widget.update(Markdown(self._content))
        self._last_render = time.time()
        self._dirty = False
```

## 8. 性能指标

| 操作 | 目标延迟 | 说明 |
|------|---------|------|
| Token 到达 UI | < 50ms | 从 LLM 返回到屏幕渲染 |
| 工具调用状态更新 | < 100ms | 开始/完成通知 |
| 规划状态更新 | < 200ms | 步骤完成时更新 |
| 预算计数更新 | < 500ms | 每轮结束时批量更新 |
| 上下文压缩通知 | < 1s | 不阻塞主循环 |

## 9. 事件流示例

用户输入："帮我重构 main.py" 的完整事件流：

```
→ AGENT_START       { task: "帮我重构 main.py" }
→ TURN_START        { turn: 1 }
→ THINKING_START    { }
→ THINKING_DELTA    { token: "用户想要重构..." }
→ THINKING_END      { }
→ TOKEN_DELTA       { token: "我来" }
→ TOKEN_DELTA       { token: "帮你" }
→ TOOL_CALL_START   { tool: "file_read", args: {path: "main.py"} }
→ TOOL_CALL_END     { tool: "file_read", status: "success", duration: 80 }
→ TURN_END          { turn: 1 }

→ TURN_START        { turn: 2 }
→ TOKEN_DELTA       { token: "我已读取文件，" }
→ TOKEN_DELTA       { token: "现在开始重构..." }
→ TOOL_CALL_START   { tool: "file_edit", args: {path: "main.py", ...} }
→ TOOL_CALL_END     { tool: "file_edit", status: "success", duration: 150 }
→ BUDGET_UPDATE     { cost: 0.032, remaining: 4.968 }
→ TURN_END          { turn: 2 }

→ TURN_START        { turn: 3 }
→ TOKEN_DELTA       { token: "重构完成！" }
→ TOKEN_DELTA       { token: "主要变更：\n1. 拆分了..." }
→ AGENT_END         { status: "completed", turns: 3, cost: 0.032 }
```
