"""Header-authenticated, bounded human management of durable routine plans."""

from __future__ import annotations

import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.routine_tools import (
    MAX_REQUEST_BYTES,
    RoutineControlPayload,
    RoutineCreatePayload,
    RoutinePreviewPayload,
    RoutineRevisePayload,
    public_error,
)

_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _error(code, message, status):
    return JSONResponse(
        status_code=status,
        content={"code": code, "error": code, "detail": message},
        headers=_HEADERS,
    )


def _authorize(request):
    config = getattr(request.app.state, "config", None)
    keys = getattr(getattr(config, "api", None), "api_keys", None)
    if (
        not isinstance(keys, (list, tuple))
        or not keys
        or not all(type(key) is str and 0 < len(key) <= 4096 and key.strip() == key for key in keys)
    ):
        return _error("configuration_error", "请先配置有效的运行时访问密钥", 503)
    if "api_key" in request.query_params:
        return _error("authentication_required", "请使用请求头验证运行时身份", 401)
    api = request.headers.getlist("x-api-key")
    auth = request.headers.getlist("authorization")
    if len(api) > 1 or len(auth) > 1 or api and auth:
        return _error("authentication_required", "运行时身份验证未通过", 401)
    value = api[0] if api else ""
    if auth:
        parts = auth[0].split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return _error("authentication_required", "运行时身份验证未通过", 401)
        value = parts[1]
    try:
        valid = bool(value) and any(
            secrets.compare_digest(value.encode("utf-8"), key.encode("utf-8")) for key in keys
        )
    except UnicodeError:
        valid = False
    if not valid:
        return _error("authentication_required", "运行时身份验证未通过", 401)
    return None


class RoutineRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            auth_error = _authorize(request)
            if auth_error is not None:
                return auth_error
            try:
                query = request.query_params
                if self.path.endswith("/personal/routines") and request.method == "GET":
                    allowed = {"status", "query", "limit", "after"}
                elif self.path.endswith(("/history", "/occurrences")):
                    allowed = {"limit", "after"}
                else:
                    allowed = set()
                if set(query) - allowed or any(len(query.getlist(key)) != 1 for key in query):
                    return _error("invalid_input", "周期查询字段不正确", 422)
                numeric = {"limit"} | ({"after"} if self.path.endswith("/history") else set())
                if any(
                    not value.isascii() or not value.isdecimal() or len(value) > 10
                    for key, value in query.items()
                    if key in numeric
                ):
                    return _error("invalid_input", "周期分页需要有效整数", 422)
                if request.method == "POST":
                    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if media != "application/json":
                        return _error("invalid_input", "周期请求需要 JSON 正文", 415)
                    chunks, length = [], 0
                    async for chunk in request.stream():
                        length += len(chunk)
                        if length > MAX_REQUEST_BYTES:
                            return _error("invalid_input", "周期请求超过 256 KB 上限", 413)
                        chunks.append(chunk)
                    request._body = b"".join(chunks)
                    request._json = strict_json_loads(request._body)
                response = await original(request)
            except RequestValidationError:
                return _error("invalid_input", "周期请求字段不完整或格式不正确", 422)
            except (RecursionError, UnicodeError):
                return _error("invalid_input", "周期请求结构或编码不正确", 422)
            except PersonalAgentError as exc:
                status = {
                    "not_found": 404,
                    "scope_denied": 403,
                    "storage_error": 503,
                    "configuration_error": 503,
                    "routine_unknown": 503,
                    "version_conflict": 409,
                    "binding_conflict": 409,
                    "idempotency_conflict": 409,
                    "preview_expired": 409,
                    "budget_exhausted": 409,
                    "routine_closed": 409,
                }.get(exc.code, 422)
                return _error(exc.code, public_error(exc.code), status)
            except Exception:
                return _error("routine_unknown", public_error("routine_unknown"), 503)
            response.headers.update(_HEADERS)
            return response

        return handler


router = APIRouter(tags=["personal-routines"], route_class=RoutineRoute)
RoutinePath = Annotated[str, Path(pattern=r"^pr_[a-f0-9]{32}$")]
RequestPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]


def _service(request):
    service = getattr(getattr(request.app.state, "engine", None), "personal_routines", None)
    if service is None:
        raise PersonalAgentError("configuration_error", "周期服务尚未启动")
    return service


def _body(value):
    return value.model_dump(exclude_unset=True)


@router.get("/personal/routines/context")
async def context(request: Request):
    return await _service(request).context()


@router.get("/personal/routines/requests/{request_id:path}")
async def request_status(request_id: RequestPath, request: Request):
    return await _service(request).request_status(request_id, caller_actor=None)


@router.post("/personal/routines/previews")
async def preview(body: RoutinePreviewPayload, request: Request):
    return await _service(request).preview(_body(body), caller_actor=None)


@router.get("/personal/routines")
async def list_routines(
    request: Request,
    status: Literal["active", "paused", "cancelled", "closed"] | None = None,
    query: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
    after: str | None = Query(default=None, pattern=r"^pr_[a-f0-9]{32}$"),
):
    return await _service(request).list(
        status=status, query=query, limit=limit, after=after, caller_actor=None
    )


@router.post("/personal/routines", status_code=201)
async def create(body: RoutineCreatePayload, request: Request):
    return await _service(request).create(_body(body), caller_actor=None)


@router.get("/personal/routines/{identity}")
async def get_routine(identity: RoutinePath, request: Request):
    return await _service(request).get(identity, caller_actor=None)


@router.get("/personal/routines/{identity}/history")
async def history(
    identity: RoutinePath,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    after: int | None = Query(default=None, ge=0, le=2**31 - 1),
):
    return await _service(request).history(identity, limit=limit, after=after, caller_actor=None)


@router.get("/personal/routines/{identity}/occurrences")
async def occurrences(
    identity: RoutinePath,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    after: str | None = Query(default=None, pattern=r"^pro_[a-f0-9]{32}$"),
):
    return await _service(request).occurrences(
        identity, limit=limit, after=after, caller_actor=None
    )


@router.post("/personal/routines/{identity}/revise")
async def revise(identity: RoutinePath, body: RoutineRevisePayload, request: Request):
    return await _service(request).revise(identity, _body(body), caller_actor=None)


@router.post("/personal/routines/{identity}/{action}")
async def control(
    identity: RoutinePath,
    action: Literal["pause", "resume", "cancel", "extra-run"],
    body: RoutineControlPayload,
    request: Request,
):
    return await _service(request).control(identity, action, _body(body), caller_actor=None)
