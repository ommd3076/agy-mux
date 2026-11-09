---
name: figma-cua
description: Autonomous multi-agent computer use on Figma or Canva via Chromium CDP and SwarmOrchestrator. Use whenever the user asks to design, draw, animate, inspect, or run computer use workflows on Figma or Canva.
---

# Figma Computer Use Agent (CUA)

This skill activates autonomous multi-agent computer use on Figma or Canva web canvases using `core/orchestrator.py`.

## Invocation Protocol

When asked to design, draw, animate, layout, or execute visual computer use on Figma or Canva, invoke **exactly** this command with the prompt safely quoted:

```powershell
C:\Users\ommd3\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe C:\ommd3076\Proejcts\nav-com\core\orchestrator.py --prompt "<user_request>"
```

### Swarm Demonstration Mode

To run the deterministic 4-persona swarm demonstration (Orchestrator scan -> Layout 240x160 rectangle -> Motion reversible adjustment -> Visual Critic hover & Ctrl+Z rollback) on `localhost:9222`:

```powershell
C:\Users\ommd3\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe C:\ommd3076\Proejcts\nav-com\core\orchestrator.py --swarm-demo
```

To retain the drawn elements on the canvas without undoing, add `--keep`:

```powershell
C:\Users\ommd3\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe C:\ommd3076\Proejcts\nav-com\core\orchestrator.py --swarm-demo --keep
```

## Persona Architecture & Responsibilities

- **Orchestrator** (`#8B5CF6`): Captures initial screenshot, decomposes design requests into ordered work items, and verifies final visual outcome.
- **Layout_Agent** (`#3B82F6`): Manages layout frames, draws geometric primitives (`F`, `R`, `T`), and aligns canvas elements.
- **Motion_Agent** (`#10B981`): Configures prototype states, micro-interactions, animation parameters, and property edits.
- **Visual_Critic** (`#F59E0B`): Inspects rendered screenshots, verifies boundaries and alignment, and executes automated rollback (`Ctrl+Z`) when defects occur.

## Execution Guardrails

1. **Synthetic CDP Events Only**: Pointer and keyboard actions are scoped to the active tab via CDP. Never invoke OS-level mouse or window focus APIs.
2. **Lock Serialization**: All actions execute under the tab's dedicated `asyncio.Lock` (`TabSession.lock`).
3. **In-Memory Buffers**: Screenshots are captured directly into in-memory base64 buffers without creating disk image artifacts.
4. **Browser Survival**: The managed browser session on `localhost:9222` must survive disconnects; never close or terminate the browser process on teardown.
