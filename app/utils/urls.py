"""URL normalisation, classification and static-path mapping.

Getting this layer right is what makes the difference between a static export
that works and one full of broken links. Three jobs live here:

1. **Normalisation** -- collapse the many spellings of one page
   (``//host/About``, ``/about``, ``/about/?utm_source=x#top``) to a single
   canonical key, so a page is rendered once.
2. **Classification** -- decide whether a URL is part of the site being
   exported (LOCAL), belongs to somebody else (EXTERNAL), is a server-side
   endpoint that cannot survive as a file (DYNAMIC), or must not be fetched
   at all (BLOCKED).
3. **Path mapping** -- turn a URL into the file it becomes on disk, and then
   work out the relative href that links one output file to another.
"""

from __future__ import annotations

import posixpath
import re
from enum import StrEnum
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

__all__ = [
    "ResourceClass",
    "to_local_origin",
    "is_local_origin",
    "host_variants",
    "normalise_url",
    "classify_url",
    "same_site",
    "url_to_output_path",
    "relative_href",
    "is_probably_asset",
    "strip_tracking_params",
    "split_srcset",
    "join_srcset",
    "guess_extension",
]


class ResourceClass(StrEnum):
    """How a referenced resource should be treated during rewriting."""

    LOCAL = "LOCAL"
    """Belongs to the site being exported. Download and rewrite to a local path."""

    EXTERNAL = "EXTERNAL"
    """Third-party. Preserve, download or block according to configuration."""

    DYNAMIC = "DYNAMIC"
    """A server-side endpoint (admin-ajax, wp-json, wp-admin). Cannot be made
    static; it is reported rather than silently rewritten."""

    BLOCKED = "BLOCKED"
    """Never fetch: non-HTTP schemes, private addresses, and so on."""


# Query parameters that only ever identify a visitor, never a distinct page.
# Stripping them prevents the same page being exported dozens of times.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gclid", "dclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "igshid", "twclid",
    "_ga", "_gl", "ref", "referrer", "yclid", "wbraid", "gbraid",
})

# Query parameters WordPress uses to identify real, distinct content. These are
# preserved through normalisation so a site without pretty permalinks still
# exports correctly.
_MEANINGFUL_PARAMS = frozenset({
    "p", "page_id", "cat", "tag", "author", "s", "paged", "page",
    "post_type", "attachment_id", "m", "year", "monthnum", "day", "feed",
})

#: Paths that only exist because PHP is running behind them.
_DYNAMIC_PATTERNS = (
    re.compile(r"/wp-admin(/|$)"),
    re.compile(r"/wp-login\.php"),
    re.compile(r"/wp-cron\.php"),
    re.compile(r"/wp-json(/|$)"),
    re.compile(r"/admin-ajax\.php"),
    re.compile(r"/xmlrpc\.php"),
    re.compile(r"/wp-signup\.php"),
    re.compile(r"/wp-activate\.php"),
    re.compile(r"[?&]rest_route="),
)

#: Extensions that are assets rather than documents.
ASSET_EXTENSIONS = frozenset({
    # styles & scripts
    ".css", ".js", ".mjs", ".map",
    # raster images
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".ico", ".apng",
    # vector
    ".svg",
    # fonts
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    # media
    ".mp4", ".webm", ".ogv", ".mov", ".m4v", ".mp3", ".ogg", ".wav", ".m4a", ".flac",
    # data / manifests
    ".json", ".xml", ".webmanifest", ".txt", ".vtt", ".srt",
    # documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".csv",
})

DOCUMENT_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".csv",
})

MEDIA_EXTENSIONS = frozenset({
    ".mp4", ".webm", ".ogv", ".mov", ".m4v", ".mp3", ".ogg", ".wav", ".m4a", ".flac",
})

#: Extensions that are served as HTML documents and so become directories.
_HTML_EXTENSIONS = frozenset({".html", ".htm", ".php", ""})

_SAFE_PATH_CHARS = "/:@-._~!$&'()*+,;="


def strip_tracking_params(query: str, *, keep_meaningful: bool = True) -> str:
    """Drop analytics parameters, keeping ones that select real content."""
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    kept = [
        (k, v) for k, v in pairs
        if k.lower() not in _TRACKING_PARAMS
        and (keep_meaningful or k.lower() in _MEANINGFUL_PARAMS)
    ]
    # Sort so ``?a=1&b=2`` and ``?b=2&a=1`` collapse to one page.
    kept.sort()
    return urlencode(kept, doseq=True)


