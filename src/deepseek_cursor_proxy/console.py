"""Interactive commands typed in the proxy window, like a CLI prompt."""

from __future__ import annotations

from dataclasses import replace
import os
import sys
import threading
from typing import Any, TextIO

from .config import ProxyConfig
from .cursor_effort import read_cursor_efforts
from .logging import configure_logging
from .reasoning_store import ReasoningStore
from .transform import split_cursor_model

_ON_VALUES = {"on", "true", "yes", "1", "enable", "enabled"}
_OFF_VALUES = {"off", "false", "no", "0", "disable", "disabled"}
_FRIENDLY_MODELS = {
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gpt-5.6-terra": "GPT-5.6 Terra",
    "deepseek-v4-pro": "DeepSeek Pro",
    "deepseek-flash": "DeepSeek Flash",
}

HELP_TEXT = """\
/ or help                    show this list
settings                     show settings and cache rows
settings verbose on|off      turn verbose logs on or off
logs on|off                  same as settings verbose
status                       cache rows, model, and urls
clear                        delete local thinking memory
quit                         stop the proxy
A leading / is optional.\
"""


class ProxyStats:
    """Request counters the status command prints."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.request_count = 0
        self.last_model_line = ""

    def note(self, line: str) -> None:
        """Remember one forwarded request."""
        with self._lock:
            self.request_count += 1
            self.last_model_line = line

    def snapshot(self) -> tuple[int, str]:
        """Return the request count and the latest model line."""
        with self._lock:
            return self.request_count, self.last_model_line


def split_command(line: str) -> list[str]:
    """Drop an optional leading slash and split the command on whitespace."""
    text = line.strip()
    if text.startswith("/"):
        text = text[1:].strip()
    return text.split()


def parse_on_off(value: str) -> bool | None:
    """Parse on/off words. Unknown text returns None."""
    lowered = value.strip().lower()
    if lowered in _ON_VALUES:
        return True
    if lowered in _OFF_VALUES:
        return False
    return None


def friendly_model(model: str) -> str:
    """Short display name for a Cursor or DeepSeek model id."""
    return _FRIENDLY_MODELS.get(model.strip().lower(), model.strip())


def format_started_model(original: str, upstream: str, effort: str) -> str:
    """Describe the model that just started and the effort sent upstream."""
    catalog, _suffix_effort = split_cursor_model(original)
    cursor_name = friendly_model(catalog)
    deepseek_name = friendly_model(upstream)
    if cursor_name.casefold() == deepseek_name.casefold():
        shown = deepseek_name
    else:
        shown = f"{cursor_name} → {deepseek_name}"
    shown_effort = effort.strip() or "none"
    return f"started model {shown}, effort {shown_effort}"


def format_cursor_effort_line(config: ProxyConfig) -> str:
    """Show the per-model Cursor Effort map the proxy actually reads."""
    models = read_cursor_efforts(config.cursor_effort_path)
    if not models:
        return "cursor_effort: no Cursor effort stored yet"
    parts = ", ".join(f"{model}={effort}" for model, effort in sorted(models.items()))
    return f"cursor_effort: {parts}"


def format_cache_rows(current: int, maximum: int) -> str:
    """Human line for current reasoning cache rows against the cap."""
    current_text = "?" if current < 0 else f"{current:,}"
    return f"reasoning_cache: {current_text} / {maximum:,} rows"


def format_duration(seconds: int) -> str:
    """Render a cache age limit as days, hours, or seconds."""
    if seconds <= 0:
        return "off"
    if seconds % 86_400 == 0:
        return f"{seconds // 86_400}d"
    if seconds % 3_600 == 0:
        return f"{seconds // 3_600}h"
    return f"{seconds}s"


class ProxyConsole:
    """Reads commands from the terminal while the HTTP server keeps running."""

    def __init__(
        self,
        server: Any,
        store: ReasoningStore,
        *,
        api_base_url: str,
        local_base_url: str,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        self.server = server
        self.store = store
        self.api_base_url = api_base_url
        self.local_base_url = local_base_url
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout

    def write(self, text: str) -> None:
        """Print one command reply and flush it."""
        self.stdout.write(text)
        if not text.endswith("\n"):
            self.stdout.write("\n")
        self.stdout.flush()

    def prompt(self) -> None:
        """Show the input prompt. A piped launcher prints its own prompt."""
        if not self.stdin.isatty():
            return
        self.stdout.write("> ")
        self.stdout.flush()

    def run(self) -> None:
        """Block on stdin until quit. Closing stdin leaves the proxy running."""
        self.write("type help for commands")
        self.prompt()
        try:
            while True:
                line = self.stdin.readline()
                if line == "":
                    return
                if self.handle_line(line) == "quit":
                    self.write("shutting down")
                    self.request_stop()
                    return
                self.prompt()
        except KeyboardInterrupt:
            self.request_stop()

    def request_stop(self) -> None:
        """Unblock serve_forever from this thread."""
        shutdown = getattr(self.server, "shutdown", None)
        if callable(shutdown):
            shutdown()

    def handle_line(self, line: str) -> str:
        """Run one command. Return 'quit' when the proxy should stop."""
        if line.strip() in {"/", "/?"}:
            self.write(HELP_TEXT)
            return "ok"
        parts = split_command(line)
        if not parts:
            return "ok"
        command = parts[0].lower()
        args = parts[1:]
        if command in {"help", "?"}:
            self.write(HELP_TEXT)
        elif command == "settings":
            self._settings(args)
        elif command in {"verbose", "logs"}:
            self._set_verbose_arg(args)
        elif command in {"clear", "clear-thinking", "clear-cache"}:
            self._clear()
        elif command == "status":
            self.write("\n".join(self.settings_lines()))
        elif command in {"quit", "exit", "stop"}:
            return "quit"
        else:
            self.write(f"unknown command: {command}. type help")
        return "ok"

    def settings_lines(self) -> list[str]:
        """Current proxy settings, including cache fill."""
        config: ProxyConfig = self.server.config
        count, last = self._stats()
        lines = [
            f"verbose: {'on' if config.verbose else 'off'}",
            f"thinking: {config.thinking}",
            f"reasoning_effort: {config.reasoning_effort}",
            format_cursor_effort_line(config),
            f"model: {config.upstream_model}",
            f"display_reasoning: {self._display_reasoning(config)}",
            f"missing_reasoning_strategy: {config.missing_reasoning_strategy}",
            format_cache_rows(self.store.row_count(), config.reasoning_cache_max_rows),
            (
                "reasoning_cache_max_age: "
                f"{format_duration(config.reasoning_cache_max_age_seconds)}"
            ),
            f"requests: {count}",
        ]
        if last:
            lines.append(f"last: {last}")
        lines.append(f"api_base_url: {self.api_base_url}")
        lines.append(f"local_base_url: {self.local_base_url}")
        return lines

    def set_verbose(self, enabled: bool) -> None:
        """Turn full request logs on or off without restarting."""
        self.server.config = replace(self.server.config, verbose=enabled)
        configure_logging(verbose=enabled)
        self.write(f"verbose: {'on' if enabled else 'off'}")
        if enabled:
            self.write(
                "verbose logging on; prompts and code may be written to the console"
            )
        self.write(
            format_cache_rows(
                self.store.row_count(),
                self.server.config.reasoning_cache_max_rows,
            )
        )

    def _settings(self, args: list[str]) -> None:
        if not args:
            self.write("\n".join(self.settings_lines()))
            self.write("change logs: settings verbose on|off")
            return
        key = args[0].lower()
        if key in {"verbose", "logs"}:
            self._set_verbose_arg(args[1:])
            return
        self.write(f"unknown setting: {key}. try: settings verbose on")

    def _set_verbose_arg(self, args: list[str]) -> None:
        if not args:
            self.set_verbose(not self.server.config.verbose)
            return
        enabled = parse_on_off(args[0])
        if enabled is None:
            self.write("use on or off. example: settings verbose on")
            return
        self.set_verbose(enabled)

    def _clear(self) -> None:
        """Delete every cached thinking row."""
        deleted = self.store.clear()
        self.write(f"cleared {deleted} reasoning cache row(s)")
        self.write(
            format_cache_rows(
                self.store.row_count(),
                self.server.config.reasoning_cache_max_rows,
            )
        )

    def _stats(self) -> tuple[int, str]:
        stats = getattr(self.server, "stats", None)
        if stats is None:
            return 0, ""
        return stats.snapshot()

    def _display_reasoning(self, config: ProxyConfig) -> str:
        if not config.display_reasoning:
            return "off"
        if config.collapsible_reasoning:
            return "on (collapsible)"
        return "on"


def console_requested(stdin: TextIO | None = None) -> bool:
    """True when a terminal or the launcher asked for the command prompt."""
    stream = stdin if stdin is not None else sys.stdin
    if stream.isatty():
        return True
    flag = os.environ.get("DCP_CONSOLE", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def start_console(
    server: Any,
    store: ReasoningStore,
    *,
    api_base_url: str,
    local_base_url: str,
) -> None:
    """Start the command prompt when stdin is an interactive terminal."""
    if not console_requested():
        return
    console = ProxyConsole(
        server,
        store,
        api_base_url=api_base_url,
        local_base_url=local_base_url,
    )
    threading.Thread(target=console.run, name="proxy-console", daemon=True).start()
