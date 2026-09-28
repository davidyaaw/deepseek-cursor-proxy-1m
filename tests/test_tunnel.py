from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from deepseek_cursor_proxy.tunnel import (
    NgrokTunnel,
    fatal_ngrok_failure,
    local_tunnel_target,
    ngrok_agent_urls,
    ngrok_log_excerpt,
    ngrok_web_api_url,
    parse_ngrok_public_url,
    public_url_from_ngrok_log,
    retryable_ngrok_failure,
)


class TunnelTests(unittest.TestCase):
    def _write_log(self, text: str) -> Path:
        """Write ngrok log text to a temp file and return its path."""
        with tempfile.NamedTemporaryFile(
            "w", delete=False, encoding="utf-8", suffix=".log"
        ) as handle:
            handle.write(text)
            return Path(handle.name)

    def test_local_tunnel_target_uses_loopback_for_wildcard_hosts(self) -> None:
        self.assertEqual(local_tunnel_target("0.0.0.0", 9000), "http://127.0.0.1:9000")
        self.assertEqual(local_tunnel_target("::", 9000), "http://127.0.0.1:9000")

    def test_local_tunnel_target_formats_ipv6_hosts(self) -> None:
        self.assertEqual(local_tunnel_target("::1", 9000), "http://[::1]:9000")

    def test_parse_ngrok_public_url_prefers_https(self) -> None:
        payload = {
            "tunnels": [
                {"public_url": "http://example.ngrok-free.app"},
                {"public_url": "https://example.ngrok-free.app"},
            ]
        }

        self.assertEqual(
            parse_ngrok_public_url(payload), "https://example.ngrok-free.app"
        )

    def test_parse_ngrok_public_url_supports_endpoint_api(self) -> None:
        payload = {"endpoints": [{"url": "https://example.ngrok-free.app"}]}

        self.assertEqual(
            parse_ngrok_public_url(payload), "https://example.ngrok-free.app"
        )

    def test_parse_ngrok_public_url_ignores_missing_tunnels(self) -> None:
        self.assertIsNone(parse_ngrok_public_url({"tunnels": []}))
        self.assertIsNone(parse_ngrok_public_url({}))

    def test_ngrok_agent_urls_use_current_api_then_legacy_fallback(self) -> None:
        self.assertEqual(
            ngrok_agent_urls("http://127.0.0.1:4040/api"),
            [
                "http://127.0.0.1:4040/api/endpoints",
                "http://127.0.0.1:4040/api/tunnels",
            ],
        )

    def test_ngrok_tunnel_appends_url_flag_when_configured(self) -> None:
        with patch(
            "deepseek_cursor_proxy.tunnel.shutil.which", return_value="/x/ngrok"
        ):
            with patch("deepseek_cursor_proxy.tunnel.subprocess.Popen") as popen:
                popen.return_value = MagicMock(poll=lambda: None)
                with patch.object(
                    NgrokTunnel,
                    "wait_for_public_url",
                    return_value="https://example.ngrok-free.app",
                ):
                    tunnel = NgrokTunnel(
                        "http://127.0.0.1:9000",
                        ngrok_url="https://my.ngrok.dev",
                    )
                    tunnel.start()
                popen.assert_called_once()
                argv, _kwargs = popen.call_args
                self.assertEqual(
                    argv[0],
                    [
                        "ngrok",
                        "http",
                        "http://127.0.0.1:9000",
                        "--log=stdout",
                        "--log-format=logfmt",
                        "--url=https://my.ngrok.dev",
                    ],
                )
            tunnel.stop()

    def test_log_helpers_find_the_web_port_and_strip_secrets(self) -> None:
        log = "\n".join(
            [
                'lvl=warn msg="can\'t bind default web address" addr=127.0.0.1:4040',
                'lvl=info msg="starting web service" obj=web addr=127.0.0.1:4041',
                'lvl=info msg="started tunnel" url=https://example.ngrok-free.dev',
                "authtoken=secret-token",
            ]
        )

        self.assertEqual(ngrok_web_api_url(log), "http://127.0.0.1:4041/api")
        self.assertEqual(
            public_url_from_ngrok_log(log), "https://example.ngrok-free.dev"
        )
        excerpt = ngrok_log_excerpt(log)
        self.assertIn("authtoken=<redacted>", excerpt)
        self.assertNotIn("secret-token", excerpt)
        self.assertIsNone(fatal_ngrok_failure(log))
        self.assertTrue(retryable_ngrok_failure("ERR_NGROK_334 already online"))

    def test_wait_polls_the_web_port_recorded_in_the_log(self) -> None:
        tunnel = NgrokTunnel("http://127.0.0.1:9000", startup_timeout=2)
        tunnel.process = MagicMock(poll=lambda: None)
        log_path = self._write_log(
            'lvl=info msg="starting web service" obj=web addr=127.0.0.1:4041\n'
        )
        tunnel._log_path = log_path
        seen: list[str] = []

        class _Body:
            """Minimal urlopen response whose JSON has a public URL."""

            def __enter__(self) -> _Body:
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"endpoints":[{"url":"https://example.ngrok-free.dev"}]}'

        def fake_urlopen(url: str, timeout: int = 1) -> _Body:
            seen.append(url)
            return _Body()

        try:
            with patch(
                "deepseek_cursor_proxy.tunnel.urlopen", side_effect=fake_urlopen
            ):
                public_url = tunnel.wait_for_public_url()
        finally:
            log_path.unlink(missing_ok=True)

        self.assertEqual(public_url, "https://example.ngrok-free.dev")
        self.assertTrue(any("4041" in item for item in seen))

    def test_timeout_includes_the_scrubbed_ngrok_log(self) -> None:
        tunnel = NgrokTunnel("http://127.0.0.1:9000", startup_timeout=0.2)
        tunnel.process = MagicMock(poll=lambda: None)
        log_path = self._write_log(
            "lvl=info msg=reconnecting authtoken=secret-token\n"
        )
        tunnel._log_path = log_path

        class _Body:
            """urlopen response with an empty endpoint list."""

            def __enter__(self) -> _Body:
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"endpoints":[]}'

        try:
            with patch(
                "deepseek_cursor_proxy.tunnel.urlopen", return_value=_Body()
            ):
                with self.assertRaises(RuntimeError) as raised:
                    tunnel.wait_for_public_url()
        finally:
            log_path.unlink(missing_ok=True)

        message = str(raised.exception)
        self.assertIn("reconnecting", message)
        self.assertNotIn("secret-token", message)

    def test_fatal_endpoint_conflict_fails_immediately(self) -> None:
        tunnel = NgrokTunnel("http://127.0.0.1:9000", startup_timeout=30)
        tunnel.process = MagicMock(poll=lambda: None)
        log_path = self._write_log(
            "lvl=eror err=ERR_NGROK_334 endpoint is already online\n"
        )
        tunnel._log_path = log_path
        try:
            with self.assertRaises(RuntimeError) as raised:
                tunnel.wait_for_public_url()
        finally:
            log_path.unlink(missing_ok=True)
        self.assertIn("ERR_NGROK_334", str(raised.exception))

    def test_start_retries_when_the_endpoint_is_already_online(self) -> None:
        tunnel = NgrokTunnel("http://127.0.0.1:9000")
        calls = {"count": 0}

        def launch() -> str:
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("ERR_NGROK_334 endpoint is already online")
            return "https://example.ngrok-free.dev"

        with patch(
            "deepseek_cursor_proxy.tunnel.shutil.which", return_value="ngrok"
        ):
            with patch.object(tunnel, "_launch_and_wait", side_effect=launch):
                with patch.object(tunnel, "stop"):
                    with patch("deepseek_cursor_proxy.tunnel.time.sleep"):
                        public_url = tunnel.start()

        self.assertEqual(public_url, "https://example.ngrok-free.dev")
        self.assertEqual(calls["count"], 3)

    def test_start_does_not_retry_a_plain_timeout(self) -> None:
        tunnel = NgrokTunnel("http://127.0.0.1:9000")
        with patch(
            "deepseek_cursor_proxy.tunnel.shutil.which", return_value="ngrok"
        ):
            with patch.object(
                tunnel,
                "_launch_and_wait",
                side_effect=RuntimeError(
                    "Timed out waiting for ngrok tunnel: ngrok did not report a public URL"
                ),
            ):
                with patch.object(tunnel, "stop"):
                    with patch("deepseek_cursor_proxy.tunnel.time.sleep") as sleep:
                        with self.assertRaises(RuntimeError):
                            tunnel.start()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
