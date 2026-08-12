"""Button delivery discipline — the "exactly one server event per press"
rules ported from the sibling firmware's button_report.c. Pure logic: no
GPIO, no HTTP.
"""
from __future__ import annotations

from tesserae_pi_bin_client.button_report import (
    ButtonReport,
    frame_status_acknowledges,
)


def test_no_press_produces_no_query_or_status_fields() -> None:
    report = ButtonReport(seed=100)
    assert report.pending() is None
    assert report.frame_query_params() == {}
    assert report.status_fields() == {}


def test_press_rides_the_frame_query() -> None:
    report = ButtonReport(seed=100)
    press = report.set("refresh")
    assert press.event_id == 100
    assert report.frame_query_params() == {
        "button": "refresh",
        "button_event_id": "100",
    }


def test_event_ids_increment_per_press() -> None:
    report = ButtonReport(seed=7)
    assert report.set("left").event_id == 7
    assert report.set("right").event_id == 8
    assert report.set("refresh").event_id == 9


def test_seed_is_not_zero_based_by_default() -> None:
    """A restart must not replay ids the server has already deduped away."""
    seeds = {ButtonReport().set("refresh").event_id for _ in range(20)}
    # Astronomically unlikely to collide on 0 twenty times unless unseeded.
    assert seeds != {0}


def test_burst_of_presses_collapses_to_the_latest() -> None:
    """The panel repaints once per cycle, so only the last press matters."""
    report = ButtonReport(seed=1)
    report.set("left")
    report.set("right")
    pending = report.pending()
    assert pending is not None
    assert pending.name == "right"
    assert pending.event_id == 2


def test_acknowledged_frame_clears_the_press() -> None:
    report = ButtonReport(seed=1)
    report.set("refresh")
    report.finish_frame(True)
    assert report.pending() is None
    # ...so /status must not deliver it a second time.
    assert report.status_fields() == {}


def test_unacknowledged_frame_retains_the_press_for_status() -> None:
    report = ButtonReport(seed=42)
    report.set("left")
    report.finish_frame(False)
    assert report.status_fields() == {"button": "left", "button_event_id": 42}


def test_304_counts_as_an_acknowledgement() -> None:
    """"Nothing changed" still proves the request arrived and the action ran."""
    assert frame_status_acknowledges(304) is True
    report = ButtonReport(seed=1)
    report.set("refresh")
    report.finish_frame(frame_status_acknowledges(304))
    assert report.pending() is None


def test_acknowledging_statuses() -> None:
    for code in (200, 204, 304):
        assert frame_status_acknowledges(code) is True
    # Auth, rate-limit and server errors do NOT prove dispatch.
    for code in (401, 403, 404, 429, 500, 502):
        assert frame_status_acknowledges(code) is False


def test_clear_drops_a_pending_press() -> None:
    report = ButtonReport(seed=1)
    report.set("refresh")
    report.clear()
    assert report.pending() is None
