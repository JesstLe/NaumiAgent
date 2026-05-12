# 第四部分：记忆系统

## 1. 三层记忆架构

```
┌──────────────────────────────────────────────────────────────┐
│                     工作记忆 (Working Memory)                  │
│                                                               │
│  存储位置：LLM 的上下文窗口（in-context）                      │
│  容量：     128K tokens（Claude Sonnet 4.6）                   │
│  生命周期：  单次任务执行期间                                   │
│  管理策略：  Context Compaction（上下文压缩）                   │
│                                                               │
│  内容：                                                       │
│  ├─ System Prompt + 工具描述                                  │
│  ├─ 对话历史（用户消息 + Agent 回复）                          │
│  ├─ 工具调用记录和结果                                         │
│  └─ 当前任务计划和进度                                         │
├──────────────────────────────────────────────────────────────┤
│                     短期记忆 (Short-term Memory)               │
│                                                               │
│  存储位置：内存 + SQLite                                      │
│  容量：     无硬性限制                                         │
│  生命周期：  单次会话                                          │
│  管理策略：  自动摘要 + 关键信息提取                           │
│                                                               │
│  内容：                                                       │
│  ├─ 完整对话历史（不受上下文窗口限制）                          │
│  ├─ 所有工具调用的详细记录                                     │
│  ├─ 中间计算结果                                               │
│  └─ 任务执行的状态快照                                         │
├──────────────────────────────────────────────────────────────┤
│                     长期记忆 (Long-term Memory)                │
│                                                               │
│  存储位置：向量数据库（ChromaDB / pgvector）                   │
│  容量：     无硬性限制                                         │
│  生命周期：  跨会话持久化                                      │
│  管理策略：  向量检索 + 相关性排序                              │
│                                                               │
│  内容：                                                       │
│  ├─ 用户偏好和习惯                                             │
│  ├─ 历史任务摘要                                               │
│  ├─ 学到的知识和经验                                           │
│  └─ 常用代码片段和模式                                         │
└──────────────────────────────────────────────────────────────┘
```

## 2. 上下文压缩（Context Compaction）

上下文窗口是稀缺资源。当消息历史接近窗口上限时，自动压缩历史消息。

### 2.1 压缩策略

```python
# src/naumi_agent/memory/compaction.py

COMPACTION_PROMPT = """请将以下对话历史压缩为简洁的摘要。

保留以下信息：
1. 用户的原始任务和关键需求
2. 已完成的关键步骤及其结果
3. 尚未完成的步骤
4. 重要的中间发现和决策
5. 用户的偏好和约束

删除：
- 重复的或冗余的对话
- 失败的尝试（除非包含重要教训）
- 工具调用的原始输出（保留关键结论即可）

对话历史：
{messages}

输出压缩后的摘要（不超过 {max_tokens} tokens）："""

class ContextCompactor:
    def __init__(self, model_router: ModelRouter):
        self.model_router = model_router

    async def compact(self, messages: list[Message]) -> list[Message]:
        """压缩消息历史"""
        # 1. 分离不可压缩的消息（最近的 + 系统消息）
        system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
        recent_msgs = messages[-4:]  # 保留最近 2 轮对话
        old_msgs = messages[len(system_msgs):-4]

        if not old_msgs:
            return messages  # 不需要压缩

        # 2. 将旧消息压缩为摘要
        old_text = self._serialize_messages(old_msgs)
        summary = await self.model_router.call(
            messages=[
                SystemMessage(COMPACTION_PROMPT.format(
                    messages=old_text,
                    max_tokens=2000,
                ))
            ],
            model_tier="fast",  # 用 Haiku 压缩，节省成本
            max_tokens=2000,
        )

        # 3. 重建消息列表
        compacted = [
            *system_msgs,
            CompactionMessage(content=summary.content),  # 插入压缩摘要
            *recent_msgs,
        ]
        return compacted

    def estimate_tokens(self, messages: list[Message]) -> int:
        """估算消息列表的 token 数"""
        # 粗略估算：中文 1 字 ≈ 1 token，英文 4 字符 ≈ 1 token
        total = 0
        for msg in messages:
            total += len(msg.content) // 3
        return total

    def needs_compaction(self, messages: list[Message], window_size: int) -> bool:
        """判断是否需要压缩"""
        used = self.estimate_tokens(messages)
        threshold = int(window_size * 0.75)  # 75% 时触发压缩
        return used > threshold
```

