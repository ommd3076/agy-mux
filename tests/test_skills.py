"""Unit tests for browser skills, queue metrics, and endpoint validation."""

import unittest
from core.browser_skills import (
    BoundedMetricQueue,
    redact_headers,
    REDACTED_HEADER_VALUE,
)
from core.engine import BrowserEngine


class TestHeaderRedaction(unittest.TestCase):
    """Verify sensitive header redaction rules."""

    def test_sensitive_headers_redacted(self) -> None:
        headers = {
            "Authorization": "Bearer token-12345",
            "authorization": "Basic dXNlcjpwYXNz",
            "Cookie": "session=abc",
            "Set-Cookie": "auth=xyz; Path=/",
            "set-cookie": "pref=dark",
            "Proxy-Authorization": "Basic cHJveHk6cGFzcw==",
            "X-API-Key": "key-999",
            "x-auth-token": "auth-token-888",
            "X-Secret-Token": "secret-value",
            "X-Bearer-Token": "bearer-data",
        }
        redacted = redact_headers(headers)
        for key in headers:
            self.assertEqual(
                redacted[key],
                REDACTED_HEADER_VALUE,
                f"Header {key!r} should be redacted to {REDACTED_HEADER_VALUE}",
            )

    def test_nonsensitive_headers_preserved(self) -> None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/html",
            "User-Agent": "Mozilla/5.0",
            "Host": "127.0.0.1:9222",
            "Cache-Control": "no-cache",
            "Content-Length": "42",
        }
        redacted = redact_headers(headers)
        for key, value in headers.items():
            self.assertEqual(redacted[key], value)

    def test_empty_and_none(self) -> None:
        self.assertEqual(redact_headers({}), {})
        self.assertEqual(redact_headers(None), {})


class TestBoundedMetricQueue(unittest.TestCase):
    """Verify BoundedMetricQueue drop-oldest behavior and metric accounting."""

    def test_basic_put_and_get(self) -> None:
        queue: BoundedMetricQueue[str] = BoundedMetricQueue(maxsize=3)
        dropped, dropped_item = queue.put_dropping_oldest("item1")
        self.assertFalse(dropped)
        self.assertIsNone(dropped_item)
        self.assertEqual(queue.overflow_count, 0)
        self.assertEqual(queue.dropped_count, 0)
        self.assertEqual(queue.get_nowait(), "item1")

    def test_saturation_drops_oldest(self) -> None:
        queue: BoundedMetricQueue[int] = BoundedMetricQueue(maxsize=3)
        queue.put_dropping_oldest(1)
        queue.put_dropping_oldest(2)
        queue.put_dropping_oldest(3)
        self.assertEqual(queue.overflow_count, 0)
        self.assertEqual(queue.dropped_count, 0)

        # 4th item forces drop of item 1
        dropped, dropped_item = queue.put_dropping_oldest(4)
        self.assertTrue(dropped)
        self.assertEqual(dropped_item, 1)
        self.assertEqual(queue.overflow_count, 1)
        self.assertEqual(queue.dropped_count, 1)

        # Queue contains 2, 3, 4
        items = [queue.get_nowait(), queue.get_nowait(), queue.get_nowait()]
        self.assertEqual(items, [2, 3, 4])
        self.assertTrue(queue.empty())

    def test_continuation_failure_count(self) -> None:
        queue: BoundedMetricQueue[dict[str, str]] = BoundedMetricQueue(maxsize=2)
        self.assertEqual(queue.continuation_failure_count, 0)
        queue.continuation_failure_count += 1
        self.assertEqual(queue.continuation_failure_count, 1)

    def test_invalid_maxsize(self) -> None:
        with self.assertRaises(ValueError):
            BoundedMetricQueue(maxsize=0)
        with self.assertRaises(ValueError):
            BoundedMetricQueue(maxsize=-1)


class TestEngineValidation(unittest.TestCase):
    """Verify engine endpoint and target parameter validation."""

    def test_non_loopback_rejected(self) -> None:
        invalid_endpoints = [
            "http://example.com:9222",
            "http://192.168.1.100:9222",
            "http://10.0.0.1:9222",
            "ftp://127.0.0.1:9222",
        ]
        for ep in invalid_endpoints:
            with self.subTest(endpoint=ep):
                with self.assertRaises(ValueError):
                    BrowserEngine(ep)

    def test_loopback_accepted(self) -> None:
        valid_endpoints = [
            "http://127.0.0.1:9222",
            "http://localhost:9222",
            "http://[::1]:9222",
        ]
        for ep in valid_endpoints:
            with self.subTest(endpoint=ep):
                engine = BrowserEngine(ep)
                self.assertTrue(any(host in engine.endpoint for host in ("127.0.0.1", "localhost", "::1")))


if __name__ == "__main__":
    unittest.main()
