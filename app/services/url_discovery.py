"""Discover every public URL the running WordPress site exposes.

Four independent sources are combined, because no single one is complete:

1. **The site manifest** -- the mu-plugin installed by the restorer exposes
   registered post types, taxonomies, the permalink structure and the front
   page configuration. This is the only way to learn about custom post types
   without guessing at the schema.
2. **The database** -- ``wp_posts`` and the term tables are queried directly for
   published content. This finds everything, including posts that no page links
   to, and it is far faster than crawling.
3. **Sitemaps** -- ``/wp-sitemap.xml`` (core, since WP 5.5) and ``/sitemap.xml``
   (Yoast, Rank Math, All in One SEO), including sitemap indexes. These reflect
   what the site owner considers public.
4. **Rendered pages** -- internal links found while crawling, which catches
   anything the first three miss, such as pages generated entirely by a plugin.

Everything is normalised and deduplicated through :mod:`app.utils.urls`, and
the total is capped so a calendar widget or a faceted archive cannot expand the
crawl without bound.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from app.config import ConversionOptions
from app.models.job import UrlRecord
from app.utils.urls import ResourceClass, classify_url, normalise_url, to_local_origin

logger = logging.getLogger(__name__)

_SITEMAP_CANDIDATES = (
    "/wp-sitemap.xml",
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/post-sitemap.xml",
)

_XML_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


@dataclass(slots=True)
class SiteManifest:
    """What the mu-plugin reports about the running site."""

    home: str
    site: str = ""
    wp_version: str = ""
    name: str = ""
    charset: str = "UTF-8"
    permalink: str = ""
    posts_per_page: int = 10
    show_on_front: str = "posts"
    page_on_front: int = 0
    page_for_posts: int = 0
    theme: dict = field(default_factory=dict)
    active_plugins: list[str] = field(default_factory=list)
    post_types: dict = field(default_factory=dict)
    taxonomies: dict = field(default_factory=dict)

    @property
    def hosts(self) -> set[str]:
        hosts = set()
        for value in (self.home, self.site):
            host = urlsplit(value).hostname if value else None
            if host:
                hosts.add(host.lower())
        return hosts


def fetch_manifest(base_url: str, timeout: float = 60.0) -> SiteManifest | None:
    """Read the site manifest exposed by the restorer's mu-plugin."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(f"{base_url.rstrip('/')}/?wpsc_manifest=1")
        if response.status_code != 200:
            logger.warning("manifest returned HTTP %s", response.status_code)
            return None
        data = response.json()
    except Exception as exc:
        logger.warning("could not read the site manifest: %s", exc)
        return None

    if not isinstance(data, dict) or "home" not in data:
        logger.warning("manifest response was not in the expected shape")
        return None

    return SiteManifest(
        home=data.get("home") or base_url,
        site=data.get("site") or base_url,
        wp_version=str(data.get("wp_version") or ""),
        name=str(data.get("name") or ""),
        charset=str(data.get("charset") or "UTF-8"),
        permalink=str(data.get("permalink") or ""),
        posts_per_page=int(data.get("posts_per_page") or 10),
        show_on_front=str(data.get("show_on_front") or "posts"),
        page_on_front=int(data.get("page_on_front") or 0),
        page_for_posts=int(data.get("page_for_posts") or 0),
        theme=data.get("theme") or {},
        active_plugins=list(data.get("active_plugins") or []),
        post_types=data.get("post_types") or {},
        taxonomies=data.get("taxonomies") or {},
    )


