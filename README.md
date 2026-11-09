# AGY Computer Use Engine

This package provides a small asynchronous CDP client for browser tabs selected by target metadata. It uses a loopback debugging endpoint, direct DOM inspection for ordinary controls, and serialized CDP input scoped to the chosen page target. It never invokes OS-level input APIs.

## Install and inspect

Use Python 3.11 or newer. Install `requirements.txt`, then start a Chromium-based browser with a loopback-only remote debugging port using the browser's supported launcher. Keep the port bound to loopback and use a dedicated browser profile when possible.

Run `python -m core.engine --endpoint http://127.0.0.1:9222` to list discoverable page targets. This command performs no page actions. A caller must select a target, connect with `BrowserEngine.connect_target`, and pass that session to `BrowserSkills`.

## Skills

`BrowserSkills` provides accessibility tree retrieval, visible interactive element snapshots, selector-based CDP clicks, focused text insertion, passive response metadata observation, and opt-in paused response interception. Intercepted responses must be continued with `continue_response`; otherwise the page request remains paused. The engine does not rewrite response data. App-specific model APIs or MCP integrations should be preferred when available.

## Git sync

`daemon_sync.py --dry-run` validates Python syntax, the manifest entrypoint, whitespace, and changed files for common credential patterns without modifying Git state. `daemon_sync.py --once` runs one guarded sync cycle. With no mode flag, the daemon repeats every 16 minutes, stages changes, runs the same checks, commits, and pushes `main` to `origin`. It refuses other branches, scans candidate files before staging, uses a single-instance lock, and retries unpushed local commits on later cycles. Keep the daemon attached to a dedicated checkout so it cannot publish unrelated workspace files.

See `constitution.md` for operational boundaries and `manifest.json` for declared capabilities.
