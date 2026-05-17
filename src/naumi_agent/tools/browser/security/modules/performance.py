# ruff: noqa: E501
"""Performance analysis module (Core Web Vitals)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def analyze_performance(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Analyze page performance including navigation timing and Core Web Vitals."""
    url = page.url
    findings: list[dict[str, Any]] = []

    nav_timing, resources, web_vitals = await page.evaluate(
        """async () => {
            const navTiming = (() => {
                const nav = performance.getEntriesByType("navigation")[0];
                if (!nav) return null;
                return {
                    dns: nav.domainLookupEnd - nav.domainLookupStart,
                    tcp: nav.connectEnd - nav.connectStart,
                    tls: nav.secureConnectionStart > 0 ? nav.connectEnd - nav.secureConnectionStart : 0,
                    ttfb: nav.responseStart - nav.requestStart,
                    download: nav.responseEnd - nav.responseStart,
                    domParsing: nav.domInteractive - nav.responseEnd,
                    domComplete: nav.domComplete - nav.responseEnd,
                    loadEvent: nav.loadEventEnd - nav.loadEventStart,
                    total: nav.loadEventEnd - nav.startTime,
                    transferSize: nav.transferSize,
                    protocol: nav.nextHopProtocol,
                };
            })();

            const resources = performance.getEntriesByType("resource").map((r) => ({
                name: r.name.slice(0, 120),
                type: r.initiatorType,
                duration: Math.round(r.duration),
                size: r.transferSize,
                protocol: r.nextHopProtocol,
            }));

            const webVitals = {};
            try {
                const paintEntries = performance.getEntriesByType("paint");
                for (const entry of paintEntries) {
                    webVitals[entry.name] = Math.round(entry.startTime);
                }
            } catch {}

            try {
                const observer = new PerformanceObserver((list) => {
                    for (const entry of list.getEntries()) {
                        if (entry.entryType === "largest-contentful-paint") {
                            webVitals.LCP = Math.round(entry.startTime);
                        }
                        if (entry.entryType === "layout-shift" && !entry.hadRecentInput) {
                            webVitals.CLS = (webVitals.CLS || 0) + entry.value;
                        }
                        if (entry.entryType === "first-input") {
                            webVitals.FID = Math.round(entry.processingStart - entry.startTime);
                        }
                    }
                });
                observer.observe({ type: "largest-contentful-paint", buffered: true });
                observer.observe({ type: "layout-shift", buffered: true });
                observer.observe({ type: "first-input", buffered: true });
            } catch {}

            await new Promise((r) => setTimeout(r, 500));
            if (webVitals.CLS !== undefined) {
                webVitals.CLS = Math.round(webVitals.CLS * 1000) / 1000;
            }

            return [navTiming, resources, webVitals];
        }"""
    )

    if nav_timing:
        ttfb = nav_timing.get("ttfb", 0)
        if ttfb > 800:
            findings.append(
                add_finding(
                    {
                        "category": "performance",
                        "severity": "medium",
                        "title": "Slow TTFB",
                        "description": f"Time to First Byte is {ttfb}ms (should be < 800ms)",
                        "url": url,
                        "evidence": nav_timing,
                    }
                )
            )

        total = nav_timing.get("total", 0)
        if total > 3000:
            findings.append(
                add_finding(
                    {
                        "category": "performance",
                        "severity": "high",
                        "title": "Slow page load",
                        "description": f"Total page load time is {total}ms (should be < 3000ms)",
                        "url": url,
                        "evidence": nav_timing,
                    }
                )
            )
        elif total > 1000:
            findings.append(
                add_finding(
                    {
                        "category": "performance",
                        "severity": "low",
                        "title": "Page load could be faster",
                        "description": f"Total page load time is {total}ms",
                        "url": url,
                        "evidence": nav_timing,
                    }
                )
            )

        transfer_size = nav_timing.get("transferSize", 0)
        if transfer_size > 3 * 1024 * 1024:
            findings.append(
                add_finding(
                    {
                        "category": "performance",
                        "severity": "medium",
                        "title": "Large page transfer size",
                        "description": f"Page transferred {transfer_size / 1024 / 1024:.1f}MB (should be < 3MB)",
                        "url": url,
                        "evidence": {"transferSize": transfer_size},
                    }
                )
            )

    lcp = web_vitals.get("LCP")
    if lcp and lcp > 2500:
        findings.append(
            add_finding(
                {
                    "category": "performance",
                    "severity": "high",
                    "title": "Poor LCP (Largest Contentful Paint)",
                    "description": f"LCP is {lcp}ms (should be < 2500ms)",
                    "url": url,
                    "evidence": web_vitals,
                }
            )
        )

    fcp = web_vitals.get("first-contentful-paint")
    if fcp is None and web_vitals.get("LCP") is None:
        findings.append(
            add_finding(
                {
                    "category": "performance",
                    "severity": "info",
                    "title": "FCP not captured",
                    "description": "First Contentful Paint not available",
                    "url": url,
                    "evidence": web_vitals,
                }
            )
        )

    cls_val = web_vitals.get("CLS")
    if cls_val is not None and cls_val > 0.1:
        findings.append(
            add_finding(
                {
                    "category": "performance",
                    "severity": "high",
                    "title": "Poor CLS (Cumulative Layout Shift)",
                    "description": f"CLS is {cls_val} (should be < 0.1)",
                    "url": url,
                    "evidence": web_vitals,
                }
            )
        )

    fid = web_vitals.get("FID")
    if fid and fid > 100:
        findings.append(
            add_finding(
                {
                    "category": "performance",
                    "severity": "medium",
                    "title": "Poor FID (First Input Delay)",
                    "description": f"FID is {fid}ms (should be < 100ms)",
                    "url": url,
                    "evidence": web_vitals,
                }
            )
        )

    large_resources = sorted(
        [r for r in resources if r.get("size", 0) > 200 * 1024],
        key=lambda r: r.get("size", 0),
        reverse=True,
    )[:10]

    for res in large_resources:
        findings.append(
            add_finding(
                {
                    "category": "performance",
                    "severity": "medium",
                    "title": "Large resource detected",
                    "description": f"{res['name']} ({res['type']}) is {res['size'] // 1024}KB",
                    "url": url,
                    "evidence": {
                        "name": res["name"],
                        "type": res["type"],
                        "sizeKB": res["size"] // 1024,
                        "duration": res["duration"],
                    },
                }
            )
        )

    slow_resources = sorted(
        [r for r in resources if r.get("duration", 0) > 1000],
        key=lambda r: r.get("duration", 0),
        reverse=True,
    )[:10]

    for res in slow_resources:
        findings.append(
            add_finding(
                {
                    "category": "performance",
                    "severity": "low",
                    "title": "Slow resource",
                    "description": f"{res['name']} ({res['type']}) took {res['duration']}ms",
                    "url": url,
                    "evidence": {
                        "name": res["name"],
                        "type": res["type"],
                        "duration": res["duration"],
                    },
                }
            )
        )

    return {
        "category": "performance",
        "findings": findings,
        "metrics": {
            "navigationTiming": nav_timing,
            "webVitals": web_vitals,
            "resourceCount": len(resources),
            "totalTransferSize": sum(r.get("size", 0) for r in resources),
        },
    }
