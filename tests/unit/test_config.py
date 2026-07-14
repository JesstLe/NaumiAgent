"""配置系统测试."""

from pathlib import Path

import pytest
import yaml

from naumi_agent.config.settings import AppConfig
from naumi_agent.model.reasoning import ReasoningEffort, ReasoningEffortSetting


class TestAppConfig:
    def test_default_config(self) -> None:
        config = AppConfig()
        assert config.models.default_model == "claude-sonnet-4-6"
        assert config.models.reasoning_effort is ReasoningEffortSetting.AUTO
        assert config.safety.max_turns == 50
        assert config.safety.max_parallel_tools == 4
        assert config.safety.max_parallel_agents == 4
        assert config.memory.session_db_path == "data/sessions.db"
        assert config.ui.theme == "dark"
        assert config.ui.output_style == "detailed"
        assert config.browser_daemon.base_url == "http://127.0.0.1:3005"
        assert config.browser_daemon.project_dir.endswith("browser-debugging-daemon")
        assert config.browser.max_concurrent_runs == 2
        assert config.browser.run_history_limit == 200
        assert config.search.provider_order == ("brave", "duckduckgo", "browser")
        assert config.search.brave.api_key_ref == "{env:BRAVE_SEARCH_API_KEY}"
        assert config.search.brave.safesearch == "moderate"

    def test_search_config_loads_advanced_brave_options(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            """
search:
  provider_order: [brave, duckduckgo]
  brave:
    enabled: true
    api_key_ref: "{env:NAUMI_TEST_BRAVE_KEY}"
    country: CN
    search_lang: zh-hans
    ui_lang: zh-CN
    safesearch: strict
    spellcheck: false
    freshness: pw
    timeout_seconds: 12
""",
            encoding="utf-8",
        )

        config = AppConfig.from_yaml(yaml_path)

        assert config.search.provider_order == ("brave", "duckduckgo")
        assert config.search.brave.country == "CN"
        assert config.search.brave.search_lang == "zh-hans"
        assert config.search.brave.ui_lang == "zh-CN"
        assert config.search.brave.safesearch == "strict"
        assert config.search.brave.spellcheck is False
        assert config.search.brave.freshness == "pw"
        assert config.search.brave.timeout_seconds == 12

    def test_search_config_resolves_custom_environment_reference(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = AppConfig(
            search={"brave": {"api_key_ref": "{env:NAUMI_CUSTOM_BRAVE_KEY}"}}
        )
        monkeypatch.setenv("NAUMI_CUSTOM_BRAVE_KEY", "custom-search-secret")

        assert config.search.brave.resolve_api_key() == "custom-search-secret"
        assert config.search.brave.resolve_api_key({}) is None
        assert "custom-search-secret" not in repr(config.search.brave)

    @pytest.mark.parametrize(
        "freshness",
        ["today", "2026-13-01to2026-14-01", "2026-07-14to2026-07-01"],
    )
    def test_search_config_rejects_invalid_freshness(self, freshness: str) -> None:
        with pytest.raises(ValueError, match="freshness"):
            AppConfig(search={"brave": {"freshness": freshness}})  # type: ignore[arg-type]

    def test_search_config_rejects_inline_brave_secret_without_echo(self) -> None:
        secret = "brave-inline-secret-value"

        with pytest.raises(ValueError) as error:
            AppConfig(search={"brave": {"api_key_ref": secret}})  # type: ignore[arg-type]

        assert secret not in str(error.value)
        assert "api_key_ref" in str(error.value)

    @pytest.mark.parametrize(
        "provider_order",
        [[], ["brave", "brave"], ["brave", "unknown"]],
    )
    def test_search_config_rejects_invalid_provider_order(
        self,
        provider_order: list[str],
    ) -> None:
        with pytest.raises(ValueError, match="provider_order"):
            AppConfig(search={"provider_order": provider_order})  # type: ignore[arg-type]

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

    def test_from_yaml_anchors_model_catalog_path(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            "models:\n  catalog_path: catalogs/providers.json\n",
            encoding="utf-8",
        )

        config = AppConfig.from_yaml(yaml_path)

        assert config.models.catalog_path == str(
            tmp_path / "catalogs" / "providers.json"
        )

    def test_naumi_yaml_anchors_catalog_and_runtime_data(
        self,
        tmp_path: Path,
    ) -> None:
        project = tmp_path / "project"
        yaml_path = project / ".naumi" / "config.yaml"
        yaml_path.parent.mkdir(parents=True)
        yaml_path.write_text(
            "models:\n  catalog_path: providers.json\n",
            encoding="utf-8",
        )

        config = AppConfig.from_yaml(yaml_path)

        assert config.models.catalog_path == str(
            project / ".naumi" / "providers.json"
        )
        assert config.memory.session_db_path == str(
            project / ".naumi" / "data" / "sessions.db"
        )
        assert config.memory.vector_db_path == str(
            project / ".naumi" / "data" / "chroma"
        )

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

    def test_bind_runtime_workspace_replaces_legacy_root_and_preserves_extra_dirs(
        self,
        tmp_path: Path,
    ) -> None:
        legacy = tmp_path / "legacy"
        launch = tmp_path / "launch"
        shared = tmp_path / "shared"
        for path in (legacy, launch, shared):
            path.mkdir()
        config = AppConfig(
            workspace_root=str(legacy),
            safety={
                "allowed_dirs": [
                    str(legacy),
                    str(shared),
                    str(shared),
                    "relative-extra",
                ],
            },
        )

        result = config.bind_runtime_workspace(launch)

        assert result == launch.resolve()
        assert config.workspace_root == str(launch.resolve())
        assert config.safety.allowed_dirs == [
            str(launch.resolve()),
            str(shared.resolve()),
            str((launch / "relative-extra").resolve()),
        ]

    def test_bind_runtime_workspace_uses_current_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        config = AppConfig(workspace_root=".", safety={"allowed_dirs": ["."]})

        result = config.bind_runtime_workspace()

        assert result == tmp_path.resolve()
        assert config.safety.allowed_dirs == [str(tmp_path.resolve())]

    def test_bind_runtime_workspace_rejects_missing_directory(
        self,
        tmp_path: Path,
    ) -> None:
        missing = tmp_path / "missing"
        config = AppConfig()

        with pytest.raises(ValueError, match="启动工作区不存在或不是目录"):
            config.bind_runtime_workspace(missing)

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

    def test_example_documents_naumi_config_and_local_catalog(self) -> None:
        example_path = Path(__file__).resolve().parents[2] / "config.yaml.example"
        text = example_path.read_text(encoding="utf-8")

        assert "复制为 .naumi/config.yaml" in text
        assert '# catalog_path: "providers.json"' in text
        assert "reasoning_effort: auto" in text
        assert "reasoning_efforts:" in text
        assert "default_reasoning_effort:" in text
        assert 'api_key_ref: "{env:BRAVE_SEARCH_API_KEY}"' in text
        assert "api_key:" not in text

    @pytest.mark.parametrize(
        "value",
        ["auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"],
    )
    def test_reasoning_effort_accepts_public_config_values(self, value: str) -> None:
        config = AppConfig(models={"reasoning_effort": value})  # type: ignore[arg-type]

        assert config.models.reasoning_effort.value == value

    def test_reasoning_effort_rejects_unknown_config_value(self) -> None:
        with pytest.raises(ValueError, match="reasoning_effort"):
            AppConfig(models={"reasoning_effort": "turbo"})  # type: ignore[arg-type]

    def test_reasoning_effort_loads_from_nested_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NAUMI_MODELS__REASONING_EFFORT", "high")
        monkeypatch.setitem(AppConfig.model_config, "env_file", None)

        config = AppConfig()

        assert config.models.reasoning_effort is ReasoningEffortSetting.HIGH

    def test_model_info_loads_reasoning_selection_and_capability_override(self) -> None:
        config = AppConfig(
            models={
                "model_info": {
                    "anthropic/claude-opus-4-6": {
                        "reasoning_effort": "max",
                        "reasoning_efforts": ["low", "medium", "high", "max"],
                        "default_reasoning_effort": "high",
                    }
                }
            }
        )

        meta = config.models.model_info["anthropic/claude-opus-4-6"]
        assert meta.reasoning_effort is ReasoningEffortSetting.MAX
        assert meta.reasoning_efforts == (
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
            ReasoningEffort.MAX,
        )
        assert meta.default_reasoning_effort is ReasoningEffort.HIGH

    @pytest.mark.parametrize(
        "meta",
        [
            {"max_context": 0},
            {"max_output": -1},
            {"input_cost_per_million": -0.01},
            {"output_cost_per_million": -0.01},
            {"max_context": 4_096, "max_output": 8_192},
        ],
    )
    def test_model_info_rejects_impossible_limits_and_prices(
        self,
        meta: dict[str, int | float],
    ) -> None:
        with pytest.raises(ValueError):
            AppConfig(models={"model_info": {"vendor/model": meta}})  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "meta",
        [
            {"input_modalities": ("text", "text")},
            {"supports_vision": False, "input_modalities": ("text", "image")},
            {"supports_reasoning": False, "reasoning_efforts": ("low",)},
            {"supports_reasoning": False, "reasoning_effort": "high"},
            {"supports_tools": False, "supports_parallel_tools": True},
            {"input_cost_per_million": float("inf")},
        ],
    )
    def test_model_info_rejects_contradictory_capabilities(
        self,
        meta: dict[str, object],
    ) -> None:
        with pytest.raises(ValueError):
            AppConfig(models={"model_info": {"vendor/model": meta}})  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "meta",
        [
            {"reasoning_efforts": []},
            {"reasoning_efforts": ["low", "low"]},
            {
                "reasoning_efforts": ["low", "high"],
                "default_reasoning_effort": "medium",
            },
            {"default_reasoning_effort": "high"},
        ],
    )
    def test_model_info_rejects_invalid_reasoning_capability_override(
        self,
        meta: dict[str, object],
    ) -> None:
        with pytest.raises(ValueError):
            AppConfig(models={"model_info": {"model-a": meta}})  # type: ignore[arg-type]

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

    @pytest.mark.parametrize("value", [0, 33])
    def test_parallel_agent_limit_rejects_out_of_range_values(self, value: int) -> None:
        with pytest.raises(ValueError):
            AppConfig(safety={"max_parallel_agents": value})  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [1, 4, 32])
    def test_parallel_agent_limit_accepts_supported_values(self, value: int) -> None:
        config = AppConfig(safety={"max_parallel_agents": value})  # type: ignore[arg-type]

        assert config.safety.max_parallel_agents == value

    @pytest.mark.parametrize("value", [0, 9])
    def test_browser_concurrency_rejects_out_of_range_values(self, value: int) -> None:
        with pytest.raises(ValueError):
            AppConfig(browser={"max_concurrent_runs": value})  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [1, 2, 8])
    def test_browser_concurrency_accepts_supported_values(self, value: int) -> None:
        config = AppConfig(browser={"max_concurrent_runs": value})  # type: ignore[arg-type]

        assert config.browser.max_concurrent_runs == value

    def test_browser_concurrency_loads_from_nested_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NAUMI_BROWSER__MAX_CONCURRENT_RUNS", "5")

        config = AppConfig()

        assert config.browser.max_concurrent_runs == 5

    @pytest.mark.parametrize("value", [19, 5001])
    def test_browser_history_limit_rejects_out_of_range_values(
        self,
        value: int,
    ) -> None:
        with pytest.raises(ValueError):
            AppConfig(browser={"run_history_limit": value})  # type: ignore[arg-type]
