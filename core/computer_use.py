"""Screenshot-based mouse agent using Chromium CDP input and vision models."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

# Ensure workspace root is in sys.path when executed directly as a script
_WORKSPACE_ROOT = str(Path(__file__).resolve().parent.parent)
if _WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, _WORKSPACE_ROOT)

from core.browser_skills import BrowserSkills
from core.engine import BrowserEngine, TabSession


class ComputerUseAgent:
    WIDTH = 1280
    HEIGHT = 800

    def __init__(self, engine: BrowserEngine, tab: TabSession) -> None:
        self.engine, self.tab = engine, tab
        self.skills = BrowserSkills(tab)
        self.cdp = None

    async def attach(self) -> None:
        await self.tab.page.set_viewport_size({"width": self.WIDTH, "height": self.HEIGHT})
        self.cdp = await self.tab.get_cdp_session()
        await self.cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        await self.tab.page.evaluate("""() => {
          let el = document.getElementById('agy-agent-cursor');
          if (!el) {
            el = document.createElement('div'); el.id = 'agy-agent-cursor';
            el.setAttribute('aria-hidden','true');
            el.style.cssText='position:fixed;left:0;top:0;z-index:2147483647;pointer-events:none;transition:transform 0.12s linear;';
            el.innerHTML='<svg width="28" height="36" viewBox="0 0 28 36" style="display:block;filter:drop-shadow(1px 2px 2px #2228)"><path d="M2 2v25l7-7 5 11 5-2-5-11h10z" fill="#ff3b30" stroke="white" stroke-width="2"/></svg><span id="agy-agent-cursor-label" style="position:absolute;left:23px;top:23px;background:#b91c1c;color:white;padding:3px 7px;border-radius:4px;font:600 12px sans-serif;white-space:nowrap">CUA</span>';
            (document.body||document.documentElement).appendChild(el);
          }
        }""")

    async def _cursor(self, x: float, y: float, agent_name: str, color: str | None = None) -> None:
        if not color:
            color = "#" + hashlib.sha256(agent_name.encode()).hexdigest()[:6]
        await self.tab.page.evaluate("""({x,y,name,color}) => {
          const el=document.getElementById('agy-agent-cursor'); if(!el)return;
          el.style.transform=`translate3d(${x}px,${y}px,0)`;
          const label=document.getElementById('agy-agent-cursor-label');
          if(label){label.textContent=name;label.style.backgroundColor=color;}
          const path=el.querySelector('svg path'); if(path)path.setAttribute('fill',color);
        }""", {"x": x, "y": y, "name": agent_name, "color": color})

    async def screenshot(self) -> str:
        assert self.cdp is not None
        result = await self.cdp.send("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        return result["data"]

    async def execute_action(self, action: dict, agent_name: str = "CUA", color: str | None = None) -> str:
        assert self.cdp is not None, "Call attach() first"
        kind = str(action.get("action", "screenshot")).lower()
        x, y = float(action.get("x", 0)), float(action.get("y", 0))
        async with self.tab.lock:
            await self._cursor(x, y, agent_name, color=color)
            await self.cdp.send("Input.dispatchMouseEvent", {"type":"mouseMoved", "x":x, "y":y})
            if kind in {"click", "double_click"}:
                count = 2 if kind == "double_click" else 1
                for _ in range(count):
                    await self.cdp.send("Input.dispatchMouseEvent", {"type":"mousePressed","x":x,"y":y,"button":action.get("button","left"),"clickCount":count})
                    await self.cdp.send("Input.dispatchMouseEvent", {"type":"mouseReleased","x":x,"y":y,"button":action.get("button","left"),"clickCount":count})
            elif kind == "move":
                pass
            elif kind == "drag":
                sx, sy = float(action.get("start_x",x)), float(action.get("start_y",y))
                ex, ey = float(action.get("end_x",x)), float(action.get("end_y",y))
                steps = max(2, int(action.get("steps", 12)))
                await self._cursor(sx,sy,agent_name, color=color)
                await self.cdp.send("Input.dispatchMouseEvent", {"type":"mouseMoved","x":sx,"y":sy})
                await self.cdp.send("Input.dispatchMouseEvent", {"type":"mousePressed","x":sx,"y":sy,"button":"left"})
                try:
                    for i in range(1,steps+1):
                        px,py=sx+(ex-sx)*i/steps,sy+(ey-sy)*i/steps
                        await self._cursor(px,py,agent_name, color=color)
                        await self.cdp.send("Input.dispatchMouseEvent", {"type":"mouseMoved","x":px,"y":py,"button":"left"})
                        await asyncio.sleep(.025)
                finally:
                    await self.cdp.send("Input.dispatchMouseEvent", {"type":"mouseReleased","x":ex,"y":ey,"button":"left"})
            elif kind == "scroll":
                await self.cdp.send("Input.dispatchMouseEvent", {"type":"mouseWheel","x":x,"y":y,"deltaX":float(action.get("delta_x",0)),"deltaY":float(action.get("delta_y",0))})
            elif kind == "type":
                await self.cdp.send("Input.insertText", {"text":str(action.get("text",""))})
            elif kind == "keypress":
                key = str(action.get("key","Enter")); mods = int(action.get("modifiers",0))
                code = str(action.get("code",""))
                payload_down: dict[str, Any] = {"type":"keyDown","key":key,"modifiers":mods}
                payload_up: dict[str, Any] = {"type":"keyUp","key":key,"modifiers":mods}
                if code:
                    payload_down["code"] = code
                    payload_up["code"] = code
                await self.cdp.send("Input.dispatchKeyEvent", payload_down)
                await self.cdp.send("Input.dispatchKeyEvent", payload_up)
            elif kind == "wait":
                await asyncio.sleep(max(0,float(action.get("seconds",1))))
            elif kind != "screenshot":
                raise ValueError(f"Unsupported action: {kind}")
            return await self.screenshot()

    async def run(self, prompt: str, max_steps: int = 15) -> None:
        if os.getenv("OPENAI_API_KEY"):
            await self._run_openai(prompt, max_steps)
        elif os.getenv("GEMINI_API_KEY"):
            await self._run_gemini(prompt, max_steps)
        else:
            raise RuntimeError("Set OPENAI_API_KEY or GEMINI_API_KEY, or use --demo")

    @staticmethod
    def _post(url: str, headers: dict, payload: dict) -> dict:
        req=urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=90) as response:
            return json.loads(response.read())

    async def _run_openai(self, prompt: str, max_steps: int) -> None:
        key=os.environ["OPENAI_API_KEY"]
        image=await self.screenshot()
        payload={"model":"computer-use-preview","tools":[{"type":"computer_use_preview","display_width":1280,"display_height":800,"environment":"browser"}],"input":[{"role":"user","content":[{"type":"input_text","text":prompt},{"type":"input_image","image_url":"data:image/png;base64,"+image}]}]}
        data=await asyncio.to_thread(self._post,"https://api.openai.com/v1/responses",{"Authorization":"Bearer "+key,"Content-Type":"application/json"},payload)
        for _ in range(max_steps):
            calls=[item for item in data.get("output",[]) if item.get("type")=="computer_call"]
            if not calls: return
            outputs=[]
            for call in calls:
                observed=image
                for raw_action in call.get("actions",[]):
                    kind=raw_action.get("type", "screenshot")
                    if kind == "screenshot":
                        observed=await self.screenshot()
                    else:
                        action=dict(raw_action); action["action"]=kind
                        observed=await self.execute_action(action,"OpenAI")
                outputs.append({"type":"computer_call_output","call_id":call["call_id"],"output":{"type":"computer_screenshot","image_url":"data:image/png;base64,"+observed}})
                image=observed
            data=await asyncio.to_thread(self._post,"https://api.openai.com/v1/responses",{"Authorization":"Bearer "+key,"Content-Type":"application/json"},{"model":"computer-use-preview","tools":[{"type":"computer_use_preview","display_width":1280,"display_height":800,"environment":"browser"}],"previous_response_id":data["id"],"input":outputs})

    async def _run_gemini(self, prompt: str, max_steps: int) -> None:
        key=os.environ["GEMINI_API_KEY"]
        for _ in range(max_steps):
            image=await self.screenshot()
            instruction=("Return only JSON: {\"action\":\"click|double_click|move|drag|scroll|type|keypress|wait|screenshot|done\",\"x\":int,\"y\":int,...}. "+prompt)
            payload={"contents":[{"parts":[{"text":instruction},{"inline_data":{"mime_type":"image/png","data":image}}]}],"generationConfig":{"responseMimeType":"application/json"}}
            data=await asyncio.to_thread(self._post,f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}",{"Content-Type":"application/json"},payload)
            action=json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
            if action.get("action")=="done": return
            await self.execute_action(action,"Gemini")


async def demo(endpoint: str) -> None:
    engine=BrowserEngine(endpoint)
    await engine.connect()
    tab=await engine.get_or_open_target("figma.com", "https://www.figma.com")
    agent=ComputerUseAgent(engine,tab)
    try:
        await agent.attach()
        before=base64.b64decode(await agent.screenshot())
        points=[(200,200),(640,400),(800,500)]
        for x,y in points:
            raw=base64.b64decode(await agent.execute_action({"action":"move","x":x,"y":y},"CUA Demo"))
            print(f"cursor=({x}, {y}) overlay={await tab.page.locator('#agy-agent-cursor').count()} bytes={len(raw)} sha256={hashlib.sha256(raw).hexdigest()}")
            await asyncio.sleep(.2)
        # Visible canvas gesture samples: click then drag, with real CDP input.
        await agent.execute_action({"action":"click","x":640,"y":400},"CUA Demo")
        after=base64.b64decode(await agent.execute_action({"action":"drag","start_x":600,"start_y":420,"end_x":720,"end_y":500},"CUA Demo"))
        print(f"before_png_bytes={len(before)} before_pixel_hash={_pixel_hash(before)}")
        print(f"after_png_bytes={len(after)} after_pixel_hash={_pixel_hash(after)}")
    finally:
        await engine.disconnect()


def _pixel_hash(png: bytes) -> str:
    try:
        from PIL import Image
        import io
        image=Image.open(io.BytesIO(png)).convert("RGBA")
        return hashlib.sha256(image.tobytes()).hexdigest()
    except ImportError:
        return "png-sha256:"+hashlib.sha256(png).hexdigest()


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint",default="http://127.0.0.1:9222")
    parser.add_argument("--demo",action="store_true")
    parser.add_argument("--prompt",default="Complete the requested task using the visible page.")
    parser.add_argument("--max-steps",type=int,default=15)
    args=parser.parse_args()
    if args.demo:
        asyncio.run(demo(args.endpoint)); return
    async def execute() -> None:
        engine=BrowserEngine(args.endpoint); await engine.connect()
        tab=await engine.get_or_open_target(); agent=ComputerUseAgent(engine,tab)
        try: await agent.attach(); await agent.run(args.prompt,args.max_steps)
        finally: await engine.disconnect()
    asyncio.run(execute())


if __name__ == "__main__":
    main()
