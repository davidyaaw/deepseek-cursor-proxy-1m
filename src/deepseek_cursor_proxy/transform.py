from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any

from .config import ProxyConfig
from .cursor_cdp_guard import sanitize_response_payload
from .cursor_effort import read_cursor_effort
from .logging import LOG, request_id
from .reasoning_store import (
    ReasoningStore,
    agent_root,
    conversation_lineage,
    conversation_scope,
    message_signature,
    portable_key_prefix,
    tool_call_ids,
    tool_call_names,
    tool_call_signature,
    turn_context_signature,
)
from .streaming import fold_reasoning_into_content


SUPPORTED_REQUEST_FIELDS = {
    "model",
    "messages",
    "stream",
    "stream_options",
    "max_tokens",
    "response_format",
    "stop",
    "tools",
    "tool_choice",
    "thinking",
    "reasoning_effort",
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "logprobs",
    "top_logprobs",
    # Standard OpenAI Chat Completions fields that DeepSeek either honors or
    # safely ignores. Cursor and most OpenAI SDKs send these unconditionally,
    # so forwarding keeps clients happy and avoids log spam.
    "user",
    "seed",
    "n",
    "logit_bias",
}

MESSAGE_FIELDS = {
    "role",
    "content",
    "name",
    "tool_call_id",
    "tool_calls",
    "reasoning_content",
    "prefix",
}

ROLE_MESSAGE_FIELDS = {
    "system": {"role", "content", "name"},
    "user": {"role", "content", "name"},
    "assistant": {
        "role",
        "content",
        "name",
        "tool_calls",
        "reasoning_content",
        "prefix",
    },
    "tool": {"role", "content", "tool_call_id"},
}

# Cursor Effort / OpenAI aliases → DeepSeek reasoning_effort.
# DeepSeek accepts none | low | high | max. medium maps to high;
# Cursor Extra High (xhigh) is sent as max.
EFFORT_ALIASES = {
    "none": "none",
    "off": "none",
    "disabled": "none",
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "max": "max",
    "xhigh": "max",
}

# Cursor catalog ids that hijack OpenAI BYOK. Strip effort/-fast first.
CURSOR_MODEL_ALIASES = {
    "gpt-5.6-sol": "deepseek-v4-pro",
    "gpt-5.6-terra": "deepseek-flash",
}

CURSOR_THINKING_BLOCK_RE = re.compile(
    r"""
    (?:
        <(?:think|thinking)\b[^>]*>[\s\S]*?(?:</(?:think|thinking)>|\Z)
        |
        <details\b[^>]*>\s*
        <summary\b[^>]*>\s*Thinking\s*</summary>
        [\s\S]*?(?:</details>|\Z)
    )\s*
    """,
    re.IGNORECASE | re.VERBOSE,
)

RECOVERY_NOTICE_TEXT = "[deepseek-cursor-proxy] Refreshed reasoning_content history."
RECOVERY_NOTICE_CONTENT = f"{RECOVERY_NOTICE_TEXT}\n\n"
RECOVERY_SYSTEM_CONTENT = (
    "deepseek-cursor-proxy recovered this request because older DeepSeek "
    "thinking-mode tool-call reasoning_content was unavailable. Older "
    "unrecoverable tool-call history was omitted; continue using only the "
    "remaining recovered context."
)

# DeepSeek rejects tool names outside this pattern (api-docs create-chat-completion).
DEEPSEEK_FUNCTION_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

# Filled onto assistant tool turns when thinking is on but the cache is cold.
PRIOR_REASONING_UNAVAILABLE = "(prior reasoning unavailable)"

_CUSTOM_INPUT_INSTRUCTION = (
    "Put the entire tool input in the `input` string property, using this "
    "tool's own format — not wrapped in extra JSON."
)


@dataclass(frozen=True)
class PreparedRequest:
    payload: dict[str, Any]
    original_model: str
    upstream_model: str
    cache_namespace: str
    patched_reasoning_messages: int
    missing_reasoning_messages: int
    recovered_reasoning_messages: int = 0
    recovery_dropped_messages: int = 0
    recovery_notice: str | None = None
    record_response_scope: str | None = None
    record_response_messages: list[dict[str, Any]] = field(default_factory=list)
    record_response_contexts: list[tuple[str, list[dict[str, Any]]]] = field(
        default_factory=list
    )
    reasoning_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    recovery_steps: list[dict[str, Any]] = field(default_factory=list)
    continued_recovery_boundary: bool = False
    retired_prefix_messages: int = 0
    lineage: str = ""
    root: str = ""
    agent_id: str = ""
    agent_id_source: str = "transcript"
    cursor_effort: str | None = None
    converted_custom_tool_names: frozenset[str] = field(default_factory=frozenset)


def normalize_reasoning_effort(value: Any) -> str:
    """Map a Cursor/OpenAI effort label onto a DeepSeek reasoning_effort value."""
    if not isinstance(value, str):
        return "high"
    return EFFORT_ALIASES.get(value.strip().lower(), "high")


