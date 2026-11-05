"""Authenticated bounded research operations over the original domain service."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from naumi_agent.api.routes.routines import _authorize
from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.research_tools import (
    MAX_REQUEST_BYTES,
    execute_research_operation,
    public_code,
    public_error,
    serializable_result,
    validate_research_arguments,
)

_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_QUERY = {
    "session_context": frozenset({"actor_id"}),
    "list_egress": frozenset({"limit", "after", "notebook_id"}),
    "list_notebooks": frozenset({"limit", "after"}),
    "get_notebook": frozenset({"revision"}),
    "get_source": frozenset({"revision"}),
    "history": frozenset({"limit", "after_revision"}),
}
_STATUS = {
    "scope_denied": 403,
    "not_found": 404,
    "version_conflict": 409,
    "idempotency_conflict": 409,
    "request_pending": 409,
    "source_unavailable": 503,
    "source_stale": 409,
    "source_fetch_failed": 503,
    "artifact_network_denied": 403,
    "artifact_access_revoked": 409,
    "binding_conflict": 409,
    "execution_session_readonly": 409,
    "agent_inactive": 409,
    "research_busy": 409,
    "storage_error": 503,
    "configuration_error": 503,
    "input_limit": 413,
    "research_unknown": 503,
    "approval_required": 409,
    "approval_invalidated": 409,
    "action_expired": 409,
    "action_locked": 409,
    "lease_lost": 409,
}


def _error(code, status=None):
    code = public_code(code)
    return JSONResponse(
        {"code": code, "error": code, "detail": public_error(code)},
        status_code=status or _STATUS.get(code, 422),
        headers=_HEADERS,
    )


class ResearchRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        allowed_query = _QUERY.get(self.endpoint.__name__, frozenset())

        async def handler(request):
            denied = _authorize(request)
            if denied is not None:
                return denied
            try:
                pairs = list(request.query_params.multi_items())
                if len({key for key, _ in pairs}) != len(pairs) or any(
                    key not in allowed_query for key, _ in pairs
                ):
                    return _error("invalid_input", 422)
                if request.method == "POST":
                    media = request.headers.getlist("content-type")
                    if (
                        "content-encoding" in request.headers
                        or len(media) != 1
                        or media[0].strip().lower()
                        not in {
                            "application/json",
                            "application/json; charset=utf-8",
                            "application/json;charset=utf-8",
                        }
                    ):
                        return _error("invalid_input", 415)
                    size, chunks = 0, []
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_REQUEST_BYTES:
                            return _error("input_limit", 413)
                        chunks.append(chunk)
                    request._body = b"".join(chunks)
                    request._json = strict_json_loads(request._body.decode("utf-8"))
                response = await original(request)
            except PersonalAgentError as exc:
                return _error(exc.code)
            except (RequestValidationError, ValueError, TypeError, UnicodeError, RecursionError):
                return _error("invalid_input", 422)
            except Exception:
                return _error("research_unknown", 503)
            response.headers.update(_HEADERS)
            return response

        return handler


router = APIRouter(tags=["personal-research"], route_class=ResearchRoute)


def _query(request):
    result = dict(request.query_params)
    for name in ("limit", "revision", "after_revision"):
        if name in result:
            value = result[name]
            if not value.isascii() or not value.isdecimal() or len(value) > 16:
                raise PersonalAgentError("invalid_input", public_error("invalid_input"))
            result[name] = int(value)
    return result


async def _execute(request, arguments):
    arguments = validate_research_arguments(arguments, human=True)
    service = getattr(getattr(request.app.state, "engine", None), "personal_research", None)
    if service is None:
        raise PersonalAgentError("configuration_error", public_error("configuration_error"))
    result = await execute_research_operation(service, arguments, trusted_channel="web")
    return JSONResponse(serializable_result(result), headers=_HEADERS)


@router.get("/personal/research/context")
async def context(request: Request):
    return await _execute(request, {"operation": "context"})


@router.get("/personal/research/egress")
async def list_egress(request: Request):
    return await _execute(request, {"operation": "egress_list", **_query(request)})


@router.get("/personal/research/session-context/{session_id}")
async def session_context(session_id: str, request: Request):
    return await _execute(
        request, {"operation": "context_preview", "session_id": session_id, **_query(request)}
    )


@router.get("/personal/research/context-requests/{request_id}")
async def context_request(request_id: str, request: Request):
    return await _execute(request, {"operation": "context_request", "request_id": request_id})


@router.get("/personal/research/egress/requests/{request_id}")
async def original_egress_request(request_id: str, request: Request):
    return await _execute(request, {"operation": "egress_request", "request_id": request_id})


@router.get("/personal/research/egress/{id}")
async def get_egress(id: str, request: Request):
    return await _execute(request, {"operation": "egress_get", "id": id})


@router.get("/personal/research")
async def list_notebooks(request: Request):
    return await _execute(request, {"operation": "list", **_query(request)})


@router.get("/personal/research/sources/{id}")
async def get_source(id: str, request: Request):
    return await _execute(request, {"operation": "source", "id": id, **_query(request)})


@router.get("/personal/research/requests/{request_id}")
async def original_request(request_id: str, request: Request):
    return await _execute(request, {"operation": "request", "request_id": request_id})


@router.post("/personal/research/operations")
async def operations(request: Request):
    return await _execute(request, request._json)


@router.post("/personal/research/operations/{operation}")
async def operation(operation: str, request: Request):
    body = request._json
    if type(body) is not dict or "operation" in body:
        raise PersonalAgentError("invalid_input", public_error("invalid_input"))
    return await _execute(request, {"operation": operation, **body})


@router.get("/personal/research/{id}/history")
async def history(id: str, request: Request):
    return await _execute(request, {"operation": "history", "id": id, **_query(request)})


@router.get("/personal/research/{id}")
async def get_notebook(id: str, request: Request):
    return await _execute(request, {"operation": "get", "id": id, **_query(request)})
