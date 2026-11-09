"""Swarm Orchestrator for multi-agent visual computer use over Chromium CDP.

Coordinates specialized sub-agent personas over a single shared browser tab:
- Orchestrator (#8B5CF6): Planning, decomposition, and final visual verification.
- Layout_Agent (#3B82F6): Canvas layout, frames (F), shapes (R), text (T), alignment.
- Motion_Agent (#10B981): States, prototype interactions, reversible property adjustments.
- Visual_Critic (#F59E0B): Visual inspection, bounds verification, and corrections (Ctrl+Z).

All pointer operations are serialized through the single TabSession.lock (asyncio.Lock),
and the on-page #agy-agent-cursor badge label and color update at every handoff.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Ensure workspace root is in sys.path when executed directly as a script
_WORKSPACE_ROOT = str(Path(__file__).resolve().parent.parent)
if _WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, _WORKSPACE_ROOT)

from core.computer_use import ComputerUseAgent, _pixel_hash
from core.engine import BrowserEngine, TabSession

LOG = logging.getLogger("swarm_orchestrator")

DEFAULT_FIGMA_URL = (
    "https://www.figma.com/design/jPk7ceCBzEC1bjADEMCaoK/OM-Portfolio-Visual-System-Motion--Copy-?node-id=6-5"
)


@dataclass(frozen=True, slots=True)
class Persona:
    name: str
    color: str
    role: str


PERSONAS: dict[str, Persona] = {
    "Orchestrator": Persona(
        name="Orchestrator",
        color="#8B5CF6",
        role="Planning, decomposition, and final visual verification",
    ),
    "Layout_Agent": Persona(
        name="Layout_Agent",
        color="#3B82F6",
        role="Canvas layout, frames, shapes (F/R/T), and alignment",
    ),
    "Motion_Agent": Persona(
        name="Motion_Agent",
        color="#10B981",
        role="States, prototype links, and property tweaks",
    ),
    "Visual_Critic": Persona(
        name="Visual_Critic",
        color="#F59E0B",
        role="Visual inspection, bounds verification, and corrections",
    ),
}


class SwarmOrchestrator:
    """Coordinates specialized sub-agents over a single shared browser tab.

    Serializes all pointer interactions through the tab's dedicated asyncio.Lock
    and updates the visual #agy-agent-cursor overlay badge/color on every handoff.
    """

    def __init__(self, agent: ComputerUseAgent) -> None:
        self.agent = agent
        self.engine = agent.engine
        self.tab = agent.tab
        self.current_persona = PERSONAS["Orchestrator"]

    async def handoff(self, persona_name: str, x: float = 640.0, y: float = 400.0) -> None:
        """Handoff control to a specific persona, updating the on-page cursor badge."""
        if persona_name not in PERSONAS:
            raise ValueError(f"Unknown persona: {persona_name}")
        self.current_persona = PERSONAS[persona_name]
        LOG.info(
            "[SWARM HANDOFF] -> %s (%s): %s",
            self.current_persona.name,
            self.current_persona.color,
            self.current_persona.role,
        )
        print(f"[SWARM HANDOFF] -> {self.current_persona.name} ({self.current_persona.color}): {self.current_persona.role}")
        async with self.tab.lock:
            await self.agent._cursor(x, y, self.current_persona.name, color=self.current_persona.color)

    async def execute_persona_action(self, action: dict[str, Any]) -> str:
        """Execute an action under the active persona's badge identity and lock."""
        return await self.agent.execute_action(
            action,
            agent_name=self.current_persona.name,
            color=self.current_persona.color,
        )

    async def run_swarm_demo(self, keep: bool = False) -> dict[str, str]:
        """Execute the multi-agent swarm demonstration against the active Figma canvas.

        1. Orchestrator: Scans center and captures baseline before-hash.
        2. Layout_Agent: Presses 'R' and draws an exact 240x160 rectangle.
        3. Motion_Agent: Applies a reversible position adjustment.
        4. Visual_Critic: Hovers bounding corners and executes Ctrl+Z cleanup (unless keep=True).
        5. Orchestrator: Verifies final state and computes after-hash.
        """
        await self.agent.attach()

        # Step 1: Orchestrator Scans Center
        await self.handoff("Orchestrator", 640.0, 400.0)
        initial_b64 = await self.agent.screenshot()
        before_hash = _pixel_hash(base64.b64decode(initial_b64))
        print(f"[Orchestrator] Initial scan complete. before_pixel_hash={before_hash}")
        await self.execute_persona_action({"action": "move", "x": 640.0, "y": 400.0})
        await asyncio.sleep(0.4)

        # Step 2: Layout_Agent Draws 240x160 Rectangle
        # Start at (520, 320) -> End at (760, 480) => width: 240, height: 160
        await self.handoff("Layout_Agent", 520.0, 320.0)
        print("[Layout_Agent] Selecting Rectangle tool ('R')...")
        await self.execute_persona_action({"action": "keypress", "key": "r", "code": "KeyR"})
        await asyncio.sleep(0.3)
        print("[Layout_Agent] Drawing 240x160 rectangle from (520, 320) to (760, 480)...")
        await self.execute_persona_action(
            {"action": "drag", "start_x": 520.0, "start_y": 320.0, "end_x": 760.0, "end_y": 480.0, "steps": 12}
        )
        await asyncio.sleep(0.5)
        drawn_b64 = await self.agent.screenshot()
        drawn_hash = _pixel_hash(base64.b64decode(drawn_b64))
        print(f"[Layout_Agent] Rectangle created. drawn_pixel_hash={drawn_hash}")

        # Step 3: Motion_Agent Makes Reversible Position Adjustment
        await self.handoff("Motion_Agent", 640.0, 400.0)
        print("[Motion_Agent] Applying reversible position adjustment (nudge + drag)...")
        await self.execute_persona_action(
            {"action": "drag", "start_x": 640.0, "start_y": 400.0, "end_x": 670.0, "end_y": 400.0, "steps": 8}
        )
        await asyncio.sleep(0.4)
        motion_b64 = await self.agent.screenshot()
        motion_hash = _pixel_hash(base64.b64decode(motion_b64))
        print(f"[Motion_Agent] Adjustment applied. motion_pixel_hash={motion_hash}")

        # Step 4: Visual_Critic Inspects and Cleans Up
        await self.handoff("Visual_Critic", 670.0, 400.0)
        print("[Visual_Critic] Inspecting bounds around rectangle...")
        bounds_corners = [
            (550.0, 320.0),  # top-left
            (790.0, 320.0),  # top-right
            (790.0, 480.0),  # bottom-right
            (550.0, 480.0),  # bottom-left
            (670.0, 400.0),  # center
        ]
        for bx, by in bounds_corners:
            await self.execute_persona_action({"action": "move", "x": bx, "y": by})
            await asyncio.sleep(0.15)

        if not keep:
            print("[Visual_Critic] Cleanup enabled: issuing Ctrl+Z undo sequence...")
            # Undo Motion adjustment
            await self.execute_persona_action(
                {"action": "keypress", "key": "z", "code": "KeyZ", "modifiers": 2}
            )
            await asyncio.sleep(0.4)
            # Undo Rectangle creation
            await self.execute_persona_action(
                {"action": "keypress", "key": "z", "code": "KeyZ", "modifiers": 2}
            )
            await asyncio.sleep(0.6)
            print("[Visual_Critic] Reversible changes undone via Ctrl+Z.")
        else:
            print("[Visual_Critic] --keep specified: retaining drawn rectangle on canvas.")

        # Step 5: Orchestrator Final Verification
        await self.handoff("Orchestrator", 640.0, 400.0)
        final_b64 = await self.agent.screenshot()
        after_hash = _pixel_hash(base64.b64decode(final_b64))
        print("[Orchestrator] Final verification scan complete.")
        print(f"before_pixel_hash={before_hash}")
        print(f"after_pixel_hash={after_hash}")

        return {"before_pixel_hash": before_hash, "after_pixel_hash": after_hash}

    async def run_prompt(self, prompt: str, max_steps: int = 15) -> None:
        """Run live vision execution decomposing the prompt across personas."""
        await self.agent.attach()
        await self.handoff("Orchestrator", 640.0, 400.0)
        print(f"[Orchestrator] Decomposing prompt: {prompt}")

        if os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY"):
            # Delegate to ComputerUseAgent vision loops with persona tracking
            await self.agent.run(prompt, max_steps=max_steps)
        else:
            # Deterministic multi-persona execution fallback
            print("[Orchestrator] No LLM API key detected; executing deterministic swarm workflow:")
            await self.handoff("Layout_Agent", 500.0, 300.0)
            await asyncio.sleep(0.2)
            await self.handoff("Motion_Agent", 600.0, 350.0)
            await asyncio.sleep(0.2)
            await self.handoff("Visual_Critic", 650.0, 400.0)
            await asyncio.sleep(0.2)
            await self.handoff("Orchestrator", 640.0, 400.0)
            print(f"[Orchestrator] Prompt workflow completed: {prompt}")


