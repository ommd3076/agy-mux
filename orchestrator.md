# Swarm Orchestrator Architecture

`core/orchestrator.py` implements the multi-agent visual computer use system (`SwarmOrchestrator`) over `core/computer_use.py` and `core/engine.py`.

## Persona Definitions & Exact Badge Colors

| Persona | Badge Color | Primary Responsibilities |
| :--- | :--- | :--- |
| **Orchestrator** | `#8B5CF6` (Purple) | Initial baseline capture, prompt decomposition, task queue creation, and final screenshot verification. |
| **Layout_Agent** | `#3B82F6` (Blue) | Canvas structure, frame creation (`F`), rectangle drawing (`R`), text placement (`T`), and geometric alignment. |
| **Motion_Agent** | `#10B981` (Green) | State definition, interactive prototype connections, micro-interactions, and reversible property adjustments. |
| **Visual_Critic** | `#F59E0B` (Amber) | Visual inspection, bounding box verification, anomaly detection, and automated rollback (`Ctrl+Z`) or confirmation. |

---

## State Machine & Handoff Flow

```
[Start]
   │
   ▼
┌──────────────────────────────────────────────┐
│  1. Orchestrator (#8B5CF6)                   │
│  - Captures baseline screenshot & pixel hash │
│  - Decomposes user request into subtasks     │
│  - Positions pointer at canvas center        │
└──────────────────────┬───────────────────────┘
                       │
                       ▼ [Handoff: Update #agy-agent-cursor to #3B82F6]
┌──────────────────────────────────────────────┐
│  2. Layout_Agent (#3B82F6)                   │
│  - Selects tool (e.g. 'R' for rectangle)     │
│  - Executes coordinate drag (e.g. 240x160)   │
│  - Establishes canvas element boundaries     │
└──────────────────────┬───────────────────────┘
                       │
                       ▼ [Handoff: Update #agy-agent-cursor to #10B981]
┌──────────────────────────────────────────────┐
│  3. Motion_Agent (#10B981)                   │
│  - Inspects initial placement                │
│  - Performs reversible position adjustments  │
│  - Sets interactive parameters               │
└──────────────────────┬───────────────────────┘
                       │
                       ▼ [Handoff: Update #agy-agent-cursor to #F59E0B]
┌──────────────────────────────────────────────┐
│  4. Visual_Critic (#F59E0B)                  │
│  - Inspects rendered result screenshot       │
│  - Hovers bounding box corners to test bounds│
│  - If clean/demo: issues Ctrl+Z rollback     │
│  - If permanent: validates acceptance        │
└──────────────────────┬───────────────────────┘
                       │
                       ▼ [Handoff: Update #agy-agent-cursor to #8B5CF6]
┌──────────────────────────────────────────────┐
│  5. Orchestrator (#8B5CF6)                   │
│  - Captures final screenshot & pixel hash    │
│  - Compares before/after state metrics       │
│  - Reports verified completion               │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
                    [Finish]
```

---

## Core Invariants

1. **Shared Pointer Serialization**:
   All 4 personas operate sequentially on a single `BrowserEngine` tab. Every mouse movement, drag, click, and keypress runs inside `async with tab.lock:`. Pointer collisions or race conditions are structurally impossible.

2. **Visual Feedback (`#agy-agent-cursor`)**:
   At every handoff, the on-screen DOM cursor badge is updated with the active persona name and its exact hex color code, providing transparent visibility of agent transitions.

3. **In-Memory Capture**:
   All screenshots are taken via CDP (`Page.captureScreenshot`) directly into in-memory base64 buffers. No temporary PNG files are written to disk.

4. **Process & Session Longevity**:
   On completion or disconnect, Playwright CDP sessions detach cleanly. The managed Chromium/Brave instance on `localhost:9222` is **never killed**, keeping user canvases and Figma files alive.
