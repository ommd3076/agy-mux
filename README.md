# AGY Computer Use Engine

Target-scoped browser automation engine using Python, powered primarily by Playwright async `connect_over_cdp` with per-page `CDPSession` fallback for canvas coordinate inputs and Fetch interception.

The engine connects to a local Chromium-based browser via loopback, discovers existing tabs, and attaches strictly to explicitly selected tabs. It never creates unrequested tabs, never closes user pages or contexts, and serializes all inputs per tab.

---

## Architectural Principles

1. **Playwright Async as Primary Engine**: High-level page operations, element locators, actionability waiting, clicks, and text input use Playwright async (`connect_over_cdp`). Raw CDP is not primary.
2. **Targeted CDPSession Fallback**: A per-page `CDPSession` is lazily obtained on demand strictly for:
   - Canvas / WebGL coordinate mouse input (`Input.dispatchMouseEvent`).
   - Opt-in response interception (`Fetch.enable` / `Fetch.requestPaused`).
   - Deep accessibility trees (`Accessibility.getFullAXTree`).
3. **Explicit Tab Scope & Preservation**: The engine only attaches to explicitly selected existing Chromium tabs. It never opens new tabs or contexts (`new_page()` / `new_context()`), and it never closes pages or contexts (`page.close()` / `context.close()`). Upon disconnect or detachment, CDPSessions detach cleanly, leaving user tabs alive.
4. **Tab-Scoped Input Serialization**: Every attached tab has a dedicated `asyncio.Lock` serializing all DOM actions, canvas events, text insertions, and evaluations. Parallelization is supported across distinct tabs only.
5. **Bounded Non-Blocking Network Observation**: Passive response metadata observation uses a bounded queue (`BoundedMetricQueue`). When full, it drops the oldest item and increments an overflow metric without stalling network traffic. Sensitive headers (`authorization`, `cookie`, `set-cookie`, `proxy-authorization`, tokens/API keys) are automatically redacted.
6. **Deliberate Protocol Fallback on Continuation**: Opt-in Fetch response interception enforces hard timeouts on paused events. On watchdog timeouts, queue overflow, or teardown, the engine executes a deliberate best-effort protocol fallback (`Fetch.continueResponse` falling back to `Fetch.continueRequest`) and records explicit metrics and errors on failure. The engine does not make an absolute guarantee that network continuations never fail; unreleased requests increment explicit failure metrics.

---

## Operational Capabilities and Limitations

- **No Guaranteed Background Execution**: Background tabs in Chromium are subject to browser timer throttling, background process priorities, and lifecycle state changes (freezing, memory pressure discarding). The engine does not claim guaranteed unthrottled background execution.
- **No WebGL / Canvas DOM Access**: HTML5 `<canvas>` and WebGL render to internal pixel buffers without exposing DOM nodes or accessibility elements for internal 3D/2D objects. Canvas interactions rely on coordinate-based input via the CDPSession fallback.
- **Loopback Enforcement**: The debugging endpoint must be on loopback (`127.0.0.1`, `localhost`, or `::1`). External network debugging endpoints are rejected.
- **Best-Effort Continuation with Failure Metrics**: Response interception uses a timeout watchdog and protocol fallback (`continueResponse` -> `continueRequest`), but does not claim an absolute guarantee that the browser will never stall or fail a continuation. Continuation failures increment explicit failure counters (`continuation_failure_count`).

---

## Installation

Requires Python 3.11 or newer.

```bash
pip install -r requirements.txt
```

Start Chromium or Microsoft Edge with remote debugging enabled on loopback:

```bash
msedge.exe --remote-debugging-port=9222 --user-data-dir="C:\tmp\browser-profile"
```

---

## Quickstart & Usage

### 1. Discover Existing Targets

```bash
python -m core.engine --endpoint http://127.0.0.1:9222
```

Outputs JSON records of discoverable page tabs:

```json
{"id": "404E38FCFBE0636F10586670A923F6AB", "title": "Example Domain", "url": "https://example.com/"}
```

### 2. Connect, Attach, and Automate

