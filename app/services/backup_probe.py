"""Read what a ``.wpress`` backup contains, without extracting it.

The time calculator needs a page count, and the one thing someone planning a
batch does not know is how many pages their sites have -- they have a backup
file and nothing else. Extracting a 3 GB archive to find out costs five
minutes and 9 GB of disk, which is absurd for a number used to fill in a form.

So this reads the archive in place. A ``.wpress`` is a flat sequence of
4377-byte headers each followed by its file's bytes, uncompressed, so the
whole archive can be stepped through by seeking from header to header -- the
content is never read. Only ``database.sql`` is, and only its ``posts`` rows.

Counting those rows is not the same as counting the pages a conversion will
render: a crawl also finds category and paged archive URLs, and it never
reaches a published page that nothing links to. Measured against two real
conversions the count came within about a tenth, which is the right accuracy
for an estimate and is why this returns a number the interface labels as one.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

HEADER_SIZE = 4377
_NAME, _SIZE = 255, 14
#: The header also records the folder the file came from, which is what
#: separates the archive's own package.json from the npm manifest that
#: some plugin ships. Reading the wrong one overwrites real metadata with
#: blanks -- the same trap as picking a cache plugin's copy of wp-content.
_PREFIX_AT, _PREFIX_END = 281, 4377
_ROOT = ("", ".", "./")
_CHUNK = 8 * 1024 * 1024

_POSTS = re.compile(rb"^INSERT INTO `[A-Za-z0-9_]*posts` VALUES \(", re.I)

#: A posts row ends with menu_order, post_type, post_mime_type, comment_count.
#: However wild the content columns are -- and they hold entire page builders'
#: JSON -- those last three are a short string, a short string and a number,
#: so the tail can be read even though the middle cannot be parsed at all.
#: Some dumps quote every value, including numbers, hence the optional quotes.
_TAIL = re.compile(rb",'([a-z0-9_\-]{1,40})','[^']*','?\d+'?\);?\s*$")

_PUBLISHED = b",'publish',"

#: Post types that never become a page of their own on the exported site.
SKIPPED_TYPES = frozenset({
    "attachment", "revision", "nav_menu_item", "custom_css", "customize_changeset",
    "elementor_library", "wp_global_styles", "wp_template", "wp_template_part",
    "wp_navigation", "wp_font_family", "wp_font_face", "oembed_cache", "user_request",
    "scheduled-action", "acf-field", "acf-field-group", "wpcf7_contact_form",
    "e-landing-page", "product_variation", "shop_order", "shop_coupon",
    "wp_block", "popup_theme", "custom-css-js",
})

#: Chunks without a single posts row after the posts rows have started. They
#: are written together, so a gap this long means the table is behind us --
#: which on a 1.9 GB dump saves reading almost all of it.
_QUIET_CHUNKS = 6


@dataclass
class BackupFacts:
    """What could be learned about a backup without unpacking it."""

    path: str
    size_bytes: int
    pages: int = 0
    """Published content that would become a page. An estimate, not a promise."""
    by_type: dict[str, int] = field(default_factory=dict)
    site_url: str = ""
    wordpress_version: str = ""
    theme: str = ""
    plugins: int = 0
    database_bytes: int = 0
    files: int = 0
    read_bytes: int = 0
    """How much of the archive had to be read, for the log."""
    complete: bool = True
    """False when the scan stopped early or found no database."""
    note: str = ""


def _headers(handle) -> tuple[str, int, str] | None:
    """``(name, size, folder)`` for the next entry, or None at the end."""
    head = handle.read(HEADER_SIZE)
    if len(head) < HEADER_SIZE or head[:_NAME].strip(b"\0") == b"":
        return None
    name = head[:_NAME].rstrip(b"\0").decode("utf-8", "replace")
    raw = head[_NAME:_NAME + _SIZE].rstrip(b"\0")
    folder = head[_PREFIX_AT:_PREFIX_END].rstrip(b"\0").decode("utf-8", "replace")
    try:
        size = int(raw or 0)
    except ValueError:
        return None
    return name, size, folder


def probe(archive: Path, *, on_progress: Callable[[str], None] | None = None) -> BackupFacts:
    """Read a backup's metadata and estimate how many pages it holds."""
    archive = Path(archive)
    facts = BackupFacts(path=str(archive), size_bytes=archive.stat().st_size)

    def say(message: str) -> None:
        logger.debug("%s: %s", archive.name, message)
        if on_progress:
            on_progress(message)

    with archive.open("rb") as handle:
        while True:
            entry = _headers(handle)
            if entry is None:
                break
            name, size, folder = entry
            facts.files += 1

            # Only the archive's own metadata, at its root -- not a plugin's
            # npm manifest of the same name, which would blank what we read.
            if (name == "package.json" and folder in _ROOT
                    and not facts.site_url and size < 4 * 1024 * 1024):
                _read_package(handle.read(size), facts)
                continue

            if name == "database.sql" and folder in _ROOT:
                facts.database_bytes = size
                say(f"reading the database ({size / 1048576:.0f} MB)")
                _count_posts(handle, size, facts)
                break

            handle.seek(size, 1)

    if not facts.database_bytes:
        facts.complete = False
        facts.note = "no database.sql in the archive"
    elif not facts.pages:
        facts.complete = False
        facts.note = "no published content found in the database"
    return facts


def _read_package(raw: bytes, facts: BackupFacts) -> None:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return
    facts.site_url = str(data.get("SiteURL") or data.get("HomeURL") or "")
    facts.wordpress_version = str((data.get("WordPress") or {}).get("Version") or "")
    facts.theme = str(data.get("Stylesheet") or data.get("Template") or "")
    plugins = data.get("Plugins")
    facts.plugins = len(plugins) if isinstance(plugins, list) else 0


def _count_posts(handle, size: int, facts: BackupFacts) -> None:
    published: dict[str, int] = {}
    buffer, left = b"", size
    started, quiet = False, 0

    while left > 0:
        chunk = handle.read(min(_CHUNK, left))
        if not chunk:
            break
        left -= len(chunk)
        facts.read_bytes += len(chunk)

        lines = (buffer + chunk).split(b"\n")
        buffer = lines.pop()

        hits = 0
        for line in lines:
            if not _POSTS.match(line):
                continue
            hits += 1
            tail = _TAIL.search(line)
            if not tail or _PUBLISHED not in line:
                continue
            kind = tail.group(1).decode("ascii", "replace")
            published[kind] = published.get(kind, 0) + 1

        started = started or hits > 0
        quiet = 0 if hits else quiet + 1
        if started and quiet > _QUIET_CHUNKS:
            break

    facts.by_type = dict(sorted(published.items(), key=lambda kv: -kv[1]))
    facts.pages = sum(n for kind, n in published.items() if kind not in SKIPPED_TYPES)
