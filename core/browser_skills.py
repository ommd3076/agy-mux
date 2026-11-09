"""DOM-aware browser skills built on a target-scoped CDP session."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from core.engine import CDPError, CDPSession, BrowserEngine


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
    """Composable browser operations that never use the OS pointer or focus."""

    def __init__(self, engine: BrowserEngine, session: CDPSession) -> None:
        self.engine = engine
        self.session = session
        self._input_lock = asyncio.Lock()

    async def accessibility_tree(self, *, depth: int = 8) -> dict[str, Any]:
        if depth < 1 or depth > 32:
            raise ValueError("depth must be between 1 and 32")
        await self.session.send("Accessibility.enable")
        return await self.session.send("Accessibility.getFullAXTree", {"depth": depth})

    async def interactive_elements(self) -> list[ElementSnapshot]:
        expression = """(() => Array.from(document.querySelectorAll('button,a,input,textarea,select,[role="button"],[role="link"],[contenteditable="true"]'))
          .map((el,i) => { const r=el.getBoundingClientRect(), s=getComputedStyle(el);
            return {tag:el.tagName.toLowerCase(),role:el.getAttribute('role')||'',name:(el.innerText||el.getAttribute('aria-label')||el.placeholder||el.value||'').trim().slice(0,160),
              selector:el.id?'#'+CSS.escape(el.id):el.getAttribute('data-testid')?'[data-testid="'+CSS.escape(el.getAttribute('data-testid'))+'"]':el.tagName.toLowerCase()+':nth-of-type('+(Array.from(el.parentElement?.children||[]).filter(x=>x.tagName===el.tagName).indexOf(el)+1)+')',
              x:r.x+r.width/2,y:r.y+r.height/2,width:r.width,height:r.height,enabled:!el.disabled&&el.getAttribute('aria-disabled')!=='true',visible:r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'}; })
          .filter(el=>el.visible).map(({visible,...el})=>el))})()"""
        raw = await self.engine.evaluate(self.session, expression)
        return [ElementSnapshot(**item) for item in (raw or [])]

    async def click(self, selector: str, *, timeout_ms: int = 5000) -> None:
        if not selector.strip():
            raise ValueError("selector must not be empty")
        expression = """(selector) => { const el=document.querySelector(selector); if(!el) return {ok:false,error:'not_found'};
          const r=el.getBoundingClientRect(), s=getComputedStyle(el); if(r.width<=0||r.height<=0||s.visibility==='hidden'||s.display==='none') return {ok:false,error:'not_visible'};
          if(el.disabled||el.getAttribute('aria-disabled')==='true') return {ok:false,error:'disabled'};
          return {ok:true,x:r.x+r.width/2,y:r.y+r.height/2}; }"""
        result = await self._evaluate_function(expression, selector)
        if not result or not result.get("ok"):
            raise CDPError(f"Cannot click selector {selector!r}: {(result or {}).get('error', 'evaluation failed')}")
        async with self._input_lock:
            await self.session.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": result["x"], "y": result["y"], "button": "left", "clickCount": 1})
            await self.session.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": result["x"], "y": result["y"], "button": "left", "clickCount": 1})

    async def type_text(self, selector: str, text: str, *, timeout_ms: int = 5000) -> None:
        if not selector.strip():
            raise ValueError("selector must not be empty")
        async with self._input_lock:
            result = await self._evaluate_function("""(selector) => { const el=document.querySelector(selector); if(!el) return {ok:false,error:'not_found'};
              if(el.disabled||el.getAttribute('aria-disabled')==='true') return {ok:false,error:'disabled'};
              el.focus({preventScroll:true}); return {ok:true}; }""", selector)
            if not result or not result.get("ok"):
                raise CDPError(f"Cannot focus selector {selector!r}: {(result or {}).get('error', 'evaluation failed')}")
            await self.session.send("Input.insertText", {"text": text})

    async def observe_network(self, *, buffer_size: int = 128) -> asyncio.Queue[dict[str, Any]]:
        """Observe response metadata without pausing or changing browser traffic."""
        if not 1 <= buffer_size <= 4096:
            raise ValueError("buffer_size must be between 1 and 4096")
        queue = self.session.events("Network.responseReceived", max_queue=buffer_size)
        await self.session.send("Network.enable", {"maxTotalBufferSize": 4 * 1024 * 1024, "maxResourceBufferSize": 512 * 1024})
        return queue

    async def intercept_responses(self, *, buffer_size: int = 64) -> asyncio.Queue[dict[str, Any]]:
        """Pause responses for explicit inspection; each event must be continued."""
        if not 1 <= buffer_size <= 1024:
            raise ValueError("buffer_size must be between 1 and 1024")
        queue = self.session.events("Fetch.requestPaused", max_queue=buffer_size)
        try:
            await self.session.send("Fetch.enable", {
                "patterns": [{"urlPattern": "*", "requestStage": "Response"}],
                "handleAuthRequests": False,
            })
        except Exception:
            self.session.remove_events("Fetch.requestPaused", queue)
            raise
        return queue

    async def continue_response(self, event: dict[str, Any]) -> None:
        """Release a paused response without changing its status, headers, or body."""
        params = event.get("params", {})
        request_id = params.get("requestId")
        if event.get("method") != "Fetch.requestPaused" or not request_id:
            raise ValueError("event must be a Fetch.requestPaused protocol event")
        await self.session.send("Fetch.continueResponse", {"requestId": request_id})

    async def stop_response_interception(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Stop interception after the caller has continued all queued responses."""
        self.session.remove_events("Fetch.requestPaused", queue)
        if not self.session._events.get("Fetch.requestPaused"):
            await self.session.send("Fetch.disable")

    async def stop_network_observation(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.session.remove_events("Network.responseReceived", queue)
        if not self.session._events.get("Network.responseReceived"):
            await self.session.send("Network.disable")

    async def _evaluate_function(self, function_source: str, argument: Any) -> Any:
        payload = json.dumps(argument, ensure_ascii=False)
        source = f"({function_source})({payload})"
        return await self.engine.evaluate(self.session, source)
