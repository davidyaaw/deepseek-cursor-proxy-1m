"""Pure-function unit tests for transform.py.

Anything that requires a fake DeepSeek upstream lives in test_protocol.py.
This file only exercises helpers that take dicts/strings and return
dicts/strings — content extraction, request normalization, response rewrite,
recovery-notice stripping, and warning behaviour for dropped fields.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import unittest

from deepseek_cursor_proxy.config import ProxyConfig
from deepseek_cursor_proxy.reasoning_store import (
    ReasoningStore,
    conversation_lineage,
    conversation_scope,
    message_signature,
)
from deepseek_cursor_proxy.transform import (
    PRIOR_REASONING_UNAVAILABLE,
    RECOVERY_NOTICE_CONTENT,
    RECOVERY_NOTICE_TEXT,
    extract_text_content,
    normalize_reasoning_effort,
    prepare_upstream_request,
    reasoning_cache_namespace,
    rewrite_response_body,
    strip_cursor_thinking_blocks,
    strip_recovery_notice_for_upstream,
)
from deepseek_cursor_proxy.streaming import (
    ConvertedCustomToolArgsAdapter,
    StreamAccumulator,
)


def _default_cache_namespace() -> str:
    return reasoning_cache_namespace(
        ProxyConfig(),
        "deepseek-v4-pro",
        {"type": "enabled"},
        "max",
    )


def _cache_scope(messages: list[dict]) -> str:
    return conversation_scope(messages, _default_cache_namespace())


class ContentHelpersTests(unittest.TestCase):
    def test_extract_text_content_flattens_multipart_array(self) -> None:
        content = [
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
            {"type": "input_text", "text": "world"},
        ]
        self.assertEqual(
            extract_text_content(content),
            "hello\n[image_url omitted by DeepSeek text proxy]\nworld",
        )

    def test_extract_text_content_passes_through_string_and_none(self) -> None:
        self.assertEqual(extract_text_content("plain"), "plain")
        self.assertIsNone(extract_text_content(None))

    def test_strip_cursor_thinking_blocks_removes_details_and_think(self) -> None:
        self.assertEqual(
            strip_cursor_thinking_blocks(
                "<details>\n<summary>Thinking</summary>\n\nplan\n</details>\n\nanswer"
            ),
            "answer",
        )
        self.assertEqual(
            strip_cursor_thinking_blocks("<think>\nplan\n</think>\n\nanswer"),
            "answer",
        )

    def test_strip_cursor_thinking_blocks_preserves_unrelated_details(self) -> None:
        kept = "<details><summary>Diff</summary>\nrelevant\n</details>"
        self.assertEqual(strip_cursor_thinking_blocks(kept), kept)

    def test_normalize_reasoning_effort_aliases(self) -> None:
        self.assertEqual(normalize_reasoning_effort("none"), "none")
        self.assertEqual(normalize_reasoning_effort("minimal"), "low")
        self.assertEqual(normalize_reasoning_effort("low"), "low")
        self.assertEqual(normalize_reasoning_effort("medium"), "high")
        self.assertEqual(normalize_reasoning_effort("high"), "high")
        self.assertEqual(normalize_reasoning_effort("max"), "max")
        self.assertEqual(normalize_reasoning_effort("xhigh"), "max")
        self.assertEqual(normalize_reasoning_effort("nonsense"), "high")


class RequestPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReasoningStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_legacy_functions_field_is_converted_to_tools(self) -> None:
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hi"}],
                "functions": [{"name": "lookup", "parameters": {"type": "object"}}],
                "function_call": "auto",
            },
            ProxyConfig(),
            self.store,
        )
        self.assertEqual(prepared.payload["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(prepared.payload["tool_choice"], "auto")
        self.assertNotIn("functions", prepared.payload)
        self.assertNotIn("function_call", prepared.payload)

    def test_named_function_call_becomes_named_tool_choice(self) -> None:
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hi"}],
                "function_call": {"name": "lookup"},
            },
            ProxyConfig(),
            self.store,
        )
        self.assertEqual(
            prepared.payload["tool_choice"],
            {"type": "function", "function": {"name": "lookup"}},
        )

    def test_custom_tool_call_in_history_is_sent_as_function(self) -> None:
        patch_text = "*** Begin Patch\n*** Add File: proxy-check.txt\n+after\n*** End Patch"
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "user", "content": "create the file"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_patch",
                                "type": "custom",
                                "custom": {
                                    "name": "ApplyPatch",
                                    "input": patch_text,
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_patch",
                        "content": "applied",
                    },
                    {"role": "user", "content": "read it"},
                ],
                "tools": [{"type": "custom", "name": "ApplyPatch"}],
            },
            ProxyConfig(thinking="disabled"),
            self.store,
        )
        assistant = prepared.payload["messages"][1]
        self.assertEqual(assistant["tool_calls"][0]["type"], "function")
        self.assertEqual(
            json.loads(assistant["tool_calls"][0]["function"]["arguments"]),
            {"input": patch_text},
        )

    def test_raw_function_patch_in_history_is_wrapped_for_deepseek(self) -> None:
        patch_text = "*** Begin Patch\n*** Add File: proxy-check.txt\n+after\n*** End Patch"
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "user", "content": "create the file"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_patch",
                                "type": "function",
                                "function": {
                                    "name": "ApplyPatch",
                                    "arguments": patch_text,
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_patch",
                        "content": "applied",
                    },
                ],
                "tools": [{"type": "custom", "name": "ApplyPatch"}],
            },
            ProxyConfig(thinking="disabled"),
            self.store,
        )
        assistant = prepared.payload["messages"][1]
        self.assertEqual(
            json.loads(assistant["tool_calls"][0]["function"]["arguments"]),
            {"input": patch_text},
        )

    def test_custom_tool_type_is_converted_to_function(self) -> None:
        # Cursor sends OpenAI-only free-form tools (`type: "custom"`) for
        # ApplyPatch. DeepSeek only accepts function tools, so the proxy maps
        # a valid custom name onto a single required string `input` property.
        with self.assertNoLogs("deepseek_cursor_proxy", level="WARNING"):
            prepared = prepare_upstream_request(
                {
                    "model": "deepseek-flash",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "parameters": {"type": "object"},
                            },
                        },
                        {
                            "type": "custom",
                            "name": "apply_patch",
                            "description": "Free-form patch tool",
                        },
                    ],
                },
                ProxyConfig(),
                self.store,
            )
        tools = prepared.payload["tools"]
        self.assertEqual(
            [tool["function"]["name"] for tool in tools],
            ["read_file", "apply_patch"],
        )
        apply_patch = tools[1]
        self.assertEqual(apply_patch["type"], "function")
        self.assertEqual(
            apply_patch["function"]["parameters"],
            {
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
        )
        self.assertIn("Free-form patch tool", apply_patch["function"]["description"])
        self.assertIn("input", apply_patch["function"]["description"])
        self.assertEqual(prepared.converted_custom_tool_names, frozenset({"apply_patch"}))

    def test_tools_and_tool_choice_keep_converted_custom_tool(self) -> None:
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "custom", "name": "apply_patch"}],
                "tool_choice": {"type": "custom", "name": "apply_patch"},
            },
            ProxyConfig(),
            self.store,
        )
        self.assertEqual(
            prepared.payload["tools"][0]["function"]["name"], "apply_patch"
        )
        self.assertEqual(
            prepared.payload["tool_choice"],
            {"type": "function", "function": {"name": "apply_patch"}},
        )
        self.assertEqual(prepared.converted_custom_tool_names, frozenset({"apply_patch"}))

    def test_tool_choice_to_a_converted_custom_tool_is_kept(self) -> None:
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "read_file", "parameters": {}},
                    },
                    {"type": "custom", "name": "apply_patch"},
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "apply_patch"},
                },
            },
            ProxyConfig(),
            self.store,
        )
        self.assertEqual(
            [tool["function"]["name"] for tool in prepared.payload["tools"]],
            ["read_file", "apply_patch"],
        )
        self.assertEqual(
            prepared.payload["tool_choice"],
            {"type": "function", "function": {"name": "apply_patch"}},
        )

    def test_custom_tool_with_invalid_name_is_dropped(self) -> None:
        with self.assertLogs("deepseek_cursor_proxy", level="WARNING") as captured:
            prepared = prepare_upstream_request(
                {
                    "model": "deepseek-flash",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [
                        {"type": "custom", "name": "bad name!"},
                        {
                            "type": "function",
                            "function": {"name": "read_file", "parameters": {}},
                        },
                    ],
                },
                ProxyConfig(),
                self.store,
            )
        self.assertEqual(
            [tool["function"]["name"] for tool in prepared.payload["tools"]],
            ["read_file"],
        )
        self.assertIn("bad name!", "\n".join(captured.output))
        self.assertEqual(prepared.converted_custom_tool_names, frozenset())

    def test_converted_tool_arguments_are_unwrapped_in_response(self) -> None:
        patch_text = "*** Begin Patch\n*** Update File: a.py\n*** End Patch"
        body = json.dumps(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_patch",
                                    "type": "function",
                                    "function": {
                                        "name": "ApplyPatch",
                                        "arguments": json.dumps({"input": patch_text}),
                                    },
                                },
                                {
                                    "id": "call_read",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps(
                                            {"input": "should stay wrapped"}
                                        ),
                                    },
                                },
                            ],
                        },
                    }
                ],
            }
        ).encode()
        rewritten = rewrite_response_body(
            body,
            "deepseek-flash",
            self.store,
            [{"role": "user", "content": "edit"}],
            converted_custom_tool_names=frozenset({"ApplyPatch"}),
        )
        tool_calls = json.loads(rewritten)["choices"][0]["message"]["tool_calls"]
        self.assertEqual(tool_calls[0]["type"], "function")
        self.assertEqual(tool_calls[0]["id"], "call_patch")
        self.assertEqual(tool_calls[0]["function"]["name"], "ApplyPatch")
        self.assertEqual(tool_calls[0]["function"]["arguments"], patch_text)
        self.assertNotIn("custom", tool_calls[0])
        self.assertEqual(
            tool_calls[1]["function"]["arguments"],
            json.dumps({"input": "should stay wrapped"}),
        )

    def test_converted_tool_arguments_are_unwrapped_when_streaming(self) -> None:
        patch_text = "*** Begin Patch"
        wrapped = json.dumps({"input": patch_text})
        accumulator = StreamAccumulator()
        adapter = ConvertedCustomToolArgsAdapter({"ApplyPatch"})
        chunks = [
            {
                "id": "chatcmpl-stream",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_patch",
                                    "type": "function",
                                    "function": {"name": "ApplyPatch"},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-stream",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": wrapped[:8]},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-stream",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": wrapped[8:]},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-stream",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        ]
        outbound_calls: list[dict] = []
        for chunk in chunks:
            accumulator.ingest_chunk(chunk)
            adapter.rewrite_chunk(chunk, accumulator)
            for choice in chunk.get("choices") or []:
                for tool_call in (choice.get("delta") or {}).get("tool_calls") or []:
                    outbound_calls.append(tool_call)
        self.assertEqual(len(outbound_calls), 1)
        self.assertEqual(outbound_calls[0]["type"], "function")
        self.assertEqual(outbound_calls[0]["id"], "call_patch")
        self.assertEqual(outbound_calls[0]["function"]["name"], "ApplyPatch")
        self.assertEqual(outbound_calls[0]["function"]["arguments"], patch_text)
        self.assertNotIn("custom", outbound_calls[0])
        self.assertEqual(
            accumulator.choices[0].tool_calls[0]["function"]["arguments"],
            patch_text,
        )

    def test_normal_function_tool_arguments_are_not_rewritten(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_read",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"a.py"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ).encode()
        rewritten = rewrite_response_body(
            body,
            "deepseek-flash",
            self.store,
            [],
            converted_custom_tool_names=frozenset({"ApplyPatch"}),
        )
        self.assertEqual(
            json.loads(rewritten)["choices"][0]["message"]["tool_calls"][0][
                "function"
            ]["arguments"],
            '{"path":"a.py"}',
        )

    def test_function_tool_without_name_is_dropped(self) -> None:
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {"type": "function"},
                    {
                        "type": "function",
                        "function": {"name": "read_file", "parameters": {}},
                    },
                ],
            },
            ProxyConfig(),
            self.store,
        )
        self.assertEqual(
            [tool["function"]["name"] for tool in prepared.payload["tools"]],
            ["read_file"],
        )

    def test_max_completion_tokens_is_aliased_to_max_tokens(self) -> None:
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hi"}],
                "max_completion_tokens": 256,
            },
            ProxyConfig(),
            self.store,
        )
        self.assertEqual(prepared.payload["max_tokens"], 256)

    def test_standard_openai_fields_are_forwarded_without_warning(self) -> None:
        # Cursor and the OpenAI SDK send these on every request; forward them
        # so DeepSeek can use what it understands and ignore the rest.
        with self.assertNoLogs("deepseek_cursor_proxy", level="WARNING"):
            prepared = prepare_upstream_request(
                {
                    "model": "deepseek-v4-pro",
                    "messages": [{"role": "user", "content": "hi"}],
                    "user": "user-abc",
                    "seed": 42,
                    "n": 1,
                    "logit_bias": {"50256": -100},
                },
                ProxyConfig(),
                self.store,
            )
        self.assertEqual(prepared.payload["user"], "user-abc")
        self.assertEqual(prepared.payload["seed"], 42)
        self.assertEqual(prepared.payload["n"], 1)
        self.assertEqual(prepared.payload["logit_bias"], {"50256": -100})

    def test_unknown_request_fields_are_dropped_with_warning(self) -> None:
        with self.assertLogs("deepseek_cursor_proxy", level="WARNING") as captured:
            prepared = prepare_upstream_request(
                {
                    "model": "deepseek-v4-pro",
                    "messages": [{"role": "user", "content": "hi"}],
                    "parallel_tool_calls": True,
                    "service_tier": "fast",
                },
                ProxyConfig(),
                self.store,
            )
        self.assertNotIn("parallel_tool_calls", prepared.payload)
        self.assertNotIn("service_tier", prepared.payload)
        log = "\n".join(captured.output)
        self.assertIn("parallel_tool_calls", log)
        self.assertIn("service_tier", log)

    def test_non_deepseek_model_is_rewritten_with_warning(self) -> None:
        with self.assertLogs("deepseek_cursor_proxy", level="WARNING") as captured:
            prepared = prepare_upstream_request(
                {
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                ProxyConfig(upstream_model="deepseek-v4-pro"),
                self.store,
            )
        self.assertEqual(prepared.payload["model"], "deepseek-v4-pro")
        self.assertIn("non-DeepSeek", "\n".join(captured.output))

    def test_gpt56_sol_rewrites_to_v4_pro(self) -> None:
        """Cursor GPT-5.6 Sol (and effort suffixes) map to DeepSeek V4 Pro."""
        for model in (
            "gpt-5.6-sol",
            "gpt-5.6-sol-high",
            "gpt-5.6-sol-xhigh-fast",
            "gpt-5.6-sol-high[1m]",
        ):
            prepared = prepare_upstream_request(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                ProxyConfig(upstream_model="deepseek-flash"),
                self.store,
            )
            self.assertEqual(prepared.payload["model"], "deepseek-v4-pro", model)
            self.assertEqual(prepared.upstream_model, "deepseek-v4-pro", model)

    def test_gpt56_terra_rewrites_to_flash(self) -> None:
        """Cursor GPT-5.6 Terra (and effort suffixes) map to DeepSeek Flash."""
        for model in (
            "gpt-5.6-terra",
            "gpt-5.6-terra-high",
            "gpt-5.6-terra-low-fast",
        ):
            prepared = prepare_upstream_request(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                ProxyConfig(upstream_model="deepseek-v4-pro"),
                self.store,
            )
            self.assertEqual(prepared.payload["model"], "deepseek-flash", model)
            self.assertEqual(prepared.upstream_model, "deepseek-flash", model)

    def test_cursor_effort_suffix_is_forwarded_to_deepseek(self) -> None:
        """Cursor encodes Effort in the model slug when BYOK drops reasoning_effort."""
        prepared = prepare_upstream_request(
            {
                "model": "gpt-5.6-sol-low",
                "messages": [{"role": "user", "content": "hi"}],
            },
            ProxyConfig(reasoning_effort="max"),
            self.store,
        )
        self.assertEqual(prepared.payload["reasoning_effort"], "low")
        self.assertEqual(prepared.payload["thinking"], {"type": "enabled"})

        extra_high = prepare_upstream_request(
            {
                "model": "gpt-5.6-terra-xhigh",
                "messages": [{"role": "user", "content": "hi"}],
            },
            ProxyConfig(reasoning_effort="high"),
            self.store,
        )
        self.assertEqual(extra_high.payload["reasoning_effort"], "max")
        self.assertEqual(extra_high.payload["model"], "deepseek-flash")

    def test_request_reasoning_effort_beats_model_suffix_and_config(self) -> None:
        prepared = prepare_upstream_request(
            {
                "model": "gpt-5.6-sol-low",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
            ProxyConfig(reasoning_effort="max"),
            self.store,
        )
        self.assertEqual(prepared.payload["reasoning_effort"], "high")

        nested = prepare_upstream_request(
            {
                "model": "gpt-5.6-terra",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning": {"effort": "xhigh"},
            },
            ProxyConfig(reasoning_effort="low"),
            self.store,
        )
        self.assertEqual(nested.payload["reasoning_effort"], "max")
        self.assertNotIn("reasoning", nested.payload)

    def test_cursor_none_effort_disables_thinking(self) -> None:
        prepared = prepare_upstream_request(
            {
                "model": "gpt-5.6-sol-none",
                "messages": [{"role": "user", "content": "hi"}],
            },
            ProxyConfig(thinking="enabled", reasoning_effort="max"),
            self.store,
        )
        self.assertEqual(prepared.payload["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", prepared.payload)

    def test_missing_cursor_effort_falls_back_to_config(self) -> None:
        prepared = prepare_upstream_request(
            {
                "model": "gpt-5.6-sol",
                "messages": [{"role": "user", "content": "hi"}],
            },
            ProxyConfig(
                reasoning_effort="max",
                cursor_effort_path=Path("missing-cursor-effort.json"),
            ),
            self.store,
        )
        self.assertEqual(prepared.payload["reasoning_effort"], "max")

    def test_saved_cursor_effort_overrides_config(self) -> None:
        """Cursor's picker is stored beside the config because BYOK omits the field."""
        from tempfile import TemporaryDirectory

        from deepseek_cursor_proxy.cursor_effort import remember_cursor_effort

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            effort_path = root / "cursor-effort.json"
            config_path = root / "config.yaml"
            config_path.write_text("reasoning_effort: high\n", encoding="utf-8")
            remember_cursor_effort("gpt-5.6-sol", "max", effort_path, config_path)
            remember_cursor_effort("gpt-5.6-terra", "medium", effort_path, config_path)
            sol = prepare_upstream_request(
                {
                    "model": "gpt-5.6-sol",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                ProxyConfig(reasoning_effort="high", cursor_effort_path=effort_path),
                self.store,
            )
            terra = prepare_upstream_request(
                {
                    "model": "gpt-5.6-terra",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                ProxyConfig(reasoning_effort="high", cursor_effort_path=effort_path),
                self.store,
            )
            config_text = config_path.read_text(encoding="utf-8")

        self.assertEqual(sol.payload["model"], "deepseek-v4-pro")
        self.assertEqual(sol.payload["reasoning_effort"], "max")
        self.assertEqual(terra.payload["model"], "deepseek-flash")
        self.assertEqual(terra.payload["reasoning_effort"], "high")
        self.assertEqual(terra.cursor_effort, "medium")
        self.assertIn("reasoning_effort: medium", config_text)

    def test_client_1m_context_marker_is_stripped_before_upstream(self) -> None:
        # Claude Code / Cherry Studio decorate 1M-capable model ids with a
        # `[1m]` suffix that DeepSeek's OpenAI-format API rejects with a 400.
        # The proxy must accept it (that is what makes the client's 1M budget
        # work) while sending the bare model name upstream.
        with self.assertNoLogs("deepseek_cursor_proxy", level="WARNING"):
            prepared = prepare_upstream_request(
                {
                    "model": "deepseek-flash[1m]",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                ProxyConfig(),
                self.store,
            )
        self.assertEqual(prepared.original_model, "deepseek-flash[1m]")
        self.assertEqual(prepared.upstream_model, "deepseek-flash")
        self.assertEqual(prepared.payload["model"], "deepseek-flash")

    def test_1m_context_marker_does_not_split_reasoning_cache(self) -> None:
        # Switching the model id between `deepseek-flash` and
        # `deepseek-flash[1m]` mid-conversation must not orphan the cached
        # reasoning, since both resolve to the same upstream model.
        messages = [{"role": "user", "content": "read README"}]
        plain = prepare_upstream_request(
            {"model": "deepseek-flash", "messages": messages},
            ProxyConfig(),
            self.store,
        )
        marked = prepare_upstream_request(
            {"model": "deepseek-flash[1M]", "messages": messages},
            ProxyConfig(),
            self.store,
        )
        self.assertEqual(plain.cache_namespace, marked.cache_namespace)

    def test_thinking_disabled_strips_reasoning_from_assistant_history(self) -> None:
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {
                        "role": "assistant",
                        "content": "answer",
                        "reasoning_content": "should be discarded",
                    },
                ],
            },
            ProxyConfig(thinking="disabled"),
            self.store,
        )
        self.assertEqual(prepared.payload["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_content", prepared.payload["messages"][1])

    def test_plain_chat_history_does_not_require_reasoning(self) -> None:
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                    {"role": "user", "content": "again"},
                ],
            },
            ProxyConfig(),
            self.store,
        )
        self.assertEqual(prepared.missing_reasoning_messages, 0)
        self.assertNotIn("reasoning_content", prepared.payload["messages"][1])

    def test_tools_request_restores_reasoning_on_plain_assistant(self) -> None:
        """DeepSeek thinking mode rejects any assistant turn once tools are set."""
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "read_file", "parameters": {}},
                    }
                ],
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                    {"role": "user", "content": "again"},
                ],
            },
            ProxyConfig(),
            self.store,
        )
        self.assertEqual(prepared.missing_reasoning_messages, 0)
        self.assertEqual(prepared.recovery_dropped_messages, 0)
        self.assertEqual(
            [message["role"] for message in prepared.payload["messages"]],
            ["user", "assistant", "user"],
        )
        self.assertEqual(
            prepared.payload["messages"][1]["reasoning_content"],
            PRIOR_REASONING_UNAVAILABLE,
        )

    def test_tools_request_keeps_history_when_only_some_reasoning_is_cached(
        self,
    ) -> None:
        history_before_tool = [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "noted"},
            {"role": "user", "content": "read the file"},
        ]
        tool_call = {
            "id": "call_read",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }
        namespace = _default_cache_namespace()
        lineage = conversation_lineage(history_before_tool)
        self.store.store_assistant_message(
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "Need the file.",
                "tool_calls": [tool_call],
            },
            conversation_scope(history_before_tool, namespace, lineage),
            namespace,
            history_before_tool,
            lineage=lineage,
        )
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "read_file", "parameters": {}},
                    }
                ],
                "messages": [
                    *history_before_tool,
                    {"role": "assistant", "content": "", "tool_calls": [tool_call]},
                    {
                        "role": "tool",
                        "tool_call_id": "call_read",
                        "content": "contents",
                    },
                ],
            },
            ProxyConfig(),
            self.store,
        )
        self.assertEqual(prepared.recovery_dropped_messages, 0)
        self.assertEqual(len(prepared.payload["messages"]), 5)
        self.assertEqual(
            prepared.payload["messages"][1]["reasoning_content"],
            PRIOR_REASONING_UNAVAILABLE,
        )
        self.assertEqual(
            prepared.payload["messages"][3]["reasoning_content"],
            "Need the file.",
        )

    def test_uncached_plain_assistant_does_not_drop_uncached_tool_turn(self) -> None:
        tool_call = {
            "id": "call_read",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "read_file", "parameters": {}},
                    }
                ],
                "messages": [
                    {"role": "user", "content": "earlier"},
                    {"role": "assistant", "content": "noted"},
                    {"role": "user", "content": "read the file"},
                    {"role": "assistant", "content": "", "tool_calls": [tool_call]},
                    {
                        "role": "tool",
                        "tool_call_id": "call_read",
                        "content": "contents",
                    },
                ],
            },
            ProxyConfig(),
            self.store,
        )
        self.assertEqual(prepared.recovery_dropped_messages, 0)
        self.assertEqual(
            [message["role"] for message in prepared.payload["messages"]],
            ["user", "assistant", "user", "assistant", "tool"],
        )
        self.assertEqual(
            prepared.payload["messages"][1]["reasoning_content"],
            PRIOR_REASONING_UNAVAILABLE,
        )
        self.assertEqual(
            prepared.payload["messages"][3]["reasoning_content"],
            PRIOR_REASONING_UNAVAILABLE,
        )

    def test_blank_reasoning_on_custom_tool_call_is_restored_from_cache(
        self,
    ) -> None:
        prior = [{"role": "user", "content": "create the file"}]
        patch_text = (
            "*** Begin Patch\n*** Add File: proxy-check.txt\n+after\n*** End Patch"
        )
        namespace = _default_cache_namespace()
        lineage = conversation_lineage(prior)
        self.store.store_assistant_message(
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "Add the file with ApplyPatch.",
                "tool_calls": [
                    {
                        "id": "call_patch",
                        "type": "function",
                        "function": {
                            "name": "ApplyPatch",
                            "arguments": patch_text,
                        },
                    }
                ],
            },
            conversation_scope(prior, namespace, lineage),
            namespace,
            prior,
            lineage=lineage,
        )
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "tools": [{"type": "custom", "name": "ApplyPatch"}],
                "messages": [
                    *prior,
                    {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "",
                        "tool_calls": [
                            {
                                "id": "call_patch",
                                "type": "custom",
                                "custom": {
                                    "name": "ApplyPatch",
                                    "input": patch_text,
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_patch",
                        "content": "applied",
                    },
                ],
            },
            ProxyConfig(),
            self.store,
        )
        self.assertEqual(
            prepared.payload["messages"][1]["reasoning_content"],
            "Add the file with ApplyPatch.",
        )
        self.assertEqual(prepared.missing_reasoning_messages, 0)

    def test_echoed_thinking_block_fills_reasoning_when_cache_misses(self) -> None:
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "tools": [{"type": "custom", "name": "ApplyPatch"}],
                "messages": [
                    {"role": "user", "content": "create the file"},
                    {
                        "role": "assistant",
                        "content": (
                            "<details>\n<summary>Thinking</summary>\n\n"
                            "Add proxy-check.txt via ApplyPatch.\n</details>\n\n"
                        ),
                    },
                ],
            },
            ProxyConfig(),
            self.store,
        )
        assistant = prepared.payload["messages"][1]
        self.assertEqual(
            assistant["reasoning_content"],
            "Add proxy-check.txt via ApplyPatch.",
        )
        self.assertNotIn("<details>", assistant["content"])
        self.assertEqual(prepared.recovery_dropped_messages, 0)