def _nonempty_effort(value: Any) -> str | None:
    """Return a stripped effort string, or None when the field is absent."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def request_reasoning_effort(
    payload: dict[str, Any],
    model_suffix_effort: str | None,
) -> str | None:
    """Read Cursor Effort from the body, then from the model slug."""
    effort = _nonempty_effort(payload.get("reasoning_effort"))
    if effort:
        return effort
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = _nonempty_effort(reasoning.get("effort"))
        if effort:
            return effort
    return model_suffix_effort


def resolve_thinking_effort(
    requested_effort: str | None,
    config: ProxyConfig,
) -> tuple[str, str | None]:
    """Turn Cursor/config effort into DeepSeek thinking + reasoning_effort."""
    if requested_effort is None:
        if config.thinking == "disabled":
            return "disabled", None
        return "enabled", normalize_reasoning_effort(config.reasoning_effort)

    normalized = normalize_reasoning_effort(requested_effort)
    if normalized == "none":
        return "disabled", None
    return "enabled", normalized


def extract_text_content(content: Any) -> str | None:
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            item_type = item.get("type")
            text = item.get("text") or item.get("content")
            if item_type in {"text", "input_text"} and isinstance(text, str):
                parts.append(text)
            elif isinstance(text, str):
                parts.append(text)
            elif item_type:
                parts.append(f"[{item_type} omitted by DeepSeek text proxy]")
        return "\n".join(part for part in parts if part)
    if isinstance(content, (dict, tuple)):
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    return str(content)


def strip_cursor_thinking_blocks(content: str) -> str:
    return CURSOR_THINKING_BLOCK_RE.sub("", content).lstrip("\r\n")


def cursor_thinking_text(content: str) -> str:
    """Return thinking text Cursor echoed inside a display block."""
    details = re.search(
        r"<summary\b[^>]*>\s*Thinking\s*</summary>\s*([\s\S]*?)\s*</details>",
        content,
        re.IGNORECASE,
    )
    if details:
        return details.group(1).strip()
    tagged = re.search(
        r"<(?:think|thinking)\b[^>]*>([\s\S]*?)</(?:think|thinking)>",
        content,
        re.IGNORECASE,
    )
    if tagged:
        return tagged.group(1).strip()
    return ""


def usable_reasoning_content(value: Any) -> str | None:
    """Reasoning DeepSeek accepts. Blank strings count as missing."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def normalize_tool_call(tool_call: Any) -> dict[str, Any]:
    """Turn one client tool call into a DeepSeek function call.

    Cursor echoes ApplyPatch as ``type: custom``. DeepSeek only accepts
    ``type: function``, so the free-form ``custom.input`` is wrapped as the
    JSON string ``{"input": "<raw>"}``.
    """
    if not isinstance(tool_call, dict):
        tool_call = {}
    if tool_call.get("type") == "custom":
        custom = tool_call.get("custom")
        if not isinstance(custom, dict):
            custom = {}
        name = str(custom.get("name") or tool_call.get("name") or "")
        raw_input = custom.get("input")
        if not isinstance(raw_input, str):
            raw_input = "" if raw_input is None else str(raw_input)
        arguments = json.dumps({"input": raw_input}, ensure_ascii=False)
        normalized = {
            "id": str(tool_call.get("id") or ""),
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }
        if not normalized["id"]:
            normalized.pop("id")
        return normalized

    function = tool_call.get("function") or {}
    if not isinstance(function, dict):
        function = {}

    arguments = function.get("arguments", "")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True)

    normalized = {
        "id": str(tool_call.get("id") or ""),
        "type": "function",
        "function": {
            "name": str(function.get("name") or ""),
            "arguments": arguments,
        },
    }
    if not normalized["id"]:
        normalized.pop("id")
    return normalized


def is_valid_deepseek_function_name(name: str) -> bool:
    """True when a tool name meets DeepSeek's function-name regex."""
    return bool(DEEPSEEK_FUNCTION_NAME_RE.fullmatch(name))


def _custom_tool_description(original: Any) -> str:
    """Keep the client description and instruct the model to use ``input``."""
    base = original.strip() if isinstance(original, str) else ""
    if base:
        return f"{base.rstrip()}\n\n{_CUSTOM_INPUT_INSTRUCTION}"
    return _CUSTOM_INPUT_INSTRUCTION


def custom_tool_as_function(tool: dict[str, Any]) -> dict[str, Any] | None:
    """Map an OpenAI ``custom`` tool onto a DeepSeek ``function`` tool."""
    name = tool.get("name")
    if not isinstance(name, str) or not is_valid_deepseek_function_name(name):
        return None
    description = _custom_tool_description(tool.get("description"))
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "input": {
                        "type": "string",
                        "description": (
                            "Entire tool input in this tool's own format, "
                            "not wrapped in extra JSON."
                        ),
                    }
                },
                "required": ["input"],
                "additionalProperties": False,
            },
        },
    }


