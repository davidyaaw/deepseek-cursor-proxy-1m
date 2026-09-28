"""Cursor hook: save the selected Effort before the chat request is sent."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .cursor_effort import effort_from_hook_payload, remember_cursor_effort


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
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            response = hook_response(payload)
    except Exception:
        response = {"continue": True}
    sys.stdout.write(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
