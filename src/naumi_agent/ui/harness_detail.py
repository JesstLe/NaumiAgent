"""Shared, bounded text projection for Harness Explain and Replay details."""

from __future__ import annotations

from typing import Any

_FAILURE_LABELS = {
    "specification_gap": "规格缺口",
    "knowledge_gap": "知识缺口",
    "context_overflow": "上下文溢出",
    "tool_contract_error": "工具契约错误",
    "permission_block": "权限阻断",
    "environment_error": "环境异常",
    "implementation_error": "实现错误",
    "verification_failure": "验证失败",
    "evaluation_error": "评测错误",
    "agent_premature_finish": "Agent 过早结束",
    "agent_repetition": "Agent 重复执行",
    "human_judgment_required": "需要人工判断",
}

_STATUS_LABELS = {
    "completed_verified": "已验证",
    "completed_unverified": "未验证",
    "satisfied": "已满足",
    "unsatisfied": "未满足",
    "passed": "通过",
    "failed": "失败",
    "recorded": "已记录",
    "verified": "已验证",
    "reproduced": "已复现",
    "changed": "已变化",
    "digest_mismatch": "摘要不一致",
    "missing": "缺失",
    "ok": "正常",
}


def render_harness_detail_markdown(
    explain_payload: dict[str, Any],
    replay_payload: dict[str, Any],
) -> str:
    """Render only the typed public detail fields shared by CLI and TUI."""
    run_id = _text(explain_payload.get("run_id") or replay_payload.get("run_id") or "-")
    lines = ["# Harness 运行详情", "", f"- Run ID：`{_code(run_id)}`"]
    lines.extend(_render_explain(explain_payload))
    lines.extend(_render_replay(replay_payload))
    return "\n".join(lines).rstrip()


def render_harness_evidence_markdown(explain_payload: dict[str, Any]) -> str:
    """Render an evidence-first view using only typed Harness Explain fields."""
    run_id = _text(explain_payload.get("run_id") or "-")
    lines = ["# Harness 证据焦点", "", f"- Run ID：`{_code(run_id)}`"]
    if explain_payload.get("lookup_status") != "ok" or not isinstance(
        explain_payload.get("explanation"), dict
    ):
        lines.extend(
            ["", f"> {_message(explain_payload, 'Harness 证据详情不可用。')}"]
        )
        return "\n".join(lines).rstrip()

    value = explain_payload["explanation"]
    criteria = _objects(value.get("criteria"), 100)
    findings = _objects(value.get("findings"), 20)
    evidence = _objects(value.get("evidence"), 100)
    criterion_refs, finding_refs, referenced_ids = _evidence_references(
        criteria,
        findings,
    )
    known_ids = {
        evidence_id
        for item in evidence
        if (evidence_id := _text(item.get("id")))
    }
    missing_ids = [item for item in referenced_ids if item not in known_ids]
    lines.extend(
        [
            f"- 状态：{_status(value.get('status'))}",
            f"- 目标：{_text(value.get('objective')) or '未记录'}",
            f"- 摘要：{_text(value.get('summary')) or '无'}",
            "",
            "## 权威证据记录",
            "",
        ]
    )
    if not evidence:
        lines.append("- 未记录证据")
    for item in evidence:
        evidence_id = _text(item.get("id"))
        lines.extend(
            [
                f"### `{_code(evidence_id or '未命名证据')}`",
                "",
                f"- 类型：{_text(item.get('kind')) or 'unknown'}",
                f"- 状态：{_status(item.get('status'))}",
            ]
        )
        digest = _text(item.get("digest_prefix"))
        uri = _text(item.get("uri"))
        if digest:
            lines.append(f"- Digest：`{_code(digest)}`")
        if uri:
            lines.append(f"- URI：{uri}")

        related_criteria = criterion_refs.get(evidence_id, ())
        related_findings = finding_refs.get(evidence_id, ())
        for criterion in related_criteria:
            lines.append(
                f"- 准则：[{_status(criterion.get('status'))}] "
                f"`{_code(criterion.get('id'))}` · "
                f"{_text(criterion.get('description')) or '未记录描述'}"
            )
        for finding in related_findings:
            failure_class = _text(finding.get("failure_class"))
            label = _FAILURE_LABELS.get(failure_class, failure_class or "发现")
            source = _text(finding.get("source"))
            check_ids = _texts(finding.get("check_ids"), 50)
            suffix = (
                f" · 检查 {', '.join(check_ids)}" if check_ids else ""
            )
            if source:
                suffix += f" · 来源 {source}"
            lines.append(
                f"- 发现：{label} · "
                f"{_text(finding.get('message')) or '无说明'}{suffix}"
            )
            next_step = _text(finding.get("next_step"))
            if next_step:
                lines.append(f"  - 下一步：{next_step}")
        if not related_criteria and not related_findings:
            lines.append("- 关联：未被准则或发现引用")
        lines.append("")

    if missing_ids:
        lines.extend(["## 引用缺口", ""])
        lines.extend(
            f"- `{_code(item)}`：引用存在但权威证据记录缺失"
            for item in missing_ids
        )
    return "\n".join(lines).rstrip()


