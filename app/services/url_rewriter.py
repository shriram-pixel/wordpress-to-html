"""Decide where every URL lands in the export, and how to link to it.

This module owns the single source of truth for two mappings:

* **page URL -> output file** (``/about/`` -> ``about/index.html``)
* **asset URL -> output file** (``/wp-content/uploads/a.png?ver=2`` ->
  ``wp-content/uploads/a.png``)

Everything that rewrites markup -- the HTML processor, the CSS rewriter, the
sitemap generator -- asks :class:`AssetMap` rather than computing paths itself,
which is what keeps a page's ``<img src>`` and the file actually written to
disk from ever disagreeing.

Two problems make this less trivial than it looks:

*Query strings.* WordPress cache-busts with ``?ver=6.4.2``. Those URLs must
become real files, and two URLs differing only by ``ver`` are nearly always the
same asset -- but not always, so content is compared before they are merged.

*Collisions.* ``/a/index.html`` as a page and ``/a/index.html`` as an asset
cannot both exist. Allocation is centralised so a collision is detected and
resolved once, deterministically.
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

from app.utils.security import sanitise_component
from app.utils.urls import (
    guess_extension,
    normalise_url,
    relative_href,
    root_relative_href,
    url_to_output_path,
)

logger = logging.getLogger(__name__)

#: Query parameters that only cache-bust and never select different content.
_CACHE_BUST_PARAMS = frozenset({"ver", "v", "version", "rev", "cache", "t", "ts", "_"})

_MAX_SEGMENT = 120


@dataclass(slots=True)
class AssetRecord:
    """One localised asset."""

    url: str
    output_path: str
    """POSIX path relative to the export root."""
    content_type: str = ""
    size: int = 0
    digest: str = ""
    source: str = "html"
    """Where the reference was found: ``html``, ``css``, ``network``."""
    downloaded: bool = False
    error: str = ""
    retryable: bool | None = None
    """Whether a failed download is worth retrying. ``None`` means unknown."""


class AssetMap:
    """Allocates output paths and answers "what does this URL become?".

    Path allocation is deterministic and collision-free: the same input always
    produces the same output path, and two different URLs never share one.
    """

    def __init__(
        self,
        *,
        site_hosts: set[str] | None = None,
        flat: bool = False,
        folder_links: bool = True,
    ) -> None:
        self.site_hosts = site_hosts or set()
        self.flat = flat
        """``about.html`` per page rather than ``about/index.html``."""
        self.folder_links = folder_links
        """Link to ``about/`` rather than ``about/index.html``."""

        self._assets: dict[str, AssetRecord] = {}
        """normalised asset URL -> record"""

        self._pages: dict[str, str] = {}
        """normalised page URL -> output path"""

        self._claimed: dict[str, str] = {}
        """output path (lowercased) -> the URL that owns it.

        Lowercased because Windows and macOS filesystems are case-insensitive:
        two URLs differing only in case would otherwise silently overwrite one
        another on those platforms but not on Linux."""

    # -- pages --------------------------------------------------------------
    def add_page(self, url: str) -> str:
        """Register a page and return its output path."""
        url = normalise_url(url) or url
        existing = self._pages.get(url)
        if existing:
            return existing

        candidate = url_to_output_path(url, flat=self.flat)
        path = self._claim(candidate, url)
        self._pages[url] = path
        return path

    def page_path(self, url: str) -> str | None:
        return self._pages.get(normalise_url(url) or url)

    @property
    def pages(self) -> dict[str, str]:
        return dict(self._pages)

    # -- assets -------------------------------------------------------------
    def add_asset(self, url: str, *, content_type: str = "", source: str = "html") -> AssetRecord:
        """Register an asset and allocate its output path."""
        url = normalise_url(url, drop_fragment=True, force_trailing_slash=False) or url
        existing = self._assets.get(url)
        if existing:
            return existing

        candidate = self._asset_output_path(url, content_type)
        path = self._claim(candidate, url)

        record = AssetRecord(url=url, output_path=path, content_type=content_type, source=source)
        self._assets[url] = record
        return record

    def asset(self, url: str) -> AssetRecord | None:
        key = normalise_url(url, drop_fragment=True, force_trailing_slash=False) or url
        return self._assets.get(key)

    @property
    def assets(self) -> dict[str, AssetRecord]:
        return dict(self._assets)

    def asset_path(self, url: str) -> str | None:
        record = self.asset(url)
        return record.output_path if record else None

    # -- lookup used by the rewriters ---------------------------------------
    def output_path_for(self, url: str) -> str | None:
        """Output path for *url*, whether it is a page or an asset."""
        normalised = normalise_url(url) or url
        if normalised in self._pages:
            return self._pages[normalised]

        record = self.asset(url)
        if record:
            return record.output_path

        # A page may have been registered with a trailing slash while the link
        # omits it, or the other way round.
        alternative = normalise_url(url, force_trailing_slash=False)
        if alternative and alternative in self._pages:
            return self._pages[alternative]
        return None

    def href_for(self, target_url: str, from_output_path: str, *, root_relative: bool = False) -> str | None:
        """The href that links *from_output_path* to *target_url*.

        Relative by default, so the export works from a subdirectory as well as
        from a domain root.
        """
        target = self.output_path_for(target_url)
        if target is None:
            return None

        fragment = urlsplit(target_url).fragment
        # Folder links ("about/") are what WordPress produces and what a web
        # server expects. Naming the file ("about/index.html") is for a site
        # that will be opened straight from a disk, where nothing resolves a
        # folder to its index page.
        # Only in the folder layout. With one file per page the home page is
        # still index.html, and linking to "../" instead of "../index.html"
        # would be the one link in the export that needs a server.
        pretty = self.folder_links and not self.flat
        href = (
            root_relative_href(target, pretty=pretty)
            if root_relative
            else relative_href(from_output_path, target, pretty=pretty)
        )
        return f"{href}#{fragment}" if fragment else href

    # -- allocation ---------------------------------------------------------
    def _claim(self, candidate: str, owner: str) -> str:
        """Reserve *candidate*, disambiguating if another URL already holds it."""
        candidate = _tidy_path(candidate)
        key = candidate.lower()

        held_by = self._claimed.get(key)
        if held_by is None:
            self._claimed[key] = owner
            return candidate
        if held_by == owner:
            return candidate

        # Deterministic suffix derived from the URL, so re-running the
        # conversion produces identical paths.
        digest = hashlib.sha1(owner.encode("utf-8")).hexdigest()[:8]
        stem, dot, extension = candidate.rpartition(".")
        if dot and len(extension) <= 8 and "/" not in extension:
            disambiguated = f"{stem}.{digest}.{extension}"
        else:
            disambiguated = f"{candidate}.{digest}"

        self._claimed[disambiguated.lower()] = owner
        logger.debug("path collision on %s; %s allocated instead", candidate, disambiguated)
        return disambiguated

    def _asset_output_path(self, url: str, content_type: str = "") -> str:
        """Map an asset URL to a path, preserving the site's own structure.

        Keeping ``wp-content/uploads/2026/01/x.jpg`` intact matters: it means the
        exported site's asset URLs match the original, so external references,
        cached links and any hard-coded paths in inline JavaScript keep working.
        """
        parts = urlsplit(url)
        path = parts.path or "/"

        segments = [
            sanitise_component(segment)[:_MAX_SEGMENT]
            for segment in path.split("/")
            if segment not in {"", ".", ".."}
        ]

        if not segments:
            segments = ["index"]

        filename = segments[-1]
        extension = posixpath.splitext(filename)[1].lower()

        if not extension:
            guessed = guess_extension(url, content_type)
            if guessed:
                filename += guessed
                segments[-1] = filename

        meaningful = _meaningful_query(parts.query)
        if meaningful:
            # A query that selects different content becomes part of the name,
            # so two variants cannot overwrite each other.
            digest = hashlib.sha1(meaningful.encode("utf-8")).hexdigest()[:8]
            stem, dot, extension = filename.rpartition(".")
            segments[-1] = f"{stem}.{digest}.{extension}" if dot else f"{filename}.{digest}"

        # Assets from another host are namespaced so they cannot collide with
        # the site's own files.
        host = (parts.hostname or "").lower()
        if host and self.site_hosts and host not in self.site_hosts:
            bare = host[4:] if host.startswith("www.") else host
            if bare not in self.site_hosts and f"www.{bare}" not in self.site_hosts:
                segments = ["external", sanitise_component(host), *segments]

        return "/".join(segments)


def _meaningful_query(query: str) -> str:
    """The part of a query string that genuinely selects different content."""
    if not query:
        return ""
    kept = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in _CACHE_BUST_PARAMS
    ]
    kept.sort()
    return "&".join(f"{k}={v}" for k, v in kept)


_MULTI_SLASH = re.compile(r"/{2,}")


def _tidy_path(path: str) -> str:
    path = _MULTI_SLASH.sub("/", path.strip("/"))
    return path or "index.html"


# ---------------------------------------------------------------------------
# CSS rewriting
# ---------------------------------------------------------------------------
#: ``url(...)`` in all three quoting forms.
#:
#: The quoted and unquoted cases are separate alternatives on purpose. Writing
#: the quote as an optional backreference group looks tidier but is broken: when
#: the group matches empty, a ``(?!\1)`` guard can never succeed, so every
#: unquoted ``url(img/a.png)`` is silently skipped -- and unquoted URLs are the
#: norm in minified theme and plugin CSS.
_CSS_URL = re.compile(
    r"""url\(\s*(?:"(?P<dq>(?:\\.|[^"\\])*)"|'(?P<sq>(?:\\.|[^'\\])*)'"""
    r"""|(?P<bare>(?:\\.|[^)"'\s\\])*))\s*\)""",
    re.IGNORECASE,
)


