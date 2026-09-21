"""Validate the generated static site before it is packaged.

Two complementary checks run here:

* **A filesystem crawl** -- parse every generated HTML file, resolve every
  reference it makes, and confirm the target exists on disk. This is fast,
  needs no browser and no server, and catches the failure that matters most: a
  page referencing an asset that was never downloaded.
* **A browser pass** -- serve the output over HTTP and load a sample of pages in
  Chromium, collecting console errors, page errors and failed requests. Only a
  browser finds problems that appear once JavaScript runs.

Both report findings; neither modifies the export.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import logging
import posixpath
import re
import socketserver
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from app.services.html_processor import HTML_PARSER
from app.services.url_rewriter import extract_css_urls
from app.utils.filesystem import iter_files
from app.utils.urls import split_srcset

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BrokenReference:
    """One reference in the export that does not resolve."""

    source_file: str
    reference: str
    kind: str
    """``link``, ``image``, ``css``, ``js``, ``font``, ``media``, ``other``."""
    reason: str = "file not found"


@dataclass(slots=True)
class ValidationReport:
    html_files: int = 0
    total_files: int = 0
    references_checked: int = 0
    broken_links: list[BrokenReference] = field(default_factory=list)
    missing_assets: list[BrokenReference] = field(default_factory=list)
    external_links: int = 0
    orphaned_files: list[str] = field(default_factory=list)

    console_errors: list[dict] = field(default_factory=list)
    page_errors: list[dict] = field(default_factory=list)
    failed_requests: list[dict] = field(default_factory=list)
    pages_checked_in_browser: int = 0

    @property
    def broken_link_count(self) -> int:
        return len(self.broken_links)

    @property
    def missing_asset_count(self) -> int:
        return len(self.missing_assets)

    @property
    def is_clean(self) -> bool:
        return not (self.broken_links or self.missing_assets or self.page_errors)

    def summary(self) -> dict:
        return {
            "html_files": self.html_files,
            "total_files": self.total_files,
            "references_checked": self.references_checked,
            "broken_links": len(self.broken_links),
            "missing_assets": len(self.missing_assets),
            "external_links": self.external_links,
            "console_errors": len(self.console_errors),
            "page_errors": len(self.page_errors),
            "failed_requests": len(self.failed_requests),
            "pages_checked_in_browser": self.pages_checked_in_browser,
        }


#: Attributes to follow when crawling the generated HTML.
_ASSET_ATTRS = (
    ("img", "src", "image"), ("img", "data-src", "image"),
    ("script", "src", "js"), ("link", "href", "css"),
    ("source", "src", "media"), ("video", "src", "media"),
    ("video", "poster", "image"), ("audio", "src", "media"),
    ("iframe", "src", "other"), ("embed", "src", "other"),
    ("object", "data", "other"), ("track", "src", "other"),
    ("input", "src", "image"),
)


def _scan_html(job: tuple[str, str]) -> tuple[str, list[tuple[str, str]], str]:
    """Parse one exported page and list every local reference it makes.

    Runs in a worker process: parsing is the expensive part of checking a site
    and it is pure CPU, so it is the one piece worth spreading over cores.
    Returns ``(relative path, [(reference, kind), ...], error)``.
    """
    path, relative = job
    refs: list[tuple[str, str]] = []
    try:
        markup = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return relative, refs, f"unreadable: {exc}"

    soup = BeautifulSoup(markup, HTML_PARSER)

    for tag_name, attribute, kind in _ASSET_ATTRS:
        for element in soup.find_all(tag_name):
            value = element.get(attribute)
            if not isinstance(value, str) or not value.strip():
                continue
            if tag_name == "link":
                rel = element.get("rel") or []
                if isinstance(rel, str):
                    rel = rel.split()
                rel_set = {r.lower() for r in rel}
                if not rel_set & {
                    "stylesheet", "icon", "shortcut icon", "apple-touch-icon",
                    "manifest", "preload", "mask-icon",
                }:
                    continue
                kind = "css" if "stylesheet" in rel_set else "other"
            refs.append((value, kind))

    for element in soup.find_all(attrs={"srcset": True}):
        for candidate, _descriptor in split_srcset(element.get("srcset") or ""):
            refs.append((candidate, "image"))

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.startswith("#") or href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        refs.append((href, "external" if _is_external(href) else "link"))

    for style_tag in soup.find_all("style"):
        css = style_tag.string or style_tag.get_text() or ""
        refs.extend((raw, "css") for raw in _local_css_refs(css))

    for element in soup.find_all(style=True):
        refs.extend((raw, "image") for raw in _local_css_refs(element.get("style") or ""))

    return relative, refs, ""


def _scan_css(job: tuple[str, str]) -> tuple[str, list[tuple[str, str]], str]:
    """Stylesheets reference fonts and images the HTML never mentions."""
    path, relative = job
    try:
        css = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return relative, [], f"unreadable: {exc}"
    return relative, [(raw, _css_ref_kind(raw)) for raw in _local_css_refs(css)], ""


def validate_output(
    output_dir: Path, *, max_files: int = 20_000, workers: int = 1
) -> ValidationReport:
    """Crawl the generated site on disk and check that every reference resolves.

    *workers* > 1 parses the pages in that many processes. Checking whether a
    reference resolves stays here, in one place, so the result does not depend
    on how many workers were used.
    """
    output_dir = Path(output_dir)
    report = ValidationReport()

    all_files = list(iter_files(output_dir))
    report.total_files = len(all_files)

    existing = {p.relative_to(output_dir).as_posix().lower() for p in all_files}
    referenced: set[str] = set()

    html_files = [p for p in all_files if p.suffix.lower() in {".html", ".htm"}][:max_files]
    css_files = [p for p in all_files if p.suffix.lower() == ".css"]
    report.html_files = len(html_files)

    def resolve(relative: str, raw: str, kind: str) -> None:
        base_dir = posixpath.dirname(relative)
        report.references_checked += 1
        if kind == "external":
            report.external_links += 1
            return
        resolved = _resolve_local(raw, base_dir)
        if resolved is None:
            return  # anchor, data: URI or other non-file reference
        referenced.add(resolved.lower())
        if resolved.lower() not in existing:
            entry = BrokenReference(relative, raw, "other" if kind == "link" else kind)
            if kind == "link":
                report.broken_links.append(BrokenReference(relative, raw, "link"))
            else:
                report.missing_assets.append(entry)

    jobs = [
        (_scan_html, [(str(p), p.relative_to(output_dir).as_posix()) for p in html_files]),
        (_scan_css, [(str(p), p.relative_to(output_dir).as_posix()) for p in css_files]),
    ]

    for scan, items in jobs:
        if not items:
            continue
        for relative, refs, error in _run_scan(scan, items, workers):
            if error:
                report.broken_links.append(BrokenReference(relative, "", "other", error))
                continue
            for raw, kind in refs:
                resolve(relative, raw, kind)

    logger.info(
        "static validation: %d HTML files, %d references, %d broken links, %d missing assets",
        report.html_files, report.references_checked,
        len(report.broken_links), len(report.missing_assets),
    )
    return report


def _run_scan(scan, items: list[tuple[str, str]], workers: int):
    """Map *scan* over *items*, in worker processes when that is worth it."""
    if workers <= 1 or len(items) < 40:
        return [scan(item) for item in items]
    try:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(scan, items, chunksize=8))
    except Exception as exc:
        # A sandbox that forbids subprocesses, or a spawn failure: checking the
        # site matters more than checking it quickly.
        logger.warning("parallel scan unavailable (%s); checking in one process", exc)
        return [scan(item) for item in items]


_CSS_URL_SIMPLE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", re.IGNORECASE)


def _local_css_refs(css: str) -> list[str]:
    out = []
    for match in _CSS_URL_SIMPLE.finditer(css or ""):
        raw = match.group(1).strip()
        if raw and not raw.startswith(("data:", "#", "about:")) and not _is_external(raw):
            out.append(raw)
    return out


def _css_ref_kind(raw: str) -> str:
    extension = posixpath.splitext(urlsplit(raw).path)[1].lower()
    if extension in {".woff", ".woff2", ".ttf", ".otf", ".eot"}:
        return "font"
    if extension == ".css":
        return "css"
    return "image"


def _is_external(raw: str) -> bool:
    return raw.startswith(("http://", "https://", "//"))


def _resolve_local(raw: str, base_dir: str) -> str | None:
    """Resolve a reference to a path relative to the export root, or ``None``."""
    raw = raw.strip()
    if not raw or _is_external(raw):
        return None
    if raw.startswith(("data:", "#", "mailto:", "tel:", "javascript:", "about:", "blob:")):
        return None

    path = unquote(urlsplit(raw).path)
    if not path:
        return None

    # Whether the reference names a directory has to be decided *before*
    # normalising, because normpath strips the trailing slash that says so.
    is_directory = path.endswith("/")

    if path.startswith("/"):
        resolved = posixpath.normpath(path.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(base_dir, path))

    if resolved.startswith(".."):
        # A reference that climbs above the export root can never resolve.
        return resolved

    # normpath renders the export root as "." -- the common case for a link
    # like "../" from a page one level down. Treating that as a literal path
    # component would make every homepage link look broken.
    if resolved in {".", "./", ""}:
        resolved = ""

    if is_directory or not posixpath.splitext(resolved)[1]:
        resolved = posixpath.join(resolved, "index.html") if resolved else "index.html"

    # Strip a leading "./" or "/" only -- not with lstrip(), which would eat the
    # leading dot of a legitimate path such as ".well-known/".
    if resolved.startswith("./"):
        resolved = resolved[2:]
    return resolved.lstrip("/")


# ---------------------------------------------------------------------------
# Serving the export for browser validation
# ---------------------------------------------------------------------------
class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """A static file handler that resolves directories to index.html silently."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def send_head(self):
        path = self.translate_path(self.path)
        if Path(path).is_dir():
            index = Path(path) / "index.html"
            if index.is_file():
                self.path = self.path.rstrip("/") + "/index.html"
        return super().send_head()


