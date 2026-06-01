"""TUI message renderers — one per UIMessage type.

Each renderer takes a typed UIMessage and a ChatPanel, applying the
appropriate Textual widget update. The dispatch is table-driven: new
message types only require adding a renderer function and registering it.
"""

from naumi_agent.tui.renderers.registry import TUIRenderer

__all__ = ["TUIRenderer"]
