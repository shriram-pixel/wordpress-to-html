"""Tests for assembling the WordPress tree, and for the disk it consumes.

The move-vs-copy behaviour has no visible effect on the exported site, which is
exactly why it needs tests: a regression here would silently double the disk a
conversion needs, and only show up as a failure on somebody's large backup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.wordpress_restorer import (
    ArchiveLayout,
    _copy_tree,
    _move_tree,
    build_wordpress_tree,
    detect_table_prefix,
    inspect_archive,
)


def _usage(path: Path) -> int:
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())


@pytest.fixture
def fake_core(tmp_path: Path) -> Path:
    """A minimal stand-in for an unpacked WordPress core."""
    core = tmp_path / "core"
    (core / "wp-admin").mkdir(parents=True)
    (core / "wp-includes").mkdir(parents=True)
    (core / "wp-content" / "themes" / "twentytwentyfive").mkdir(parents=True)
    (core / "wp-content" / "plugins").mkdir(parents=True)

    (core / "index.php").write_text("<?php // core", encoding="utf-8")
    (core / "wp-includes" / "version.php").write_text(
        "<?php $wp_version = '7.1';", encoding="utf-8"
    )
    (core / "wp-content" / "themes" / "twentytwentyfive" / "style.css").write_text(
        "/* bundled theme */", encoding="utf-8"
    )
    return core


@pytest.fixture
def fake_extracted(tmp_path: Path) -> Path:
    """A stand-in for an extracted .wpress: database.sql plus wp-content."""
    extracted = tmp_path / "extracted"
    uploads = extracted / "wp-content" / "uploads" / "2026" / "01"
    uploads.mkdir(parents=True)
    (extracted / "wp-content" / "themes" / "custom").mkdir(parents=True)
    (extracted / "wp-content" / "plugins" / "demo").mkdir(parents=True)

    (extracted / "database.sql").write_text(
        "CREATE TABLE `nw_options` (id int);\nCREATE TABLE `nw_posts` (id int);\n",
        encoding="utf-8",
    )
    (extracted / "package.json").write_text('{"SiteURL":"https://example.com"}', encoding="utf-8")
    # A megabyte of "media", so duplication is measurable.
    (uploads / "big.bin").write_bytes(b"\0" * (1024 * 1024))
    (extracted / "wp-content" / "themes" / "custom" / "style.css").write_text(
        "/* the site's own theme */", encoding="utf-8"
    )
    (extracted / "wp-content" / "plugins" / "demo" / "demo.php").write_text(
        "<?php // plugin", encoding="utf-8"
    )
    return extracted


# ---------------------------------------------------------------------------
# _move_tree
# ---------------------------------------------------------------------------
def test_move_tree_merges_into_an_existing_directory(tmp_path: Path):
    source = tmp_path / "src"
    (source / "themes" / "custom").mkdir(parents=True)
    (source / "themes" / "custom" / "a.css").write_text("a", encoding="utf-8")
    (source / "uploads").mkdir()
    (source / "uploads" / "img.png").write_bytes(b"png")

    destination = tmp_path / "dst"
    (destination / "themes" / "bundled").mkdir(parents=True)
    (destination / "themes" / "bundled" / "b.css").write_text("b", encoding="utf-8")

    _move_tree(source, destination)

    # The archive's content arrived...
    assert (destination / "themes" / "custom" / "a.css").read_text() == "a"
    assert (destination / "uploads" / "img.png").read_bytes() == b"png"
    # ...without displacing what was already there.
    assert (destination / "themes" / "bundled" / "b.css").read_text() == "b"
    # ...and the source is gone, which is the point.
    assert not (source / "uploads" / "img.png").exists()


def test_move_tree_overwrites_a_conflicting_file(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "style.css").write_text("from the archive", encoding="utf-8")

    destination = tmp_path / "dst"
    destination.mkdir()
    (destination / "style.css").write_text("from core", encoding="utf-8")

    _move_tree(source, destination)
    assert (destination / "style.css").read_text() == "from the archive"


def test_move_tree_refuses_to_escape_the_destination(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "ok.txt").write_text("fine", encoding="utf-8")

    destination = tmp_path / "dst"
    _move_tree(source, destination)

    assert (destination / "ok.txt").is_file()
    for path in destination.rglob("*"):
        assert destination.resolve() in path.resolve().parents


def test_move_tree_tolerates_a_missing_source(tmp_path: Path):
    _move_tree(tmp_path / "nothing-here", tmp_path / "dst")  # must not raise


# ---------------------------------------------------------------------------
# build_wordpress_tree
# ---------------------------------------------------------------------------
def test_copy_mode_leaves_the_extracted_tree_intact(fake_core: Path, fake_extracted: Path,
                                                    tmp_path: Path):
    layout = inspect_archive(fake_extracted)
    destination = tmp_path / "wordpress"

    build_wordpress_tree(layout, destination, fake_core, consume_archive=False)

    assert (fake_extracted / "wp-content" / "uploads" / "2026" / "01" / "big.bin").is_file()
    assert (destination / "wp-content" / "uploads" / "2026" / "01" / "big.bin").is_file()


def test_move_mode_does_not_duplicate_the_media(fake_core: Path, fake_extracted: Path,
                                                tmp_path: Path):
    """The saving that makes large backups viable."""
    layout = inspect_archive(fake_extracted)
    destination = tmp_path / "wordpress"

    build_wordpress_tree(layout, destination, fake_core, consume_archive=True)

    # The media exists exactly once, in the install tree.
    assert (destination / "wp-content" / "uploads" / "2026" / "01" / "big.bin").is_file()
    assert not (fake_extracted / "wp-content" / "uploads" / "2026" / "01" / "big.bin").exists()

    # What remains of the extracted tree is only the metadata the restore
    # still needs; the megabyte of media is not counted twice.
    assert _usage(fake_extracted) < 100 * 1024
    assert (fake_extracted / "database.sql").is_file()


@pytest.mark.parametrize("consume", [False, True])
def test_the_assembled_tree_is_correct_either_way(fake_core: Path, fake_extracted: Path,
                                                  tmp_path: Path, consume: bool):
    layout = inspect_archive(fake_extracted)
    destination = tmp_path / "wordpress"

    build_wordpress_tree(layout, destination, fake_core, consume_archive=consume)

    assert (destination / "index.php").is_file()
    assert (destination / "wp-includes" / "version.php").is_file()

    themes = {p.name for p in (destination / "wp-content" / "themes").iterdir() if p.is_dir()}
    assert "custom" in themes, "the site's own theme is missing"
    assert "twentytwentyfive" in themes, "core's bundled theme was displaced"

    assert (destination / "wp-content" / "plugins" / "demo" / "demo.php").is_file()
    assert (destination / "wp-content" / "uploads" / "2026" / "01" / "big.bin").is_file()


def test_a_stale_wp_config_is_set_aside(fake_core: Path, fake_extracted: Path, tmp_path: Path):
    """The backup's wp-config names credentials that do not exist here."""
    (fake_extracted / "wp-content").mkdir(exist_ok=True)
    layout = inspect_archive(fake_extracted)
    destination = tmp_path / "wordpress"
    destination.mkdir()
    (destination / "wp-config.php").write_text("<?php // stale", encoding="utf-8")

    build_wordpress_tree(layout, destination, fake_core, consume_archive=True)

    assert not (destination / "wp-config.php").is_file()
    assert (destination / "wp-config.php.original").is_file()


