"""Authenticated human Library operations; agent identity is never a tool argument."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from naumi_agent.api.deps import AuthDep
from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.output_publish import MAX_ASSET_BYTES


class ArtifactRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                if request.method in {"POST", "PATCH", "PUT"}:
                    chunks, total = [], 0
                    async for chunk in request.stream():
                        total += len(chunk)
                        if total > MAX_ASSET_BYTES + 1024 * 1024:
                            raise PersonalAgentError(
                                "invalid_input", "资料请求超过 20 MB 文件及必要字段的上限"
                            )
                        chunks.append(chunk)
                    request._body = b"".join(chunks)
                    if "application/json" in request.headers.get("content-type", ""):
                        request._json = strict_json_loads(request._body)
                response = await original(request)
                response.headers["Cache-Control"] = "no-store"
                response.headers["Pragma"] = "no-cache"
                return response
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": "invalid_input",
                        "code": "invalid_input",
                        "detail": "资料请求参数不正确，请检查类型与必填项",
                    },
                    headers={"Cache-Control": "no-store"},
                )
            except PersonalAgentError as exc:
                status = {
                    "not_found": 404,
                    "storage_error": 503,
                    "configuration_error": 503,
                    "idempotency_conflict": 409,
                    "version_conflict": 409,
                    "artifact_access_revoked": 409,
                    "agent_inactive": 409,
                    "binding_conflict": 409,
                    "execution_session_readonly": 409,
                }.get(exc.code, 422)
                details = {"error": exc.code, "code": exc.code, "detail": exc.message}
                for field in ("accepted", "artifact_id", "version"):
                    if hasattr(exc, field):
                        details[field] = getattr(exc, field)
                return JSONResponse(
                    status_code=status, content=details, headers={"Cache-Control": "no-store"}
                )

        return handler


router = APIRouter(tags=["personal-artifacts"], route_class=ArtifactRoute)


class ArtifactCreate(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    actor_id: str = Field(pattern=r"^(main|pa_[0-9a-f]{32})$")
    request_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=120)
    kind: Literal["material", "output"]
    filename: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=MAX_ASSET_BYTES)
    source_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    evidence: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)


class ArtifactRevise(ArtifactCreate):
    expected_version: int = Field(ge=1, le=10000)


class ArtifactShare(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    actor_id: str = Field(pattern=r"^(main|pa_[0-9a-f]{32})$")
    request_id: str = Field(min_length=1, max_length=128)
    recipient_id: str = Field(pattern=r"^(main|pa_[0-9a-f]{32})$")


class ArtifactRevoke(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    actor_id: str = Field(pattern=r"^(main|pa_[0-9a-f]{32})$")
    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1, le=2)


def _service(request):
    return request.app.state.engine.personal_artifacts


@router.get("/personal/artifacts")
async def list_artifacts(
    request: Request,
    actor_id: str = Query(min_length=1, max_length=35),
    kind: Literal["material", "output"] | None = None,
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    auth: str = AuthDep,
):
    return {
        "artifacts": await _service(request).list(
            actor_id=actor_id, kind=kind, query=q, limit=limit, offset=offset
        )
    }


@router.post("/personal/artifacts", status_code=201)
async def publish_artifact(body: ArtifactCreate, request: Request, auth: str = AuthDep):
    fields = body.model_dump()
    fields["content"] = fields["content"].encode("utf-8")
    return await _service(request).publish(**fields)


async def _import(
    request,
    file,
    actor_id,
    request_id,
    title,
    kind,
    session_id,
    artifact_id=None,
    expected_version=None,
):
    try:
        data = await file.read(MAX_ASSET_BYTES + 1)
    finally:
        await file.close()
    if not file.filename or not data or len(data) > MAX_ASSET_BYTES:
        raise PersonalAgentError("invalid_input", "文件不能为空，且不能超过 20 MB")
    return await _service(request).publish(
        actor_id=actor_id,
        request_id=request_id,
        title=title or file.filename,
        kind=kind,
        filename=file.filename,
        content=data,
        session_id=session_id,
        artifact_id=artifact_id,
        expected_version=expected_version,
    )


@router.post("/personal/artifacts/import", status_code=201)
async def import_artifact(
    request: Request,
    file: UploadFile = File(...),
    actor_id: str = Query(min_length=1, max_length=35),
    request_id: str = Query(min_length=1, max_length=128),
    title: str = Query(default="", max_length=120),
    kind: Literal["material", "output"] = "material",
    session_id: str | None = Query(default=None, max_length=128),
    auth: str = AuthDep,
):
    return await _import(request, file, actor_id, request_id, title, kind, session_id)


@router.get("/personal/artifacts/{artifact_id}")
async def get_artifact(
    artifact_id: str,
    request: Request,
    actor_id: str,
    version: int | None = Query(default=None, ge=1),
    auth: str = AuthDep,
):
    return await _service(request).get(actor_id=actor_id, artifact_id=artifact_id, version=version)


@router.get("/personal/artifacts/{artifact_id}/versions")
async def versions(artifact_id: str, request: Request, actor_id: str, auth: str = AuthDep):
    return {
        "versions": await _service(request).versions(actor_id=actor_id, artifact_id=artifact_id)
    }


@router.post("/personal/artifacts/{artifact_id}/versions", status_code=201)
async def revise(artifact_id: str, body: ArtifactRevise, request: Request, auth: str = AuthDep):
    fields = body.model_dump()
    fields["content"] = fields["content"].encode("utf-8")
    return await _service(request).publish(artifact_id=artifact_id, **fields)


@router.post("/personal/artifacts/{artifact_id}/versions/import", status_code=201)
async def import_version(
    artifact_id: str,
    request: Request,
    file: UploadFile = File(...),
    actor_id: str = Query(min_length=1, max_length=35),
    request_id: str = Query(min_length=1, max_length=128),
    expected_version: int = Query(ge=1, le=10000),
    title: str = Query(default="", max_length=120),
    kind: Literal["material", "output"] = "material",
    session_id: str | None = Query(default=None, max_length=128),
    auth: str = AuthDep,
):
    return await _import(
        request, file, actor_id, request_id, title, kind, session_id, artifact_id, expected_version
    )


@router.get("/personal/artifacts/{artifact_id}/content")
async def content(
    artifact_id: str,
    request: Request,
    actor_id: str,
    version: int = Query(ge=1),
    auth: str = AuthDep,
):
    item, data = await _service(request).read(
        actor_id=actor_id, artifact_id=artifact_id, version=version
    )
    filename = quote(item["title"] + item["suffix"], safe="")
    return Response(
        content=data,
        media_type=item["mime_type"],
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; frame-ancestors 'self'",
            "X-Content-SHA256": item["sha256"],
            "Content-Disposition": f"inline; filename*=UTF-8''{filename}",
        },
    )


@router.get("/personal/artifacts/{artifact_id}/preview")
async def artifact_preview(
    artifact_id: str,
    request: Request,
    actor_id: str,
    version: int = Query(ge=1),
    auth: str = AuthDep,
):
    value = await _service(request).preview(
        actor_id=actor_id, artifact_id=artifact_id, version=version
    )
    return JSONResponse(content=value, headers={"Cache-Control": "no-store"})


@router.get("/personal/artifacts/{artifact_id}/versions/{version}/grants")
async def grants(
    artifact_id: str, version: int, request: Request, actor_id: str, auth: str = AuthDep
):
    return {
        "grants": await _service(request).grants(
            actor_id=actor_id, artifact_id=artifact_id, version=version
        )
    }


@router.post("/personal/artifacts/{artifact_id}/versions/{version}/grants", status_code=201)
async def share(
    artifact_id: str, version: int, body: ArtifactShare, request: Request, auth: str = AuthDep
):
    return await _service(request).share(
        artifact_id=artifact_id, version=version, **body.model_dump()
    )


@router.post("/personal/artifact-grants/{grant_id}/revoke")
async def revoke(grant_id: str, body: ArtifactRevoke, request: Request, auth: str = AuthDep):
    return await _service(request).revoke(grant_id=grant_id, **body.model_dump())