### 2.2 压缩触发时机

```
每次 LLM 调用前检查：
  token 使用量 > 窗口 * 75%  →  触发压缩

压缩后：
  [System Prompt]
  [压缩摘要 — 之前对话的关键信息]
  [最近 2 轮对话 — 保持精确]
  [当前任务上下文]
```

## 3. 短期记忆（Session Memory）

```python
# src/naumi_agent/memory/short_term.py

from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class SessionMemory:
    session_id: str
    created_at: datetime
    messages: list[Message] = field(default_factory=list)
    tool_history: list[ToolRecord] = field(default_factory=list)
    task_snapshots: list[TaskSnapshot] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def add_message(self, message: Message) -> None:
        self.messages.append(message)

    def add_tool_record(self, record: ToolRecord) -> None:
        self.tool_history.append(record)

    def snapshot(self, label: str) -> None:
        """保存当前任务状态快照"""
        self.task_snapshots.append(TaskSnapshot(
            label=label,
            timestamp=datetime.now(),
            message_count=len(self.messages),
            last_tool=self.tool_history[-1].tool_name if self.tool_history else None,
        ))

    def get_tool_results(self, tool_name: str) -> list[str]:
        """获取指定工具的所有执行结果"""
        return [
            r.result for r in self.tool_history
            if r.tool_name == tool_name and r.status == "success"
        ]

    def get_recent_context(self, n_turns: int = 5) -> str:
        """获取最近 n 轮的上下文摘要"""
        recent = self.messages[-(n_turns * 2):]  # 每轮 2 条消息
        return "\n".join(m.content for m in recent)

class SessionStore:
    """会话持久化存储"""

    def __init__(self, db_path: str = "data/sessions.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,    -- JSON 序列化的 SessionMemory
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    async def save(self, session: SessionMemory) -> None:
        """保存会话"""
        import json, sqlite3
        data = json.dumps(session.__dict__, default=str)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO sessions (id, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (session.session_id, data, session.created_at.isoformat(), datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

    async def load(self, session_id: str) -> SessionMemory | None:
        """加载会话"""
        import json, sqlite3
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT data FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        conn.close()

        if not row:
            return None

        return SessionMemory(**json.loads(row[0]))

    async def list_sessions(self, limit: int = 20) -> list[dict]:
        """列出最近的会话"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT id, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [{"id": r[0], "created_at": r[1], "updated_at": r[2]} for r in rows]
```

## 4. 长期记忆（Long-term Memory）

### 4.1 向量存储

```python
# src/naumi_agent/memory/long_term.py

import chromadb
from dataclasses import dataclass

@dataclass
class MemoryEntry:
    id: str
    content: str              # 记忆内容
    category: str             # "preference" | "knowledge" | "task_summary" | "pattern"
    embedding: list[float]    # 向量嵌入
    metadata: dict            # 附加元数据
    created_at: str
    access_count: int = 0     # 被检索次数（用于遗忘策略）

class LongTermMemory:
    def __init__(self, persist_dir: str = "data/chroma"):
        self.client = chromadb.PersistentClient(path=persist_dir)
        self.collection = self.client.get_or_create_collection(
            name="naumi_memory",
            metadata={"hnsw:space": "cosine"},
        )

    async def store(self, entry: MemoryEntry) -> None:
        """存储一条记忆"""
        self.collection.upsert(
            ids=[entry.id],
            documents=[entry.content],
            metadatas=[{
                "category": entry.category,
                "created_at": entry.created_at,
                "access_count": entry.access_count,
                **entry.metadata,
            }],
        )

    async def recall(
        self,
        query: str,
        category: str | None = None,
        top_k: int = 5,
    ) -> list[MemoryEntry]:
        """检索相关记忆"""
        where_filter = {"category": category} if category else None

        results = self.collection.query(
            query_texts=[query],
            n_results=top_k,
            where=where_filter,
        )

        entries = []
        for i in range(len(results["ids"][0])):
            entries.append(MemoryEntry(
                id=results["ids"][0][i],
                content=results["documents"][0][i],
                category=results["metadatas"][0][i].get("category", ""),
                embedding=[],
                metadata=results["metadatas"][0][i],
                created_at=results["metadatas"][0][i].get("created_at", ""),
            ))
        return entries

    async def store_user_preference(self, key: str, value: str) -> None:
        """存储用户偏好"""
        await self.store(MemoryEntry(
            id=f"pref_{key}",
            content=f"用户偏好：{key} = {value}",
            category="preference",
            embedding=[],
            metadata={"key": key, "value": value},
            created_at=datetime.now().isoformat(),
        ))

    async def store_task_summary(
        self, task: str, result: str, lessons: list[str]
    ) -> None:
        """存储任务摘要和经验"""
        content = f"任务：{task}\n结果：{result}\n经验：{'; '.join(lessons)}"
        await self.store(MemoryEntry(
            id=f"task_{uuid4().hex[:8]}",
            content=content,
            category="task_summary",
            embedding=[],
            metadata={"task": task},
            created_at=datetime.now().isoformat(),
        ))

    async def get_relevant_context(self, task: str) -> str:
        """获取与当前任务相关的记忆上下文"""
        memories = await self.recall(task, top_k=3)

        if not memories:
            return ""

        context_parts = ["## 相关记忆"]
        for m in memories:
            context_parts.append(f"- {m.content}")

        return "\n".join(context_parts)
```

