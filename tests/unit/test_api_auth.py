"""API 认证方式对齐测试：支持 X-API-Key / Authorization Bearer / api_key 查询参数."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from naumi_agent.api.deps import extract_api_key, verify_api_key
from naumi_agent.api.middleware import AuthMiddleware


class _FakeRequest:
    """只提供 verify_api_key/extract_api_key 所需最小接口的请求替身."""

    def __init__(
        self,
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        config: SimpleNamespace | None = None,
    ) -> None:
        self.headers = headers or {}
        self.query_params = query or {}
        self.app = SimpleNamespace(state=SimpleNamespace(config=config))


def _fake_config(api_keys: list[str]) -> SimpleNamespace:
    return SimpleNamespace(api=SimpleNamespace(api_keys=api_keys))


class TestExtractAPIKey:
    def test_x_api_key_has_highest_priority(self) -> None:
        request = _FakeRequest(
            headers={"X-API-Key": "x-key", "Authorization": "Bearer bearer-key"},
            query={"api_key": "query-key"},
        )
        assert extract_api_key(request) == "x-key"

    def test_bearer_token_accepted(self) -> None:
        request = _FakeRequest(headers={"Authorization": "Bearer valid-token"})
        assert extract_api_key(request) == "valid-token"

    def test_bearer_scheme_is_case_insensitive(self) -> None:
        request = _FakeRequest(headers={"Authorization": "bearer lower-token"})
        assert extract_api_key(request) == "lower-token"

    def test_query_api_key_fallback(self) -> None:
        request = _FakeRequest(query={"api_key": "query-token"})
        assert extract_api_key(request) == "query-token"

    def test_malformed_authorization_is_ignored(self) -> None:
        request = _FakeRequest(
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
            query={"api_key": "query-token"},
        )
        assert extract_api_key(request) == "query-token"

    def test_empty_bearer_token_is_ignored(self) -> None:
        request = _FakeRequest(headers={"Authorization": "Bearer   "})
        assert extract_api_key(request) is None

    def test_missing_credentials_returns_none(self) -> None:
        request = _FakeRequest()
        assert extract_api_key(request) is None


class TestVerifyAPIKey:
    async def test_accepts_bearer_token_when_configured(self) -> None:
        request = _FakeRequest(
            headers={"Authorization": "Bearer valid-key"},
            config=_fake_config(["valid-key"]),
        )
        assert await verify_api_key(request) == "valid-key"

    async def test_accepts_x_api_key(self) -> None:
        request = _FakeRequest(
            headers={"X-API-Key": "valid-key"},
            config=_fake_config(["valid-key"]),
        )
        assert await verify_api_key(request) == "valid-key"

    async def test_accepts_query_api_key(self) -> None:
        request = _FakeRequest(
            query={"api_key": "valid-key"},
            config=_fake_config(["valid-key"]),
        )
        assert await verify_api_key(request) == "valid-key"

    async def test_invalid_token_raises_401(self) -> None:
        request = _FakeRequest(
            headers={"Authorization": "Bearer wrong-key"},
            config=_fake_config(["valid-key"]),
        )
        with pytest.raises(Exception) as exc_info:
            await verify_api_key(request)
        assert exc_info.value.status_code == 401

    async def test_missing_token_raises_401(self) -> None:
        request = _FakeRequest(config=_fake_config(["valid-key"]))
        with pytest.raises(Exception) as exc_info:
            await verify_api_key(request)
        assert exc_info.value.status_code == 401

    async def test_no_api_keys_configured_returns_anonymous(self) -> None:
        request = _FakeRequest(config=_fake_config([]))
        assert await verify_api_key(request) == "anonymous"

    async def test_no_config_returns_anonymous(self) -> None:
        request = _FakeRequest()
        assert await verify_api_key(request) == "anonymous"


class TestAuthMiddleware:
    @pytest.fixture
    def client(self) -> TestClient:
        app = FastAPI()
        app.state.config = _fake_config(["valid-key"])
        app.add_middleware(AuthMiddleware)

        @app.get("/api/v1/protected")
        def protected():
            return {"ok": True}

        @app.get("/health")
        def health():
            return {"status": "up"}

        @app.get("/api/v1/health")
        def api_health():
            return {"status": "up"}

        return TestClient(app)

    def test_accepts_bearer_for_protected_route(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/protected",
            headers={"Authorization": "Bearer valid-key"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_accepts_x_api_key_for_protected_route(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/protected",
            headers={"X-API-Key": "valid-key"},
        )
        assert response.status_code == 200

    def test_rejects_missing_token(self, client: TestClient) -> None:
        response = client.get("/api/v1/protected")
        assert response.status_code == 401
        assert response.json()["error"] == "Invalid or missing API key"

    def test_public_path_bypasses_auth(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200

    def test_api_v1_health_bypasses_auth(self, client: TestClient) -> None:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json() == {"status": "up"}