def normalize_tool(tool: Any) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(tool, converted)`` for DeepSeek, or ``(None, False)`` if dropped.

    DeepSeek only accepts ``{"type": "function", ...}``. Cursor's ApplyPatch
    arrives as OpenAI ``type: "custom"``; those are rewritten to a function
    tool with a single required string ``input`` property when the name is
    valid. Other non-function types are still dropped.
    """
    if not isinstance(tool, dict):
        return None, False
    tool_type = tool.get("type") or "function"
    if tool_type == "custom":
        converted = custom_tool_as_function(tool)
        return (converted, True) if converted is not None else (None, False)
    if tool_type != "function":
        return None, False
    function = tool.get("function")
    if not isinstance(function, dict) or not function.get("name"):
        return None, False
    name = str(function["name"])
    if not is_valid_deepseek_function_name(name):
        return None, False
    normalized = dict(tool)
    normalized["type"] = "function"
    return normalized, False


def tool_label(tool: Any) -> str:
    if not isinstance(tool, dict):
        return "unnamed"
    function = tool.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    if tool.get("name"):
        return str(tool["name"])
    tool_type = tool.get("type")
    return f"{tool_type} tool" if tool_type else "unnamed"


def normalize_tools(
    tools: Any,
) -> tuple[list[dict[str, Any]], list[str], frozenset[str]]:
    """Split client tools into kept entries, dropped labels, and converted names."""
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    converted: set[str] = set()
    if not isinstance(tools, list):
        return kept, dropped, frozenset()
    for tool in tools:
        normalized, was_converted = normalize_tool(tool)
        if normalized is None:
            dropped.append(tool_label(tool))
            continue
        kept.append(normalized)
        if was_converted:
            converted.add(str(normalized["function"]["name"]))
    return kept, dropped, frozenset(converted)


def tool_names(tools: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict):
                function = tool.get("function")
                if isinstance(function, dict) and function.get("name"):
                    names.add(str(function["name"]))
    return names


def legacy_function_to_tool(function: Any) -> dict[str, Any]:
    if not isinstance(function, dict):
        function = {}
    return {"type": "function", "function": function}


def convert_function_call(function_call: Any) -> Any:
    if isinstance(function_call, str):
        if function_call in {"auto", "none", "required"}:
            return function_call
        return None
    if isinstance(function_call, dict) and function_call.get("name"):
        return {
            "type": "function",
            "function": {"name": str(function_call["name"])},
        }
    return None


def normalize_tool_choice(tool_choice: Any) -> Any:
    if isinstance(tool_choice, str):
        if tool_choice in {"auto", "none", "required"}:
            return tool_choice
        return None
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            function = tool_choice.get("function")
            if isinstance(function, dict) and function.get("name"):
                return {
                    "type": "function",
                    "function": {"name": str(function["name"])},
                }
        # Cursor forces ApplyPatch with type "custom"; rewrite to function.
        if tool_choice.get("type") == "custom" and tool_choice.get("name"):
            name = str(tool_choice["name"])
            if is_valid_deepseek_function_name(name):
                return {"type": "function", "function": {"name": name}}
        return None
    return None


def unwrap_converted_tool_arguments(arguments: str) -> str:
    """If arguments are ``{"input": "<raw>"}``, return the raw string."""
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError, ValueError):
        return arguments
    if (
        isinstance(parsed, dict)
        and set(parsed.keys()) == {"input"}
        and isinstance(parsed["input"], str)
    ):
        return parsed["input"]
    return arguments


def present_converted_tool_call(
    tool_call: dict[str, Any],
    converted_names: frozenset[str] | set[str],
) -> None:
    """Give Cursor the raw patch as a function call it can execute.

    The agent parser accepts only ``type: function`` plus ``function.name``.
    ApplyPatch then reads that arguments string as the patch itself.
    """
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return
    name = function.get("name")
    if name not in converted_names:
        return
    arguments = function.get("arguments")
    raw_input = (
        unwrap_converted_tool_arguments(arguments)
        if isinstance(arguments, str)
        else ""
    )
    function["arguments"] = raw_input
    tool_call["type"] = "function"
    tool_call.pop("custom", None)


def wrap_converted_history_for_upstream(
    messages: list[dict[str, Any]],
    converted_names: frozenset[str] | set[str],
) -> list[dict[str, Any]]:
    """Send converted tools upstream as ``{"input": "<raw>"}`` without mutating history."""
    if not converted_names:
        return messages
    wrapped: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            wrapped.append(message)
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            wrapped.append(message)
            continue
        updated_calls: list[Any] = []
        changed = False
        for tool_call in tool_calls:
            updated = _wrap_converted_tool_call(tool_call, converted_names)
            changed = changed or updated is not tool_call
            updated_calls.append(updated)
        if not changed:
            wrapped.append(message)
            continue
        copied = dict(message)
        copied["tool_calls"] = updated_calls
        wrapped.append(copied)
    return wrapped


def _wrap_converted_tool_call(
    tool_call: Any,
    converted_names: frozenset[str] | set[str],
) -> Any:
    """Wrap one echoed raw patch so it matches the converted function schema."""
    if not isinstance(tool_call, dict):
        return tool_call
    function = tool_call.get("function")
    if not isinstance(function, dict) or function.get("name") not in converted_names:
        return tool_call
    arguments = function.get("arguments", "")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    raw_input = unwrap_converted_tool_arguments(arguments)
    wrapped_arguments = json.dumps({"input": raw_input}, ensure_ascii=False)
    if wrapped_arguments == arguments:
        return tool_call
    copied_call = dict(tool_call)
    copied_function = dict(function)
    copied_function["arguments"] = wrapped_arguments
    copied_call["function"] = copied_function
    return copied_call


def unwrap_converted_tool_calls(
    tool_calls: Any,
    converted_names: frozenset[str] | set[str],
) -> None:
    """Present converted tools as function calls whose arguments are the raw input."""
    if not converted_names or not isinstance(tool_calls, list):
        return
    for tool_call in tool_calls:
        if isinstance(tool_call, dict):
            present_converted_tool_call(tool_call, converted_names)


def unwrap_converted_tools_in_response(
    response_payload: dict[str, Any],
    converted_names: frozenset[str] | set[str],
) -> None:
    """Unwrap converted-tool arguments on a non-streaming chat completion."""
    if not converted_names:
        return
    choices = response_payload.get("choices")
    if not isinstance(choices, list):
        return
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict):
            unwrap_converted_tool_calls(message.get("tool_calls"), converted_names)


def fill_missing_reasoning_placeholders(
    messages: list[dict[str, Any]],
    missing_indexes: list[int],
) -> None:
    """Attach placeholder reasoning when every cache lookup missed."""
    for index in missing_indexes:
        messages[index]["reasoning_content"] = PRIOR_REASONING_UNAVAILABLE


def cache_placeholder_reasoning(
    store: ReasoningStore,
    messages: list[dict[str, Any]],
    missing_indexes: list[int],
    cache_namespace: str,
    agent_id: str = "",
) -> None:
    """Persist placeholder reasoning so later turns hit instead of truncating."""
    root = agent_root(messages, agent_id)
    for index in missing_indexes:
        prior = messages[:index]
        msg_lineage = conversation_lineage(prior, agent_id)
        scope = conversation_scope(prior, cache_namespace, msg_lineage)
        store.store_assistant_message(
            messages[index],
            scope,
            cache_namespace,
            prior,
            lineage=msg_lineage,
            root=root,
            agent_id=agent_id,
        )


def normalize_message(
    message: Any,
    store: ReasoningStore | None,
    prior_messages: list[dict[str, Any]],
    cache_namespace: str,
    repair_reasoning: bool,
    keep_reasoning: bool,
    agent_id: str = "",
    require_assistant_reasoning: bool = False,
) -> tuple[dict[str, Any], bool, bool, dict[str, Any] | None]:
    if not isinstance(message, dict):
        message = {"role": "user", "content": str(message)}
    normalized = {key: value for key, value in message.items() if key in MESSAGE_FIELDS}
    role = normalized.get("role") or "user"
    normalized["role"] = role

    if role == "function":
        normalized["role"] = "tool"

    if "content" in normalized:
        normalized["content"] = extract_text_content(normalized["content"]) or ""
    elif normalized["role"] in {"assistant", "tool", "system", "user"}:
        normalized["content"] = ""
    echoed_thinking = ""
    if normalized["role"] == "assistant" and isinstance(normalized.get("content"), str):
        echoed_thinking = cursor_thinking_text(normalized["content"])
        normalized["content"] = strip_cursor_thinking_blocks(normalized["content"])
        # Keep the echoed chain of thought. The repair pass strips this
        # content before it can see the block.
        if echoed_thinking and usable_reasoning_content(
            normalized.get("reasoning_content")
        ) is None:
            normalized["reasoning_content"] = echoed_thinking

    if normalized.get("tool_calls"):
        normalized["tool_calls"] = [
            normalize_tool_call(tool_call)
            for tool_call in normalized.get("tool_calls") or []
        ]

    patched = False
    missing = False
    diagnostic: dict[str, Any] | None = None
    if normalized["role"] == "assistant":
        if not keep_reasoning:
            normalized.pop("reasoning_content", None)
        elif repair_reasoning:
            # User text before this message. Later user messages in the same
            # request belong to a newer turn and must not retarget this lookup.
            lineage = conversation_lineage(prior_messages, agent_id)
            reasoning = usable_reasoning_content(normalized.get("reasoning_content"))
            if reasoning is None:
                normalized.pop("reasoning_content", None)
                # Tool turns always need reasoning. With tools in the request,
                # DeepSeek also requires it on assistant turns that did not
                # call a tool.
                needs_tool_reasoning = assistant_needs_reasoning_for_tool_context(
                    normalized, prior_messages
                )
                needs_reasoning = (
                    needs_tool_reasoning or require_assistant_reasoning
                )
                lookup_scope = conversation_scope(
                    prior_messages, cache_namespace, lineage
                )
                lookup_keys = (
                    reasoning_lookup_keys(
                        normalized,
                        lookup_scope,
                        cache_namespace,
                        prior_messages,
                        lineage,
                    )
                    if needs_reasoning
                    else []
                )
                hit_kind = None
                if needs_reasoning and store is not None:
                    for lookup_key in lookup_keys:
                        restored = store.get(str(lookup_key["key"]))
                        if restored is not None and restored.strip():
                            lookup_key["hit"] = True
                            hit_kind = lookup_key["kind"]
                            normalized["reasoning_content"] = restored
                            patched = True
                            if not lookup_key.get("portable"):
                                store.backfill_portable_aliases(
                                    normalized,
                                    restored,
                                    cache_namespace,
                                    prior_messages,
                                    lineage,
                                    agent_root(prior_messages, agent_id),
                                )
                            break
                if needs_reasoning and not patched and echoed_thinking:
                    normalized["reasoning_content"] = echoed_thinking
                    hit_kind = "echoed_thinking"
                elif needs_tool_reasoning and not patched:
                    missing = True
                elif needs_reasoning and not patched:
                    # A placeholder keeps the transcript. Do not count it as a
                    # cache hit: a tool-turn miss in the same request must still
                    # take the cold-cache path instead of dropping history.
                    normalized["reasoning_content"] = PRIOR_REASONING_UNAVAILABLE
                    hit_kind = "placeholder"
                if needs_reasoning:
                    if hit_kind == "placeholder":
                        cache_event = "filled"
                    elif patched or hit_kind == "echoed_thinking":
                        cache_event = "hit"
                    else:
                        cache_event = "miss"
                    LOG.info(
                        "reasoning_cache_%s id=%s lineage=%s hit_kind=%s",
                        cache_event,
                        request_id(),
                        (lineage or "-")[:16],
                        hit_kind or "-",
                    )
                if needs_reasoning:
                    diagnostic = {
                        "message_index": len(prior_messages),
                        "role": "assistant",
                        "needs_reasoning": True,
                        "had_reasoning_content": False,
                        "patched": patched,
                        "missing": missing,
                        "lookup_scope": lookup_scope,
                        "message_signature": message_signature(normalized),
                        "tool_call_ids": tool_call_ids(normalized),
                        "lookup_keys": lookup_keys,
                        "hit_kind": hit_kind,
                    }
            elif assistant_needs_reasoning_for_tool_context(normalized, prior_messages):
                diagnostic = {
                    "message_index": len(prior_messages),
                    "role": "assistant",
                    "needs_reasoning": True,
                    "had_reasoning_content": True,
                    "patched": False,
                    "missing": False,
                    "lookup_scope": conversation_scope(
                        prior_messages, cache_namespace, lineage
                    ),
                    "message_signature": message_signature(normalized),
                    "tool_call_ids": tool_call_ids(normalized),
                    "lookup_keys": [],
                    "hit_kind": "request",
                }

    allowed_fields = ROLE_MESSAGE_FIELDS.get(str(normalized["role"]), MESSAGE_FIELDS)
    normalized = {
        key: value for key, value in normalized.items() if key in allowed_fields
    }
    return normalized, patched, missing, diagnostic


def reasoning_lookup_keys(
    message: dict[str, Any],
    scope: str,
    cache_namespace: str = "",
    prior_messages: list[dict[str, Any]] | None = None,
    lineage: str = "",
) -> list[dict[str, Any]]:
    keys = [
        {
            "kind": "message_signature",
            "key": f"scope:{scope}:signature:{message_signature(message)}",
            "portable": False,
            "hit": False,
        }
    ]
    keys.extend(
        {
            "kind": "tool_call_id",
            "tool_call_id": tool_call_id,
            "key": f"scope:{scope}:tool_call:{tool_call_id}",
            "portable": False,
            "hit": False,
        }
        for tool_call_id in tool_call_ids(message)
    )
    keys.extend(
        {
            "kind": "tool_call_signature",
            "function_name": str((tool_call.get("function") or {}).get("name") or ""),
            "key": (
                f"scope:{scope}:tool_call_signature:"
                f"{tool_call_signature(tool_call)}"
            ),
            "portable": False,
            "hit": False,
        }
        for tool_call in (message.get("tool_calls") or [])
        if isinstance(tool_call, dict)
    )
    keys.extend(
        {
            "kind": "tool_name",
            "function_name": tool_name,
            "key": f"scope:{scope}:tool_name:{tool_name}",
            "portable": False,
            "hit": False,
        }
        for tool_name in tool_call_names(message)
    )
    if cache_namespace and prior_messages is not None:
        prefix = portable_key_prefix(cache_namespace, prior_messages, lineage)
        turn_signature = turn_context_signature(prior_messages)
        keys.append(
            {
                "kind": "portable_message_signature",
                "key": f"{prefix}signature:{message_signature(message)}",
                "turn_context_signature": turn_signature,
                "lineage": lineage or conversation_lineage(prior_messages),
                "portable": True,
                "hit": False,
            }
        )
        keys.extend(
            {
                "kind": "portable_tool_call_id",
                "tool_call_id": tool_call_id,
                "key": f"{prefix}tool_call:{tool_call_id}",
                "turn_context_signature": turn_signature,
                "portable": True,
                "hit": False,
            }
            for tool_call_id in tool_call_ids(message)
        )
        keys.extend(
            {
                "kind": "portable_tool_call_signature",
                "function_name": str(
                    (tool_call.get("function") or {}).get("name") or ""
                ),
                "key": (
                    f"{prefix}tool_call_signature:{tool_call_signature(tool_call)}"
                ),
                "turn_context_signature": turn_signature,
                "portable": True,
                "hit": False,
            }
            for tool_call in (message.get("tool_calls") or [])
            if isinstance(tool_call, dict)
        )
        keys.extend(
            {
                "kind": "portable_tool_name",
                "function_name": tool_name,
                "key": f"{prefix}tool_name:{tool_name}",
                "turn_context_signature": turn_signature,
                "portable": True,
                "hit": False,
            }
            for tool_name in tool_call_names(message)
        )
    return keys


def normalize_messages(
    messages: Any,
    store: ReasoningStore | None,
    cache_namespace: str,
    repair_reasoning: bool,
    keep_reasoning: bool,
    agent_id: str = "",
    require_assistant_reasoning: bool = False,
) -> tuple[list[dict[str, Any]], int, list[int], list[dict[str, Any]]]:
    if not isinstance(messages, list):
        return [], 0, [], []
    normalized_messages: list[dict[str, Any]] = []
    patched_count = 0
    missing_indexes: list[int] = []
    diagnostics: list[dict[str, Any]] = []
    for message in messages:
        normalized, patched, missing, diagnostic = normalize_message(
            message,
            store,
            normalized_messages,
            cache_namespace,
            repair_reasoning,
            keep_reasoning,
            agent_id,
            require_assistant_reasoning,
        )
        normalized_messages.append(normalized)
        if patched:
            patched_count += 1
        if missing:
            missing_indexes.append(len(normalized_messages) - 1)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    return normalized_messages, patched_count, missing_indexes, diagnostics


def has_recovery_notice(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "assistant"
        and isinstance(content, str)
        and content.startswith(RECOVERY_NOTICE_TEXT)
    )


def strip_recovery_notice_for_upstream(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cursor echoes the proxy's recovery notice back to us in later turns.
    The notice serves as a boundary marker for the proxy, but DeepSeek must
    not see proxy-generated prose. Return a copy with assistant prefixes
    stripped; leave the input untouched so cache scopes/recording contexts
    keep matching the with-prefix history that Cursor will send next time."""
    stripped: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "assistant":
            stripped.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.startswith(RECOVERY_NOTICE_TEXT):
            stripped.append(message)
            continue
        cleaned = dict(message)
        cleaned["content"] = content[len(RECOVERY_NOTICE_TEXT) :].lstrip("\r\n")
        stripped.append(cleaned)
    return stripped


