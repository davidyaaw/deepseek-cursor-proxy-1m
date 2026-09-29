"""Remember the Effort selected in Cursor.

Cursor's custom OpenAI base URL drops reasoning_effort from the chat
request. A hook writes the picker value here before the request is sent,
and the proxy reads it when the body does not carry an effort.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from .config import default_config_path, default_cursor_effort_path
from .logging import LOG


_EFFORT_PARAM_IDS = {"reasoning", "reasoning_effort", "effort", "thought_level"}
_CONFIG_EFFORT_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra"}
_EFFORT_SUFFIX = re.compile(
    r"(?i)-(none|minimal|low|medium|high|xhigh|max)(?:-fast)?(?:\[1m\])?$"
)
_CONFIG_EFFORT_LINE = re.compile(r"(?m)^reasoning_effort:.*$")
_LOCK_WAIT_SECONDS = 3.0
_LOCK_POLL_SECONDS = 0.05
_REPLACE_RETRIES = 10


def effort_from_hook_payload(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Return (model id, Cursor effort) from a hook event, if both are present."""
    model_id = payload.get("model_id")
    model_name = payload.get("model")
    if isinstance(model_id, str) and model_id.strip():
        model_id = model_id.strip()
    elif isinstance(model_name, str) and model_name.strip():
        model_id = _EFFORT_SUFFIX.sub("", model_name.strip())
    else:
        return None

    params = payload.get("model_params")
    if isinstance(params, list):
        for item in params:
            if not isinstance(item, dict) or item.get("id") not in _EFFORT_PARAM_IDS:
                continue
            value = item.get("value")
            if isinstance(value, str) and value.strip():
                return model_id, value.strip()
    if isinstance(model_name, str):
        match = _EFFORT_SUFFIX.search(model_name.strip())
        if match:
            return model_id, match.group(1)
    return None


def read_cursor_effort(model_id: str, path: Path | None = None) -> str | None:
    """Return the last Cursor Effort stored for this model id."""
    models = read_cursor_efforts(path)
    value = models.get(model_id)
    if value is None:
        value = models.get(model_id.lower())
    return value


def read_cursor_efforts(path: Path | None = None) -> dict[str, str]:
    """Return the full model→effort map the proxy uses for Cursor requests."""
    effort_path = path or default_cursor_effort_path()
    return _load_models(effort_path)


def remember_cursor_effort(
    model_id: str,
    effort: str,
    effort_path: Path | None = None,
    config_path: Path | None = None,
) -> None:
    """Store one model's Cursor Effort and mirror Sol/Terra into config.yaml."""
    path = effort_path or default_cursor_effort_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not _with_effort_lock(path, lambda: _write_model_effort(path, model_id, effort)):
            return
    except OSError as exc:
        LOG.warning("cursor effort save failed: %s", exc)
        return
    if model_id.lower() in _CONFIG_EFFORT_MODELS:
        _update_config_reasoning_effort(config_path or default_config_path(), effort)


def _lock_file_path(effort_path: Path) -> Path:
    """Path of the exclusive lock file sitting next to cursor-effort.json."""
    return effort_path.with_name(effort_path.name + ".lock")


def _with_effort_lock(effort_path: Path, action: Callable[[], None]) -> bool:
    """Run action under an exclusive lock; return False if the lock timed out."""
    lock_path = _lock_file_path(effort_path)
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        try:
            handle = open(lock_path, "a+b")
        except OSError as exc:
            LOG.warning("cursor effort lock open failed: %s", exc)
            return False
        try:
            locked = _try_lock(handle)
        except OSError as exc:
            LOG.warning("cursor effort lock failed: %s", exc)
            handle.close()
            return False
        if locked:
            try:
                action()
            finally:
                _unlock(handle)
            return True
        handle.close()
        if time.monotonic() >= deadline:
            LOG.warning("cursor effort lock timed out for %s", effort_path)
            return False
        time.sleep(_LOCK_POLL_SECONDS)


def _try_lock(handle: BinaryIO) -> bool:
    """Attempt a non-blocking exclusive lock on the open lock file."""
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(handle: BinaryIO) -> None:
    """Release the exclusive lock and close the lock file handle."""
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    handle.close()


def _write_model_effort(path: Path, model_id: str, effort: str) -> None:
    """Read-modify-write one model entry via a unique temp file and os.replace."""
    models = _load_models(path)
    models[model_id] = effort.strip()
    temporary = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"models": models}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _replace_with_retry(temporary, path)


def _replace_with_retry(temporary: Path, path: Path) -> None:
    """Replace the effort file, retrying briefly on Windows PermissionError."""
    last_error: OSError | None = None
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(temporary, path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(_LOCK_POLL_SECONDS * (attempt + 1))
    if last_error is not None:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise last_error


def _load_models(path: Path) -> dict[str, str]:
    """Read the stored model→effort map, or an empty map if it is missing."""
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    models = loaded.get("models") if isinstance(loaded, dict) else None
    if not isinstance(models, dict):
        return {}
    return {
        str(key): value.strip()
        for key, value in models.items()
        if isinstance(value, str) and value.strip()
    }


def _update_config_reasoning_effort(config_path: Path, effort: str) -> None:
    """Replace the reasoning_effort line so the config shows Cursor's selection."""
    if not config_path.is_file():
        return
    text = config_path.read_text(encoding="utf-8")
    line = f"reasoning_effort: {effort.strip()}"
    updated, count = _CONFIG_EFFORT_LINE.subn(line, text, count=1)
    if count == 0:
        if updated and not updated.endswith("\n"):
            updated += "\n"
        updated += line + "\n"
    if updated != text:
        config_path.write_text(updated, encoding="utf-8")
