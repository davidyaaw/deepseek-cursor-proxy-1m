from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from .logging import LOG


DEFAULT_NGROK_API_URL = "http://127.0.0.1:4040/api"
_NGROK_START_ATTEMPTS = 4
_NGROK_RETRY_DELAY_SECONDS = 3.0
_WEB_SERVICE_ADDR = re.compile(r'msg="starting web service".*?\baddr=(\S+)')
_LOG_TUNNEL_URL = re.compile(r'\burl="?(https://[^"\s]+)')
_FATAL_NGROK_LOG = re.compile(
    r"(?i)(authentication failed|invalid authtoken|ERR_NGROK_334|already online)"
)
_AUTHTOKEN_VALUE = re.compile(r"(?i)(authtoken[\s:=]+)(\S+)")


def local_tunnel_target(host: str, port: int) -> str:
    local_host = host.strip() or "127.0.0.1"
    if local_host in {"0.0.0.0", "::"}:
        local_host = "127.0.0.1"
    if ":" in local_host and not local_host.startswith("["):
        local_host = f"[{local_host}]"
    return f"http://{local_host}:{port}"


def parse_ngrok_public_url(payload: dict[str, Any]) -> str | None:
    records = payload.get("endpoints")
    if not isinstance(records, list):
        records = payload.get("tunnels")
    if not isinstance(records, list):
        return None

    public_urls = [
        public_url
        for record in records
        if isinstance(record, dict)
        for public_url in (record.get("url"), record.get("public_url"))
        if isinstance(public_url, str)
    ]
    for public_url in public_urls:
        if public_url.startswith("https://"):
            return public_url
    for public_url in public_urls:
        if public_url.startswith("http://"):
            return public_url
    return None


def ngrok_agent_urls(api_url: str) -> list[str]:
    normalized = api_url.rstrip("/")
    if normalized.endswith("/endpoints") or normalized.endswith("/tunnels"):
        return [normalized]
    return [f"{normalized}/endpoints", f"{normalized}/tunnels"]


def ngrok_web_api_url(text: str) -> str | None:
    """Return the agent API base URL from ngrok's web-service log line."""
    matches = _WEB_SERVICE_ADDR.findall(text)
    if not matches:
        return None
    return f"http://{matches[-1]}/api"


def public_url_from_ngrok_log(text: str) -> str | None:
    """Return the https tunnel URL printed after ngrok starts the tunnel."""
    matches = _LOG_TUNNEL_URL.findall(text)
    if not matches:
        return None
    return matches[-1].rstrip(".,)")


def ngrok_log_excerpt(text: str, limit: int = 12) -> str:
    """Return the last log lines with authtoken values removed."""
    cleaned = _AUTHTOKEN_VALUE.sub(r"\1<redacted>", text)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if limit < 1:
        return ""
    return "\n".join(lines[-limit:])


def fatal_ngrok_failure(text: str) -> str | None:
    """Return a permanent ngrok startup error, with authtoken values removed."""
    for line in text.splitlines():
        if _FATAL_NGROK_LOG.search(line):
            return ngrok_log_excerpt(line, limit=1)
    return None


def retryable_ngrok_failure(message: str) -> bool:
    """True when another attempt can work after the previous endpoint releases."""
    lowered = message.lower()
    return "err_ngrok_334" in lowered or "already online" in lowered


@dataclass
class NgrokTunnel:
    """Local ngrok process and the public URL it publishes for the proxy."""

    target_url: str
    ngrok_url: str | None = None
    command: str = "ngrok"
    api_url: str = DEFAULT_NGROK_API_URL
    startup_timeout: float = 60.0

    process: subprocess.Popen[bytes] | None = None
    _log_path: Path | None = None

    def start(self) -> str:
        """Start ngrok and return its public https URL."""
        if shutil.which(self.command) is None:
            raise RuntimeError(
                "ngrok is not installed or is not on PATH. Install it, then run "
                "`ngrok config add-authtoken <token>` once."
            )

        last_error = RuntimeError("ngrok did not report a public URL")
        for attempt in range(1, _NGROK_START_ATTEMPTS + 1):
            try:
                return self._launch_and_wait()
            except RuntimeError as exc:
                last_error = exc
                self.stop()
                if attempt == _NGROK_START_ATTEMPTS or not retryable_ngrok_failure(
                    str(exc)
                ):
                    raise
                time.sleep(_NGROK_RETRY_DELAY_SECONDS)
        raise last_error

    def _launch_and_wait(self) -> str:
        """Spawn one ngrok process and wait until it publishes a URL."""
        log_path = self._prepare_log_file()
        argv = [
            self.command,
            "http",
            self.target_url,
            "--log=stdout",
            "--log-format=logfmt",
        ]
        if self.ngrok_url:
            argv.append(f"--url={self.ngrok_url}")
        log_handle = log_path.open("wb")
        try:
            self.process = subprocess.Popen(
                argv,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        except Exception:
            log_handle.close()
            raise
        log_handle.close()
        try:
            return self.wait_for_public_url()
        except Exception:
            self.stop()
            raise

    def _prepare_log_file(self) -> Path:
        """Create an empty log file for this ngrok process."""
        directory = Path(tempfile.gettempdir()) / "deepseek-cursor-proxy"
        directory.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix="ngrok-",
            suffix=".log",
            dir=directory,
            delete=False,
        )
        handle.close()
        self._log_path = Path(handle.name)
        return self._log_path

    def _read_log(self) -> str:
        """Read the ngrok log written so far."""
        path = self._log_path
        if path is None:
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _failure_message(self, prefix: str) -> str:
        """Attach the scrubbed ngrok log to a startup error."""
        detail = ngrok_log_excerpt(self._read_log())
        if not detail:
            return prefix
        return f"{prefix}\n{detail}"

    def wait_for_public_url(self) -> str:
        """Poll the agent API until it publishes an https URL."""
        deadline = time.monotonic() + self.startup_timeout
        last_error = "ngrok did not report a public URL"
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(
                    self._failure_message("ngrok exited before creating a tunnel")
                )
            log_text = self._read_log()
            failure = fatal_ngrok_failure(log_text)
            if failure:
                raise RuntimeError(failure)
            logged_url = public_url_from_ngrok_log(log_text)
            if logged_url:
                return logged_url
            api_url = ngrok_web_api_url(log_text) or self.api_url
            for candidate in ngrok_agent_urls(api_url):
                try:
                    with urlopen(candidate, timeout=1) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    public_url = parse_ngrok_public_url(payload)
                    if public_url:
                        return public_url
                except (OSError, URLError, json.JSONDecodeError) as exc:
                    last_error = str(exc)
            time.sleep(0.25)
        raise RuntimeError(
            self._failure_message(f"Timed out waiting for ngrok tunnel: {last_error}")
        )

    def _remove_log_file(self) -> None:
        """Delete the ngrok log after the process is finished."""
        path = self._log_path
        self._log_path = None
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return

    def stop(self) -> None:
        """Stop the ngrok process and delete its log file."""
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            LOG.info("stopping ngrok tunnel")
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self._remove_log_file()
