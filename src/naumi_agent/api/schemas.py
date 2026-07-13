"""API 数据模型."""

from __future__ import annotations

from typing import Any, Literal

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
    runtime_mode: Literal["default", "plan", "bypass"] = "default"
    workbench_issue: WorkbenchIssueFromMessage | None = None
    source_ids: list[str] = Field(default_factory=list, max_length=3)
    linked_issue_id: str | None = None


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    timestamp: str
    metadata: dict = Field(default_factory=dict)


class MessageListResponse(BaseModel):
    messages: list[MessageResponse]
    total: int


class ChatRunStepResponse(BaseModel):
    sequence: int
    stage: str
    status: str
    summary: str
    detail: str = ""
    event_id: str = ""
    started_at: str
    completed_at: str = ""
    metadata: dict = Field(default_factory=dict)


class ChatArtifactResponse(BaseModel):
    id: str
    kind: str
    title: str
    summary: dict = Field(default_factory=dict)
    status: str
    created_at: str
    metadata: dict = Field(default_factory=dict)


class ChatRunResponse(BaseModel):
    id: str
    session_id: str
    user_message_id: str
    assistant_message_id: str = ""
    status: str
    started_at: str
    updated_at: str
    completed_at: str = ""
    steps: list[ChatRunStepResponse] = Field(default_factory=list)
    artifacts: list[ChatArtifactResponse] = Field(default_factory=list)
    receipt: dict[str, Any] | None = None


class ChatRunListResponse(BaseModel):
    runs: list[ChatRunResponse]
    total: int


class ChatGitEnvironmentResponse(BaseModel):
    available: bool = False
    branch: str = ""
    changed_files: int = 0
    additions: int = 0
    deletions: int = 0
    ahead: int = 0
    behind: int = 0
    dirty: bool = False


class ChatBackgroundProcessResponse(BaseModel):
    id: str
    command: str
    pid: int | None = None
    status: str
    started_at: str = ""
    cwd: str


class ChatSourceReferenceResponse(BaseModel):
    id: str
    kind: str
    title: str
    path: str
    run_id: str = ""
    created_at: str


class ChatSourceCreate(BaseModel):
    path: str
    kind: Literal["file", "screenshot"] = "file"
    title: str = ""


class ChatEnvironmentResponse(BaseModel):
    session_id: str
    workspace_root: str
    workspace_name: str
    git: ChatGitEnvironmentResponse
    processes: list[ChatBackgroundProcessResponse] = Field(default_factory=list)
    sources: list[ChatSourceReferenceResponse] = Field(default_factory=list)


class ChatRunCancelResponse(BaseModel):
    status: Literal["cancellation_requested", "already_finished"]


class PermissionResolutionCreate(BaseModel):
    decision: Literal["allow", "deny", "bypass"]


class PermissionResolutionResponse(BaseModel):
    status: Literal["resolved"]


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict


class ModelInfo(BaseModel):
    id: str
    name: str
    provider: str
    tier: str
    upstream_id: str
    source: str
    max_context: int | None = None
    max_output: int | None = None
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    supports_vision: bool | None = None


class ConfigResponse(BaseModel):
    models: list[ModelInfo]
    model_warnings: list[str] = Field(default_factory=list)
    tools: list[ToolInfo]
    permission_mode: str
    max_budget_usd: float | None
    max_turns: int


class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_seconds: float
    active_sessions: int


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
