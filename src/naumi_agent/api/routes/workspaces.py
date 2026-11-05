"""Separate human management and mutual-proof federation HTTP boundaries."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from naumi_agent.api.routes.routines import _authorize
from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.workspace_models import (
    MAX_REQUEST_BYTES,
    FederationRequest,
    error,
    normalize,
)
from naumi_agent.personal.workspace_tools import (
    execute_workspace_operation,
    serializable_result,
    validate_workspace_arguments,
)

_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_PROOF = re.compile(r"NaumiWorkspace [a-f0-9]{32}:[1-9][0-9]{0,15}:[a-f0-9]{64}")
_SHA = re.compile(r"[a-f0-9]{64}")


def _error(code, status):
    failure = error(code)
    return JSONResponse(
        {"code": failure.code, "error": failure.code, "detail": failure.message},
        status_code=status,
        headers=_HEADERS,
    )


def _federation_auth(request):
    auth, trace = (
        request.headers.getlist("authorization"),
        request.headers.getlist("x-naumi-workspace-trace"),
    )
    if (
        len(auth) != 1
        or not _PROOF.fullmatch(auth[0])
        or len(trace) > 1
        or request.query_params
        or "cookie" in request.headers
        or "origin" in request.headers
        or "x-api-key" in request.headers
    ):
        raise error("scope_denied")
    raw = trace[0] if trace else ""
    values = raw.split(",") if raw else []
    if (
        len(values) > 8
        or len(values) != len(set(values))
        or any(not _SHA.fullmatch(value) for value in values)
    ):
        raise error("invalid_input")
    return auth[0], values


class WorkspaceRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        federation = self.path.endswith("/personal/workspaces/federation")

        async def handler(request):
            try:
                if federation:
                    request.state.workspace_federation_auth = _federation_auth(request)
                else:
                    denied = _authorize(request)
                    if denied is not None:
                        return denied
                    if request.query_params:
                        return _error("invalid_input", 422)
                if request.method == "POST":
                    if "content-encoding" in request.headers:
                        return _error("invalid_input", 415)
                    media = request.headers.getlist("content-type")
                    if len(media) != 1 or media[0].strip().lower() not in {
                        "application/json",
                        "application/json; charset=utf-8",
                        "application/json;charset=utf-8",
                    }:
                        return _error("invalid_input", 415)
                    length, chunks = 0, []
                    async for chunk in request.stream():
                        length += len(chunk)
                        if length > MAX_REQUEST_BYTES:
                            return _error("invalid_input", 413)
                        chunks.append(chunk)
                    request._body = b"".join(chunks)
                    # Decode explicitly: json.loads(bytes) also accepts UTF-16/32.
                    request._json = strict_json_loads(request._body.decode("utf-8"))
                response = await original(request)
            except (RequestValidationError, UnicodeError, RecursionError):
                return _error("invalid_input", 422)
            except PersonalAgentError as exc:
                failure = error(exc.code)
                status = {
                    "scope_denied": 403,
                    "source_unavailable": 503,
                    "source_stale": 409,
                    "grant_revoked": 403,
                    "version_conflict": 409,
                    "not_found": 404,
                    "storage_error": 503,
                    "input_limit": 413,
                    "workspace_busy": 409,
                    "idempotency_conflict": 409,
                    "workspace_unknown": 503,
                    "configuration_error": 503,
                    "credential_error": 503,
                    "credential_missing": 503,
                    "vault_corrupt": 503,
                }.get(failure.code, 422)
                return _error(failure.code, status)
            except Exception:
                return _error("workspace_unknown", 503)
            response.headers.update(_HEADERS)
            return response

        return handler


router = APIRouter(tags=["personal-workspaces"], route_class=WorkspaceRoute)


def _service(request):
    service = getattr(getattr(request.app.state, "engine", None), "personal_workspaces", None)
    if service is None:
        raise error("configuration_error")
    return service


async def _execute(request, arguments):
    arguments = validate_workspace_arguments(arguments, human=True)
    result = await execute_workspace_operation(_service(request), arguments, trusted_channel="web")
    return JSONResponse(serializable_result(result), headers=_HEADERS)


@router.get("/personal/workspaces/context")
async def context(request: Request):
    return await _execute(request, {"operation": "context"})


@router.post("/personal/workspaces/operations")
async def operations(request: Request):
    return await _execute(request, request._json)


@router.post("/personal/workspaces/federation")
async def federation(request: Request):
    arguments = normalize(FederationRequest, request._json)
    token, trace = request.state.workspace_federation_auth
    result = await _service(request).federation(arguments, token=token, trace=trace)
    return JSONResponse(serializable_result(result), headers=_HEADERS)
