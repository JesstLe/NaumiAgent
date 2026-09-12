from naumi_agent.orchestrator.engine import AgentEngine


class AuthenticationError(Exception):
    pass


def test_authentication_error_recommends_provider_scoped_reconfiguration() -> None:
    message = AgentEngine._format_error(AuthenticationError("private-provider-detail"))

    assert "模型服务拒绝" in message
    assert "provider" in message
    assert "naumi configure" in message
    assert "export " not in message
    assert "private-provider-detail" not in message
