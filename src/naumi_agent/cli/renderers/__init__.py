"""CLI message renderers — one per UIMessage type.

Each renderer takes a typed UIMessage and returns ANSI-formatted text
suitable for the prompt_toolkit CLIApp's output window.

The dispatch is table-driven: new message types only require adding
a renderer function and registering it — no if/elif chain modifications.
"""

from naumi_agent.cli.renderers.registry import CLIRenderer

__all__ = ["CLIRenderer"]