# ---------------------------------------------------------------------------
# Authoritative permalink enumeration
# ---------------------------------------------------------------------------
def discover_from_wordpress(
    base_url: str,
    options: ConversionOptions,
    *,
    max_urls: int = 5000,
    timeout: float = 120.0,
) -> list[UrlRecord] | None:
    """Ask WordPress itself for every public permalink.

    This is the preferred source, because WordPress applies its own rewrite
    rules: a custom post type registered with ``'slug' => 'projects'`` really
    does live at ``/projects/<name>/`` and no amount of inspecting ``wp_posts``
    would reveal that. Returns ``None`` when the endpoint is unavailable, so
    the caller can fall back to querying the database.
    """
    base = base_url.rstrip("/")
    collected: list[UrlRecord] = []
    offset = 0

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            while len(collected) < max_urls:
                response = client.get(
                    f"{base}/?wpsc_urls=1&offset={offset}&limit=500"
                )
                if response.status_code != 200:
                    logger.warning("permalink endpoint returned HTTP %s", response.status_code)
                    return None if offset == 0 else collected

                payload = response.json()
                batch = payload.get("urls") or []
                if not batch:
                    break

                for entry in batch:
                    location = entry.get("loc")
                    if not location:
                        continue
                    kind = entry.get("kind", "page")
                    if not _wanted(kind, options):
                        continue
                    collected.append(UrlRecord(
                        url=location, source="wordpress", kind=kind,
                        title=(entry.get("title") or None),
                    ))

                if not payload.get("has_more"):
                    break
                offset += payload.get("limit", 500)

    except Exception as exc:
        logger.warning("could not enumerate permalinks from WordPress: %s", exc)
        return None if not collected else collected

    logger.info("WordPress reported %d permalink(s)", len(collected))
    return collected


def _wanted(kind: str, options: ConversionOptions) -> bool:
    """Whether this kind of URL is included by the current export options."""
    return {
        "post": options.include_posts,
        "page": options.include_pages,
        "custom_post_type": options.include_custom_post_types,
        "category": options.include_categories,
        "tag": options.include_tags,
        "taxonomy": options.include_custom_taxonomies,
        "author": options.include_author_archives,
        "archive": True,
        "pagination": options.include_pagination,
    }.get(kind, True)


# ---------------------------------------------------------------------------
# Database discovery (fallback)
# ---------------------------------------------------------------------------
def discover_from_database(
    server,
    database: str,
    table_prefix: str,
    base_url: str,
    options: ConversionOptions,
    manifest: SiteManifest | None = None,
) -> list[UrlRecord]:
    """Query WordPress's own tables for published, publicly visible content.

    Permalinks are not reconstructed by hand -- that would mean reimplementing
    WordPress's rewrite engine and getting it subtly wrong. Instead the post
    *slugs and hierarchy* are read here and turned into URLs using the site's
    actual permalink structure, and anything ambiguous is left to the sitemap
    and link-following passes to catch.
    """
    records: list[UrlRecord] = []
    base = base_url.rstrip("/")

    def add(path: str, kind: str, title: str | None = None) -> None:
        url = normalise_url(path if path.startswith("http") else base + path)
        if url:
            records.append(UrlRecord(url=url, source="database", kind=kind, title=title))

    add("/", "home")

    try:
        with server.connect(database) as conn:
            _discover_posts(conn, table_prefix, add, options, manifest)
            _discover_terms(conn, table_prefix, add, options)
            _discover_authors(conn, table_prefix, add, options)
            _discover_dates(conn, table_prefix, add, options)
    except Exception as exc:
        logger.warning("database discovery failed: %s", exc)

    logger.info("database discovery produced %d URLs", len(records))
    return records


def _discover_posts(conn, prefix: str, add, options: ConversionOptions,
                    manifest: SiteManifest | None) -> None:
    """Published posts, pages and custom post types."""
    wanted: list[str] = []
    if options.include_posts:
        wanted.append("post")
    if options.include_pages:
        wanted.append("page")

    if options.include_custom_post_types and manifest:
        for name, info in manifest.post_types.items():
            if name in {"post", "page", "attachment"}:
                continue
            wanted.append(name)
            archive = info.get("archive_link")
            if archive and info.get("has_archive"):
                add(archive, "archive")

    if not wanted:
        return

    placeholders = ", ".join(["%s"] * len(wanted))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT ID, post_name, post_type, post_title, post_parent, post_date "
            f"FROM `{prefix}posts` "
            f"WHERE post_status='publish' AND post_type IN ({placeholders}) "
            f"AND post_password='' "
            f"ORDER BY post_type, post_date DESC",
            wanted,
        )
        rows = cur.fetchall()

        # Page hierarchy: a child page's URL includes its ancestors' slugs.
        pages = {row[0]: (row[1], row[4]) for row in rows if row[2] == "page"}

    front_page_id = manifest.page_on_front if manifest else 0
    posts_page_id = manifest.page_for_posts if manifest else 0
    permalink = (manifest.permalink if manifest else "") or ""

    for post_id, slug, post_type, title, parent, post_date in rows:
        if not slug:
            continue

        if post_type == "page":
            if post_id == front_page_id:
                continue  # already covered by "/"
            path = "/" + _page_path(post_id, pages) + "/"
            add(path, "page", title)
            if post_id == posts_page_id:
                _add_pagination(add, path, "archive")

        elif post_type == "post":
            add("/" + _post_permalink(slug, post_date, permalink), "post", title)

        else:
            # Custom post types use their rewrite slug, which defaults to the
            # post type name. The sitemap pass corrects any that differ.
            add(f"/{post_type}/{slug}/", "custom_post_type", title)


