"""Install a user-level Cursor hook that saves Effort from every workspace."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


_HOOK_EVENTS = ("beforeSubmitPrompt", "sessionStart", "subagentStart")


def default_user_hooks_path() -> Path:
    """Return ~/.cursor/hooks.json where Cursor loads user-level hooks."""
    return Path.home() / ".cursor" / "hooks.json"


def resolve_hook_python(repo_root: Path) -> Path:
    """Prefer the repo venv interpreter when present, else this process's Python."""
    venv_python = repo_root / ".venv" / "Scripts" / "python.exe"
    if venv_python.is_file():
        return venv_python.resolve()
    return Path(sys.executable).resolve()


def quote_shell_path(path: Path) -> str:
    """Quote a filesystem path for a Cursor hook command string."""
    return '"' + str(path).replace('"', '\\"') + '"'


def cursor_effort_hook_command(repo_root: Path, python: Path | None = None) -> str:
    """Build the absolute hook command that works from any workspace cwd."""
    interpreter = (python or resolve_hook_python(repo_root)).resolve()
    script = (repo_root / ".cursor" / "hooks" / "cursor_effort.py").resolve()
    return f"{quote_shell_path(interpreter)} {quote_shell_path(script)}"


def install_user_cursor_effort_hook(
    repo_root: Path,
    hooks_path: Path | None = None,
    python: Path | None = None,
) -> Path:
    """Merge the Effort hook into the user hooks file without duplicating commands."""
    path = hooks_path or default_user_hooks_path()
    command = cursor_effort_hook_command(repo_root, python)
    document = _load_hooks_document(path)
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        document["hooks"] = hooks
    document["version"] = 1
    for event in _HOOK_EVENTS:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
            hooks[event] = entries
        if not _command_already_registered(entries, command):
            entries.append({"command": command})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _load_hooks_document(path: Path) -> dict[str, Any]:
    """Load an existing hooks.json, or start a version-1 empty document."""
    if not path.is_file():
        return {"version": 1, "hooks": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "hooks": {}}
    if not isinstance(loaded, dict):
        return {"version": 1, "hooks": {}}
    return loaded


def _command_already_registered(entries: list[Any], command: str) -> bool:
    """True when this exact hook command is already listed for the event."""
    for entry in entries:
        if isinstance(entry, dict) and entry.get("command") == command:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    """Install the user-level Effort hook for this repository."""
    args = argv if argv is not None else sys.argv[1:]
    repo_root = Path(__file__).resolve().parents[2]
    hooks_path: Path | None = None
    if args:
        hooks_path = Path(args[0])
    path = install_user_cursor_effort_hook(repo_root, hooks_path=hooks_path)
    print(f"Installed Cursor Effort hook in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
