"""Deck manifest parsing and local navigation.

Manifest shapes here mirror what the server's app/deck_sync.py build_manifest
actually emits, not a paraphrase of it.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from tesserae_pi_bin_client.deck import (
    DeckParseError,
    nav_button,
    nav_zone,
    parse_manifest,
    sync_plan,
)

D1 = "0123456789abcdef"
D2 = "fedcba9876543210"
D3 = "aaaabbbbccccdddd"


def _manifest(**over: Any) -> str:
    body: dict[str, Any] = {
        "deck_id": "deck1",
        "version": "v1",
        "entry_page_id": "home",
        "pages": [
            {
                "page_id": "home",
                "digest": D1,
                "bytes": 100,
                "ttl_s": 3600,
                "links": [
                    {"button": "right", "zone": None, "target_page_id": "next"},
                    {"button": "left", "zone": None, "target_page_id": "prev"},
                ],
            },
            {"page_id": "next", "digest": D2, "bytes": 100, "ttl_s": 3600, "links": []},
            {"page_id": "prev", "digest": D3, "bytes": 100, "ttl_s": 3600, "links": []},
        ],
    }
    body.update(over)
    return json.dumps(body)


# --- parsing -------------------------------------------------------------------


def test_parses_a_server_shaped_manifest() -> None:
    m = parse_manifest(_manifest())
    assert m.deck_id == "deck1"
    assert m.version == "v1"
    assert m.entry_page_id == "home"
    assert len(m.pages) == 3
    assert m.page("home") is not None


def test_home_card_is_read_when_present() -> None:
    m = parse_manifest(_manifest(home={"page_id": "home", "timeout_s": 600}))
    assert m.home_page_id == "home"
    assert m.home_timeout_s == 600


def test_bad_digest_fails_the_whole_manifest() -> None:
    """A page we could never verify must not be half-trusted."""
    body = json.loads(_manifest())
    body["pages"][0]["digest"] = "nothex"
    with pytest.raises(DeckParseError, match="bad digest"):
        parse_manifest(json.dumps(body))


def test_zero_bytes_fails_the_whole_manifest() -> None:
    body = json.loads(_manifest())
    body["pages"][1]["bytes"] = 0
    with pytest.raises(DeckParseError, match="bytes="):
        parse_manifest(json.dumps(body))


def test_missing_version_fails() -> None:
    body = json.loads(_manifest())
    del body["version"]
    with pytest.raises(DeckParseError, match="missing version"):
        parse_manifest(json.dumps(body))


def test_not_json_fails_cleanly() -> None:
    with pytest.raises(DeckParseError, match="not JSON"):
        parse_manifest("<html>502 Bad Gateway</html>")


def test_empty_pages_fails() -> None:
    with pytest.raises(DeckParseError, match="no pages"):
        parse_manifest(json.dumps({"deck_id": "d", "version": "v", "pages": []}))


def test_a_bad_link_is_dropped_not_fatal() -> None:
    """One broken edge must not cost the whole deck's local navigation."""
    body = json.loads(_manifest())
    body["pages"][0]["links"].append(
        {"button": "refresh", "zone": None, "target_page_id": "does_not_exist"}
    )
    m = parse_manifest(json.dumps(body))
    assert nav_button(m, "home", "refresh") is None
    # ...but the good links still work.
    assert nav_button(m, "home", "right") == "next"


def test_link_with_neither_button_nor_zone_is_dropped() -> None:
    body = json.loads(_manifest())
    body["pages"][0]["links"] = [
        {"button": None, "zone": None, "target_page_id": "next"}
    ]
    m = parse_manifest(json.dumps(body))
    assert m.page("home") is not None
    assert m.page("home").links == ()  # type: ignore[union-attr]


def test_unknown_entry_page_falls_back_to_the_first() -> None:
    m = parse_manifest(_manifest(entry_page_id="ghost"))
    assert m.entry_page_id == "home"


# --- navigation ----------------------------------------------------------------


def test_button_navigation() -> None:
    m = parse_manifest(_manifest())
    assert nav_button(m, "home", "right") == "next"
    assert nav_button(m, "home", "left") == "prev"
    assert nav_button(m, "home", "refresh") is None
    assert nav_button(m, "next", "right") is None
    assert nav_button(m, "ghost", "right") is None


def test_first_matching_link_wins() -> None:
    body = json.loads(_manifest())
    body["pages"][0]["links"] = [
        {"button": "right", "zone": None, "target_page_id": "prev"},
        {"button": "right", "zone": None, "target_page_id": "next"},
    ]
    m = parse_manifest(json.dumps(body))
    assert nav_button(m, "home", "right") == "prev"


def test_zone_hit_test_uses_the_pixel_centre() -> None:
    body = json.loads(_manifest())
    body["pages"][0]["links"] = [
        {"button": None, "zone": {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0},
         "target_page_id": "prev"},
        {"button": None, "zone": {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0},
         "target_page_id": "next"},
    ]
    m = parse_manifest(json.dumps(body))
    # Panel 100 wide: pixel 49 centre = 0.495 -> left; pixel 50 = 0.505 -> right.
    assert nav_zone(m, "home", 49, 10, 100, 100) == "prev"
    assert nav_zone(m, "home", 50, 10, 100, 100) == "next"


def test_abutting_zones_never_both_claim_a_pixel() -> None:
    body = json.loads(_manifest())
    body["pages"][0]["links"] = [
        {"button": None, "zone": {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0},
         "target_page_id": "prev"},
        {"button": None, "zone": {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0},
         "target_page_id": "next"},
    ]
    m = parse_manifest(json.dumps(body))
    hits = [nav_zone(m, "home", x, 0, 100, 100) for x in range(100)]
    assert all(h is not None for h in hits)
    assert hits.count("prev") == 50
    assert hits.count("next") == 50


def test_zone_miss_returns_none() -> None:
    body = json.loads(_manifest())
    body["pages"][0]["links"] = [
        {"button": None, "zone": {"x": 0.0, "y": 0.0, "w": 0.25, "h": 0.25},
         "target_page_id": "next"},
    ]
    m = parse_manifest(json.dumps(body))
    assert nav_zone(m, "home", 90, 90, 100, 100) is None


# --- sync ----------------------------------------------------------------------


def test_sync_plan_fetches_missing_and_prunes_orphans() -> None:
    m = parse_manifest(_manifest())
    fetch, orphan = sync_plan(m, have={D1, "9999888877776666"})
    assert fetch == sorted([D2, D3])
    assert orphan == ["9999888877776666"]


def test_pages_marked_uncacheable_are_not_fetched_and_are_pruned() -> None:
    """The server marks overflow pages cache:false against our advertised
    capacity; holding them anyway would defeat the point."""
    body = json.loads(_manifest())
    body["pages"][1]["cache"] = False
    m = parse_manifest(json.dumps(body))

    fetch, orphan = sync_plan(m, have={D2})
    assert D2 not in fetch
    assert D2 in orphan