def normalise_url(
    url: str,
    base: str | None = None,
    *,
    drop_fragment: bool = True,
    force_trailing_slash: bool = True,
) -> str:
    """Canonicalise *url*, resolving it against *base* when it is relative.

    Returns an empty string for things that are not addressable resources
    (``#anchor``, ``javascript:``, ``data:``, ``mailto:`` and friends).
    """
    if not url:
        return ""

    url = url.strip().replace("\n", "").replace("\r", "").replace("\t", "")
    if not url or url.startswith("#"):
        return ""

    lowered = url.lower()
    if lowered.startswith(("javascript:", "data:", "mailto:", "tel:", "sms:", "about:", "blob:")):
        return ""

    # Protocol-relative URLs inherit the base scheme.
    if url.startswith("//"):
        scheme = urlsplit(base).scheme if base else "https"
        url = f"{scheme}:{url}"

    if base:
        url = urljoin(base, url)

    parts = urlsplit(url)
    if parts.scheme and parts.scheme not in {"http", "https"}:
        return ""
    if not parts.netloc:
        return ""

    # Host: lowercase, drop the default port, drop a trailing dot.
    host = parts.hostname or ""
    host = host.lower().rstrip(".")
    if parts.port and not (
        (parts.scheme == "http" and parts.port == 80)
        or (parts.scheme == "https" and parts.port == 443)
    ):
        netloc = f"{host}:{parts.port}"
    else:
        netloc = host

    # Path: resolve ``.``/``..``, re-encode consistently.
    path = parts.path or "/"
    path = posixpath.normpath(unquote(path))
    if path == ".":
        path = "/"
    # normpath eats a meaningful trailing slash; put it back.
    if (parts.path.endswith("/") or parts.path == "") and not path.endswith("/"):
        path += "/"
    if not path.startswith("/"):
        path = "/" + path

    extension = posixpath.splitext(path)[1].lower()
    if force_trailing_slash and extension == "" and not path.endswith("/"):
        # WordPress pretty permalinks are directories; ``/about`` and
        # ``/about/`` are the same page and must not be rendered twice.
        path += "/"

    path = quote(path, safe=_SAFE_PATH_CHARS)
    query = strip_tracking_params(parts.query)
    fragment = "" if drop_fragment else parts.fragment

    return urlunsplit((parts.scheme, netloc, path, query, fragment))


def to_local_origin(url: str, base_url: str) -> str:
    """Re-point a same-site URL at the local render server.

    A page's markup routinely refers to the site by its public address, and not
    always the same spelling of it: a site whose ``siteurl`` is
    ``https://www.example.com`` will still contain links written as
    ``https://example.com/...``. Those are correctly recognised as internal
    pages that belong in the export.

    What must never follow from that is fetching them at their public address.
    Doing so would send the crawler to the live website -- the one thing this
    tool exists to avoid -- reading production content instead of the restored
    copy, and hammering a server the user did not ask us to touch.

    So every page URL is forced onto the local origin before it is requested.
    The path, query and fragment are preserved; only scheme, host and port are
    replaced.
    """
    if not url or not base_url:
        return url
    parts = urlsplit(url)
    base = urlsplit(base_url)
    if parts.scheme == base.scheme and parts.netloc == base.netloc:
        return url
    return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, parts.fragment))


def is_local_origin(url: str, base_url: str) -> bool:
    """Whether *url* already points at the local render server."""
    parts, base = urlsplit(url), urlsplit(base_url)
    return parts.scheme == base.scheme and parts.netloc == base.netloc


def host_variants(url: str) -> set[str]:
    """Every spelling of a site's own address that can appear in its content.

    WordPress stores one canonical ``siteurl``, but themes, page builders and
    hand-written links use the others freely: both schemes, with and without
    ``www``, and protocol-relative. A URL replacement that covers only the
    canonical form leaves the rest pointing at the live domain.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return set()

    port = f":{parts.port}" if parts.port else ""
    bare = host[4:] if host.startswith("www.") else host
    hosts = {bare, f"www.{bare}"}

    variants: set[str] = set()
    for candidate in hosts:
        authority = f"{candidate}{port}"
        variants.add(f"https://{authority}")
        variants.add(f"http://{authority}")
        variants.add(f"//{authority}")
    return variants


def same_site(url: str, site_hosts: set[str]) -> bool:
    """Whether *url* belongs to one of the hosts making up the exported site."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    if host in site_hosts:
        return True
    # Treat ``www.example.com`` and ``example.com`` as the same site.
    bare = host[4:] if host.startswith("www.") else host
    return bare in site_hosts or f"www.{bare}" in site_hosts


