"""Strict authenticated human followup management, never implicit model decisions."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from naumi_agent.api.routes.routines import _authorize
from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.followup_tools import (
    MAX_REQUEST_BYTES,
    execute_followup_operation,
    public_code,
    public_error,
    serializable_result,
    validate_followup_arguments,
)
from naumi_agent.personal.models import PersonalAgentError

_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _error(code, status):
    code = public_code(code)
    return JSONResponse(
        {"code": code, "error": code, "detail": public_error(code)},
        status_code=status,
        headers=_HEADERS,
    )


class FollowupRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            denied = _authorize(request)
            if denied is not None:
                return denied
            try:
                if request.query_params:
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
                    length, chunks = 0, []
                    async for chunk in request.stream():
                        length += len(chunk)
                        if length > MAX_REQUEST_BYTES:
                            return _error("invalid_input", 413)
                        chunks.append(chunk)
                    request._body = b"".join(chunks)
                    request._json = strict_json_loads(request._body.decode("utf-8"))
                response = await original(request)
            except (RequestValidationError, UnicodeError, RecursionError):
                return _error("invalid_input", 422)
            except PersonalAgentError as exc:
                code = public_code(exc.code)
                status = {
                    "scope_denied": 403,
                    "not_found": 404,
                    "source_unavailable": 503,
                    "source_stale": 409,
                    "version_conflict": 409,
                    "idempotency_conflict": 409,
                    "storage_error": 503,
                    "configuration_error": 503,
                    "followup_busy": 409,
                    "followup_unknown": 503,
                    "input_limit": 413,
                    "clock_rollback": 409,
                    "timezone_data_changed": 409,
                    "calendar_stale": 409,
                    "agent_inactive": 409,
                    "rule_inactive": 409,
                    "proposal_resolved": 409,
                    "proposal_expired": 409,
                    "decision_conflict": 409,
                    "effect_blocked": 409,
                }.get(code, 422)
                return _error(code, status)
            except Exception:
                return _error("followup_unknown", 503)
            response.headers.update(_HEADERS)
            return response

        return handler


router = APIRouter(tags=["personal-followups"], route_class=FollowupRoute)


async def _execute(request, arguments):
    arguments = validate_followup_arguments(arguments, human=True)
    service = getattr(getattr(request.app.state, "engine", None), "personal_followups", None)
    if service is None:
        raise PersonalAgentError("configuration_error", public_error("configuration_error"))
    result = await execute_followup_operation(service, arguments, trusted_channel="web")
    return JSONResponse(serializable_result(result, arguments), headers=_HEADERS)


@router.get("/personal/followups/context")
async def context(request: Request):
    return await _execute(request, {"operation": "context"})


@router.post("/personal/followups/operations")
async def operations(request: Request):
    return await _execute(request, request._json)
