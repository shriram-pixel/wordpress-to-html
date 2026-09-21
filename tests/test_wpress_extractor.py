"""Tests for ``.wpress`` reading, writing and safe extraction."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.wpress_extractor import (
    EOF_BLOCK,
    HEADER_SIZE,
    CorruptWpressArchive,
    InvalidWpressArchive,
    PurePythonWpressExtractor,
    WpressArchive,
    WpressWriter,
    build_header,
    get_extractor,
    parse_header,
)


# ---------------------------------------------------------------------------
# Header codec
# ---------------------------------------------------------------------------
def test_header_round_trips():
    block = build_header("style.css", 1234, 1700000000, "wp-content/themes/x")
    assert len(block) == HEADER_SIZE

    entry = parse_header(block, 0)
    assert entry is not None
    assert entry.name == "style.css"
    assert entry.size == 1234
    assert entry.mtime == 1700000000
    assert entry.prefix == "wp-content/themes/x"
    assert entry.offset == HEADER_SIZE


def test_eof_block_parses_as_none():
    assert parse_header(EOF_BLOCK, 0) is None


def test_short_header_is_rejected():
    with pytest.raises(CorruptWpressArchive):
        parse_header(b"\x00" * 100, 0)


def test_non_numeric_size_is_rejected():
    block = bytearray(build_header("a.txt", 1, 1, "."))
    block[255:269] = b"not-a-number\x00\x00"
    with pytest.raises(CorruptWpressArchive):
        parse_header(bytes(block), 0)


def test_unicode_filenames_use_byte_lengths():
    name = "café-ünïcode.png"
    entry = parse_header(build_header(name, 5, 1, "uploads/2026"), 0)
    assert entry.name == name


def test_oversized_name_is_rejected():
    with pytest.raises(ValueError):
        build_header("x" * 300, 1, 1, ".")


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_archive(tmp_path: Path) -> Path:
    archive = tmp_path / "sample.wpress"
    with WpressWriter(archive) as writer:
        writer.add_bytes("database.sql", b"-- dump\nCREATE TABLE wp_posts(id int);\n")
        writer.add_bytes("package.json", b'{"SiteURL":"https://example.com"}')
        writer.add_bytes("wp-content/uploads/2026/01/a.jpg", b"\xff\xd8\xff\xe0JPEG")
        writer.add_bytes("wp-content/themes/t/style.css", b"body{color:red}")
        writer.add_bytes("empty.txt", b"")
    return archive


def test_valid_archive_enumerates(sample_archive: Path):
    archive = WpressArchive(sample_archive)
    assert archive.looks_valid()
    assert archive.count_files() == 5

    paths = {str(e.safe_path) for e in archive.iter_entries()}
    assert "database.sql" in paths
    assert "wp-content/uploads/2026/01/a.jpg" in paths


def test_extraction_writes_exact_bytes(sample_archive: Path, tmp_path: Path):
    destination = tmp_path / "out"
    result = PurePythonWpressExtractor().extract(sample_archive, destination)

    assert result.files_written == 5
    assert result.entries_skipped == 0
    assert not result.truncated
    assert (destination / "wp-content" / "themes" / "t" / "style.css").read_bytes() == b"body{color:red}"
    assert (destination / "empty.txt").read_bytes() == b""


def test_progress_is_reported(sample_archive: Path, tmp_path: Path):
    seen: list[tuple[int, int]] = []
    PurePythonWpressExtractor().extract(
        sample_archive, tmp_path / "out",
        progress=lambda done, total, path: seen.append((done, total)),
    )
    assert seen, "no progress callbacks were made"
    assert seen[-1][0] <= seen[-1][1]


def test_read_member_returns_payload(sample_archive: Path):
    archive = WpressArchive(sample_archive)
    entry = archive.find(lambda e: e.name == "package.json")
    assert entry is not None
    assert b"SiteURL" in archive.read_member(entry)


# ---------------------------------------------------------------------------
# Security: the reference Go extractor is vulnerable to all of these
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "prefix,name",
    [
        ("../../../../etc", "passwd"),
        ("../../..", "evil.txt"),
        ("wp-content/../../..", "escape.txt"),
        ("/etc", "shadow"),
        ("C:\\Windows\\System32", "evil.dll"),
        ("\\\\server\\share", "unc.txt"),
    ],
)
def test_path_traversal_is_contained(tmp_path: Path, prefix: str, name: str):
    archive = tmp_path / "hostile.wpress"
    with archive.open("wb") as fh:
        payload = b"PWNED"
        fh.write(build_header(name, len(payload), 1700000000, prefix))
        fh.write(payload)
        fh.write(EOF_BLOCK)

    destination = tmp_path / "out"
    result = PurePythonWpressExtractor().extract(archive, destination)

    for path in destination.rglob("*"):
        if path.is_file():
            assert destination.resolve() in path.resolve().parents, f"{path} escaped"

    # Nothing was written next to, or above, the destination.
    assert not (tmp_path / name).exists()
    assert not (tmp_path.parent / name).exists()
    assert result.files_written >= 1


def test_windows_reserved_names_are_escaped(tmp_path: Path):
    archive = tmp_path / "reserved.wpress"
    with WpressWriter(archive) as writer:
        writer.add_bytes("wp-content/CON.php", b"x")
        writer.add_bytes("wp-content/aux.txt", b"y")

    destination = tmp_path / "out"
    PurePythonWpressExtractor().extract(archive, destination)

    written = {p.name for p in destination.rglob("*") if p.is_file()}
    assert "CON.php" not in written
    assert any(n.startswith("_") for n in written)


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------
def test_not_a_wpress_file_is_rejected(tmp_path: Path):
    bogus = tmp_path / "photo.wpress"
    bogus.write_bytes(os.urandom(HEADER_SIZE * 2))
    with pytest.raises(InvalidWpressArchive):
        PurePythonWpressExtractor().validate(bogus)


def test_tiny_file_is_rejected(tmp_path: Path):
    tiny = tmp_path / "tiny.wpress"
    tiny.write_bytes(b"nope")
    with pytest.raises(InvalidWpressArchive):
        WpressArchive(tiny)


def test_missing_file_is_rejected(tmp_path: Path):
    with pytest.raises(InvalidWpressArchive):
        WpressArchive(tmp_path / "absent.wpress")


def test_truncated_archive_keeps_what_it_recovered(sample_archive: Path, tmp_path: Path):
    """A backup cut short by a failed download should still yield its files."""
    data = sample_archive.read_bytes()
    truncated = tmp_path / "truncated.wpress"
    truncated.write_bytes(data[: len(data) - HEADER_SIZE - 10])

    result = PurePythonWpressExtractor().extract(truncated, tmp_path / "out")

    assert result.files_written >= 3
    assert result.truncated or result.warnings


def test_empty_archive_raises(tmp_path: Path):
    archive = tmp_path / "empty.wpress"
    archive.write_bytes(EOF_BLOCK)
    with pytest.raises((CorruptWpressArchive, InvalidWpressArchive)):
        PurePythonWpressExtractor().extract(archive, tmp_path / "out")


def test_archive_with_nothing_recoverable_fails_clearly(tmp_path: Path):
    """An incomplete download must say so, not produce an empty export."""
    archive = tmp_path / "liar.wpress"
    with archive.open("wb") as fh:
        fh.write(build_header("big.bin", 10_000_000, 1, "."))
        fh.write(b"only a few bytes")
        fh.write(EOF_BLOCK)

    destination = tmp_path / "out"
    with pytest.raises(CorruptWpressArchive, match="incomplete download"):
        PurePythonWpressExtractor().extract(archive, destination)

    # The half-written member must not be left behind looking complete.
    assert not (destination / "big.bin").exists()


def test_partial_trailing_member_is_discarded_but_earlier_files_kept(tmp_path: Path):
    archive = tmp_path / "cut.wpress"
    with archive.open("wb") as fh:
        for name, payload in (("a.txt", b"aaa"), ("b.txt", b"bbb")):
            fh.write(build_header(name, len(payload), 1, "."))
            fh.write(payload)
        fh.write(build_header("c.bin", 5_000_000, 1, "."))
        fh.write(b"short")

    destination = tmp_path / "out"
    result = PurePythonWpressExtractor().extract(archive, destination)

    assert result.files_written == 2
    assert result.truncated
    assert (destination / "a.txt").read_bytes() == b"aaa"
    assert not (destination / "c.bin").exists()


# ---------------------------------------------------------------------------
# Larger archive
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_many_files_and_a_large_member(tmp_path: Path):
    archive = tmp_path / "big.wpress"
    blob = os.urandom(5 * 1024 * 1024)

    with WpressWriter(archive) as writer:
        writer.add_bytes("wp-content/uploads/big.bin", blob)
        for index in range(400):
            writer.add_bytes(f"wp-content/uploads/batch/{index:04d}.txt", b"x" * 512)

    destination = tmp_path / "out"
    result = PurePythonWpressExtractor().extract(archive, destination)

    assert result.files_written == 401
    assert (destination / "wp-content/uploads/big.bin").read_bytes() == blob


def test_add_tree_round_trips(tmp_path: Path):
    source = tmp_path / "src"
    (source / "a" / "b").mkdir(parents=True)
    (source / "a" / "one.txt").write_bytes(b"one")
    (source / "a" / "b" / "two.txt").write_bytes(b"two")

    archive = tmp_path / "tree.wpress"
    with WpressWriter(archive) as writer:
        count = writer.add_tree(source, "wp-content")
    assert count == 2

    destination = tmp_path / "out"
    PurePythonWpressExtractor().extract(archive, destination)
    assert (destination / "wp-content/a/one.txt").read_bytes() == b"one"
    assert (destination / "wp-content/a/b/two.txt").read_bytes() == b"two"


# ---------------------------------------------------------------------------
# Backend abstraction
# ---------------------------------------------------------------------------
def test_factory_returns_the_dependency_free_backend():
    extractor = get_extractor("auto")
    assert isinstance(extractor, PurePythonWpressExtractor)
    assert extractor.is_available()


def test_factory_rejects_unknown_backend():
    with pytest.raises(ValueError):
        get_extractor("something-else")


def test_binary_backend_reports_unavailable_rather_than_crashing():
    from app.services.wpress_extractor import BinaryWpressExtractor

    backend = BinaryWpressExtractor(binary_path="/definitely/not/here")
    assert backend.is_available() is False