def leading_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    leading_messages: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "system":
            leading_messages.append(message)
            continue
        break
    return leading_messages


def active_messages_from_recovery_boundary(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, dict[str, Any]] | None:
    recovery_boundary_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if has_recovery_notice(messages[index])
        ),
        -1,
    )
    if recovery_boundary_index == -1:
        return None

    context_user_index = next(
        (
            index
            for index in range(recovery_boundary_index - 1, -1, -1)
            if messages[index].get("role") == "user"
        ),
        -1,
    )
    leading_messages = leading_system_messages(messages)
    recovered_tail = []
    if context_user_index != -1:
        recovered_tail.append(messages[context_user_index])
    recovered_tail.extend(messages[recovery_boundary_index:])
    active_messages = [
        *leading_messages,
        {"role": "system", "content": RECOVERY_SYSTEM_CONTENT},
        *recovered_tail,
    ]
    kept_context_messages = 1 if context_user_index != -1 else 0
    retired_messages = (
        recovery_boundary_index - len(leading_messages) - kept_context_messages
    )
    retired_messages = max(retired_messages, 0)
    step = {
        "strategy": "continued_recovery_boundary",
        "recovery_boundary_index": recovery_boundary_index,
        "context_user_index": context_user_index,
        "retired_prefix_messages": retired_messages,
    }
    return active_messages, retired_messages, step


