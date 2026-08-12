"""Album collection parsing, cache differ, and playback traversal.

Manifest shapes mirror the server's app/collection_sync.py build_manifest.
"""
from __future__ import annotations

import json
import random
from typing import Any

import pytest

from tesserae_pi_bin_client.collection import (
    INTERVAL_MIN_S,
    AlbumPlayer,
    CollectionParseError,
    clamp_interval,
    parse_manifest,
    sync_plan,
)

D = [f"{i:016x}" for i in range(1, 6)]


def _manifest(n: int = 3, **over: Any) -> str:
    body: dict[str, Any] = {
        "schema": 1,
        "collection_id": "album:7",
        "kind": "album",
        "version": "v1",
        "total_frames": n,
        "cursor": None,
        "next_cursor": None,
        "frames": [
            {
                "frame_id": f"f{i}",
                "position": i,
                "digest": D[i],
                "bytes": 100,
                "ttl_s": 86400,
                "cache": True,
                "url": f"/api/v1/device/pi_bin/collection/frame/{D[i]}",
            }
            for i in range(n)
        ],
        "producer": {
            "album": {
                "playback": {"mode": "sequential", "interval_s": 1800, "repeat": "loop"}
            }
        },
    }
    body.update(over)
    return json.dumps(body)


# --- parsing -------------------------------------------------------------------


def test_parses_a_server_shaped_manifest() -> None:
    m = parse_manifest(_manifest())
    assert m.collection_id == "album:7"
    assert m.kind == "album"
    assert len(m.frames) == 3
    assert m.playback.mode == "sequential"
    assert m.playback.interval_s == 1800
    assert m.playback.repeat == "loop"


def test_unsupported_schema_is_rejected() -> None:
    with pytest.raises(CollectionParseError, match="unsupported collection schema"):
        parse_manifest(_manifest(schema=2))


def test_bad_digest_on_a_cached_frame_fails() -> None:
    body = json.loads(_manifest())
    body["frames"][0]["digest"] = "zzz"
    with pytest.raises(CollectionParseError, match="bad digest"):
        parse_manifest(json.dumps(body))


def test_uncached_frames_may_be_thin() -> None:
    """cache:false entries count toward total but are never retained, so they
    don't need a usable digest."""
    body = json.loads(_manifest())
    body["frames"][2]["cache"] = False
    body["frames"][2]["digest"] = ""
    body["frames"][2]["bytes"] = 0
    m = parse_manifest(json.dumps(body))
    assert len(m.cached_frames()) == 2
    assert m.total_frames == 3


def test_frames_sort_by_producer_position() -> None:
    body = json.loads(_manifest())
    body["frames"][0]["position"] = 9
    m = parse_manifest(json.dumps(body))
    assert [f.frame_id for f in m.cached_frames()] == ["f1", "f2", "f0"]


def test_same_digest_may_occupy_several_positions() -> None:
    """The firmware had a bug rejecting this; a photo can legitimately repeat."""
    body = json.loads(_manifest())
    body["frames"][1]["digest"] = body["frames"][0]["digest"]
    m = parse_manifest(json.dumps(body))
    assert len(m.cached_frames()) == 3
    assert len(m.cacheable_digests()) == 2


def test_interval_is_clamped() -> None:
    """A photo frame repainting every few seconds would burn the panel's
    rated refreshes, and e-ink takes seconds to update anyway."""
    m = parse_manifest(
        _manifest(
            producer={"album": {"playback": {"mode": "shuffle", "interval_s": 1,
                                             "repeat": "loop"}}}
        )
    )
    assert m.playback.interval_s == INTERVAL_MIN_S
    assert clamp_interval(10) == INTERVAL_MIN_S
    assert clamp_interval(999_999) < 999_999


def test_unknown_mode_and_repeat_fall_back_to_defaults() -> None:
    m = parse_manifest(
        _manifest(
            producer={"album": {"playback": {"mode": "spiral", "interval_s": 600,
                                             "repeat": "forever"}}}
        )
    )
    assert m.playback.mode == "sequential"
    assert m.playback.repeat == "loop"


# --- sync ----------------------------------------------------------------------


def test_sync_plan() -> None:
    m = parse_manifest(_manifest())
    fetch, orphan = sync_plan(m, have={D[0], "ffffffffffffffff"})
    assert fetch == sorted([D[1], D[2]])
    assert orphan == ["ffffffffffffffff"]


# --- playback ------------------------------------------------------------------


def test_sequential_loop_cycles_in_position_order() -> None:
    player = AlbumPlayer(parse_manifest(_manifest()))
    order = [player.next_frame().frame_id for _ in range(6)]  # type: ignore[union-attr]
    assert order == ["f0", "f1", "f2", "f0", "f1", "f2"]


def test_once_holds_after_the_last_frame() -> None:
    m = parse_manifest(
        _manifest(
            producer={"album": {"playback": {"mode": "sequential",
                                             "interval_s": 600, "repeat": "once"}}}
        )
    )
    player = AlbumPlayer(m)
    assert [player.next_frame().frame_id for _ in range(3)] == ["f0", "f1", "f2"]  # type: ignore[union-attr]
    assert player.next_frame() is None
    assert player.exhausted


def test_shuffle_covers_every_frame_before_repeating() -> None:
    """A bag, not independent draws — the same photo twice in a row reads as
    a bug to anyone watching the frame."""
    m = parse_manifest(
        _manifest(
            producer={"album": {"playback": {"mode": "shuffle",
                                             "interval_s": 600, "repeat": "loop"}}}
        )
    )
    player = AlbumPlayer(m, rng=random.Random(7))
    first_pass = {player.next_frame().frame_id for _ in range(3)}  # type: ignore[union-attr]
    assert first_pass == {"f0", "f1", "f2"}


def test_version_change_resets_traversal() -> None:
    """Positions may have moved under us, so the old cursor means nothing."""
    player = AlbumPlayer(parse_manifest(_manifest()))
    player.next_frame()
    player.next_frame()
    player.adopt(parse_manifest(_manifest(version="v2")))
    assert player.next_frame().frame_id == "f0"  # type: ignore[union-attr]


def test_same_version_keeps_its_place() -> None:
    manifest = parse_manifest(_manifest())
    player = AlbumPlayer(manifest)
    player.next_frame()
    player.adopt(manifest)
    assert player.next_frame().frame_id == "f1"  # type: ignore[union-attr]


def test_empty_album_yields_nothing() -> None:
    player = AlbumPlayer(parse_manifest(_manifest(n=0, total_frames=0)))
    assert player.next_frame() is None
