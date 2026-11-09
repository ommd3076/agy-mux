# AGY Computer Use Engine

This package provides a small asynchronous CDP client for browser tabs selected by target metadata. It uses a loopback debugging endpoint, direct DOM inspection for ordinary controls, and serialized CDP input scoped to the chosen page target. It never invokes OS-level input APIs.

## Install and inspect

Use Python 3.11 or newer. Install `requirements.txt`, then start a Chromium-based browser with a loopback-only remote debugging port using the browser's supported launcher. Keep the port bound to loopback and use a dedicated browser profile when possible.

Run `python -m core.engine --endpoint http://127.0.0.1:9222` to list discoverable page targets. This command performs no page actions. A caller must select a target, connect with `BrowserEngine.connect_target`, and pass that session to `BrowserSkills`.

## Skills

`BrowserSkills` provides accessibility tree retrieval, visible interactive element snapshots, selector-based CDP clicks, focused text insertion, and opt-in response metadata observation. Network observation records response metadata only; it does not intercept or rewrite traffic. App-specific model APIs or MCP integrations should be preferred when available.

## Git sync

`daemon_sync.py --dry-run` reports the current worktree status. It intentionally has no timer and never stages, commits, or pushes automatically. Review and publish changes with explicit Git commands after validation.

See `constitution.md` for operational boundaries and `manifest.json` for declared capabilities.
