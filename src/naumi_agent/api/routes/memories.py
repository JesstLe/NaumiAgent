"""Strict authenticated management for scoped memory and opaque durable requests."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.api.deps import AuthDep
from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.memory_tools import validate_operation_payload
from naumi_agent.personal.models import PersonalAgentError


class MemoryRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            if request.method == "POST":
                chunks, length = [], 0
                async for chunk in request.stream():
                    length += len(chunk)
                    if length > 1024 * 1024:
                        raise PersonalAgentError("invalid_input", "记忆请求超过 1 MB 上限")
                    chunks.append(chunk)
                request._body = b"".join(chunks)
                request._json = strict_json_loads(request._body)
            try:
                response = await original(request)
            except RequestValidationError as exc:
                raise PersonalAgentError("invalid_input", "记忆请求字段不完整或格式不正确") from exc
            except HTTPException as exc:
                exc.headers = {
                    **(exc.headers or {}),
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                }
                raise
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return response

        return handler


router = APIRouter(tags=["personal-memory"], route_class=MemoryRoute)
Actor = Annotated[str, Query(pattern=r"^(main|pa_[a-f0-9]{32})$")]
MemoryId = Annotated[str, Path(pattern=r"^pm_[a-f0-9]{32}$")]
RequestId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]
Operation = Literal[
    "create",
    "propose",
    "confirm",
    "revise",
    "delete",
    "reject",
    "share",
    "revoke",
    "import_legacy",
]
Scope = Literal["all", "personal", "workspace", "item"]
State = Literal["proposed", "active", "superseded", "rejected", "deleted", "conflict"]


class StrictBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class PrepareMemoryRequest(StrictBody):
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
    actor_id: str = Field(pattern=r"^(main|pa_[a-f0-9]{32})$")
    operation: Operation
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_payload(self):
        validate_operation_payload(self.operation, self.payload, manual=True)
        if self.payload.get("request_id") != self.request_id:
            raise PersonalAgentError("invalid_input", "内外请求编号需要保持一致")
        return self


class ApplyMemoryRequest(StrictBody):
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


def _service(request):
    return request.app.state.engine.personal_memory


@router.get("/personal/memories/context")
async def memory_context(request: Request, actor_id: Actor = "main", auth: str = AuthDep):
    return await _service(request).context(actor_id=actor_id)


@router.get("/personal/memories")
async def list_memories(
    request: Request,
    actor_id: Actor = "main",
    scope: Scope = "all",
    item_id: str | None = Query(default=None, pattern=r"^pi_[a-f0-9]{32}$"),
    state: State | None = None,
    query: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=2**31 - 1),
    auth: str = AuthDep,
):
    return await _service(request).list(
        actor_id=actor_id,
        scope=scope,
        item_id=item_id,
        state=state,
        query=query,
        limit=limit,
        offset=offset,
    )


@router.get("/personal/memories/search")
async def search_memories(
    request: Request,
    query: str = Query(min_length=1, max_length=2000),
    actor_id: Actor = "main",
    scope: Scope = "all",
    item_id: str | None = Query(default=None, pattern=r"^pi_[a-f0-9]{32}$"),
    category: Literal["preference", "fact", "decision", "experience", "plan_template"]
    | None = None,
    limit: int = Query(default=10, ge=1, le=20),
    auth: str = AuthDep,
):
    return await _service(request).search(
        query, actor_id=actor_id, scope=scope, item_id=item_id, category=category, limit=limit
    )


@router.get("/personal/memories/export")
async def export_memories(
    request: Request,
    actor_id: Actor = "main",
    scope: Scope = "all",
    item_id: str | None = Query(default=None, pattern=r"^pi_[a-f0-9]{32}$"),
    auth: str = AuthDep,
):
    return await _service(request).export(actor_id=actor_id, scope=scope, item_id=item_id)


@router.get("/personal/memories/legacy")
async def legacy_memories(
    request: Request,
    actor_id: Actor = "main",
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=2**31 - 1),
    auth: str = AuthDep,
):
    return await _service(request).legacy(actor_id=actor_id, limit=limit, offset=offset)


@router.get("/personal/memories/{memory_id}")
async def get_memory(
    memory_id: MemoryId, request: Request, actor_id: Actor = "main", auth: str = AuthDep
):
    return await _service(request).get(memory_id, actor_id=actor_id)


@router.get("/personal/memories/{memory_id}/history")
async def memory_history(
    memory_id: MemoryId, request: Request, actor_id: Actor = "main", auth: str = AuthDep
):
    return await _service(request).history(memory_id, actor_id=actor_id)


@router.get("/personal/memories/{memory_id}/grants")
async def memory_grants(
    memory_id: MemoryId, request: Request, actor_id: Actor = "main", auth: str = AuthDep
):
    return await _service(request).grants(memory_id, actor_id=actor_id)


@router.post("/personal/memory-requests", status_code=201)
async def prepare_memory_request(body: PrepareMemoryRequest, request: Request, auth: str = AuthDep):
    return await _service(request).prepare_request(
        body.model_dump(), trusted_channel="management_web"
    )


@router.get("/personal/memory-requests/{request_id}")
async def memory_request(
    request_id: RequestId, request: Request, actor_id: Actor = "main", auth: str = AuthDep
):
    return await _service(request).request(request_id, actor_id=actor_id)


@router.post("/personal/memory-requests/{request_id}/apply")
async def apply_memory_request(
    request_id: RequestId,
    body: ApplyMemoryRequest,
    request: Request,
    actor_id: Actor = "main",
    auth: str = AuthDep,
):
    return await _service(request).apply_request(
        request_id,
        actor_id=actor_id,
        expected_fingerprint=body.expected_fingerprint,
        trusted_channel="management_web",
    )


@router.post("/personal/memory-requests/{request_id}/discard")
async def discard_memory_request(
    request_id: RequestId,
    body: ApplyMemoryRequest,
    request: Request,
    actor_id: Actor = "main",
    auth: str = AuthDep,
):
    return await _service(request).discard_request(
        request_id,
        actor_id=actor_id,
        expected_fingerprint=body.expected_fingerprint,
        trusted_channel="management_web",
    )


@router.post("/personal/memories/{operation}")
async def mutate_memory(
    operation: Literal[
        "create",
        "propose",
        "confirm",
        "revise",
        "delete",
        "reject",
        "share",
        "revoke",
        "import_legacy",
        "index_sync",
    ],
    request: Request,
    payload: dict[str, Any] = Body(),
    actor_id: Actor = "main",
    auth: str = AuthDep,
):
    validate_operation_payload(operation, payload, manual=True)
    return await _service(request).dispatch(
        operation, payload, actor_id=actor_id, trusted_channel="management_web"
    )
