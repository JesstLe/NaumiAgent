# 第十一部分：REST API 与 WebSocket 层

## 1. API 架构总览

NaumiAgent 提供三层对外接口：CLI（TUI）、REST API、WebSocket。REST 用于管理操作和简单查询，WebSocket 用于实时流式交互。

```
┌─────────────────────────────────────────────────────────────────┐
│                        客户端                                    │
│   TUI (Textual)  │  Web UI  │  curl / SDK  │  第三方集成         │
└────────┬──────────┴─────┬────┴──────┬───────┴────────┬──────────┘
         │                │           │                │
         ▼                ▼           ▼                ▼
    EventEmitter      WebSocket    REST API         REST API
    (内部队列)         (实时流)     (管理/查询)      (管理/查询)
         │                │           │                │
         └────────────────┴───────────┴────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │   FastAPI 应用     │
                    │                   │
                    │  ┌─────────────┐  │
                    │  │ Auth 中间件 │  │
                    │  └──────┬──────┘  │
                    │  ┌──────┴──────┐  │
                    │  │ Rate Limit  │  │
                    │  └──────┬──────┘  │
                    │  ┌──────┴──────┐  │
                    │  │  路由层     │  │
                    │  │  /sessions  │  │
                    │  │  /messages  │  │
                    │  │  /tools     │  │
                    │  │  /config    │  │
                    │  │  /ws/*      │  │
                    │  └──────┬──────┘  │
                    │  ┌──────┴──────┐  │
                    │  │ AgentEngine │  │
                    │  └─────────────┘  │
                    └───────────────────┘
```

## 2. FastAPI 应用

### 2.1 应用入口

```python
# src/naumi_agent/api/app.py

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：初始化引擎
    config = AppConfig.from_yaml("config.yaml")
    engine = AgentEngine(config)
    app.state.engine = engine
    app.state.config = config
    yield
    # 关闭：清理资源
    await engine.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="NaumiAgent API",
        version="0.1.0",
        description="通用智能 Agent 的 REST API 与 WebSocket 接口",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # 生产环境应限制
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册路由
    from .routes import sessions, messages, tools, config, ws, health
    app.include_router(health.router)
    app.include_router(sessions.router, prefix="/api/v1")
    app.include_router(messages.router, prefix="/api/v1")
    app.include_router(tools.router, prefix="/api/v1")
    app.include_router(config.router, prefix="/api/v1")
    app.include_router(ws.router, prefix="/api/v1")

    # 中间件
    from .middleware import AuthMiddleware, RateLimitMiddleware
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RateLimitMiddleware)

    return app


app = create_app()
```

### 2.2 依赖注入

```python
# src/naumi_agent/api/deps.py

from fastapi import Depends, HTTPException, Request


def get_engine(request: Request) -> AgentEngine:
    return request.app.state.engine


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


async def verify_api_key(request: Request) -> str:
    """API Key 验证"""
    api_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    config: AppConfig = request.app.state.config

    if not config.api.api_keys:
        return "anonymous"

    if not api_key or api_key not in config.api.api_keys:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return api_key


EngineDep = Depends(get_engine)
ConfigDep = Depends(get_config)
AuthDep = Depends(verify_api_key)
```

## 3. 数据模型

### 3.1 API Schema

```python
# src/naumi_agent/api/schemas.py

from pydantic import BaseModel, Field
from datetime import datetime


# --- 会话 ---

class SessionCreate(BaseModel):
    title: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    metadata: dict = Field(default_factory=dict)

class SessionResponse(BaseModel):
    id: str
    title: str | None
    model: str
    created_at: str
    updated_at: str
    message_count: int
    total_tokens: int
    total_cost_usd: float
    status: str  # "active" | "archived"

class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]
    total: int
    page: int
    page_size: int


# --- 消息 ---

class MessageCreate(BaseModel):
    content: str
    stream: bool = True  # 是否流式响应

class MessageResponse(BaseModel):
    id: str
    role: str  # "user" | "assistant" | "tool" | "system"
    content: str
    timestamp: str
    metadata: dict = Field(default_factory=dict)

class MessageListResponse(BaseModel):
    messages: list[MessageResponse]
    total: int


# --- 工具 ---

class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict  # JSON Schema

class ToolCallResponse(BaseModel):
    call_id: str
    tool_name: str
    args: dict
    status: str
    result_preview: str | None
    duration_ms: int | None


# --- 规划 ---

class PlanResponse(BaseModel):
    plan_id: str
    steps: list[dict]
    mode: str
    status: str  # "pending" | "executing" | "completed" | "failed"


# --- 配置 ---

class ModelInfo(BaseModel):
    id: str
    name: str
    provider: str
    tier: str  # "fast" | "capable" | "reasoning"

class ConfigResponse(BaseModel):
    models: list[ModelInfo]
    tools: list[ToolInfo]
    permission_mode: str
    max_budget_usd: float
    max_turns: int


# --- 通用 ---

class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
    code: str | None = None

class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_seconds: float
    active_sessions: int
```