def recover_messages_from_missing_reasoning(
    messages: list[dict[str, Any]],
    missing_indexes: list[int],
) -> tuple[list[dict[str, Any]], int, str | None, dict[str, Any]]:
    recovery_boundary_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if has_recovery_notice(messages[index])
            and any(missing_index < index for missing_index in missing_indexes)
        ),
        -1,
    )
    if recovery_boundary_index != -1:
        context_user_index = next(
            (
                index
                for index in range(recovery_boundary_index - 1, -1, -1)
                if messages[index].get("role") == "user"
            ),
            -1,
        )
        leading_messages = leading_system_messages(messages)
        recovered_tail = []
        if context_user_index != -1:
            recovered_tail.append(messages[context_user_index])
        recovered_tail.extend(messages[recovery_boundary_index:])
        recovered = [
            *leading_messages,
            {"role": "system", "content": RECOVERY_SYSTEM_CONTENT},
            *recovered_tail,
        ]
        kept_context_messages = 1 if context_user_index != -1 else 0
        omitted_messages = (
            recovery_boundary_index - len(leading_messages) - kept_context_messages
        )
        return (
            recovered,
            omitted_messages,
            None,
            {
                "strategy": "recovery_boundary",
                "missing_indexes": missing_indexes,
                "recovery_boundary_index": recovery_boundary_index,
                "context_user_index": context_user_index,
                "dropped_messages": omitted_messages,
                "notice": None,
            },
        )

    last_user_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("role") == "user"
        ),
        -1,
    )
    if last_user_index == -1:
        return (
            messages,
            0,
            None,
            {
                "strategy": "none",
                "missing_indexes": missing_indexes,
                "last_user_index": None,
                "dropped_messages": 0,
                "notice": None,
            },
        )

    recovered = leading_system_messages(messages)
    omitted_messages = len(messages) - len(recovered) - 1
    recovered.append({"role": "system", "content": RECOVERY_SYSTEM_CONTENT})
    recovered.append(messages[last_user_index])
    return (
        recovered,
        omitted_messages,
        RECOVERY_NOTICE_CONTENT,
        {
            "strategy": "latest_user",
            "missing_indexes": missing_indexes,
            "last_user_index": last_user_index,
            "dropped_messages": omitted_messages,
            "notice": RECOVERY_NOTICE_CONTENT,
        },
    )