class RecoveryNoticeStrippingTests(unittest.TestCase):
    def test_strips_only_the_recovery_notice_prefix(self) -> None:
        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": RECOVERY_NOTICE_CONTENT + "real answer",
            },
            {"role": "assistant", "content": "ordinary"},
        ]
        result = strip_recovery_notice_for_upstream(messages)
        self.assertEqual(result[1]["content"], "real answer")
        self.assertEqual(result[2]["content"], "ordinary")

    def test_returns_a_copy_so_caller_keeps_with_prefix_messages(self) -> None:
        original = [
            {
                "role": "assistant",
                "content": RECOVERY_NOTICE_CONTENT + "answer",
            }
        ]
        stripped = strip_recovery_notice_for_upstream(original)
        # The cache scope is computed on the with-prefix history, so the
        # caller's list must NOT be mutated in place.
        self.assertEqual(original[0]["content"], RECOVERY_NOTICE_CONTENT + "answer")
        self.assertEqual(stripped[0]["content"], "answer")
        self.assertIsNot(stripped[0], original[0])

    def test_text_constant_matches_content_prefix(self) -> None:
        # Sanity check that the user-visible text used as a boundary marker
        # is consistent with the wire-format prefix.
        self.assertTrue(RECOVERY_NOTICE_CONTENT.startswith(RECOVERY_NOTICE_TEXT))


class ResponseRewriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ReasoningStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_records_reasoning_and_restores_original_model_name(self) -> None:
        body = json.dumps(
            {
                "id": "chatcmpl",
                "object": "chat.completion",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "Final.",
                            "reasoning_content": "Done thinking.",
                        },
                    }
                ],
            }
        ).encode()
        request_messages = [{"role": "user", "content": "hi"}]
        rewritten = rewrite_response_body(
            body, "deepseek-v4-pro", self.store, request_messages
        )
        payload = json.loads(rewritten)
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        stored = self.store.get(
            f"scope:{conversation_scope(request_messages)}:signature:"
            f"{message_signature(payload['choices'][0]['message'])}"
        )
        self.assertEqual(stored, "Done thinking.")

    def test_recovery_notice_is_prefixed_into_response_content(self) -> None:
        body = json.dumps(
            {
                "id": "chatcmpl",
                "object": "chat.completion",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Final."},
                    }
                ],
            }
        ).encode()
        rewritten = rewrite_response_body(
            body,
            "deepseek-v4-pro",
            self.store,
            [{"role": "user", "content": "hi"}],
            content_prefix=RECOVERY_NOTICE_CONTENT,
        )
        self.assertIn(
            RECOVERY_NOTICE_CONTENT,
            json.loads(rewritten)["choices"][0]["message"]["content"],
        )

    def test_preserves_prompt_cache_usage_fields(self) -> None:
        body = json.dumps(
            {
                "id": "chatcmpl",
                "object": "chat.completion",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "prompt_cache_hit_tokens": 6,
                    "prompt_cache_miss_tokens": 4,
                    "completion_tokens": 1,
                    "total_tokens": 11,
                },
            }
        ).encode()
        rewritten = rewrite_response_body(body, "deepseek-v4-flash", self.store, [])
        usage = json.loads(rewritten)["usage"]
        self.assertEqual(usage["prompt_cache_hit_tokens"], 6)
        self.assertEqual(usage["prompt_cache_miss_tokens"], 4)