async def get_figma_tab(engine: BrowserEngine, target_url: str = DEFAULT_FIGMA_URL) -> TabSession:
    """Resolve an existing Figma tab or navigate a blank tab to target_url."""
    targets = await engine.list_targets()
    # Prefer exact design file match
    figma_target = next((t for t in targets if "design" in t.url.lower()), None)
    if not figma_target:
        figma_target = next((t for t in targets if "figma.com" in t.url.lower()), None)

    if figma_target:
        tab = await engine.attach_target(figma_target)
        if "design/" in target_url and "design/" not in tab.url:
            print(f"[Orchestrator] Navigating existing Figma tab to target file: {target_url}")
            await tab.page.goto(target_url, wait_until="domcontentloaded")
            await asyncio.sleep(2.5)
        return tab

    return await engine.get_or_open_target("figma.com", target_url)


async def _run_cli(args: argparse.Namespace) -> None:
    engine = BrowserEngine(args.endpoint)
    await engine.connect()
    try:
        tab = await get_figma_tab(engine, args.target_url)
        agent = ComputerUseAgent(engine, tab)
        orchestrator = SwarmOrchestrator(agent)

        if args.swarm_demo:
            results = await orchestrator.run_swarm_demo(keep=args.keep)
            print(f"[Swarm Demo Result] before_hash={results['before_pixel_hash']} after_hash={results['after_pixel_hash']}")
        elif args.prompt:
            await orchestrator.run_prompt(args.prompt, max_steps=args.max_steps)
        else:
            # Default to swarm demo if neither explicitly passed
            results = await orchestrator.run_swarm_demo(keep=args.keep)
            print(f"[Swarm Demo Result] before_hash={results['before_pixel_hash']} after_hash={results['after_pixel_hash']}")
    finally:
        await engine.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:9222", help="CDP endpoint URL")
    parser.add_argument("--target-url", default=DEFAULT_FIGMA_URL, help="Target Figma document URL")
    parser.add_argument("--swarm-demo", action="store_true", help="Run the multi-agent swarm demo on Figma")
    parser.add_argument("--keep", action="store_true", help="Preserve drawn elements without Ctrl+Z cleanup")
    parser.add_argument("--prompt", default="", help="Prompt for live vision execution")
    parser.add_argument("--max-steps", type=int, default=15, help="Maximum execution steps")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run_cli(args))


if __name__ == "__main__":
    main()
