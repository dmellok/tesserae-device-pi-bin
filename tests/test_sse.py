"""SSE framing + dispatch. The parser is fed arbitrary chunk boundaries on
purpose: an event split mid-line across two TCP reads is the case that breaks
naive line splitting, and it happens in the field.
"""
from __future__ import annotations

import threading
from typing import Any

from tesserae_pi_bin_client.transports.sse import (
    SILENCE_TIMEOUT_S,
    SseListener,
    iter_events,
    parse_event_lines,
)


def _events(*chunks: str) -> list[tuple[str, str]]:
    return list(iter_events(iter(chunks)))  # type: ignore[call-overload]


def test_parses_a_simple_event() -> None:
    assert _events('event: sync\ndata: {"seq":1}\n\n') == [("sync", '{"seq":1}')]


def test_event_split_across_chunks_mid_line() -> None:
    assert _events("event: sy", "nc\ndata: {", '"seq":1}\n', "\n") == [
        ("sync", '{"seq":1}')
    ]


def test_multiple_events_in_one_chunk() -> None:
    assert _events("event: sync\ndata: a\n\nevent: values\ndata: b\n\n") == [
        ("sync", "a"),
        ("values", "b"),
    ]


def test_multiline_data_joins_with_newlines() -> None:
    assert _events("event: sync\ndata: one\ndata: two\n\n") == [
        ("sync", "one\ntwo")
    ]


def test_keepalive_comment_does_not_emit_an_event() -> None:
    """The server sends `:ka` every 25 s; dispatching it would fake a repaint."""
    assert _events(":ka\n\n") == []
    assert _events(":ka\n\n:ka\n\nevent: sync\ndata: x\n\n") == [("sync", "x")]


def test_crlf_line_endings() -> None:
    assert _events("event: sync\r\ndata: x\r\n\r\n") == [("sync", "x")]


def test_event_without_explicit_name_defaults_to_message() -> None:
    assert _events("data: x\n\n") == [("message", "x")]


def test_incomplete_trailing_event_is_not_emitted() -> None:
    """No blank line yet means the event hasn't been dispatched."""
    assert _events("event: sync\ndata: x\n") == []


def test_leading_space_after_colon_is_stripped_once_only() -> None:
    assert parse_event_lines(["data:  two spaces"]) == ("message", " two spaces")


def test_field_without_colon_has_empty_value() -> None:
    assert parse_event_lines(["event", "data: x"]) == ("message", "x")


# --- dispatch ----------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, body: str = "") -> None:
        self.status_code = status_code
        self._body = body.encode("utf-8")
        self.closed = False

    def iter_content(self, chunk_size: Any = None) -> Any:
        yield self._body

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self, *responses: _FakeResponse) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if not self._responses:
            raise OSError("no more queued responses")
        return self._responses.pop(0)


def _listener(session: _FakeSession, on_sync: Any, shutdown: threading.Event) -> SseListener:
    return SseListener(
        "http://server:8765",
        "pi_bin",
        "tok",
        on_sync=on_sync,
        shutdown=shutdown,
        session=session,
    )


def test_sync_event_fires_the_callback() -> None:
    fired = threading.Event()
    session = _FakeSession(_FakeResponse(200, 'event: sync\ndata: {"seq":3}\n\n'))
    shutdown = threading.Event()
    listener = _listener(session, fired.set, shutdown)

    assert listener._run_once() is True
    assert fired.is_set()


def test_values_and_patches_are_ignored() -> None:
    """They drive overlay rendering, which needs partial refresh the
    Impression doesn't have. Ignored by design, not by omission."""
    calls: list[int] = []
    session = _FakeSession(
        _FakeResponse(200, "event: values\ndata: {}\n\nevent: patches\ndata: {}\n\n")
    )
    listener = _listener(session, lambda: calls.append(1), threading.Event())

    assert listener._run_once() is True  # events arrived...
    assert calls == []  # ...but none of them meant "repaint"


def test_stream_url_and_headers() -> None:
    session = _FakeSession(_FakeResponse(200, ""))
    listener = _listener(session, lambda: None, threading.Event())
    listener._run_once()

    call = session.calls[0]
    assert call["url"] == "http://server:8765/api/v1/device/pi_bin/stream"
    assert call["headers"]["Accept"] == "text/event-stream"
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["stream"] is True
    # Read timeout must clear the server's 25 s keepalive, or every healthy
    # stream reconnects on a timer.
    assert call["timeout"][1] == SILENCE_TIMEOUT_S
    assert SILENCE_TIMEOUT_S > 25


def test_non_200_is_not_an_error_just_no_push() -> None:
    """404 = server predates the push channel. Keep polling, stay quiet."""
    resp = _FakeResponse(404)
    session = _FakeSession(resp)
    listener = _listener(session, lambda: None, threading.Event())

    assert listener._run_once() is False
    assert resp.closed


def test_shutdown_stops_dispatch_mid_stream() -> None:
    shutdown = threading.Event()
    shutdown.set()
    session = _FakeSession(_FakeResponse(200, "event: sync\ndata: x\n\n"))
    fired: list[int] = []
    listener = _listener(session, lambda: fired.append(1), shutdown)

    listener._run_once()
    assert fired == []
