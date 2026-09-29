from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Any

from .reasoning_store import ReasoningStore


THINKING_BLOCK_START = "<think>\n"
THINKING_BLOCK_END = "\n</think>\n\n"
COLLAPSIBLE_THINKING_BLOCK_START = "<details>\n<summary>Thinking</summary>\n\n"
COLLAPSIBLE_THINKING_BLOCK_END = "\n</details>\n\n"


@dataclass
class StreamingChoice:
    role: str = "assistant"
    content: str = ""
    reasoning_content: str = ""
    has_reasoning_content: bool = False
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None

    def to_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
        }
        if self.has_reasoning_content:
            message["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        return message


class StreamAccumulator:
    def __init__(self) -> None:
        self.choices: dict[int, StreamingChoice] = {}
        self._stored_choices: dict[tuple[int, str], str] = {}

    def ingest_chunk(self, chunk: dict[str, Any]) -> None:
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return

        for raw_choice in choices:
            if not isinstance(raw_choice, dict):
                continue
            index = int(raw_choice.get("index") or 0)
            choice = self.choices.setdefault(index, StreamingChoice())
            finish_reason = raw_choice.get("finish_reason")
            if isinstance(finish_reason, str):
                choice.finish_reason = finish_reason

            delta = raw_choice.get("delta")
            if not isinstance(delta, dict):
                continue

            role = delta.get("role")
            if isinstance(role, str) and role:
                choice.role = role

            content = delta.get("content")
            if isinstance(content, str):
                choice.content += content

            reasoning_content = delta.get("reasoning_content")
            if isinstance(reasoning_content, str):
                choice.has_reasoning_content = True
                choice.reasoning_content += reasoning_content

            self._merge_tool_call_deltas(choice, delta.get("tool_calls"))

    def store_reasoning(
        self,
        store: ReasoningStore,
        scope: str,
        cache_namespace: str = "",
        prior_messages: list[dict[str, Any]] | None = None,
        lineage: str = "",
        root: str = "",
        agent_id: str = "",
    ) -> int:
        """Cache completed reasoning for one agent conversation."""
        stored = 0
        for index, choice in self.choices.items():
            stored += self._store_choice(
                index,
                choice,
                store,
                scope,
                "final",
                cache_namespace,
                prior_messages,
                lineage,
                root,
                agent_id,
            )
        return stored

    def store_finished_reasoning(
        self,
        store: ReasoningStore,
        scope: str,
        cache_namespace: str = "",
        prior_messages: list[dict[str, Any]] | None = None,
        lineage: str = "",
        root: str = "",
        agent_id: str = "",
    ) -> int:
        stored = 0
        for index, choice in self.choices.items():
            if choice.finish_reason is not None:
                stored += self._store_choice(
                    index,
                    choice,
                    store,
                    scope,
                    "final",
                    cache_namespace,
                    prior_messages,
                    lineage,
                    root,
                    agent_id,
                )
        return stored

    def store_ready_reasoning(
        self,
        store: ReasoningStore,
        scope: str,
        cache_namespace: str = "",
        prior_messages: list[dict[str, Any]] | None = None,
        lineage: str = "",
        root: str = "",
        agent_id: str = "",
    ) -> int:
        """Cache reasoning as soon as a tool call has an id, before [DONE]."""
        stored = 0
        for index, choice in self.choices.items():
            if choice.finish_reason is not None:
                stored += self._store_choice(
                    index,
                    choice,
                    store,
                    scope,
                    "final",
                    cache_namespace,
                    prior_messages,
                    lineage,
                    root,
                    agent_id,
                )
            elif self._has_identified_tool_calls(choice):
                stored += self._store_choice(
                    index,
                    choice,
                    store,
                    scope,
                    "tool_call",
                    cache_namespace,
                    prior_messages,
                    lineage,
                    root,
                    agent_id,
                )
        return stored

    def messages(self) -> list[dict[str, Any]]:
        return [choice.to_message() for _, choice in sorted(self.choices.items())]

    def _merge_tool_call_deltas(self, choice: StreamingChoice, deltas: Any) -> None:
        if not isinstance(deltas, list):
            return

        for raw_delta in deltas:
            if not isinstance(raw_delta, dict):
                continue
            index = raw_delta.get("index")
            if not isinstance(index, int):
                index = len(choice.tool_calls)
            while len(choice.tool_calls) <= index:
                choice.tool_calls.append(
                    {"type": "function", "function": {"name": "", "arguments": ""}}
                )

            tool_call = choice.tool_calls[index]
            if raw_delta.get("id"):
                tool_call["id"] = raw_delta["id"]
            if raw_delta.get("type"):
                tool_call["type"] = raw_delta["type"]

            function_delta = raw_delta.get("function")
            if not isinstance(function_delta, dict):
                continue
            function = tool_call.setdefault("function", {"name": "", "arguments": ""})
            if function_delta.get("name"):
                existing_name = function.get("name") or ""
                new_name = str(function_delta["name"])
                function["name"] = (
                    new_name if not existing_name else existing_name + new_name
                )
            if (
                "arguments" in function_delta
                and function_delta["arguments"] is not None
            ):
                function["arguments"] = (function.get("arguments") or "") + str(
                    function_delta["arguments"]
                )

    def _store_choice(
        self,
        index: int,
        choice: StreamingChoice,
        store: ReasoningStore,
        scope: str,
        stage: str = "final",
        cache_namespace: str = "",
        prior_messages: list[dict[str, Any]] | None = None,
        lineage: str = "",
        root: str = "",
        agent_id: str = "",
    ) -> int:
        stage_rank = {"tool_call": 1, "final": 2}
        storage_key = (index, scope)
        previous_stage = self._stored_choices.get(storage_key)
        if stage_rank.get(previous_stage or "", 0) >= stage_rank.get(stage, 0):
            return 0
        stored = store.store_assistant_message(
            choice.to_message(),
            scope,
            cache_namespace,
            prior_messages,
            lineage=lineage,
            root=root,
            agent_id=agent_id,
        )
        if stored:
            self._stored_choices[storage_key] = stage
        return stored

    def _has_identified_tool_calls(self, choice: StreamingChoice) -> bool:
        if not choice.has_reasoning_content or not choice.tool_calls:
            return False
        return all(bool(tool_call.get("id")) for tool_call in choice.tool_calls)


class CursorReasoningDisplayAdapter:
    """Mirror reasoning_content into content for Cursor's visible thinking UI path."""

    def __init__(self, collapsible: bool = True) -> None:
        self._open_choices: set[int] = set()
        self._last_chunk_metadata: dict[str, Any] = {}
        self._block_start = (
            COLLAPSIBLE_THINKING_BLOCK_START if collapsible else THINKING_BLOCK_START
        )
        self._block_end = (
            COLLAPSIBLE_THINKING_BLOCK_END if collapsible else THINKING_BLOCK_END
        )

    def rewrite_chunk(self, chunk: dict[str, Any]) -> None:
        self._remember_chunk_metadata(chunk)
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return

        for raw_choice in choices:
            if not isinstance(raw_choice, dict):
                continue
            index = int(raw_choice.get("index") or 0)
            delta = raw_choice.get("delta")
            if not isinstance(delta, dict):
                delta = {}
                raw_choice["delta"] = delta

            mirrored_parts: list[str] = []
            reasoning_content = delta.get("reasoning_content")
            if isinstance(reasoning_content, str) and reasoning_content:
                if index not in self._open_choices:
                    mirrored_parts.append(self._block_start)
                    self._open_choices.add(index)
                mirrored_parts.append(reasoning_content)

            existing_content = delta.get("content")
            should_close = index in self._open_choices and (
                bool(existing_content)
                or bool(delta.get("tool_calls"))
                or raw_choice.get("finish_reason") is not None
            )
            if should_close:
                mirrored_parts.append(self._block_end)
                self._open_choices.discard(index)

            if not mirrored_parts:
                continue
            if isinstance(existing_content, str):
                mirrored_parts.append(existing_content)
            delta["content"] = "".join(mirrored_parts)

    def flush_chunk(self, model: str) -> dict[str, Any] | None:
        if not self._open_choices:
            return None

        choices = [
            {
                "index": index,
                "delta": {"content": self._block_end},
                "finish_reason": None,
            }
            for index in sorted(self._open_choices)
        ]
        self._open_choices.clear()

        chunk: dict[str, Any] = {
            "id": self._last_chunk_metadata.get("id", "chatcmpl-reasoning-close"),
            "object": self._last_chunk_metadata.get("object", "chat.completion.chunk"),
            "created": self._last_chunk_metadata.get("created", int(time.time())),
            "model": model,
            "choices": choices,
        }
        return chunk

    def _remember_chunk_metadata(self, chunk: dict[str, Any]) -> None:
        metadata = {
            key: chunk[key] for key in ("id", "object", "created") if key in chunk
        }
        if metadata:
            self._last_chunk_metadata.update(metadata)


def fold_reasoning_into_content(
    response_payload: dict[str, Any],
    collapsible: bool,
) -> None:
    """Mirror `reasoning_content` into the visible `content` field for
    non-streaming responses, matching the streaming `<details>` layout."""
    block_start = (
        COLLAPSIBLE_THINKING_BLOCK_START if collapsible else THINKING_BLOCK_START
    )
    block_end = COLLAPSIBLE_THINKING_BLOCK_END if collapsible else THINKING_BLOCK_END
    choices = response_payload.get("choices")
    if not isinstance(choices, list):
        return
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        reasoning = message.get("reasoning_content")
        if not isinstance(reasoning, str) or not reasoning:
            continue
        content = message.get("content")
        message["content"] = (
            block_start
            + reasoning
            + block_end
            + (content if isinstance(content, str) else "")
        )


def _unwrap_input_arguments(arguments: str) -> str:
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


@dataclass
class _ConvertedToolArgState:
    """Buffered stream state for one converted custom tool call."""

    name: str = ""
    arguments: str = ""
    call_id: str | None = None
    call_type: str | None = None
    converted: bool | None = None
    flushed: bool = False


class ConvertedCustomToolArgsAdapter:
    """Deliver converted tools as function calls with the raw patch text.

    DeepSeek streams ``{"input": "<patch>"}``. Cursor's parser requires
    ``type: function`` and ``function.name``, and ApplyPatch reads the
    arguments string as the patch itself.
    """

    def __init__(self, converted_names: frozenset[str] | set[str] | None = None) -> None:
        self._converted_names = frozenset(converted_names or ())
        self._state: dict[tuple[int, int], _ConvertedToolArgState] = {}
        self._last_chunk_metadata: dict[str, Any] = {}

    def rewrite_chunk(
        self,
        chunk: dict[str, Any],
        accumulator: StreamAccumulator | None = None,
    ) -> None:
        """Strip converted-tool arg fragments; emit unwrapped args before finish."""
        if not self._converted_names:
            return
        self._remember_chunk_metadata(chunk)
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return
        for raw_choice in choices:
            if not isinstance(raw_choice, dict):
                continue
            choice_index = int(raw_choice.get("index") or 0)
            delta = raw_choice.get("delta")
            if not isinstance(delta, dict):
                delta = {}
                raw_choice["delta"] = delta
            self._filter_tool_call_deltas(choice_index, delta)
            if raw_choice.get("finish_reason") is not None:
                flushed = self._flush_choice(choice_index)
                if flushed:
                    delta.setdefault("tool_calls", [])
                    delta["tool_calls"].extend(flushed)
                if accumulator is not None:
                    self._unwrap_accumulator_choice(accumulator, choice_index)

    def flush_chunks(self, accumulator: StreamAccumulator | None = None) -> list[dict[str, Any]]:
        """Emit any still-buffered converted-tool arguments at stream end."""
        if not self._converted_names:
            return []
        by_choice: dict[int, list[dict[str, Any]]] = {}
        for (choice_index, _tool_index), state in list(self._state.items()):
            if state.flushed or not state.converted:
                continue
            flushed = self._flush_choice(choice_index)
            if flushed:
                by_choice.setdefault(choice_index, []).extend(flushed)
            if accumulator is not None:
                self._unwrap_accumulator_choice(accumulator, choice_index)
        chunks: list[dict[str, Any]] = []
        for choice_index, deltas in sorted(by_choice.items()):
            chunks.append(
                {
                    "id": self._last_chunk_metadata.get(
                        "id", "chatcmpl-converted-tool-args"
                    ),
                    "object": self._last_chunk_metadata.get(
                        "object", "chat.completion.chunk"
                    ),
                    "created": self._last_chunk_metadata.get(
                        "created", int(time.time())
                    ),
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

    def _filter_tool_call_deltas(self, choice_index: int, delta: dict[str, Any]) -> None:
        raw_calls = delta.get("tool_calls")
        if not isinstance(raw_calls, list):
            return
        kept: list[dict[str, Any]] = []
        for raw in raw_calls:
            if not isinstance(raw, dict):
                continue
            rewritten = self._consume_tool_call_delta(choice_index, raw)
            if rewritten is not None:
                kept.append(rewritten)
        if kept:
            delta["tool_calls"] = kept
        else:
            delta.pop("tool_calls", None)

    def _consume_tool_call_delta(
        self,
        choice_index: int,
        raw: dict[str, Any],
    ) -> dict[str, Any] | None:
        tool_index = raw.get("index")
        if not isinstance(tool_index, int):
            tool_index = 0
        state = self._state.setdefault(
            (choice_index, tool_index), _ConvertedToolArgState()
        )
        function = raw.get("function")
        if not isinstance(function, dict):
            function = {}
        if function.get("name"):
            state.name += str(function["name"])
            state.converted = state.name in self._converted_names
        if raw.get("id"):
            state.call_id = str(raw["id"])
        if raw.get("type"):
            state.call_type = str(raw["type"])
        if "arguments" in function and function["arguments"] is not None:
            state.arguments += str(function["arguments"])

        # Hold converted calls until the JSON arguments can be unwrapped.
        # Cursor rejects a tool delta that is not type=function with a name.
        if state.converted is None:
            return None
        if state.converted:
            return None

        # Name resolved to a normal function: forward args (including any held).
        outbound = dict(raw)
        if state.call_id and "id" not in outbound:
            outbound["id"] = state.call_id
        if state.call_type and "type" not in outbound:
            outbound["type"] = state.call_type
        if state.arguments:
            outbound_fn = dict(function)
            if state.name and "name" not in outbound_fn:
                outbound_fn["name"] = state.name
            outbound_fn["arguments"] = state.arguments
            outbound["function"] = outbound_fn
            state.arguments = ""
            return outbound
        return outbound

    def _flush_choice(self, choice_index: int) -> list[dict[str, Any]]:
        """Emit one function delta whose arguments are the raw patch."""
        released: list[dict[str, Any]] = []
        for (held_choice, tool_index), state in self._state.items():
            if held_choice != choice_index or state.flushed:
                continue
            if state.converted is not True:
                continue
            state.flushed = True
            released.append(_function_tool_delta(tool_index, state))
        return released

    def _unwrap_accumulator_choice(
        self,
        accumulator: StreamAccumulator,
        choice_index: int,
    ) -> None:
        choice = accumulator.choices.get(choice_index)
        if choice is None:
            return
        for tool_index, tool_call in enumerate(choice.tool_calls):
            state = self._state.get((choice_index, tool_index))
            if state is None or state.converted is not True:
                continue
            function = tool_call.setdefault(
                "function", {"name": "", "arguments": ""}
            )
            function["arguments"] = _unwrap_input_arguments(
                state.arguments or str(function.get("arguments") or "")
            )

    def _remember_chunk_metadata(self, chunk: dict[str, Any]) -> None:
        metadata = {
            key: chunk[key] for key in ("id", "object", "created") if key in chunk
        }
        if metadata:
            self._last_chunk_metadata.update(metadata)


def _function_tool_delta(
    tool_index: int,
    state: _ConvertedToolArgState,
) -> dict[str, Any]:
    """One OpenAI tool delta Cursor's agent parser can execute."""
    delta: dict[str, Any] = {
        "index": tool_index,
        "type": "function",
        "function": {
            "name": state.name,
            "arguments": _unwrap_input_arguments(state.arguments),
        },
    }
    if state.call_id:
        delta["id"] = state.call_id
    return delta


def _tool_call_delta_without_arguments(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Copy a tool_call delta with function.arguments removed."""
    outbound = {key: value for key, value in raw.items() if key != "function"}
    function = raw.get("function")
    if isinstance(function, dict):
        outbound_fn = {
            key: value for key, value in function.items() if key != "arguments"
        }
        if outbound_fn:
            outbound["function"] = outbound_fn
    if not (outbound.keys() - {"index"}):
        return None
    return outbound