def assistant_needs_reasoning_for_tool_context(
    message: dict[str, Any],
    prior_messages: list[dict[str, Any]],
) -> bool:
    if message.get("tool_calls"):
        return True
    for prior_message in reversed(prior_messages):
        role = prior_message.get("role")
        if role == "tool":
            return True
        if role in {"user", "system"}:
            return False
    return False


_ONE_M_CONTEXT_MARKER = re.compile(r"\[1m\]$", re.IGNORECASE)
_CURSOR_VARIANT_RE = re.compile(
    r"(?i)(?:-(none|minimal|low|medium|high|xhigh|max))?(-fast)?$"
)


def split_cursor_model(original_model: str) -> tuple[str, str | None]:
    """Strip Cursor `[1m]`, Effort, and `-fast` suffixes from a model id."""
    model = _ONE_M_CONTEXT_MARKER.sub("", original_model.strip())
    match = _CURSOR_VARIANT_RE.search(model)
    suffix_effort = None
    if match and (match.group(1) or match.group(2)):
        if match.group(1):
            suffix_effort = match.group(1).lower()
        model = model[: match.start()]
    return model, suffix_effort


def upstream_model_for(original_model: str, config: ProxyConfig) -> str:
    # `[1m]` is a client-side context marker (Claude Code parses it to raise
    # its local budget to 1M, Cherry Studio and other agents append it for
    # backends that declare a >=1M window). DeepSeek's OpenAI-format API
    # rejects the suffix, while `deepseek-flash` / `deepseek-v4-pro` serve 1M
    # natively, so it is dropped before forwarding upstream. Keeping the
    # normalization here also keeps the reasoning cache namespace identical
    # whether or not the client decorates the model id.
    # Cursor also appends Effort (`-high`) and `-fast` to GPT-5.6 slugs.
    unmarked = _ONE_M_CONTEXT_MARKER.sub("", original_model.strip())
    if unmarked != original_model.strip():
        LOG.info(
            "stripping client-side 1M context marker %r -> %r",
            original_model,
            unmarked,
        )
    model, _suffix_effort = split_cursor_model(original_model)
    if model.startswith("deepseek-"):
        return model
    alias = CURSOR_MODEL_ALIASES.get(model.lower())
    if alias:
        LOG.info("rewriting Cursor model %r to %r", original_model, alias)
        return alias
    LOG.warning(
        "rewriting non-DeepSeek model %r to configured fallback %r",
        model,
        config.upstream_model,
    )
    return config.upstream_model


def reasoning_model_family(upstream_model: str) -> str:
    if upstream_model in {"deepseek-v4-pro", "deepseek-v4-flash"}:
        return "deepseek-v4"
    return upstream_model


