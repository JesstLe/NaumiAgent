"""配置系统测试."""

from pathlib import Path

import pytest
import yaml

from naumi_agent.config.settings import AppConfig


class TestAppConfig:
    def test_default_config(self) -> None:
        config = AppConfig()
        assert config.models.default_model == "claude-sonnet-4-6"
        assert config.safety.max_turns == 50
        assert config.safety.max_parallel_tools == 4
        assert config.memory.session_db_path == "data/sessions.db"
        assert config.ui.theme == "dark"
        assert config.ui.output_style == "detailed"
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
                    "ui": {
                        "theme": "high_contrast",
                        "output_style": "debug",
                    },
                }
            )
        )

        config = AppConfig.from_yaml(yaml_path)
        assert config.models.default_model == "gpt-4o"
        assert config.models.fast_model == "gpt-4o-mini"
        assert config.safety.max_turns == 50
        assert config.browser_daemon.base_url == "http://127.0.0.1:3999"
        assert config.ui.theme == "high_contrast"
        assert config.ui.output_style == "debug"
        assert config.memory.session_db_path == str(tmp_path / "data" / "sessions.db")
        assert config.memory.vector_db_path == str(tmp_path / "data" / "chroma")

    def test_from_yaml_loads_model_key_from_system_credential_store(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            "models:\n  provider: openai\n  default_model: test-model\n",
            encoding="utf-8",
        )
        requested_providers: list[str | None] = []
        monkeypatch.delenv("NAUMI_MODELS__API_KEY", raising=False)
        monkeypatch.setitem(AppConfig.model_config, "env_file", None)
        monkeypatch.setattr(
            "naumi_agent.config.settings.load_model_api_key",
            lambda *, provider=None: requested_providers.append(provider) or "credential-key",
            raising=False,
        )

        config = AppConfig.from_yaml(yaml_path)

        assert config.models.api_key == "credential-key"
        assert requested_providers == ["openai"]

    def test_from_yaml_environment_key_skips_system_credential_store(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            "models:\n  provider: anthropic\n  default_model: claude-test\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("NAUMI_MODELS__API_KEY", "environment-key")
        monkeypatch.setitem(AppConfig.model_config, "env_file", None)

        def fail_load(*, provider=None):
            pytest.fail(f"环境变量存在时不应读取 provider 凭据：{provider}")

        monkeypatch.setattr(
            "naumi_agent.config.settings.load_model_api_key",
            fail_load,
            raising=False,
        )

        config = AppConfig.from_yaml(yaml_path)

        assert config.models.api_key == "environment-key"

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

    def test_api_host_defaults_to_localhost(self) -> None:
        assert AppConfig().api.host == "127.0.0.1"

    def test_api_port_defaults_to_mac_workbench_daemon_port(self) -> None:
        assert AppConfig().api.port == 8765

    def test_api_host_from_example_yaml(self) -> None:
        example_path = Path(__file__).resolve().parents[2] / "config.yaml.example"
        config = AppConfig.from_yaml(example_path)
        assert config.api.host == "127.0.0.1"
        assert config.models.provider == "kimi"
        assert config.models.temperature == 1.0

    def test_api_port_from_example_yaml(self) -> None:
        example_path = Path(__file__).resolve().parents[2] / "config.yaml.example"
        config = AppConfig.from_yaml(example_path)
        assert config.api.port == 8765

    @pytest.mark.parametrize("value", [0, 17])
    def test_parallel_tool_limit_rejects_out_of_range_values(self, value: int) -> None:
        with pytest.raises(ValueError):
            AppConfig(safety={"max_parallel_tools": value})  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [1, 4, 16])
    def test_parallel_tool_limit_accepts_supported_values(self, value: int) -> None:
        config = AppConfig(safety={"max_parallel_tools": value})  # type: ignore[arg-type]

        assert config.safety.max_parallel_tools == value
