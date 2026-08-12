"""Pending physical-button report shared by the frame GET and the status POST.

A direct port of the sibling firmware's `button_report.c` — pure logic, no I/O,
so the delivery discipline is testable without GPIO or a server. The rules it
encodes are what make "one physical press produces exactly one server event"
true by construction rather than by convention:

1. A press is dispatched on the **frame GET** (`?button=&button_event_id=`), so
   the server acts on it *before* it composes the response — the repaint that
   comes back already reflects the press.
2. A 200, 204 **or 304** all prove the server received and dispatched the
   event, so the report is cleared before the normal status POST. 304 counts:
   "nothing changed" is still an acknowledgement that the request arrived.
3. Any pre-acknowledgement failure (network, auth, 5xx) *retains* the report,
   and the status POST delivers `{"button", "button_event_id"}` as the fallback
   path. The server dedups on the event id, so the belt-and-braces delivery
   cannot double-fire.

`event_id` is seeded randomly at start-up rather than from zero: the server
dedups against the last id it saw, and a client that restarts and replays
0, 1, 2, ... would have its first presses silently swallowed as duplicates.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass

# The id travels as a JSON number. Anything above 2^53 loses integer precision
# in a double-based JSON parser (the firmware's cJSON hits this too), so the
# seed is kept well below that and the counter only ever increments.
_EVENT_ID_SEED_MAX = 1 << 40

BUTTON_REPORT_FALLBACK_NOTE = (
    "delivered on /status only when the frame GET could not carry it"
)


@dataclass(frozen=True)
class ButtonPress:
    """One press awaiting delivery."""

    name: str
    event_id: int


class ButtonReport:
    """Holds at most one press awaiting delivery.

    At most one: a burst of presses between two wake cycles collapses to the
    most recent, which is what the user expects from a panel that repaints
    once per cycle. Thread-safe because the GPIO watcher sets from its own
    thread while the wake loop reads and clears.
    """

    def __init__(self, seed: int | None = None) -> None:
        self._lock = threading.Lock()
        self._pending: ButtonPress | None = None
        self._next_id = (
            seed if seed is not None else secrets.randbelow(_EVENT_ID_SEED_MAX)
        )

    def set(self, name: str) -> ButtonPress:
        """Record a press, allocating it the next event id. Replaces any
        press still undelivered."""
        with self._lock:
            press = ButtonPress(name=name, event_id=self._next_id)
            self._next_id += 1
            self._pending = press
            return press

    def pending(self) -> ButtonPress | None:
        with self._lock:
            return self._pending

    def frame_query_params(self) -> dict[str, str]:
        """Query params for the frame GET; empty when nothing is pending."""
        press = self.pending()
        if press is None:
            return {}
        return {"button": press.name, "button_event_id": str(press.event_id)}

    def status_fields(self) -> dict[str, object]:
        """Fallback delivery fields for the status body; empty when nothing
        is pending (i.e. when the frame GET already acknowledged it)."""
        press = self.pending()
        if press is None:
            return {}
        return {"button": press.name, "button_event_id": press.event_id}

    def finish_frame(self, acknowledged: bool) -> None:
        """Resolve the frame GET. `acknowledged` must be True only for a
        response that proves the server received the request (200/204/304)."""
        if not acknowledged:
            return
        with self._lock:
            self._pending = None

    def clear(self) -> None:
        with self._lock:
            self._pending = None


def frame_status_acknowledges(code: int) -> bool:
    """Does this frame-GET status code prove the server got the request?

    200/204/304 do. Everything else — including 401/403/429/5xx — does not,
    so the press stays pending for the status POST to deliver.
    """
    return code in (200, 204, 304)


__all__ = [
    "BUTTON_REPORT_FALLBACK_NOTE",
    "ButtonPress",
    "ButtonReport",
    "frame_status_acknowledges",
]
