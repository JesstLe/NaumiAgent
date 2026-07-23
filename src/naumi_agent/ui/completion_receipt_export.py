"""Cross-surface export of one authoritative completion receipt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from naumi_agent.clipboard import CopyResult, copy_or_save_transcript
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.ui.completion_receipt import (
    format_completion_receipt_text,
    sanitize_completion_receipt_inline,
)

_MAX_SELECTOR_CHARS = 500


class CompletionReceiptExportError(RuntimeError):
    """A bounded user-facing failure while resolving or exporting a receipt."""


@dataclass(frozen=True, slots=True)
class CompletionReceiptExportResult:
    receipt_id: str
    run_id: str
    text: str
    harness_included: bool
    harness_warning: str
    copy_result: CopyResult


async def copy_completion_receipt(
    engine: Any,
    selector: str = "latest",
) -> CompletionReceiptExportResult:
    """Resolve one current-session receipt, save it, and copy it when possible."""
    normalized_selector = str(selector or "latest").strip() or "latest"
    if len(normalized_selector) > _MAX_SELECTOR_CHARS:
        raise CompletionReceiptExportError("完成回执 ID 过长，无法查询。")

    session = getattr(engine, "_session", None)
    session_id = str(getattr(session, "id", "") or "").strip()
    if not session_id:
        raise CompletionReceiptExportError(
            "当前没有活动会话；请先发送消息或用 /resume 恢复会话。"
        )

    store = getattr(engine, "chat_run_store", None)
    if store is None:
        raise CompletionReceiptExportError("当前运行时未提供完成回执存储。")

    try:
        receipt = await _resolve_receipt(
            store,
            session_id=session_id,
            selector=normalized_selector,
        )
    except CompletionReceiptExportError:
        raise
    except Exception as exc:
        raise CompletionReceiptExportError(
            "完成回执存储暂时不可用；请运行 /doctor 后重试。"
        ) from exc

    harness_receipt, harness_warning = await _resolve_harness_receipt(
        engine,
        session_id=session_id,
        receipt=receipt,
    )
    text = _format_export_text(
        receipt,
        harness_receipt=harness_receipt,
        harness_warning=harness_warning,
    )

    runtime_data_dir = getattr(engine, "_runtime_data_dir", None)
    if runtime_data_dir is None:
        raise CompletionReceiptExportError("当前运行时未提供安全的回执导出目录。")
    try:
        runtime_root = Path(runtime_data_dir).expanduser().resolve()
        copied = copy_or_save_transcript(
            text,
            base_dir=runtime_root,
            prefix="completion-receipt",
            content_label="完成回执",
        )
    except (OSError, RuntimeError) as exc:
        raise CompletionReceiptExportError(
            "完成回执无法写入 Naumi 状态目录；请检查目录权限后重试。"
        ) from exc

    return CompletionReceiptExportResult(
        receipt_id=receipt.receipt_id,
        run_id=receipt.run_id,
        text=text,
        harness_included=harness_receipt is not None,
        harness_warning=harness_warning,
        copy_result=copied,
    )


async def _resolve_receipt(
    store: Any,
    *,
    session_id: str,
    selector: str,
) -> CompletionReceipt:
    if selector.lower() == "latest":
        list_runs = getattr(store, "list_runs", None)
        if not callable(list_runs):
            raise CompletionReceiptExportError("当前回执存储不支持查询最近回执。")
        runs = await list_runs(session_id, limit=200)
        receipt = next(
            (
                candidate
                for run in runs
                if (candidate := getattr(run, "receipt", None)) is not None
            ),
            None,
        )
        if receipt is None:
            raise CompletionReceiptExportError("当前会话还没有可复制的完成回执。")
        return CompletionReceipt.from_dict(receipt.to_dict())

    get_receipt = getattr(store, "get_receipt", None)
    if not callable(get_receipt):
        raise CompletionReceiptExportError("当前回执存储不支持按 ID 查询。")
    receipt = await get_receipt(session_id, selector)
    if receipt is None:
        raise CompletionReceiptExportError(
            f"完成回执不存在或不属于当前会话: {selector}"
        )
    return CompletionReceipt.from_dict(receipt.to_dict())


async def _resolve_harness_receipt(
    engine: Any,
    *,
    session_id: str,
    receipt: CompletionReceipt,
) -> tuple[dict[str, Any] | None, str]:
    store = getattr(engine, "_harness_store", None)
    get_run = getattr(store, "get_run", None)
    if not callable(get_run):
        return None, ""
    try:
        stored = await get_run(receipt.run_id)
    except Exception:
        return None, "Harness 伴随回执暂不可用，已复制通用完成回执。"
    if stored is None or getattr(stored, "receipt", None) is None:
        return None, ""

    stored_session_id = str(getattr(stored, "session_id", "") or "").strip()
    stored_workspace = str(getattr(stored, "workspace_root", "") or "").strip()
    try:
        workspace = (
            Path(getattr(engine, "workspace_root", Path.cwd()))
            .expanduser()
            .resolve()
        )
    except (OSError, RuntimeError):
        return None, "Harness 伴随回执暂不可用，已复制通用完成回执。"
    if stored_session_id != session_id or not stored_workspace:
        return None, ""
    try:
        if Path(stored_workspace).expanduser().resolve() != workspace:
            return None, ""
    except (OSError, RuntimeError):
        return None, ""

    try:
        harness_receipt = stored.receipt.model_dump(mode="json")
        if not isinstance(harness_receipt, dict):
            raise TypeError("Harness receipt projection must be an object")
    except Exception:
        return None, "Harness 伴随回执暂不可用，已复制通用完成回执。"
    return dict(harness_receipt), ""


def _format_export_text(
    receipt: CompletionReceipt,
    *,
    harness_receipt: dict[str, Any] | None,
    harness_warning: str,
) -> str:
    duration = (
        f"{receipt.duration_ms / 1000:.1f}s"
        if receipt.duration_ms >= 1_000
        else f"{receipt.duration_ms}ms"
    )
    lines = [
        "NaumiAgent 完成回执",
        f"回执 ID: {sanitize_completion_receipt_inline(receipt.receipt_id)}",
        f"运行 ID: {sanitize_completion_receipt_inline(receipt.run_id)}",
        f"耗时: {duration}",
        "",
        format_completion_receipt_text(receipt, harness_receipt).plain,
    ]
    if harness_warning:
        lines.extend(["", harness_warning])
    return "\n".join(lines).strip() + "\n"


__all__ = [
    "CompletionReceiptExportError",
    "CompletionReceiptExportResult",
    "copy_completion_receipt",
]