def classify_url(url: str, site_hosts: set[str]) -> ResourceClass:
    """Sort a URL into one of the four handling buckets."""
    if not url:
        return ResourceClass.BLOCKED

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return ResourceClass.BLOCKED

    target = f"{parts.path}?{parts.query}" if parts.query else parts.path
    is_local = same_site(url, site_hosts)

    for pattern in _DYNAMIC_PATTERNS:
        if pattern.search(target):
            # Only the exported site's own endpoints are "dynamic"; the same
            # path on someone else's domain is just external.
            return ResourceClass.DYNAMIC if is_local else ResourceClass.EXTERNAL

    return ResourceClass.LOCAL if is_local else ResourceClass.EXTERNAL


def is_probably_asset(url: str) -> bool:
    """Whether the URL points at an asset rather than an HTML document."""
    path = urlsplit(url).path
    ext = posixpath.splitext(path)[1].lower()
    return bool(ext) and ext in ASSET_EXTENSIONS and ext not in {".php", ".html", ".htm"}


def guess_extension(url: str, content_type: str | None = None) -> str:
    """Best extension for a downloaded asset, preferring the URL's own."""
    ext = posixpath.splitext(urlsplit(url).path)[1].lower()
    if ext and len(ext) <= 6 and re.fullmatch(r"\.[a-z0-9]+", ext):
        return ext
    if not content_type:
        return ""
    mime = content_type.split(";", 1)[0].strip().lower()
    return {
        "text/css": ".css",
        "text/javascript": ".js",
        "application/javascript": ".js",
        "application/x-javascript": ".js",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
        "image/svg+xml": ".svg",
        "image/x-icon": ".ico",
        "image/vnd.microsoft.icon": ".ico",
        "font/woff": ".woff",
        "font/woff2": ".woff2",
        "font/ttf": ".ttf",
        "font/otf": ".otf",
        "application/font-woff": ".woff",
        "application/font-woff2": ".woff2",
        "application/vnd.ms-fontobject": ".eot",
        "application/json": ".json",
        "application/manifest+json": ".webmanifest",
        "text/html": ".html",
        "application/pdf": ".pdf",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "audio/mpeg": ".mp3",
    }.get(mime, "")


def url_to_output_path(url: str, *, index_name: str = "index.html", flat: bool = False) -> str:
    """Map a page URL to its POSIX output path, relative to the site root.

    Folder layout (``flat=False``) keeps the site's URLs identical on a web
    server, because every server maps ``/about/`` to ``about/index.html``:

    ``https://site/``                 -> ``index.html``
    ``https://site/about/``           -> ``about/index.html``
    ``https://site/a/b.html``         -> ``a/b.html``
    ``https://site/?page_id=7``       -> ``page_id-7/index.html``
    ``https://site/blog/?paged=2``    -> ``blog/page/2/index.html``

    Flat layout (``flat=True``) gives each page its own ``.html`` file. The URLs
    change (``/about/`` becomes ``/about.html``), but there is one file per page
    instead of one folder per page:

    ``https://site/``                 -> ``index.html``
    ``https://site/about/``           -> ``about.html``
    ``https://site/about/team/``      -> ``about/team.html``
    ``https://site/blog/?paged=2``    -> ``blog/page/2.html``
    """
    path = _folder_output_path(url, index_name)
    suffix = "/" + index_name
    if flat and path.endswith(suffix):
        # Everything except the home page, which is index.html either way.
        path = path[: -len(suffix)] + ".html"
    return path


