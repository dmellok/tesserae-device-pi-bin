"""Pure deck logic — manifest parsing, navigation, and the cache sync differ.

A "deck" is a small graph of pre-rendered pages the user navigates with the
front buttons. The server serves a manifest (`GET /api/v1/device/<id>/deck`)
describing pages, their frame digests, and the link graph; the frames live in
the local cache so a press paints without a server round trip.

No I/O here, so every rule below is testable without a server or a panel. A
direct port of the sibling firmware's `deck.c`, which is host-tested the same
way for the same reason.

Parse strictness is deliberate and asymmetric:

* A malformed **page** (bad digest, zero bytes) fails the whole manifest — the
  device then falls back to plain network fetches, which is correct behaviour,
  just slower.
* A malformed **link** is dropped individually. One bad edge should not cost
  you the entire deck's local navigation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .frame_cache import digest_valid


@dataclass(frozen=True)
class DeckLink:
    """One navigation edge. Either a button link or a zone link."""

    target_page_id: str
    button: str = ""
    zone: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class DeckPage:
    page_id: str
    digest: str
    bytes: int
    ttl_s: int = 0
    cache: bool = True
    links: tuple[DeckLink, ...] = ()


@dataclass(frozen=True)
class DeckManifest:
    deck_id: str
    version: str
    entry_page_id: str
    pages: tuple[DeckPage, ...] = ()
    home_page_id: str = ""
    home_timeout_s: int = 0

    _by_id: dict[str, DeckPage] = field(default_factory=dict, compare=False, repr=False)

    def page(self, page_id: str) -> DeckPage | None:
        return self._by_id.get(page_id)

    def cacheable_digests(self) -> set[str]:
        """Digests the server wants us to hold. Pages marked `cache: false`
        exceeded our advertised capacity and are fetched on demand."""
        return {p.digest for p in self.pages if p.cache and p.digest}


class DeckParseError(ValueError):
    """The manifest could not be trusted; fall back to the network path."""


def _require_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise DeckParseError(f"deck manifest missing {key}")
    return value


def _zone_of(raw: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, dict):
        return None
    try:
        zone = (
            float(raw["x"]),
            float(raw["y"]),
            float(raw["w"]),
            float(raw["h"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if any(not (0.0 <= v <= 1.0) for v in zone[:2]):
        return None
    if zone[2] <= 0.0 or zone[3] <= 0.0:
        return None
    return zone


def _links_of(raw: Any, known_pages: set[str]) -> tuple[DeckLink, ...]:
    if not isinstance(raw, list):
        return ()
    links: list[DeckLink] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        target = item.get("target_page_id")
        if not isinstance(target, str) or target not in known_pages:
            # An edge pointing nowhere is dropped, not fatal.
            continue
        button = item.get("button")
        button = button if isinstance(button, str) else ""
        zone = _zone_of(item.get("zone"))
        if not button and zone is None:
            continue
        links.append(DeckLink(target_page_id=target, button=button, zone=zone))
    return tuple(links)


def parse_manifest(body: str | bytes) -> DeckManifest:
    """Parse a `GET /deck` 200 body. Raises DeckParseError on anything
    we would not be able to navigate safely."""
    try:
        raw = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise DeckParseError(f"deck manifest is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise DeckParseError("deck manifest must be an object")

    deck_id = _require_str(raw, "deck_id")
    version = _require_str(raw, "version")
    entry = raw.get("entry_page_id")

    raw_pages = raw.get("pages")
    if not isinstance(raw_pages, list) or not raw_pages:
        raise DeckParseError("deck manifest has no pages")

    # Two passes: collect ids first so link targets can be validated against
    # the full set regardless of declaration order.
    ids: set[str] = set()
    for item in raw_pages:
        if isinstance(item, dict) and isinstance(item.get("page_id"), str):
            ids.add(item["page_id"])

    pages: list[DeckPage] = []
    for item in raw_pages:
        if not isinstance(item, dict):
            raise DeckParseError("deck page must be an object")
        page_id = item.get("page_id")
        digest = item.get("digest")
        size = item.get("bytes")
        if not isinstance(page_id, str) or not page_id:
            raise DeckParseError("deck page missing page_id")
        if not isinstance(digest, str) or not digest_valid(digest):
            raise DeckParseError(
                f"deck page {page_id!r} has a bad digest: {digest!r}"
            )
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise DeckParseError(f"deck page {page_id!r} has bytes={size!r}")
        ttl = item.get("ttl_s", 0)
        ttl = ttl if isinstance(ttl, int) and not isinstance(ttl, bool) else 0
        cache = item.get("cache", True)
        cache = cache if isinstance(cache, bool) else True
        pages.append(
            DeckPage(
                page_id=page_id,
                digest=digest,
                bytes=size,
                ttl_s=ttl,
                cache=cache,
                links=_links_of(item.get("links"), ids),
            )
        )

    entry_page_id = entry if isinstance(entry, str) and entry in ids else pages[0].page_id

    home = raw.get("home")
    home_page_id, home_timeout_s = "", 0
    if isinstance(home, dict):
        candidate = home.get("page_id")
        timeout = home.get("timeout_s")
        if isinstance(candidate, str) and candidate in ids:
            home_page_id = candidate
        if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
            home_timeout_s = timeout

    return DeckManifest(
        deck_id=deck_id,
        version=version,
        entry_page_id=entry_page_id,
        pages=tuple(pages),
        home_page_id=home_page_id,
        home_timeout_s=home_timeout_s,
        _by_id={p.page_id: p for p in pages},
    )


def nav_button(manifest: DeckManifest, page_id: str, button: str) -> str | None:
    """Target page for `button` pressed on `page_id`, or None.

    First matching link wins, mirroring the firmware, so an explicit graph
    edge declared before a synthesized default takes precedence.
    """
    page = manifest.page(page_id)
    if page is None:
        return None
    for link in page.links:
        if link.button == button:
            return link.target_page_id
    return None


def nav_zone(
    manifest: DeckManifest, page_id: str, x: int, y: int, panel_w: int, panel_h: int
) -> str | None:
    """Target page for a tap at pixel (x, y), or None.

    Zones are normalised 0..1 rects and the **pixel centre** decides: pixel x
    hits when zx <= (x + 0.5)/w < zx + zw. Left/top edges are inclusive and
    right/bottom exclusive, so abutting zones never both claim a boundary
    pixel — the rule the firmware uses, kept identical so a deck authored
    against one behaves the same on the other.
    """
    page = manifest.page(page_id)
    if page is None or panel_w <= 0 or panel_h <= 0:
        return None
    fx = (x + 0.5) / panel_w
    fy = (y + 0.5) / panel_h
    for link in page.links:
        if link.zone is None:
            continue
        zx, zy, zw, zh = link.zone
        if zx <= fx < zx + zw and zy <= fy < zy + zh:
            return link.target_page_id
    return None


def sync_plan(
    manifest: DeckManifest, have: set[str]
) -> tuple[list[str], list[str]]:
    """(digests to fetch, digests to delete) to make the cache match.

    Deleting is driven by the *cacheable* set, so a page the server demoted to
    `cache: false` because it exceeded our capacity is dropped from disk
    rather than silently retained forever.
    """
    want = manifest.cacheable_digests()
    fetch = sorted(want - have)
    orphan = sorted(have - want)
    return fetch, orphan


__all__ = [
    "DeckLink",
    "DeckManifest",
    "DeckPage",
    "DeckParseError",
    "nav_button",
    "nav_zone",
    "parse_manifest",
    "sync_plan",
]