class CrossModeAndModelTests(unittest.TestCase):
    """Regression coverage for PR #28's cross-mode/model context preservation
    (Pro↔Flash family normalization, portable turn-scoped keys, recovery
    boundary continuation). Originally shipped with PR #28 in test_transform.py
    and dropped by PR #33's test refactor; restored from commit 5f14da3."""

    def setUp(self) -> None:
        self.store = ReasoningStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_deepseek_pro_and_flash_share_reasoning_namespace(self) -> None:
        config = ProxyConfig()
        namespace_pro = reasoning_cache_namespace(
            config,
            "deepseek-v4-pro",
            {"type": "enabled"},
            "max",
            "Bearer key-a",
        )
        namespace_flash = reasoning_cache_namespace(
            config,
            "deepseek-v4-flash",
            {"type": "enabled"},
            "max",
            "Bearer key-a",
        )
        self.assertEqual(namespace_pro, namespace_flash)

        prior = [{"role": "user", "content": "read README"}]
        tool_call = {
            "id": "call_shared",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"README.md"}',
            },
        }
        self.store.store_assistant_message(
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "Shared DeepSeek reasoning.",
                "tool_calls": [tool_call],
            },
            conversation_scope(prior, namespace_pro),
            namespace_pro,
            prior,
        )

        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-flash",
                "messages": [
                    *prior,
                    {"role": "assistant", "content": "", "tool_calls": [tool_call]},
                ],
            },
            config,
            self.store,
            authorization="Bearer key-a",
        )

        self.assertEqual(prepared.missing_reasoning_messages, 0)
        self.assertEqual(
            prepared.payload["messages"][1]["reasoning_content"],
            "Shared DeepSeek reasoning.",
        )

    def test_strict_hit_backfills_portable_cache_for_mode_switch(self) -> None:
        agent_prior = [
            {"role": "system", "content": "Agent mode."},
            {"role": "user", "content": "set up the task"},
            {"role": "user", "content": "read README"},
        ]
        plan_prior = [
            {"role": "system", "content": "Plan mode."},
            {"role": "user", "content": "set up the task"},
            {"role": "user", "content": "read README"},
        ]
        tool_call = {
            "id": "call_mode_switch",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
        }
        assistant_message = {
            "role": "assistant",
            "content": "",
            "reasoning_content": "Need README before answering.",
            "tool_calls": [tool_call],
        }
        # Store under the Agent scope the proxy writes: the user-message
        # lineage is part of the hash, and portable aliases are not yet.
        self.store.store_assistant_message(
            assistant_message,
            conversation_scope(
                agent_prior,
                _default_cache_namespace(),
                conversation_lineage(agent_prior),
            ),
        )

        # Agent re-request: strict scope hit, should backfill portable.
        strict_prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "messages": [
                    *agent_prior,
                    {"role": "assistant", "content": "", "tool_calls": [tool_call]},
                ],
            },
            ProxyConfig(),
            self.store,
        )
        # Plan re-request: scope changed (different system prompt) but the
        # turn signature still matches, so the portable alias hits.
        portable_prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "messages": [
                    *plan_prior,
                    {"role": "assistant", "content": "", "tool_calls": [tool_call]},
                ],
            },
            ProxyConfig(),
            self.store,
        )

        self.assertEqual(strict_prepared.patched_reasoning_messages, 1)
        self.assertEqual(portable_prepared.patched_reasoning_messages, 1)
        self.assertEqual(portable_prepared.missing_reasoning_messages, 0)
        self.assertEqual(
            portable_prepared.payload["messages"][3]["reasoning_content"],
            "Need README before answering.",
        )
        self.assertTrue(
            str(portable_prepared.reasoning_diagnostics[-1]["hit_kind"]).startswith(
                "portable_"
            )
        )

    def test_portable_turn_cache_restores_final_assistant_after_tool_result(
        self,
    ) -> None:
        agent_user = {"role": "user", "content": "look up project state"}
        plan_user = dict(agent_user)
        tool_call = {
            "id": "call_project_state",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"query":"state"}'},
        }
        tool_result = {
            "role": "tool",
            "tool_call_id": "call_project_state",
            "content": '{"state":"ready"}',
        }
        tool_assistant = {
            "role": "assistant",
            "content": "",
            "reasoning_content": "Need the project state.",
            "tool_calls": [tool_call],
        }
        final_assistant = {
            "role": "assistant",
            "content": "The project is ready.",
            "reasoning_content": "The tool result is enough to answer.",
        }
        agent_initial_prior = [
            {"role": "system", "content": "Agent mode."},
            agent_user,
        ]
        agent_final_prior = [*agent_initial_prior, tool_assistant, tool_result]
        self.store.store_assistant_message(
            tool_assistant,
            _cache_scope(agent_initial_prior),
            _default_cache_namespace(),
            agent_initial_prior,
        )
        self.store.store_assistant_message(
            final_assistant,
            _cache_scope(agent_final_prior),
            _default_cache_namespace(),
            agent_final_prior,
        )

        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "system", "content": "Plan mode."},
                    plan_user,
                    {"role": "assistant", "content": "", "tool_calls": [tool_call]},
                    tool_result,
                    {"role": "assistant", "content": "The project is ready."},
                    {"role": "user", "content": "continue"},
                ],
            },
            ProxyConfig(missing_reasoning_strategy="reject"),
            self.store,
        )

        self.assertEqual(prepared.missing_reasoning_messages, 0)
        self.assertEqual(prepared.patched_reasoning_messages, 2)
        self.assertEqual(
            prepared.payload["messages"][4]["reasoning_content"],
            "The tool result is enough to answer.",
        )

    def test_portable_turn_cache_isolated_for_reused_tool_call_id(self) -> None:
        # Two different conversations both happen to reuse the same
        # tool_call.id. Cache must NOT cross-contaminate.
        tool_call = {
            "id": "call_reused",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
        assistant_a = {
            "role": "assistant",
            "content": "",
            "reasoning_content": "Reasoning for thread A.",
            "tool_calls": [tool_call],
        }
        assistant_b = {
            "role": "assistant",
            "content": "",
            "reasoning_content": "Reasoning for thread B.",
            "tool_calls": [tool_call],
        }
        prior_a = [
            {"role": "system", "content": "Agent mode."},
            {"role": "user", "content": "thread A"},
        ]
        prior_b = [
            {"role": "system", "content": "Agent mode."},
            {"role": "user", "content": "thread B"},
        ]
        self.store.store_assistant_message(
            assistant_a,
            _cache_scope(prior_a),
            _default_cache_namespace(),
            prior_a,
        )
        self.store.store_assistant_message(
            assistant_b,
            _cache_scope(prior_b),
            _default_cache_namespace(),
            prior_b,
        )

        # Plan-mode replay of thread A — should retrieve A's reasoning, not B's.
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "system", "content": "Plan mode."},
                    {"role": "user", "content": "thread A"},
                    {"role": "assistant", "content": "", "tool_calls": [tool_call]},
                ],
            },
            ProxyConfig(),
            self.store,
        )

        self.assertEqual(
            prepared.payload["messages"][2]["reasoning_content"],
            "Reasoning for thread A.",
        )

    def test_cold_cache_keeps_transcript_with_placeholder_reasoning(self) -> None:
        old_tool_call = {
            "id": "call_old",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"README.md"}',
            },
        }
        new_tool_call = {
            "id": "call_new",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"query":"new"}'},
        }
        first_payload = {
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "user", "content": "old model turn"},
                {"role": "assistant", "content": "", "tool_calls": [old_tool_call]},
                {"role": "tool", "tool_call_id": "call_old", "content": "old result"},
                {"role": "user", "content": "continue with DeepSeek"},
            ],
        }
        first_prepared = prepare_upstream_request(
            first_payload,
            ProxyConfig(missing_reasoning_strategy="recover"),
            self.store,
        )
        self.assertEqual(first_prepared.recovered_reasoning_messages, 0)
        self.assertEqual(first_prepared.recovery_dropped_messages, 0)
        self.assertIsNone(first_prepared.recovery_notice)
        self.assertEqual(first_prepared.missing_reasoning_messages, 0)
        self.assertEqual(
            [message["role"] for message in first_prepared.payload["messages"]],
            ["user", "assistant", "tool", "user"],
        )
        self.assertEqual(
            first_prepared.payload["messages"][1]["reasoning_content"],
            PRIOR_REASONING_UNAVAILABLE,
        )

        # Simulate DeepSeek's response; reasoning is recorded under the kept
        # transcript scope so the next turn can restore it from cache.
        response_body = json.dumps(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "Need the new lookup.",
                            "tool_calls": [new_tool_call],
                        },
                    }
                ],
            }
        ).encode()
        rewritten = rewrite_response_body(
            response_body,
            "deepseek-v4-pro",
            self.store,
            first_prepared.payload["messages"],
            first_prepared.cache_namespace,
            content_prefix=first_prepared.recovery_notice,
            recording_contexts=first_prepared.record_response_contexts,
            lineage=first_prepared.lineage,
            agent_id=first_prepared.agent_id,
        )
        recovered_assistant = json.loads(rewritten)["choices"][0]["message"]
        self.assertEqual(
            self.store.get(
                f"scope:{first_prepared.record_response_scope}:signature:"
                f"{message_signature(recovered_assistant)}"
            ),
            "Need the new lookup.",
        )
        recovered_assistant.pop("reasoning_content", None)

        second_payload = {
            "model": "deepseek-v4-pro",
            "messages": [
                *first_payload["messages"],
                recovered_assistant,
                {"role": "tool", "tool_call_id": "call_new", "content": "new result"},
            ],
        }

        second_prepared = prepare_upstream_request(
            second_payload,
            ProxyConfig(missing_reasoning_strategy="recover"),
            self.store,
        )

        self.assertEqual(second_prepared.missing_reasoning_messages, 0)
        self.assertEqual(second_prepared.recovered_reasoning_messages, 0)
        self.assertEqual(second_prepared.recovery_dropped_messages, 0)
        self.assertIsNone(second_prepared.recovery_notice)
        self.assertEqual(
            second_prepared.payload["messages"][1]["reasoning_content"],
            PRIOR_REASONING_UNAVAILABLE,
        )
        self.assertEqual(
            second_prepared.payload["messages"][4]["reasoning_content"],
            "Need the new lookup.",
        )

    def test_parallel_subagents_do_not_share_reasoning_after_system_churn(self) -> None:
        """Each sub-agent keeps its own thinking when Cursor rewrites the system
        prompt. The shared tail ("continue") must not collapse their caches."""
        tool_call = {
            "id": "call_0",
            "type": "function",
            "function": {"name": "Read", "arguments": "{}"},
        }

        def history(label: str, system: str) -> list[dict]:
            return [
                {"role": "system", "content": system},
                {"role": "user", "content": f"task {label}"},
                {"role": "assistant", "content": "ack"},
                {"role": "user", "content": "continue"},
            ]

        for label, reasoning in (("A", "think A"), ("B", "think B")):
            prepared = prepare_upstream_request(
                {
                    "model": "deepseek-v4-pro",
                    "messages": history(label, f"sys {label} t=1"),
                },
                ProxyConfig(),
                self.store,
            )
            rewrite_response_body(
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "reasoning_content": reasoning,
                                    "tool_calls": [tool_call],
                                }
                            }
                        ]
                    }
                ).encode(),
                "deepseek-v4-pro",
                self.store,
                prepared.payload["messages"],
                prepared.cache_namespace,
                recording_contexts=prepared.record_response_contexts,
                lineage=prepared.lineage,
                root=prepared.root,
                agent_id=prepared.agent_id,
            )

        for label, reasoning in (("A", "think A"), ("B", "think B")):
            follow_up = prepare_upstream_request(
                {
                    "model": "deepseek-v4-pro",
                    "messages": [
                        *history(label, f"sys {label} t=2"),
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [tool_call],
                        },
                    ],
                },
                ProxyConfig(),
                self.store,
            )
            self.assertEqual(
                follow_up.payload["messages"][-1].get("reasoning_content"),
                reasoning,
            )
            self.assertNotEqual(follow_up.lineage, "")
            self.assertNotEqual(follow_up.root, "")

    def test_explicit_subagent_id_isolates_identical_transcripts(self) -> None:
        """Same prompt text stays isolated when Cursor sends a sub-agent id."""
        tool_call = {
            "id": "call_0",
            "type": "function",
            "function": {"name": "Read", "arguments": "{}"},
        }
        transcript = [
            {"role": "system", "content": "You are running as a subagent."},
            {"role": "user", "content": "continue"},
        ]

        def prepare(agent_id: str, system: str) -> object:
            return prepare_upstream_request(
                {
                    "model": "deepseek-v4-pro",
                    "metadata": {"subagent_id": agent_id},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": "continue"},
                    ],
                },
                ProxyConfig(),
                self.store,
            )

        for agent_id, reasoning in (("sub-a", "think A"), ("sub-b", "think B")):
            prepared = prepare(agent_id, "sys t=1")
            rewrite_response_body(
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "reasoning_content": reasoning,
                                    "tool_calls": [tool_call],
                                }
                            }
                        ]
                    }
                ).encode(),
                "deepseek-v4-pro",
                self.store,
                prepared.payload["messages"],
                prepared.cache_namespace,
                recording_contexts=prepared.record_response_contexts,
                lineage=prepared.lineage,
                root=prepared.root,
                agent_id=prepared.agent_id,
            )

        roots = set()
        for agent_id, reasoning in (("sub-a", "think A"), ("sub-b", "think B")):
            follow_up = prepare_upstream_request(
                {
                    "model": "deepseek-v4-pro",
                    "metadata": {"subagent_id": agent_id},
                    "messages": [
                        {"role": "system", "content": "sys t=2"},
                        *transcript[1:],
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [tool_call],
                        },
                    ],
                },
                ProxyConfig(),
                self.store,
            )
            roots.add(follow_up.root)
            self.assertEqual(
                follow_up.payload["messages"][-1].get("reasoning_content"),
                reasoning,
            )
        self.assertEqual(len(roots), 2)

    def test_request_id_does_not_split_one_conversation(self) -> None:
        """x-request-id changes every HTTP call and must not become the agent id."""
        tool_call = {
            "id": "call_0",
            "type": "function",
            "function": {"name": "Read", "arguments": "{}"},
        }
        prepared = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "system", "content": "sys t=1"},
                    {"role": "user", "content": "same task"},
                ],
            },
            ProxyConfig(),
            self.store,
            headers={"x-request-id": "req-1"},
        )
        rewrite_response_body(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "reasoning_content": "think once",
                                "tool_calls": [tool_call],
                            }
                        }
                    ]
                }
            ).encode(),
            "deepseek-v4-pro",
            self.store,
            prepared.payload["messages"],
            prepared.cache_namespace,
            recording_contexts=prepared.record_response_contexts,
            lineage=prepared.lineage,
            root=prepared.root,
        )
        follow_up = prepare_upstream_request(
            {
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "system", "content": "sys t=2"},
                    {"role": "user", "content": "same task"},
                    {"role": "assistant", "content": "", "tool_calls": [tool_call]},
                ],
            },
            ProxyConfig(),
            self.store,
            headers={"x-request-id": "req-2"},
        )
        self.assertEqual(follow_up.agent_id_source, "transcript")
        self.assertEqual(follow_up.root, prepared.root)
        self.assertEqual(
            follow_up.payload["messages"][-1].get("reasoning_content"),
            "think once",
        )

    def test_concurrent_subagents_do_not_cross_read(self) -> None:
        """Parallel requests keep the reasoning written by their own agent."""
        errors: list[BaseException] = []
        barrier = threading.Barrier(3)

        def run(label: str) -> None:
            try:
                tool_call = {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                }
                prepared = prepare_upstream_request(
                    {
                        "model": "deepseek-v4-pro",
                        "metadata": {"subagent_id": label},
                        "messages": [
                            {"role": "system", "content": "sys"},
                            {"role": "user", "content": "same task"},
                        ],
                    },
                    ProxyConfig(),
                    self.store,
                )
                rewrite_response_body(
                    json.dumps(
                        {
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": "",
                                        "reasoning_content": f"think {label}",
                                        "tool_calls": [tool_call],
                                    }
                                }
                            ]
                        }
                    ).encode(),
                    "deepseek-v4-pro",
                    self.store,
                    prepared.payload["messages"],
                    prepared.cache_namespace,
                    recording_contexts=prepared.record_response_contexts,
                    lineage=prepared.lineage,
                    root=prepared.root,
                    agent_id=prepared.agent_id,
                )
                barrier.wait(timeout=5)
                follow_up = prepare_upstream_request(
                    {
                        "model": "deepseek-v4-pro",
                        "metadata": {"subagent_id": label},
                        "messages": [
                            {"role": "system", "content": "sys changed"},
                            {"role": "user", "content": "same task"},
                            {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [tool_call],
                            },
                        ],
                    },
                    ProxyConfig(),
                    self.store,
                )
                got = follow_up.payload["messages"][-1].get("reasoning_content")
                if got != f"think {label}":
                    errors.append(AssertionError(f"{label}: {got}"))
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(label,)) for label in ("A", "B", "C")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])


