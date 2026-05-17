"""SoM-based browser tools replacing the old CSS-selector tools.

Ported from browser-debugging-daemon/scripts/mcp_server.js tool handlers.

Each tool wraps a BrowserRuntime method and returns structured text for the LLM.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from naumi_agent.tools.base import Tool

from .runtime.browser_runtime import BrowserRuntime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_url(url: Any) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return "url must be a non-empty string."
    url = url.strip()
    if not url.startswith(("http://", "https://", "file://")):
        return f"Invalid URL scheme: {url[:30]}"
    return None


def _validate_positive_int(value: Any, field: str) -> str | None:
    if not isinstance(value, (int, float)) or value < 0:
        return f"{field} must be a positive integer."
    return None


def _validate_string(value: Any, field: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return f"{field} must be a non-empty string."
    return None


def _validate_enum(
    value: Any, field: str, allowed: list[str]
) -> str | None:
    if value not in allowed:
        return (
            f"{field} must be one of {allowed}, got: {value!r}"
        )
    return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_goto_result(result: dict[str, Any]) -> str:
    parts: list[str] = [
        f"Successfully navigated to {result.get('url', 'unknown')}"
    ]
    metadata = result.get("metadata")
    if metadata:
        parts.append(
            f"# Page Metadata\n{json.dumps(metadata, indent=2)}"
        )
    tree = result.get("accessibilityTree")
    if tree:
        parts.append(f"# Accessibility Tree (YAML)\n{tree}")
    content = result.get("pageContent")
    if content and isinstance(content, dict) and content:
        parts.append(
            f"# Page Content\n{json.dumps(content, indent=2)}"
        )
    elements = result.get("elements", [])
    parts.append(
        f"# Interactive Elements (SoM)\n"
        f"{json.dumps(elements, indent=2)}"
    )
    tabs = result.get("tabs")
    if tabs and len(tabs) > 1:
        parts.append(f"# Open Tabs\n{json.dumps(tabs, indent=2)}")
    captcha = result.get("captchaChallenge")
    if captcha:
        parts.append(
            f"# CAPTCHA Detected\n"
            f"{json.dumps(captcha, indent=2)}"
        )
    return "\n\n".join(parts)


def _format_observe_result(result: dict[str, Any]) -> str:
    parts: list[str] = [
        f"Observation complete. Screenshot: {result.get('screenshotPath', 'N/A')}"
    ]
    tree = result.get("accessibilityTree")
    if tree:
        parts.append(f"# Accessibility Tree (YAML)\n{tree}")
    content = result.get("pageContent")
    if content and isinstance(content, dict) and content:
        parts.append(
            f"# Page Content\n{json.dumps(content, indent=2)}"
        )
    errors = result.get("recentErrors")
    if errors:
        parts.append(
            f"# Recent Console Errors\n"
            f"{json.dumps(errors, indent=2)}"
        )
    elements = result.get("elements", [])
    parts.append(
        f"# Interactive Elements (SoM)\n"
        f"{json.dumps(elements, indent=2)}"
    )
    tabs = result.get("tabs")
    if tabs and len(tabs) > 1:
        parts.append(f"# Open Tabs\n{json.dumps(tabs, indent=2)}")
    captcha = result.get("captchaChallenge")
    if captcha:
        parts.append(
            f"# CAPTCHA Detected\n"
            f"{json.dumps(captcha, indent=2)}"
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Tool classes
# ---------------------------------------------------------------------------


class BrowserGotoTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_goto"

    @property
    def description(self) -> str:
        return "Navigate the persistent browser to a specified URL."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full URL to navigate to",
                },
            },
            "required": ["url"],
        }

    async def execute(self, **kwargs: Any) -> str:
        url = kwargs.get("url")
        err = _validate_url(url)
        if err:
            return f"Error: {err}"
        try:
            result = await self._runtime.goto(str(url))
            return _format_goto_result(result)
        except Exception as exc:
            return f"Error navigating to {url}: {exc}"


class BrowserObserveTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_observe"

    @property
    def description(self) -> str:
        return (
            "Analyze the current page with full observability. "
            "Returns SoM interactive elements, Accessibility Tree, "
            "page content, recent errors, and screenshot. "
            "ALWAYS call this before interacting with the page."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await self._runtime.observe()
            return _format_observe_result(result)
        except Exception as exc:
            return f"Error observing page: {exc}"


class BrowserClickTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_click"

    @property
    def description(self) -> str:
        return (
            "Click an element on the page based on the ID "
            "returned by browser_observe."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "id": {
                    "type": "number",
                    "description": (
                        "The ID of the Set-of-Mark element"
                    ),
                },
            },
            "required": ["id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        som_id = kwargs.get("id")
        err = _validate_positive_int(som_id, "id")
        if err:
            return f"Error: {err}"
        try:
            result = await self._runtime.click(int(som_id))
            tag = result.get("target", {}).get("tag", "?")
            return (
                f"Successfully clicked element {som_id} "
                f"[{tag}]"
            )
        except Exception as exc:
            return f"Error clicking element {som_id}: {exc}"


class BrowserTypeTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_type"

    @property
    def description(self) -> str:
        return (
            "Type text into an input element based on the ID "
            "returned by browser_observe. Set submit to true to "
            "press Enter after typing."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "id": {
                    "type": "number",
                    "description": (
                        "The ID of the Input element"
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "The text to type",
                },
                "submit": {
                    "type": "boolean",
                    "description": (
                        "If true, press Enter after typing"
                    ),
                },
            },
            "required": ["id", "text"],
        }

    async def execute(self, **kwargs: Any) -> str:
        som_id = kwargs.get("id")
        text = kwargs.get("text")
        submit = bool(kwargs.get("submit", False))
        id_err = _validate_positive_int(som_id, "id")
        text_err = _validate_string(text, "text")
        if id_err:
            return f"Error: {id_err}"
        if text_err:
            return f"Error: {text_err}"
        try:
            await self._runtime.type_text(
                int(som_id), str(text), submit=submit
            )
            suffix = " and pressed Enter" if submit else ""
            return (
                f'Successfully typed "{text}" into element '
                f"{som_id}{suffix}"
            )
        except Exception as exc:
            return (
                f"Error typing into element {som_id}: {exc}"
            )


class BrowserHoverTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_hover"

    @property
    def description(self) -> str:
        return (
            "Hover over an element on the page based on the ID "
            "returned by browser_observe."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "id": {
                    "type": "number",
                    "description": (
                        "The ID of the Set-of-Mark element"
                    ),
                },
            },
            "required": ["id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        som_id = kwargs.get("id")
        err = _validate_positive_int(som_id, "id")
        if err:
            return f"Error: {err}"
        try:
            result = await self._runtime.hover(int(som_id))
            tag = result.get("target", {}).get("tag", "?")
            return (
                f"Successfully hovered element {som_id} [{tag}]"
            )
        except Exception as exc:
            return f"Error hovering element {som_id}: {exc}"


class BrowserKeypressTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_keypress"

    @property
    def description(self) -> str:
        return (
            "Press a keyboard key such as Enter, Escape, or "
            "ArrowDown."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The key name to press",
                },
            },
            "required": ["key"],
        }

    async def execute(self, **kwargs: Any) -> str:
        key = kwargs.get("key")
        err = _validate_string(key, "key")
        if err:
            return f"Error: {err}"
        try:
            await self._runtime.keypress(str(key))
            return f"Successfully pressed {key}"
        except Exception as exc:
            return f"Error pressing key {key}: {exc}"


class BrowserScrollTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_scroll"

    @property
    def description(self) -> str:
        return "Scroll the page."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["down", "up", "top", "bottom"],
                },
            },
            "required": ["direction"],
        }

    async def execute(self, **kwargs: Any) -> str:
        direction = kwargs.get("direction")
        err = _validate_enum(
            direction, "direction", ["up", "down", "top", "bottom"]
        )
        if err:
            return f"Error: {err}"
        try:
            await self._runtime.scroll(str(direction))
            return f"Scrolled {direction}"
        except Exception as exc:
            return f"Error scrolling {direction}: {exc}"


class BrowserEvaluateTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_evaluate"

    @property
    def description(self) -> str:
        return (
            "Execute JavaScript code in the browser page context "
            "and return the result. Results are serialized (max 8KB)."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "JavaScript code to execute.",
                },
            },
            "required": ["expression"],
        }

    async def execute(self, **kwargs: Any) -> str:
        expression = kwargs.get("expression")
        err = _validate_string(expression, "expression")
        if err:
            return f"Error: {err}"
        try:
            result = await self._runtime.evaluate(str(expression))
            if result.get("isError"):
                return f"Error evaluating: {result['result']}"
            return str(result["result"])
        except Exception as exc:
            return f"Error evaluating expression: {exc}"


class BrowserSelectOptionTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_select_option"

    @property
    def description(self) -> str:
        return (
            "Select one or more options in a dropdown (<select>) "
            "element identified by its SoM ID."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "id": {
                    "type": "number",
                    "description": (
                        "The SoM element ID of the select element."
                    ),
                },
                "values": {
                    "oneOf": [
                        {"type": "string"},
                        {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    ],
                    "description": (
                        "The option value(s) to select."
                    ),
                },
            },
            "required": ["id", "values"],
        }

    async def execute(self, **kwargs: Any) -> str:
        som_id = kwargs.get("id")
        values = kwargs.get("values")
        id_err = _validate_positive_int(som_id, "id")
        if id_err:
            return f"Error: {id_err}"
        if values is None:
            return "Error: values is required."
        if isinstance(values, str):
            values = [values]
        try:
            await self._runtime.select_option(
                int(som_id), values
            )
            return (
                f"Successfully selected {values} in element "
                f"{som_id}"
            )
        except Exception as exc:
            return (
                f"Error selecting option in element {som_id}: "
                f"{exc}"
            )


class BrowserHandleDialogTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_handle_dialog"

    @property
    def description(self) -> str:
        return (
            "Accept or dismiss a browser dialog (alert, confirm, "
            "prompt). For prompt dialogs, you can provide text."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["accept", "dismiss"],
                    "description": (
                        "Whether to accept or dismiss the dialog."
                    ),
                    "default": "accept",
                },
                "promptText": {
                    "type": "string",
                    "description": (
                        "Text to enter in a prompt dialog."
                    ),
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action", "accept")
        prompt_text = kwargs.get("promptText", "")
        try:
            result = await self._runtime.handle_dialog(
                action=action, prompt_text=prompt_text
            )
            if not result.get("handled"):
                return result.get("message", "No dialog appeared.")
            info = result.get("dialogInfo", {})
            return (
                f"Dialog {action}ed: [{info.get('type')}] "
                f"{info.get('message', '')}"
            )
        except Exception as exc:
            return f"Error handling dialog: {exc}"


class BrowserNavigateBackTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_navigate_back"

    @property
    def description(self) -> str:
        return (
            "Go back to the previous page in browser history."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await self._runtime.go_back()
            return f"Navigated back to {result.get('url', 'unknown')}"
        except Exception as exc:
            return f"Error navigating back: {exc}"


class BrowserTabsTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_tabs"

    @property
    def description(self) -> str:
        return (
            "Manage browser tabs: list all open tabs, create a "
            "new tab, switch to a tab by index, or close a tab."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "new", "close", "select"],
                    "description": "Tab action to perform.",
                },
                "index": {
                    "type": "number",
                    "description": (
                        "Tab index for close or select actions."
                    ),
                },
                "url": {
                    "type": "string",
                    "description": (
                        "URL to navigate to when creating a new tab."
                    ),
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action")
        err = _validate_enum(
            action, "action", ["list", "new", "close", "select"]
        )
        if err:
            return f"Error: {err}"
        try:
            result = await self._runtime.tab_action(
                str(action),
                index=kwargs.get("index"),
                url=kwargs.get("url"),
            )
            return json.dumps(result, indent=2, default=str)
        except Exception as exc:
            return f"Error in tab action {action}: {exc}"


class BrowserSwitchTabTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_switch_tab"

    @property
    def description(self) -> str:
        return (
            "Switch to a different browser tab by index. Use "
            "after browser_observe/browser_goto when the required "
            "page is not the active tab."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "index": {
                    "type": "number",
                    "description": (
                        "Zero-based tab index from the tabs list"
                    ),
                },
            },
            "required": ["index"],
        }

    async def execute(self, **kwargs: Any) -> str:
        index = kwargs.get("index")
        err = _validate_positive_int(index, "index")
        if err:
            return f"Error: {err}"
        try:
            result = await self._runtime.tab_action(
                "select", index=int(index)
            )
            return (
                f"Switched to tab {result.get('active')}: "
                f"{result.get('url', 'unknown')}"
            )
        except Exception as exc:
            return f"Error switching tab: {exc}"


class BrowserWaitForTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_waitFor"

    @property
    def description(self) -> str:
        return (
            "Wait for a specific condition on the page before "
            "proceeding. Supports waiting for text to appear, "
            "text to disappear, or a CSS selector to become "
            "visible."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "Wait until this text appears."
                    ),
                },
                "textGone": {
                    "type": "string",
                    "description": (
                        "Wait until this text disappears."
                    ),
                },
                "selector": {
                    "type": "string",
                    "description": (
                        "Wait until a CSS selector matches "
                        "a visible element."
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        "Maximum wait time in ms "
                        "(default 30000, max 300000)."
                    ),
                    "default": 30000,
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        if not any(
            kwargs.get(k)
            for k in ("text", "textGone", "selector")
        ):
            return (
                "Error: waitFor requires at least one of: "
                "text, textGone, selector."
            )
        try:
            result = await self._runtime.wait_for(
                text=kwargs.get("text"),
                text_gone=kwargs.get("textGone"),
                selector=kwargs.get("selector"),
                timeout=int(kwargs.get("timeout", 30000)),
            )
            return json.dumps(result, indent=2, default=str)
        except Exception as exc:
            return f"Error waiting: {exc}"


class BrowserUploadTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_upload"

    @property
    def description(self) -> str:
        return (
            "Upload files to a file input element on the page "
            "based on the ID from browser_observe. Supports "
            "local file paths and/or base64-encoded files."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "id": {
                    "type": "number",
                    "description": (
                        "The ID of the file input element."
                    ),
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Absolute file paths on the local "
                        "filesystem."
                    ),
                },
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "mimeType": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["name", "content"],
                    },
                    "description": (
                        "Base64-encoded file objects."
                    ),
                },
            },
            "required": ["id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        som_id = kwargs.get("id")
        err = _validate_positive_int(som_id, "id")
        if err:
            return f"Error: {err}"
        try:
            result = await self._runtime.upload(
                int(som_id),
                paths=kwargs.get("paths", []),
                files=kwargs.get("files", []),
            )
            count = result.get("fileCount", 0)
            return (
                f"Uploaded {count} file(s) to element {som_id}"
            )
        except Exception as exc:
            return f"Error uploading to element {som_id}: {exc}"


class BrowserDragTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_drag"

    @property
    def description(self) -> str:
        return (
            "Drag a page element onto another element. Both "
            "identified by their Set-of-Mark IDs."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "fromId": {
                    "type": "number",
                    "description": "ID of the element to drag.",
                },
                "toId": {
                    "type": "number",
                    "description": "ID of the drop zone element.",
                },
            },
            "required": ["fromId", "toId"],
        }

    async def execute(self, **kwargs: Any) -> str:
        from_id = kwargs.get("fromId")
        to_id = kwargs.get("toId")
        err_from = _validate_positive_int(from_id, "fromId")
        err_to = _validate_positive_int(to_id, "toId")
        if err_from:
            return f"Error: {err_from}"
        if err_to:
            return f"Error: {err_to}"
        try:
            await self._runtime.drag(int(from_id), int(to_id))
            return (
                f"Dragged element {from_id} onto element {to_id}"
            )
        except Exception as exc:
            return (
                f"Error dragging {from_id} to {to_id}: {exc}"
            )


class BrowserDragFileTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_drag_file"

    @property
    def description(self) -> str:
        return (
            "Drag local files onto a page drop zone element. "
            "Supports local file paths and/or base64-encoded "
            "file content."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "toId": {
                    "type": "number",
                    "description": (
                        "ID of the drop zone element."
                    ),
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Local file paths.",
                },
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "mimeType": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["name", "content"],
                    },
                    "description": (
                        "Base64-encoded file objects."
                    ),
                },
            },
            "required": ["toId"],
        }

    async def execute(self, **kwargs: Any) -> str:
        to_id = kwargs.get("toId")
        err = _validate_positive_int(to_id, "toId")
        if err:
            return f"Error: {err}"
        try:
            result = await self._runtime.drag_file(
                paths=kwargs.get("paths", []),
                files=kwargs.get("files", []),
                to_id=int(to_id),
            )
            count = result.get("fileCount", 0)
            return (
                f"Dropped {count} file(s) onto element {to_id}"
            )
        except Exception as exc:
            return f"Error dragging files: {exc}"


class BrowserScreenshotTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_screenshot"

    @property
    def description(self) -> str:
        return (
            "Take a screenshot of the current page and return "
            "it as a base64-encoded PNG."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        try:
            b64 = await self._runtime.screenshot_base64()
            return (
                f"Screenshot captured ({len(b64)} chars base64)\n"
                f"[data:image/png;base64,{b64[:100]}...]"
            )
        except Exception as exc:
            return f"Error taking screenshot: {exc}"


class BrowserDebugStateTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_debug_state"

    @property
    def description(self) -> str:
        return (
            "Return recent console, network, error, and "
            "artifact state for the active or last browser "
            "session."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "number",
                    "description": (
                        "How many recent events to include"
                    ),
                    "default": 20,
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        limit = int(kwargs.get("limit", 20))
        try:
            state = self._runtime.get_debug_state(limit)
            return json.dumps(state, indent=2, default=str)
        except Exception as exc:
            return f"Error getting debug state: {exc}"


class BrowserTextLayoutAuditTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_text_layout_audit"

    @property
    def description(self) -> str:
        return (
            "Audit visible UI text for overflow risk using "
            "canvas-based line estimation and actual overflow "
            "checks."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "number",
                    "description": (
                        "Max candidate elements to inspect"
                    ),
                    "default": 80,
                },
                "selectors": {
                    "type": "string",
                    "description": (
                        "Optional CSS selector list for "
                        "text-bearing elements."
                    ),
                },
                "overflow_threshold": {
                    "type": "number",
                    "description": (
                        "Pixel threshold before overflow "
                        "is flagged."
                    ),
                    "default": 1,
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await self._runtime.audit_text_layout({
                "limit": kwargs.get("limit"),
                "selectors": kwargs.get("selectors"),
                "overflowThreshold": kwargs.get(
                    "overflow_threshold"
                ),
            })
            return json.dumps(result, indent=2, default=str)
        except Exception as exc:
            return f"Error auditing text layout: {exc}"


class BrowserCdpHealthTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_cdp_health"

    @property
    def description(self) -> str:
        return (
            "Check whether the target Chrome remote debugging "
            "endpoint is reachable."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "cdp_endpoint": {
                    "type": "string",
                    "description": (
                        "CDP endpoint such as "
                        "http://127.0.0.1:9222"
                    ),
                },
                "timeout_ms": {
                    "type": "number",
                    "description": (
                        "Health-check timeout in ms"
                    ),
                    "default": 3000,
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await self._runtime.get_cdp_health({
                "endpoint": kwargs.get("cdp_endpoint"),
                "timeoutMs": kwargs.get("timeout_ms"),
            })
            return json.dumps(result, indent=2, default=str)
        except Exception as exc:
            return f"Error checking CDP health: {exc}"


class BrowserAttachDiagnosticsTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_attach_diagnostics"

    @property
    def description(self) -> str:
        return (
            "Run one-shot diagnostics for attaching to an "
            "existing Chrome session and return remediation "
            "hints."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "cdp_endpoint": {
                    "type": "string",
                    "description": (
                        "CDP endpoint such as "
                        "http://127.0.0.1:9222"
                    ),
                },
                "timeout_ms": {
                    "type": "number",
                    "description": (
                        "Diagnostics timeout in ms"
                    ),
                    "default": 3000,
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await self._runtime.get_cdp_diagnostics({
                "endpoint": kwargs.get("cdp_endpoint"),
                "timeoutMs": kwargs.get("timeout_ms"),
            })
            return json.dumps(result, indent=2, default=str)
        except Exception as exc:
            return f"Error running diagnostics: {exc}"


class BrowserChromeLauncherInfoTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_chrome_launcher_info"

    @property
    def description(self) -> str:
        return (
            "Get Chrome auto-launch diagnostics: binary path, "
            "profile directories, process info."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        try:
            info = self._runtime.get_chrome_launcher_info()
            return json.dumps(info, indent=2, default=str)
        except Exception as exc:
            return f"Error getting launcher info: {exc}"


class BrowserStartTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_start"

    @property
    def description(self) -> str:
        return (
            "Start a browser session. Supports managed "
            "(headless/headful), attached (CDP), and auto "
            "(try attached then fallback) modes."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["auto", "managed", "attached"],
                    "description": (
                        "Browser source strategy. auto prefers "
                        "attached Chrome and falls back to "
                        "managed runtime."
                    ),
                },
                "cdp_endpoint": {
                    "type": "string",
                    "description": (
                        "Optional CDP endpoint for attached mode."
                    ),
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        options: dict[str, Any] = {}
        if kwargs.get("source"):
            options["source"] = kwargs["source"]
        if kwargs.get("cdp_endpoint"):
            options["cdpEndpoint"] = kwargs["cdp_endpoint"]
        try:
            result = await self._runtime.start(options)
            return json.dumps(result, indent=2, default=str)
        except Exception as exc:
            return f"Error starting browser: {exc}"


class BrowserStopTool(Tool):
    def __init__(self, runtime: BrowserRuntime) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "browser_stop"

    @property
    def description(self) -> str:
        return (
            "Stop the browser and save debug trace recording. "
            "Use when the testing task is complete."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await self._runtime.stop()
            return json.dumps(result, indent=2, default=str)
        except Exception as exc:
            return f"Error stopping browser: {exc}"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_BROWSER_TOOL_CLASSES: list[type[Tool]] = [
    BrowserGotoTool,
    BrowserObserveTool,
    BrowserClickTool,
    BrowserTypeTool,
    BrowserHoverTool,
    BrowserKeypressTool,
    BrowserScrollTool,
    BrowserEvaluateTool,
    BrowserSelectOptionTool,
    BrowserHandleDialogTool,
    BrowserNavigateBackTool,
    BrowserTabsTool,
    BrowserSwitchTabTool,
    BrowserWaitForTool,
    BrowserUploadTool,
    BrowserDragTool,
    BrowserDragFileTool,
    BrowserScreenshotTool,
    BrowserDebugStateTool,
    BrowserTextLayoutAuditTool,
    BrowserCdpHealthTool,
    BrowserAttachDiagnosticsTool,
    BrowserChromeLauncherInfoTool,
    BrowserStartTool,
    BrowserStopTool,
]


def create_browser_tools(
    runtime: BrowserRuntime,
) -> list[Tool]:
    """Create all SoM-based browser tools backed by *runtime*."""
    return [cls(runtime) for cls in _BROWSER_TOOL_CLASSES]
