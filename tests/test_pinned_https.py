from __future__ import annotations

import ssl
from collections.abc import Callable
from typing import ClassVar
from urllib.parse import urlsplit

import pytest

from xhs_workbench import pinned_https


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: bytes = b"",
        location: str | None = None,
        read_error: BaseException | None = None,
        header_error: BaseException | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._location = location
        self._read_error = read_error
        self._header_error = header_error
        self._close_error = close_error
        self.closed = False

    def getheader(self, name: str) -> str | None:
        if self._header_error is not None:
            raise self._header_error
        return self._location if name == "Location" else None

    def read(self) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        return self._body

    def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class RecordingConnection:
    instances: ClassVar[list[RecordingConnection]] = []
    response_factory: ClassVar[Callable[[], FakeResponse]] = staticmethod(lambda: FakeResponse(200))
    request_error: ClassVar[BaseException | None] = None

    def __init__(self, hostname: str, pinned_ip: str, **kwargs: object) -> None:
        self.hostname = hostname
        self.pinned_ip = pinned_ip
        self.kwargs = kwargs
        self.response: FakeResponse | None = None
        self.closed = False
        self.request_args: tuple[object, ...] | None = None
        self.request_kwargs: dict[str, object] | None = None
        type(self).instances.append(self)

    def request(self, *args: object, **kwargs: object) -> None:
        self.request_args = args
        self.request_kwargs = kwargs
        if type(self).request_error is not None:
            raise type(self).request_error

    def connect(self) -> None:
        pass

    def getresponse(self) -> FakeResponse:
        self.response = type(self).response_factory()
        return self.response

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def reset_recording_connection() -> None:
    RecordingConnection.instances = []
    RecordingConnection.response_factory = staticmethod(lambda: FakeResponse(200))
    RecordingConnection.request_error = None


def redirect_then_success_connection() -> type[RecordingConnection]:
    responses = iter([FakeResponse(302, location="https://second.xhscdn.com/b"), FakeResponse(200)])

    class RedirectConnection(RecordingConnection):
        @classmethod
        def response_factory(cls) -> FakeResponse:
            return next(responses)

    return RedirectConnection


def _validate_xhs_media_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
    ):
        raise ValueError("unsafe media URL")


def test_transport_connects_to_validated_ip_but_preserves_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(pinned_https, "_resolve_global_ips", lambda _host: ("203.0.113.10",))

    class Connection:
        def __init__(self, hostname: str, pinned_ip: str, **_kwargs: object) -> None:
            calls.append((hostname, pinned_ip))
            self.response = FakeResponse(200, b"media")

        def request(self, *_args: object, **_kwargs: object) -> None:
            pass

        def connect(self) -> None:
            pass

        def getresponse(self) -> FakeResponse:
            return self.response

        def close(self) -> None:
            pass

    monkeypatch.setattr(pinned_https, "_PinnedHTTPSConnection", Connection)

    with pinned_https.open_pinned_https(
        "https://sns-video-qc.xhscdn.com/path/video.mp4?sig=ephemeral",
        headers={},
        timeout=15,
        validate_url=lambda _url: None,
    ) as response:
        assert response.read() == b"media"

    assert calls == [("sns-video-qc.xhscdn.com", "203.0.113.10")]


def test_transport_does_not_reresolve_at_connection_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = iter([("203.0.113.10",), ("127.0.0.1",)])
    resolver_calls = 0

    def resolve(_host: str) -> tuple[str, ...]:
        nonlocal resolver_calls
        resolver_calls += 1
        return next(resolutions)

    monkeypatch.setattr(pinned_https, "_resolve_global_ips", resolve)
    monkeypatch.setattr(pinned_https, "_PinnedHTTPSConnection", RecordingConnection)

    with pinned_https.open_pinned_https(
        "https://sns-webpic-qc.xhscdn.com/a.jpg", headers={}, timeout=15, validate_url=lambda _url: None
    ):
        pass

    assert resolver_calls == 1


def test_transport_tries_each_ip_from_one_resolution_until_tls_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver_calls = 0
    attempts: list[str] = []

    def resolve(_host: str) -> tuple[str, ...]:
        nonlocal resolver_calls
        resolver_calls += 1
        return ("203.0.113.10", "203.0.113.11")

    class FailFirstTlsConnection(RecordingConnection):
        def connect(self) -> None:
            attempts.append(self.pinned_ip)
            if self.pinned_ip == "203.0.113.10":
                raise OSError("TLS failed")

    monkeypatch.setattr(pinned_https, "_resolve_global_ips", resolve)
    monkeypatch.setattr(pinned_https, "_PinnedHTTPSConnection", FailFirstTlsConnection)

    with pinned_https.open_pinned_https(
        "https://sns-webpic-qc.xhscdn.com/a.jpg", headers={}, timeout=15, validate_url=lambda _url: None
    ):
        pass

    assert resolver_calls == 1
    assert attempts == ["203.0.113.10", "203.0.113.11"]
    assert RecordingConnection.instances[0].closed is True
    assert RecordingConnection.instances[1].closed is True


