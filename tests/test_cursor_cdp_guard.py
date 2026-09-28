"""CDP calls that restart the Cursor window are rewritten before they are forwarded."""

from __future__ import annotations

import json
import unittest

from deepseek_cursor_proxy.cursor_cdp_guard import (
    CursorCdpGuard,
    sanitize_response_payload,
)


def _tool_delta(
    name: str, arguments: str, *, index: int = 0, call_id: str = "call_1"
) -> dict:
    return {
        "index": index,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _chunk(tool_calls: list[dict], *, finish_reason: str | None = None) -> dict:
    choice: dict = {"index": 0, "delta": {"tool_calls": tool_calls}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"id": "chunk", "choices": [choice]}


def _arguments(chunks: list[dict]) -> list[str]:
    found: list[str] = []
    for chunk in chunks:
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            for tool_call in delta.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                if function.get("arguments"):
                    found.append(function["arguments"])
    return found


class CursorCdpGuardTests(unittest.TestCase):
    def test_page_reload_is_replaced_before_it_is_forwarded(self) -> None:
        guard = CursorCdpGuard()
        arguments = '{"method":"Page.reload","params":{"ignoreCache":true}}'
        self.assertEqual(
            guard.apply(
                _chunk(
                    [
                        _tool_delta(
                            "browser_cdp",
                            arguments[:18],
                            call_id="call_reload",
                        )
                    ]
                )
            ),
            [],
        )
        forwarded = guard.apply(
            _chunk(
                [_tool_delta("browser_cdp", arguments[18:], call_id="call_reload")],
                finish_reason="tool_calls",
            )
        )

        self.assertEqual(len(forwarded), 1)
        body = json.dumps(forwarded)
        self.assertNotIn("Page.reload", body)
        tool_call = forwarded[0]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(tool_call["id"], "call_reload")
        parsed = json.loads(tool_call["function"]["arguments"])
        self.assertEqual(parsed["method"], "Runtime.evaluate")
        self.assertIn("browser_navigate", parsed["params"]["expression"])

    def test_runtime_evaluate_is_forwarded_intact(self) -> None:
        guard = CursorCdpGuard()
        arguments = (
            '{"method":"Runtime.evaluate","params":{"expression":"1+1",'
            '"returnByValue":true}}'
        )
        forwarded = guard.apply(_chunk([_tool_delta("browser_cdp", arguments)]))

        self.assertEqual(_arguments(forwarded), [arguments])
        self.assertEqual(
            json.loads(_arguments(forwarded)[0])["params"]["expression"], "1+1"
        )

    def test_other_tools_stream_immediately(self) -> None:
        guard = CursorCdpGuard()
        first = guard.apply(
            _chunk([_tool_delta("read_file", '{"path"', call_id="call_read")])
        )
        second = guard.apply(
            _chunk([{"index": 0, "function": {"arguments": ':"README.md"}'}}])
        )

        self.assertEqual(_arguments(first), ['{"path"'])
        self.assertEqual(_arguments(second), [':"README.md"}'])

    def test_call_dynamic_tool_page_reload_is_rewritten_in_place(self) -> None:
        guard = CursorCdpGuard()
        arguments = json.dumps(
            {
                "namespace": "cursor-ide-browser",
                "toolName": "browser_cdp",
                "arguments": {
                    "method": "Page.reload",
                    "params": {"ignoreCache": True},
                },
            }
        )
        forwarded = guard.apply(
            _chunk([_tool_delta("CallDynamicTool", arguments, call_id="call_dyn")])
        )

        parsed = json.loads(_arguments(forwarded)[0])
        self.assertEqual(parsed["toolName"], "browser_cdp")
        self.assertEqual(parsed["namespace"], "cursor-ide-browser")
        self.assertEqual(parsed["arguments"]["method"], "Runtime.evaluate")
        self.assertNotIn("Page.reload", _arguments(forwarded)[0])

    def test_incomplete_reload_is_flushed_as_a_safe_call(self) -> None:
        guard = CursorCdpGuard()
        self.assertEqual(
            guard.apply(
                _chunk(
                    [
                        _tool_delta(
                            "browser_cdp",
                            '{"method":"Page.reload","params":{"ignoreCache":true}',
                        )
                    ]
                )
            ),
            [],
        )
        flushed = guard.flush()

        self.assertEqual(len(flushed), 1)
        parsed = json.loads(_arguments(flushed)[0])
        self.assertEqual(parsed["method"], "Runtime.evaluate")
        self.assertNotIn("Page.reload", json.dumps(flushed))

    def test_nonstreaming_page_reload_is_rewritten(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_reload",
                                "type": "function",
                                "function": {
                                    "name": "browser_cdp",
                                    "arguments": '{"method":"Page.crash"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
        sanitize_response_payload(payload)
        arguments = payload["choices"][0]["message"]["tool_calls"][0]["function"][
            "arguments"
        ]
        self.assertEqual(json.loads(arguments)["method"], "Runtime.evaluate")
        self.assertNotIn("Page.crash", arguments)


if __name__ == "__main__":
    unittest.main()
