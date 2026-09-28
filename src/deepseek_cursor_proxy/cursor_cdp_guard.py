"""Drop CDP calls that restart the Cursor window.

``browser_cdp`` with ``Page.reload`` is delivered to the glass renderer.
The window closes, Cursor comes back, and the agent stops. The same happens
for the other page-lifecycle methods below. Safe calls are forwarded as-is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Any

from .logging import LOG


# Methods that tear down the Cursor window when browser_cdp runs them.
CRASHING_CDP_METHODS = (
    "Page.reload",
    "Page.crash",
    "Page.close",
    "Page.navigate",
    "Page.navigateToHistoryEntry",
    "Target.closeTarget",
    "Target.createTarget",
    "Browser.close",
)

_BLOCKED_REASON = (
    "This CDP method restarts the Cursor window and stops the agent. "
    "Do not retry it. Open the same http or https URL with browser_navigate."
)

# Tool names whose arguments can carry a browser_cdp call.
_GUARDED_TOOL_NAMES = ("browser_cdp", "CallDynamicTool")


def is_guarded_tool(name: str) -> bool:
    """True for browser_cdp and for CallDynamicTool, which can wrap it."""
    return name == "CallDynamicTool" or name.endswith("browser_cdp")


def _proper_prefix_of_guarded(name: str) -> bool:
    """True while a streamed function name may still grow into a guarded tool."""
    return any(
        candidate.startswith(name) and candidate != name
        for candidate in _GUARDED_TOOL_NAMES
    )


def _mentioned_crashing_method(arguments: str) -> str | None:
    for method in CRASHING_CDP_METHODS:
        if f'"{method}"' in arguments:
            return method
    return None


def _blocked_expression() -> str:
    """JS expression the model reads when a crashing CDP method is removed."""
    payload = json.dumps(
        {"blocked": True, "reason": _BLOCKED_REASON},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"JSON.stringify({payload})"


def _replace_crashing_call(node: dict[str, Any], method: str) -> None:
    node.clear()
    node["method"] = "Runtime.evaluate"
    node["params"] = {
        "expression": _blocked_expression(),
        "returnByValue": True,
    }
    LOG.warning(
        "blocked CDP %s; it restarts the Cursor window",
        method,
    )


def neutralize_cdp(value: Any) -> str | None:
    """Rewrite crashing CDP methods in place. Returns the first method removed."""
    blocked: list[str] = []
    _neutralize(value, blocked)
    return blocked[0] if blocked else None


def _neutralize(value: Any, blocked: list[str]) -> None:
    if isinstance(value, dict):
        method = value.get("method")
        if isinstance(method, str) and method in CRASHING_CDP_METHODS:
            _replace_crashing_call(value, method)
            blocked.append(method)
            return
        for key, item in list(value.items()):
            if key == "expression":
                continue
            if isinstance(item, str):
                parsed = _parse_json_container(item)
                if parsed is not None and _neutralize_and_dump(parsed, blocked):
                    value[key] = json.dumps(
                        parsed, ensure_ascii=False, separators=(",", ":")
                    )
                continue
            _neutralize(item, blocked)
        return
    if isinstance(value, list):
        for item in value:
            _neutralize(item, blocked)


def _neutralize_and_dump(parsed: Any, blocked: list[str]) -> bool:
    before = len(blocked)
    _neutralize(parsed, blocked)
    return len(blocked) != before


def _parse_json_container(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, (dict, list)):
        return parsed
    return None


def sanitize_tool_arguments(name: str, arguments: str) -> tuple[str, str | None]:
    """Return arguments safe to send to Cursor, plus the blocked method if any."""
    if not is_guarded_tool(name):
        return arguments, None
    parsed = _parse_json_container(arguments)
    if parsed is not None:
        blocked = neutralize_cdp(parsed)
        if blocked is None:
            return arguments, None
        return (
            json.dumps(parsed, ensure_ascii=False, separators=(",", ":")),
            blocked,
        )
    mentioned = _mentioned_crashing_method(arguments)
    if mentioned is None:
        return arguments, None
    LOG.warning(
        "blocked CDP %s; it restarts the Cursor window",
        mentioned,
    )
    return (
        json.dumps(
            {
                "method": "Runtime.evaluate",
                "params": {
                    "expression": _blocked_expression(),
                    "returnByValue": True,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        mentioned,
    )


def sanitize_response_payload(payload: dict[str, Any]) -> None:
    """Rewrite crashing browser_cdp calls in a non-streaming chat completion."""
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "")
            arguments = function.get("arguments")
            if not isinstance(arguments, str) or not is_guarded_tool(name):
                continue
            updated, _blocked = sanitize_tool_arguments(name, arguments)
            function["arguments"] = updated


def _json_object_complete(arguments: str) -> bool:
    parsed = _parse_json_container(arguments)
    return isinstance(parsed, dict)


def _name_is_final(name: str, included_name: bool) -> bool:
    if not name:
        return False
    if is_guarded_tool(name):
        return True
    if _proper_prefix_of_guarded(name):
        return not included_name
    return True


@dataclass
class _HeldToolCall:
    """One streamed tool call withheld until its name and arguments are known."""

    name: str = ""
    call_id: str | None = None
    call_type: str | None = None
    arguments: str = ""
    buffer: list[dict[str, Any]] = field(default_factory=list)
    passthrough: bool = False
    released: bool = False


class CursorCdpGuard:
    """Hold browser_cdp argument fragments so Page.reload never reaches Cursor."""

    def __init__(self) -> None:
        self._held: dict[tuple[int, int], _HeldToolCall] = {}

    def apply(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the chunk to forward, or nothing while a risky call is incomplete."""
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return [chunk]
        for choice in choices:
            if isinstance(choice, dict):
                self._filter_choice(int(choice.get("index") or 0), choice)
        if _chunk_has_payload(chunk):
            return [chunk]
        return []

    def flush(self) -> list[dict[str, Any]]:
        """Release calls still held when the upstream stream ends."""
        chunks: list[dict[str, Any]] = []
        choice_indexes = sorted({choice_index for choice_index, _ in self._held})
        for choice_index in choice_indexes:
            deltas = self._release_choice(choice_index)
            if not deltas:
                continue
            chunks.append(
                {
                    "id": "chatcmpl-cdp-guard",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "choices": [
                        {
                            "index": choice_index,
                            "delta": {"tool_calls": deltas},
                            "finish_reason": None,
                        }
                    ],
                }
            )
        return chunks

    def _filter_choice(self, choice_index: int, choice: dict[str, Any]) -> None:
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            delta = None
        kept: list[dict[str, Any]] = []
        if delta is not None:
            raw_calls = delta.get("tool_calls")
            if isinstance(raw_calls, list):
                for raw in raw_calls:
                    if isinstance(raw, dict):
                        kept.extend(self._consume(choice_index, raw))
                if kept:
                    delta["tool_calls"] = kept
                else:
                    delta.pop("tool_calls", None)
        if choice.get("finish_reason") is None:
            return
        flushed = self._release_choice(choice_index)
        if not flushed:
            return
        if delta is None:
            delta = {}
            choice["delta"] = delta
        delta.setdefault("tool_calls", [])
        delta["tool_calls"].extend(flushed)

    def _consume(self, choice_index: int, raw: dict[str, Any]) -> list[dict[str, Any]]:
        tool_index = raw.get("index")
        if not isinstance(tool_index, int):
            tool_index = 0
        state = self._held.setdefault((choice_index, tool_index), _HeldToolCall())
        if state.passthrough:
            return [raw]
        if state.released:
            return []

        state.buffer.append(raw)
        included_name = False
        function = raw.get("function")
        if isinstance(function, dict):
            if function.get("name"):
                state.name += str(function["name"])
                included_name = True
            if "arguments" in function and function["arguments"] is not None:
                state.arguments += str(function["arguments"])
        if raw.get("id"):
            state.call_id = str(raw["id"])
        if raw.get("type"):
            state.call_type = str(raw["type"])

        if not _name_is_final(state.name, included_name):
            return []
        if not is_guarded_tool(state.name):
            state.passthrough = True
            pending = state.buffer
            state.buffer = []
            return pending
        if not _json_object_complete(state.arguments):
            return []
        state.released = True
        state.buffer = []
        return [self._released_delta(tool_index, state)]

    def _release_choice(self, choice_index: int) -> list[dict[str, Any]]:
        released: list[dict[str, Any]] = []
        for (held_choice, tool_index), state in self._held.items():
            if held_choice != choice_index or state.passthrough or state.released:
                continue
            if not state.buffer and not state.name:
                continue
            state.released = True
            if is_guarded_tool(state.name):
                released.append(self._released_delta(tool_index, state))
            else:
                released.extend(state.buffer)
            state.buffer = []
        return released

    def _released_delta(self, tool_index: int, state: _HeldToolCall) -> dict[str, Any]:
        arguments, _blocked = sanitize_tool_arguments(state.name, state.arguments)
        delta: dict[str, Any] = {
            "index": tool_index,
            "function": {"name": state.name, "arguments": arguments},
        }
        if state.call_id:
            delta["id"] = state.call_id
        if state.call_type:
            delta["type"] = state.call_type
        return delta


def _chunk_has_payload(chunk: dict[str, Any]) -> bool:
    """False when every choice delta was withheld and nothing else remains."""
    if chunk.get("usage"):
        return True
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return True
    for choice in choices:
        if not isinstance(choice, dict):
            return True
        if choice.get("finish_reason") is not None:
            return True
        delta = choice.get("delta")
        if isinstance(delta, dict) and delta:
            return True
        if delta is not None and not isinstance(delta, dict):
            return True
    return False