def test_redirect_is_revalidated_and_repinned(monkeypatch: pytest.MonkeyPatch) -> None:
    validated: list[str] = []
    monkeypatch.setattr(
        pinned_https,
        "_resolve_global_ips",
        lambda host: ("203.0.113.10" if host.startswith("first") else "203.0.113.11",),
    )
    monkeypatch.setattr(pinned_https, "_PinnedHTTPSConnection", redirect_then_success_connection())

    with pinned_https.open_pinned_https(
        "https://first.xhscdn.com/a", headers={}, timeout=15, validate_url=validated.append
    ):
        pass

    assert validated == ["https://first.xhscdn.com/a", "https://second.xhscdn.com/b"]


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.1.1", "::1", "fe80::1"])
def test_resolution_rejects_private_and_link_local_addresses(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    monkeypatch.setattr(
        pinned_https.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, (address, 443))],
    )

    with pytest.raises(ValueError, match="unsafe media address"):
        pinned_https._resolve_global_ips("cdn.xhscdn.com")


def test_resolution_deduplicates_and_orders_only_global_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pinned_https.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (None, None, None, None, ("8.8.4.4", 443)),
            (None, None, None, None, ("8.8.8.8", 443)),
            (None, None, None, None, ("8.8.4.4", 443)),
        ],
    )

    assert pinned_https._resolve_global_ips("cdn.xhscdn.com") == ("8.8.4.4", "8.8.8.8")


def test_resolution_does_not_retain_malformed_address_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pinned_https.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (None, None, None, None, ("not-an-ip?signature=concealed", 443))
        ],
    )

    with pytest.raises(ValueError, match="unsafe media address") as error:
        pinned_https._resolve_global_ips("cdn.xhscdn.com")

    assert "signature=concealed" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_pinned_connection_rejects_connected_peer_mismatch_and_closes_raw_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RawSocket:
        closed = False

        def getpeername(self) -> tuple[str, int]:
            return ("8.8.4.4", 443)

        def close(self) -> None:
            self.closed = True

    raw = RawSocket()
    monkeypatch.setattr(pinned_https.socket, "create_connection", lambda *_args, **_kwargs: raw)
    connection = pinned_https._PinnedHTTPSConnection(
        "cdn.xhscdn.com", "8.8.8.8", timeout=15, context=ssl.create_default_context()
    )

    with pytest.raises(OSError, match="connected peer mismatch"):
        connection.connect()

    assert raw.closed is True


def test_pinned_connection_uses_original_hostname_for_tls_sni(monkeypatch: pytest.MonkeyPatch) -> None:
    class RawSocket:
        def getpeername(self) -> tuple[str, int]:
            return ("8.8.8.8", 443)

        def close(self) -> None:
            pass

    class Context:
        server_hostname: str | None = None

        def wrap_socket(self, raw: RawSocket, *, server_hostname: str) -> RawSocket:
            self.server_hostname = server_hostname
            return raw

    context = Context()
    monkeypatch.setattr(pinned_https.socket, "create_connection", lambda *_args, **_kwargs: RawSocket())
    connection = pinned_https._PinnedHTTPSConnection(
        "media.xhscdn.com", "8.8.8.8", timeout=15, context=context  # type: ignore[arg-type]
    )

    connection.connect()

    assert context.server_hostname == "media.xhscdn.com"


def test_transport_closes_response_and_connection_when_response_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(200, read_error=OSError("read failed"))
    RecordingConnection.response_factory = staticmethod(lambda: response)
    monkeypatch.setattr(pinned_https, "_resolve_global_ips", lambda _host: ("203.0.113.10",))
    monkeypatch.setattr(pinned_https, "_PinnedHTTPSConnection", RecordingConnection)

    with pytest.raises(OSError, match="read failed"), pinned_https.open_pinned_https(
        "https://sns-webpic-qc.xhscdn.com/a.jpg",
        headers={},
        timeout=15,
        validate_url=lambda _url: None,
    ) as actual_response:
        actual_response.read()

    assert response.closed is True
    assert RecordingConnection.instances[0].closed is True


def test_transport_preserves_read_error_when_response_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(200, close_error=RuntimeError("close failed"))
    RecordingConnection.response_factory = staticmethod(lambda: response)
    monkeypatch.setattr(pinned_https, "_resolve_global_ips", lambda _host: ("203.0.113.10",))
    monkeypatch.setattr(pinned_https, "_PinnedHTTPSConnection", RecordingConnection)

    with pytest.raises(OSError, match="caller read failed"), pinned_https.open_pinned_https(
        "https://sns-webpic-qc.xhscdn.com/a.jpg",
        headers={},
        timeout=15,
        validate_url=lambda _url: None,
    ):
        raise OSError("caller read failed")

    assert response.closed is True
    assert RecordingConnection.instances[0].closed is True


def test_transport_closes_connection_when_request_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingConnection.request_error = OSError("request failed")
    monkeypatch.setattr(pinned_https, "_resolve_global_ips", lambda _host: ("203.0.113.10",))
    monkeypatch.setattr(pinned_https, "_PinnedHTTPSConnection", RecordingConnection)

    with pytest.raises(OSError, match="media request failed"), pinned_https.open_pinned_https(
        "https://sns-webpic-qc.xhscdn.com/a.jpg",
        headers={},
        timeout=15,
        validate_url=lambda _url: None,
    ):
        pass

    assert RecordingConnection.instances[0].closed is True


