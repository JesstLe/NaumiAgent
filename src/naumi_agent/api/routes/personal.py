"""Authenticated personal-agent CRUD and independent conversation admission."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from naumi_agent.api.deps import AuthDep
from naumi_agent.personal.models import PersonalAgentError

_ERROR_STATUS = {
    "not_found": 404,
    "member_not_found": 422,
    "version_conflict": 409,
    "idempotency_conflict": 409,
    "binding_conflict": 409,
    "session_conflict": 409,
    "agent_inactive": 409,
    "session_scope_mismatch": 409,
    "team_execution_pending": 409,
    "storage_error": 503,
    "configuration_error": 503,
    "model_unavailable": 503,
}


class PersonalRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": "请求参数不正确，请检查字段类型、长度和必填项",
                        "code": "invalid_input",
                    },
                )
            except PersonalAgentError as exc:
                return JSONResponse(
                    status_code=_ERROR_STATUS.get(exc.code, 422),
                    content={
                        "detail": exc.message,
                        "code": exc.code,
                    },
                )

        return handler


router = APIRouter(tags=["personal-agents"], route_class=PersonalRoute)


class AgentCreate(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)
    request_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    instructions: str = Field(min_length=1, max_length=12000)
    model_tier: Literal["fast", "capable", "reasoning"] = "capable"
    tool_names: list[str] = Field(default_factory=list, max_length=128)
    kind: Literal["specialist", "team"] = "specialist"
    member_ids: list[str] = Field(default_factory=list, max_length=128)


class AgentUpdate(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(default=None, min_length=1, max_length=12000)
    model_tier: Literal["fast", "capable", "reasoning"] | None = None
    tool_names: list[str] | None = Field(default=None, max_length=128)
    member_ids: list[str] | None = Field(default=None, max_length=128)
    status: Literal["active", "paused", "archived"] | None = None


class OpenSessionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


def _service(request: Request):
    service = getattr(request.app.state.engine, "personal_agents", None)
    if service is None:
        raise PersonalAgentError("configuration_error", "个人助理服务尚未启动，请重启应用")
    return service


@router.get("/personal/agents")
def list_agents(request: Request, include_archived: bool = False, auth: str = AuthDep):
    return {
        "agents": [a.to_dict() for a in _service(request).list(include_archived=include_archived)]
    }


@router.get("/personal/tools")
def list_tools(request: Request, auth: str = AuthDep):
    return {"tools": _service(request).assignable_tools()}


@router.get("/personal/current")
async def current_agent(
    request: Request,
    session_id: str = Query(min_length=1, max_length=128),
    auth: str = AuthDep,
):
    service = _service(request)
    agent = service.current(session_id)
    session = await service.engine.session_store.load(session_id)
    marker = getattr(session, "personal_agent_id", "")
    if marker and (agent is None or marker != agent.id):
        raise PersonalAgentError("binding_conflict", "专员身份资料暂不可用，请恢复后继续")
    return {"agent": agent.to_dict() if agent is not None else None}


@router.post("/personal/agents", status_code=201)
def create_agent(body: AgentCreate, request: Request, auth: str = AuthDep):
    return _service(request).create(**body.model_dump()).to_dict()


@router.get("/personal/agents/{agent_id}")
def get_agent(agent_id: str, request: Request, auth: str = AuthDep):
    return _service(request).get(agent_id).to_dict()


@router.patch("/personal/agents/{agent_id}")
def update_agent(agent_id: str, body: AgentUpdate, request: Request, auth: str = AuthDep):
    fields = body.model_dump(exclude_unset=True)
    expected_version = fields.pop("expected_version")
    return _service(request).update(agent_id, expected_version, **fields).to_dict()


@router.get("/personal/agents/{agent_id}/history")
def agent_history(agent_id: str, request: Request, auth: str = AuthDep):
    return {"revisions": [a.to_dict() for a in _service(request).history(agent_id)]}


@router.post("/personal/agents/{agent_id}/session")
async def open_agent_session(
    agent_id: str,
    request: Request,
    body: OpenSessionRequest | None = None,
    auth: str = AuthDep,
):
    return await _service(request).open_session(agent_id)
