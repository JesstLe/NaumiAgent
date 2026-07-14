"""Slash command completer tests."""

from __future__ import annotations

from unittest.mock import patch

from prompt_toolkit.document import Document

from naumi_agent.cli_completer import COMMANDS, SlashCommandCompleter


def _complete(text: str) -> list[str]:
    c = SlashCommandCompleter()
    doc = Document(text, len(text))
    return [r.text for r in c.get_completions(doc, None)]


class TestSlashCommandCompleter:
    def test_slash_only_shows_all(self):
        results = _complete("/")
        assert len(results) == len(COMMANDS)

    def test_exact_match(self):
        assert "/chaos" in _complete("/chaos")

    def test_partial_match(self):
        results = _complete("/ch")
        assert "/chaos" in results

    def test_regex_match(self):
        results = _complete("/co.*o")
        assert "/cooe" in results
        assert "/cosmos" in results

    def test_invalid_regex_does_not_crash(self):
        assert _complete("/[") == []

    def test_no_match(self):
        assert _complete("/zzzzz") == []

    def test_non_slash_no_completions(self):
        assert _complete("hello") == []

    def test_space_stops_completion(self):
        assert _complete("/chaos ") == []

    def test_all_commands_start_with_slash(self):
        assert _complete("/") == [cmd for cmd, _, _ in COMMANDS]

    def test_quit_and_exit_registered(self):
        results = _complete("/")
        assert "/quit" in results
        assert "/exit" in results

    def test_browser_daemon_registered(self):
        assert "/bdaemon" in _complete("/")

    def test_provider_models_command_registered(self):
        assert "/models" in _complete("/")
        descriptions = {cmd: desc for cmd, desc, _ in COMMANDS}
        assert "provider" in descriptions["/models"].lower()

    def test_reasoning_effort_command_registered(self):
        assert "/effort" in _complete("/")

    def test_browser_daemon_description_lists_control_subcommands(self):
        descriptions = {cmd: desc for cmd, desc, _ in COMMANDS}
        description = descriptions["/bdaemon"]
        for subcommand in ("reply", "resume", "abort", "manual"):
            assert subcommand in description

    async def test_prompt_with_completion_fallback(self):
        with patch(
            "naumi_agent.main.console"
        ) as mock_console, patch(
            "prompt_toolkit.PromptSession",
            side_effect=RuntimeError("no tty"),
        ):
            mock_console.input.return_value = "  hello  "
            from naumi_agent.cli_completer import prompt_with_completion

            result = await prompt_with_completion()
            mock_console.input.assert_called_once()
            assert result == "hello"