## 4. REST 路由

### 4.1 健康检查

```python
# src/naumi_agent/api/routes/health.py

from fastapi import APIRouter, Depends
from ..schemas import HealthResponse
from ..deps import EngineDep

router = APIRouter(tags=["health"])

@router.get("/health", response_model=HealthResponse)
async def health_check(engine: EngineDep):
    return HealthResponse(
        status="healthy",
        version="0.1.0",
        uptime_seconds=engine.uptime,
        active_sessions=engine.active_session_count,
    )
```

### 4.2 会话管理

```python
# src/naumi_agent/api/routes/sessions.py

from fastapi import APIRouter, Depends, HTTPException, Query
from ..schemas import SessionCreate, SessionResponse, SessionListResponse
from ..deps import EngineDep, AuthDep

router = APIRouter(tags=["sessions"])

@router.post("/sessions", response_model=SessionResponse, status_code=201)
async def create_session(
    body: SessionCreate,
    engine: EngineDep,
    auth: str = AuthDep,
):
    """创建新会话"""
    session = await engine.create_session(
        title=body.title,
        system_prompt=body.system_prompt,
        model=body.model,
        metadata=body.metadata,
    )
    return _to_session_response(session)


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    engine: EngineDep = AuthDep and EngineDep,
):
    """列出会话"""
    sessions, total = await engine.list_sessions(page=page, page_size=page_size)
    return SessionListResponse(
        sessions=[_to_session_response(s) for s in sessions],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    engine: EngineDep,
    auth: str = AuthDep,
):
    """获取会话详情"""
    session = await engine.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return _to_session_response(session)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    engine: EngineDep,
    auth: str = AuthDep,
):
    """删除会话"""
    deleted = await engine.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")


@router.get("/sessions/{session_id}/plan", response_model=PlanResponse)
async def get_session_plan(
    session_id: str,
    engine: EngineDep,
    auth: str = AuthDep,
):
    """获取当前会话的执行规划"""
    plan = await engine.get_session_plan(session_id)
    if not plan:
        raise HTTPException(status_code=404, detail="No active plan")
    return PlanResponse(
        plan_id=plan.id,
        steps=[_to_step_dict(s) for s in plan.steps],
        mode=plan.mode.value,
        status=plan.status,
    )


def _to_session_response(session) -> SessionResponse:
    return SessionResponse(
        id=session.id,
        title=session.title,
        model=session.model,
        created_at=session.created_at.isoformat(),
        updated_at=session.updated_at.isoformat(),
        message_count=len(session.messages),
        total_tokens=session.total_tokens,
        total_cost_usd=session.total_cost_usd,
        status=session.status,
    )
```

### 4.3 消息

