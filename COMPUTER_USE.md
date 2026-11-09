# COMPUTER_USE.md — Screenshot-Based CDP Mouse Agent Specification

## 1. Core Loop Architecture (OpenAI `computer_use_preview` / Vision CUA Pattern)
The Computer Use Agent operates strictly on a closed-loop visual feedback cycle over CDP (`127.0.0.1:9222`) without hijacking the OS physical mouse or stealing window focus:

1. **Capture State (`Page.captureScreenshot`):** Capture an in-memory PNG screenshot of the attached viewport (`1280x800` fixed logical resolution so model `(x, y)` coordinates map 1:1 to viewport pixels).
2. **Model Inference:** Send the base64 screenshot + user goal to the Computer Use model (`computer-use-preview` via Responses API or Gemini Vision structured action output).
3. **Action Execution & Visible Overlay (`#agy-agent-cursor`):**
   - For every coordinate action (`click`, `double_click`, `move`, `drag`, `scroll`), first move the injected `#agy-agent-cursor` DOM overlay smoothly to `(x, y)` so the user can see the virtual mouse moving inside the tab.
   - Immediately dispatch the corresponding CDP `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent` inside the tab's `asyncio.Lock`.
4. **Settle & Repeat:** Wait `150ms–300ms` for WebGL/canvas frame mutation, capture the next screenshot, and feed `computer_call_output` back until the model signals task completion.

## 2. Supported Computer Actions
- `click(x, y, button="left")`
- `double_click(x, y)`
- `move(x, y)`
- `drag(path=[{"x": x1, "y": y1}, {"x": x2, "y": y2}])`
- `scroll(x, y, scroll_x, scroll_y)`
- `type(text)`
- `keypress(keys=[...])`
- `wait(ms=1000)`
- `screenshot()`

## 3. Non-Negotiable Guardrails
- **Never use OS-level mouse control (`pyautogui`, `SendInput`, `SetCursorPos`).** All actions go through Playwright CDP `Input.dispatchMouseEvent`.
- **Single-Pointer Serialization:** All multi-agent calls share a per-tab `asyncio.Lock()`. When switching active sub-agents (e.g., `Layout`, `Motion`, `Critic`), update the `#agy-agent-cursor` badge label and color before moving.
- **Zero Disk Bloat:** Screenshots stay in memory as base64 buffers unless explicitly saved for debugging.