def _render_explain(payload: dict[str, Any]) -> list[str]:
    if payload.get("lookup_status") != "ok" or not isinstance(payload.get("explanation"), dict):
        return ["", "## Explain", "", f"> {_message(payload, 'Explain 详情不可用。')}"]
    value = payload["explanation"]
    lines = [
        "",
        "## 概览",
        "",
        f"- 目标：{_text(value.get('objective')) or '未记录'}",
        f"- 状态：{_status(value.get('status'))}",
        f"- 摘要：{_text(value.get('summary')) or '无'}",
        "",
        "## 准则",
        "",
    ]
    criteria = _objects(value.get("criteria"), 100)
    lines.extend(
        (
            f"- [{_status(item.get('status'))}] "
            f"`{_code(item.get('id'))}` · "
            f"{_text(item.get('description')) or '未命名准则'}"
            f" · 证据 {len(_texts(item.get('evidence_ids'), 100))}"
        )
        for item in criteria
    )
    if not criteria:
        lines.append("- 未记录验收准则")

    lines.extend(["", "## 失败分类", ""])
    failure_classes = _texts(value.get("failure_classes"), 20)
    lines.extend(f"- {_FAILURE_LABELS.get(item, item)}" for item in failure_classes)
    if not failure_classes:
        lines.append("- 无已分类失败")

    findings = _objects(value.get("findings"), 20)
    for item in findings:
        failure_class = _text(item.get("failure_class"))
        label = _FAILURE_LABELS.get(failure_class, failure_class)
        source = _text(item.get("source"))
        suffix = f" · 来源 {source}" if source else ""
        lines.append(
            f"- {label or '发现'}：{_text(item.get('message')) or '无说明'}{suffix}"
        )
        next_step = _text(item.get("next_step"))
        if next_step:
            lines.append(f"  - 下一步：{next_step}")

    lines.extend(["", "## 检查", ""])
    checks = _objects(value.get("checks"), 50)
    lines.extend(
        (
            f"- `{_code(item.get('id'))}` · {_status(item.get('status'))} · "
            f"{max(0, _integer(item.get('duration_ms')))}ms"
        )
        for item in checks
    )
    if not checks:
        lines.append("- 未记录检查")

    lines.extend(["", "## 证据", ""])
    evidence = _objects(value.get("evidence"), 100)
    lines.extend(
        (
            f"- `{_code(item.get('id'))}` · {_text(item.get('kind')) or 'unknown'} · "
            f"{_status(item.get('status'))}"
            + (
                f" · digest {_text(item.get('digest_prefix'))}"
                if _text(item.get("digest_prefix"))
                else ""
            )
        )
        + (f" · {_text(item.get('uri'))}" if _text(item.get("uri")) else "")
        for item in evidence
    )
    if not evidence:
        lines.append("- 未记录证据")
    return lines


def _render_replay(payload: dict[str, Any]) -> list[str]:
    lines = ["", "## Replay", ""]
    if payload.get("lookup_status") != "ok" or not isinstance(payload.get("result"), dict):
        lines.append(f"> {_message(payload, 'Replay 详情不可用。')}")
        return lines
    value = payload["result"]
    lines.append(f"- 状态：{_status(value.get('status'))}")
    anomalies = _texts(value.get("anomalies"), 50)
    if anomalies:
        lines.append(f"- 异常：{', '.join(anomalies)}")
    lines.extend(["", "### 差异", ""])
    differences = _objects(value.get("differences"), 50)
    lines.extend(
        (
            f"- `{_code(item.get('field'))}`：{_text(item.get('baseline'))} → "
            f"{_text(item.get('current'))}"
        )
        for item in differences
    )
    if not differences:
        lines.append("- 无差异")
    lines.extend(["", "### Artifact", ""])
    artifacts = _objects(value.get("artifacts"), 100)
    lines.extend(
        (
            f"- `{_code(item.get('id'))}` · {_text(item.get('kind')) or 'unknown'} · "
            f"{_status(item.get('status'))}"
        )
        + (f" · {_text(item.get('reference'))}" if _text(item.get("reference")) else "")
        for item in artifacts
    )
    if not artifacts:
        lines.append("- 无 Artifact")
    timeline = _objects(value.get("timeline"), 200)
    lines.extend(["", f"- Timeline {len(timeline)} 条事件"])
    return lines


def _message(payload: dict[str, Any], fallback: str) -> str:
    return _text(payload.get("message")) or fallback


def _status(value: Any) -> str:
    text = _text(value)
    return _STATUS_LABELS.get(text, text or "未知")


def _objects(value: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value[:limit] if isinstance(item, dict)]


def _texts(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value[:limit] if _text(item)]


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())[:500]


def _code(value: Any) -> str:
    return _text(value).replace("`", "\\`")


def _integer(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _evidence_references(
    criteria: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> tuple[
    dict[str, tuple[dict[str, Any], ...]],
    dict[str, tuple[dict[str, Any], ...]],
    tuple[str, ...],
]:
    criterion_refs: dict[str, list[dict[str, Any]]] = {}
    finding_refs: dict[str, list[dict[str, Any]]] = {}
    referenced_ids: list[str] = []
    seen: set[str] = set()
    for target, items in ((criterion_refs, criteria), (finding_refs, findings)):
        for item in items:
            for evidence_id in dict.fromkeys(_texts(item.get("evidence_ids"), 100)):
                target.setdefault(evidence_id, []).append(item)
                if evidence_id not in seen:
                    seen.add(evidence_id)
                    referenced_ids.append(evidence_id)
    return (
        {key: tuple(value) for key, value in criterion_refs.items()},
        {key: tuple(value) for key, value in finding_refs.items()},
        tuple(referenced_ids),
    )


__all__ = [
    "render_harness_detail_markdown",
    "render_harness_evidence_markdown",
]
