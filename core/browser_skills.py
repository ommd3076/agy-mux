"""DOM-aware and canvas-capable browser skills built on a target-scoped tab.

Architecture:
- Primary interface: Playwright async Page, Locators, and Keyboard for DOM interaction.
- Fallback interface: Per-page CDPSession for WebGL/canvas coordinate input and Fetch interception.
- Tab isolation: All actions on the tab are serialized by TabSession.lock.
- Passive network observations: Bounded non-blocking queue, drops oldest and increments
  an overflow metric on queue saturation.
- Response interception: Opt-in with best-effort timeout watchdog, deliberate protocol fallback
  (Fetch.continueResponse to Fetch.continueRequest), and explicit error/metric tracking on failure
  without claiming an absolute guarantee that requests can never remain paused.
- Transparent limits: No DOM access within WebGL pixel buffers; no unthrottled background claims.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, TypeVar

from playwright.async_api import CDPSession, Page, Response

from core.engine import CDPError, EngineError, TabSession, BrowserEngine

LOG = logging.getLogger("agy_skills")
T = TypeVar("T")

REDACTED_HEADER_VALUE = "[REDACTED]"
SENSITIVE_HEADER_NAMES = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
})
SENSITIVE_HEADER_PATTERN = re.compile(
    r"^(?:authorization|cookie|set-cookie|proxy-authorization|.*(?:api[_-]?key|token|secret|credential|auth|bearer).*)$",
    re.IGNORECASE,
)


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Redact sensitive headers (authorization, cookies, tokens, api keys) from observations."""
    if not headers:
        return {}
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        lower_key = key.lower().strip()
        if lower_key in SENSITIVE_HEADER_NAMES or SENSITIVE_HEADER_PATTERN.match(lower_key):
            sanitized[key] = REDACTED_HEADER_VALUE
        else:
            sanitized[key] = value
    return sanitized


class BoundedMetricQueue(asyncio.Queue[T]):
    """A bounded non-blocking queue tracking overflow/dropped metrics on saturation."""

    def __init__(self, maxsize: int = 128) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        super().__init__(maxsize=maxsize)
        self.overflow_count: int = 0
        self.dropped_count: int = 0
        self.continuation_failure_count: int = 0

    def put_dropping_oldest(self, item: T) -> tuple[bool, T | None]:
        """Insert an item into the queue.

        If full, discard the oldest item, increment overflow_count, and insert
        the new item. Returns (was_dropped: bool, dropped_item: T | None).
        """
        dropped_item: T | None = None
        if self.full():
            try:
                dropped_item = self.get_nowait()
                self.overflow_count += 1
                self.dropped_count += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self.put_nowait(item)
        except asyncio.QueueFull:
            self.overflow_count += 1
            self.dropped_count += 1
        return (dropped_item is not None, dropped_item)


@dataclass(frozen=True, slots=True)
class ElementSnapshot:
    tag: str
    role: str
    name: str
    selector: str
    x: float
    y: float
    width: float
    height: float
    enabled: bool