def _page_path(page_id: int, pages: dict[int, tuple[str, int]]) -> str:
    """Build a hierarchical page path from its ancestors."""
    segments: list[str] = []
    current = page_id
    seen: set[int] = set()
    while current in pages and current not in seen:
        seen.add(current)
        slug, parent = pages[current]
        segments.append(slug)
        current = parent
    return "/".join(reversed(segments))


def _post_permalink(slug: str, post_date, structure: str) -> str:
    """Render a post's path using the site's permalink structure."""
    if not structure or "%postname%" not in structure:
        # Date-based or plain structures are covered by the sitemap and the
        # link-following pass; guessing here would create 404s.
        return f"{slug}/"

    path = structure
    if post_date is not None:
        replacements = {
            "%year%": f"{post_date.year:04d}",
            "%monthnum%": f"{post_date.month:02d}",
            "%day%": f"{post_date.day:02d}",
            "%hour%": f"{post_date.hour:02d}",
            "%minute%": f"{post_date.minute:02d}",
            "%second%": f"{post_date.second:02d}",
        }
        for token, value in replacements.items():
            path = path.replace(token, value)

    path = path.replace("%postname%", slug).replace("%post_id%", "")
    # Anything left unresolved (%category%, %author%) makes the guess unsafe.
    if "%" in path:
        return f"{slug}/"
    return path.strip("/") + "/"


def _add_pagination(add, base_path: str, kind: str, max_pages: int = 50) -> None:
    """Paged archive URLs are added lazily by the crawler following links.

    Only page 2 is seeded here: if it 404s the crawl stops, and if it exists the
    renderer finds the rest by following the pagination links, which is both
    correct and cheaper than guessing a page count.
    """
    add(f"{base_path.rstrip('/')}/page/2/", kind)


def _discover_terms(conn, prefix: str, add, options: ConversionOptions) -> None:
    """Category, tag and custom taxonomy archives that actually have content."""
    wanted: list[str] = []
    if options.include_categories:
        wanted.append("category")
    if options.include_tags:
        wanted.append("post_tag")
    if not wanted and not options.include_custom_taxonomies:
        return

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT t.slug, tt.taxonomy, tt.count, t.name "
            f"FROM `{prefix}term_taxonomy` tt "
            f"JOIN `{prefix}terms` t ON t.term_id = tt.term_id "
            f"WHERE tt.count > 0"
        )
        rows = cur.fetchall()

    for slug, taxonomy, count, name in rows:
        if taxonomy in {"nav_menu", "link_category", "post_format", "wp_theme",
                        "wp_template_part_area", "wp_pattern_category"}:
            continue

        if taxonomy == "category":
            if not options.include_categories:
                continue
            add(f"/category/{slug}/", "category", name)
        elif taxonomy == "post_tag":
            if not options.include_tags:
                continue
            add(f"/tag/{slug}/", "tag", name)
        else:
            if not options.include_custom_taxonomies:
                continue
            # The rewrite base usually matches the taxonomy name; the sitemap
            # and link passes correct it when it does not.
            add(f"/{taxonomy}/{slug}/", "taxonomy", name)


