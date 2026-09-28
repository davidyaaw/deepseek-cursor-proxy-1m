"""Open a DeepSeek request and retry before Cursor has seen any bytes.

Windows often reports WinError 10060 after about 21 seconds when the TCP
handshake dies. Cursor then shows a generic provider error. A short connect
timeout plus a few retries avoids that, as long as nothing has been sent to
the client yet.
"""

from __future__ import annotations

import http.client
import socket
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from .logging import LOG, request_id

# Handshake budget per address. The read budget stays request_timeout.
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_UPSTREAM_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (0.5, 1.5)
_TRANSIENT_HTTP_STATUS = {408, 429, 500, 502, 503, 504}
_TRANSIENT_WINERRORS = {10051, 10053, 10054, 10060, 10061, 10065}
_TRANSIENT_ERRNOS = {
    32,  # EPIPE
    54,  # ECONNRESET on some platforms
    60,  # ETIMEDOUT
    61,  # ECONNREFUSED
    104,  # ECONNRESET
    110,  # ETIMEDOUT
    111,  # ECONNREFUSED
    113,  # EHOSTUNREACH
    101,  # ENETUNREACH
}


def candidate_addresses(host: str, port: int) -> list[tuple[Any, ...]]:
    """IPv4 first, then IPv6. A dead IPv6 route must not block the call."""
    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    ipv4: list[tuple[Any, ...]] = []
    ipv6: list[tuple[Any, ...]] = []
    seen: set[tuple[Any, ...]] = set()
    for family, _socktype, _proto, _canon, sockaddr in infos:
        if sockaddr in seen:
            continue
        seen.add(sockaddr)
        if family == socket.AF_INET:
            ipv4.append(sockaddr)
        elif family == socket.AF_INET6:
            ipv6.append(sockaddr)
    return ipv4 + ipv6


def dial_upstream(
    host: str,
    port: int,
    connect_timeout: float,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Open one TCP connection, trying the next address when one fails."""
    addresses = candidate_addresses(host, port)
    if not addresses:
        raise OSError(f"no address for {host}:{port}")
    last: OSError | None = None
    for index, sockaddr in enumerate(addresses):
        family = socket.AF_INET6 if len(sockaddr) > 2 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if source_address:
                sock.bind(source_address)
            sock.settimeout(connect_timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
            if index < len(addresses) - 1:
                LOG.info(
                    "upstream_dial id=%s address=%s reason=%s",
                    request_id(),
                    sockaddr[0],
                    exc,
                )
    assert last is not None
    raise last


def transient_failure(exc: BaseException) -> bool:
    """True when DeepSeek never produced a response we should show to Cursor."""
    if isinstance(exc, HTTPError):
        return exc.code in _TRANSIENT_HTTP_STATUS
    reason: BaseException = exc
    if isinstance(exc, URLError) and isinstance(exc.reason, BaseException):
        reason = exc.reason
    if isinstance(reason, socket.gaierror | TimeoutError):
        return True
    if isinstance(reason, OSError):
        winerror = getattr(reason, "winerror", None)
        if winerror in _TRANSIENT_WINERRORS:
            return True
        if reason.errno in _TRANSIENT_ERRNOS:
            return True
        text = str(reason).lower()
        if (
            "10060" in text
            or "10054" in text
            or "timed out" in text
            or "timeout" in text
            or "closed connection" in text
            or "connection reset" in text
            or "broken pipe" in text
        ):
            return True
    if isinstance(exc, http.client.HTTPException) and not isinstance(exc, HTTPError):
        return True
    return False


def _apply_read_timeout(sock: socket.socket | None, timeout: float | None) -> None:
    """Keep the long read budget after the short connect attempt."""
    if sock is None or not isinstance(timeout, (int, float)) or timeout <= 0:
        return
    sock.settimeout(timeout)


def _connection_classes(
    connect_timeout: float,
) -> tuple[type[http.client.HTTPConnection], type[http.client.HTTPSConnection]]:
    """HTTP and HTTPS connections that dial with a short timeout."""

    def dial(address: tuple[Any, ...], timeout: float, source_address: Any) -> socket.socket:
        del timeout
        host, port = address[0], address[1]
        return dial_upstream(host, int(port), connect_timeout, source_address)

    class DialHTTPConnection(http.client.HTTPConnection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._create_connection = dial

        def connect(self) -> None:
            read_timeout = self.timeout
            super().connect()
            _apply_read_timeout(self.sock, read_timeout)

    class DialHTTPSConnection(http.client.HTTPSConnection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._create_connection = dial

        def connect(self) -> None:
            read_timeout = self.timeout
            http.client.HTTPConnection.connect(self)
            server_hostname = self._tunnel_host or self.host
            assert self.sock is not None
            self.sock = self._context.wrap_socket(
                self.sock, server_hostname=server_hostname
            )
            _apply_read_timeout(self.sock, read_timeout)

    return DialHTTPConnection, DialHTTPSConnection


def _default_send(
    request: Request,
    timeout: float,
    connect_timeout: float,
) -> Any:
    """One attempt, with a short TCP handshake and the caller's read budget."""
    from urllib.request import HTTPHandler as UrllibHTTPHandler
    from urllib.request import HTTPSHandler as UrllibHTTPSHandler

    http_cls, https_cls = _connection_classes(connect_timeout)

    class UpstreamHTTPHandler(UrllibHTTPHandler):
        """Plain HTTP with the short connect timeout."""

        def http_open(self, req: Request) -> Any:
            return self.do_open(http_cls, req)

    class UpstreamHTTPSHandler(UrllibHTTPSHandler):
        """HTTPS with the short connect timeout."""

        def https_open(self, req: Request) -> Any:
            return self.do_open(https_cls, req, context=self._context)

    opener = build_opener(UpstreamHTTPHandler(), UpstreamHTTPSHandler())
    return opener.open(request, timeout=timeout)


def open_upstream(
    request: Request,
    *,
    read_timeout: float,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    attempts: int = DEFAULT_UPSTREAM_ATTEMPTS,
    send: Callable[[Request, float], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """POST to DeepSeek. Retry connect and gateway failures in place."""
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            if send is None:
                return _default_send(request, read_timeout, connect_timeout)
            return send(request, read_timeout)
        except HTTPError as exc:
            last = exc
            if exc.code not in _TRANSIENT_HTTP_STATUS or attempt == attempts:
                raise
            exc.close()
        except (URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            last = exc
            if not transient_failure(exc) or attempt == attempts:
                raise
        LOG.warning(
            "upstream_retry id=%s attempt=%s/%s reason=%s",
            request_id(),
            attempt,
            attempts,
            last,
        )
        if attempt - 1 < len(_RETRY_BACKOFF_SECONDS):
            sleep(_RETRY_BACKOFF_SECONDS[attempt - 1])
    assert last is not None
    raise last
