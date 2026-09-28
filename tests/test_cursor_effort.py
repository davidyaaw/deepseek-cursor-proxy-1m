"""Tests for saving Cursor's Effort picker into the proxy config."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from deepseek_cursor_proxy.cursor_effort import effort_from_hook_payload
from deepseek_cursor_proxy.cursor_effort_hook import (
    decode_hook_stdin,
    hook_response,
    load_hook_payload,
)
from deepseek_cursor_proxy.server import model_catalog


class CursorEffortHookTests(unittest.TestCase):
    def test_reads_reasoning_param_before_the_model_suffix(self) -> None:
        found = effort_from_hook_payload(
            {
                "model": "gpt-5.6-sol-high",
                "model_id": "gpt-5.6-sol",
                "model_params": [{"id": "reasoning", "value": "max"}],
            }
        )

        self.assertEqual(found, ("gpt-5.6-sol", "max"))

    def test_windows_powershell_bom_still_yields_the_effort(self) -> None:
        payload = load_hook_payload(
            '\ufeff{"model_id":"gpt-5.6-terra","model":"gpt-5.6-terra-max",'
            '"model_params":[{"id":"reasoning","value":"max"}],'
            '"hook_event_name":"beforeSubmitPrompt"}'
        )

        self.assertEqual(effort_from_hook_payload(payload), ("gpt-5.6-terra", "max"))

    def test_cp1251_stdin_still_decodes_powershell_utf8(self) -> None:
        raw = (
            '{"model_id":"gpt-5.6-sol","model_params":[{"id":"reasoning","value":"low"}]}'
        )
        payload = load_hook_payload(decode_hook_stdin(b"\xef\xbb\xbf" + raw.encode("utf-8")))

        self.assertEqual(effort_from_hook_payload(payload), ("gpt-5.6-sol", "low"))

    def test_hook_writes_sol_and_terra_without_clobbering_each_other(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            effort_path = root / "cursor-effort.json"
            config_path = root / "config.yaml"
            config_path.write_text(
                "model: deepseek-v4-pro\nreasoning_effort: high\n",
                encoding="utf-8",
            )
            sol = hook_response(
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "model": "gpt-5.6-sol-max",
                    "model_id": "gpt-5.6-sol",
                    "model_params": [{"id": "reasoning", "value": "max"}],
                },
                effort_path,
                config_path,
            )
            terra = hook_response(
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "model": "gpt-5.6-terra-medium",
                    "model_id": "gpt-5.6-terra",
                    "model_params": [{"id": "reasoning", "value": "medium"}],
                },
                effort_path,
                config_path,
            )
            stored = json.loads(effort_path.read_text(encoding="utf-8"))
            config_text = config_path.read_text(encoding="utf-8")

        self.assertEqual(sol, {"continue": True})
        self.assertEqual(terra, {"continue": True})
        self.assertEqual(stored["models"]["gpt-5.6-sol"], "max")
        self.assertEqual(stored["models"]["gpt-5.6-terra"], "medium")
        self.assertIn("model: deepseek-v4-pro", config_text)
        self.assertIn("reasoning_effort: medium", config_text)

    def test_model_list_advertises_reasoning_effort(self) -> None:
        catalog = model_catalog(["deepseek-v4-pro"], 1)
        model = catalog["data"][0]

        self.assertEqual(model["api_types"], ["chat_completions"])
        self.assertIn("max", model["capabilities"]["reasoning_effort"])
        self.assertTrue(model["capabilities"]["supports_reasoning"])