```python
import asyncio
from core.engine import BrowserEngine
from core.browser_skills import BrowserSkills

async def main():
    async with BrowserEngine("http://127.0.0.1:9222") as engine:
        # Discover existing page targets
        targets = await engine.list_targets()
        if not targets:
            print("No open page tabs found")
            return

        # Attach ONLY to an explicitly selected existing tab
        tab = await engine.attach_target(targets[0])
        skills = BrowserSkills(tab)

        # Inspect interactive elements
        elements = await skills.interactive_elements()
        for el in elements:
            print(f"[{el.tag}] {el.name} -> {el.selector}")

        # DOM interactions (Playwright primary)
        # Serialized by tab.lock
        await skills.click("button#submit", timeout_ms=5000)
        await skills.type_text("input#search", "Playwright CDP", timeout_ms=5000)

        # Canvas coordinate input (CDPSession fallback)
        await skills.canvas_click(150.0, 200.0)

        # Detach cleanly (tab remains open in browser)
        await engine.detach_target(tab)

asyncio.run(main())
```

---

## Skills Reference

### DOM and Accessibility (Playwright Primary)
- `aria_snapshot()`: Returns the ARIA accessibility snapshot of the page.
- `accessibility_tree(depth=8)`: Returns full accessibility tree via CDPSession with depth control.
- `interactive_elements()`: Returns visible, enabled interactive elements with CSS selectors and bounding boxes.
- `click(selector, timeout_ms=5000)`: Serialized click waiting for actionability.
- `type_text(selector, text, timeout_ms=5000)`: Serialized focus and text insertion.

### Canvas Coordinate Input (CDPSession Fallback)
- `canvas_click(x, y, button="left", click_count=1)`: Dispatches `mousePressed` and `mouseReleased` events to canvas coordinates.
- `canvas_mouse_move(x, y)`: Dispatches `mouseMoved` to coordinate `(x, y)`.
- `canvas_drag(start_x, start_y, end_x, end_y, steps=5)`: Interpolates drag movement between coordinates.

### Passive Network Observation
- `observe_network(buffer_size=128)`: Returns a `BoundedMetricQueue` receiving `{url, status, status_text, headers, ok, request}`. Sensitive headers (`authorization`, `cookie`, `set-cookie`, `proxy-authorization`, API keys/tokens) are redacted to `[REDACTED]`. Drops oldest and increments `queue.overflow_count` on overflow.
- `stop_network_observation(queue)`: Unsubscribes the observer.

### Opt-In Response Interception
- `intercept_responses(buffer_size=64, timeout_s=10.0)`: Pauses HTTP responses for inspection. Enforces hard timeout watchdog; attempts best-effort auto-continuation via protocol fallback (`continueResponse` -> `continueRequest`) on timeout or queue overflow.
- `continue_response(event)`: Releases a paused request/response with deliberate protocol fallback; raises `CDPError` if continuation fails.
- `stop_response_interception(queue)`: Drains and safely attempts continuation for all queued events before disabling Fetch interception.
- `skills.continuation_failure_count`: Counter tracking failed continuation attempts.
- `skills.last_continuation_error`: Contains the most recent continuation failure exception, if any.

---

## Autonomous Cadence & Repository Sync

This repository is explicitly authorized to maintain an autonomous, guarded synchronization cadence via `daemon_sync.py`:

```bash
# Run detached in the background on the authorized 16-minute cadence
python daemon_sync.py --interval-minutes 16

# Validate syntax, entrypoints, requirements, and secrets without modifying git
python daemon_sync.py --dry-run

# Run a single validated sync cycle
python daemon_sync.py --once
```

### Governance and Invariant Safety Controls

1. **Explicit Cadence Objective**: The user has authorized `daemon_sync.py` to run continuously in the background on a 16-minute interval, staging changes, committing validated code to `main`, and pushing to `origin/main`.
2. **Pre-Flight Validation & Secret Scanning**: Every cycle scans all candidate files for credential-like content and sensitive file patterns (`.env`, `credentials.json`, `id_rsa`, `.key`, tokens) before staging. It validates Python AST syntax across all `.py` files, checks `manifest.json` entrypoint and dependencies, verifies `requirements.txt` and `constitution.md`, and rejects git whitespace errors.
3. **Branch and Remote Restriction**: Operates strictly on `main` against `origin`. If upstream commits are detected ahead of local `main`, the daemon refuses implicit rebases or merges and halts safely.
4. **Dedicated Workspace & Staging Scope**: The daemon executes `git add .` strictly under the assumption that this checkout is a dedicated, isolated project workspace. Never place unversioned secrets, personal files, or unrelated user data into this directory.
5. **No Credentials in Logs**: Sensitive parameters, tokens, and credentials are never emitted to logs or commit messages.
6. **Active Cadence Requirement**: The daemon process must remain actually running in the background to sustain the autonomous synchronization loop. Executing a successful one-shot cycle (`--once`) or a dry run (`--dry-run`) validates repository health, but is **not** proof of an active running cadence.

See `constitution.md` for operational governance and `manifest.json` for declared capabilities.
