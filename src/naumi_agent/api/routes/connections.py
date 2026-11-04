"""Header-authenticated human calendar management with bounded, non-cacheable requests."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.connection_tools import (
    MAX_REQUEST_BYTES,
    ActionApproval,
    ActionProposal,
    ActionRevision,
    ClientPayload,
    DisconnectPayload,
    EventItemPayload,
    GrantPayload,
    MutationPayload,
    OAuthPayload,
    RequestPayload,
    RevokePayload,
)
from naumi_agent.personal.models import PersonalAgentError

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
        or not all(type(key) is str and key for key in keys)
    ):
        return _error("configuration_error", "请先配置运行时访问密钥", 503)
    if "api_key" in request.query_params:
        return _error("authentication_required", "请使用请求头验证运行时身份", 401)
    api_headers = request.headers.getlist("x-api-key")
    auth_headers = request.headers.getlist("authorization")
    if len(api_headers) > 1 or len(auth_headers) > 1:
        return _error("authentication_required", "运行时身份验证未通过", 401)
    value = api_headers[0] if api_headers else ""
    if not value and auth_headers:
        parts = auth_headers[0].split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            value = parts[1]
    if not value or not any(secrets.compare_digest(value.encode(), key.encode()) for key in keys):
        return _error("authentication_required", "运行时身份验证未通过", 401)
    return None


class ConnectionRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            auth_error = _authorize(request)
            if auth_error is not None:
                return auth_error
            try:
                allowed = {"limit", "offset"} if self.path.endswith("/events") else set()
                query = request.query_params
                if set(query) - allowed or any(len(query.getlist(key)) != 1 for key in query):
                    return _error("invalid_input", "连接查询字段不正确", 422)
                if any(
                    not value.isascii() or not value.isdecimal() or len(value) > 10
                    for value in query.values()
                ):
                    return _error("invalid_input", "连接分页需要有效整数", 422)
                if request.method in {"POST", "PATCH"}:
                    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if media != "application/json":
                        return _error("invalid_input", "连接请求需要 JSON 正文", 415)
                    chunks, length = [], 0
                    async for chunk in request.stream():
                        length += len(chunk)
                        if length > MAX_REQUEST_BYTES:
                            return _error("invalid_input", "连接请求超过 128 KB 上限", 413)
                        chunks.append(chunk)
                    request._body = b"".join(chunks)
                    request._json = strict_json_loads(request._body)
                response = await original(request)
            except RequestValidationError:
                return _error("invalid_input", "连接请求字段不完整或格式不正确", 422)
            except PersonalAgentError as exc:
                status = {
                    "not_found": 404,
                    "storage_error": 503,
                    "configuration_error": 503,
                    "version_conflict": 409,
                    "idempotency_conflict": 409,
                    "connection_busy": 409,
                    "connection_unknown": 409,
                    "scope_denied": 403,
                    "connection_revoked": 409,
                }.get(exc.code, 422)
                return _error(exc.code, exc.message, status)
            except Exception:
                return _error("connection_unknown", "日程操作结果待核对，请查询原请求", 503)
            response.headers.update(_HEADERS)
            return response

        return handler


router = APIRouter(tags=["personal-connections"], route_class=ConnectionRoute)
ConnectionId = Annotated[str, Path(pattern=r"^pc_[a-f0-9]{32}$")]
OAuthId = Annotated[str, Path(pattern=r"^co_[a-f0-9]{32}$")]
ActionId = Annotated[str, Path(pattern=r"^ca_[a-f0-9]{32}$")]
RequestId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]
EventId = Annotated[str, Path(min_length=1, max_length=1024)]


def _service(request):
    service = getattr(getattr(request.app.state, "engine", None), "personal_connections", None)
    if service is None:
        raise PersonalAgentError("configuration_error", "账户连接服务尚未启动，请重启应用")
    return service


def _body(value):
    return value.model_dump(exclude_unset=True, exclude_none=True)


@router.get("/personal/connections/context")
async def context(request: Request):
    return await _service(request).context()


@router.get("/personal/connections")
async def connections(request: Request):
    return await _service(request).list_connections()


@router.get("/personal/connections/requests/{request_id:path}")
async def request_status(request_id: RequestId, request: Request):
    return await _service(request).request_status(request_id)


@router.post("/personal/connections/client")
async def client(body: ClientPayload, request: Request):
    return await _service(request).configure_client(_body(body), trusted_channel="manual_api")


@router.post("/personal/connections/oauth")
async def oauth(body: OAuthPayload, request: Request):
    return await _service(request).start_oauth(_body(body), trusted_channel="manual_api")


@router.get("/personal/connections/oauth/{identity}")
async def oauth_status(identity: OAuthId, request: Request):
    return await _service(request).oauth_status(identity)


@router.post("/personal/connections/oauth/{identity}/cancel")
async def cancel_oauth(identity: OAuthId, body: RequestPayload, request: Request):
    return await _service(request).cancel_oauth(identity, _body(body), trusted_channel="manual_api")


@router.post("/personal/connections/{identity}/sync")
async def sync(identity: ConnectionId, body: MutationPayload, request: Request):
    return await _service(request).sync(identity, _body(body), trusted_channel="manual_api")


@router.post("/personal/connections/{identity}/disconnect")
async def disconnect(identity: ConnectionId, body: DisconnectPayload, request: Request):
    return await _service(request).disconnect(identity, _body(body), trusted_channel="manual_api")


@router.get("/personal/connections/{identity}/events")
async def events(
    identity: ConnectionId,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=2**31 - 1),
):
    return await _service(request).events(identity, limit=limit, offset=offset)


@router.post("/personal/connections/{identity}/events/{event_id:path}/items")
async def event_to_item(
    identity: ConnectionId, event_id: EventId, body: EventItemPayload, request: Request
):
    return await _service(request).event_to_item(
        identity, event_id, _body(body), trusted_channel="manual_api"
    )


@router.post("/personal/connections/{identity}/grant")
async def grant(identity: ConnectionId, body: GrantPayload, request: Request):
    return await _service(request).grant(identity, _body(body), trusted_channel="manual_api")


@router.post("/personal/connections/{identity}/revoke-grant")
async def revoke(identity: ConnectionId, body: RevokePayload, request: Request):
    return await _service(request).grant(
        identity, _body(body), trusted_channel="manual_api", revoke=True
    )


@router.get("/personal/actions")
async def actions(request: Request):
    return await _service(request).list_actions()


@router.post("/personal/actions")
async def prepare_action(body: ActionProposal, request: Request):
    return await _service(request).prepare_action(_body(body))


@router.get("/personal/actions/{identity}")
async def action(identity: ActionId, request: Request):
    return await _service(request).get_action(identity)


@router.post("/personal/actions/{identity}/revise")
async def revise_action(identity: ActionId, body: ActionRevision, request: Request):
    return await _service(request).revise_action(identity, _body(body))


@router.post("/personal/actions/{identity}/approve")
async def approve_action(identity: ActionId, body: ActionApproval, request: Request):
    return await _service(request).approve_action(
        identity, _body(body), trusted_channel="manual_api"
    )


@router.post("/personal/actions/{identity}/reject")
async def reject_action(identity: ActionId, body: MutationPayload, request: Request):
    return await _service(request).reject_action(
        identity, _body(body), trusted_channel="manual_api"
    )


@router.post("/personal/actions/{identity}/reconcile")
async def reconcile_action(identity: ActionId, body: MutationPayload, request: Request):
    return await _service(request).reconcile_action(identity, _body(body))
