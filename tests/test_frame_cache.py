"""Content-addressed frame cache. The rules that matter are the verification
ones: a cache that can serve a wrong frame is worse than no cache, because you
get yesterday's dashboard with no error anywhere.
"""
from __future__ import annotations

from pathlib import Path

from tesserae_pi_bin_client.frame_cache import (
    FrameCache,
    capacity_bytes,
    digest_of,
    digest_valid,
)


def _cache(tmp_path: Path) -> FrameCache:
    cache = FrameCache(tmp_path, "decks", "deck1")
    cache.ensure()
    return cache


def test_digest_is_truncated_sha256() -> None:
    import hashlib

    data = b"hello"
    assert digest_of(data) == hashlib.sha256(data).hexdigest()[:16]
    assert len(digest_of(data)) == 16


def test_digest_valid_requires_lowercase_hex_16() -> None:
    assert digest_valid("0123456789abcdef")
    assert not digest_valid("0123456789ABCDEF")  # uppercase
    assert not digest_valid("0123456789abcde")  # too short
    assert not digest_valid("0123456789abcdeg")  # not hex


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    data = b"frame-bytes"
    digest = digest_of(data)
    assert cache.write(digest, data)
    assert cache.read(digest) == data


def test_write_refuses_content_that_does_not_match_its_digest(tmp_path: Path) -> None:
    """A truncated or substituted download must never reach the cache."""
    cache = _cache(tmp_path)
    assert not cache.write(digest_of(b"expected"), b"something else")
    assert cache.have() == set()


def test_read_rejects_and_deletes_a_corrupted_file(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    data = b"frame-bytes"
    digest = digest_of(data)
    cache.write(digest, data)
    cache.path_for(digest).write_bytes(b"tampered-with-same-length")

    assert cache.read(digest) is None
    # Corrupt entries are dropped so the next sync re-fetches them.
    assert digest not in cache.have()


def test_read_rejects_a_length_mismatch_without_deleting(tmp_path: Path) -> None:
    """A short read is a miss, not corruption: the firmware hit this on real
    hardware after sleep/wake and deleting would thrash the cache."""
    cache = _cache(tmp_path)
    data = b"frame-bytes"
    digest = digest_of(data)
    cache.write(digest, data)

    assert cache.read(digest, expected_bytes=999) is None
    assert digest in cache.have()  # still there


def test_read_missing_is_none_not_an_error(tmp_path: Path) -> None:
    assert _cache(tmp_path).read("0123456789abcdef") is None


def test_have_ignores_foreign_files(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.write(digest_of(b"a"), b"a")
    (cache.directory / "notes.txt").write_text("hi")
    (cache.directory / "manifest.json").write_text("{}")
    assert cache.have() == {digest_of(b"a")}


def test_prune_removes_unreferenced(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    keep, drop = digest_of(b"keep"), digest_of(b"drop")
    cache.write(keep, b"keep")
    cache.write(drop, b"drop")

    assert cache.prune({keep}) == 1
    assert cache.have() == {keep}


def test_write_is_atomic_leaving_no_temp_files(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.write(digest_of(b"x"), b"x")
    leftovers = [p.name for p in cache.directory.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_namespace_with_a_colon_is_path_safe(tmp_path: Path) -> None:
    """Collection ids look like "album:3"."""
    cache = FrameCache(tmp_path, "collections", "album:3")
    assert cache.ensure()
    assert cache.directory.parent == tmp_path / "collections"


def test_namespace_cannot_traverse(tmp_path: Path) -> None:
    cache = FrameCache(tmp_path, "decks", "../../etc")
    cache.ensure()
    assert tmp_path in cache.directory.parents


def test_manifest_round_trip(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.write_manifest('{"deck_id":"d"}')
    assert cache.read_manifest() == '{"deck_id":"d"}'


def test_clear_drops_everything(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.write(digest_of(b"x"), b"x")
    cache.clear()
    assert not cache.directory.exists()


def test_capacity_reports_free_space(tmp_path: Path) -> None:
    assert capacity_bytes(tmp_path) > 0


def test_capacity_probes_up_to_an_existing_parent(tmp_path: Path) -> None:
    """The cache root may not exist yet on first run."""
    assert capacity_bytes(tmp_path / "not" / "yet" / "created") > 0
