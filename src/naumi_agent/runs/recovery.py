"""Conservative read-time recovery of legacy tool previews from session messages."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from naumi_agent.runs.store import ChatRunRecord
from naumi_agent.runs.tool_evidence import OUTPUT_PREVIEW_LIMIT, public_tool_text


def restore_tool_previews(run: ChatRunRecord, messages: list[dict[str, Any]]) -> ChatRunRecord:
    tools = [step for step in run.steps if step.stage == "tool"]
    if not tools or all(step.detail or step.metadata.get("output_recorded") for step in tools):
        return run
    request = next((step.summary for step in run.steps if step.stage == "request"), None)
    if request is None:
        return run
    blocks: list[list[dict[str, Any]]] = []
    for message in messages:
        if message.get("role") == "user":
            blocks.append([message])
        elif blocks:
            blocks[-1].append(message)
    candidates = []
    for block in blocks:
        task = public_tool_text(block[0].get("content")).strip()
        summary = task if len(task) <= 160 else task[:157] + "..."
        if summary != request:
            continue
        calls = [
            call
            for message in block
            if message.get("role") == "assistant"
            for call in message.get("tool_calls", [])
        ]
        if [call.get("function", {}).get("name") for call in calls] != [
            step.summary for step in tools
        ]:
            continue
        ids = [call.get("id") for call in calls]
        if not all(isinstance(call_id, str) and call_id for call_id in ids) or len(set(ids)) != len(
            ids
        ):
            continue
        results = [message for message in block if message.get("role") == "tool"]
        if len(results) != len(ids) or {result.get("tool_call_id") for result in results} != set(
            ids
        ):
            continue
        if any(
            step.metadata.get("tool_call_id", call_id) != call_id
            for step, call_id in zip(tools, ids, strict=True)
        ):
            continue
        exact_ids = all(
            step.metadata.get("tool_call_id") == call_id
            for step, call_id in zip(tools, ids, strict=True)
        )
        refs = run.receipt.evidence_refs if run.receipt else ()
        anchored = any(
            ref in {f"run:{run.id}:tool:{call_id}", f"run:{run.id}:approval:{call_id}"}
            for ref in refs
            for call_id in ids
        )
        if not exact_ids and not anchored:
            continue
        candidates.append((calls, {result["tool_call_id"]: result for result in results}))
    # Repeated prompts/tool sequences cannot identify a historical run reliably.
    if len(candidates) != 1:
        return run
    calls, results = candidates[0]
    restored = {}
    for step, call in zip(tools, calls, strict=True):
        if step.detail or step.metadata.get("output_recorded"):
            continue
        content = results[call["id"]].get("content")
        if not isinstance(content, str):
            continue
        restored[step.sequence] = replace(
            step,
            detail=public_tool_text(content),
            metadata={
                **step.metadata,
                "tool_call_id": call["id"],
                "input": public_tool_text(call.get("function", {}).get("arguments")),
                "output_recorded": True,
                "output_truncated": len(content) > OUTPUT_PREVIEW_LIMIT,
                "recovered_from": "session_tool_message",
            },
        )
    return replace(run, steps=[restored.get(step.sequence, step) for step in run.steps])
