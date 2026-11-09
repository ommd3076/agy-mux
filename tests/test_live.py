"""Live integration tests against an isolated ephemeral browser process."""

import asyncio
import http.server
import json
import logging
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import (
    BrowserEngine,
    CDPError,
    EngineError,
    TabSession,
    Target,
    TargetNotFoundError,
)
from core.browser_skills import (
    BoundedMetricQueue,
    BrowserSkills,
    redact_headers,
    REDACTED_HEADER_VALUE,
)

LOG = logging.getLogger("tests.live")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _find_browser_binary() -> str | None:
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "msedge",
        "google-chrome",
        "chromium",
        "chrome",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
        found = shutil.which(c)
        if found:
            return found
    return None


class _TestHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/shared.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Set-Cookie", "session_id=secret-cookie-12345; Path=/")
            self.send_header("X-Custom-Meta", "public-metadata")
            self.end_headers()
            html = """<!DOCTYPE html>
            <html>
            <head><title>Shared Duplicate URL</title></head>
            <body>
                <h1>Target Isolation Verification</h1>
                <button id="btn1">Action Button</button>
                <input id="input1" type="text" value="initial" />
                <input id="input2" type="text" value="" />
                <div id="output">idle</div>
                <canvas id="test_canvas" width="300" height="200" style="display: block; width: 300px; height: 200px;"></canvas>

                <script>
                    window._btn_clicked = false;
                    document.getElementById('btn1').addEventListener('click', () => {
                        window._btn_clicked = true;
                        document.getElementById('output').innerText = 'clicked';
                    });

                    window._canvas_events = [];
                    const canvas = document.getElementById('test_canvas');
                    ['mousedown', 'mousemove', 'mouseup', 'click'].forEach(evt => {
                        canvas.addEventListener(evt, (e) => {
                            window._canvas_events.push({
                                type: evt,
                                x: Math.round(e.clientX),
                                y: Math.round(e.clientY),
                                button: e.button
                            });
                        });
                    });
                </script>
            </body>
            </html>"""
            self.wfile.write(html.encode("utf-8"))
        elif self.path == "/api/data":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "auth_token=super-secret-token; HttpOnly")
            self.send_header("X-API-Key-Header", "top-secret-api-key")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass  # Quiet HTTP logs


class TestLiveBrowserIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration test suite executing against a dedicated, isolated browser process."""

    proc: subprocess.Popen[bytes] | None = None
    cdp_endpoint: str = ""
    cdp_port: int = 0
    temp_dir: Path | None = None
    httpd: socketserver.TCPServer | None = None
    server_port: int = 0
    server_thread: threading.Thread | None = None

    @classmethod
    def setUpClass(cls) -> None:
        # Check Playwright availability
        try:
            import playwright.async_api  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(
                f"Playwright library genuinely unavailable in active environment: {exc}. "
                "Install dependencies with 'python -m pip install -r requirements.txt'."
            )

        # Check browser binary availability
        browser_bin = _find_browser_binary()
        if not browser_bin:
            raise unittest.SkipTest(
                "Chromium/Edge browser binary genuinely unavailable on this system "
                "(searched Program Files and PATH for msedge, chrome, chromium, google-chrome)."
            )

        # Start local test HTTP server
        cls.httpd = socketserver.TCPServer(("127.0.0.1", 0), _TestHTTPHandler)
        cls.server_port = cls.httpd.server_address[1]
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

        # Allocate high loopback port and ephemeral profile
        cls.cdp_port = _find_free_port()
        cls.cdp_endpoint = f"http://127.0.0.1:{cls.cdp_port}"
        cls.temp_dir = Path(tempfile.gettempdir()) / f"nav_com_live_test_{os.getpid()}_{int(time.time())}"
        cls.temp_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            browser_bin,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={cls.cdp_port}",
            f"--user-data-dir={cls.temp_dir}",
            "about:blank",
        ]
        cls.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Wait for CDP endpoint readiness
        ready = False
        for _ in range(30):
            time.sleep(0.5)
            try:
                req = urllib.request.Request(f"{cls.cdp_endpoint}/json/version")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                pass

        if not ready:
            cls.tearDownClass()
            raise unittest.SkipTest(f"Could not connect to isolated browser CDP endpoint at {cls.cdp_endpoint}")

    @classmethod
    def tearDownClass(cls) -> None:
        # Cleanly terminate browser process tree
        if cls.proc is not None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/pid", str(cls.proc.pid), "/T", "/F"], capture_output=True)
            else:
                cls.proc.kill()
            try:
                cls.proc.wait(timeout=3.0)
            except Exception:
                pass
            cls.proc = None

        time.sleep(0.5)

        # Remove ephemeral profile directory
        if cls.temp_dir and cls.temp_dir.exists():
            shutil.rmtree(cls.temp_dir, ignore_errors=True)
            cls.temp_dir = None

        # Stop HTTP test server
        if cls.httpd is not None:
            cls.httpd.shutdown()
            try:
                cls.httpd.server_close()
            except Exception:
                pass
            cls.httpd = None

    async def asyncSetUp(self) -> None:
        self.engine = BrowserEngine(self.cdp_endpoint)

    async def asyncTearDown(self) -> None:
        await self.engine.disconnect()

    async def _create_test_tab(self) -> str:
        shared_url = f"http://127.0.0.1:{self.server_port}/shared.html"
        req = urllib.request.Request(f"{self.cdp_endpoint}/json/new?{shared_url}", method="PUT")
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode())
            return data["id"]

    async def test_01_two_same_url_tabs_and_target_id_attachment(self) -> None:
        """Verify strict target ID matching and isolation when two tabs share identical URLs."""
        shared_url = f"http://127.0.0.1:{self.server_port}/shared.html"

        # Open two tabs with the exact same URL via CDP control
        id1 = await self._create_test_tab()
        id2 = await self._create_test_tab()
        created_ids = [id1, id2]

        await asyncio.sleep(0.5)

        # Discover targets
        targets = await self.engine.list_targets()
        shared_targets = [t for t in targets if t.target_id in created_ids]
        self.assertEqual(len(shared_targets), 2, f"Expected 2 shared targets, found {len(shared_targets)}")

        target_a, target_b = shared_targets[0], shared_targets[1]
        self.assertNotEqual(target_a.target_id, target_b.target_id)
        self.assertEqual(target_a.url, target_b.url)

        # URL string attachment must be rejected to prevent ambiguous tab selection
        with self.assertRaises(ValueError) as ctx:
            await self.engine.attach_target(shared_url)
        self.assertIn("rejected to prevent ambiguous target selection", str(ctx.exception))

        # Attach target A via Target object
        tab_a = await self.engine.attach_target(target_a)
        self.assertEqual(tab_a.target_id, target_a.target_id)
        self.assertTrue(tab_a.is_attached)

        # Attach target B via string target_id
        tab_b = await self.engine.attach_target(target_b.target_id)
        self.assertEqual(tab_b.target_id, target_b.target_id)
        self.assertTrue(tab_b.is_attached)

        self.assertNotEqual(tab_a, tab_b)
        self.assertNotEqual(tab_a.page, tab_b.page)

        # Evaluate unique JS state on each tab to prove total scope isolation
        await self.engine.evaluate(tab_a, "window.__marker = 'TAB_ALPHA'")
        await self.engine.evaluate(tab_b, "window.__marker = 'TAB_BETA'")

        val_a = await self.engine.evaluate(tab_a, "window.__marker")
        val_b = await self.engine.evaluate(tab_b, "window.__marker")
        self.assertEqual(val_a, "TAB_ALPHA")
        self.assertEqual(val_b, "TAB_BETA")

        # Invalid target ID raises TargetNotFoundError
        with self.assertRaises(TargetNotFoundError):
            await self.engine.attach_target("DEADBEEF000000000000000000000000")

    async def test_02_passive_network_observation_header_redaction(self) -> None:
        """Verify sensitive headers are redacted in passive network observation."""
        tab_id = await self._create_test_tab()
        tab = await self.engine.attach_target(tab_id)
        skills = BrowserSkills(tab)
        obs_queue = await skills.observe_network(buffer_size=16)

        # Dispatch fetch containing sensitive request headers and expecting sensitive response headers
        fetch_js = """fetch('/api/data', {
            headers: {
                'Authorization': 'Bearer secret-jwt-token',
                'X-API-Key': 'client-api-key-123',
                'X-Normal-Header': 'public-client-val'
            }
        })"""
        await self.engine.evaluate(tab, fetch_js)

        event = await asyncio.wait_for(obs_queue.get(), timeout=5.0)
        resp_headers = event.get("headers", {})
        req_headers = event.get("request", {}).get("headers", {})

        # Verify response header redaction
        for k, v in resp_headers.items():
            if k.lower() == "set-cookie" or "api-key" in k.lower():
                self.assertEqual(v, REDACTED_HEADER_VALUE, f"Response header {k} not redacted")

        # Verify request header redaction
        for k, v in req_headers.items():
            if k.lower() == "authorization" or "api-key" in k.lower():
                self.assertEqual(v, REDACTED_HEADER_VALUE, f"Request header {k} not redacted")

        # Non-sensitive metadata preserved
        self.assertEqual(event.get("status"), 200)
        self.assertTrue(event.get("ok"))

        await skills.stop_network_observation(obs_queue)

    async def test_03_detached_tab_guards_all_actions(self) -> None:
        """Verify all 11 BrowserSkills action entry points reject detached tabs."""
        tab_id = await self._create_test_tab()
        tab = await self.engine.attach_target(tab_id)
        skills = BrowserSkills(tab)

        # Detach the tab
        await self.engine.detach_target(tab)
        self.assertFalse(tab.is_attached)

        # All 11 action entry points must reject the detached tab with EngineError
        actions = [
            ("aria_snapshot", skills.aria_snapshot()),
            ("accessibility_tree", skills.accessibility_tree()),
            ("interactive_elements", skills.interactive_elements()),
            ("click", skills.click("#btn1")),
            ("type_text", skills.type_text("#input1", "hello")),
            ("canvas_click", skills.canvas_click(10, 10)),
            ("canvas_mouse_move", skills.canvas_mouse_move(10, 10)),
            ("canvas_drag", skills.canvas_drag(10, 10, 20, 20)),
            ("observe_network", skills.observe_network()),
            ("intercept_responses", skills.intercept_responses()),
            ("continue_response", skills.continue_response({"requestId": "dummy_123"})),
        ]

        for name, coro in actions:
            with self.subTest(action=name):
                with self.assertRaises(EngineError) as ctx:
                    await coro
                self.assertIn("is detached", str(ctx.exception))

        # Cleanup methods should execute safely and idempotently without raising
        await skills.close()

    async def test_04_response_interception_teardown(self) -> None:
        """Verify response interception is cleanly deactivated and drained on tab detach."""
        tab_id = await self._create_test_tab()
        tab = await self.engine.attach_target(tab_id)
        skills = BrowserSkills(tab)
        queue = await skills.intercept_responses(buffer_size=16)
        self.assertTrue(skills._interception_active)
        self.assertIsNotNone(skills._interception_queue)

        # Detaching tab must cleanly deactivate interception
        await self.engine.detach_target(tab)
        self.assertFalse(tab.is_attached)
        self.assertFalse(skills._interception_active)
        self.assertIsNone(skills._interception_queue)

    async def test_05_aria_accessibility_and_interactive_elements(self) -> None:
        """Verify aria_snapshot, accessibility_tree, and interactive_elements return valid representations."""
        tab_id = await self._create_test_tab()
        tab = await self.engine.attach_target(tab_id)
        skills = BrowserSkills(tab)

        # 1. aria_snapshot
        snapshot = await skills.aria_snapshot()
        self.assertIsInstance(snapshot, str)
        self.assertTrue(len(snapshot) > 0)
        self.assertIn("Action Button", snapshot)

        # 2. accessibility_tree (CDP fallback)
        ax_tree = await skills.accessibility_tree(depth=8)
        self.assertIsInstance(ax_tree, dict)
        nodes = ax_tree.get("nodes", [])
        self.assertTrue(len(nodes) > 0, "AXTree should contain nodes")

        # 3. interactive_elements
        elements = await skills.interactive_elements()
        self.assertTrue(len(elements) >= 2, "Should discover button and input elements")

        btn = next((el for el in elements if el.selector == "#btn1"), None)
        self.assertIsNotNone(btn, "Interactive elements must find #btn1")
        self.assertEqual(btn.tag, "button")
        self.assertEqual(btn.name, "Action Button")
        self.assertTrue(btn.enabled)
        self.assertTrue(btn.width > 0)
        self.assertTrue(btn.height > 0)

        inp = next((el for el in elements if el.selector == "#input1"), None)
        self.assertIsNotNone(inp, "Interactive elements must find #input1")
        self.assertEqual(inp.tag, "input")
        self.assertTrue(inp.enabled)

    async def test_06_dom_interaction_click_and_type(self) -> None:
        """Verify Playwright click and type_text produce verified DOM side effects in the page."""
        tab_id = await self._create_test_tab()
        tab = await self.engine.attach_target(tab_id)
        skills = BrowserSkills(tab)

        # Check initial state
        btn_clicked_initial = await self.engine.evaluate(tab, "window._btn_clicked")
        self.assertFalse(btn_clicked_initial)

        # Click the button
        await skills.click("#btn1")

        # Verify page state updated
        btn_clicked_after = await self.engine.evaluate(tab, "window._btn_clicked")
        self.assertTrue(btn_clicked_after)
        output_text = await self.engine.evaluate(tab, "document.getElementById('output').innerText")
        self.assertEqual(output_text, "clicked")

        # Type text into empty input
        await skills.type_text("#input2", "typed text")
        inp_value = await self.engine.evaluate(tab, "document.getElementById('input2').value")
        self.assertEqual(inp_value, "typed text")

    async def test_07_canvas_mouse_events_dispatch(self) -> None:
        """Verify CDP canvas_click, canvas_mouse_move, and canvas_drag dispatch verified coordinates."""
        tab_id = await self._create_test_tab()
        tab = await self.engine.attach_target(tab_id)
        skills = BrowserSkills(tab)

        # Get canvas bounding rect in viewport coordinates
        rect = await self.engine.evaluate(tab, "document.getElementById('test_canvas').getBoundingClientRect()")
        cx = rect["x"] + 50
        cy = rect["y"] + 50

        # 1. canvas_click
        await self.engine.evaluate(tab, "window._canvas_events = []")
        await skills.canvas_click(cx, cy)
        events = await self.engine.evaluate(tab, "window._canvas_events")
        event_types = [e["type"] for e in events]
        self.assertIn("mousedown", event_types)
        self.assertIn("mouseup", event_types)
        down = next(e for e in events if e["type"] == "mousedown")
        self.assertAlmostEqual(down["x"], cx, delta=2)
        self.assertAlmostEqual(down["y"], cy, delta=2)

        # 2. canvas_mouse_move
        await self.engine.evaluate(tab, "window._canvas_events = []")
        await skills.canvas_mouse_move(cx + 25, cy + 25)
        events = await self.engine.evaluate(tab, "window._canvas_events")
        move = next((e for e in events if e["type"] == "mousemove"), None)
        self.assertIsNotNone(move, "Canvas should receive mousemove event")
        self.assertAlmostEqual(move["x"], cx + 25, delta=2)
        self.assertAlmostEqual(move["y"], cy + 25, delta=2)

        # 3. canvas_drag
        await self.engine.evaluate(tab, "window._canvas_events = []")
        await skills.canvas_drag(cx, cy, cx + 50, cy, steps=4)
        events = await self.engine.evaluate(tab, "window._canvas_events")
        types = [e["type"] for e in events]
        self.assertIn("mousedown", types)
        self.assertIn("mousemove", types)
        self.assertIn("mouseup", types)
        self.assertEqual(types[0], "mousemove")
        self.assertEqual(types[1], "mousedown")
        up = next(e for e in reversed(events) if e["type"] == "mouseup")
        self.assertAlmostEqual(up["x"], cx + 50, delta=2)

    async def test_08_opt_in_fetch_interception_continued_successfully(self) -> None:
        """Verify real opt-in Fetch interception pauses a response and continues successfully to completion."""
        tab_id = await self._create_test_tab()
        tab = await self.engine.attach_target(tab_id)
        skills = BrowserSkills(tab)

        queue = await skills.intercept_responses(buffer_size=16, timeout_s=10.0)
        self.assertTrue(skills._interception_active)

        # Trigger fetch fire-and-forget so evaluate completes while response is paused
        trigger_js = """
        window._fetch_data = null;
        window._fetch_done = false;
        setTimeout(() => {
            fetch('/api/data')
                .then(r => r.json())
                .then(d => {
                    window._fetch_data = d;
                    window._fetch_done = true;
                })
                .catch(e => {
                    window._fetch_error = String(e);
                    window._fetch_done = true;
                });
        }, 0);
        """
        await self.engine.evaluate(tab, trigger_js)

        # Await paused event from queue
        paused_event = await asyncio.wait_for(queue.get(), timeout=5.0)
        self.assertIn("requestId", paused_event)

        # Verify that response was genuinely paused: fetch has not completed yet
        is_done = await self.engine.evaluate(tab, "window._fetch_done")
        self.assertFalse(is_done, "Fetch must remain paused while held by interception queue")

        # Now continue the paused response exactly once
        await skills.continue_response(paused_event)

        # Poll for fetch completion in the page
        completed = False
        for _ in range(30):
            if await self.engine.evaluate(tab, "window._fetch_done"):
                completed = True
                break
            await asyncio.sleep(0.1)

        self.assertTrue(completed, "Fetch request should complete after continue_response")
        result_data = await self.engine.evaluate(tab, "window._fetch_data")
        self.assertEqual(result_data, {"status": "ok"})

        await skills.stop_response_interception(queue)
        self.assertFalse(skills._interception_active)


if __name__ == "__main__":
    unittest.main()
