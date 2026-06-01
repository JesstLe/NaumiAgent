"""Code excerpt rendering tests."""

from naumi_agent.ui.code_excerpt import excerpt_markdown_code_blocks


def test_short_code_block_is_preserved() -> None:
    markdown = "说明\n```python\nprint('ok')\n```\n结束"

    assert excerpt_markdown_code_blocks(markdown, max_lines=5) == markdown


def test_long_code_block_shows_prefix_and_hidden_count() -> None:
    code = "\n".join(f"line_{idx}" for idx in range(1, 7))
    markdown = f"```python\n{code}\n```\n"

    rendered = excerpt_markdown_code_blocks(markdown, max_lines=3)

    assert "line_1" in rendered
    assert "line_3" in rendered
    assert "line_4" not in rendered
    assert "已隐藏 3 行代码" in rendered
    assert "仅展示前 3 行摘录" in rendered


def test_unclosed_long_code_block_is_closed_for_display() -> None:
    code = "\n".join(f"line_{idx}" for idx in range(1, 6))
    markdown = f"```python\n{code}"

    rendered = excerpt_markdown_code_blocks(markdown, max_lines=2)

    assert rendered.count("```") == 2
    assert "line_2" in rendered
    assert "line_3" not in rendered
    assert "已隐藏 3 行代码" in rendered
