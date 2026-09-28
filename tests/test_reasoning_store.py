from __future__ import annotations

from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import time
import unittest

from deepseek_cursor_proxy.reasoning_store import ReasoningStore, conversation_scope


class ReasoningStoreTests(unittest.TestCase):
    def test_file_store_creates_private_database_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            reasoning_content_path = (
                Path(temp_dir) / "nested" / "reasoning_content.sqlite3"
            )

            store = ReasoningStore(reasoning_content_path)
            store.close()

            self.assertTrue(reasoning_content_path.exists())
            self.assertEqual(stat.S_IMODE(reasoning_content_path.stat().st_mode), 0o600)

    def test_store_prunes_to_max_rows_and_can_clear(self) -> None:
        store = ReasoningStore(":memory:", max_rows=2)
        try:
            store.put("a", "reasoning a", {"role": "assistant"})
            store.put("b", "reasoning b", {"role": "assistant"})
            store.put("c", "reasoning c", {"role": "assistant"})

            self.assertIsNone(store.get("a"))
            self.assertEqual(store.get("b"), "reasoning b")
            self.assertEqual(store.get("c"), "reasoning c")
            self.assertEqual(store.clear(), 2)
            self.assertIsNone(store.get("b"))
            self.assertIsNone(store.get("c"))
        finally:
            store.close()

    def test_empty_reasoning_content_is_stored_as_present_value(self) -> None:
        store = ReasoningStore(":memory:")
        try:
            scope = conversation_scope([{"role": "user", "content": "lookup"}])
            tool_call = {
                "id": "call_empty",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }
            message = {
                "role": "assistant",
                "content": "",
                "reasoning_content": "",
                "tool_calls": [tool_call],
            }

            self.assertGreater(store.store_assistant_message(message, scope), 0)
            self.assertEqual(store.get(f"scope:{scope}:tool_call:call_empty"), "")
            self.assertEqual(
                store.lookup_for_message(
                    {"role": "assistant", "content": "", "tool_calls": [tool_call]},
                    scope,
                ),
                "",
            )
        finally:
            store.close()

    def test_inflight_agent_is_not_pruned(self) -> None:
        """A running agent keeps its reasoning even when the row cap is exceeded."""
        store = ReasoningStore(
            ":memory:", max_rows=1, max_age_seconds=30, active_hold_seconds=0
        )
        try:
            store.store_assistant_message(
                _assistant("live"), "scope-live", root="agent-live"
            )
            store.acquire("agent-live")
            store._conn.execute(
                "UPDATE reasoning_cache SET created_at = ? WHERE lineage = ?",
                (time.time() - 10_000, "agent-live"),
            )
            store.put("noise", "drop", {"role": "assistant"})
            row = store._conn.execute(
                "SELECT reasoning FROM reasoning_cache WHERE lineage = ?",
                ("agent-live",),
            ).fetchone()
            self.assertEqual(row[0], "live")
            self.assertIsNone(store.get("noise"))
        finally:
            store.release("agent-live")
            store.close()

    def test_recent_chat_is_kept_after_the_request_finishes(self) -> None:
        """The parent chat stays pinned after its request returns."""
        store = ReasoningStore(
            ":memory:",
            max_rows=1,
            max_age_seconds=30,
            active_hold_seconds=3600,
        )
        try:
            store.store_assistant_message(
                _assistant("current"), "scope-current", root="agent-current"
            )
            store._conn.execute(
                "UPDATE reasoning_cache SET created_at = ? WHERE lineage = ?",
                (time.time() - 10_000, "agent-current"),
            )
            store.put("noise", "drop", {"role": "assistant"})
            row = store._conn.execute(
                "SELECT reasoning FROM reasoning_cache WHERE lineage = ?",
                ("agent-current",),
            ).fetchone()
            self.assertEqual(row[0], "current")
        finally:
            store.close()

    def test_idle_completed_chat_is_pruned(self) -> None:
        """Only chats idle past the hold window are eligible for cleanup."""
        store = ReasoningStore(":memory:", max_rows=1, active_hold_seconds=60)
        try:
            store.store_assistant_message(
                _assistant("old"), "scope-old", root="agent-old"
            )
            store._conn.execute(
                "UPDATE reasoning_lineage SET last_seen = ? WHERE lineage = ?",
                (time.time() - 3600, "agent-old"),
            )
            store.put("fresh", "new", {"role": "assistant"})
            row = store._conn.execute(
                "SELECT reasoning FROM reasoning_cache WHERE lineage = ?",
                ("agent-old",),
            ).fetchone()
            self.assertIsNone(row)
            self.assertEqual(store.get("fresh"), "new")
        finally:
            store.close()


def _assistant(reasoning: str) -> dict:
    """One cached tool-call turn."""
    return {
        "role": "assistant",
        "content": "",
        "reasoning_content": reasoning,
        "tool_calls": [
            {
                "id": "call_0",
                "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
