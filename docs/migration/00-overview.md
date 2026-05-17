# Migration: browser-debugging-daemon → NaumiAgent

## Goal

Replace NaumiAgent's built-in browser tools (6 basic CSS-selector-based tools) with the full capabilities of `~/Workspace/browser-debugging-daemon`, a 10k+ LOC Node.js project providing:

- Set-of-Mark (SoM) visual interaction model (numbered overlays instead of CSS selectors)
- Accessibility Tree snapshots (ariaSnapshot)
- Autonomous browser subagent with LLM planning, verification, and CAPTCHA handling
- Queued task runs with state machine, persistence, and recovery
- Human handoff (waiting_for_instruction, manual_control, resume, abort)
- Multi-browser modes (managed headless/headful, attached CDP, auto fallback)
- 25-module security auditor with OSV.dev CVE lookup
- Multi-agent parallel security scanning
- Structured artifacts (screenshots, videos, traces, events, walkthroughs, timelines)
- Template system with assertion rules and run comparison
- Dashboard web UI
- Storage state persistence with AES-256-GCM encryption

## Strategy

Port all JavaScript logic into Python (Playwright async API), keeping the same architecture and feature set. Each phase builds on the previous one and must pass ruff + tests before proceeding.

## Source Reference

| Component | Source (JS) | Lines |
|-----------|-------------|-------|
| SoM + helpers | `scripts/shared.js` | 430 |
| Browser runtime | `scripts/runtime/BrowserRuntime.js` | 2565 |
| Artifact store | `scripts/runtime/ArtifactStore.js` | 202 |
| Chrome launcher | `scripts/runtime/ChromeLauncher.js` | 326 |
| Network recorder | `scripts/runtime/NetworkRecorder.js` | 163 |
| Download manager | `scripts/runtime/DownloadManager.js` | 115 |
| Browser subagent | `scripts/subagent/BrowserSubagent.js` | 748 |
| LLM planner | `scripts/subagent/OpenAIPlanner.js` | 423 |
| Task runner | `scripts/orchestrator/TaskRunner.js` | 825 |
| Task run store | `scripts/orchestrator/TaskRunStore.js` | 93 |
| Run template store | `scripts/orchestrator/RunTemplateStore.js` | 30 |
| Security auditor | `scripts/security/SecurityAuditor.js` | 3085 |
| Agent coordinator | `scripts/security/AgentCoordinator.js` | 324 |
| HTTP daemon | `scripts/daemon.js` | 1571 |
| MCP server | `scripts/mcp_server.js` | 1925 |
| **Total** | | **~12,900** |

## Current NaumiAgent browser.py

6 tools: navigate, screenshot, click, type, extract, get_html — all CSS-selector based, no SoM, no subagent, no artifacts, no security scanning.

## Phase Summary

| Phase | Focus | Target Files | Depends On |
|-------|-------|-------------|------------|
| 1 | SoM + shared helpers | `tools/browser/som.py` | — |
| 2 | Runtime (BrowserRuntime, ArtifactStore, ChromeLauncher, NetworkRecorder, DownloadManager) | `tools/browser/runtime/` | Phase 1 |
| 3 | Agent tools (replace old browser.py) | `tools/browser/tools.py` | Phase 2 |
| 4 | Subagent (BrowserSubagent + Planner) | `tools/browser/subagent/` | Phase 2 |
| 5 | Orchestrator (TaskRunner, stores, templates) | `tools/browser/orchestrator/` | Phase 4 |
| 6 | Security auditor (25 modules) | `tools/browser/security/` | Phase 2 |
| 7 | Engine integration + slash commands | `orchestrator/engine.py`, `main.py`, `cli_completer.py` | Phase 3, 5, 6 |
| 8 | TUI integration | `tui/app.py` | Phase 7 |

Each phase has its own detailed document: `01-som.md` through `08-tui.md`.
