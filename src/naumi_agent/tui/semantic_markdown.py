"""Textual Markdown widgets with visible math source support."""

from markdown_it import MarkdownIt
from markdown_it.rules_core import StateCore
from mdit_py_plugins.dollarmath import dollarmath_plugin
from textual.widgets import Markdown


def semantic_markdown_parser() -> MarkdownIt:
    """Return a Textual-compatible parser that keeps LaTeX source visible."""
    parser = MarkdownIt("gfm-like").use(dollarmath_plugin)
    parser.core.ruler.after("inline", "naumi_math_display", _rewrite_math_tokens)
    return parser


def _rewrite_math_tokens(state: StateCore) -> None:
    for token in state.tokens:
        if token.type == "math_block":
            token.type = "fence"
            token.tag = "code"
            token.info = "latex"
            token.markup = "$$"
            token.content = token.content.strip("\n")
            continue
        if token.type != "inline" or token.children is None:
            continue
        for child in token.children:
            if child.type != "math_inline":
                continue
            child.type = "code_inline"
            child.tag = "code"
            child.markup = "`"
            child.content = f"${child.content}$"


class SemanticMarkdown(Markdown):
    """Markdown widget using NaumiAgent's semantic math parser."""

    def __init__(self, markdown: str | None = None, **kwargs: object) -> None:
        kwargs.setdefault("parser_factory", semantic_markdown_parser)
        super().__init__(markdown, **kwargs)


__all__ = ["SemanticMarkdown", "semantic_markdown_parser"]
