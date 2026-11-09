"""Asynchronous, target-scoped browser engine using Playwright over CDP.

Architecture:
- Primary transport: Playwright async `connect_over_cdp`.
- Targeted fallback: Per-page `CDPSession` for canvas input and Fetch interception.
- Isolation: Attaches only to explicitly selected existing Chromium tabs.
- Preservation: Never closes pages or contexts; cleanly detaches sessions on exit.
- Serialization: Each tab gets an `asyncio.Lock` serializing all inputs and actions.

Limitations:
- No guaranteed unthrottled background execution. Chromium background tabs remain
  subject to timer throttling, lifecycle suspension, and OS resource constraints.
- No DOM access inside WebGL or canvas pixel buffers; canvas interaction requires
  coordinate-based inputs via the per-page CDPSession fallback.
- Raw CDP is not primary; Playwright async API is the primary engine interface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
import urllib.error
import urllib.request

from playwright.async_api import (
    Browser,
    CDPSession,
    Page,
    Playwright,
    async_playwright,
)

LOG = logging.getLogger("agy_engine")


class EngineError(RuntimeError):
    """Base exception for browser engine failures."""


class CDPError(EngineError):
    """A protocol, transport, or browser-side command failure."""


class TargetNotFoundError(EngineError):
    """Raised when an explicitly requested target tab cannot be found."""


@dataclass(frozen=True, slots=True)
class Target:
    target_id: str
    type: str
    title: str
    url: str
    websocket_url: str = ""


class TabSession:
    """An attached, explicitly selected existing Chromium tab.

    Serializes all interactions through a dedicated lock and manages an
    optional per-page CDPSession for fallback operations (canvas, Fetch).
    Never closes the underlying Page or BrowserContext upon detachment.
    """

    def __init__(
        self,
        page: Page,
        target_id: str,
        title: str,
        url: str,
        engine: BrowserEngine,
    ) -> None:
        self.page = page
        self.target_id = target_id
        self.title = title
        self.url = url
        self.engine = engine
        self.lock = asyncio.Lock()
        self._cdp_session: CDPSession | None = None
        self._attached: bool = True
        self._registered_skills: set[Any] = set()
        self._cleanup_callbacks: list[Any] = []

    def register_skills(self, skills: Any) -> None:
        """Register a BrowserSkills instance with this tab for lifecycle management."""
        self._registered_skills.add(skills)

    def unregister_skills(self, skills: Any) -> None:
        """Unregister a BrowserSkills instance."""
        self._registered_skills.discard(skills)

    def register_cleanup(self, callback: Any) -> None:
        """Register an arbitrary async cleanup callback."""
        self._cleanup_callbacks.append(callback)

    def unregister_cleanup(self, callback: Any) -> None:
        """Unregister a cleanup callback."""
        if callback in self._cleanup_callbacks:
            self._cleanup_callbacks.remove(callback)

    @property
    def is_attached(self) -> bool:
        return self._attached

    async def get_cdp_session(self) -> CDPSession:
        """Lazily initialize and return a per-page CDPSession fallback."""
        if not self._attached:
            raise EngineError(f"Tab {self.target_id} is detached")
        if self._cdp_session is None:
            self._cdp_session = await self.page.context.new_cdp_session(self.page)
        return self._cdp_session

    async def detach(self) -> None:
        """Detach this tab session and its CDPSession cleanly.

        Shuts down any active response interception and removes passive observers
        on registered skills instances before detaching CDPSession.
        Never calls page.close() or context.close(). The tab remains alive in
        the user's browser.
        """
        for skills in list(self._registered_skills):
            try:
                if hasattr(skills, "close"):
                    await skills.close()
            except Exception as exc:
                LOG.debug("Error closing skills on tab %s detach: %s", self.target_id, exc)
        self._registered_skills.clear()

        for cb in list(self._cleanup_callbacks):
            try:
                res = cb()
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                LOG.debug("Error executing cleanup callback on tab %s detach: %s", self.target_id, exc)
        self._cleanup_callbacks.clear()

        if self._cdp_session is not None:
            try:
                await self._cdp_session.detach()
            except Exception as exc:
                LOG.debug("Error detaching CDPSession for tab %s: %s", self.target_id, exc)
            self._cdp_session = None
        self._attached = False


class BrowserEngine:
    """Connect to a local browser over CDP and control selected page tabs.

    Uses Playwright async `connect_over_cdp` as the primary engine. Only
    existing tabs may be attached; new tabs/contexts are never created,
    and user tabs/contexts are never closed.
    """

    def __init__(self, endpoint: str = "http://127.0.0.1:9222", *, timeout: float = 15.0) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme not in {"http", "https"} or parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("CDP endpoint must use HTTP(S) on loopback (127.0.0.1, localhost, or ::1)")
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._attached_tabs: dict[str, TabSession] = {}

    def _http_json(self, path: str) -> Any:
        request = urllib.request.Request(f"{self.endpoint}{path}", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise CDPError(f"Could not read CDP endpoint {path}: {exc}") from exc

    async def list_targets(self) -> list[Target]:
        """Discover existing page targets over loopback without modifying browser state."""
        rows = await asyncio.to_thread(self._http_json, "/json/list")
        return [
            Target(
                target_id=row["id"],
                type=row.get("type", ""),
                title=row.get("title", ""),
                url=row.get("url", ""),
                websocket_url=row.get("webSocketDebuggerUrl", ""),
            )
            for row in rows
            if row.get("type") == "page"
        ]

    async def connect(self) -> None:
        """Connect Playwright to the remote Chromium browser via CDP."""
        if self._browser is not None:
            return
        parts = urlsplit(self.endpoint)
        if parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Refusing connection to non-loopback endpoint")
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(
                self.endpoint,
                timeout=self.timeout * 1000,
            )
        except Exception as exc:
            await self.disconnect()
            raise CDPError(f"Failed to connect to browser over CDP at {self.endpoint}: {exc}") from exc

    async def attach_target(self, target: Target | str) -> TabSession:
        """Attach to an explicitly selected existing Chromium tab.

        Never opens a new tab or context. Raises TargetNotFoundError if no
        matching existing tab is found.
        """
        await self.connect()
        assert self._browser is not None

        if isinstance(target, str) and (target.startswith("http://") or target.startswith("https://")):
            raise ValueError(
                f"Attachment by URL string {target!r} is rejected to prevent ambiguous target selection. "
                "Pass a Target from list_targets() or an explicit target_id."
            )

        target_id = target.target_id if isinstance(target, Target) else str(target)
        existing = self._attached_tabs.get(target_id)
        if existing and existing.is_attached:
            return existing

        matched_page: Page | None = None
        resolved_target_id = target_id
        resolved_title = ""
        resolved_url = ""

        for context in self._browser.contexts:
            for page in context.pages:
                temp_cdp: CDPSession | None = None
                p_target_id = ""
                try:
                    temp_cdp = await context.new_cdp_session(page)
                    info = await temp_cdp.send("Target.getTargetInfo")
                    p_target_id = info.get("targetInfo", {}).get("targetId", "")
                except Exception as exc:
                    LOG.debug("Could not retrieve targetInfo for page: %s", exc)
                finally:
                    if temp_cdp is not None:
                        try:
                            await temp_cdp.detach()
                        except Exception:
                            pass

                if p_target_id == target_id:
                    matched_page = page
                    resolved_target_id = p_target_id
                    try:
                        resolved_title = await page.title()
                    except Exception:
                        resolved_title = ""
                    resolved_url = page.url
                    break
            if matched_page is not None:
                break

        if matched_page is None:
            raise TargetNotFoundError(
                f"No existing Chromium tab matches target ID {target_id!r}. "
                "The engine only attaches to explicitly selected existing tabs and never opens new ones."
            )

        tab = TabSession(
            page=matched_page,
            target_id=resolved_target_id,
            title=resolved_title,
            url=resolved_url,
            engine=self,
        )
        self._attached_tabs[resolved_target_id] = tab
        return tab

    async def connect_target(self, target: Target) -> TabSession:
        """Compatibility alias for attach_target."""
        return await self.attach_target(target)

    async def detach_target(self, tab: TabSession) -> None:
        """Detach a specific tab session without closing the page or context."""
        await tab.detach()
        self._attached_tabs.pop(tab.target_id, None)

    async def evaluate(self, tab: TabSession, expression: str, *, arg: Any = None) -> Any:
        """Evaluate JavaScript inside the tab's main frame using Playwright."""
        if not tab.is_attached:
            raise EngineError(f"Tab {tab.target_id} is detached")
        async with tab.lock:
            try:
                return await tab.page.evaluate(expression, arg)
            except Exception as exc:
                raise CDPError(f"Evaluation failed: {exc}") from exc

    async def disconnect(self) -> None:
        """Cleanly detach all sessions and disconnect Playwright.

        Never closes user pages or browser contexts.
        """
        for tab in list(self._attached_tabs.values()):
            await tab.detach()
        self._attached_tabs.clear()

        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:
                LOG.debug("Error closing browser CDP connection: %s", exc)
            self._browser = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                LOG.debug("Error stopping Playwright: %s", exc)
            self._playwright = None

    async def close(self) -> None:
        """Alias for disconnect."""
        await self.disconnect()

    async def __aenter__(self) -> BrowserEngine:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.disconnect()


async def _main(endpoint: str) -> None:
    engine = BrowserEngine(endpoint)
    targets = await engine.list_targets()
    for target in targets:
        print(json.dumps({"id": target.target_id, "title": target.title, "url": target.url}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:9222", help="Loopback browser debugging endpoint")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(_main(args.endpoint))
    except (EngineError, ValueError) as exc:
        parser.exit(2, f"Engine error: {exc}\n")


if __name__ == "__main__":
    main()