```python
# src/naumi_agent/api/routes/messages.py

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from ..schemas import MessageCreate, MessageResponse, MessageListResponse
from ..deps import EngineDep, AuthDep

router = APIRouter(tags=["messages"])

@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: MessageCreate,
    engine: EngineDep,
    auth: str = AuthDep,
):
    """发送消息 — 支持流式和非流式"""
    session = await engine.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if body.stream:
        return StreamingResponse(
            _stream_response(engine, session_id, body.content),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        result = await engine.run(body.content, session_id=session_id)
        return MessageResponse(
            id=result.message_id,
            role="assistant",
            content=result.response,
            timestamp=datetime.now().isoformat(),
            metadata={
                "turns": result.usage.turns,
                "cost_usd": result.usage.total_cost,
                "tokens": result.usage.total_tokens,
            },
        )


@router.get("/sessions/{session_id}/messages", response_model=MessageListResponse)
async def list_messages(
    session_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    engine: EngineDep = AuthDep and EngineDep,
):
    """获取会话消息历史"""
    messages, total = await engine.get_messages(
        session_id, page=page, page_size=page_size
    )
    return MessageListResponse(
        messages=[
            MessageResponse(
                id=m.id,
                role=m.role,
                content=m.content,
                timestamp=m.timestamp.isoformat(),
                metadata=m.metadata,
            )
            for m in messages
        ],
        total=total,
    )


async def _stream_response(
    engine: AgentEngine, session_id: str, content: str
) -> AsyncIterator[str]:
    """SSE 流式响应生成器"""
    subscriber_id = f"sse_{session_id}_{uuid.uuid4().hex[:8]}"
    queue = engine.emitter.subscribe(
        subscriber_id,
        event_types={
            EventType.TOKEN_DELTA,
            EventType.THINKING_DELTA,
            EventType.TOOL_CALL_START,
            EventType.TOOL_CALL_END,
            EventType.PLAN_CREATED,
            EventType.PLAN_STEP_END,
            EventType.BUDGET_UPDATE,
            EventType.AGENT_END,
            EventType.AGENT_ERROR,
        },
    )

    # 启动 Agent 任务（后台）
    agent_task = asyncio.create_task(
        engine.run_stream(content, session_id=session_id)
    )

    try:
        while not agent_task.done():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield event.to_sse()
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"

        # 消费剩余事件
        while not queue.empty():
            event = queue.get_nowait()
            yield event.to_sse()

    finally:
        engine.emitter.unsubscribe(subscriber_id)
        # 确保任务完成
        try:
            await agent_task
        except Exception:
            pass
```

### 4.4 工具与配置

```python
# src/naumi_agent/api/routes/tools.py

from fastapi import APIRouter, Depends
from ..schemas import ToolInfo, ToolCallResponse
from ..deps import EngineDep, AuthDep

router = APIRouter(tags=["tools"])

@router.get("/tools", response_model=list[ToolInfo])
async def list_tools(engine: EngineDep, auth: str = AuthDep):
    """列出可用工具"""
    return [
        ToolInfo(
            name=tool.name,
            description=tool.description,
            parameters=tool.schema.parameters,
        )
        for tool in engine.tool_registry.all()
    ]


@router.get("/tools/{tool_name}", response_model=ToolInfo)
async def get_tool(tool_name: str, engine: EngineDep, auth: str = AuthDep):
    """获取工具详情"""
    tool = engine.tool_registry.get(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    return ToolInfo(
        name=tool.name,
        description=tool.description,
        parameters=tool.schema.parameters,
    )


# src/naumi_agent/api/routes/config.py

router = APIRouter(tags=["config"])

@router.get("/config", response_model=ConfigResponse)
async def get_config(engine: EngineDep, auth: str = AuthDep):
    """获取当前配置（脱敏）"""
    config = engine.config
    return ConfigResponse(
        models=[
            ModelInfo(
                id="claude-sonnet-4-6",
                name="Claude Sonnet 4.6",
                provider="anthropic",
                tier="capable",
            ),
            ModelInfo(
                id="claude-haiku-4-5",
                name="Claude Haiku 4.5",
                provider="anthropic",
                tier="fast",
            ),
            ModelInfo(
                id="claude-opus-4-7",
                name="Claude Opus 4.7",
                provider="anthropic",
                tier="reasoning",
            ),
        ],
        tools=[
            ToolInfo(name=t.name, description=t.description, parameters=t.schema.parameters)
            for t in engine.tool_registry.all()
        ],
        permission_mode=config.safety.permission_mode,
        max_budget_usd=config.safety.max_budget_usd,
        max_turns=config.safety.max_turns,
    )
```

## 5. WebSocket 接口

### 5.1 协议设计

```
客户端 ──────────────────────────────────── 服务端

  连接 WS /api/v1/ws/sessions/{id}
                    │
                    ├─ 服务端: {"type": "connected", "session_id": "..."}
                    │
  ── {"type": "message", "content": "帮我..."} ──▶
                    │
                    ├─ 服务端: {"type": "agent_start", ...}
                    ├─ 服务端: {"type": "token_delta", "token": "我", ...}
                    ├─ 服务端: {"type": "token_delta", "token": "来", ...}
                    ├─ 服务端: {"type": "tool_call_start", ...}
                    ├─ 服务端: {"type": "tool_call_end", ...}
                    ├─ 服务端: {"type": "agent_end", ...}
                    │
  ◀── {"type": "message_ack", "message_id": "..."} ──
                    │
  ── {"type": "interrupt"} ───────────────────▶  中断当前任务
                    │
                    ├─ 服务端: {"type": "interrupted", ...}
                    │
  ── {"type": "ping"} ───────────────────────▶
                    │
                    ├─ 服务端: {"type": "pong", "timestamp": "..."}
```