def _css_url_value(match: re.Match) -> str:
    """The URL text from whichever quoting alternative matched."""
    for group in ("dq", "sq", "bare"):
        value = match.group(group)
        if value is not None:
            return value
    return ""

#: ``@import "x.css"`` and ``@import url("x.css")``.
_CSS_IMPORT = re.compile(
    r"""@import\s+(?!url\()(?P<quote>["'])(?P<url>(?:\\.|(?!\1).)*)(?P=quote)""",
    re.IGNORECASE,
)

#: ``image-set()`` / ``-webkit-image-set()`` entries that are bare strings.
_CSS_IMAGE_SET = re.compile(
    r"""(?P<prefix>image-set\(\s*)(?P<quote>["'])(?P<url>(?:\\.|(?!\2).)*)(?P=quote)""",
    re.IGNORECASE,
)


def extract_css_urls(css: str, base_url: str) -> list[str]:
    """Every absolute URL a stylesheet references."""
    found: list[str] = []

    def collect(raw: str) -> None:
        raw = raw.strip()
        if not raw or raw.startswith(("data:", "#", "about:")):
            return
        absolute = normalise_url(raw, base_url, force_trailing_slash=False)
        if absolute:
            found.append(absolute)

    for match in _CSS_URL.finditer(css):
        collect(_css_url_value(match))
    for match in _CSS_IMPORT.finditer(css):
        collect(match.group("url"))
    for match in _CSS_IMAGE_SET.finditer(css):
        collect(match.group("url"))

    # Deduplicate, preserving order for stable logs.
    seen: set[str] = set()
    return [u for u in found if not (u in seen or seen.add(u))]