def _discover_authors(conn, prefix: str, add, options: ConversionOptions) -> None:
    if not options.include_author_archives:
        return
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT u.user_nicename FROM `{prefix}users` u "
            f"JOIN `{prefix}posts` p ON p.post_author = u.ID "
            f"WHERE p.post_status='publish' AND p.post_type='post'"
        )
        for (nicename,) in cur.fetchall():
            if nicename:
                add(f"/author/{nicename}/", "author")


def _discover_dates(conn, prefix: str, add, options: ConversionOptions) -> None:
    if not options.include_date_archives:
        return
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT YEAR(post_date), MONTH(post_date) FROM `{prefix}posts` "
            f"WHERE post_status='publish' AND post_type='post' "
            f"ORDER BY 1 DESC, 2 DESC LIMIT 120"
        )
        for year, month in cur.fetchall():
            add(f"/{year:04d}/{month:02d}/", "archive")


# ---------------------------------------------------------------------------
# Sitemap discovery
# ---------------------------------------------------------------------------
def discover_from_sitemaps(base_url: str, timeout: float = 45.0,
                           max_sitemaps: int = 60) -> list[UrlRecord]:
    """Walk the site's sitemap(s), following index files one level deep."""
    base = base_url.rstrip("/")
    found: dict[str, UrlRecord] = {}
    visited: set[str] = set()
    queue: list[str] = [base + path for path in _SITEMAP_CANDIDATES]

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        while queue and len(visited) < max_sitemaps:
            sitemap_url = queue.pop(0)
            if sitemap_url in visited:
                continue
            visited.add(sitemap_url)

            try:
                response = client.get(sitemap_url)
            except Exception as exc:
                logger.debug("sitemap %s unreachable: %s", sitemap_url, exc)
                continue
            if response.status_code != 200:
                continue
            content_type = response.headers.get("content-type", "")
            if "xml" not in content_type and not response.text.lstrip().startswith("<"):
                continue

            children, pages = _parse_sitemap(response.content, sitemap_url)
            for child in children:
                if child not in visited and len(visited) + len(queue) < max_sitemaps:
                    queue.append(child)
            for page in pages:
                url = normalise_url(page)
                if url and url not in found:
                    found[url] = UrlRecord(url=url, source="sitemap", kind="page")

    if found:
        logger.info("sitemap discovery produced %d URLs from %d sitemap(s)", len(found), len(visited))
    return list(found.values())