### 4.2 记忆遗忘策略

```python
class MemoryForgetter:
    """基于时间和访问频率的记忆遗忘"""

    def __init__(self, memory: LongTermMemory):
        self.memory = memory

    async def forget_outdated(self, days: int = 90) -> int:
        """删除超过 N 天且未被访问的记忆"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        # 查找过期记忆
        results = self.memory.collection.get(
            where={"created_at": {"$lt": cutoff}},
        )

        if results["ids"]:
            self.memory.collection.delete(ids=results["ids"])
            return len(results["ids"])
        return 0

    async def decay_access_counts(self) -> None:
        """衰减所有记忆的访问计数（模拟遗忘曲线）"""
        all_data = self.memory.collection.get()

        for i, id in enumerate(all_data["ids"]):
            meta = all_data["metadatas"][i]
            meta["access_count"] = max(0, meta.get("access_count", 0) - 1)

            self.memory.collection.update(
                ids=[id],
                metadatas=[meta],
            )
```

## 5. 记忆系统集成

```python
# src/naumi_agent/memory/__init__.py

class MemorySystem:
    """统一记忆管理"""

    def __init__(self, config: MemoryConfig):
        self.compactor = ContextCompactor(None)  # 后续注入 model_router
        self.session_store = SessionStore(config.session_db_path)
        self.long_term = LongTermMemory(config.vector_db_path)

    def inject_model_router(self, router: ModelRouter) -> None:
        self.compactor = ContextCompactor(router)

    async def compact_if_needed(self, session: SessionMemory) -> None:
        """检查并执行上下文压缩"""
        if self.compactor.needs_compaction(session.messages, window_size=128000):
            session.messages = await self.compactor.compact(session.messages)

    async def get_context_for_task(self, task: str, session: SessionMemory) -> str:
        """为任务准备记忆上下文"""
        parts = []

        # 长期记忆中的相关信息
        relevant = await self.long_term.get_relevant_context(task)
        if relevant:
            parts.append(relevant)

        # 用户偏好
        prefs = await self.long_term.recall(task, category="preference", top_k=3)
        if prefs:
            parts.append("## 用户偏好")
            for p in prefs:
                parts.append(f"- {p.content}")

        return "\n\n".join(parts)

    async def save_session(self, session: SessionMemory) -> None:
        """保存会话，提取经验存入长期记忆"""
        await self.session_store.save(session)

    async def summarize_and_remember(self, session: SessionMemory) -> None:
        """会话结束时提取关键信息存入长期记忆"""
        if len(session.messages) < 4:
            return

        # 提取任务摘要
        task = session.messages[0].content if session.messages else ""
        result = session.messages[-1].content if session.messages else ""

        await self.long_term.store_task_summary(
            task=task,
            result=result,
            lessons=[],  # 后续可以用 LLM 提取
        )
```
