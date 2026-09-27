"""Regression tests for the long-session proxy hang.

A warm reasoning cache used to rewrite portable aliases and prune the whole
SQLite table on every historical tool call, while holding one process-wide
lock. Cursor then sat on "Planning next moves" until the proxy was restarted.
"""

from __future__ import annotations

from io import BytesIO
import json
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest

from deepseek_cursor_proxy.config import ProxyConfig
from deepseek_cursor_proxy.reasoning_store import ReasoningStore, conversation_scope
from deepseek_cursor_proxy.server import DeepSeekProxyHandler
from deepseek_cursor_proxy.transform import (
    prepare_upstream_request,
    reasoning_cache_namespace,
)


def _namespace() -> str:
    return reasoning_cache_namespace(
        ProxyConfig(),
        "deepseek-v4-pro",
        {"type": "enabled"},
        "max",
    )


def _tool_call(index: int) -> dict:
    return {
        "id": f"call_{index}",
        "type": "function",
        "function": {"name": "Read", "arguments": json.dumps({"path": f"f{index}.py"})},
    }


def _handler(wfile: object) -> DeepSeekProxyHandler:
    handler = object.__new__(DeepSeekProxyHandler)
    handler.server = SimpleNamespace(
        config=ProxyConfig(),
        reasoning_store=ReasoningStore(":memory:"),
    )
    handler.wfile = wfile
    handler.close_connection = False
    handler.send_response = lambda status: None
    handler.send_header = lambda name, value: None
    handler.end_headers = lambda: None
    return handler