### 5.2 WebSocket 路由

```python
# src/naumi_agent/api/routes/ws.py

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import json

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/sessions/{session_id}")
async def websocket_session(websocket: WebSocket, session_id: str):
    """WebSocket 会话端点"""
    await websocket.accept()

    engine: AgentEngine = websocket.app.state.engine

    session = await engine.get_session(session_id)
    if not session:
        await websocket.send_json({"type": "error", "message": "Session not found"})
        await websocket.close()
        return

    # 订阅事件流
    subscriber_id = f"ws_{session_id}_{uuid.uuid4().hex[:8]}"
    queue = engine.emitter.subscribe(subscriber_id)

    await websocket.send_json({
        "type": "connected",
        "session_id": session_id,
    })

    # 启动事件推送协程
    push_task = asyncio.create_task(
        _push_events(websocket, queue)
    )

    try:
        # 接收客户端消息
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            match msg_type:
                case "message":
                    await _handle_message(
                        websocket, engine, session_id, data, queue
                    )

                case "interrupt":
                    await engine.interrupt(session_id)
                    await websocket.send_json({
                        "type": "interrupted",
                        "session_id": session_id,
                    })

                case "ping":
                    await websocket.send_json({
                        "type": "pong",
                        "timestamp": datetime.now().isoformat(),
                    })

                case _:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Unknown message type: {msg_type}",
                    })

    except WebSocketDisconnect:
        pass
    finally:
        engine.emitter.unsubscribe(subscriber_id)
        push_task.cancel()


async def _push_events(
    websocket: WebSocket, queue: asyncio.Queue[StreamEvent]
) -> None:
    """持续推送事件到 WebSocket 客户端"""
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=30.0)
            await websocket.send_text(event.to_ws())
        except asyncio.TimeoutError:
            try:
                await websocket.send_json({"type": "ping"})
            except Exception:
                break
        except Exception:
            break


async def _handle_message(
    websocket: WebSocket,
    engine: AgentEngine,
    session_id: str,
    data: dict,
    queue: asyncio.Queue[StreamEvent],
) -> None:
    """处理客户端发送的消息"""
    content = data.get("content", "")
    if not content:
        await websocket.send_json({"type": "error", "message": "Empty message"})
        return

    # 后台启动 Agent
    task = asyncio.create_task(
        engine.run_stream(content, session_id=session_id)
    )

    # 消息确认
    await websocket.send_json({
        "type": "message_received",
        "content_preview": content[:100],
    })

    # 等待完成
    try:
        result = await task
        await websocket.send_json({
            "type": "message_complete",
            "message_id": result.message_id,
            "status": result.status,
        })
    except Exception as e:
        await websocket.send_json({
            "type": "error",
            "message": f"Agent error: {type(e).__name__}: {e}",
        })
```

### 5.3 新建会话的 WebSocket 快捷方式

```python
@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """无需预先创建 session 的快捷聊天入口"""
    await websocket.accept()
    engine: AgentEngine = websocket.app.state.engine

    session = await engine.create_session(title="WebSocket Chat")

    subscriber_id = f"ws_quick_{session.id}"
    queue = engine.emitter.subscribe(subscriber_id)

    await websocket.send_json({
        "type": "connected",
        "session_id": session.id,
    })

    push_task = asyncio.create_task(_push_events(websocket, queue))

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            if data.get("type") == "message":
                content = data.get("content", "")
                if content:
                    task = asyncio.create_task(
                        engine.run_stream(content, session_id=session.id)
                    )
                    await websocket.send_json({"type": "message_received"})
                    try:
                        result = await task
                        await websocket.send_json({
                            "type": "message_complete",
                            "status": result.status,
                        })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "error",
                            "message": str(e),
                        })

    except WebSocketDisconnect:
        pass
    finally:
        engine.emitter.unsubscribe(subscriber_id)
        push_task.cancel()
        await engine.save_session(session.id)
```

## 6. 中间件

### 6.1 认证

