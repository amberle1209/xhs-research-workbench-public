"""HTTPS transport that pins each request to a validated DNS result."""

from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.client import HTTPResponse, HTTPSConnection
from urllib.parse import urljoin, urlsplit, urlunsplit

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class _PinnedHTTPSConnection(HTTPSConnection):
    """An HTTPS connection that dials an already-validated address directly."""

    def __init__(
        self, hostname: str, pinned_ip: str, *, timeout: int, context: ssl.SSLContext
    ) -> None:
        super().__init__(hostname, 443, timeout=timeout, context=context)
        self._pinned_ip = ipaddress.ip_address(pinned_ip)
        self._ssl_context = context

    def connect(self) -> None:
        raw = socket.create_connection((str(self._pinned_ip), 443), self.timeout)
        try:
            peer = ipaddress.ip_address(raw.getpeername()[0])
            if peer != self._pinned_ip:
                raise OSError("connected peer mismatch")
            self.sock = self._ssl_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _resolve_global_ips(hostname: str) -> tuple[str, ...]:
    """Resolve a host once, accepting only a deterministic set of public IPs."""
    try:
        results = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        results = []
    if not results:
        raise OSError("media DNS lookup failed") from None

    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    invalid_address = False
    for result in results:
        try:
            raw_address = result[4][0]
            if not isinstance(raw_address, str):
                raise TypeError
            address = ipaddress.ip_address(raw_address)
        except (IndexError, TypeError, ValueError):
            invalid_address = True
            break
        if not address.is_global:
            invalid_address = True
            break
        addresses.add(address)
    if invalid_address or not addresses:
        raise ValueError("unsafe media address") from None

    return tuple(
        str(address) for address in sorted(addresses, key=lambda value: (value.version, int(value)))
    )


def _connect_first_validated_ip(hostname: str, timeout: int) -> _PinnedHTTPSConnection:
    addresses = _resolve_global_ips(hostname)
    context = ssl.create_default_context()
    for address in addresses:
        connection = _PinnedHTTPSConnection(hostname, address, timeout=timeout, context=context)
        try:
            connection.connect()
        except Exception:  # noqa: BLE001 - TLS failures are intentionally retried by IP.
            _close_connection(connection)
            continue
        return connection
    raise OSError("media connection failed") from None


def _validated_target(url: str, validate_url: Callable[[str], None]) -> tuple[str, str]:
    validated_target: tuple[str, str] | None = None
    try:
        validate_url(url)
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme == "https"
            and hostname is not None
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
        ):
            target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            validated_target = (hostname, target)
    except Exception:  # noqa: BLE001 - validator and URL parser errors are untrusted.
        validated_target = None
    if validated_target is not None:
        return validated_target
    raise ValueError("invalid media target") from None


def _close_response(response: HTTPResponse) -> None:
    try:
        response.close()
    except Exception:  # noqa: BLE001 - cleanup must not mask the primary failure.
        return


def _close_connection(connection: HTTPSConnection) -> None:
    try:
        connection.close()
    except Exception:  # noqa: BLE001 - cleanup must not mask the primary failure.
        return


@contextmanager
def open_pinned_https(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int,
    validate_url: Callable[[str], None],
    max_redirects: int = 5,
) -> Iterator[HTTPResponse]:
    """Open one HTTPS response, DNS-pinning and revalidating every redirect hop."""
    if max_redirects < 0:
        raise ValueError("invalid media redirect limit") from None

    current = url
    for hop in range(max_redirects + 1):
        hostname, target = _validated_target(current, validate_url)
        connection = _connect_first_validated_ip(hostname, timeout)
        response: HTTPResponse | None = None
        try:
            request_failed = False
            try:
                connection.request("GET", target, headers={**headers, "Host": hostname})
                response = connection.getresponse()
                status = response.status
            except Exception:  # noqa: BLE001 - request errors may contain a signed URL.
                request_failed = True
            if request_failed or response is None:
                raise OSError("media request failed") from None
            active_response = response

            if status not in _REDIRECT_STATUSES:
                try:
                    yield active_response
                finally:
                    _close_response(active_response)
                    response = None
                return

            location: str | None = None
            location_failed = False
            try:
                location = active_response.getheader("Location")
            except Exception:  # noqa: BLE001 - Location is an untrusted response header.
                location_failed = True
            finally:
                _close_response(active_response)
                response = None
            if location_failed:
                raise OSError("media request failed") from None
            if location is None or hop == max_redirects:
                raise OSError("invalid media redirect") from None

            redirect_failed = False
            try:
                current = urljoin(current, location)
            except Exception:  # noqa: BLE001 - redirect targets are untrusted input.
                redirect_failed = True
            if redirect_failed:
                raise OSError("invalid media redirect") from None
        finally:
            try:
                if response is not None:
                    _close_response(response)
            finally:
                _close_connection(connection)

    raise OSError("media redirect limit") from None
