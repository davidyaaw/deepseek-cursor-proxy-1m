"""Remember the Effort selected in Cursor.

Cursor's custom OpenAI base URL drops reasoning_effort from the chat
request. A hook writes the picker value here before the request is sent,
and the proxy reads it when the body does not carry an effort.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import default_config_path, default_cursor_effort_path


_EFFORT_PARAM_IDS = {"reasoning", "reasoning_effort", "effort", "thought_level"}
_CONFIG_EFFORT_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra"}
_EFFORT_SUFFIX = re.compile(
    r"(?i)-(none|minimal|low|medium|high|xhigh|max)(?:-fast)?(?:\[1m\])?$"
)
_CONFIG_EFFORT_LINE = re.compile(r"(?m)^reasoning_effort:.*$")


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
    effort_path = path or default_cursor_effort_path()
    if not effort_path.is_file():
        return None
    try:
        loaded = json.loads(effort_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    models = loaded.get("models") if isinstance(loaded, dict) else None
    if not isinstance(models, dict):
        return None
    value = models.get(model_id)
    if not isinstance(value, str) or not value.strip():
        value = models.get(model_id.lower())
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def remember_cursor_effort(
    model_id: str,
    effort: str,
    effort_path: Path | None = None,
    config_path: Path | None = None,
) -> None:
    """Store one model's Cursor Effort and mirror Sol/Terra into config.yaml."""
    path = effort_path or default_cursor_effort_path()
    models = _load_models(path)
    models[model_id] = effort.strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"models": models}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    if model_id.lower() in _CONFIG_EFFORT_MODELS:
        _update_config_reasoning_effort(config_path or default_config_path(), effort)


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
