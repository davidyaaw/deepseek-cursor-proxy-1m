"""Console commands typed while the proxy is running."""

from __future__ import annotations

from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from deepseek_cursor_proxy.config import ProxyConfig
from deepseek_cursor_proxy.console import (
    ProxyConsole,
    ProxyStats,
    console_requested,
    format_cache_rows,
    format_started_model,
)
from deepseek_cursor_proxy.logging import configure_logging
from deepseek_cursor_proxy.reasoning_store import ReasoningStore


class _Server:
    def __init__(self, config: ProxyConfig) -> None:
        self.config = config
        self.stats = ProxyStats()
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class ConsoleCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.effort_path = Path(self._temp.name) / "cursor-effort.json"
        self.store = ReasoningStore(":memory:", max_rows=100)
        self.server = _Server(
            ProxyConfig(
                verbose=False,
                reasoning_cache_max_rows=100,
                thinking="enabled",
                cursor_effort_path=self.effort_path,
            )
        )
        self.stdout = StringIO()
        self.console = ProxyConsole(
            self.server,
            self.store,
            api_base_url="https://example.ngrok.app/v1",
            local_base_url="http://127.0.0.1:9000/v1",
            stdin=StringIO(""),
            stdout=self.stdout,
        )

    def tearDown(self) -> None:
        configure_logging(verbose=False)
        self.store.close()
        self._temp.cleanup()

    def output(self) -> str:
        return self.stdout.getvalue()

    def test_settings_verbose_on_enables_logs_and_shows_cache(self) -> None:
        self.store.put("a", "thinking", {"role": "assistant"})

        self.assertEqual(self.console.handle_line("/settings verbose on"), "ok")

        self.assertTrue(self.server.config.verbose)
        text = self.output()
        self.assertIn("verbose: on", text)
        self.assertIn("reasoning_cache: 1 / 100 rows", text)
        self.assertIn("prompts and code may be written", text)

    def test_settings_shows_fill_level_and_toggle_hint(self) -> None:
        self.console.handle_line("settings")

        text = self.output()
        self.assertIn("verbose: off", text)
        self.assertIn("reasoning_cache: 0 / 100 rows", text)
        self.assertIn("settings verbose on|off", text)
        self.assertIn("https://example.ngrok.app/v1", text)
        self.assertIn("cursor_effort: no Cursor effort stored yet", text)
        self.assertIn("reasoning_effort:", text)

    def test_settings_shows_per_model_cursor_effort_map(self) -> None:
        self.effort_path.write_text(
            json.dumps(
                {"models": {"gpt-5.6-sol": "max", "gpt-5.6-terra": "medium"}}
            ),
            encoding="utf-8",
        )

        self.console.handle_line("settings")

        text = self.output()
        self.assertIn("cursor_effort: gpt-5.6-sol=max, gpt-5.6-terra=medium", text)
        self.assertIn(f"reasoning_effort: {self.server.config.reasoning_effort}", text)

    def test_logs_off_disables_verbose(self) -> None:
        self.console.set_verbose(True)
        self.stdout.seek(0)
        self.stdout.truncate()

        self.console.handle_line("logs off")

        self.assertFalse(self.server.config.verbose)
        self.assertIn("verbose: off", self.output())

    def test_clear_deletes_thinking_rows(self) -> None:
        self.store.put("a", "thinking", {"role": "assistant"})

        self.console.handle_line("clear")

        self.assertEqual(self.store.row_count(), 0)
        self.assertIn("cleared 1 reasoning cache row(s)", self.output())
        self.assertIn("reasoning_cache: 0 / 100 rows", self.output())

    def test_status_includes_last_model_line(self) -> None:
        self.server.stats.note("started model GPT-5.6 Sol → DeepSeek Pro, effort high")

        self.console.handle_line("status")

        self.assertIn("requests: 1", self.output())
        self.assertIn("GPT-5.6 Sol → DeepSeek Pro", self.output())

    def test_slash_alone_lists_commands(self) -> None:
        self.assertEqual(self.console.handle_line("/"), "ok")
        self.assertIn("settings verbose on|off", self.output())
        self.assertIn("clear", self.output())

    def test_eof_leaves_the_proxy_running(self) -> None:
        self.console.stdin = StringIO("")
        self.console.run()
        self.assertEqual(self.server.shutdown_calls, 0)

    def test_quit_stops_without_running_as_unknown(self) -> None:
        stdin = StringIO("quit\n")
        self.console.stdin = stdin

        self.console.run()

        self.assertEqual(self.server.shutdown_calls, 1)
        self.assertIn("shutting down", self.output())

    def test_unknown_command(self) -> None:
        self.console.handle_line("nope")
        self.assertIn("unknown command: nope", self.output())

    def test_piped_stdin_requests_console_when_launcher_asks(self) -> None:
        previous = os.environ.get("DCP_CONSOLE")
        os.environ["DCP_CONSOLE"] = "1"
        try:
            self.assertTrue(console_requested(StringIO("")))
        finally:
            if previous is None:
                os.environ.pop("DCP_CONSOLE", None)
            else:
                os.environ["DCP_CONSOLE"] = previous


class ConsoleFormatTests(unittest.TestCase):
    def test_started_model_names_sol_and_pro(self) -> None:
        self.assertEqual(
            format_started_model("gpt-5.6-sol-high", "deepseek-v4-pro", "high"),
            "started model GPT-5.6 Sol → DeepSeek Pro, effort high",
        )

    def test_started_model_hides_duplicate_name(self) -> None:
        self.assertEqual(
            format_started_model("deepseek-v4-pro", "deepseek-v4-pro", "max"),
            "started model DeepSeek Pro, effort max",
        )

    def test_cache_rows_marks_unknown_count(self) -> None:
        self.assertEqual(format_cache_rows(-1, 100_000), "reasoning_cache: ? / 100,000 rows")
