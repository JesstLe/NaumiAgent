"""Header-only, bounded human Today management; model reads use a separate pin barrier."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from naumi_agent.api.routes.routines import _authorize
from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.today_tools import (
    MAX_REQUEST_BYTES,
    CheckPayload,
    HumanPlacePayload,
    MovePayload,
    RebasePayload,
    RemovePayload,
    ReorderPayload,
    SetMetadataPayload,
    execute_today_operation,
    public_code,
    public_error,
    serializable_result,
    validate_today_arguments,
)

_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_QUERIES = {
    "/personal/today": {"date", "timezone", "planner_id", "limit", "cursor"},
    "/personal/today/sources": {"limit", "after"},
    "/personal/today/days/{day_id}": {"revision"},
    "/personal/today/history/{kind}/{identity}": {"limit", "after_revision"},
}


def _error(code, status, arguments=None):
    code = public_code(code)
    body = {"code": code, "error": code, "detail": public_error(code)}
    request = (arguments or {}).get("payload", {}).get("request_id")
    if request:
        body["request_id"] = request
    return JSONResponse(body, status_code=status, headers=_HEADERS)


class TodayRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            denied = _authorize(request)
            if denied is not None:
                return denied
            try:
                query = request.query_params
                allowed = (
                    next(
                        (fields for path, fields in _QUERIES.items() if self.path.endswith(path)),
                        set(),
                    )
                    if request.method == "GET"
                    else set()
                )
                if set(query) - allowed or any(len(query.getlist(key)) != 1 for key in query):
                    return _error("invalid_input", 422)
                for key, value in query.items():
                    if key in {"limit", "revision", "after_revision"} and (
                        not value.isascii() or not value.isdecimal() or len(value) > 16
                    ):
                        return _error("invalid_input", 422)
                if request.method == "POST":
                    media = request.headers.getlist("content-type")
                    if (
                        len(media) != 1
                        or media[0].split(";", 1)[0].strip().lower() != "application/json"
                    ):
                        return _error("invalid_input", 415)
                    length, chunks = 0, []
                    async for chunk in request.stream():
                        length += len(chunk)
                        if length > MAX_REQUEST_BYTES:
                            return _error("invalid_input", 413)
                        chunks.append(chunk)
                    request._body = b"".join(chunks)
                    request._json = strict_json_loads(request._body)
                response = await original(request)
            except RequestValidationError:
                return _error("invalid_input", 422)
            except (UnicodeError, RecursionError):
                return _error("invalid_input", 422)
            except PersonalAgentError as exc:
                code = public_code(exc.code)
                status = {
                    "not_found": 404,
                    "scope_denied": 403,
                    "storage_error": 503,
                    "configuration_error": 503,
                    "today_unknown": 503,
                    "version_conflict": 409,
                    "binding_conflict": 409,
                    "idempotency_conflict": 409,
                    "source_stale": 409,
                    "limit_exceeded": 409,
                    "source_unavailable": 403,
                }.get(code, 422)
                return _error(code, status, getattr(request.state, "today_arguments", None))
            except Exception:
                return _error("today_unknown", 503, getattr(request.state, "today_arguments", None))
            response.headers.update(_HEADERS)
            return response

        return handler


router = APIRouter(tags=["personal-today"], route_class=TodayRoute)
DayPath = Annotated[str, Path(pattern=r"^td_[a-f0-9]{32}$")]
ItemPath = Annotated[str, Path(pattern=r"^pi_[a-f0-9]{32}$")]
RequestPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]


async def _execute(request, arguments):
    arguments = validate_today_arguments(arguments, human=True)
    request.state.today_arguments = arguments
    service = getattr(getattr(request.app.state, "engine", None), "personal_today", None)
    if service is None:
        raise PersonalAgentError("configuration_error", public_error("configuration_error"))
    result = await execute_today_operation(service, arguments, trusted_channel="web")
    return JSONResponse(serializable_result(result, arguments), headers=_HEADERS)


@router.get("/personal/today/context")
async def context(request: Request):
    return await _execute(request, {"operation": "context"})


@router.get("/personal/today")
async def day(
    request: Request,
    date: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
    timezone: str = Query(min_length=1, max_length=128),
    planner_id: str | None = Query(default=None, pattern=r"^(main|pa_[a-f0-9]{32})$"),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=1, max_length=8192),
):
    value = {"operation": "list", "date": date, "timezone": timezone, "limit": limit}
    if planner_id is not None:
        value["planner_id"] = planner_id
    if cursor is not None:
        value["cursor"] = cursor
    return await _execute(request, value)


@router.get("/personal/today/sources")
async def sources(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    after: str = Query(default="", pattern=r"^(|pi_[a-f0-9]{32})$"),
):
    return await _execute(request, {"operation": "sources", "limit": limit, "after": after})


@router.get("/personal/today/metadata/{item_id}")
async def metadata(item_id: ItemPath, request: Request):
    return await _execute(request, {"operation": "metadata", "item_id": item_id})


@router.get("/personal/today/days/{day_id}")
async def get_day(
    day_id: DayPath,
    request: Request,
    revision: int | None = Query(default=None, ge=0, le=2**53 - 1),
):
    value = {"operation": "get", "day_id": day_id}
    if revision is not None:
        value["revision"] = revision
    return await _execute(request, value)


@router.get("/personal/today/history/{kind}/{identity}")
async def history(
    kind: Literal["day", "metadata"],
    identity: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    after_revision: int | None = Query(default=None, ge=0, le=2**53 - 1),
):
    value = {"operation": "history", "kind": kind, "identity": identity, "limit": limit}
    if after_revision is not None:
        value["after_revision"] = after_revision
    return await _execute(request, value)


@router.get("/personal/today/requests/{request_id:path}")
async def request_status(request_id: RequestPath, request: Request):
    return await _execute(request, {"operation": "requests", "request_id": request_id})


@router.post("/personal/today/set-metadata")
async def set_metadata(body: SetMetadataPayload, request: Request):
    return await _execute(
        request, {"operation": "set_metadata", "payload": body.model_dump(exclude_unset=True)}
    )


@router.post("/personal/today/place")
async def place(body: HumanPlacePayload, request: Request):
    return await _execute(
        request, {"operation": "place", "payload": body.model_dump(exclude_unset=True)}
    )


@router.post("/personal/today/reorder")
async def reorder(body: ReorderPayload, request: Request):
    return await _execute(
        request, {"operation": "reorder", "payload": body.model_dump(exclude_unset=True)}
    )


@router.post("/personal/today/remove")
async def remove(body: RemovePayload, request: Request):
    return await _execute(
        request, {"operation": "remove", "payload": body.model_dump(exclude_unset=True)}
    )


@router.post("/personal/today/rebase")
async def rebase(body: RebasePayload, request: Request):
    return await _execute(
        request, {"operation": "rebase", "payload": body.model_dump(exclude_unset=True)}
    )


@router.post("/personal/today/check")
async def check(body: CheckPayload, request: Request):
    return await _execute(
        request, {"operation": "check", "payload": body.model_dump(exclude_unset=True)}
    )


@router.post("/personal/today/move")
async def move(body: MovePayload, request: Request):
    return await _execute(
        request, {"operation": "move", "payload": body.model_dump(exclude_unset=True)}
    )
