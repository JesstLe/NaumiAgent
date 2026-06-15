"""main.py TUI 启动噪声拦截测试."""

import logging
import subprocess
import sys

import pytest


def test_capture_tui_launch_noise_captures_stdout_stderr_and_loggers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from naumi_agent.main import _capture_tui_launch_noise

    noisy = logging.getLogger("LiteLLM")
    engine_logger = logging.getLogger("naumi_agent")
    old_noisy_level = noisy.level
    old_engine_level = engine_logger.level

    try:
        noisy.setLevel(logging.INFO)
        engine_logger.setLevel(logging.INFO)
        with _capture_tui_launch_noise() as (stdout_buf, stderr_buf):
            print("startup text")
            noisy.info("hidden litellm")
            engine_logger.info("hidden naumi agent")
        assert stdout_buf.getvalue() == "startup text\n"
        assert "hidden litellm" not in stderr_buf.getvalue()
        assert "hidden naumi agent" not in stderr_buf.getvalue()

        output = capsys.readouterr()
        assert output.out == ""
        assert output.err == ""
    finally:
        noisy.setLevel(old_noisy_level)
        engine_logger.setLevel(old_engine_level)


def test_main_import_suppresses_litellm_provider_preload_warnings() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import naumi_agent.main; "
                "from naumi_agent.orchestrator.engine import AgentEngine; "
                "print('imported')"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "imported" in result.stdout
    assert "could not pre-load bedrock-runtime" not in combined
    assert "could not pre-load sagemaker-runtime" not in combined
