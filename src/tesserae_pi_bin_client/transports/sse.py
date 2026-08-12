"""Server-Sent Events push channel — instant repaints instead of poll latency.

`GET /api/v1/device/<id>/stream` (Bearer, `Accept: text/event-stream`) emits
`values` / `patches` / `sync` envelopes with a comment keepalive every 25 s.

The sibling firmware gates this behind kiosk mode because a deep-sleeping,
battery-powered device cannot hold a socket open. That constraint does not
exist on a mains-powered Pi, which makes this the one firmware feature that
fits *better* here than it does there: instead of waiting out a 60 s poll, the
daemon repaints the moment the server says the frame moved.

Only `sync` is acted on — it means "the frame/manifest digests moved", so the
existing frame GET is re-run and the normal paint path handles the rest.
`values` and `patches` drive on-device overlay rendering, which needs a
partial-refresh panel the Impression doesn't have; they are ignored by design,
not by omission.

The stream is an OPTIMISATION, never a correctness requirement — polling
carries the same information. Every failure path here degrades to "keep
polling" rather than raising.
"""
from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable, Iterable, Iterator

import requests

log = logging.getLogger(__name__)

# Reconnect backoff, matching the firmware's sse_client: 1 s doubling to 60 s,
# with +/-25 % jitter so a fleet of panels reconnecting after a server restart
# doesn't arrive as a thundering herd.
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0
BACKOFF_JITTER = 0.25

# The server sends a keepalive comment every 25 s. Silence well past that means
# a dead connection TCP hasn't noticed yet (a NAT idle-timeout drops the socket
# without an RST), so force a reconnect.
#
# This doubles as the socket read timeout, which is what makes the rule
# self-enforcing: the keepalive keeps bytes flowing on a healthy stream, so a
# read timeout *is* the silence condition and needs no separate deadline. It
# must stay comfortably above the keepalive interval — set it below 25 s and
# every healthy stream reconnects on a timer.
SILENCE_TIMEOUT_S = 60.0


def _jittered(delay: float) -> float:
    return delay * (1.0 + random.uniform(-BACKOFF_JITTER, BACKOFF_JITTER))


def parse_event_lines(lines: list[str]) -> tuple[str, str]:
    """Reduce one SSE event's raw lines to (event_name, data).

    Per the SSE spec: `field: value`, a leading space after the colon is
    stripped, multiple `data:` lines join with newlines, a line starting with
    `:` is a comment, and a bare field name with no colon has an empty value.
    Absent an explicit `event:`, the name defaults to "message".
    """
    name = ""
    data_parts: list[str] = []
    for line in lines:
        if not line or line.startswith(":"):
            continue
        field, sep, value = line.partition(":")
        if not sep:
            field, value = line, ""
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            name = value
        elif field == "data":
            data_parts.append(value)
    return (name or "message"), "\n".join(data_parts)


def iter_events(chunks: Iterable[str]) -> Iterator[tuple[str, str]]:
    """Yield (event_name, data) pairs from an iterable of decoded text chunks.

    Written against text chunks rather than requests' `iter_lines` so the
    parser can be tested against arbitrary chunk boundaries — an event split
    mid-line across two TCP reads is the case that breaks naive line
    splitting, and it happens in the field.
    """
    buffer = ""
    pending: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk
        # Normalise CRLF/CR so the dispatch rule is a single blank line.
        buffer = buffer.replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in buffer:
            line, _, buffer = buffer.partition("\n")
            if line == "":
                # A block of nothing but comments is a keepalive, not an
                # event: dispatching it would fake a "message" every 25 s.
                if any(not ln.startswith(":") for ln in pending):
                    yield parse_event_lines(pending)
                pending = []
            else:
                pending.append(line)


class SseListener:
    """Holds the stream on a daemon thread, calling `on_sync` per sync event.

    The caller's wake loop waits on its own shutdown Event; `on_sync` is what
    lets a push cut that wait short. Nothing here paints or fetches — that
    stays in the wake loop, so there is exactly one code path that can drive
    the panel.
    """

    def __init__(
        self,
        base_url: str,
        device_id: str,
        token: str,
        on_sync: Callable[[], None],
        shutdown: threading.Event,
        session: requests.Session | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/api/v1/device/{device_id}/stream"
        self._token = token
        self._on_sync = on_sync
        self._shutdown = shutdown
        self._session = session
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="tesserae-sse", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        backoff = BACKOFF_MIN_S
        while not self._shutdown.is_set():
            try:
                clean = self._run_once()
            except (requests.RequestException, OSError) as exc:
                log.debug("sse: stream ended (%s: %s)", type(exc).__name__, exc)
                clean = False
            finally:
                self._connected.clear()
            if self._shutdown.is_set():
                return
            # A stream that carried events before dropping gets the fast
            # retry; one that failed to establish backs off, so an
            # SSE-less server is probed sparingly rather than hammered.
            backoff = BACKOFF_MIN_S if clean else min(backoff * 2, BACKOFF_MAX_S)
            self._shutdown.wait(_jittered(backoff))

    def _run_once(self) -> bool:
        """One connection. Returns True if it delivered at least one event."""
        session = self._session if self._session is not None else requests
        resp = session.get(
            self._url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "X-Tesserae-Token": self._token,
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
            },
            stream=True,
            timeout=(10.0, SILENCE_TIMEOUT_S),
        )
        if resp.status_code != 200:
            # 404 = server predates the push channel; 401/403 = the wake loop
            # will notice and re-pair. Either way: keep polling, stay quiet.
            log.debug("sse: stream unavailable (HTTP %d)", resp.status_code)
            resp.close()
            return False

        log.info("sse: stream connected; repaints will be pushed")
        self._connected.set()
        delivered = False
        try:
            # The generator ends on a read timeout — i.e. on the silence
            # condition — which returns us to the reconnect backoff. A
            # shutdown mid-stream can block here until the next byte or
            # timeout; the thread is a daemon and stop() does not wait on it,
            # so process exit is never held up by it.
            for name, data in iter_events(_resilient_chunks(resp)):
                if self._shutdown.is_set():
                    return delivered
                delivered = True
                self._dispatch(name, data)
        finally:
            resp.close()
        return delivered

    def _dispatch(self, name: str, data: str) -> None:
        if name == "sync":
            log.info("sse: sync push — waking the frame cycle")
            self._on_sync()
        else:
            # values/patches drive overlay rendering on partial-refresh
            # panels; the Impression has none. Logged at debug so an
            # unexpected event name is still discoverable.
            log.debug("sse: ignoring %r event (%d bytes)", name, len(data))


def _resilient_chunks(resp: requests.Response) -> Iterator[str]:
    """Decoded text chunks from the response, ending quietly on read timeout.

    A read timeout is the normal way this stream goes idle — it means the
    keepalive is overdue, which the caller handles as a reconnect, not an
    error worth raising through.
    """
    try:
        for raw in resp.iter_content(chunk_size=None):
            if not raw:
                continue
            yield raw.decode("utf-8", errors="replace")
    except (requests.RequestException, OSError) as exc:
        log.debug("sse: read ended (%s: %s)", type(exc).__name__, exc)


__all__ = [
    "BACKOFF_MAX_S",
    "BACKOFF_MIN_S",
    "SILENCE_TIMEOUT_S",
    "SseListener",
    "iter_events",
    "parse_event_lines",
]
