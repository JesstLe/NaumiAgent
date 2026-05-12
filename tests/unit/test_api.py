"""API 路由单元测试."""

import pytest
from fastapi.testclient import TestClient

from naumi_agent.api.schemas import HealthResponse, SessionCreate


class TestSchemas:
    def test_session_create(self) -> None:
        s = SessionCreate(title="test")
        assert s.title == "test"

    def test_session_create_defaults(self) -> None:
        s = SessionCreate()
        assert s.title is None

    def test_health_response(self) -> None:
        h = HealthResponse(status="healthy", version="0.1.0", uptime_seconds=0.0, active_sessions=0)
        assert h.status == "healthy"


class TestHealthEndpoint:
    def test_health_check(self) -> None:
        from naumi_agent.api.app import create_app
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["version"] == "0.1.0"