def _parse_sitemap(content: bytes, source: str) -> tuple[list[str], list[str]]:
    """Return ``(child_sitemaps, page_urls)`` from a sitemap document."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        logger.debug("could not parse sitemap %s: %s", source, exc)
        return [], []

    tag = root.tag.rsplit("}", 1)[-1]

    if tag == "sitemapindex":
        children = [
            loc.text.strip()
            for loc in root.iterfind(".//sm:sitemap/sm:loc", _XML_NS)
            if loc.text
        ]
        if not children:  # namespace-less sitemap
            children = [loc.text.strip() for loc in root.iterfind(".//sitemap/loc") if loc.text]
        return children, []

    pages = [loc.text.strip() for loc in root.iterfind(".//sm:url/sm:loc", _XML_NS) if loc.text]
    if not pages:
        pages = [loc.text.strip() for loc in root.iterfind(".//url/loc") if loc.text]
    return [], pages


# ---------------------------------------------------------------------------
# Combination
# ---------------------------------------------------------------------------
def build_seed_list(
    base_url: str,
    manifest: SiteManifest | None,
    options: ConversionOptions,
    *,
    server=None,
    database: str = "",
    table_prefix: str = "wp_",
) -> list[UrlRecord]:
    """Combine every discovery source into one deduplicated, capped seed list."""
    site_hosts = {urlsplit(base_url).hostname or "127.0.0.1"}
    if manifest:
        site_hosts |= manifest.hosts

    collected: dict[str, UrlRecord] = {}

    def merge(records: Iterable[UrlRecord]) -> None:
        for record in records:
            url = normalise_url(record.url)
            if not url:
                continue
            if classify_url(url, site_hosts) is not ResourceClass.LOCAL:
                continue
            existing = collected.get(url)
            if existing is None:
                record.url = url
                collected[url] = record
            elif existing.kind == "page" and record.kind != "page":
                # Prefer the more specific classification for the report.
                existing.kind = record.kind
                existing.title = existing.title or record.title

    # Always start from the home page.
    merge([UrlRecord(url=base_url.rstrip("/") + "/", source="seed", kind="home")])

    # WordPress's own permalinks first: they are authoritative. Only if that
    # endpoint is unavailable does the database pass run, since reconstructing
    # permalinks by hand cannot honour custom rewrite slugs and produces 404s.
    from_wordpress = discover_from_wordpress(base_url, options)
    if from_wordpress is not None:
        merge(from_wordpress)
    elif server is not None and database:
        logger.info("falling back to database discovery")
        merge(discover_from_database(server, database, table_prefix, base_url, options, manifest))

    merge(discover_from_sitemaps(base_url))

    if options.generate_sitemap or options.include_feeds:
        # These are fetched as documents so they end up in the export.
        for path in ("/wp-sitemap.xml", "/sitemap.xml"):
            url = normalise_url(base_url.rstrip("/") + path)
            if url and url not in collected:
                collected[url] = UrlRecord(url=url, source="seed", kind="sitemap")

    if options.include_feeds:
        for path in ("/feed/", "/comments/feed/"):
            url = normalise_url(base_url.rstrip("/") + path)
            if url:
                collected.setdefault(url, UrlRecord(url=url, source="seed", kind="feed"))

    records = list(collected.values())
    logger.info("discovery produced %d unique URLs", len(records))
    return records


def filter_discovered_links(
    links: Iterable[str],
    site_hosts: set[str],
    *,
    known: set[str],
    depth: int,
    options: ConversionOptions,
    base_url: str = "",
) -> list[UrlRecord]:
    """Turn links found in a rendered page into new queue entries.

    Applies the export options, so a run configured without tag archives does
    not pull them in through the back door by following a link.
    """
    out: list[UrlRecord] = []
    for raw in links:
        url = normalise_url(raw)
        if not url:
            continue
        if classify_url(url, site_hosts) is not ResourceClass.LOCAL:
            continue

        # A page of ours, but the markup may address it by the live domain --
        # often in a different spelling to siteurl (bare host vs www). Force it
        # onto the local server before it can ever be requested; fetching the
        # public address would crawl the live website.
        if base_url:
            url = normalise_url(to_local_origin(url, base_url)) or url
        if url in known:
            continue

        parts = urlsplit(url)
        kind = _kind_from_path(parts.path, parts.query)

        if kind == "tag" and not options.include_tags:
            continue
        if kind == "author" and not options.include_author_archives:
            continue
        if kind == "archive" and not options.include_date_archives:
            continue
        if kind == "category" and not options.include_categories:
            continue
        if kind == "pagination" and not options.include_pagination:
            continue
        if kind == "feed" and not options.include_feeds:
            continue
        if kind == "attachment":
            # Attachment pages are rarely wanted and multiply the crawl.
            continue
        if kind == "comment-reply":
            # ?replytocom=N is the same post with a relocated reply form. It
            # would be exported once per comment as duplicate content.
            continue

        known.add(url)
        out.append(UrlRecord(url=url, source="link", kind=kind, depth=depth))
    return out


_DATE_ARCHIVE = re.compile(r"^/\d{4}/(\d{2}/)?(\d{2}/)?$")


def _kind_from_path(path: str, query: str = "") -> str:
    if "replytocom=" in (query or ""):
        return "comment-reply"
    if path in {"", "/"}:
        return "home"
    if "/page/" in path:
        return "pagination"
    if path.startswith("/category/"):
        return "category"
    if path.startswith("/tag/"):
        return "tag"
    if path.startswith("/author/"):
        return "author"
    if path.endswith("/feed/"):
        return "feed"
    if _DATE_ARCHIVE.match(path):
        return "archive"
    if "/attachment/" in path:
        return "attachment"
    return "page"
