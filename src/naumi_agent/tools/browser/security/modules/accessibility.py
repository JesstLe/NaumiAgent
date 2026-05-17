# ruff: noqa: E501
"""Accessibility (WCAG) audit module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

SEVERITY_MAP: dict[str, str] = {
    "missing-alt": "medium",
    "missing-label": "high",
    "empty-button": "medium",
    "empty-link": "medium",
    "iframe-no-title": "medium",
    "empty-heading": "low",
    "heading-skip": "low",
    "missing-lang": "high",
    "zoom-disabled": "medium",
    "negative-tabindex": "low",
    "potential-low-contrast": "medium",
    "focusable-hidden": "high",
}

DESC_MAP: dict[str, str] = {
    "missing-alt": "Image missing alt text — screen readers cannot describe this image",
    "missing-label": "Form control missing label — screen readers cannot identify this field",
    "empty-button": "Button has no accessible text content",
    "empty-link": "Link has no accessible text content",
    "iframe-no-title": "iframe missing title attribute",
    "empty-heading": "Heading element is empty",
    "heading-skip": "Heading levels are skipped, breaking document outline",
    "missing-lang": "html element missing lang attribute — screen readers may use wrong language",
    "zoom-disabled": "Viewport zoom is disabled — violates WCAG 1.4.4",
    "negative-tabindex": "Element has negative tabindex — removes from tab order",
    "potential-low-contrast": "Elements with inline color/background may have insufficient contrast",
    "focusable-hidden": "Focusable elements inside aria-hidden — unreachable by keyboard",
}


async def audit_accessibility(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Run WCAG accessibility checks on the current page."""
    url = page.url
    findings: list[dict[str, Any]] = []

    a11y_results = await page.evaluate(
        """() => {
            const issues = [];

            // Missing alt on images
            const images = document.querySelectorAll("img:not([alt])");
            images.forEach((img) => {
                if (img.getAttribute("role") !== "presentation") {
                    issues.push({ type: "missing-alt", tag: "img", src: (img.src || "").slice(0, 80) });
                }
            });

            // Form controls without labels
            const inputs = document.querySelectorAll("input, select, textarea");
            inputs.forEach((el) => {
                if (["hidden", "submit", "button", "reset"].includes(el.type)) return;
                const hasLabel = el.getAttribute("aria-label") || el.getAttribute("aria-labelledby")
                    || (el.id && document.querySelector('label[for="' + el.id + '"]'))
                    || el.closest("label");
                if (!hasLabel) {
                    issues.push({
                        type: "missing-label",
                        tag: el.tagName,
                        name: el.name || el.id || "",
                        inputType: el.type,
                    });
                }
            });

            // Empty buttons
            const buttons = document.querySelectorAll("button, [role='button']");
            buttons.forEach((btn) => {
                const text = (btn.textContent || "").trim();
                const ariaLabel = btn.getAttribute("aria-label");
                const title = btn.getAttribute("title");
                if (!text && !ariaLabel && !title
                    && !btn.querySelector("img[alt], svg, [aria-label]")) {
                    issues.push({ type: "empty-button", tag: btn.tagName });
                }
            });

            // Empty links
            const links = document.querySelectorAll("a[href]");
            links.forEach((link) => {
                const text = (link.textContent || "").trim();
                const ariaLabel = link.getAttribute("aria-label");
                const title = link.getAttribute("title");
                if (!text && !ariaLabel && !title && !link.querySelector("img[alt]")) {
                    issues.push({ type: "empty-link", tag: "a", href: (link.href || "").slice(0, 80) });
                }
            });

            // iframe without title
            const iframes = document.querySelectorAll("iframe");
            iframes.forEach((iframe) => {
                if (!iframe.getAttribute("title")) {
                    issues.push({ type: "iframe-no-title", tag: "iframe", src: (iframe.src || "").slice(0, 80) });
                }
            });

            // Empty headings
            document.querySelectorAll("h1, h2, h3, h4, h5, h6").forEach((el) => {
                if (!(el.textContent || "").trim()) {
                    issues.push({ type: "empty-heading", tag: el.tagName });
                }
            });

            // Heading level skips
            const headings = Array.from(document.querySelectorAll("h1, h2, h3, h4, h5, h6"));
            for (let i = 1; i < headings.length; i++) {
                const prev = Number(headings[i - 1].tagName[1]);
                const curr = Number(headings[i].tagName[1]);
                if (curr > prev + 1) {
                    issues.push({
                        type: "heading-skip",
                        from: "h" + prev,
                        to: "h" + curr,
                        text: (headings[i].textContent || "").slice(0, 40),
                    });
                }
            }

            // Missing lang
            if (!document.documentElement.getAttribute("lang")) {
                issues.push({ type: "missing-lang" });
            }

            // Zoom disabled
            const metaViewport = document.querySelector('meta[name="viewport"]');
            if (metaViewport?.content?.includes("user-scalable=no")
                || metaViewport?.content?.includes("maximum-scale=1")) {
                issues.push({ type: "zoom-disabled" });
            }

            // Negative tabindex
            const tabindexNeg = document.querySelectorAll("[tabindex^='-']:not([tabindex='-1'])");
            tabindexNeg.forEach((el) => {
                issues.push({
                    type: "negative-tabindex",
                    tag: el.tagName,
                    tabindex: el.getAttribute("tabindex"),
                });
            });

            // Potential low contrast
            const colorEls = document.querySelectorAll("[style*='color'], [style*='background']");
            const lowContrast = [];
            colorEls.forEach((el) => {
                const style = el.style;
                if (style.color && style.backgroundColor) {
                    lowContrast.push({ tag: el.tagName, color: style.color, bg: style.backgroundColor });
                }
            });
            if (lowContrast.length > 0) {
                issues.push({ type: "potential-low-contrast", count: lowContrast.length, samples: lowContrast.slice(0, 3) });
            }

            // Focusable elements inside aria-hidden
            const ariaHidden = document.querySelectorAll("[aria-hidden='true']");
            let focusableHidden = 0;
            ariaHidden.forEach((el) => {
                if (el.matches("a[href], button, input, select, textarea, [tabindex]")) focusableHidden++;
            });
            if (focusableHidden > 0) {
                issues.push({ type: "focusable-hidden", count: focusableHidden });
            }

            return issues;
        }"""
    )

    # Group by type
    grouped: dict[str, list[dict[str, Any]]] = {}
    for issue in a11y_results:
        issue_type = issue.get("type", "")
        grouped.setdefault(issue_type, []).append(issue)

    for issue_type, items in grouped.items():
        title_words = issue_type.replace("-", " ").title()
        findings.append(
            add_finding(
                {
                    "category": "accessibility",
                    "severity": SEVERITY_MAP.get(issue_type, "low"),
                    "title": f"{title_words} ({len(items)} found)",
                    "description": DESC_MAP.get(issue_type, f"Accessibility issue: {issue_type}"),
                    "url": url,
                    "evidence": {
                        "type": issue_type,
                        "count": len(items),
                        "samples": items[:5],
                    },
                }
            )
        )

    return {
        "category": "accessibility",
        "findings": findings,
        "stats": {
            "totalIssues": len(a11y_results),
            "typesChecked": len(grouped),
        },
    }
