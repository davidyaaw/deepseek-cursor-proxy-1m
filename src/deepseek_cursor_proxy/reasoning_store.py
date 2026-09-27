from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator

from .logging import LOG, request_id


def normalize_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function") or {}
    if not isinstance(function, dict):
        function = {}

    arguments = function.get("arguments", "")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True)

    normalized: dict[str, Any] = {
        "id": tool_call.get("id"),
        "type": tool_call.get("type") or "function",
        "function": {
            "name": function.get("name") or "",
            "arguments": arguments,
        },
    }
    return normalized


def tool_call_signature(tool_call: dict[str, Any]) -> str:
    normalized = normalize_tool_call(tool_call)
    normalized.pop("id", None)
    canonical = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def tool_call_ids(message: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for tool_call in message.get("tool_calls") or []:
        if isinstance(tool_call, dict) and tool_call.get("id"):
            ids.append(str(tool_call["id"]))
    return ids


def tool_call_names(message: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function")
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"]))
    return names


def message_signature(message: dict[str, Any]) -> str:
    tool_calls = [
        normalize_tool_call(tool_call)
        for tool_call in (message.get("tool_calls") or [])
        if isinstance(tool_call, dict)
    ]
    payload = {
        "content": message.get("content") or "",
        "tool_calls": tool_calls,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_json(payload: Any) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_scope_message(message: dict[str, Any]) -> dict[str, Any]:
    canonical: dict[str, Any] = {"role": message.get("role")}
    for key in ("content", "name", "tool_call_id", "prefix"):
        if key in message:
            canonical[key] = message[key]
    if message.get("tool_calls"):
        canonical["tool_calls"] = [
            normalize_tool_call(tool_call)
            for tool_call in message.get("tool_calls") or []
            if isinstance(tool_call, dict)
        ]
    return canonical


def conversation_scope(messages: list[dict[str, Any]], namespace: str = "") -> str:
    scope_messages = [canonical_scope_message(message) for message in messages]
    payload: Any = scope_messages
    if namespace:
        payload = {"namespace": namespace, "messages": scope_messages}
    return _sha256_json(payload)


def turn_context_signature(prior_messages: list[dict[str, Any]]) -> str:
    last_user_index = next(
        (
            index
            for index in range(len(prior_messages) - 1, -1, -1)
            if prior_messages[index].get("role") == "user"
        ),
        -1,
    )
    start_index = 0
    if last_user_index != -1:
        start_index = last_user_index
        while start_index > 0 and prior_messages[start_index - 1].get("role") == "user":
            start_index -= 1

    context_messages = [
        canonical_scope_message(message)
        for message in prior_messages[start_index:]
        if message.get("role") != "system"
    ]
    return _sha256_json(context_messages)


def scoped_reasoning_keys(message: dict[str, Any], scope: str) -> list[str]:
    keys = [f"scope:{scope}:signature:{message_signature(message)}"]
    keys.extend(
        f"scope:{scope}:tool_call:{tool_call_id}"
        for tool_call_id in tool_call_ids(message)
    )
    keys.extend(
        f"scope:{scope}:tool_call_signature:{tool_call_signature(tool_call)}"
        for tool_call in (message.get("tool_calls") or [])
        if isinstance(tool_call, dict)
    )
    # Recovery-of-last-resort key. Catches the case where a streaming response
    # was interrupted (user pressed Stop) before the tool_call.id chunk arrived,
    # so neither tool_call_id nor tool_call_signature (which canonicalizes
    # arguments) survives the round-trip through Cursor's transcript.
    keys.extend(
        f"scope:{scope}:tool_name:{tool_name}" for tool_name in tool_call_names(message)
    )
    return keys


def portable_reasoning_keys(
    message: dict[str, Any],
    cache_namespace: str,
    prior_messages: list[dict[str, Any]],
) -> list[str]:
    if not cache_namespace:
        return []

    turn_signature = turn_context_signature(prior_messages)
    keys = [
        f"namespace:{cache_namespace}:turn:{turn_signature}:"
        f"signature:{message_signature(message)}"
    ]
    keys.extend(
        f"namespace:{cache_namespace}:turn:{turn_signature}:"
        f"tool_call:{tool_call_id}"
        for tool_call_id in tool_call_ids(message)
    )
    keys.extend(
        f"namespace:{cache_namespace}:turn:{turn_signature}:"
        f"tool_call_signature:{tool_call_signature(tool_call)}"
        for tool_call in (message.get("tool_calls") or [])
        if isinstance(tool_call, dict)
    )
    keys.extend(
        f"namespace:{cache_namespace}:turn:{turn_signature}:" f"tool_name:{tool_name}"
        for tool_name in tool_call_names(message)
    )
    return keys


class ReasoningStoreBusy(Exception):
    """Raised when the cache lock is still held after the wait limit."""


class ReasoningStore:
    """SQLite cache of DeepSeek reasoning_content, safe to share across requests."""

    def __init__(
        self,
        reasoning_content_path: str | Path,
        max_age_seconds: int | None = None,
        max_rows: int | None = None,
        *,
        busy_timeout_seconds: float = 5.0,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        self.max_age_seconds = max_age_seconds
        self.max_rows = max_rows
        self._busy_timeout_seconds = busy_timeout_seconds
        self._lock_timeout_seconds = lock_timeout_seconds
        if str(reasoning_content_path) == ":memory:":
            self.reasoning_content_path: str | Path = ":memory:"
        else:
            self.reasoning_content_path = Path(reasoning_content_path).expanduser()
            self.reasoning_content_path.parent.mkdir(
                mode=0o700, parents=True, exist_ok=True
            )
        self._lock = threading.RLock()
        self._conn = self._connect()
        self.prune()

    def _connect(self) -> sqlite3.Connection:
        """Open the cache in autocommit mode so a failed write cannot stick."""
        conn = sqlite3.connect(
            self.reasoning_content_path,
            timeout=self._busy_timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        self._configure(conn)
        if isinstance(self.reasoning_content_path, Path):
            self.reasoning_content_path.chmod(0o600)
        return conn

    def _configure(self, conn: sqlite3.Connection) -> None:
        """Enable WAL, a bounded lock wait, and the cache schema."""
        busy_ms = max(int(self._busy_timeout_seconds * 1000), 0)
        conn.execute(f"PRAGMA busy_timeout = {busy_ms}")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error as exc:
            LOG.warning("reasoning cache WAL mode unavailable: %s", exc)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reasoning_cache (
                key TEXT PRIMARY KEY,
                reasoning TEXT NOT NULL,
                message_json TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS reasoning_cache_created_at_idx
            ON reasoning_cache(created_at)
            """
        )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Take the cache lock, but give up so one writer cannot stall every request."""
        acquired = self._lock.acquire(timeout=self._lock_timeout_seconds)
        if not acquired:
            raise ReasoningStoreBusy("reasoning cache lock timed out")
        try:
            yield
        finally:
            self._lock.release()

    def _recover_locked(self) -> None:
        """Drop a failed transaction, reopening the file if the connection is dead."""
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        try:
            self._conn.execute("SELECT 1")
        except sqlite3.Error:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = self._connect()

    def _fail_locked(self, operation: str, exc: sqlite3.Error) -> None:
        """Log a cache failure and leave the connection usable for the next request."""
        LOG.warning(
            "reasoning_cache_%s id=%s error=%s",
            operation,
            request_id(),
            exc,
        )
        self._recover_locked()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            self._conn.close()

    def put(self, key: str, reasoning: str, message: dict[str, Any]) -> None:
        if not isinstance(reasoning, str):
            return
        message_json = json.dumps(message, ensure_ascii=False, sort_keys=True)
        self._put_many([(key, reasoning, message_json, time.time())])

    def _put_many(self, rows: list[tuple[str, str, str, float]]) -> int:
        """Write many cache keys in one transaction and prune once."""
        if not rows:
            return 0
        try:
            with self._locked():
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    self._conn.executemany(
                        """
                        INSERT INTO reasoning_cache(
                            key, reasoning, message_json, created_at
                        )
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            reasoning = excluded.reasoning,
                            message_json = excluded.message_json,
                            created_at = excluded.created_at
                        """,
                        rows,
                    )
                    self._prune_locked()
                    self._conn.execute("COMMIT")
                except sqlite3.Error as exc:
                    self._fail_locked("write", exc)
                    return 0
        except ReasoningStoreBusy:
            LOG.warning("reasoning_cache_write id=%s lock_timeout", request_id())
            return 0
        return len(rows)

    def get(self, key: str) -> str | None:
        row = None
        try:
            with self._locked():
                try:
                    cursor = self._conn.execute(
                        "SELECT reasoning FROM reasoning_cache WHERE key = ?",
                        (key,),
                    )
                    try:
                        row = cursor.fetchone()
                    finally:
                        cursor.close()
                except sqlite3.Error as exc:
                    self._fail_locked("read", exc)
                    return None
        except ReasoningStoreBusy:
            LOG.warning("reasoning_cache_read id=%s lock_timeout", request_id())
            return None
        if row is None:
            return None
        return str(row[0])

    def store_assistant_message(
        self,
        message: dict[str, Any],
        scope: str,
        cache_namespace: str = "",
        prior_messages: list[dict[str, Any]] | None = None,
    ) -> int:
        if message.get("role") != "assistant":
            return 0
        reasoning = message.get("reasoning_content")
        if not isinstance(reasoning, str):
            return 0

        keys = scoped_reasoning_keys(message, scope)
        if prior_messages is not None:
            keys.extend(
                portable_reasoning_keys(message, cache_namespace, prior_messages)
            )
        keys = list(dict.fromkeys(keys))
        message_json = json.dumps(message, ensure_ascii=False, sort_keys=True)
        created_at = time.time()
        return self._put_many(
            [(key, reasoning, message_json, created_at) for key in keys]
        )

    def lookup_for_message(
        self,
        message: dict[str, Any],
        scope: str,
        cache_namespace: str = "",
        prior_messages: list[dict[str, Any]] | None = None,
    ) -> str | None:
        keys = scoped_reasoning_keys(message, scope)
        if prior_messages is not None:
            keys.extend(
                portable_reasoning_keys(message, cache_namespace, prior_messages)
            )
        for key in keys:
            reasoning = self.get(key)
            if reasoning is not None:
                return reasoning
        return None

    def backfill_portable_aliases(
        self,
        message: dict[str, Any],
        reasoning: str,
        cache_namespace: str,
        prior_messages: list[dict[str, Any]],
    ) -> int:
        if not isinstance(reasoning, str):
            return 0
        keys = list(
            dict.fromkeys(
                portable_reasoning_keys(message, cache_namespace, prior_messages)
            )
        )
        if not keys:
            return 0
        # Scoped keys are consulted first, so a warm cache would otherwise
        # rewrite the same portable aliases on every later agent turn.
        if self.get(keys[0]) is not None:
            return 0
        message_with_reasoning = dict(message)
        message_with_reasoning["reasoning_content"] = reasoning
        message_json = json.dumps(
            message_with_reasoning, ensure_ascii=False, sort_keys=True
        )
        created_at = time.time()
        return self._put_many(
            [(key, reasoning, message_json, created_at) for key in keys]
        )

    def clear(self) -> int:
        try:
            with self._locked():
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    row = self._conn.execute(
                        "SELECT COUNT(*) FROM reasoning_cache"
                    ).fetchone()
                    count = int(row[0] if row else 0)
                    self._conn.execute("DELETE FROM reasoning_cache")
                    self._conn.execute("COMMIT")
                    return count
                except sqlite3.Error as exc:
                    self._fail_locked("clear", exc)
                    return 0
        except ReasoningStoreBusy:
            LOG.warning("reasoning_cache_clear id=%s lock_timeout", request_id())
            return 0

    def prune(self) -> int:
        try:
            with self._locked():
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    deleted = self._prune_locked()
                    self._conn.execute("COMMIT")
                    return deleted
                except sqlite3.Error as exc:
                    self._fail_locked("prune", exc)
                    return 0
        except ReasoningStoreBusy:
            LOG.warning("reasoning_cache_prune id=%s lock_timeout", request_id())
            return 0

    def _prune_locked(self) -> int:
        """Drop expired rows, and overflow rows only when the table is over the cap.

        The old ``DELETE ... NOT IN (SELECT ... LIMIT)`` sorted the whole table
        on every write while holding the process-wide lock. On a warm cache that
        stalled every later Cursor request before it reached DeepSeek.
        """
        deleted = 0
        if self.max_age_seconds is not None and self.max_age_seconds > 0:
            cutoff = time.time() - self.max_age_seconds
            cursor = self._conn.execute(
                "DELETE FROM reasoning_cache WHERE created_at < ?",
                (cutoff,),
            )
            try:
                deleted += cursor.rowcount if cursor.rowcount != -1 else 0
            finally:
                cursor.close()

        if self.max_rows is None or self.max_rows <= 0:
            return deleted

        cursor = self._conn.execute(
            """
            SELECT 1 FROM reasoning_cache
            ORDER BY created_at ASC
            LIMIT 1 OFFSET ?
            """,
            (self.max_rows,),
        )
        try:
            over_cap = cursor.fetchone() is not None
        finally:
            cursor.close()
        if not over_cap:
            return deleted

        cursor = self._conn.execute(
            """
            DELETE FROM reasoning_cache
            WHERE rowid IN (
                SELECT rowid FROM reasoning_cache
                ORDER BY created_at ASC
                LIMIT (SELECT COUNT(*) - ? FROM reasoning_cache)
            )
            """,
            (self.max_rows,),
        )
        try:
            deleted += cursor.rowcount if cursor.rowcount != -1 else 0
        finally:
            cursor.close()
        return deleted
