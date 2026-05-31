"""配置系统测试."""

import yaml

from naumi_agent.config.settings import AppConfig


class TestAppConfig:
    def test_default_config(self) -> None:
        config = AppConfig()
        assert config.models.default_model == "claude-sonnet-4-6"
        assert config.safety.max_turns == 30
        assert config.memory.session_db_path == "data/sessions.db"
        assert config.browser_daemon.base_url == "http://127.0.0.1:3005"
        assert config.browser_daemon.project_dir.endswith("browser-debugging-daemon")

    def test_from_yaml(self, tmp_path) -> None:
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            yaml.dump(
                {
                    "models": {
                        "default_model": "gpt-4o",
                        "fast_model": "gpt-4o-mini",
                    },
                    "safety": {
                        "max_turns": 50,
                    },
                    "browser_daemon": {
                        "base_url": "http://127.0.0.1:3999",
                    },
                }
            )
        )

        config = AppConfig.from_yaml(yaml_path)
        assert config.models.default_model == "gpt-4o"
        assert config.models.fast_model == "gpt-4o-mini"
        assert config.safety.max_turns == 50
        assert config.browser_daemon.base_url == "http://127.0.0.1:3999"

    def test_from_missing_yaml(self) -> None:
        config = AppConfig.from_yaml("/nonexistent/config.yaml")
        assert config.models.default_model == "claude-sonnet-4-6"

    def test_from_empty_yaml(self, tmp_path) -> None:
        yaml_path = tmp_path / "empty.yaml"
        yaml_path.write_text("")

        config = AppConfig.from_yaml(yaml_path)
        assert config.models.default_model == "claude-sonnet-4-6"

    def test_resolve_workspace_root(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        config = AppConfig(workspace_root="workspace")

        assert config.resolve_workspace_root() == tmp_path / "workspace"
