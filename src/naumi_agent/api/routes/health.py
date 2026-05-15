"""健康检查路由."""

from fastapi import APIRouter, Request

from naumi_agent.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request):
    return HealthResponse(
        status="healthy",
        version="0.1.0",
        uptime_seconds=0.0,
        active_sessions=0,
    )
