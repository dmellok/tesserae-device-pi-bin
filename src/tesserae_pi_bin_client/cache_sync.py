"""Deck-cache and offline-album orchestration on top of the local frame cache.

Ties together the pure logic (deck.py, collection.py) and the disk store
(frame_cache.py) with the wake loop's HTTP client. Both features are
capability-gated server-side: advertise `deck_cache` / `frame_cache` in a
heartbeat and the server starts offering manifests; stop advertising and it
stops, with the wire body reverting to a cacheless client's byte for byte.

Two rules from the contract are easy to get wrong and are called out where
they're enforced below:

* A locally-served button press is **not** reported as a button action — the
  server never saw a request, so telling it "the user pressed left" would
  double-count against the press we already resolved on-device.
* A one-off network frame interrupts album playback until the album's next
  scheduled deadline, but must not keep *postponing* that deadline — otherwise
  a chatty dashboard starves the album forever.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Protocol

from .collection import (
    AlbumPlayer,
    CollectionFrame,
    CollectionManifest,
    CollectionParseError,
)
from .collection import parse_manifest as parse_collection
from .collection import sync_plan as collection_sync_plan
from .deck import DeckManifest, DeckParseError, nav_button
from .deck import parse_manifest as parse_deck
from .deck import sync_plan as deck_sync_plan
from .frame_cache import FrameCache, capacity_bytes

log = logging.getLogger(__name__)

CACHE_SCHEMA = 1


class ManifestClient(Protocol):
    """The slice of RestClient this module needs."""

    def get_deck_manifest(self, device_id: str, token: str) -> tuple[int, str]: ...

    def get_collection_manifest(
        self, device_id: str, token: str
    ) -> tuple[int, str]: ...

    def deck_frame_url(self, device_id: str, digest: str) -> str: ...

    def absolute(self, url: str) -> str: ...

    def download_authed(
        self, url: str, token: str, max_bytes: int, timeout_s: float
    ) -> bytes: ...


class CacheManager:
    """Holds the bound deck + collection and their local caches."""

    def __init__(
        self,
        root: Path,
        device_id: str,
        max_frame_bytes: int,
        download_timeout_s: float,
        enabled: bool = True,
    ) -> None:
        self._root = root
        self._device_id = device_id
        self._max_frame_bytes = max_frame_bytes
        self._timeout = download_timeout_s
        self._enabled = enabled

        self._deck: DeckManifest | None = None
        self._deck_cache: FrameCache | None = None
        self._deck_version_seen = ""
        self._current_page_id = ""
        # Set when a page was served from disk, reported on the next status
        # (and same-cycle frame GET) so the server's nav state stays truthful.
        self._painted_page_id = ""

        self._collection: CollectionManifest | None = None
        self._collection_cache: FrameCache | None = None
        self._collection_version_seen = ""
        self._player: AlbumPlayer | None = None
        self._album_deadline = 0.0
        self._album_state = "idle"

    # -- capability advertisement ----------------------------------------

    def capabilities(self) -> dict[str, Any]:
        """Capability blocks for the register / status body.

        Omitted entirely when there is no usable storage, which is what makes
        a disabled cache byte-identical on the wire to a client that never had
        one.
        """
        if not self._enabled:
            return {}
        free = capacity_bytes(self._root)
        if free <= 0:
            return {}
        return {
            "deck_cache": {"schema": CACHE_SCHEMA, "capacity_bytes": free},
            "frame_cache": {"schema": CACHE_SCHEMA, "capacity_bytes": free},
        }

    def report(self) -> dict[str, Any]:
        """Truthful cache state for the status body."""
        out: dict[str, Any] = {}
        if self._painted_page_id and self._deck is not None:
            out["deck_page_id"] = self._painted_page_id
            out["deck_version"] = self._deck.version
        if self._collection is not None:
            cache = self._collection_cache
            cached = len(cache.have()) if cache is not None else 0
            out["collection"] = {
                "id": self._collection.collection_id,
                "version": self._collection.version,
                "cached": cached,
                "total": self._collection.total_frames,
                "state": self._album_state,
            }
        return out

    def clear_paint_report(self) -> None:
        """Called once the report has been delivered — it is per-cycle."""
        self._painted_page_id = ""

    # -- server-driven resync --------------------------------------------

    def note_status_response(self, body: dict[str, Any] | None) -> tuple[bool, bool]:
        """Read the status response's version announcements.

        Returns (deck_changed, collection_changed). A version that moved means
        resync at the tail of this cycle; an absent block means the binding
        was removed and the local copy should be dropped.
        """
        if not self._enabled or not isinstance(body, dict):
            return False, False

        deck_changed = False
        deck = body.get("deck")
        if isinstance(deck, dict):
            version = deck.get("version")
            if isinstance(version, str) and version != self._deck_version_seen:
                deck_changed = True
        elif self._deck is not None:
            log.info("deck unbound server-side; dropping local deck cache")
            self._drop_deck()

        collection_changed = False
        collection = body.get("collection")
        if isinstance(collection, dict):
            version = collection.get("version")
            if isinstance(version, str) and version != self._collection_version_seen:
                collection_changed = True
        elif self._collection is not None:
            log.info("collection unbound server-side; dropping local album cache")
            self._drop_collection()

        return deck_changed, collection_changed

    def _drop_deck(self) -> None:
        if self._deck_cache is not None:
            self._deck_cache.clear()
        self._deck = None
        self._deck_cache = None
        self._deck_version_seen = ""
        self._current_page_id = ""

    def _drop_collection(self) -> None:
        if self._collection_cache is not None:
            self._collection_cache.clear()
        self._collection = None
        self._collection_cache = None
        self._collection_version_seen = ""
        self._player = None
        self._album_state = "idle"

    # -- sync -------------------------------------------------------------

    def sync_deck(self, client: ManifestClient, token: str) -> None:
        """Fetch the manifest, fill missing digests, delete orphans.

        Every failure path leaves the previous cache intact and returns — the
        cache is an optimisation, so a bad sync must degrade to "keep using
        the network", never to "lose local navigation".
        """
        if not self._enabled:
            return
        try:
            code, body = client.get_deck_manifest(self._device_id, token)
        except Exception as exc:  # noqa: BLE001 - never fatal
            log.warning("deck manifest fetch failed (%s)", exc)
            return
        if code == 204:
            if self._deck is not None:
                self._drop_deck()
            return
        if code != 200 or not body:
            log.warning("deck manifest returned %d", code)
            return
        try:
            manifest = parse_deck(body)
        except DeckParseError as exc:
            log.warning("deck manifest rejected (%s); staying on the network path", exc)
            return

        cache = FrameCache(self._root, "decks", manifest.deck_id)
        if not cache.ensure():
            return
        fetch, orphan = deck_sync_plan(manifest, cache.have())
        by_digest = {p.digest: p for p in manifest.pages}
        fetched = 0
        for digest in fetch:
            page = by_digest.get(digest)
            if page is None:
                continue
            url = client.deck_frame_url(self._device_id, digest)
            if self._fetch_into(cache, client, url, token, digest):
                fetched += 1
        for digest in orphan:
            cache.delete(digest)
        cache.write_manifest(body)

        self._deck = manifest
        self._deck_cache = cache
        self._deck_version_seen = manifest.version
        if not self._current_page_id:
            self._current_page_id = manifest.entry_page_id
        log.info(
            "deck synced: %s v%s (%d page(s), %d fetched, %d pruned)",
            manifest.deck_id, manifest.version, len(manifest.pages), fetched, len(orphan),
        )

    def sync_collection(self, client: ManifestClient, token: str) -> None:
        if not self._enabled:
            return
        try:
            code, body = client.get_collection_manifest(self._device_id, token)
        except Exception as exc:  # noqa: BLE001 - never fatal
            log.warning("collection manifest fetch failed (%s)", exc)
            return
        if code == 204:
            if self._collection is not None:
                self._drop_collection()
            return
        if code != 200 or not body:
            log.warning("collection manifest returned %d", code)
            return
        try:
            manifest = parse_collection(body)
        except CollectionParseError as exc:
            log.warning("collection manifest rejected (%s)", exc)
            return

        cache = FrameCache(self._root, "collections", manifest.collection_id)
        if not cache.ensure():
            return
        fetch, orphan = collection_sync_plan(manifest, cache.have())
        by_digest = {f.digest: f for f in manifest.frames if f.cache}
        fetched = 0
        for digest in fetch:
            frame = by_digest.get(digest)
            if frame is None or not frame.url:
                continue
            if self._fetch_into(
                cache, client, client.absolute(frame.url), token, digest
            ):
                fetched += 1
        for digest in orphan:
            cache.delete(digest)
        cache.write_manifest(body)

        self._collection = manifest
        self._collection_cache = cache
        self._collection_version_seen = manifest.version
        if self._player is None:
            self._player = AlbumPlayer(manifest)
        else:
            self._player.adopt(manifest)
        self._album_state = "playing"
        log.info(
            "collection synced: %s v%s (%d/%d cached, %d fetched, %d pruned)",
            manifest.collection_id, manifest.version,
            len(cache.have()), manifest.total_frames, fetched, len(orphan),
        )

    def _fetch_into(
        self,
        cache: FrameCache,
        client: ManifestClient,
        url: str,
        token: str,
        digest: str,
    ) -> bool:
        try:
            data = client.download_authed(
                url, token, self._max_frame_bytes, self._timeout
            )
        except Exception as exc:  # noqa: BLE001 - one bad frame is not fatal
            log.warning("cache fetch of %s failed (%s)", digest, exc)
            return False
        # write() re-hashes and refuses a mismatch, so a truncated or
        # substituted download never reaches the cache.
        return cache.write(digest, data)

    # -- local serving ----------------------------------------------------

    def local_nav(self, button: str, expected_bytes: int) -> bytes | None:
        """Serve a button press from the deck cache, or None to go to network.

        Returning bytes here means the press was resolved entirely on-device.
        The caller must NOT then report it as a button action: the server saw
        no request, so reporting would double-count the press.
        """
        if not self._enabled or self._deck is None or self._deck_cache is None:
            return None
        target = nav_button(self._deck, self._current_page_id, button)
        if target is None:
            return None
        page = self._deck.page(target)
        if page is None or not page.cache:
            return None
        data = self._deck_cache.read(page.digest, expected_bytes=page.bytes)
        if data is None:
            return None
        if len(data) != expected_bytes:
            log.warning(
                "deck page %s is %d bytes; panel needs %d — falling back",
                target, len(data), expected_bytes,
            )
            return None
        self._current_page_id = target
        self._painted_page_id = target
        log.info("deck: served page %s locally (no server round trip)", target)
        return data

    # -- album playback ---------------------------------------------------

    def album_due(self, now: float | None = None) -> CollectionFrame | None:
        """The next album frame if its interval has elapsed, else None."""
        if not self._enabled or self._player is None or self._collection is None:
            return None
        now = time.time() if now is None else now
        if now < self._album_deadline:
            return None
        frame = self._player.next_frame()
        if frame is None:
            if self._player.exhausted:
                self._album_state = "held"
            return None
        self._album_deadline = now + self._collection.playback.interval_s
        return frame

    def album_frame_bytes(
        self, frame: CollectionFrame, expected_bytes: int
    ) -> bytes | None:
        if self._collection_cache is None:
            return None
        data = self._collection_cache.read(frame.digest, expected_bytes=frame.bytes)
        if data is None or len(data) != expected_bytes:
            return None
        return data

    def note_network_frame(self, now: float | None = None) -> None:
        """A one-off network frame just painted over the album.

        The album resumes at its **existing** deadline rather than a fresh
        interval from now, so repeated dashboard frames cannot keep pushing
        playback out indefinitely. Only an album frame moves the deadline.
        """
        if self._player is None or self._collection is None:
            return
        now = time.time() if now is None else now
        if self._album_deadline <= now:
            self._album_deadline = now + self._collection.playback.interval_s
        self._album_state = "interrupted"

    @property
    def album_interval_s(self) -> int | None:
        if self._collection is None:
            return None
        return self._collection.playback.interval_s

    @property
    def current_page_id(self) -> str:
        return self._current_page_id

    @property
    def has_deck(self) -> bool:
        return self._deck is not None


__all__ = ["CACHE_SCHEMA", "CacheManager", "ManifestClient"]
