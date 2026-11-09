"""Asynchronous, target-scoped Chrome DevTools Protocol client."""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import urllib.error
import urllib.request
import websockets
from websockets.asyncio.client import ClientConnection

LOG = logging.getLogger("agy_engine")


class CDPError(RuntimeError):
    """A protocol, transport, or browser-side command failure."""


@dataclass(frozen=True, slots=True)
class Target:
    target_id: str
    type: str
    title: str
    url: str
    websocket_url: str


class CDPSession:
    """A multiplexed CDP session with bounded command waits and event queues."""

    def __init__(self, socket: ClientConnection, *, timeout: float = 15.0) -> None:
        self._socket = socket
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._events: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._send_lock = asyncio.Lock()
        self._reader_task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        try:
            async for message in self._socket:
                payload = json.loads(message)
                command_id = payload.get("id")
                if command_id is not None:
                    future = self._pending.pop(command_id, None)
                    if future is not None and not future.done():
                        future.set_result(payload)
                    continue
                method = payload.get("method")
                if method:
                    for queue in tuple(self._events.get(method, ())):
                        if queue.full():
                            queue.get_nowait()
                        queue.put_nowait(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CDPError(f"CDP connection closed: {exc}"))
            self._pending.clear()

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        command_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[command_id] = future
        packet = {"id": command_id, "method": method, "params": params or {}}
        try:
            async with self._send_lock:
                await self._socket.send(json.dumps(packet, separators=(",", ":")))
            response = await asyncio.wait_for(future, timeout=self._timeout)
        except (TimeoutError, OSError, websockets.ConnectionClosed) as exc:
            self._pending.pop(command_id, None)
            raise CDPError(f"{method} failed or timed out: {exc}") from exc
        if "error" in response:
            error = response["error"]
            raise CDPError(f"{method}: {error.get('message', error)}")
        return response.get("result", {})

    def events(self, method: str, *, max_queue: int = 64) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue)
        self._events.setdefault(method, set()).add(queue)
        return queue

    def remove_events(self, method: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        listeners = self._events.get(method)
        if listeners is not None:
            listeners.discard(queue)
            if not listeners:
                del self._events[method]

    async def close(self) -> None:
        self._reader_task.cancel()
        await asyncio.gather(self._reader_task, return_exceptions=True)
        await self._socket.close()


class BrowserEngine:
    """Connect to a local browser and control explicitly selected page targets."""

    def __init__(self, endpoint: str = "http://127.0.0.1:9222", *, timeout: float = 15.0) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme not in {"http", "https"} or parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("CDP endpoint must use HTTP(S) on loopback")
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def _http_json(self, path: str) -> Any:
        request = urllib.request.Request(f"{self.endpoint}{path}", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise CDPError(f"Could not read CDP endpoint {path}: {exc}") from exc

    async def list_targets(self) -> list[Target]:
        rows = await asyncio.to_thread(self._http_json, "/json/list")
        return [
            Target(
                target_id=row["id"], type=row.get("type", ""), title=row.get("title", ""),
                url=row.get("url", ""), websocket_url=row.get("webSocketDebuggerUrl", ""),
            )
            for row in rows
            if row.get("type") == "page" and row.get("webSocketDebuggerUrl")
        ]

    async def connect_target(self, target: Target) -> CDPSession:
        if not target.websocket_url.startswith(("ws://127.0.0.1", "ws://localhost", "wss://127.0.0.1", "wss://localhost")):
            raise CDPError("Refusing non-loopback target WebSocket")
        socket = await websockets.connect(target.websocket_url, open_timeout=self.timeout, max_size=8 * 1024 * 1024)
        return CDPSession(socket, timeout=self.timeout)

    async def evaluate(self, session: CDPSession, expression: str, *, await_promise: bool = True) -> Any:
        result = await session.send("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
            "userGesture": False,
        })
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"].get("text", "JavaScript evaluation failed")
            raise CDPError(detail)
        return result.get("result", {}).get("value")

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
    except (CDPError, ValueError) as exc:
        parser.exit(2, f"CDP error: {exc}\n")


if __name__ == "__main__":
    main()