def reasoning_cache_namespace(
    config: ProxyConfig,
    upstream_model: str,
    thinking: Any,
    reasoning_effort: Any,
    authorization: str | None = None,
) -> str:
    auth_hash = ""
    if authorization:
        auth_hash = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
    payload = {
        "base_url": config.upstream_base_url,
        "model": reasoning_model_family(upstream_model),
        "thinking": thinking,
        "reasoning_effort": reasoning_effort,
        "authorization_hash": auth_hash,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def response_recording_contexts(
    *items: tuple[str, list[dict[str, Any]]] | None,
) -> list[tuple[str, list[dict[str, Any]]]]:
    contexts: list[tuple[str, list[dict[str, Any]]]] = []
    seen: set[str] = set()
    for item in items:
        if item is None:
            continue
        scope, messages = item
        if scope in seen:
            continue
        seen.add(scope)
        contexts.append((scope, messages))
    return contexts


# Read for agent isolation, then omitted from the DeepSeek request.
_CONSUMED_REQUEST_FIELDS = {
    "max_completion_tokens",
    "functions",
    "function_call",
    "metadata",
    "conversation_id",
    "agent_id",
    "subagent_id",
    "prompt_cache_key",
    "reasoning",
}

_AGENT_HEADER_NAMES = (
    "x-cursor-conversation-id",
    "x-conversation-id",
    "x-agent-id",
    "x-cursor-agent-id",
    "x-subagent-id",
    "x-cursor-subagent-id",
)

_AGENT_METADATA_KEYS = (
    "conversation_id",
    "conversationId",
    "agent_id",
    "agentId",
    "subagent_id",
    "subagentId",
    "composer_id",
    "composerId",
    "parent_conversation_id",
    "parentConversationId",
)


def _header_value(headers: Any, name: str) -> str:
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    value = getter(name)
    if not value:
        value = getter(name.lower())
    return str(value).strip() if value else ""


def resolve_agent_id(
    payload: dict[str, Any] | None,
    headers: Any = None,
) -> tuple[str, str]:
    """Read a Cursor conversation or sub-agent id from the request.

    Per-request ids are ignored. Native Cursor models isolate thinking by
    conversation id; this is the same id when the client sends it.
    """
    parts: list[str] = []
    sources: list[str] = []
    if headers is not None:
        for name in _AGENT_HEADER_NAMES:
            value = _header_value(headers, name)
            if value:
                parts.append(f"{name}={value}")
                sources.append(f"header:{name}")
    body = payload if isinstance(payload, dict) else {}
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        for key in _AGENT_METADATA_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(f"{key}={value.strip()}")
                sources.append(f"metadata:{key}")
    for key in ("conversation_id", "agent_id", "subagent_id", "prompt_cache_key"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}={value.strip()}")
            sources.append(f"field:{key}")
    if not parts:
        return "", "transcript"
    return "\n".join(parts), ",".join(sources)


def prepare_upstream_request(
    payload: dict[str, Any],
    config: ProxyConfig,
    store: ReasoningStore | None,
    authorization: str | None = None,
    agent_id: str = "",
    headers: Any = None,
) -> PreparedRequest:
    original_model = str(payload.get("model") or config.upstream_model)
    upstream_model = upstream_model_for(original_model, config)

    prepared = {
        key: value for key, value in payload.items() if key in SUPPORTED_REQUEST_FIELDS
    }
    dropped_fields = sorted(
        key
        for key in payload.keys()
        if key not in SUPPORTED_REQUEST_FIELDS
        and key not in _CONSUMED_REQUEST_FIELDS
    )
    if dropped_fields:
        LOG.warning(
            "dropping unsupported request field(s): %s", ", ".join(dropped_fields)
        )
    if "max_tokens" not in prepared and "max_completion_tokens" in payload:
        prepared["max_tokens"] = payload["max_completion_tokens"]

    prepared["model"] = upstream_model
    if prepared.get("stream"):
        stream_options = prepared.get("stream_options")
        if not isinstance(stream_options, dict):
            stream_options = {}
        else:
            stream_options = dict(stream_options)
        stream_options["include_usage"] = True
        prepared["stream_options"] = stream_options

    converted_custom_tool_names: frozenset[str] = frozenset()
    if "tools" in prepared and isinstance(prepared["tools"], list):
        normalized_tools, dropped_tools, converted_custom_tool_names = normalize_tools(
            prepared["tools"]
        )
        if dropped_tools:
            LOG.warning(
                "dropping tool(s) DeepSeek cannot accept: %s",
                ", ".join(dropped_tools),
            )
        if normalized_tools:
            prepared["tools"] = normalized_tools
        else:
            prepared.pop("tools", None)
    elif isinstance(payload.get("functions"), list):
        prepared["tools"] = [
            legacy_function_to_tool(function) for function in payload["functions"]
        ]

    if "tool_choice" in prepared:
        tool_choice = normalize_tool_choice(prepared["tool_choice"])
        available_tools = tool_names(prepared.get("tools"))
        if (
            isinstance(tool_choice, dict)
            and tool_choice["function"]["name"] not in available_tools
        ):
            # A forced tool that was just dropped can no longer be honored.
            tool_choice = None
        if tool_choice is None:
            prepared.pop("tool_choice", None)
        else:
            prepared["tool_choice"] = tool_choice
    elif "function_call" in payload:
        tool_choice = convert_function_call(payload.get("function_call"))
        if tool_choice is not None:
            prepared["tool_choice"] = tool_choice

    catalog_model, suffix_effort = split_cursor_model(original_model)
    cursor_effort = read_cursor_effort(catalog_model, config.cursor_effort_path)
    requested_effort = request_reasoning_effort(payload, suffix_effort)
    if requested_effort is None:
        requested_effort = cursor_effort
    thinking_type, reasoning_effort = resolve_thinking_effort(
        requested_effort,
        config,
    )
    prepared["thinking"] = {"type": thinking_type}
    thinking_enabled = thinking_type == "enabled"
    thinking_disabled = thinking_type == "disabled"
    if thinking_enabled and reasoning_effort is not None:
        prepared["reasoning_effort"] = reasoning_effort
    else:
        prepared.pop("reasoning_effort", None)

    cache_namespace = reasoning_cache_namespace(
        config,
        upstream_model,
        prepared.get("thinking"),
        prepared.get("reasoning_effort"),
        authorization,
    )
    resolved_agent_id, agent_id_source = resolve_agent_id(payload, headers)
    if agent_id:
        resolved_agent_id = agent_id
        agent_id_source = "caller"
    pre_repair_messages, _, _, _ = normalize_messages(
        payload.get("messages"),
        None,
        cache_namespace,
        repair_reasoning=False,
        keep_reasoning=not thinking_disabled,
    )
    record_response_messages = pre_repair_messages
    root = agent_root(pre_repair_messages, resolved_agent_id)
    lineage = conversation_lineage(pre_repair_messages, resolved_agent_id)
    LOG.info(
        "agent_lineage id=%s root=%s source=%s",
        request_id(),
        root[:16],
        agent_id_source,
    )
    record_response_scope = conversation_scope(
        record_response_messages, cache_namespace, lineage
    )
    messages_for_repair = pre_repair_messages
    continued_recovery_boundary = False
    retired_prefix_messages = 0
    recovered_count = 0
    recovery_dropped_messages = 0
    recovery_notice = None
    recovery_steps: list[dict[str, Any]] = []
    if thinking_enabled and config.missing_reasoning_strategy == "recover":
        boundary = active_messages_from_recovery_boundary(pre_repair_messages)
        if boundary is not None:
            messages_for_repair, retired_prefix_messages, boundary_step = boundary
            continued_recovery_boundary = True
            recovery_steps.append(boundary_step)

    request_has_tools = bool(prepared.get("tools"))
    messages, patched_count, missing_indexes, reasoning_diagnostics = (
        normalize_messages(
            messages_for_repair,
            store,
            cache_namespace,
            repair_reasoning=thinking_enabled,
            keep_reasoning=not thinking_disabled,
            agent_id=resolved_agent_id,
            require_assistant_reasoning=request_has_tools and thinking_enabled,
        )
    )
    # Cold cache (every lookup missed): keep the full transcript and fill
    # placeholders instead of dropping history down to the latest user turn.
    if (
        thinking_enabled
        and config.missing_reasoning_strategy == "recover"
        and missing_indexes
        and patched_count == 0
    ):
        fill_missing_reasoning_placeholders(messages, missing_indexes)
        if store is not None:
            cache_placeholder_reasoning(
                store,
                messages,
                missing_indexes,
                cache_namespace,
                resolved_agent_id,
            )
        LOG.info(
            "reasoning_cache_filled id=%s messages=%s reason=no_prior_reasoning",
            request_id(),
            len(missing_indexes),
        )
        missing_indexes = []
    # Mixed hit/miss: recover from a boundary or the latest user message.
    # A hard cap guarantees a missed cache row cannot spin forever.
    recovery_passes = 0
    while (
        missing_indexes
        and config.missing_reasoning_strategy == "recover"
        and recovery_passes < 4
    ):
        recovery_passes += 1
        recovered_messages, dropped_messages, notice, recovery_step = (
            recover_messages_from_missing_reasoning(messages, missing_indexes)
        )
        recovery_steps.append(recovery_step)
        if not dropped_messages:
            break
        recovered_count += len(missing_indexes)
        recovery_dropped_messages += dropped_messages
        if notice:
            recovery_notice = notice
        (
            messages,
            patched_count,
            missing_indexes,
            latest_diagnostics,
        ) = normalize_messages(
            recovered_messages,
            store,
            cache_namespace,
            repair_reasoning=thinking_enabled,
            keep_reasoning=not thinking_disabled,
            agent_id=resolved_agent_id,
            require_assistant_reasoning=request_has_tools and thinking_enabled,
        )
        reasoning_diagnostics.extend(latest_diagnostics)
    active_lineage = conversation_lineage(messages, resolved_agent_id)
    active_record_response_scope = conversation_scope(
        messages, cache_namespace, active_lineage
    )
    record_response_contexts = response_recording_contexts(
        (record_response_scope, record_response_messages),
        (active_record_response_scope, messages),
    )
    prepared["messages"] = wrap_converted_history_for_upstream(
        strip_recovery_notice_for_upstream(messages),
        converted_custom_tool_names,
    )

    return PreparedRequest(
        payload=prepared,
        original_model=original_model,
        upstream_model=upstream_model,
        cache_namespace=cache_namespace,
        patched_reasoning_messages=patched_count,
        missing_reasoning_messages=len(missing_indexes),
        recovered_reasoning_messages=recovered_count,
        recovery_dropped_messages=recovery_dropped_messages,
        recovery_notice=recovery_notice,
        record_response_scope=record_response_scope,
        record_response_messages=record_response_messages,
        record_response_contexts=record_response_contexts,
        reasoning_diagnostics=reasoning_diagnostics,
        recovery_steps=recovery_steps,
        continued_recovery_boundary=continued_recovery_boundary,
        retired_prefix_messages=retired_prefix_messages,
        lineage=lineage,
        root=root,
        agent_id=resolved_agent_id,
        agent_id_source=agent_id_source,
        cursor_effort=requested_effort,
        converted_custom_tool_names=converted_custom_tool_names,
    )


def record_response_reasoning(
    response_payload: dict[str, Any],
    store: ReasoningStore | None,
    request_messages: list[dict[str, Any]],
    cache_namespace: str = "",
    scope: str | None = None,
    prior_messages: list[dict[str, Any]] | None = None,
    recording_contexts: list[tuple[str, list[dict[str, Any]]]] | None = None,
    lineage: str = "",
    root: str = "",
    agent_id: str = "",
) -> int:
    if store is None:
        return 0
    stored = 0
    choices = response_payload.get("choices")
    if not isinstance(choices, list):
        return stored
    if recording_contexts is None:
        response_scope = (
            scope
            if scope is not None
            else conversation_scope(request_messages, cache_namespace, lineage)
        )
        response_prior_messages = (
            prior_messages if prior_messages is not None else request_messages
        )
        recording_contexts = [(response_scope, response_prior_messages)]
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict):
            for response_scope, response_prior_messages in recording_contexts:
                stored += store.store_assistant_message(
                    message,
                    response_scope,
                    cache_namespace,
                    response_prior_messages,
                    lineage=lineage,
                    root=root,
                    agent_id=agent_id,
                )
    return stored


