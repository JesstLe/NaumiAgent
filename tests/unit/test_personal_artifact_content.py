"""Actual Page/CSV preview contracts; labels never certify an external fact."""

import json

import pytest

from naumi_agent.personal.artifact_content import page_refs, preview, validate_page, validate_widget
from naumi_agent.personal.models import PersonalAgentError


def page():
    return {
        "version": 1,
        "type": "page",
        "title": "Ari 的预算",
        "sections": [
            {
                "id": "result",
                "title": "结果",
                "evidence_status": "fact",
                "markdown": "825 元",
                "widget": {
                    "version": 1,
                    "type": "table",
                    "title": "核算",
                    "columns": [{"key": "amount", "label": "金额"}],
                    "rows": [{"amount": 825}],
                },
            }
        ],
    }


def test_page_roundtrip_retains_actual_numbers_and_independent_identity():
    value = page()
    content = json.dumps(value, ensure_ascii=False).encode()
    assert preview(content, ".json") == value
    assert page_refs(content, ".json") == []
    assert preview(b"# Hello", ".md") == {"type": "text", "content": "# Hello", "truncated": False}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(version=True),
        lambda p: p.update(version=2),
        lambda p: p.update(sections=[]),
        lambda p: p.update(extra="unknown"),
        lambda p: p["sections"][0].update(id="../path"),
        lambda p: p["sections"][0].update(evidence_status="verified"),
        lambda p: p["sections"][0].update(source_refs=[{"artifact_id": "forged"}]),
        lambda p: p["sections"].append(p["sections"][0].copy()),
        lambda p: p["sections"][0]["widget"].update(rows=[{"amount": float("nan")}]),
        lambda p: p["sections"][0]["widget"].update(columns=[]),
        lambda p: p["sections"][0]["widget"].update(rows=[{"amount": {"object": 1}}]),
        lambda p: p["sections"][0]["widget"].update(rows=[{"amount": "a" * 2001}]),
        lambda p: p["sections"][0]["widget"].update(version=True),
    ],
)
def test_invalid_page_cannot_be_saved_as_real_interactive_page(mutation):
    value = page()
    mutation(value)
    with pytest.raises(PersonalAgentError):
        validate_page(value)


def test_csv_reads_real_multiline_quotes_bom_empty_cells_and_formula_as_data():
    content = '\ufeff名称,值\r\n"两行\n文字",825\r\n=1+1,\r\n'.encode()
    value = preview(content, ".csv")
    assert value["total_rows"] == 2
    assert value["rows"] == [{"c0": "两行\n文字", "c1": "825"}, {"c0": "=1+1", "c1": ""}]


def test_csv_preview_reports_actual_total_without_silently_losing_rows():
    value = preview(("value\n" + "825\n" * 2001).encode(), ".csv")
    assert len(value["rows"]) == 2000
    assert value["total_rows"] == 2001 and value["preview_limit"] == 2000


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"a,b\n1\n",
        b'a,b\n"unterminated,2',
        ("a\n" + "x" * 2001).encode(),
        (",".join(str(i) for i in range(21)) + "\n").encode(),
    ],
)
def test_invalid_or_excessive_csv_is_not_reported_as_a_successful_preview(content):
    with pytest.raises(PersonalAgentError):
        preview(content, ".csv")


@pytest.mark.parametrize(
    "widget",
    [
        {"type": "image", "title": "x", "url": "file:///private"},
        {"type": "html", "title": "x", "html": "<p>x</p>", "height": True},
        {"type": "html", "title": "x", "html": "<p>x</p>", "height": 10000},
        {"type": "tabs", "title": "x", "tabs": []},
        {
            "type": "chart",
            "title": "x",
            "x": "day",
            "series": [{"key": "n", "label": "n"}],
            "rows": [{"day": "now", "n": True}],
        },
        {"type": "metrics", "title": "x", "items": [{"label": "x", "value": True}]},
    ],
)
def test_components_match_bounded_frontend_contract(widget):
    with pytest.raises(PersonalAgentError):
        validate_widget(widget)


def test_large_text_is_explicitly_truncated_and_binary_not_decoded_as_text():
    assert preview(b"x" * 100001, ".txt")["truncated"] is True
    assert preview(b"binary", ".pdf") == {"type": "file"}
    with pytest.raises(PersonalAgentError):
        preview(b'{"same":1,"same":2}', ".json")
