"""Deck/album orchestration: capability gating, sync, local nav, and the two
contract rules whose failure modes are silent — a locally-served press being
double-counted, and a chatty dashboard starving album playback.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tesserae_pi_bin_client.cache_sync import CacheManager
from tesserae_pi_bin_client.frame_cache import digest_of

FRAME_A = b"A" * 100
FRAME_B = b"B" * 100
FRAME_C = b"C" * 100
DA, DB, DC = digest_of(FRAME_A), digest_of(FRAME_B), digest_of(FRAME_C)


class FakeClient:
    """Implements the ManifestClient protocol slice CacheManager needs."""

    def __init__(
        self,
        deck: tuple[int, str] = (204, ""),
        collection: tuple[int, str] = (204, ""),
        blobs: dict[str, bytes] | None = None,
    ) -> None:
        self._deck = deck
        self._collection = collection
        self._blobs = blobs or {}
        self.downloads: list[str] = []

    def get_deck_manifest(self, device_id: str, token: str) -> tuple[int, str]:
        return self._deck

    def get_collection_manifest(self, device_id: str, token: str) -> tuple[int, str]:
        return self._collection

    def deck_frame_url(self, device_id: str, digest: str) -> str:
        return f"http://srv/api/v1/device/{device_id}/deck/frame/{digest}"

    def absolute(self, url: str) -> str:
        return url if url.startswith("http") else f"http://srv{url}"

    def download_authed(
        self, url: str, token: str, max_bytes: int, timeout_s: float
    ) -> bytes:
        self.downloads.append(url)
        digest = url.rsplit("/", 1)[-1]
        if digest not in self._blobs:
            raise OSError("404")
        return self._blobs[digest]


def _deck_body(**over: Any) -> str:
    body: dict[str, Any] = {
        "deck_id": "deck1",
        "version": "v1",
        "entry_page_id": "home",
        "pages": [
            {
                "page_id": "home",
                "digest": DA,
                "bytes": len(FRAME_A),
                "ttl_s": 3600,
                "links": [{"button": "right", "zone": None, "target_page_id": "next"}],
            },
            {
                "page_id": "next",
                "digest": DB,
                "bytes": len(FRAME_B),
                "ttl_s": 3600,
                "links": [{"button": "left", "zone": None, "target_page_id": "home"}],
            },
        ],
    }
    body.update(over)
    return json.dumps(body)


def _album_body(**over: Any) -> str:
    body: dict[str, Any] = {
        "schema": 1,
        "collection_id": "album:7",
        "kind": "album",
        "version": "v1",
        "total_frames": 2,
        "frames": [
            {
                "frame_id": "p0", "position": 0, "digest": DA,
                "bytes": len(FRAME_A), "ttl_s": 86400, "cache": True,
                "url": f"/api/v1/device/pi_bin/collection/frame/{DA}",
            },
            {
                "frame_id": "p1", "position": 1, "digest": DC,
                "bytes": len(FRAME_C), "ttl_s": 86400, "cache": True,
                "url": f"/api/v1/device/pi_bin/collection/frame/{DC}",
            },
        ],
        "producer": {
            "album": {
                "playback": {"mode": "sequential", "interval_s": 600, "repeat": "loop"}
            }
        },
    }
    body.update(over)
    return json.dumps(body)


def _manager(tmp_path: Path, enabled: bool = True) -> CacheManager:
    return CacheManager(
        tmp_path, "pi_bin", max_frame_bytes=1_000_000,
        download_timeout_s=5.0, enabled=enabled,
    )


# --- capability gating ---------------------------------------------------------


def test_capabilities_advertise_both_caches(tmp_path: Path) -> None:
    caps = _manager(tmp_path).capabilities()
    assert caps["deck_cache"]["schema"] == 1
    assert caps["frame_cache"]["schema"] == 1
    assert caps["deck_cache"]["capacity_bytes"] > 0


def test_disabled_cache_advertises_nothing(tmp_path: Path) -> None:
    """The wire body must be byte-identical to a client that never had one."""
    assert _manager(tmp_path, enabled=False).capabilities() == {}


# --- deck sync + local nav -----------------------------------------------------


def test_deck_sync_fetches_and_verifies_pages(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    client = FakeClient(deck=(200, _deck_body()), blobs={DA: FRAME_A, DB: FRAME_B})
    mgr.sync_deck(client, "TOK")

    assert len(client.downloads) == 2
    assert mgr.has_deck


def test_local_nav_serves_a_page_without_the_network(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    client = FakeClient(deck=(200, _deck_body()), blobs={DA: FRAME_A, DB: FRAME_B})
    mgr.sync_deck(client, "TOK")

    assert mgr.local_nav("right", len(FRAME_B)) == FRAME_B
    assert mgr.current_page_id == "next"
    # ...and navigating back works from the new page.
    assert mgr.local_nav("left", len(FRAME_A)) == FRAME_A


def test_local_nav_reports_the_page_it_served(tmp_path: Path) -> None:
    """The server needs deck_page_id/deck_version to keep its nav state
    truthful, even though the press itself is never reported as an action."""
    mgr = _manager(tmp_path)
    client = FakeClient(deck=(200, _deck_body()), blobs={DA: FRAME_A, DB: FRAME_B})
    mgr.sync_deck(client, "TOK")
    mgr.local_nav("right", len(FRAME_B))

    report = mgr.report()
    assert report["deck_page_id"] == "next"
    assert report["deck_version"] == "v1"

    # The report is per-cycle; it must not repeat on the next beat.
    mgr.clear_paint_report()
    assert "deck_page_id" not in mgr.report()


def test_local_nav_declines_an_unmapped_button(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    client = FakeClient(deck=(200, _deck_body()), blobs={DA: FRAME_A, DB: FRAME_B})
    mgr.sync_deck(client, "TOK")
    assert mgr.local_nav("refresh", len(FRAME_A)) is None


def test_local_nav_declines_when_the_size_does_not_match_the_panel(
    tmp_path: Path,
) -> None:
    mgr = _manager(tmp_path)
    client = FakeClient(deck=(200, _deck_body()), blobs={DA: FRAME_A, DB: FRAME_B})
    mgr.sync_deck(client, "TOK")
    # Panel wants a different buffer size -> fall back to the network.
    assert mgr.local_nav("right", 999) is None


def test_local_nav_falls_back_when_the_page_is_missing_from_cache(
    tmp_path: Path,
) -> None:
    mgr = _manager(tmp_path)
    # Page "next" fails to download; nav must degrade to the network path.
    client = FakeClient(deck=(200, _deck_body()), blobs={DA: FRAME_A})
    mgr.sync_deck(client, "TOK")
    assert mgr.local_nav("right", len(FRAME_B)) is None


def test_bad_manifest_leaves_the_network_path_intact(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    mgr.sync_deck(FakeClient(deck=(200, "<html>502</html>")), "TOK")
    assert not mgr.has_deck
    assert mgr.local_nav("right", len(FRAME_B)) is None


def test_deck_unbound_drops_the_local_copy(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    client = FakeClient(deck=(200, _deck_body()), blobs={DA: FRAME_A, DB: FRAME_B})
    mgr.sync_deck(client, "TOK")
    assert mgr.has_deck

    mgr.note_status_response({})  # no "deck" block == unbound
    assert not mgr.has_deck


def test_version_change_triggers_resync(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    deck_changed, _ = mgr.note_status_response({"deck": {"version": "v2"}})
    assert deck_changed


def test_unchanged_version_does_not_resync(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    client = FakeClient(deck=(200, _deck_body()), blobs={DA: FRAME_A, DB: FRAME_B})
    mgr.sync_deck(client, "TOK")
    deck_changed, _ = mgr.note_status_response({"deck": {"version": "v1"}})
    assert not deck_changed


# --- album playback ------------------------------------------------------------


def test_album_plays_on_its_interval(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    client = FakeClient(collection=(200, _album_body()), blobs={DA: FRAME_A, DC: FRAME_C})
    mgr.sync_collection(client, "TOK")

    first = mgr.album_due(now=1000.0)
    assert first is not None and first.frame_id == "p0"
    # Too soon for the next one.
    assert mgr.album_due(now=1100.0) is None
    second = mgr.album_due(now=1000.0 + 600)
    assert second is not None and second.frame_id == "p1"


def test_album_frames_are_verified_before_use(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    client = FakeClient(collection=(200, _album_body()), blobs={DA: FRAME_A, DC: FRAME_C})
    mgr.sync_collection(client, "TOK")
    frame = mgr.album_due(now=1000.0)
    assert frame is not None

    assert mgr.album_frame_bytes(frame, len(FRAME_A)) == FRAME_A
    # A panel expecting a different size must not be handed the frame.
    assert mgr.album_frame_bytes(frame, 12345) is None


def test_network_frame_does_not_keep_postponing_the_album(tmp_path: Path) -> None:
    """A chatty dashboard would otherwise starve playback forever: each
    interrupt must resume at the EXISTING deadline, not a fresh interval."""
    mgr = _manager(tmp_path)
    client = FakeClient(collection=(200, _album_body()), blobs={DA: FRAME_A, DC: FRAME_C})
    mgr.sync_collection(client, "TOK")

    mgr.album_due(now=1000.0)  # deadline is now 1600
    for t in range(1000, 1600, 60):  # a network frame every minute
        mgr.note_network_frame(now=float(t))

    # The deadline never moved, so the next photo still lands on time.
    assert mgr.album_due(now=1600.0) is not None


def test_collection_report_is_truthful(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    client = FakeClient(collection=(200, _album_body()), blobs={DA: FRAME_A, DC: FRAME_C})
    mgr.sync_collection(client, "TOK")

    report = mgr.report()["collection"]
    assert report["id"] == "album:7"
    assert report["version"] == "v1"
    assert report["cached"] == 2
    assert report["total"] == 2
    assert report["state"] == "playing"


def test_partial_album_download_reports_the_real_cached_count(tmp_path: Path) -> None:
    """Reporting the manifest's count rather than what's on disk would tell
    the server the album is ready when it isn't."""
    mgr = _manager(tmp_path)
    client = FakeClient(collection=(200, _album_body()), blobs={DA: FRAME_A})
    mgr.sync_collection(client, "TOK")
    assert mgr.report()["collection"]["cached"] == 1


def test_collection_unbound_drops_playback(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    client = FakeClient(collection=(200, _album_body()), blobs={DA: FRAME_A, DC: FRAME_C})
    mgr.sync_collection(client, "TOK")
    mgr.note_status_response({})
    assert "collection" not in mgr.report()
    assert mgr.album_due(now=9999.0) is None


def test_disabled_manager_does_nothing(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, enabled=False)
    client = FakeClient(deck=(200, _deck_body()), blobs={DA: FRAME_A, DB: FRAME_B})
    mgr.sync_deck(client, "TOK")
    assert not mgr.has_deck
    assert client.downloads == []
    assert mgr.note_status_response({"deck": {"version": "v9"}}) == (False, False)