```python
# src/naumi_agent/api/middleware.py

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class AuthMiddleware(BaseHTTPMiddleware):
    """API Key 认证中间件"""

    # 不需要认证的路径
    PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)

        # WebSocket 走查询参数认证
        if request.url.path.startswith("/api/v1/ws"):
            api_key = request.query_params.get("api_key")
            config: AppConfig = request.app.state.config
            if config.api.api_keys and api_key not in config.api.api_keys:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Invalid API key"},
                )
            return await call_next(request)

        # REST 走 Header 认证
        api_key = request.headers.get("X-API-Key")
        config: AppConfig = request.app.state.config

        if config.api.api_keys:
            if not api_key or api_key not in config.api.api_keys:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Invalid or missing API key"},
                }

        return await call_next(request)
```

### 6.2 限流

```python
class RateLimitMiddleware(BaseHTTPMiddleware):
    """简单的令牌桶限流"""

    def __init__(self, app, requests_per_minute: int = 60):
        super().__init__(app)
        self.rpm = requests_per_minute
        self._buckets: dict[str, list[float]] = {}

    async def dispatch(self, request: Request, call_next):
        # WebSocket 不限流（连接级限制）
        if request.url.path.startswith("/api/v1/ws"):
            return await call_next(request)

        client_id = request.client.host if request.client else "unknown"
        now = time.time()

        if client_id not in self._buckets:
            self._buckets[client_id] = []

        # 清理过期记录
        self._buckets[client_id] = [
            t for t in self._buckets[client_id]
            if now - t < 60
        ]

        if len(self._buckets[client_id]) >= self.rpm:
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded", "retry_after": 60},
                headers={"Retry-After": "60"},
            )

        self._buckets[client_id].append(now)
        return await call_next(request)
```

## 7. 配置扩展

```yaml
# config.yaml — API 配置部分

api:
  host: "0.0.0.0"
  port: 8080
  workers: 1  # Agent 是有状态的，推荐单 worker

  # API Key 认证（为空则不需要认证）
  api_keys:
    - "naumi-key-xxxx"

  # CORS
  cors_origins:
    - "http://localhost:3000"
    - "http://localhost:5173"

  # 限流
  rate_limit:
    requests_per_minute: 60
    ws_max_connections: 10

  # SSE
  sse:
    keepalive_interval: 30
    max_connections: 20
```

```python
# 配置类扩展
class APIConfig(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8080
    workers: int = 1
    api_keys: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    rate_limit_rpm: int = 60
    ws_max_connections: int = 10
    sse_keepalive: int = 30
    sse_max_connections: int = 20
```

## 8. 启动与部署

### 8.1 CLI 启动

```python
# src/naumi_agent/main.py — 新增 serve 命令

@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", "-h"),
    port: int = typer.Option(8080, "--port", "-p"),
    config: str = typer.Option("config.yaml", "--config", "-c"),
    reload: bool = typer.Option(False, "--reload", help="开发模式热重载"),
):
    """启动 REST API 服务"""
    import uvicorn

    if reload:
        uvicorn.run(
            "naumi_agent.api.app:app",
            host=host,
            port=port,
            reload=True,
            reload_dirs=["src/naumi_agent"],
        )
    else:
        uvicorn.run(
            "naumi_agent.api.app:app",
            host=host,
            port=port,
            workers=1,
            log_level="info",
        )
```

### 8.2 Docker

```dockerfile
# Dockerfile.api

FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install -e "."

COPY src/ src/
COPY config.yaml .

EXPOSE 8080

CMD ["naumi", "serve", "--host", "0.0.0.0", "--port", "8080"]
```

### 8.3 Docker Compose

```yaml
# docker-compose.yaml

services:
  naumi-api:
    build:
      context: .
      dockerfile: Dockerfile.api
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
      - ./config.yaml:/app/config.yaml:ro
    environment:
      - NAUMI_MODELS__DEFAULT_MODEL=claude-sonnet-4-6
    env_file:
      - .env  # ANTHROPIC_API_KEY 等
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 5s
      retries: 3
```

## 9. API 使用示例

### 9.1 curl

