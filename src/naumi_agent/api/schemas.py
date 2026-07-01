"""API 数据模型."""

from __future__ import annotations

from pydantic import BaseModel, Field

from naumi_agent.workbench.models import ParallelMode, RiskLevel


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


class WorkbenchIssueFromMessage(BaseModel):
    mission_id: str
    title: str
    description: str = ""
    blocked_by: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    parallel_mode: ParallelMode = ParallelMode.EXCLUSIVE
    risk_level: RiskLevel = RiskLevel.MEDIUM


class MessageCreate(BaseModel):
    content: str
    stream: bool = True
    workbench_issue: WorkbenchIssueFromMessage | None = None


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