class StopMidStreamingToolCallTests(unittest.TestCase):
    """Regression for the 'Stop pressed during streaming tool-call arguments'
    scenario. When the upstream stream is cut off before the tool_call.id
    chunk arrives, the cached message has tool_calls with no IDs. Cursor
    synthesises its own ID for its bookkeeping, so the next request looks
    nothing like the cached message at the id/signature/message-content
    levels. The tool_name fallback is the only thing that can rescue this."""

    def setUp(self) -> None:
        self.store = ReasoningStore(":memory:")

    def test_tool_name_fallback_restores_reasoning_when_id_missing(self) -> None:
        # Turn 1 prepares an upstream request and caches a partial assistant
        # message simulating a Stop before id arrived.
        first_payload = {
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "u1"},
            ],
        }
        first_prepared = prepare_upstream_request(
            first_payload,
            ProxyConfig(missing_reasoning_strategy="recover"),
            self.store,
        )

        partial_response = {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "Need to grep.",
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "grep_search",
                                    "arguments": '{"q":',
                                },
                            }
                        ],
                    },
                }
            ]
        }
        rewrite_response_body(
            json.dumps(partial_response).encode("utf-8"),
            original_model=first_prepared.original_model,
            store=self.store,
            request_messages=first_prepared.record_response_messages,
            cache_namespace=first_prepared.cache_namespace,
            scope=first_prepared.record_response_scope,
            prior_messages=first_prepared.record_response_messages,
            recording_contexts=first_prepared.record_response_contexts,
        )

        # Turn 2: Cursor saved the partial response with a synthesised id and
        # its own best guess for the arguments.
        second_payload = {
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "u1"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "cursor-synth-1",
                            "type": "function",
                            "function": {
                                "name": "grep_search",
                                "arguments": '{"q":"foo"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "cursor-synth-1",
                    "content": "match",
                },
                {"role": "user", "content": "u2"},
            ],
        }
        second_prepared = prepare_upstream_request(
            second_payload,
            ProxyConfig(missing_reasoning_strategy="recover"),
            self.store,
        )

        self.assertEqual(second_prepared.patched_reasoning_messages, 1)
        self.assertEqual(second_prepared.missing_reasoning_messages, 0)
        self.assertIsNone(second_prepared.recovery_notice)
        self.assertEqual(
            second_prepared.payload["messages"][2]["reasoning_content"],
            "Need to grep.",
        )

    def test_tool_name_keys_are_isolated_across_distinct_turns(self) -> None:
        # Two separate turns each interrupt with the same function name.
        # The strict scope already differs (each turn has more prior
        # messages) so the two cached entries should not collide and the
        # second turn's reasoning must not leak into the first turn's slot.
        config = ProxyConfig(missing_reasoning_strategy="recover")

        def cache_partial(payload: dict, reasoning: str, args_fragment: str) -> dict:
            prepared = prepare_upstream_request(payload, config, self.store)
            response = {
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": reasoning,
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "grep_search",
                                        "arguments": args_fragment,
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
            rewrite_response_body(
                json.dumps(response).encode("utf-8"),
                original_model=prepared.original_model,
                store=self.store,
                request_messages=prepared.record_response_messages,
                cache_namespace=prepared.cache_namespace,
                scope=prepared.record_response_scope,
                prior_messages=prepared.record_response_messages,
                recording_contexts=prepared.record_response_contexts,
            )
            return prepared

        turn_a_payload = {
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "u-A"},
            ],
        }
        cache_partial(turn_a_payload, "Reasoning A.", '{"q":')

        turn_b_payload = {
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "u-A"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "synth-A",
                            "type": "function",
                            "function": {
                                "name": "grep_search",
                                "arguments": '{"q":"a"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "synth-A", "content": "ra"},
                {"role": "user", "content": "u-B"},
            ],
        }
        cache_partial(turn_b_payload, "Reasoning B.", '{"q":')

        # Now look up turn A's assistant under its own scope. It must still
        # return Reasoning A and never Reasoning B (no scope collision).
        recovery_payload = {
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "u-A"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "synth-A",
                            "type": "function",
                            "function": {
                                "name": "grep_search",
                                "arguments": '{"q":"a"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "synth-A", "content": "ra"},
                {"role": "user", "content": "u-A2"},
            ],
        }
        prepared = prepare_upstream_request(recovery_payload, config, self.store)
        self.assertEqual(
            prepared.payload["messages"][2]["reasoning_content"],
            "Reasoning A.",
        )


if __name__ == "__main__":
    unittest.main()