# ---------------------------------------------------------------------------
# Layout detection
# ---------------------------------------------------------------------------
def test_layout_is_detected(fake_extracted: Path):
    layout = inspect_archive(fake_extracted)
    assert layout.sql_dump is not None and layout.sql_dump.name == "database.sql"
    assert layout.package_json is not None
    assert layout.wp_content is not None and layout.wp_content.name == "wp-content"
    assert layout.has_core is False
    assert layout.site_url == "https://example.com"
    assert layout.warnings == []


def test_missing_database_is_reported(tmp_path: Path):
    extracted = tmp_path / "extracted"
    (extracted / "wp-content").mkdir(parents=True)
    layout = inspect_archive(extracted)
    assert layout.sql_dump is None
    assert any("database" in w.lower() for w in layout.warnings)


@pytest.fixture
def flat_extracted(tmp_path: Path) -> Path:
    """A backup with no ``wp-content`` wrapper.

    Real exports do this: uploads/, plugins/ and themes/ sit at the top level
    alongside database.sql. It is the shape that exposed the bug this test
    guards -- the metadata would be moved into the install before the database
    had been imported from it.
    """
    extracted = tmp_path / "extracted"
    for directory in ("uploads/2026/01", "plugins/wordfence", "themes/mytheme",
                      "wflogs", "mu-plugins"):
        (extracted / directory).mkdir(parents=True)
    (extracted / "database.sql").write_text(
        "CREATE TABLE `wp_options` (id int);", encoding="utf-8"
    )
    (extracted / "package.json").write_text("{}", encoding="utf-8")
    (extracted / "uploads" / "2026" / "01" / "a.jpg").write_bytes(b"x" * 2048)
    (extracted / "themes" / "mytheme" / "style.css").write_text("body{}", encoding="utf-8")
    return extracted


def test_wrapperless_archive_resolves_to_the_root(flat_extracted: Path):
    layout = inspect_archive(flat_extracted)
    assert layout.wp_content == flat_extracted
    assert layout.has_core is False, "probing the job workspace would give a wrong answer"
    assert layout.sql_dump is not None


def test_wrapperless_archive_keeps_its_database_out_of_the_install(
    fake_core: Path, flat_extracted: Path, tmp_path: Path
):
    """The dump must still be where the restore expects it after the move."""
    layout = inspect_archive(flat_extracted)
    destination = tmp_path / "wordpress"

    build_wordpress_tree(layout, destination, fake_core, consume_archive=True)

    # Still importable.
    assert layout.sql_dump.is_file()
    # And not dragged into the served tree, where it would also be a data leak.
    assert not (destination / "wp-content" / "database.sql").exists()
    assert not (destination / "wp-content" / "package.json").exists()

    # The actual content did move across.
    assert (destination / "wp-content" / "uploads" / "2026" / "01" / "a.jpg").is_file()
    assert (destination / "wp-content" / "themes" / "mytheme" / "style.css").is_file()
    assert (destination / "wp-content" / "themes" / "twentytwentyfive").is_dir()


def test_archive_carrying_core_is_recognised(tmp_path: Path):
    extracted = tmp_path / "extracted"
    (extracted / "wp-includes").mkdir(parents=True)
    (extracted / "wp-content").mkdir(parents=True)
    (extracted / "wp-includes" / "version.php").write_text(
        "<?php $wp_version = '6.9';", encoding="utf-8"
    )
    assert inspect_archive(extracted).has_core is True


@pytest.mark.parametrize(
    "sql,expected",
    [
        (b"CREATE TABLE `wp_options` (id int);", "wp_"),
        (b"CREATE TABLE `nw_posts` (id int);\nCREATE TABLE `nw_options` (x int);", "nw_"),
        (b"CREATE TABLE IF NOT EXISTS `xyz123_users` (id int);", "xyz123_"),
        (b"-- nothing useful here", "wp_"),
    ],
)
def test_table_prefix_detection(tmp_path: Path, sql: bytes, expected: str):
    dump = tmp_path / "database.sql"
    dump.write_bytes(sql)
    assert detect_table_prefix(dump) == expected
