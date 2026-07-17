"""Shared TUI detail projection for HAR-07.3."""

from naumi_agent.ui.harness_detail import render_harness_detail_markdown


def _explain() -> dict:
    return {
        "lookup_status": "ok",
        "run_id": "detail-run",
        "message": "",
        "explanation": {
            "status": "completed_unverified",
            "objective": "修复并验证详情页",
            "summary": "发现验证问题",
            "criteria": [
                {
                    "id": "criterion-tests",
                    "description": "定向测试通过",
                    "status": "unsatisfied",
                    "evidence_ids": ["evidence-test"],
                }
            ],
            "failure_classes": ["verification_failure"],
            "findings": [
                {
                    "failure_class": "verification_failure",
                    "source": "check:unit",
                    "message": "单元测试失败",
                    "next_step": "修复后重新运行",
                    "check_ids": ["unit"],
                    "evidence_ids": ["evidence-test"],
                }
            ],
            "checks": [{"id": "unit", "status": "failed", "duration_ms": 42}],
            "evidence": [
                {
                    "id": "evidence-test",
                    "kind": "test_report",
                    "status": "recorded",
                    "digest_prefix": "abcdef123456",
                    "uri": "artifact://test-report",
                }
            ],
        },
    }


def _replay() -> dict:
    return {
        "lookup_status": "ok",
        "run_id": "detail-run",
        "message": "",
        "result": {
            "status": "changed",
            "anomalies": ["tree_changed"],
            "differences": [
                {"field": "tree", "baseline": "before", "current": "after"}
            ],
            "artifacts": [
                {
                    "id": "artifact-1",
                    "kind": "test_report",
                    "reference": "artifact://test-report",
                    "status": "digest_mismatch",
                }
            ],
            "timeline": [{"kind": "check", "id": "unit", "status": "failed"}],
        },
    }


def test_harness_detail_markdown_renders_all_authoritative_sections() -> None:
    rendered = render_harness_detail_markdown(_explain(), _replay())

    for expected in (
        "Harness 运行详情",
        "修复并验证详情页",
        "准则",
        "定向测试通过",
        "失败分类",
        "验证失败",
        "检查",
        "证据",
        "Replay",
        "差异",
        "Artifact",
    ):
        assert expected in rendered
    assert "private" not in rendered


def test_harness_detail_markdown_handles_unavailable_without_fabrication() -> None:
    rendered = render_harness_detail_markdown(
        {
            "lookup_status": "unavailable",
            "run_id": "detail-run",
            "message": "状态库暂不可用",
        },
        {
            "lookup_status": "not_found",
            "run_id": "detail-run",
            "message": "Replay 不存在",
        },
    )

    assert "状态库暂不可用" in rendered
    assert "Replay 不存在" in rendered
    assert "已验证" not in rendered