def test_transport_rejects_missing_redirect_location_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(302)
    RecordingConnection.response_factory = staticmethod(lambda: response)
    monkeypatch.setattr(pinned_https, "_resolve_global_ips", lambda _host: ("203.0.113.10",))
    monkeypatch.setattr(pinned_https, "_PinnedHTTPSConnection", RecordingConnection)

    with pytest.raises(OSError, match="invalid media redirect"), pinned_https.open_pinned_https(
        "https://sns-webpic-qc.xhscdn.com/a.jpg",
        headers={},
        timeout=15,
        validate_url=lambda _url: None,
    ):
        pass

    assert response.closed is True
    assert RecordingConnection.instances[0].closed is True


def test_transport_closes_response_when_redirect_metadata_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(
        302,
        header_error=OSError("Location https://sns-webpic-qc.xhscdn.com/a.jpg?signature=concealed"),
    )
    RecordingConnection.response_factory = staticmethod(lambda: response)
    monkeypatch.setattr(pinned_https, "_resolve_global_ips", lambda _host: ("203.0.113.10",))
    monkeypatch.setattr(pinned_https, "_PinnedHTTPSConnection", RecordingConnection)

    with pytest.raises(OSError, match="media request failed") as error, pinned_https.open_pinned_https(
        "https://sns-webpic-qc.xhscdn.com/a.jpg",
        headers={},
        timeout=15,
        validate_url=lambda _url: None,
    ):
        pass

    assert response.closed is True
    assert RecordingConnection.instances[0].closed is True
    assert "signature=concealed" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_transport_rejects_redirect_loop_at_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingConnection.response_factory = staticmethod(
        lambda: FakeResponse(302, location="https://sns-webpic-qc.xhscdn.com/a.jpg")
    )
    monkeypatch.setattr(pinned_https, "_resolve_global_ips", lambda _host: ("203.0.113.10",))
    monkeypatch.setattr(pinned_https, "_PinnedHTTPSConnection", RecordingConnection)

    with pytest.raises(OSError, match="invalid media redirect"), pinned_https.open_pinned_https(
        "https://sns-webpic-qc.xhscdn.com/a.jpg",
        headers={},
        timeout=15,
        validate_url=lambda _url: None,
        max_redirects=1,
    ):
        pass

    assert len(RecordingConnection.instances) == 2
    assert all(connection.closed for connection in RecordingConnection.instances)


@pytest.mark.parametrize(
    "url",
    [
        "http://sns-webpic-qc.xhscdn.com/a.jpg",
        "https://reader@sns-webpic-qc.xhscdn.com/a.jpg",
        "https://sns-webpic-qc.xhscdn.com:444/a.jpg",
    ],
)
def test_transport_validates_unsafe_target_before_resolving(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    validations: list[str] = []
    monkeypatch.setattr(
        pinned_https,
        "_resolve_global_ips",
        lambda _host: pytest.fail("unsafe target was resolved"),
    )

    def validate(value: str) -> None:
        validations.append(value)
        _validate_xhs_media_url(value)

    with pytest.raises(ValueError, match="invalid media target"), pinned_https.open_pinned_https(
        url, headers={}, timeout=15, validate_url=validate
    ):
        pass

    assert validations == [url]


def test_transport_does_not_leak_query_when_validation_fails() -> None:
    secret_url = "https://sns-webpic-qc.xhscdn.com/a.jpg?signature=concealed"

    def reject(url: str) -> None:
        raise ValueError(f"invalid {url}")

    with pytest.raises(ValueError) as error, pinned_https.open_pinned_https(
        secret_url, headers={}, timeout=15, validate_url=reject
    ):
        pass

    assert "signature=concealed" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_transport_sanitizes_unexpected_validation_error() -> None:
    secret_url = "https://sns-webpic-qc.xhscdn.com/a.jpg?signature=concealed"

    def reject(url: str) -> None:
        raise RuntimeError(f"invalid {url}")

    with pytest.raises(ValueError, match="invalid media target") as error, pinned_https.open_pinned_https(
        secret_url, headers={}, timeout=15, validate_url=reject
    ):
        pass

    assert "signature=concealed" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_transport_sanitizes_request_error_exception_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingConnection.request_error = OSError(
        "request https://sns-webpic-qc.xhscdn.com/a.jpg?signature=concealed"
    )
    monkeypatch.setattr(pinned_https, "_resolve_global_ips", lambda _host: ("203.0.113.10",))
    monkeypatch.setattr(pinned_https, "_PinnedHTTPSConnection", RecordingConnection)

    with pytest.raises(OSError, match="media request failed") as error, pinned_https.open_pinned_https(
        "https://sns-webpic-qc.xhscdn.com/a.jpg", headers={}, timeout=15, validate_url=lambda _url: None
    ):
        pass

    assert "signature=concealed" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