def rewrite_response_body(
    body: bytes,
    original_model: str,
    store: ReasoningStore | None,
    request_messages: list[dict[str, Any]],
    cache_namespace: str = "",
    content_prefix: str | None = None,
    scope: str | None = None,
    prior_messages: list[dict[str, Any]] | None = None,
    recording_contexts: list[tuple[str, list[dict[str, Any]]]] | None = None,
    display_reasoning: bool = False,
    collapsible_reasoning: bool = True,
    lineage: str = "",
    root: str = "",
    agent_id: str = "",
    converted_custom_tool_names: frozenset[str] | set[str] | None = None,
) -> bytes:
    response_payload = json.loads(body.decode("utf-8"))
    if isinstance(response_payload, dict):
        sanitize_response_payload(response_payload)
        unwrap_converted_tools_in_response(
            response_payload,
            frozenset(converted_custom_tool_names or ()),
        )
        if content_prefix:
            prefix_response_content(response_payload, content_prefix)
        record_response_reasoning(
            response_payload,
            store,
            request_messages,
            cache_namespace,
            scope=scope,
            prior_messages=prior_messages,
            recording_contexts=recording_contexts,
            lineage=lineage,
            root=root,
            agent_id=agent_id,
        )
        if display_reasoning:
            fold_reasoning_into_content(response_payload, collapsible_reasoning)
        if "model" in response_payload:
            response_payload["model"] = original_model
    return json.dumps(
        response_payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def prefix_response_content(response_payload: dict[str, Any], prefix: str) -> bool:
    choices = response_payload.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        message["content"] = prefix + (content if isinstance(content, str) else "")
        return True
    return False
