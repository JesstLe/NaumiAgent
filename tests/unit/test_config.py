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
        assert config.memory.session_db_path == str(tmp_path / "data" / "sessions.db")
        assert config.memory.vector_db_path == str(tmp_path / "data" / "chroma")

    def test_from_missing_yaml(self) -> None:
        config = AppConfig.from_yaml("/nonexistent/config.yaml")
        assert config.models.default_model == "claude-sonnet-4-6"

    def test_from_empty_yaml(self, tmp_path) -> None:
        yaml_path = tmp_path / "empty.yaml"
        yaml_path.write_text("")

        config = AppConfig.from_yaml(yaml_path)
        assert config.models.default_model == "claude-sonnet-4-6"
        assert config.memory.session_db_path == str(tmp_path / "data" / "sessions.db")

    def test_from_yaml_keeps_absolute_runtime_paths(self, tmp_path) -> None:
        yaml_path = tmp_path / "config.yaml"
        session_db = tmp_path / "custom" / "sessions.db"
        vector_db = tmp_path / "custom" / "chroma"
        yaml_path.write_text(
            yaml.dump(
                {
                    "memory": {
                        "session_db_path": str(session_db),
                        "vector_db_path": str(vector_db),
                    }
                }
            )
        )

        config = AppConfig.from_yaml(yaml_path)

        assert config.memory.session_db_path == str(session_db)
        assert config.memory.vector_db_path == str(vector_db)

    def test_resolve_workspace_root(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        config = AppConfig(workspace_root="workspace")

        assert config.resolve_workspace_root() == tmp_path / "workspace"
