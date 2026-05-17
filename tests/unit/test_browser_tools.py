"""Tests for SoM-based browser tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from naumi_agent.tools.browser.tools import (
    BrowserAttachDiagnosticsTool,
    BrowserCdpHealthTool,
    BrowserChromeLauncherInfoTool,
    BrowserClickTool,
    BrowserDebugStateTool,
    BrowserDragFileTool,
    BrowserDragTool,
    BrowserEvaluateTool,
    BrowserGotoTool,
    BrowserHandleDialogTool,
    BrowserHoverTool,
    BrowserKeypressTool,
    BrowserNavigateBackTool,
    BrowserObserveTool,
    BrowserScreenshotTool,
    BrowserScrollTool,
    BrowserSelectOptionTool,
    BrowserStartTool,
    BrowserStopTool,
    BrowserSwitchTabTool,
    BrowserTabsTool,
    BrowserTextLayoutAuditTool,
    BrowserTypeTool,
    BrowserUploadTool,
    BrowserWaitForTool,
    _validate_enum,
    _validate_positive_int,
    _validate_string,
    _validate_url,
    create_browser_tools,
)


def _make_runtime() -> AsyncMock:
    rt = AsyncMock()
    rt.goto.return_value = {
        "url": "https://example.com",
        "metadata": {"url": "https://example.com", "title": "Example"},
        "elements": [{"id": 1, "tag": "a", "text": "Link"}],
        "accessibilityTree": "- heading \"Welcome\"",
        "pageContent": {"headings": ["Welcome"]},
        "screenshotPath": "/tmp/shot.png",
        "captchaChallenge": None,
    }
    rt.observe.return_value = {
        "elements": [{"id": 1, "tag": "button", "text": "Click"}],
        "accessibilityTree": "- button \"Click\"",
        "pageContent": {},
        "screenshotPath": "/tmp/obs.png",
        "recentErrors": None,
        "tabs": [{"index": 0, "url": "https://example.com", "active": True}],
        "captchaChallenge": None,
    }
    rt.click.return_value = {
        "id": 1,
        "target": {"tag": "button", "text": "Click", "x": 100, "y": 200},
        "screenshotPath": "/tmp/click.png",
    }
    rt.type_text.return_value = {
        "id": 2,
        "target": {"tag": "input", "x": 50, "y": 50},
        "text": "hello",
        "submit": False,
        "screenshotPath": "/tmp/type.png",
    }
    rt.hover.return_value = {
        "id": 1,
        "target": {"tag": "a", "x": 10, "y": 20},
        "screenshotPath": "/tmp/hover.png",
    }
    rt.keypress.return_value = {"key": "Enter", "screenshotPath": "/tmp/key.png"}
    rt.scroll.return_value = {"direction": "down", "screenshotPath": "/tmp/scroll.png"}
    rt.evaluate.return_value = {"result": "42", "isError": False}
    rt.select_option.return_value = {
        "id": 3,
        "values": ["opt1"],
        "target": {"tag": "select", "x": 0, "y": 0},
        "screenshotPath": "/tmp/sel.png",
    }
    rt.handle_dialog.return_value = {
        "handled": True,
        "action": "accept",
        "dialogInfo": {"type": "alert", "message": "Hi"},
        "screenshotPath": "/tmp/dlg.png",
    }
    rt.go_back.return_value = {"url": "https://example.com", "screenshotPath": "/tmp/back.png"}
    rt.tab_action.return_value = {"active": 0, "url": "https://example.com"}
    rt.wait_for.return_value = {
        "matched": "text",
        "waitedMs": 500,
        "condition": "text",
    }
    rt.upload.return_value = {
        "id": 4,
        "target": {"tag": "input", "type": "file", "x": 0, "y": 0},
        "fileCount": 2,
        "screenshotPath": "/tmp/up.png",
    }
    rt.drag.return_value = {
        "fromId": 1,
        "toId": 2,
        "from": {"x": 0, "y": 0},
        "to": {"x": 100, "y": 100},
        "screenshotPath": "/tmp/drag.png",
    }
    rt.drag_file.return_value = {
        "toId": 3,
        "target": {"tag": "div", "x": 50, "y": 50},
        "fileCount": 1,
        "screenshotPath": "/tmp/df.png",
    }
    rt.screenshot_base64 = AsyncMock(return_value="aGVsbG8=")
    rt.get_debug_state = MagicMock(
        return_value={"active": True, "browserMode": "headless"}
    )
    rt.audit_text_layout.return_value = {
        "summary": {"hasIssues": False, "flaggedElements": 0},
        "flaggedElements": [],
    }
    rt.get_cdp_health.return_value = {"ok": True, "endpoint": "http://127.0.0.1:9222"}
    rt.get_cdp_diagnostics.return_value = {"ok": True, "hints": [], "warnings": []}
    rt.get_chrome_launcher_info = MagicMock(
        return_value={"platform": "darwin"}
    )
    rt.start.return_value = {"alreadyRunning": False, "browserMode": "headless"}
    rt.stop.return_value = {"alreadyStopped": False}
    return rt


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


class TestValidation:
    def test_validate_url_valid(self) -> None:
        assert _validate_url("https://example.com") is None

    def test_validate_url_invalid_scheme(self) -> None:
        assert _validate_url("ftp://example.com") is not None

    def test_validate_url_empty(self) -> None:
        assert _validate_url("") is not None
        assert _validate_url(None) is not None

    def test_validate_positive_int_valid(self) -> None:
        assert _validate_positive_int(5, "id") is None

    def test_validate_positive_int_negative(self) -> None:
        assert _validate_positive_int(-1, "id") is not None

    def test_validate_positive_int_string(self) -> None:
        assert _validate_positive_int("abc", "id") is not None

    def test_validate_string_valid(self) -> None:
        assert _validate_string("hello", "text") is None

    def test_validate_string_empty(self) -> None:
        assert _validate_string("", "text") is not None
        assert _validate_string(None, "text") is not None

    def test_validate_enum_valid(self) -> None:
        assert _validate_enum("down", "dir", ["up", "down"]) is None

    def test_validate_enum_invalid(self) -> None:
        assert _validate_enum("sideways", "dir", ["up", "down"]) is not None


# ---------------------------------------------------------------------------
# Tool tests (with mocked runtime)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_goto() -> None:
    rt = _make_runtime()
    tool = BrowserGotoTool(rt)
    result = await tool.execute(url="https://example.com")
    assert "Successfully navigated" in result
    assert "Interactive Elements" in result


@pytest.mark.asyncio
async def test_browser_goto_invalid_url() -> None:
    rt = _make_runtime()
    tool = BrowserGotoTool(rt)
    result = await tool.execute(url="not-a-url")
    assert "Error" in result


@pytest.mark.asyncio
async def test_browser_observe() -> None:
    rt = _make_runtime()
    tool = BrowserObserveTool(rt)
    result = await tool.execute()
    assert "Observation complete" in result
    assert "Interactive Elements" in result


@pytest.mark.asyncio
async def test_browser_click() -> None:
    rt = _make_runtime()
    tool = BrowserClickTool(rt)
    result = await tool.execute(id=1)
    assert "Successfully clicked element 1" in result


@pytest.mark.asyncio
async def test_browser_click_invalid_id() -> None:
    rt = _make_runtime()
    tool = BrowserClickTool(rt)
    result = await tool.execute(id=-1)
    assert "Error" in result


@pytest.mark.asyncio
async def test_browser_type() -> None:
    rt = _make_runtime()
    tool = BrowserTypeTool(rt)
    result = await tool.execute(id=2, text="hello")
    assert 'Successfully typed "hello" into element 2' in result


@pytest.mark.asyncio
async def test_browser_type_with_submit() -> None:
    rt = _make_runtime()
    tool = BrowserTypeTool(rt)
    result = await tool.execute(id=2, text="hello", submit=True)
    assert "pressed Enter" in result


@pytest.mark.asyncio
async def test_browser_type_missing_text() -> None:
    rt = _make_runtime()
    tool = BrowserTypeTool(rt)
    result = await tool.execute(id=2)
    assert "Error" in result


@pytest.mark.asyncio
async def test_browser_hover() -> None:
    rt = _make_runtime()
    tool = BrowserHoverTool(rt)
    result = await tool.execute(id=1)
    assert "Successfully hovered element 1" in result


@pytest.mark.asyncio
async def test_browser_keypress() -> None:
    rt = _make_runtime()
    tool = BrowserKeypressTool(rt)
    result = await tool.execute(key="Enter")
    assert "Successfully pressed Enter" in result


@pytest.mark.asyncio
async def test_browser_keypress_invalid() -> None:
    rt = _make_runtime()
    tool = BrowserKeypressTool(rt)
    result = await tool.execute(key="")
    assert "Error" in result


@pytest.mark.asyncio
async def test_browser_scroll() -> None:
    rt = _make_runtime()
    tool = BrowserScrollTool(rt)
    result = await tool.execute(direction="down")
    assert "Scrolled down" in result


@pytest.mark.asyncio
async def test_browser_scroll_invalid_direction() -> None:
    rt = _make_runtime()
    tool = BrowserScrollTool(rt)
    result = await tool.execute(direction="sideways")
    assert "Error" in result


@pytest.mark.asyncio
async def test_browser_evaluate() -> None:
    rt = _make_runtime()
    tool = BrowserEvaluateTool(rt)
    result = await tool.execute(expression="1+1")
    assert "42" in result


@pytest.mark.asyncio
async def test_browser_evaluate_error() -> None:
    rt = _make_runtime()
    rt.evaluate.return_value = {"result": "boom", "isError": True}
    tool = BrowserEvaluateTool(rt)
    result = await tool.execute(expression="bad()")
    assert "Error evaluating" in result


@pytest.mark.asyncio
async def test_browser_select_option() -> None:
    rt = _make_runtime()
    tool = BrowserSelectOptionTool(rt)
    result = await tool.execute(id=3, values="opt1")
    assert "Successfully selected" in result


@pytest.mark.asyncio
async def test_browser_select_option_array() -> None:
    rt = _make_runtime()
    tool = BrowserSelectOptionTool(rt)
    result = await tool.execute(id=3, values=["a", "b"])
    assert "Successfully selected" in result


@pytest.mark.asyncio
async def test_browser_handle_dialog_accept() -> None:
    rt = _make_runtime()
    tool = BrowserHandleDialogTool(rt)
    result = await tool.execute(action="accept")
    assert "accept" in result


@pytest.mark.asyncio
async def test_browser_handle_dialog_no_dialog() -> None:
    rt = _make_runtime()
    rt.handle_dialog.return_value = {
        "handled": False,
        "message": "No dialog appeared within 3 seconds.",
    }
    tool = BrowserHandleDialogTool(rt)
    result = await tool.execute(action="dismiss")
    assert "No dialog appeared" in result


@pytest.mark.asyncio
async def test_browser_navigate_back() -> None:
    rt = _make_runtime()
    tool = BrowserNavigateBackTool(rt)
    result = await tool.execute()
    assert "Navigated back to" in result


@pytest.mark.asyncio
async def test_browser_tabs() -> None:
    rt = _make_runtime()
    tool = BrowserTabsTool(rt)
    result = await tool.execute(action="list")
    data = json.loads(result)
    assert "active" in data


@pytest.mark.asyncio
async def test_browser_switch_tab() -> None:
    rt = _make_runtime()
    tool = BrowserSwitchTabTool(rt)
    result = await tool.execute(index=0)
    assert "Switched to tab" in result


@pytest.mark.asyncio
async def test_browser_wait_for() -> None:
    rt = _make_runtime()
    tool = BrowserWaitForTool(rt)
    result = await tool.execute(text="Welcome")
    data = json.loads(result)
    assert data["matched"] == "text"


@pytest.mark.asyncio
async def test_browser_wait_for_no_condition() -> None:
    rt = _make_runtime()
    tool = BrowserWaitForTool(rt)
    result = await tool.execute()
    assert "Error" in result


@pytest.mark.asyncio
async def test_browser_upload() -> None:
    rt = _make_runtime()
    tool = BrowserUploadTool(rt)
    result = await tool.execute(id=4, paths=["/tmp/file.txt"])
    assert "Uploaded 2 file(s)" in result


@pytest.mark.asyncio
async def test_browser_drag() -> None:
    rt = _make_runtime()
    tool = BrowserDragTool(rt)
    result = await tool.execute(fromId=1, toId=2)
    assert "Dragged element 1 onto element 2" in result


@pytest.mark.asyncio
async def test_browser_drag_file() -> None:
    rt = _make_runtime()
    tool = BrowserDragFileTool(rt)
    result = await tool.execute(toId=3, paths=["/tmp/file.txt"])
    assert "Dropped 1 file(s)" in result


@pytest.mark.asyncio
async def test_browser_screenshot() -> None:
    rt = _make_runtime()
    tool = BrowserScreenshotTool(rt)
    result = await tool.execute()
    assert "Screenshot captured" in result


@pytest.mark.asyncio
async def test_browser_debug_state() -> None:
    rt = _make_runtime()
    tool = BrowserDebugStateTool(rt)
    result = await tool.execute()
    data = json.loads(result)
    assert data["active"] is True


@pytest.mark.asyncio
async def test_browser_text_layout_audit() -> None:
    rt = _make_runtime()
    tool = BrowserTextLayoutAuditTool(rt)
    result = await tool.execute()
    data = json.loads(result)
    assert "summary" in data


@pytest.mark.asyncio
async def test_browser_cdp_health() -> None:
    rt = _make_runtime()
    tool = BrowserCdpHealthTool(rt)
    result = await tool.execute()
    data = json.loads(result)
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_browser_attach_diagnostics() -> None:
    rt = _make_runtime()
    tool = BrowserAttachDiagnosticsTool(rt)
    result = await tool.execute()
    data = json.loads(result)
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_browser_chrome_launcher_info() -> None:
    rt = _make_runtime()
    tool = BrowserChromeLauncherInfoTool(rt)
    result = await tool.execute()
    data = json.loads(result)
    assert "platform" in data


@pytest.mark.asyncio
async def test_browser_start() -> None:
    rt = _make_runtime()
    tool = BrowserStartTool(rt)
    result = await tool.execute(source="managed")
    data = json.loads(result)
    assert data["browserMode"] == "headless"


@pytest.mark.asyncio
async def test_browser_stop() -> None:
    rt = _make_runtime()
    tool = BrowserStopTool(rt)
    result = await tool.execute()
    data = json.loads(result)
    assert data["alreadyStopped"] is False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestFactory:
    def test_create_browser_tools_count(self) -> None:
        rt = _make_runtime()
        tools = create_browser_tools(rt)
        assert len(tools) == 25

    def test_all_tools_have_unique_names(self) -> None:
        rt = _make_runtime()
        tools = create_browser_tools(rt)
        names = [t.name for t in tools]
        assert len(names) == len(set(names))

    def test_all_tools_have_required_properties(self) -> None:
        rt = _make_runtime()
        tools = create_browser_tools(rt)
        for t in tools:
            assert t.name
            assert t.description
            assert "type" in t.parameters_schema
            assert "properties" in t.parameters_schema
            schema = t.to_openai_tool()
            assert schema["type"] == "function"
            assert schema["function"]["name"] == t.name