class ReasoningCacheReliabilityTests(unittest.TestCase):
    def test_long_session_restores_reasoning_without_rewriting_cache(self) -> None:
        namespace = _namespace()
        messages: list[dict] = [{"role": "system", "content": "agent"}]
        with TemporaryDirectory() as temp_dir:
            store = ReasoningStore(f"{temp_dir}/cache.sqlite3", max_rows=100_000)
            try:
                for index in range(12):
                    prior = list(messages)
                    assistant = {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": f"reason {index}",
                        "tool_calls": [_tool_call(index)],
                    }
                    store.store_assistant_message(
                        assistant,
                        conversation_scope(prior, namespace),
                        namespace,
                        prior,
                    )
                    messages.append(assistant)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": f"call_{index}",
                            "content": f"result {index}",
                        }
                    )
                messages.append({"role": "user", "content": "continue"})
                echoed = [
                    (
                        {
                            key: value
                            for key, value in message.items()
                            if key != "reasoning_content"
                        }
                        if message.get("role") == "assistant"
                        else message
                    )
                    for message in messages
                ]
                started = time.perf_counter()
                first = prepare_upstream_request(
                    {"model": "deepseek-v4-pro", "messages": echoed, "stream": True},
                    ProxyConfig(),
                    store,
                )
                queries: list[str] = []
                store._conn.set_trace_callback(queries.append)
                second = prepare_upstream_request(
                    {"model": "deepseek-v4-pro", "messages": echoed, "stream": True},
                    ProxyConfig(),
                    store,
                )
                elapsed = time.perf_counter() - started
            finally:
                store.close()

        self.assertLess(elapsed, 3)
        self.assertFalse(any("INSERT" in query for query in queries))
        restored = [
            message.get("reasoning_content")
            for message in second.payload["messages"]
            if message.get("tool_calls")
        ]
        self.assertEqual(restored, [f"reason {index}" for index in range(12)])
        self.assertEqual(first.missing_reasoning_messages, 0)
        self.assertEqual(second.missing_reasoning_messages, 0)

    def test_sequential_tool_calls_keep_their_own_reasoning(self) -> None:
        namespace = _namespace()
        config = ProxyConfig()
        store = ReasoningStore(":memory:")
        try:
            messages: list[dict] = [{"role": "user", "content": "inspect the repo"}]
            for index in range(3):
                prior = list(messages)
                assistant = {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": f"step {index}",
                    "tool_calls": [_tool_call(index)],
                }
                store.store_assistant_message(
                    assistant,
                    conversation_scope(prior, namespace),
                    namespace,
                    prior,
                )
                messages.extend(
                    [
                        assistant,
                        {
                            "role": "tool",
                            "tool_call_id": f"call_{index}",
                            "content": "ok",
                        },
                    ]
                )
            echoed = [
                (
                    {
                        key: value
                        for key, value in message.items()
                        if key != "reasoning_content"
                    }
                    if message.get("role") == "assistant"
                    else message
                )
                for message in messages
            ]
            prepared = prepare_upstream_request(
                {"model": "deepseek-v4-pro", "messages": echoed},
                config,
                store,
            )
        finally:
            store.close()

        restored = [
            message["reasoning_content"]
            for message in prepared.payload["messages"]
            if message.get("tool_calls")
        ]
        self.assertEqual(restored, ["step 0", "step 1", "step 2"])

    def test_missing_cache_entry_recovers_without_looping(self) -> None:
        messages: list[dict] = []
        for index in range(8):
            messages.extend(
                [
                    {"role": "user", "content": f"turn {index}"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [_tool_call(index)],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": f"call_{index}",
                        "content": "gone",
                    },
                ]
            )
        store = ReasoningStore(":memory:")
        try:
            started = time.perf_counter()
            prepared = prepare_upstream_request(
                {"model": "deepseek-v4-pro", "messages": messages, "stream": True},
                ProxyConfig(),
                store,
            )
            elapsed = time.perf_counter() - started
        finally:
            store.close()

        self.assertLess(elapsed, 2)
        self.assertLessEqual(len(prepared.recovery_steps), 4)
        self.assertTrue(prepared.payload["messages"])

    def test_concurrent_cache_access_finishes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = ReasoningStore(
                f"{temp_dir}/cache.sqlite3",
                max_rows=30,
                lock_timeout_seconds=5,
            )
            errors: list[BaseException] = []

            def hammer(worker: int) -> None:
                try:
                    for index in range(25):
                        key = f"{worker}-{index}"
                        store.put(key, f"reason {key}", {"role": "assistant"})
                        store.get(key)
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=hammer, args=(worker,)) for worker in range(6)
            ]
            try:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=15)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                store.put("final", "ok", {"role": "assistant"})
                self.assertEqual(store.get("final"), "ok")
                count = store._conn.execute(
                    "SELECT COUNT(*) FROM reasoning_cache"
                ).fetchone()
                self.assertLessEqual(int(count[0]), 30)
            finally:
                store.close()

    def test_cache_lock_wait_is_bounded_and_recovers(self) -> None:
        store = ReasoningStore(":memory:", lock_timeout_seconds=0.3)
        started = threading.Event()
        release = threading.Event()

        def hold_lock() -> None:
            store._lock.acquire()
            started.set()
            release.wait(timeout=5)
            store._lock.release()

        thread = threading.Thread(target=hold_lock)
        thread.start()
        self.assertTrue(started.wait(timeout=2))
        try:
            began = time.perf_counter()
            self.assertIsNone(store.get("missing"))
            self.assertLess(time.perf_counter() - began, 2)
        finally:
            release.set()
            thread.join(timeout=2)
            store.close()

        store = ReasoningStore(":memory:")
        try:
            store.put("k", "later", {"role": "assistant"})
            self.assertEqual(store.get("k"), "later")
        finally:
            store.close()

    def test_sqlite_busy_fails_fast_and_later_write_works(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = f"{temp_dir}/cache.sqlite3"
            store = ReasoningStore(path, busy_timeout_seconds=0.3)
            other = sqlite3.connect(path, timeout=0.1, isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                started = time.perf_counter()
                store.put("blocked", "value", {"role": "assistant"})
                self.assertLess(time.perf_counter() - started, 2)
                self.assertIsNone(store.get("blocked"))
                other.execute("ROLLBACK")
                store.put("blocked", "value", {"role": "assistant"})
                self.assertEqual(store.get("blocked"), "value")
            finally:
                other.close()
                store.close()

    def test_prune_skips_full_table_delete_under_the_cap(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = ReasoningStore(f"{temp_dir}/cache.sqlite3", max_rows=100_000)
            try:
                store._conn.executemany(
                    """
                    INSERT INTO reasoning_cache(key, reasoning, message_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    [(f"preload-{index}", "r", "{}", index) for index in range(1500)],
                )
                queries: list[str] = []
                store._conn.set_trace_callback(queries.append)
                started = time.perf_counter()
                store.put("fresh", "kept", {"role": "assistant"})
                elapsed = time.perf_counter() - started
                self.assertLess(elapsed, 2)
                self.assertFalse(any("NOT IN" in query for query in queries))
                self.assertEqual(store.get("fresh"), "kept")
                store.max_rows = 2
                store.put("newer", "kept-too", {"role": "assistant"})
                self.assertIsNone(store.get("preload-0"))
                self.assertEqual(store.get("newer"), "kept-too")
            finally:
                store.close()

    def test_expired_rows_are_pruned(self) -> None:
        store = ReasoningStore(":memory:", max_age_seconds=10)
        try:
            store.put("old", "stale", {"role": "assistant"})
            store._conn.execute(
                "UPDATE reasoning_cache SET created_at = ? WHERE key = ?",
                (time.time() - 60, "old"),
            )
            self.assertGreaterEqual(store.prune(), 1)
            self.assertIsNone(store.get("old"))
        finally:
            store.close()

    def test_existing_database_gains_created_at_index(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = f"{temp_dir}/cache.sqlite3"
            legacy = sqlite3.connect(path)
            legacy.execute(
                """
                CREATE TABLE reasoning_cache (
                    key TEXT PRIMARY KEY,
                    reasoning TEXT NOT NULL,
                    message_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            legacy.execute(
                "INSERT INTO reasoning_cache VALUES (?, ?, ?, ?)",
                ("legacy", "remembered", "{}", time.time()),
            )
            legacy.commit()
            legacy.close()

            store = ReasoningStore(path)
            try:
                indexes = {
                    row[0]
                    for row in store._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }
                self.assertIn("reasoning_cache_created_at_idx", indexes)
                self.assertEqual(store.get("legacy"), "remembered")
            finally:
                store.close()


class StreamReliabilityTests(unittest.TestCase):
    def test_interrupted_stream_terminates_and_keeps_partial_reasoning(self) -> None:
        wfile = BytesIO()
        handler = _handler(wfile)
        chunk = {
            "id": "stream",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "delta": {"reasoning_content": "partial plan"},
                }
            ],
        }
        try:
            result = handler._proxy_streaming_response(
                _Lines([f"data: {json.dumps(chunk)}\n\n".encode("utf-8")]),
                "deepseek-v4-pro",
                [{"role": "user", "content": "go"}],
                "ns",
            )
            body = wfile.getvalue().decode("utf-8")
            self.assertTrue(result.sent)
            self.assertIn("data: [DONE]", body)
            self.assertIn("</details>", body)
            count = handler.reasoning_store._conn.execute(
                "SELECT COUNT(*) FROM reasoning_cache"
            ).fetchone()
            self.assertGreater(int(count[0]), 0)
            handler.reasoning_store.put("after", "ok", {"role": "assistant"})
            self.assertEqual(handler.reasoning_store.get("after"), "ok")
        finally:
            handler.reasoning_store.close()

    def test_upstream_timeout_then_next_request_works(self) -> None:
        wfile = BytesIO()
        handler = _handler(wfile)
        try:
            with self.assertLogs("deepseek_cursor_proxy", level="WARNING") as captured:
                failed = handler._proxy_streaming_response(
                    _RaisingStream(TimeoutError("timed out")),
                    "deepseek-v4-pro",
                    [{"role": "user", "content": "go"}],
                    "ns",
                )
            self.assertTrue(failed.sent)
            self.assertIn("data: [DONE]", wfile.getvalue().decode("utf-8"))
            self.assertIn(
                "upstream streaming response read failed", "\n".join(captured.output)
            )

            follow_up = BytesIO()
            handler.wfile = follow_up
            chunk = {
                "id": "stream-2",
                "model": "deepseek-v4-pro",
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": "done"}}
                ],
            }
            succeeded = handler._proxy_streaming_response(
                _Lines(
                    [
                        f"data: {json.dumps(chunk)}\n\n".encode("utf-8"),
                        b"data: [DONE]\n\n",
                    ]
                ),
                "deepseek-v4-pro",
                [{"role": "user", "content": "go"}],
                "ns",
            )
            self.assertTrue(succeeded.sent)
            self.assertIn("done", follow_up.getvalue().decode("utf-8"))
        finally:
            handler.reasoning_store.close()

    def test_client_cancellation_does_not_wedge_cache(self) -> None:
        handler = _handler(_BrokenPipe())
        chunk = {
            "id": "stream",
            "model": "deepseek-v4-pro",
            "choices": [{"index": 0, "delta": {"content": "hi"}}],
        }
        try:
            result = handler._proxy_streaming_response(
                _Lines([f"data: {json.dumps(chunk)}\n\n".encode("utf-8")]),
                "deepseek-v4-pro",
                [{"role": "user", "content": "go"}],
                "ns",
            )
            self.assertFalse(result.sent)
            handler.reasoning_store.put("after-cancel", "ok", {"role": "assistant"})
            self.assertEqual(handler.reasoning_store.get("after-cancel"), "ok")
        finally:
            handler.reasoning_store.close()


class _Lines:
    status = 200
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _RaisingStream:
    status = 200
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def readline(self) -> bytes:
        raise self._exc


class _BrokenPipe:
    def write(self, body: bytes) -> None:
        raise BrokenPipeError("cancelled")

    def flush(self) -> None:
        raise BrokenPipeError("cancelled")


if __name__ == "__main__":
    unittest.main()
