"""Authenticated human management for one-shot items and explicit result receipts."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from naumi_agent.api.deps import AuthDep
from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError


class ItemRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            if request.method in {"POST", "PATCH"}:
                chunks, total = [], 0
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > 1024 * 1024:
                        raise PersonalAgentError("invalid_input", "事项请求超过 1 MB 上限")
                    chunks.append(chunk)
                request._body = b"".join(chunks)
                request._json = strict_json_loads(request._body)
            response = await original(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return response

        return handler


router = APIRouter(tags=["personal-items"], route_class=ItemRoute)
ItemId = Annotated[str, Path(pattern=r"^pi_[a-f0-9]{32}$")]
OccurrenceId = Annotated[str, Path(pattern=r"^pio_[a-f0-9]{32}$")]
RequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]


class StrictBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ItemSchedule(StrictBody):
    mode: Literal["now", "once"]
    timezone: str = Field(min_length=1, max_length=128)
    local_time: str | None = Field(default=None, min_length=1, max_length=64)
    fold: Annotated[int, Field(strict=True, ge=0, le=1)] | None = None
    misfire_policy: Literal["run_once", "skip"] = "run_once"
    grace_seconds: int = Field(default=86400, ge=0, le=604800)


class ItemArtifactRef(StrictBody):
    artifact_id: str = Field(pattern=r"^art_[a-f0-9]{32}$")
    version: int = Field(ge=1, le=10000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    grant_id: str = Field(pattern=r"^(|asg_[a-f0-9]{32})$")
    grant_revision: int = Field(ge=0, le=2**31 - 1)


class ItemDirectory(StrictBody):
    path: str = Field(min_length=1, max_length=4096)
    pattern: str = Field(min_length=1, max_length=256)
    max_files: int = Field(ge=1, le=100)
    max_bytes: int = Field(ge=1, le=50 * 1024**2)


class ItemPolicy(StrictBody):
    tool_names: list[
        Literal["personal_artifacts", "code_execute", "bash_run", "file_read", "read"]
    ] = Field(default_factory=list, max_length=5)
    artifact_operations: list[Literal["get", "read", "publish"]] = Field(
        default_factory=list, max_length=3
    )
    model_tier: Literal["fast", "capable", "reasoning"] = "capable"
    max_turns: int = Field(default=12, ge=1, le=100)
    max_cost_microusd: int = Field(default=2000000, ge=0, le=100000000)
    timeout_seconds: int = Field(default=180, ge=1, le=3600)


class ItemCreate(StrictBody):
    request_id: RequestId
    title: str = Field(min_length=1, max_length=120)
    instructions: str = Field(min_length=1, max_length=12000)
    tags: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=16
    )
    assignee_id: str = Field(pattern=r"^(main|pa_[a-f0-9]{32})$")
    assignee_version: int = Field(ge=0, le=2**31 - 1)
    schedule: ItemSchedule
    input_refs: list[ItemArtifactRef] = Field(default_factory=list, max_length=64)
    directories: list[ItemDirectory] = Field(default_factory=list, max_length=8)
    policy: ItemPolicy | None = None
    origin_session_id: str | None = Field(default=None, min_length=1, max_length=128)


class ItemUpdate(ItemCreate):
    expected_revision: int = Field(ge=1, le=2**31 - 1)


class ItemControl(StrictBody):
    expected_revision: int = Field(ge=1, le=2**31 - 1)
    request_id: RequestId


class ItemRestart(ItemControl):
    schedule: ItemSchedule | None = None


class ItemRead(StrictBody):
    request_id: RequestId
    delivery_id: str = Field(min_length=1, max_length=512)
    expected_delivery_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def _service(request: Request):
    return request.app.state.engine.personal_items


@router.get("/personal/items")
async def list_items(
    request: Request,
    state: Literal["active", "paused", "cancelled", "closed"] | None = None,
    query: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=2**31 - 1),
    auth: str = AuthDep,
):
    return await _service(request).list(state=state, query=query, limit=limit, offset=offset)


@router.post("/personal/items", status_code=201)
async def create_item(body: ItemCreate, request: Request, auth: str = AuthDep):
    return await _service(request).create(body.model_dump(exclude_unset=True))


@router.get("/personal/items/{item_id}")
async def get_item(item_id: ItemId, request: Request, auth: str = AuthDep):
    return await _service(request).get(item_id)


@router.patch("/personal/items/{item_id}")
async def update_item(item_id: ItemId, body: ItemUpdate, request: Request, auth: str = AuthDep):
    return await _service(request).update(item_id, body.model_dump(exclude_unset=True))


@router.get("/personal/items/{item_id}/history")
async def item_history(item_id: ItemId, request: Request, auth: str = AuthDep):
    return await _service(request).history(item_id)


@router.get("/personal/items/{item_id}/occurrences")
async def item_occurrences(item_id: ItemId, request: Request, auth: str = AuthDep):
    return await _service(request).occurrences(item_id)


@router.post("/personal/items/{item_id}/restart")
async def restart_item(item_id: ItemId, body: ItemRestart, request: Request, auth: str = AuthDep):
    return await _service(request).control(item_id, "restart", body.model_dump(exclude_unset=True))


@router.post("/personal/items/{item_id}/{action}")
async def control_item(
    item_id: ItemId,
    action: Literal["pause", "resume", "cancel", "run-now", "close"],
    body: ItemControl,
    request: Request,
    auth: str = AuthDep,
):
    return await _service(request).control(item_id, action, body.model_dump(exclude_unset=True))


@router.get("/personal/item-results")
async def list_results(
    request: Request,
    unread: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=2**31 - 1),
    auth: str = AuthDep,
):
    return await _service(request).results(unread=unread, limit=limit, offset=offset)


@router.get("/personal/item-results/{occurrence_id}")
async def get_result(occurrence_id: OccurrenceId, request: Request, auth: str = AuthDep):
    return await _service(request).result(occurrence_id)


@router.post("/personal/item-results/{occurrence_id}/read")
async def read_result(
    occurrence_id: OccurrenceId,
    body: ItemRead,
    request: Request,
    auth: str = AuthDep,
):
    return await _service(request).read_result(
        occurrence_id,
        body.model_dump(exclude_unset=True),
        trusted_channel="web",
    )
