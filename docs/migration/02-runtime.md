# Phase 2: Browser Runtime Layer

## Source Files
- `scripts/runtime/BrowserRuntime.js` (2565 lines)
- `scripts/runtime/ArtifactStore.js` (202 lines)
- `scripts/runtime/ChromeLauncher.js` (326 lines)
- `scripts/runtime/NetworkRecorder.js` (163 lines)
- `scripts/runtime/DownloadManager.js` (115 lines)
- `scripts/tooling/Logger.js` (52 lines)

## Objective

Port the full browser runtime infrastructure: lifecycle management, artifact storage, Chrome launching, network recording, download handling.

## Files to Create

```
src/naumi_agent/tools/browser/
├── runtime/
│   ├── __init__.py
│   ├── browser_runtime.py   # BrowserRuntime class
│   ├── artifact_store.py    # ArtifactStore class
│   ├── chrome_launcher.py   # ChromeLauncher class
│   ├── network_recorder.py  # NetworkRecorder class
│   └── download_manager.py  # DownloadManager class
```

## Classes to Port

### 1. `ArtifactStore`

Session-based artifact management:
- `start_session()` — creates timestamped session dir with screenshots/, videos/, traces/ subdirs
- `cleanup_retained_sessions()` — enforce `BROWSER_ARTIFACT_MAX_SESSIONS` and `BROWSER_ARTIFACT_MAX_AGE_DAYS`
- `get_step_screenshot_path(label)`, `get_video_path(label)`, `get_trace_path(label)`
- `get_current_view_path()`, `get_console_log_path()`, `get_network_log_path()`, `get_error_log_path()`
- `append_event(type, payload)` — JSONL event log
- `write_json(filename, data)`, `write_text(filename, text)`
- `get_summary()` — returns all paths and file lists
- Port sanitize_segment helper for safe filenames

### 2. `ChromeLauncher`

Auto-launch Chrome with remote debugging:
- `ensure_ready(force_resync=False)` — checks if Chrome is already running on CDP port, launches if not
- `kill_chrome()` — kills the launched Chrome process
- `get_debug_info()` — returns port, PID, endpoint info
- Parse CDP endpoint URL for port number
- Use `subprocess.Popen` to launch Chrome with `--remote-debugging-port`

### 3. `NetworkRecorder`

Record network events from Playwright context:
- `attach(context)` — listen to requestfinished, requestfailed events
- `detach()` — stop listening
- `clear()` — reset recorded events
- Capture request method, URL, resource type, status, request body (non-GET, truncated 4KB)
- Capture response body for document/xhr/fetch types (truncated 4KB)

### 4. `DownloadManager`

Handle file downloads:
- `attach(page)` — listen to download events
- `detach()` — stop listening
- Save downloads to artifacts dir

### 5. `BrowserRuntime`

The main runtime class. This is the largest piece (~2500 lines). Key methods:

#### Lifecycle
- `start(options)` — managed/attached/auto modes with full fallback chain
- `launch_browser_session(headless, navigate_to_url, event_type)`
- `attach_browser_session(endpoint, event_type)`
- `stop()` — save state, finalize trace, flush logs, cleanup
- `cleanup_broken_session()`
- `switch_browser_mode(target_mode, reason)`
- `enter_manual_control()`, `exit_manual_control()`

#### Page Actions (all must port)
- `goto(url)` — navigate, observe, screenshot, collect elements, aria snapshot, captcha detect
- `observe()` — collect elements, screenshot, aria snapshot, page content, tabs, captcha detect
- `click(id)`, `type(id, text, submit)`, `hover(id)` — SoM-based, with resolve_target + action highlight
- `evaluate(expression)` — run JS in page context
- `select_option(id, values)` — dropdown selection
- `handle_dialog(action, prompt_text)` — accept/dismiss browser dialogs
- `go_back()` — navigate back
- `tab_action(action, index, url)` — list/new/close/select tabs
- `keypress(key)`, `scroll(direction)` — keyboard and scroll
- `wait_for(text, text_gone, selector, timeout)` — semantic wait with placeholder/aria/value checking
- `upload(id, paths, files)` — file upload with base64 support
- `drag(from_id, to_id)` — element-to-element drag with stepwise movement
- `drag_file(paths, files, to_id)` — file drag onto page drop zone

#### Visual Feedback (Action Highlight System)
- `show_action_highlight(id, action_type, detail, step)` — inject CSS beam animation overlay + action bar
- `clear_action_highlight(delay_ms)` — fade out and remove
- `show_browser_active_border()`, `hide_browser_active_border()` — animated border showing agent is active
- `_install_active_border_init_script()` — auto-inject on new pages

Port ALL CSS animations:
- `ACTION_HIGHLIGHT_STYLES` (beam wrap, bloom, fallback highlight, action bar)
- `BROWSER_ACTIVE_BORDER_STYLES` (frame wrapper, beam stroke, inner glow, bloom, badge)
- CSS `@property` custom properties for smooth rotation animation

#### Diagnostics
- `get_cdp_health(endpoint, timeout_ms)` — check CDP endpoint health
- `get_cdp_diagnostics(endpoint, timeout_ms)` — detailed diagnostics with hints
- `get_debug_state(limit)` — console/network/errors + capabilities
- `get_page_metadata(text_limit)` — title, URL, text preview
- `screenshot_base64()` — screenshot as base64 PNG
- `audit_text_layout(options)` — text overflow detection with grapheme-aware line estimation

#### Video/Trace
- `start_attached_screencast()` — CDP screencast + ffmpeg encoding for attached mode
- `stop_attached_screencast(reason, persist)` — finalize screencast to webm
- `finalize_trace_segment(label)` — stop Playwright tracing
- `detect_ffmpeg_availability()` — check ffmpeg binary
- `encode_screencast_frames_to_webm(input_pattern, output_path, fps)` — ffmpeg subprocess

#### Observers
- `attach_page_observers()` — console messages, page errors, network events (with trim_log_buffer at 200)
- `flush_logs_to_artifacts()` — write console/network/errors JSON files

## Environment Variables to Support

```
BROWSER_STORAGE_STATE_SECRET  — AES-256-GCM encryption key
CHROME_REMOTE_DEBUG_URL       — CDP endpoint (default http://127.0.0.1:9222)
BROWSER_ARTIFACT_MAX_SESSIONS — max artifact sessions
BROWSER_ARTIFACT_MAX_AGE_DAYS — artifact retention
BROWSER_ATTACHED_SCREENCAST_MAX_PENDING_WRITES — screencast backpressure
```

## Testing

- `tests/unit/test_artifact_store.py` — session creation, cleanup, file paths
- `tests/unit/test_chrome_launcher.py` — CDP health check, Chrome launch/kill
- `tests/unit/test_network_recorder.py` — event capture, body truncation
- `tests/unit/test_browser_runtime.py` — start/stop lifecycle, page actions with real Playwright

## Checklist

- [ ] All 5 classes ported
- [ ] BrowserRuntime supports managed/attached/auto modes
- [ ] All page actions (goto, click, type, hover, evaluate, select_option, handle_dialog, go_back, tabs, keypress, scroll, wait_for, upload, drag, drag_file) working
- [ ] Action highlight CSS animations working
- [ ] Active border CSS animations working
- [ ] CDP health check and diagnostics
- [ ] Text layout audit with grapheme-aware line estimation
- [ ] Attached screencast + ffmpeg video encoding
- [ ] Artifact store with retention controls
- [ ] All unit tests passing
- [ ] `ruff check` clean
