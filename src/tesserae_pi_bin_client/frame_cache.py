"""Content-addressed on-disk store for pre-rendered frames.

The sibling firmware caches deck pages and album frames on an SD card so a
button press paints without the radio. A Pi has no radio to save, but the same
cache buys two things that matter here: navigation with no server round trip,
and a panel that keeps working while the server is down or being restarted.

Frames are addressed by **the first 16 lowercase hex chars of sha256(bytes)**,
matching the digest the server puts in its manifests. Every cached frame is
verified — exact byte length *and* digest — before it is painted. A cache that
can serve a wrong frame is worse than no cache, because the failure is silent:
you get yesterday's dashboard with no error anywhere.

Layout mirrors the firmware's card:

    <root>/decks/<deck_id>/manifest.json
    <root>/decks/<deck_id>/<digest>.bin
    <root>/collections/<collection_id>/manifest.json
    <root>/collections/<collection_id>/<digest>.bin
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

DIGEST_HEX = 16
_DIGEST_RE = re.compile(r"^[0-9a-f]{16}$")

# Anything not matching [a-z0-9_.:-] is replaced, so a collection id like
# "album:3" becomes a safe single path segment and can never traverse.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")

DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "tesserae-pi-bin-client"


def digest_of(data: bytes) -> str:
    """The manifest's digest form: sha256, truncated to 16 lowercase hex."""
    return hashlib.sha256(data).hexdigest()[:DIGEST_HEX]


def digest_valid(value: str) -> bool:
    return bool(_DIGEST_RE.fullmatch(value))


def _safe_segment(value: str) -> str:
    cleaned = _UNSAFE.sub("_", value).strip(".")
    return cleaned or "_"


class FrameCache:
    """One namespace of cached frames (a deck, or a collection).

    Every failure here is non-fatal by design: a missing, short, or corrupt
    file returns None and the caller falls back to the network. The cache is
    an optimisation, never a requirement.
    """

    def __init__(self, root: Path, kind: str, namespace: str) -> None:
        self._dir = root / kind / _safe_segment(namespace)

    @property
    def directory(self) -> Path:
        return self._dir

    def ensure(self) -> bool:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            return True
        except OSError as exc:
            log.warning("frame cache: cannot create %s (%s)", self._dir, exc)
            return False

    def path_for(self, digest: str) -> Path:
        return self._dir / f"{digest}.bin"

    def have(self) -> set[str]:
        """Digests currently on disk. Files that don't look like a digest are
        ignored rather than deleted — this directory is the user's."""
        try:
            entries = list(self._dir.iterdir())
        except OSError:
            return set()
        found: set[str] = set()
        for entry in entries:
            if entry.suffix != ".bin":
                continue
            stem = entry.stem
            if digest_valid(stem):
                found.add(stem)
        return found

    def read(self, digest: str, expected_bytes: int | None = None) -> bytes | None:
        """Return the verified frame, or None to fall back to the network.

        Verification is length-then-digest. A short read — which the firmware
        hit on real hardware after a sleep/wake cycle — is treated as a miss,
        not as corruption worth deleting: the next attempt usually succeeds.
        """
        path = self.path_for(digest)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if expected_bytes is not None and len(data) != expected_bytes:
            log.warning(
                "frame cache: %s is %d bytes, manifest says %d; ignoring",
                path.name, len(data), expected_bytes,
            )
            return None
        actual = digest_of(data)
        if actual != digest:
            log.warning(
                "frame cache: %s hashes to %s; discarding corrupt entry",
                path.name, actual,
            )
            self.delete(digest)
            return None
        return data

    def write(self, digest: str, data: bytes) -> bool:
        """Store a frame, verifying it matches its claimed digest first.

        Writing is atomic (temp file + rename in the same directory) so a
        power cut mid-write leaves either the old file or none — never a
        truncated one that would later read as corrupt.
        """
        actual = digest_of(data)
        if actual != digest:
            log.warning(
                "frame cache: refusing to store %s — content hashes to %s",
                digest, actual,
            )
            return False
        if not self.ensure():
            return False
        target = self.path_for(digest)
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{digest}.", suffix=".tmp", dir=str(self._dir)
            )
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp_name, target)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            log.warning("frame cache: write of %s failed (%s)", digest, exc)
            return False
        return True

    def delete(self, digest: str) -> None:
        try:
            self.path_for(digest).unlink()
        except OSError:
            pass

    def prune(self, keep: set[str]) -> int:
        """Delete cached frames no longer referenced. Returns the count."""
        removed = 0
        for digest in self.have() - keep:
            self.delete(digest)
            removed += 1
        return removed

    def read_manifest(self) -> str | None:
        try:
            return (self._dir / "manifest.json").read_text(encoding="utf-8")
        except OSError:
            return None

    def write_manifest(self, body: str) -> None:
        if not self.ensure():
            return
        try:
            (self._dir / "manifest.json").write_text(body, encoding="utf-8")
        except OSError as exc:
            log.warning("frame cache: manifest write failed (%s)", exc)

    def clear(self) -> None:
        try:
            shutil.rmtree(self._dir)
        except OSError:
            pass


def capacity_bytes(root: Path) -> int:
    """Free space available to the cache, for the capability advertisement.

    The server uses this to mark overflow pages `cache: false` rather than
    letting us discover mid-sync that the set doesn't fit. Reported as free
    space on the filesystem holding the cache; 0 means "no usable storage",
    which omits the capability entirely and reverts the wire body to a
    cacheless client's.
    """
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return 0
    return int(usage.free)


__all__ = [
    "DEFAULT_CACHE_ROOT",
    "DIGEST_HEX",
    "FrameCache",
    "capacity_bytes",
    "digest_of",
    "digest_valid",
]