def _folder_output_path(url: str, index_name: str) -> str:
    from app.utils.security import sanitise_component

    parts = urlsplit(url)
    path = unquote(parts.path or "/")
    # Every folder level must be creatable on Windows and on a Linux host.
    # Real sites contain typos such as /a286-foil/%20/ -- a level that is only
    # a space -- and Windows refuses to create a folder named " ". Such a level
    # carries no meaning, so it is dropped (that URL is the a286-foil page);
    # forbidden characters and reserved names (CON, NUL...) are made safe.
    segments = []
    for raw in path.split("/"):
        raw = raw.strip()
        if raw in {"", ".", ".."}:
            continue
        safe = sanitise_component(raw, fallback="")
        if safe:
            segments.append(safe)

    query = parts.query
    if query:
        params = dict(parse_qsl(query, keep_blank_values=True))

        # Pagination is by far the most common query string on a WordPress
        # site, and it has a natural directory form.
        paged = params.pop("paged", None) or params.pop("page", None)
        if paged and paged.isdigit() and int(paged) > 1:
            segments += ["page", paged]

        if params:
            # Anything left becomes a deterministic, filesystem-safe directory
            # so two different query strings never collide.
            encoded = "-".join(
                f"{_slug(k)}-{_slug(v)}" for k, v in sorted(params.items()) if k
            )
            if encoded:
                segments.append(encoded[:100])

    if not segments:
        return index_name

    last = segments[-1]
    ext = posixpath.splitext(last)[1].lower()

    if ext in _HTML_EXTENSIONS and ext != "":
        if ext == ".php":
            # ``/feed.php`` cannot stay a .php file in a static export.
            segments[-1] = posixpath.splitext(last)[0]
            segments.append(index_name)
        # ``.html``/``.htm`` are already files: leave them be.
    elif ext and ext in ASSET_EXTENSIONS:
        # An asset URL that reached page mapping (e.g. an exported XML sitemap)
        # keeps its own filename.
        pass
    else:
        segments.append(index_name)

    return "/".join(segments)


def _slug(value: str) -> str:
    """Filesystem-safe fragment for embedding a query parameter in a path."""
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", unquote(value)).strip("-")
    return value[:40] or "x"


def relative_href(from_output_path: str, to_output_path: str, *, pretty: bool = True) -> str:
    """Relative link from one generated file to another.

    Both arguments are site-root-relative POSIX paths as produced by
    :func:`url_to_output_path`. Relative links are used so the export works
    from a subdirectory as happily as from a domain root.
    """
    from_dir = posixpath.dirname(from_output_path) or "."
    rel = posixpath.relpath(to_output_path, start=from_dir)

    if pretty and rel.endswith("/index.html"):
        rel = rel[: -len("index.html")]
    elif pretty and rel == "index.html":
        rel = "./"

    return _href_quote(rel or "./")


def _href_quote(path: str) -> str:
    """Percent-encode a file path for use in an href: a page saved in a folder
    named "sheets and plates" is linked as sheets%20and%20plates/, which every
    browser and web server resolves, where a raw space is not reliable."""
    return quote(path, safe="/-._~!$&'()*+,;=:@")


def root_relative_href(to_output_path: str, *, pretty: bool = True) -> str:
    """Absolute-from-root link, for exports deployed at a domain root."""
    href = "/" + to_output_path.lstrip("/")
    if pretty and href.endswith("/index.html"):
        href = href[: -len("index.html")]
    return _href_quote(href)


# ---------------------------------------------------------------------------
# srcset helpers
# ---------------------------------------------------------------------------
_WHITESPACE = " \t\n\r\f"


def split_srcset(value: str) -> list[tuple[str, str]]:
    """Parse a ``srcset`` into ``[(url, descriptor), ...]``.

    This follows the WHATWG "parse a srcset attribute" algorithm rather than
    splitting on commas, because a comma is legal *inside* a URL: a naive split
    mangles real filenames such as ``hero-1,200x800.jpg 2x``. The rule is that
    the URL is an unbroken run of non-whitespace; a comma only ends a candidate
    when it trails that run or appears in the descriptor part.
    """
    out: list[tuple[str, str]] = []
    text = value or ""
    i, n = 0, len(value or "")

    while i < n:
        # Skip leading whitespace and stray separator commas.
        while i < n and (text[i] in _WHITESPACE or text[i] == ","):
            i += 1
        if i >= n:
            break

        # The URL runs to the next whitespace character.
        start = i
        while i < n and text[i] not in _WHITESPACE:
            i += 1
        url = text[start:i]

        if url.endswith(","):
            # Trailing commas terminate the candidate; there is no descriptor.
            out.append((url.rstrip(","), ""))
            continue

        # Descriptor runs to the next top-level comma, ignoring commas nested
        # in parentheses (as used by the ``sizes``-style media syntax).
        while i < n and text[i] in _WHITESPACE:
            i += 1
        desc_start = i
        depth = 0
        while i < n:
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                break
            i += 1
        descriptor = text[desc_start:i].strip()
        i += 1  # step over the separating comma

        if url:
            out.append((url, descriptor))

    return out


def join_srcset(entries: list[tuple[str, str]]) -> str:
    """Inverse of :func:`split_srcset`."""
    return ", ".join(f"{url} {desc}".strip() for url, desc in entries)