def rewrite_css_urls(css: str, base_url: str, resolver) -> tuple[str, int]:
    """Rewrite every ``url()``/``@import`` in *css*.

    *resolver* takes an absolute URL and returns the replacement string, or
    ``None`` to leave the reference untouched (external resources being
    preserved, for example). Returns ``(css, number_rewritten)``.
    """
    count = 0

    def replace_url(match: re.Match) -> str:
        nonlocal count
        raw = _css_url_value(match).strip()
        if not raw or raw.startswith(("data:", "#", "about:")):
            return match.group(0)

        absolute = normalise_url(raw, base_url, force_trailing_slash=False)
        if not absolute:
            return match.group(0)

        replacement = resolver(absolute)
        if replacement is None:
            return match.group(0)

        count += 1
        # Always quote: a rewritten path can contain characters that are not
        # legal in an unquoted url() token.
        return f'url("{_escape_css_url(replacement)}")'

    def replace_import(match: re.Match) -> str:
        nonlocal count
        raw = match.group("url").strip()
        absolute = normalise_url(raw, base_url, force_trailing_slash=False)
        if not absolute:
            return match.group(0)
        replacement = resolver(absolute)
        if replacement is None:
            return match.group(0)
        count += 1
        return f'@import "{_escape_css_url(replacement)}"'

    def replace_image_set(match: re.Match) -> str:
        nonlocal count
        raw = match.group("url").strip()
        absolute = normalise_url(raw, base_url, force_trailing_slash=False)
        if not absolute:
            return match.group(0)
        replacement = resolver(absolute)
        if replacement is None:
            return match.group(0)
        count += 1
        return f'{match.group("prefix")}"{_escape_css_url(replacement)}"'

    css = _CSS_URL.sub(replace_url, css)
    css = _CSS_IMPORT.sub(replace_import, css)
    css = _CSS_IMAGE_SET.sub(replace_image_set, css)
    return css, count


def _escape_css_url(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
