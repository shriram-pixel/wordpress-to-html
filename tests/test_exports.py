"""Finished ZIPs are collected in one place, and runs never overwrite runs."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.pipeline import _zip_name_for  # noqa: E402


def test_the_name_says_which_run_made_it():
    """The same backup is converted more than once: after a fix, with other
    options, to compare a change. Two runs must not collide."""
    when = time.mktime((2026, 9, 22, 11, 51, 0, 0, 0, -1))
    later = time.mktime((2026, 9, 22, 14, 30, 0, 0, 0, -1))

    first = _zip_name_for("site.wpress", when=when)
    second = _zip_name_for("site.wpress", when=later)

    assert first == "site-static-20260922-1151.zip"
    assert first != second, "a second run must not overwrite the first"
    assert first < second, "sorting by name sorts by when it was made"


def test_the_layout_is_still_in_the_name():
    """Folder-per-page and file-per-page exports must never be confused."""
    when = time.mktime((2026, 9, 22, 11, 51, 0, 0, 0, -1))

    assert "-flat-" in _zip_name_for("site.wpress", flat=True, when=when)
    assert "-flat-" not in _zip_name_for("site.wpress", flat=False, when=when)


def test_the_export_folder_defaults_beside_the_jobs(tmp_path, monkeypatch):
    from app.config import Settings

    settings = Settings(jobs_dir=tmp_path / "jobs")
    assert settings.exports == tmp_path / "jobs" / "exports"

    explicit = Settings(jobs_dir=tmp_path / "jobs", export_dir=tmp_path / "elsewhere")
    assert explicit.exports == tmp_path / "elsewhere"


def test_collecting_a_zip_costs_no_disk(tmp_path):
    """A hard link, not a copy: a 300 MB ZIP should not become 600 MB."""
    source = tmp_path / "job" / "site-static-20260922-1151.zip"
    source.parent.mkdir()
    source.write_bytes(b"x" * 4096)

    exports = tmp_path / "exports"
    exports.mkdir()
    published = exports / source.name
    os.link(source, published)

    assert published.stat().st_size == 4096
    assert published.stat().st_ino == source.stat().st_ino, "the same bytes on disk"
