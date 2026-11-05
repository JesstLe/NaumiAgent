"""Header-authenticated, bounded human notification management."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from naumi_agent.api.routes.routines import _authorize
from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.notification_tools import (
    MAX_REQUEST_BYTES,
    ClaimPayload,
    MarkReadPayload,
    PresentPayload,
    SetPreferencesPayload,
    execute_notification_operation,
    public_code,
    public_error,
    serializable_result,
    validate_notification_arguments,
)

_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _error(code, status, arguments=None):
    code = public_code(code)
    body = {"code": code, "error": code, "detail": public_error(code)}
    request = (arguments or {}).get("payload", {}).get("request_id")
    if request:
        body["request_id"] = request
    return JSONResponse(body, status_code=status, headers=_HEADERS)


class NotificationRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            denied = _authorize(request)
            if denied is not None:
                return denied
            try:
                query = request.query_params
                allowed = (
                    {"filter", "actor_id", "limit", "after"}
                    if request.method == "GET" and self.path.endswith("/personal/notifications")
                    else set()
                )
                if set(query) - allowed or any(len(query.getlist(key)) != 1 for key in query):
                    return _error("invalid_input", 422)
                for key, value in query.items():
                    if key == "limit" and (
                        not value.isascii() or not value.isdecimal() or len(value) > 3
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
                    "source_unavailable": 403,
                    "storage_error": 503,
                    "configuration_error": 503,
                    "notification_unknown": 503,
                    "version_conflict": 409,
                    "binding_conflict": 409,
                    "idempotency_conflict": 409,
                    "immutable_change": 409,
                    "source_stale": 409,
                    "clock_rollback": 409,
                    "timezone_data_changed": 409,
                    "limit_exceeded": 409,
                    "notification_claimed": 409,
                    "notification_unavailable": 409,
                    "notification_busy": 409,
                }.get(code, 422)
                return _error(code, status, getattr(request.state, "notification_arguments", None))
            except Exception:
                return _error(
                    "notification_unknown",
                    503,
                    getattr(request.state, "notification_arguments", None),
                )
            response.headers.update(_HEADERS)
            return response

        return handler


router = APIRouter(tags=["personal-notifications"], route_class=NotificationRoute)
BatchPath = Annotated[str, Path(pattern=r"^nb_[a-f0-9]{32}$")]
RequestPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]


async def _execute(request, arguments):
    arguments = validate_notification_arguments(arguments, human=True)
    request.state.notification_arguments = arguments
    service = getattr(getattr(request.app.state, "engine", None), "personal_notifications", None)
    if service is None:
        raise PersonalAgentError("configuration_error", public_error("configuration_error"))
    result = await execute_notification_operation(service, arguments, trusted_channel="web")
    return JSONResponse(serializable_result(result, arguments), headers=_HEADERS)


@router.get("/personal/notifications/context")
async def context(request: Request):
    return await _execute(request, {"operation": "context"})


@router.get("/personal/notifications")
async def list_notifications(
    request: Request,
    filter: Literal["unread", "attention", "all"] = Query(default="unread"),
    actor_id: str | None = Query(default=None, pattern=r"^(main|pa_[a-f0-9]{32})$"),
    limit: int = Query(default=50, ge=1, le=100),
    after: str | None = Query(default=None, min_length=1, max_length=8192),
):
    value = {"operation": "list", "filter": filter, "limit": limit}
    if actor_id is not None:
        value["actor_id"] = actor_id
    if after is not None:
        value["after"] = after
    return await _execute(request, value)


@router.get("/personal/notifications/preferences")
async def preferences(request: Request):
    return await _execute(request, {"operation": "preferences"})


@router.get("/personal/notifications/batches/{batch_id}")
async def get_batch(batch_id: BatchPath, request: Request):
    return await _execute(request, {"operation": "get-batch", "batch_id": batch_id})


@router.get("/personal/notifications/requests/{request_id:path}")
async def request_status(request_id: RequestPath, request: Request):
    return await _execute(request, {"operation": "requests", "request_id": request_id})


@router.get("/personal/notifications/prompt")
async def prompt(request: Request):
    return await _execute(request, {"operation": "prompt"})


@router.post("/personal/notifications/preferences")
async def set_preferences(body: SetPreferencesPayload, request: Request):
    return await _execute(
        request, {"operation": "set_preferences", "payload": body.model_dump(exclude_unset=True)}
    )


@router.post("/personal/notifications/claim")
async def claim(body: ClaimPayload, request: Request):
    return await _execute(
        request, {"operation": "claim", "payload": body.model_dump(exclude_unset=True)}
    )


@router.post("/personal/notifications/presentation")
async def present(body: PresentPayload, request: Request):
    return await _execute(
        request, {"operation": "present", "payload": body.model_dump(exclude_unset=True)}
    )


@router.post("/personal/notifications/notice-read")
async def mark_read(body: MarkReadPayload, request: Request):
    return await _execute(
        request, {"operation": "mark_read", "payload": body.model_dump(exclude_unset=True)}
    )
