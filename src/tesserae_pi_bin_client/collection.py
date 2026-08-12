"""Pure frame-cache collection logic — offline album playback.

The Pi half of `docs/dev/frame-cache.md`. A collection is a producer-neutral
list of pre-rendered frames; the first producer is the offline photo album.
Frames sync to the local cache and then play back locally, so the panel keeps
cycling photos while the server is down, restarting, or simply idle.

No I/O, so the manifest shape, the cache differ, the interval policy and the
shuffle bag are all testable without a server.

One deliberate divergence from the firmware: it retains at most 32 `cache:
true` frames because that is what fits in an ESP32's RAM and NVS budget. A Pi
has neither constraint, so `MAX_FRAMES` here is a sanity bound rather than a
hardware one — the server still decides what to mark cacheable via the
capacity we advertise.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any

from .frame_cache import digest_valid

# A sanity ceiling, not a hardware one. Well above any plausible album; a
# manifest beyond it is far more likely to be a bug than a real request.
MAX_FRAMES = 4096

PLAY_MODES = {"sequential", "shuffle"}
REPEAT_MODES = {"loop", "reshuffle", "once"}

# The server sends interval_s as a *request*; we clamp it. A photo frame that
# repaints every few seconds would burn through an e-ink panel's rated
# refreshes, and the panel itself takes many seconds to update.
INTERVAL_MIN_S = 60
INTERVAL_MAX_S = 24 * 60 * 60


@dataclass(frozen=True)
class CollectionFrame:
    frame_id: str
    position: int
    digest: str
    bytes: int
    url: str
    ttl_s: int = 0
    cache: bool = True


@dataclass(frozen=True)
class AlbumPlayback:
    mode: str = "sequential"
    interval_s: int = 1800
    repeat: str = "loop"


@dataclass(frozen=True)
class CollectionManifest:
    schema: int
    collection_id: str
    kind: str
    version: str
    total_frames: int
    frames: tuple[CollectionFrame, ...] = ()
    next_cursor: str = ""
    playback: AlbumPlayback = AlbumPlayback()

    def cached_frames(self) -> tuple[CollectionFrame, ...]:
        """Retained frames in playback order (producer-supplied position)."""
        return tuple(sorted((f for f in self.frames if f.cache), key=lambda f: f.position))

    def cacheable_digests(self) -> set[str]:
        return {f.digest for f in self.frames if f.cache}


class CollectionParseError(ValueError):
    """The manifest could not be trusted; playback stays on whatever we hold."""


def _require_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise CollectionParseError(f"collection manifest missing {key}")
    return value


def _playback_of(raw: Any) -> AlbumPlayback:
    producer = raw.get("producer") if isinstance(raw, dict) else None
    album = producer.get("album") if isinstance(producer, dict) else None
    playback = album.get("playback") if isinstance(album, dict) else None
    if not isinstance(playback, dict):
        return AlbumPlayback()
    mode = playback.get("mode")
    repeat = playback.get("repeat")
    interval = playback.get("interval_s")
    return AlbumPlayback(
        mode=mode if mode in PLAY_MODES else "sequential",
        interval_s=clamp_interval(interval if isinstance(interval, int) else 1800),
        repeat=repeat if repeat in REPEAT_MODES else "loop",
    )


def clamp_interval(seconds: int) -> int:
    return max(INTERVAL_MIN_S, min(INTERVAL_MAX_S, seconds))


def parse_manifest(body: str | bytes) -> CollectionManifest:
    """Parse a `GET /collection` 200 body.

    Strict for every `cache: true` frame — a bad digest or zero size there
    would mean caching something we can never verify. `cache: false` entries
    are counted toward `total_frames` but not retained, so they are allowed to
    be thinner.
    """
    try:
        raw = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise CollectionParseError(f"collection manifest is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CollectionParseError("collection manifest must be an object")

    schema = raw.get("schema")
    if schema != 1:
        raise CollectionParseError(f"unsupported collection schema: {schema!r}")
    collection_id = _require_str(raw, "collection_id")
    version = _require_str(raw, "version")

    raw_frames = raw.get("frames")
    if not isinstance(raw_frames, list):
        raise CollectionParseError("collection manifest has no frames list")
    if len(raw_frames) > MAX_FRAMES:
        raise CollectionParseError(
            f"collection has {len(raw_frames)} frames; cap is {MAX_FRAMES}"
        )

    frames: list[CollectionFrame] = []
    for item in raw_frames:
        if not isinstance(item, dict):
            raise CollectionParseError("collection frame must be an object")
        cache = item.get("cache", True)
        cache = cache if isinstance(cache, bool) else True
        frame_id = item.get("frame_id")
        digest = item.get("digest")
        size = item.get("bytes")
        if not isinstance(frame_id, str) or not frame_id:
            raise CollectionParseError("collection frame missing frame_id")
        if cache:
            if not isinstance(digest, str) or not digest_valid(digest):
                raise CollectionParseError(
                    f"frame {frame_id!r} has a bad digest: {digest!r}"
                )
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                raise CollectionParseError(f"frame {frame_id!r} has bytes={size!r}")
        position = item.get("position")
        position = position if isinstance(position, int) and not isinstance(position, bool) else 0
        ttl = item.get("ttl_s", 0)
        ttl = ttl if isinstance(ttl, int) and not isinstance(ttl, bool) else 0
        url = item.get("url")
        frames.append(
            CollectionFrame(
                frame_id=frame_id,
                position=position,
                digest=digest if isinstance(digest, str) else "",
                bytes=size if isinstance(size, int) else 0,
                url=url if isinstance(url, str) else "",
                ttl_s=ttl,
                cache=cache,
            )
        )

    total = raw.get("total_frames")
    next_cursor = raw.get("next_cursor")
    kind = raw.get("kind")
    return CollectionManifest(
        schema=1,
        collection_id=collection_id,
        kind=kind if isinstance(kind, str) and kind else "album",
        version=version,
        total_frames=total if isinstance(total, int) else len(frames),
        frames=tuple(frames),
        next_cursor=next_cursor if isinstance(next_cursor, str) else "",
        playback=_playback_of(raw),
    )


def sync_plan(
    manifest: CollectionManifest, have: set[str]
) -> tuple[list[str], list[str]]:
    """(digests to fetch, digests to delete) to make the cache match."""
    want = manifest.cacheable_digests()
    return sorted(want - have), sorted(have - want)


class AlbumPlayer:
    """Playback cursor over a collection's retained frames.

    Holds no frame data — it hands back the next frame's descriptor and lets
    the caller fetch it from the cache. Traversal state resets whenever the
    manifest version moves, since positions may have been reshuffled under it.
    """

    def __init__(self, manifest: CollectionManifest, rng: random.Random | None = None) -> None:
        self._rng = rng if rng is not None else random.Random()
        self._version = ""
        self._bag: list[int] = []
        self._index = 0
        self._exhausted = False
        self.adopt(manifest)

    def adopt(self, manifest: CollectionManifest) -> None:
        """Point at a manifest, resetting traversal if the version moved."""
        self._manifest = manifest
        if manifest.version != self._version:
            self._version = manifest.version
            self._index = 0
            self._exhausted = False
            self._refill()

    @property
    def exhausted(self) -> bool:
        """True when a `once` album has played through and is holding."""
        return self._exhausted

    def _frames(self) -> tuple[CollectionFrame, ...]:
        return self._manifest.cached_frames()

    def _refill(self) -> None:
        count = len(self._frames())
        self._bag = list(range(count))
        if self._manifest.playback.mode == "shuffle":
            self._rng.shuffle(self._bag)

    def next_frame(self) -> CollectionFrame | None:
        """The next frame to paint, or None when there is nothing to play.

        `once` holds on the final frame rather than looping, so the album
        stops advancing but the panel keeps showing its last photo.
        """
        frames = self._frames()
        if not frames:
            return None
        if self._exhausted:
            return None
        if self._index >= len(self._bag):
            repeat = self._manifest.playback.repeat
            if repeat == "once":
                self._exhausted = True
                return None
            if repeat == "reshuffle":
                self._refill()
            self._index = 0
            if not self._bag:
                return None
        slot = self._bag[self._index]
        self._index += 1
        if slot >= len(frames):
            # The manifest shrank under a bag built for the old size.
            self._refill()
            self._index = 0
            return None
        return frames[slot]


__all__ = [
    "INTERVAL_MAX_S",
    "INTERVAL_MIN_S",
    "MAX_FRAMES",
    "AlbumPlayback",
    "AlbumPlayer",
    "CollectionFrame",
    "CollectionManifest",
    "CollectionParseError",
    "clamp_interval",
    "parse_manifest",
    "sync_plan",
]
