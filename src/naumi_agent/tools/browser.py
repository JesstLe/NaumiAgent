"""浏览器自动化工具 — 基于 Playwright."""

from __future__ import annotations

import base64
import logging
from typing import Any

from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)


class BrowserSession:
    """管理单个浏览器会话的生命周期."""

    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    async def get_page(self) -> Any:
        if self._page is not None:
            return self._page

        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()
        return self._page

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None


class BrowserNavigateTool(Tool):
    def __init__(self, session: BrowserSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "browser_navigate"

    @property
    def description(self) -> str:
        return "导航到指定 URL 并返回页面基本信息。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要访问的 URL"},
                "wait_until": {
                    "type": "string",
                    "description": "等待条件：load | domcontentloaded | networkidle",
                    "default": "domcontentloaded",
                },
            },
            "required": ["url"],
        }

    async def execute(
        self, *, url: str, wait_until: str = "domcontentloaded", **kwargs: Any
    ) -> str:
        try:
            page = await self._session.get_page()
            response = await page.goto(url, wait_until=wait_until, timeout=30000)
            title = await page.title()

            status = response.status if response else "unknown"
            return f"Navigated to {url}\nStatus: {status}\nTitle: {title}"
        except Exception as e:
            return f"Error navigating to {url}: {type(e).__name__}: {e}"


class BrowserScreenshotTool(Tool):
    def __init__(self, session: BrowserSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "browser_screenshot"

    @property
    def description(self) -> str:
        return "截取当前页面的屏幕截图，返回 base64 编码的 PNG 图片。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "full_page": {
                    "type": "boolean",
                    "description": "是否截取完整页面（默认只截视口）",
                    "default": False,
                },
                "selector": {
                    "type": "string",
                    "description": "CSS 选择器，只截取匹配的元素",
                },
            },
        }

    async def execute(
        self, *, full_page: bool = False, selector: str | None = None, **kwargs: Any
    ) -> str:
        try:
            page = await self._session.get_page()
            if selector:
                element = await page.query_selector(selector)
                if not element:
                    return f"Error: Element not found: {selector}"
                data = await element.screenshot()
            else:
                data = await page.screenshot(full_page=full_page)

            encoded = base64.b64encode(data).decode("ascii")
            preview = encoded[:100]
            return f"Screenshot captured ({len(data)} bytes)\n[data:image/png;base64,{preview}...]"
        except Exception as e:
            return f"Error taking screenshot: {type(e).__name__}: {e}"


class BrowserClickTool(Tool):
    def __init__(self, session: BrowserSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "browser_click"

    @property
    def description(self) -> str:
        return "点击页面上的元素（通过 CSS 选择器定位）。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS 选择器"},
                "button": {
                    "type": "string",
                    "description": "鼠标按钮：left | right | middle",
                    "default": "left",
                },
            },
            "required": ["selector"],
        }

    async def execute(self, *, selector: str, button: str = "left", **kwargs: Any) -> str:
        try:
            page = await self._session.get_page()
            element = await page.wait_for_selector(selector, timeout=5000)
            if not element:
                return f"Error: Element not found: {selector}"
            await element.click(button=button)
            return f"Clicked element: {selector}"
        except Exception as e:
            return f"Error clicking {selector}: {type(e).__name__}: {e}"


class BrowserTypeTool(Tool):
    def __init__(self, session: BrowserSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "browser_type"

    @property
    def description(self) -> str:
        return "在输入框中输入文本（通过 CSS 选择器定位）。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS 选择器（input/textarea）"},
                "text": {"type": "string", "description": "要输入的文本"},
                "press_enter": {
                    "type": "boolean",
                    "description": "输入后是否按回车",
                    "default": False,
                },
            },
            "required": ["selector", "text"],
        }

    async def execute(
        self, *, selector: str, text: str, press_enter: bool = False, **kwargs: Any
    ) -> str:
        try:
            page = await self._session.get_page()
            element = await page.wait_for_selector(selector, timeout=5000)
            if not element:
                return f"Error: Element not found: {selector}"
            await element.fill(text)
            if press_enter:
                await element.press("Enter")
            return f"Typed '{text}' into {selector}"
        except Exception as e:
            return f"Error typing into {selector}: {type(e).__name__}: {e}"


class BrowserExtractTool(Tool):
    def __init__(self, session: BrowserSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "browser_extract"

    @property
    def description(self) -> str:
        return "提取当前页面的文本内容。支持提取整个页面或通过 CSS 选择器提取特定元素。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS 选择器，不填则提取整个页面 body",
                },
                "max_length": {
                    "type": "integer",
                    "description": "最大提取字符数，默认 50000",
                    "default": 50000,
                },
            },
        }

    async def execute(
        self, *, selector: str | None = None, max_length: int = 50000, **kwargs: Any
    ) -> str:
        try:
            page = await self._session.get_page()

            if selector:
                element = await page.query_selector(selector)
                if not element:
                    return f"Error: Element not found: {selector}"
                text = await element.inner_text()
            else:
                text = await page.inner_text("body")

            if len(text) > max_length:
                text = text[:max_length] + f"\n... (truncated, {len(text)} total chars)"

            title = await page.title()
            return f"Page: {title}\n\n{text}"
        except Exception as e:
            return f"Error extracting content: {type(e).__name__}: {e}"


class BrowserGetHtmlTool(Tool):
    def __init__(self, session: BrowserSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "browser_get_html"

    @property
    def description(self) -> str:
        return "获取当前页面的 HTML 源码。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS 选择器，不填则获取整个页面",
                },
                "max_length": {
                    "type": "integer",
                    "description": "最大返回字符数，默认 50000",
                    "default": 50000,
                },
            },
        }

    async def execute(
        self, *, selector: str | None = None, max_length: int = 50000, **kwargs: Any
    ) -> str:
        try:
            page = await self._session.get_page()

            if selector:
                element = await page.query_selector(selector)
                if not element:
                    return f"Error: Element not found: {selector}"
                html = await element.inner_html()
            else:
                html = await page.content()

            if len(html) > max_length:
                html = html[:max_length] + f"\n... (truncated, {len(html)} total chars)"
            return html
        except Exception as e:
            return f"Error getting HTML: {type(e).__name__}: {e}"


def create_browser_tools(session: BrowserSession | None = None) -> list[Tool]:
    """创建浏览器工具集.

    Args:
        session: BrowserSession 实例，不传则创建新的（向后兼容）.
    """
    if session is None:
        session = BrowserSession()
    return [
        BrowserNavigateTool(session),
        BrowserScreenshotTool(session),
        BrowserClickTool(session),
        BrowserTypeTool(session),
        BrowserExtractTool(session),
        BrowserGetHtmlTool(session),
    ]
