"""Command completer tests."""

from __future__ import annotations

from unittest.mock import patch

from prompt_toolkit.document import Document

from naumi_agent.cli.completer import COMMANDS, SlashCommandCompleter


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

    def test_prompt_with_completion_fallback(self):
        with patch(
            "naumi_agent.cli.display.console"
        ) as mock_console, patch(
            "prompt_toolkit.prompt",
            side_effect=RuntimeError("no tty"),
            create=True,
        ):
            mock_console.input.return_value = "  hello  "
            from naumi_agent.cli.completer import prompt_with_completion

            result = prompt_with_completion()
            mock_console.input.assert_called_once()
            assert result == "hello"
