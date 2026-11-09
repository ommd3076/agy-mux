# Figma & Canva Visual Computer Use Skills

This document details the exact CDP keyboard and mouse primitives used by `core/orchestrator.py` and `core/computer_use.py` when controlling Figma or Canva canvases.

---

## 1. Primary Keyboard Shortcuts

| Shortcut | Tool / Action | CDP Dispatch Payload | Description |
| :--- | :--- | :--- | :--- |
| **`V`** | Move / Selection Tool | `{"type": "keypress", "key": "v", "code": "KeyV"}` | Reverts active cursor back to standard pointer tool. |
| **`F`** | Frame Tool | `{"type": "keypress", "key": "f", "code": "KeyF"}` | Activates frame creation tool for container layouts. |
| **`R`** | Rectangle Tool | `{"type": "keypress", "key": "r", "code": "KeyR"}` | Activates rectangle shape tool for drawing boxes. |
| **`T`** | Text Tool | `{"type": "keypress", "key": "t", "code": "KeyT"}` | Activates typography text tool for labels/copy. |
| **`Ctrl+D`** | Duplicate | `{"type": "keypress", "key": "d", "code": "KeyD", "modifiers": 2}` | Duplicates selected element in-place with offset. |
| **`Ctrl+Z`** | Undo | `{"type": "keypress", "key": "z", "code": "KeyZ", "modifiers": 2}` | Undoes preceding canvas or property modification. |
| **`Delete` / `Backspace`** | Delete Element | `{"type": "keypress", "key": "Backspace", "code": "Backspace"}` | Removes currently selected element. |

*Note: Chromium CDP modifier bitmask for Control is `2` (`Alt=1, Control=2, Meta/Cmd=4, Shift=8`).*

---

## 2. Canvas Navigation & Viewport Primitives

### Space + Drag (Canvas Pan)
Pans the canvas without altering selection or element positions:
1. Dispatch `Input.dispatchKeyEvent` with `{"type": "keyDown", "key": " ", "code": "Space"}`.
2. Dispatch `Input.dispatchMouseEvent` with `{"type": "mousePressed", "x": start_x, "y": start_y, "button": "left"}`.
3. Dispatch intermediate `mouseMoved` events along vector `(start_x, start_y) -> (end_x, end_y)`.
4. Dispatch `Input.dispatchMouseEvent` with `{"type": "mouseReleased", "x": end_x, "y": end_y, "button": "left"}`.
5. Dispatch `Input.dispatchKeyEvent` with `{"type": "keyUp", "key": " ", "code": "Space"}`.

### Ctrl + Wheel (Canvas Zoom)
Zooms in or out centered at coordinate `(x, y)`:
- Zoom in: `{"type": "mouseWheel", "x": center_x, "y": center_y, "deltaX": 0, "deltaY": -120, "modifiers": 2}`
- Zoom out: `{"type": "mouseWheel", "x": center_x, "y": center_y, "deltaX": 0, "deltaY": 120, "modifiers": 2}`

---

## 3. Right-Panel Coordinate CDP Primitives

On standard 1280x800 viewport resolution, Figma's right-hand Design/Prototype/Inspect panel occupies `x: 1040..1280` and `y: 48..800`.

### Position & Dimension Inputs
- **X Position field**: Click `(x: 1090, y: 88)`, double-click to select all text, send `Input.insertText` with numeric string, press `Enter`.
- **Y Position field**: Click `(x: 1180, y: 88)`, double-click, send `Input.insertText`, press `Enter`.
- **Width (W) field**: Click `(x: 1090, y: 124)`, double-click, send `Input.insertText`, press `Enter`.
- **Height (H) field**: Click `(x: 1180, y: 124)`, double-click, send `Input.insertText`, press `Enter`.
- **Corner Radius**: Click `(x: 1230, y: 160)`, double-click, send `Input.insertText`, press `Enter`.

### Color & Fill Inspector
- **Fill Swatch**: Click `(x: 1060, y: 260)` to open color picker popover.
- **Hex Code Input**: Click `(x: 1120, y: 540)` in the open popover, insert hex string (e.g. `1E40AF`), press `Enter`.

### Prototype Interactions Tab
- **Tab Header**: Click `(x: 1160, y: 24)` to toggle between Design and Prototype panels.
- **Add Interaction**: Click `(x: 1250, y: 64)` (`+` icon) to create trigger event.
