"""Cursor hook: save the selected Effort before the chat request is sent."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .cursor_effort import effort_from_hook_payload, remember_cursor_effort


def decode_hook_stdin(data: bytes) -> str:
    """Decode hook stdin as UTF-8. PowerShell sends UTF-8 while Python's stdin is cp1251."""
    return data.decode("utf-8-sig")


def load_hook_payload(raw: str) -> dict[str, Any]:
    """Parse a hook event. Windows PowerShell prefixes the JSON with a UTF-8 BOM."""
    text = raw.lstrip("\ufeff").strip()
    if not text:
        return {}
    payload = json.loads(text)
    if not isinstance(payload, dict):
        return {}
    return payload


def hook_response(
    payload: dict[str, Any],
    effort_path: Path | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Persist Effort, then allow the prompt to continue."""
    found = effort_from_hook_payload(payload)
    if found is not None:
        remember_cursor_effort(*found, effort_path, config_path)
    if payload.get("hook_event_name") == "beforeSubmitPrompt":
        return {"continue": True}
    return {}


def main() -> int:
    """Read one hook event from stdin and answer on stdout."""
    response: dict[str, Any] = {"continue": True}
    try:
        raw = decode_hook_stdin(sys.stdin.buffer.read())
        response = hook_response(load_hook_payload(raw))
    except Exception as exc:
        sys.stderr.write(f"deepseek-cursor-proxy effort hook: {exc}\n")
        response = {"continue": True}
    sys.stdout.write(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