```bash
# 创建会话
curl -X POST http://localhost:8080/api/v1/sessions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: naumi-key-xxxx" \
  -d '{"title": "代码重构"}'

# 响应
{"id": "sess_abc123", "title": "代码重构", "model": "claude-sonnet-4-6", ...}

# 发送消息（非流式）
curl -X POST http://localhost:8080/api/v1/sessions/sess_abc123/messages \
  -H "Content-Type: application/json" \
  -H "X-API-Key: naumi-key-xxxx" \
  -d '{"content": "帮我重构 main.py", "stream": false}'

# 发送消息（流式 SSE）
curl -N http://localhost:8080/api/v1/sessions/sess_abc123/messages \
  -H "Content-Type: application/json" \
  -H "X-API-Key: naumi-key-xxxx" \
  -d '{"content": "帮我重构 main.py", "stream": true}'

# SSE 输出
data: {"id":"evt_1","type":"agent_start","data":{"task":"帮我重构 main.py"},...}
data: {"id":"evt_2","type":"token_delta","data":{"token":"我来"},...}
data: {"id":"evt_3","type":"tool_call_start","data":{"tool_name":"file_read"},...}
data: {"id":"evt_4","type":"agent_end","data":{"status":"completed"},...}
```

### 9.2 WebSocket (JavaScript)

```javascript
// 连接（带 API Key）
const ws = new WebSocket(
  "ws://localhost:8080/api/v1/ws/sessions/sess_abc123?api_key=naumi-key-xxxx"
);

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  switch (data.type) {
    case "connected":
      console.log("已连接，session:", data.session_id);
      break;

    case "token_delta":
      process.stdout.write(data.data.token);
      break;

    case "tool_call_start":
      console.log(`\n[工具] ${data.data.tool_name}`, data.data.args);
      break;

    case "tool_call_end":
      console.log(`[完成] ${data.data.tool_name} (${data.data.duration_ms}ms)`);
      break;

    case "agent_end":
      console.log(`\n完成。轮次: ${data.data.total_turns}, 费用: $${data.data.total_cost_usd}`);
      break;

    case "ping":
      ws.send(JSON.stringify({ type: "pong" }));
      break;
  }
};

// 发送消息
ws.send(JSON.stringify({
  type: "message",
  content: "帮我重构 main.py",
}));

// 中断
ws.send(JSON.stringify({ type: "interrupt" }));
```

### 9.3 Python SDK

```python
# src/naumi_agent/client.py — 简易 Python 客户端

import httpx
import json
import asyncio


class NaumiClient:
    """NaumiAgent Python 客户端"""

    def __init__(self, base_url: str = "http://localhost:8080", api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._headers = {"X-API-Key": api_key} if api_key else {}

    async def create_session(self, title: str | None = None) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/v1/sessions",
                json={"title": title},
                headers=self._headers,
            )
            return resp.json()

    async def send_message(
        self, session_id: str, content: str, stream: bool = False
    ) -> dict | AsyncIterator[dict]:
        async with httpx.AsyncClient() as client:
            if not stream:
                resp = await client.post(
                    f"{self.base_url}/api/v1/sessions/{session_id}/messages",
                    json={"content": content, "stream": False},
                    headers=self._headers,
                )
                return resp.json()

            # SSE 流式
            async with client.stream(
                "POST",
                f"{self.base_url}/api/v1/sessions/{session_id}/messages",
                json={"content": content, "stream": True},
                headers=self._headers,
            ) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        yield json.loads(line[6:])


# 使用示例
async def main():
    client = NaumiClient(api_key="naumi-key-xxxx")

    session = await client.create_session(title="代码重构")
    session_id = session["id"]

    async for event in client.send_message(session_id, "帮我重构 main.py", stream=True):
        if event["type"] == "token_delta":
            print(event["data"]["token"], end="", flush=True)
        elif event["type"] == "agent_end":
            print(f"\n完成！费用: ${event['data']['total_cost_usd']}")
```

## 10. 接口总览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /health | 健康检查 |
| POST | /api/v1/sessions | 创建会话 |
| GET | /api/v1/sessions | 列出会话 |
| GET | /api/v1/sessions/{id} | 获取会话详情 |
| DELETE | /api/v1/sessions/{id} | 删除会话 |
| GET | /api/v1/sessions/{id}/plan | 获取执行规划 |
| POST | /api/v1/sessions/{id}/messages | 发送消息（支持 SSE） |
| GET | /api/v1/sessions/{id}/messages | 获取消息历史 |
| GET | /api/v1/tools | 列出可用工具 |
| GET | /api/v1/tools/{name} | 获取工具详情 |
| GET | /api/v1/config | 获取当前配置 |
| WS | /api/v1/ws/sessions/{id} | WebSocket 会话（实时流） |
| WS | /api/v1/ws/chat | WebSocket 快捷聊天 |
