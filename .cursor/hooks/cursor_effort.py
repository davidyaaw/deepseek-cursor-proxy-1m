"""Launch the Effort hook with the project virtualenv when the system Python is older."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _project_python(root: Path) -> Path | None:
    """Prefer the project's 3.10+ interpreter over an older system Python."""
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    """Forward the hook event to the package entry point."""
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    python = _project_python(root)
    if python is not None and Path(sys.executable) != python:
        completed = subprocess.run(
            [str(python), "-m", "deepseek_cursor_proxy.cursor_effort_hook"],
            input=sys.stdin.buffer.read(),
            cwd=str(root),
            env=env,
        )
        return completed.returncode
    sys.path.insert(0, str(root / "src"))
    from deepseek_cursor_proxy.cursor_effort_hook import main as hook_main

    return hook_main()


if __name__ == "__main__":
    raise SystemExit(main())
