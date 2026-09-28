"""Tests for the Cursor client patch that frees Composer and Grok from BYOK."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deepseek_cursor_proxy.cursor_byok_patch import (
    CursorByokPatchError,
    apply_patch,
    discover_cursor_app_dirs,
    main,
    patch_workbench,
    update_product_checksum,
    windows_relaunch_parameters,
    workbench_checksum,
)


_SAMPLE = (
    b'function MRm(t,e){return Cdb(t)?e.useClaudeKey?"anthropic":void 0:'
    b'Edb(t)?e.useGoogleKey?"google":void 0:e.useOpenAIKey?"openai":void 0}'
)
_DESKTOP_SAMPLE = (
    b'function vOd(e,t){return F7f(e)?t.useClaudeKey?"anthropic":void 0:'
    b'B7f(e)?t.useGoogleKey?"google":void 0:t.useOpenAIKey?"openai":void 0}'
)


class CursorByokPatchTests(unittest.TestCase):
    def test_hosted_models_skip_the_openai_key(self) -> None:
        patched = patch_workbench(_SAMPLE)

        self.assertIn(b't.startsWith("composer-")', patched)
        self.assertIn(b't.startsWith("grok-")', patched)
        self.assertIn(b'e.useOpenAIKey?"openai":void 0)}', patched)
        self.assertEqual(patched.count(b"("), patched.count(b")"))
        self.assertEqual(patch_workbench(patched), patched)

    def test_desktop_bundle_uses_its_own_model_argument(self) -> None:
        patched = patch_workbench(_DESKTOP_SAMPLE)

        self.assertIn(b'e.startsWith("composer-")||e.startsWith("grok-")', patched)
        self.assertNotIn(b't.startsWith("composer-")', patched)

    def test_gpt_models_still_use_the_openai_key(self) -> None:
        patched = patch_workbench(_SAMPLE)

        self.assertIn(b'e.useOpenAIKey?"openai":void 0', patched)
        self.assertNotIn(
            b'e.useGoogleKey?"google":void 0:e.useOpenAIKey?"openai":void 0',
            patched,
        )

    def test_unexpected_bundle_is_rejected(self) -> None:
        with self.assertRaises(CursorByokPatchError):
            patch_workbench(b"no routing check here")

    def test_product_checksum_keeps_surrounding_json(self) -> None:
        product = (
            '{\n\t"checksums": {\n'
            '\t\t"vs/workbench/workbench.desktop.main.js": "old",\n'
            '\t\t"vs/workbench/workbench.desktop.main.css": "css"\n\t}\n}'
        )

        updated = update_product_checksum(product, "new")

        self.assertIn(
            '"vs/workbench/workbench.desktop.main.js": "new"',
            updated,
        )
        self.assertIn('"vs/workbench/workbench.desktop.main.css": "css"', updated)

    def test_checksum_matches_cursor_base64_form(self) -> None:
        self.assertEqual(workbench_checksum(b""), "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU")

    def test_apply_patch_is_idempotent_and_refreshes_checksum(self) -> None:
        with TemporaryDirectory() as tmp:
            app = Path(tmp)
            workbench = app / "out" / "vs" / "workbench"
            workbench.mkdir(parents=True)
            (workbench / "workbench.desktop.main.js").write_bytes(_SAMPLE)
            (workbench / "workbench.glass.main.js").write_bytes(_DESKTOP_SAMPLE)
            product = app / "product.json"
            product.write_text(
                '{"checksums": {"vs/workbench/workbench.desktop.main.js": "old"}}',
                encoding="utf-8",
            )

            notes = apply_patch(app)
            desktop = (workbench / "workbench.desktop.main.js").read_bytes()

            self.assertTrue(any(note.startswith("patched:") for note in notes))
            self.assertIn(workbench_checksum(desktop), product.read_text(encoding="utf-8"))
            again = apply_patch(app)
            self.assertTrue(all(note.startswith("already patched:") for note in again))

    def test_discover_uses_only_installs_that_exist(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed = root / "cursor" / "resources" / "app" / "out" / "vs" / "workbench"
            installed.mkdir(parents=True)
            (installed / "workbench.desktop.main.js").write_bytes(b"bundle")
            missing = root / "missing"
            with patch(
                "deepseek_cursor_proxy.cursor_byok_patch.cursor_app_candidates",
                return_value=[missing, installed.parents[2]],
            ):
                found = discover_cursor_app_dirs()

            self.assertEqual(found, [installed.parents[2]])

    def test_cli_tells_the_user_to_restart_only_after_a_change(self) -> None:
        with TemporaryDirectory() as tmp:
            app = self._fake_install(Path(tmp))
            first = StringIO()
            with redirect_stdout(first):
                code = main(["--app-dir", str(app), "--no-elevate"])
            self.assertEqual(code, 0)
            self.assertIn("Quit Cursor completely", first.getvalue())

            second = StringIO()
            with redirect_stdout(second):
                code = main(["--app-dir", str(app), "--no-elevate"])
            self.assertEqual(code, 0)
            self.assertIn("already installed", second.getvalue())
            self.assertNotIn("Quit Cursor", second.getvalue())

    def test_relaunch_command_repeats_restore_and_app_dir(self) -> None:
        command = windows_relaunch_parameters(
            Path("C:/temp/result.txt"),
            ["--restore", "--app-dir", "C:/Cursor/resources/app"],
        )

        self.assertIn("--elevated", command)
        self.assertIn("--restore", command)
        self.assertIn("result.txt", command)
        self.assertEqual(command.count("--elevated"), 1)

    def _fake_install(self, root: Path) -> Path:
        """Write a tiny Cursor app directory the installer can patch."""
        app = root / "app"
        workbench = app / "out" / "vs" / "workbench"
        workbench.mkdir(parents=True)
        (workbench / "workbench.desktop.main.js").write_bytes(_SAMPLE)
        (workbench / "workbench.glass.main.js").write_bytes(_DESKTOP_SAMPLE)
        (app / "product.json").write_text(
            '{"checksums": {"vs/workbench/workbench.desktop.main.js": "old"}}',
            encoding="utf-8",
        )
        return app
