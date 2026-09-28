"""Retries for a dead DeepSeek handshake, before Cursor is answered."""

from __future__ import annotations

from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from deepseek_cursor_proxy.upstream import (
    candidate_addresses,
    open_upstream,
    transient_failure,
)


class _Hold(BaseHTTPRequestHandler):
    """Accepts immediately, then waits before the status line."""

    delay = 1.0

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        time.sleep(self.delay)
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ResetOnce(BaseHTTPRequestHandler):
    """First POST drops the socket. The next one answers."""

    hits = 0

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).hits += 1
        if type(self).hits == 1:
            self.connection.close()
            return
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/chat/completions"


def _request(url: str) -> Request:
    return Request(
        url,
        data=json.dumps({"model": "deepseek-v4-pro"}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )


class UpstreamRetryTests(unittest.TestCase):
    def test_connect_timeout_is_retried_then_succeeds(self) -> None:
        calls = {"n": 0}

        def send(request: Request, timeout: float) -> str:
            del request, timeout
            calls["n"] += 1
            if calls["n"] < 3:
                raise URLError(OSError(10060, "timed out", None, 10060))
            return "ok"

        result = open_upstream(
            _request("http://example/chat/completions"),
            read_timeout=5,
            send=send,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)

    def test_http_400_is_not_retried(self) -> None:
        calls = {"n": 0}

        def send(request: Request, timeout: float) -> str:
            del request, timeout
            calls["n"] += 1
            raise HTTPError(
                "http://example/chat/completions",
                400,
                "bad",
                Message(),
                BytesIO(b"{}"),
            )

        with self.assertRaises(HTTPError) as caught:
            open_upstream(
                _request("http://example/chat/completions"),
                read_timeout=5,
                send=send,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(calls["n"], 1)

    def test_winerror_10060_is_transient(self) -> None:
        exc = URLError(OSError(10060, "timed out", None, 10060))
        self.assertTrue(transient_failure(exc))

    def test_ipv4_is_tried_before_ipv6(self) -> None:
        def fake_getaddrinfo(
            host: str, port: int, family: int, socktype: int
        ) -> list[tuple]:
            del host, family, socktype
            return [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", port, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.5", port)),
            ]

        with patch(
            "deepseek_cursor_proxy.upstream.socket.getaddrinfo", fake_getaddrinfo
        ):
            addresses = candidate_addresses("api.deepseek.com", 443)
        self.assertEqual(addresses[0][0], "203.0.113.5")
        self.assertEqual(addresses[1][0], "2001:db8::1")

    def test_slow_headers_survive_a_short_connect_timeout(self) -> None:
        _Hold.delay = 1.0
        server, url = _serve(_Hold)
        try:
            started = time.perf_counter()
            with open_upstream(
                _request(url), read_timeout=5, connect_timeout=0.3
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
            elapsed = time.perf_counter() - started
            self.assertEqual(body, {"ok": True})
            self.assertGreater(elapsed, 0.8)
        finally:
            server.shutdown()
            server.server_close()

    def test_dropped_connection_is_retried(self) -> None:
        _ResetOnce.hits = 0
        server, url = _serve(_ResetOnce)
        try:
            with open_upstream(_request(url), read_timeout=5, connect_timeout=2) as response:
                body = json.loads(response.read().decode("utf-8"))
            self.assertEqual(body, {"ok": True})
            self.assertEqual(_ResetOnce.hits, 2)
        finally:
            server.shutdown()
            server.server_close()
