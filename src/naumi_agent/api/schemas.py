"""API 数据模型."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    title: str | None = None
    system_prompt: str | None = None
    model: str | None = None


class SessionResponse(BaseModel):
    id: str
    title: str | None
    model: str
    created_at: str
    updated_at: str
    message_count: int
    total_tokens: int
    total_cost_usd: float
    status: str


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]
    total: int
    page: int
    page_size: int


class MessageCreate(BaseModel):
    content: str
    stream: bool = True


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    timestamp: str
    metadata: dict = Field(default_factory=dict)


class MessageListResponse(BaseModel):
    messages: list[MessageResponse]
    total: int


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict


class ModelInfo(BaseModel):
    id: str
    name: str
    provider: str
    tier: str


class ConfigResponse(BaseModel):
    models: list[ModelInfo]
    tools: list[ToolInfo]
    permission_mode: str
    max_budget_usd: float
    max_turns: int


class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_seconds: float
    active_sessions: int


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