class BrowserSkills:
    """Composable browser operations scoped to an explicitly attached Chromium tab."""

    def __init__(
        self,
        tab_or_engine: TabSession | BrowserEngine,
        tab: TabSession | None = None,
    ) -> None:
        if isinstance(tab_or_engine, TabSession):
            self.tab: TabSession = tab_or_engine
            self.engine: BrowserEngine = tab_or_engine.engine
        elif tab is not None:
            self.engine = tab_or_engine
            self.tab = tab
        else:
            raise ValueError("A valid TabSession must be provided")

        self.page: Page = self.tab.page
        self._network_observers: dict[BoundedMetricQueue[dict[str, Any]], Any] = {}
        self._interception_active: bool = False
        self._interception_queue: BoundedMetricQueue[dict[str, Any]] | None = None
        self._interception_listener: Any = None
        self._pending_timeouts: dict[str, asyncio.Task[None]] = {}
        self._pending_events: dict[str, dict[str, Any]] = {}
        self.continuation_failure_count: int = 0
        self.last_continuation_error: Exception | None = None
        self.tab.register_skills(self)

    def _check_attached(self) -> None:
        """Reject actions if the underlying tab session has been detached."""
        if not self.tab.is_attached:
            raise EngineError(f"Tab {self.tab.target_id} is detached")

    async def close(self) -> None:
        """Shut down any active response interception and remove passive observers."""
        if self._interception_active and self._interception_queue is not None:
            try:
                await self.stop_response_interception(self._interception_queue)
            except Exception as exc:
                LOG.debug("Error stopping response interception on skills close: %s", exc)

        for queue in list(self._network_observers.keys()):
            try:
                await self.stop_network_observation(queue)
            except Exception as exc:
                LOG.debug("Error stopping network observation on skills close: %s", exc)

        try:
            await self._remove_cursor_overlay()
        except Exception as exc:
            LOG.debug("Error removing cursor overlay on skills close: %s", exc)

        self.tab.unregister_skills(self)

    async def __aenter__(self) -> BrowserSkills:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    # -------------------------------------------------------------------------
    # Visual Agent Cursor Overlay Helpers
    # -------------------------------------------------------------------------

    _CURSOR_UPSERT_JS: str = """({ x, y, label }) => {
        let overlay = document.getElementById('agy-agent-cursor');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.id = 'agy-agent-cursor';
            overlay.setAttribute('aria-hidden', 'true');
            overlay.style.cssText = [
                'position: fixed !important',
                'top: 0 !important',
                'left: 0 !important',
                'width: 0 !important',
                'height: 0 !important',
                'overflow: visible !important',
                'z-index: 2147483647 !important',
                'pointer-events: none !important',
                'user-select: none !important',
                'margin: 0 !important',
                'padding: 0 !important',
                'border: none !important',
                'display: block !important',
            ].join('; ');

            const dot = document.createElement('div');
            dot.id = 'agy-agent-cursor-dot';
            dot.style.cssText = [
                'position: absolute !important',
                'left: -5px !important',
                'top: -5px !important',
                'width: 10px !important',
                'height: 10px !important',
                'border-radius: 50% !important',
                'background-color: #2563eb !important',
                'border: 2px solid #ffffff !important',
                'box-shadow: 0 0 4px rgba(0,0,0,0.4) !important',
                'pointer-events: none !important',
            ].join('; ');

            const labelEl = document.createElement('span');
            labelEl.id = 'agy-agent-cursor-label';
            labelEl.style.cssText = [
                'position: absolute !important',
                'left: 8px !important',
                'top: -10px !important',
                'font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important',
                'font-size: 11px !important',
                'font-weight: 600 !important',
                'line-height: 1 !important',
                'color: #ffffff !important',
                'background-color: #1e40af !important',
                'padding: 2px 6px !important',
                'border-radius: 4px !important',
                'box-shadow: 0 1px 3px rgba(0,0,0,0.3) !important',
                'white-space: nowrap !important',
                'pointer-events: none !important',
            ].join('; ');
            labelEl.textContent = label || 'Agent';

            overlay.appendChild(dot);
            overlay.appendChild(labelEl);
            const target = document.body || document.documentElement;
            if (target) {
                target.appendChild(overlay);
            }
        }
        overlay.style.transform = `translate3d(${x}px, ${y}px, 0)`;
        const labelEl = document.getElementById('agy-agent-cursor-label');
        if (labelEl && label) {
            labelEl.textContent = label;
        }
    }"""

    _CURSOR_REMOVE_JS: str = """() => {
        const overlay = document.getElementById('agy-agent-cursor');
        if (overlay) overlay.remove();
    }"""

    async def _create_cursor_overlay(self) -> None:
        """Create the visual #agy-agent-cursor DOM overlay if not present."""
        await self._update_cursor_overlay(-100.0, -100.0, label="Agent")

    async def _update_cursor_overlay(self, x: float, y: float, label: str = "Agent") -> None:
        """Create or update position and label of the #agy-agent-cursor DOM overlay."""
        if not self.tab.is_attached:
            return
        try:
            await self.page.evaluate(self._CURSOR_UPSERT_JS, {"x": x, "y": y, "label": label})
        except Exception as exc:
            LOG.debug("Could not update agent cursor overlay: %s", exc)

    async def _remove_cursor_overlay(self) -> None:
        """Remove the #agy-agent-cursor DOM overlay if present."""
        if not self.tab.is_attached:
            return
        try:
            await self.page.evaluate(self._CURSOR_REMOVE_JS)
        except Exception as exc:
            LOG.debug("Could not remove agent cursor overlay: %s", exc)

    # -------------------------------------------------------------------------
    # DOM and Accessibility Inspection (Playwright Primary)
    # -------------------------------------------------------------------------

    async def aria_snapshot(self) -> str:
        """Retrieve the page ARIA accessibility snapshot using Playwright."""
        self._check_attached()
        async with self.tab.lock:
            return await self.page.locator(":root").aria_snapshot()

    async def accessibility_tree(self, *, depth: int = 8) -> dict[str, Any]:
        """Retrieve full accessibility tree via CDPSession fallback with depth control."""
        self._check_attached()
        if depth < 1 or depth > 32:
            raise ValueError("depth must be between 1 and 32")
        async with self.tab.lock:
            cdp = await self.tab.get_cdp_session()
            await cdp.send("Accessibility.enable")
            return await cdp.send("Accessibility.getFullAXTree", {"depth": depth})

    async def interactive_elements(self) -> list[ElementSnapshot]:
        """Snapshot visible interactive elements with stable selectors and bounds."""
        self._check_attached()
        expression = """(() => {
            const elements = Array.from(document.querySelectorAll(
                'button, a, input, textarea, select, [role="button"], [role="link"], [contenteditable="true"]'
            ));
            return elements.map(el => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                const tag = el.tagName.toLowerCase();
                const role = el.getAttribute('role') || '';
                const name = (el.innerText || el.getAttribute('aria-label') || el.placeholder || el.value || '').trim().slice(0, 160);
                let selector = '';
                if (el.id) {
                    selector = '#' + CSS.escape(el.id);
                } else if (el.getAttribute('data-testid')) {
                    selector = '[data-testid="' + CSS.escape(el.getAttribute('data-testid')) + '"]';
                } else {
                    const sameTagSiblings = Array.from(el.parentElement?.children || []).filter(x => x.tagName === el.tagName);
                    const index = sameTagSiblings.indexOf(el) + 1;
                    selector = tag + ':nth-of-type(' + index + ')';
                }
                const visible = r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                const enabled = !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                return {
                    tag: tag,
                    role: role,
                    name: name,
                    selector: selector,
                    x: r.x + r.width / 2,
                    y: r.y + r.height / 2,
                    width: r.width,
                    height: r.height,
                    enabled: enabled,
                    visible: visible
                };
            }).filter(item => item.visible).map(({visible, ...rest}) => rest);
        })()"""
        async with self.tab.lock:
            raw = await self.page.evaluate(expression)
            return [ElementSnapshot(**item) for item in (raw or [])]

    # -------------------------------------------------------------------------
    # DOM Interaction (Playwright Primary)
    # -------------------------------------------------------------------------

    async def click(self, selector: str, *, timeout_ms: int = 5000) -> None:
        """Click an element using Playwright locator actionability checks."""
        self._check_attached()
        if not selector.strip():
            raise ValueError("selector must not be empty")
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        async with self.tab.lock:
            locator = self.page.locator(selector)
            try:
                box = await locator.bounding_box(timeout=min(timeout_ms, 1000))
                if box:
                    center_x = box["x"] + box["width"] / 2.0
                    center_y = box["y"] + box["height"] / 2.0
                    await self._update_cursor_overlay(center_x, center_y, label="Agent: Click")
            except Exception:
                pass
            try:
                await locator.click(timeout=timeout_ms)
            except Exception as exc:
                raise EngineError(f"Cannot click selector {selector!r}: {exc}") from exc

    async def type_text(self, selector: str, text: str, *, timeout_ms: int = 5000) -> None:
        """Focus an element and insert text without clearing previous content."""
        self._check_attached()
        if not selector.strip():
            raise ValueError("selector must not be empty")
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        async with self.tab.lock:
            locator = self.page.locator(selector)
            try:
                box = await locator.bounding_box(timeout=min(timeout_ms, 1000))
                if box:
                    center_x = box["x"] + box["width"] / 2.0
                    center_y = box["y"] + box["height"] / 2.0
                    await self._update_cursor_overlay(center_x, center_y, label="Agent: Type")
            except Exception:
                pass
            try:
                await locator.focus(timeout=timeout_ms)
                await self.page.keyboard.insert_text(text)
            except Exception as exc:
                raise EngineError(f"Cannot type text into selector {selector!r}: {exc}") from exc

    # -------------------------------------------------------------------------
    # Canvas Coordinate Fallback (Per-Page CDPSession)
    # -------------------------------------------------------------------------

    async def canvas_click(
        self,
        x: float,
        y: float,
        *,
        button: str = "left",
        click_count: int = 1,
    ) -> None:
        """Dispatch coordinate-based mouse click via CDPSession fallback for canvas/WebGL."""
        self._check_attached()
        if button not in {"left", "middle", "right"}:
            raise ValueError("button must be 'left', 'middle', or 'right'")
        async with self.tab.lock:
            await self._update_cursor_overlay(x, y, label="Agent: Click")
            cdp = await self.tab.get_cdp_session()
            press = {
                "type": "mousePressed",
                "x": x,
                "y": y,
                "button": button,
                "clickCount": click_count,
            }
            release = {
                "type": "mouseReleased",
                "x": x,
                "y": y,
                "button": button,
                "clickCount": click_count,
            }
            pressed = True
            try:
                await cdp.send("Input.dispatchMouseEvent", press)
                await cdp.send("Input.dispatchMouseEvent", release)
                pressed = False
            finally:
                if pressed:
                    await asyncio.shield(cdp.send("Input.dispatchMouseEvent", release))

    async def canvas_mouse_move(self, x: float, y: float) -> None:
        """Dispatch coordinate-based mouse move via CDPSession fallback."""
        self._check_attached()
        async with self.tab.lock:
            await self._update_cursor_overlay(x, y, label="Agent: Move")
            cdp = await self.tab.get_cdp_session()
            await cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})

    async def canvas_drag(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        *,
        steps: int = 5,
        button: str = "left",
    ) -> None:
        """Perform a coordinate-based drag on a canvas element."""
        self._check_attached()
        if steps < 1:
            raise ValueError("steps must be at least 1")
        async with self.tab.lock:
            await self._update_cursor_overlay(start_x, start_y, label="Agent: Drag")
            cdp = await self.tab.get_cdp_session()
            await cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": start_x, "y": start_y})
            await cdp.send(
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", "x": start_x, "y": start_y, "button": button, "clickCount": 1},
            )
            try:
                for i in range(1, steps + 1):
                    t = i / steps
                    cur_x = start_x + (end_x - start_x) * t
                    cur_y = start_y + (end_y - start_y) * t
                    await self._update_cursor_overlay(cur_x, cur_y, label="Agent: Drag")
                    await cdp.send(
                        "Input.dispatchMouseEvent",
                        {"type": "mouseMoved", "x": cur_x, "y": cur_y, "button": button},
                    )
            finally:
                await self._update_cursor_overlay(end_x, end_y, label="Agent")
                await asyncio.shield(
                    cdp.send(
                        "Input.dispatchMouseEvent",
                        {"type": "mouseReleased", "x": end_x, "y": end_y, "button": button, "clickCount": 1},
                    )
                )

    # -------------------------------------------------------------------------
    # Passive Network Observation
    # -------------------------------------------------------------------------

    async def observe_network(self, *, buffer_size: int = 128) -> BoundedMetricQueue[dict[str, Any]]:
        """Observe response metadata via bounded non-blocking queue without pausing traffic."""
        self._check_attached()
        if not 1 <= buffer_size <= 4096:
            raise ValueError("buffer_size must be between 1 and 4096")
        queue: BoundedMetricQueue[dict[str, Any]] = BoundedMetricQueue(maxsize=buffer_size)

        def on_response(response: Response) -> None:
            try:
                data = {
                    "url": response.url,
                    "status": response.status,
                    "status_text": response.status_text,
                    "headers": redact_headers(response.headers),
                    "ok": response.ok,
                    "request": {
                        "method": response.request.method,
                        "url": response.request.url,
                        "resource_type": response.request.resource_type,
                        "headers": redact_headers(response.request.headers),
                    },
                }
                queue.put_dropping_oldest(data)
            except Exception as exc:
                LOG.debug("Error processing passive network response: %s", exc)

        self.page.on("response", on_response)
        self._network_observers[queue] = on_response
        return queue

    async def stop_network_observation(self, queue: BoundedMetricQueue[dict[str, Any]]) -> None:
        """Stop observing network responses for the given queue."""
        handler = self._network_observers.pop(queue, None)
        if handler is not None:
            try:
                self.page.remove_listener("response", handler)
            except Exception as exc:
                LOG.debug("Error removing network response listener: %s", exc)

    # -------------------------------------------------------------------------
    # Response Interception (Per-Page CDPSession Fallback)
    # -------------------------------------------------------------------------

    async def intercept_responses(
        self,
        *,
        buffer_size: int = 64,
        timeout_s: float = 10.0,
    ) -> BoundedMetricQueue[dict[str, Any]]:
        """Pause responses for explicit inspection with best-effort timeout watchdog and protocol fallback."""
        self._check_attached()
        if not 1 <= buffer_size <= 1024:
            raise ValueError("buffer_size must be between 1 and 1024")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if self._interception_active:
            raise CDPError("Response interception is already active for this tab")

        cdp = await self.tab.get_cdp_session()
        queue: BoundedMetricQueue[dict[str, Any]] = BoundedMetricQueue(maxsize=buffer_size)
        self._interception_queue = queue
        self._interception_active = True

        def on_paused(event: dict[str, Any]) -> None:
            request_id = event.get("requestId")
            if not request_id:
                return

            self._pending_events[request_id] = event

            # Hard timeout watchdog: ensure request is never stalled indefinitely
            async def _watchdog() -> None:
                try:
                    await asyncio.sleep(timeout_s)
                    LOG.warning(
                        "Interception watchdog timeout (%s s) reached for requestId %s; attempting auto-continue",
                        timeout_s,
                        request_id,
                    )
                    success = await self._safe_continue(cdp, request_id, event)
                    if not success:
                        LOG.error(
                            "Watchdog failed to auto-continue paused requestId %s: %s",
                            request_id,
                            self.last_continuation_error,
                        )
                except asyncio.CancelledError:
                    pass
                finally:
                    self._pending_timeouts.pop(request_id, None)
                    self._pending_events.pop(request_id, None)

            self._pending_timeouts[request_id] = asyncio.create_task(_watchdog())

            # Bounded queue insertion with drop-oldest
            dropped, dropped_event = queue.put_dropping_oldest(event)
            if dropped and dropped_event:
                dropped_req_id = dropped_event.get("requestId")
                if dropped_req_id:
                    LOG.warning(
                        "Interception queue overflow; auto-continuing dropped requestId %s",
                        dropped_req_id,
                    )
                    t = self._pending_timeouts.pop(dropped_req_id, None)
                    if t and not t.done():
                        t.cancel()
                    self._pending_events.pop(dropped_req_id, None)
                    asyncio.create_task(self._safe_continue(cdp, dropped_req_id, dropped_event))

        self._interception_listener = on_paused
        cdp.on("Fetch.requestPaused", on_paused)

        try:
            await cdp.send(
                "Fetch.enable",
                {
                    "patterns": [{"urlPattern": "*", "requestStage": "Response"}],
                    "handleAuthRequests": False,
                },
            )
        except Exception:
            cdp.remove_listener("Fetch.requestPaused", on_paused)
            self._interception_active = False
            self._interception_queue = None
            self._interception_listener = None
            raise

        return queue

    async def continue_response(self, event: dict[str, Any]) -> None:
        """Release a paused Fetch event, cancelling any pending watchdog task.

        Attempts Fetch.continueResponse with fallback to Fetch.continueRequest;
        raises CDPError if both primary continuation and protocol fallback fail.
        """
        self._check_attached()
        request_id = event.get("requestId") or event.get("params", {}).get("requestId")
        if not request_id:
            raise ValueError("Event must contain a valid requestId")

        task = self._pending_timeouts.pop(request_id, None)
        if task and not task.done():
            task.cancel()
        self._pending_events.pop(request_id, None)

        cdp = await self.tab.get_cdp_session()
        ok = await self._safe_continue(cdp, request_id, event)
        if not ok:
            raise CDPError(
                f"Failed to continue paused request {request_id}: {self.last_continuation_error}"
            )

    async def stop_response_interception(self, queue: BoundedMetricQueue[dict[str, Any]]) -> None:
        """Drain and attempt continuation of all paused events, then disable Fetch interception."""
        if queue is not self._interception_queue:
            raise ValueError("Queue is not registered for active response interception")

        cdp: CDPSession | None = None
        if self.tab.is_attached:
            try:
                cdp = await self.tab.get_cdp_session()
            except Exception as exc:
                LOG.debug("Could not get cdp session during stop_response_interception: %s", exc)

        # Drain queued events
        while not queue.empty():
            try:
                ev = queue.get_nowait()
                req_id = ev.get("requestId") or ev.get("params", {}).get("requestId")
                if req_id:
                    task = self._pending_timeouts.pop(req_id, None)
                    if task and not task.done():
                        task.cancel()
                    self._pending_events.pop(req_id, None)
                    if cdp is not None:
                        await self._safe_continue(cdp, req_id, ev)
            except asyncio.QueueEmpty:
                break

        # Cancel any remaining pending timeouts and continue lingering requests
        for req_id, task in list(self._pending_timeouts.items()):
            if not task.done():
                task.cancel()
            ev = self._pending_events.get(req_id, {})
            if cdp is not None:
                await self._safe_continue(cdp, req_id, ev)

        self._pending_timeouts.clear()
        self._pending_events.clear()

        if cdp is not None and self._interception_listener is not None:
            try:
                cdp.remove_listener("Fetch.requestPaused", self._interception_listener)
            except Exception as exc:
                LOG.debug("Error removing Fetch listener: %s", exc)
        self._interception_listener = None

        if cdp is not None:
            try:
                await cdp.send("Fetch.disable")
            except Exception as exc:
                LOG.debug("Error sending Fetch.disable: %s", exc)

        self._interception_active = False
        self._interception_queue = None

    async def _safe_continue(
        self,
        cdp: CDPSession,
        request_id: str,
        event: dict[str, Any],
    ) -> bool:
        """Attempt to continue a paused request with deliberate protocol fallback.

        If Fetch.continueResponse fails, falls back to Fetch.continueRequest.
        Tracks continuation_failure_count and last_continuation_error on failure.
        Returns True on success, False if both attempts failed.
        """
        params = event.get("params", event)
        has_response = "responseStatusCode" in params or "responseErrorReason" in params

        if has_response:
            try:
                await cdp.send("Fetch.continueResponse", {"requestId": request_id})
                return True
            except Exception as primary_exc:
                LOG.warning(
                    "Fetch.continueResponse failed for requestId %s (%s); attempting fallback to Fetch.continueRequest",
                    request_id,
                    primary_exc,
                )
                try:
                    await cdp.send("Fetch.continueRequest", {"requestId": request_id})
                    return True
                except Exception as fallback_exc:
                    self.continuation_failure_count += 1
                    if self._interception_queue is not None:
                        self._interception_queue.continuation_failure_count += 1
                    self.last_continuation_error = fallback_exc
                    LOG.error(
                        "Continuation protocol failed for requestId %s (continueResponse: %s, continueRequest fallback: %s)",
                        request_id,
                        primary_exc,
                        fallback_exc,
                    )
                    return False
        else:
            try:
                await cdp.send("Fetch.continueRequest", {"requestId": request_id})
                return True
            except Exception as exc:
                self.continuation_failure_count += 1
                if self._interception_queue is not None:
                    self._interception_queue.continuation_failure_count += 1
                self.last_continuation_error = exc
                LOG.error("Fetch.continueRequest failed for requestId %s: %s", request_id, exc)
                return False