class _ReusableServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    # Several pages load at once, each firing dozens of requests. The default
    # backlog of 5 refuses connections under that burst, which shows up as
    # ERR_CONNECTION_REFUSED errors that are the checker's fault, not the site's.
    request_queue_size = 256


class StaticSiteServer:
    """Serves the generated export over HTTP for validation."""

    def __init__(self, directory: Path, host: str = "127.0.0.1") -> None:
        self.directory = Path(directory)
        self.host = host
        self.port = 0
        self._server: _ReusableServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        handler = functools.partial(_QuietHandler, directory=str(self.directory))
        self._server = _ReusableServer((self.host, 0), handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("serving the static export at %s", self.base_url)
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def __enter__(self) -> "StaticSiteServer":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


async def validate_in_browser(
    output_dir: Path,
    pages: list[str],
    options,
    report: ValidationReport,
    *,
    limit: int = 25,
) -> ValidationReport:
    """Load exported pages in Chromium and collect runtime errors.

    Pages load in parallel, up to the render concurrency. *pages* should
    already be a representative choice: this checks the first *limit*.
    """
    from app.services.browser_renderer import BrowserRenderer

    sample = pages[:limit] if limit else pages
    if not sample:
        return report

    with StaticSiteServer(output_dir) as server:
        async with BrowserRenderer(options, base_url=server.base_url, timeout_ms=30_000,
                                   retries=0) as renderer:
            rendered = await asyncio.gather(
                *(renderer.render(f"{server.base_url}/{path.lstrip('/')}") for path in sample),
                return_exceptions=True,
            )

    for relative_path, page in zip(sample, rendered):
        if isinstance(page, BaseException):
            report.page_errors.append({"page": relative_path, "text": f"could not load: {page}"})
            continue
        report.pages_checked_in_browser += 1

        for message in page.console_errors:
            if message.level == "error":
                report.console_errors.append({
                    "page": relative_path,
                    "text": message.text,
                    "location": message.location,
                })

        for error in page.page_errors:
            report.page_errors.append({"page": relative_path, "text": error})

        for resource in page.resources:
            if resource.failed or (resource.status or 0) >= 400:
                report.failed_requests.append({
                    "page": relative_path,
                    "url": resource.url,
                    "status": resource.status,
                    "failure": resource.failure,
                    "base_url": server.base_url,
                })

    logger.info(
        "browser validation: %d pages, %d console errors, %d page errors, %d failed requests",
        report.pages_checked_in_browser, len(report.console_errors),
        len(report.page_errors), len(report.failed_requests),
    )
    return report
