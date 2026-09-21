"""Download the site's assets and localise the stylesheets that reference them.

Sources of asset references, in order of reliability:

1. **What the browser actually fetched.** The renderer records every network
   request Chromium made, which is the only way to catch assets injected by
   JavaScript -- a slider's images, a font loaded by a script, a stylesheet
   appended at runtime.
2. **The rendered DOM.** ``src``, ``srcset``, ``href``, ``poster``, inline
   ``style`` backgrounds and the various ``data-*`` lazy-loading attributes.
3. **Inside stylesheets.** ``url()``, ``@import`` and ``image-set()``, followed
   recursively, because an imported stylesheet can import another and the
   fonts and background images at the bottom of that chain are what make the
   design look right.

External resources are classified and handled according to policy rather than
mirrored by default: pulling down Google Fonts, a maps tile server or somebody
else's CDN is usually neither necessary nor ours to do.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from app.config import ConversionOptions, ExternalResourcePolicy
from app.services.url_rewriter import AssetMap, extract_css_urls, rewrite_css_urls
from app.utils.filesystem import atomic_write_bytes
from app.utils.security import safe_join
from app.utils.urls import (
    DOCUMENT_EXTENSIONS,
    MEDIA_EXTENSIONS,
    ResourceClass,
    classify_url,
    normalise_url,
)

logger = logging.getLogger(__name__)

#: Content types that are stylesheets and therefore need recursive traversal.
_CSS_TYPES = ("text/css",)

#: Give up on a single asset beyond this size rather than filling the disk.
_MAX_ASSET_BYTES = 512 * 1024 * 1024

#: Stylesheets are read into memory so their URLs can be rewritten. A real one
#: is a few hundred KB; this only guards against something pathological.
_MAX_CSS_BYTES = 32 * 1024 * 1024


@dataclass(slots=True)
class AssetStats:
    """Counters for the conversion report."""

    downloaded: int = 0
    failed: int = 0
    skipped_external: int = 0
    bytes_written: int = 0
    stylesheets_rewritten: int = 0
    from_disk: int = 0
    """Assets copied straight out of the restored install rather than fetched."""
    by_kind: dict[str, int] = field(default_factory=dict)
    failures: list[tuple[str, str]] = field(default_factory=list)
    """``(url, reason)`` pairs, for the report's missing-assets section."""

    def record_kind(self, kind: str) -> None:
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1


def kind_for(url: str, content_type: str = "") -> str:
    """Classify an asset for the report's breakdown."""
    import posixpath
    from urllib.parse import urlsplit

    extension = posixpath.splitext(urlsplit(url).path)[1].lower()
    mime = (content_type or "").split(";", 1)[0].strip().lower()

    if extension == ".css" or mime == "text/css":
        return "css"
    if extension in {".js", ".mjs"} or "javascript" in mime:
        return "js"
    if extension in {".woff", ".woff2", ".ttf", ".otf", ".eot"} or mime.startswith("font/"):
        return "font"
    if extension == ".svg" or mime == "image/svg+xml":
        return "image"
    if mime.startswith("image/") or extension in {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".ico", ".apng"
    }:
        return "image"
    if extension in MEDIA_EXTENSIONS or mime.startswith(("video/", "audio/")):
        return "media"
    if extension in DOCUMENT_EXTENSIONS or mime == "application/pdf":
        return "document"
    return "other"


class AssetManager:
    """Fetches assets into the export directory and rewrites stylesheets."""

    def __init__(
        self,
        base_url: str,
        output_dir: Path,
        asset_map: AssetMap,
        options: ConversionOptions,
        *,
        site_hosts: set[str] | None = None,
        concurrency: int = 8,
        timeout: float = 60.0,
        retries: int = 2,
        document_root: Path | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.output_dir = Path(output_dir)
        self.map = asset_map
        self.options = options
        self.site_hosts = site_hosts or set()
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self.retries = retries
        self.document_root = Path(document_root) if document_root else None

        self.stats = AssetStats()
        self._seen: set[str] = set()
        self._digests: dict[str, str] = {}

    # -- queue building -----------------------------------------------------
    def should_localise(self, url: str) -> bool:
        """Whether *url* is an asset this export should download."""
        classification = classify_url(url, self.site_hosts)

        if classification is ResourceClass.LOCAL:
            import posixpath
            from urllib.parse import urlsplit

            extension = posixpath.splitext(urlsplit(url).path)[1].lower()
            if extension in MEDIA_EXTENSIONS and not self.options.download_media:
                return False
            if extension in DOCUMENT_EXTENSIONS and not self.options.download_documents:
                return False
            return True

        if classification is ResourceClass.EXTERNAL:
            return self.options.external_policy is ExternalResourcePolicy.DOWNLOAD

        # DYNAMIC endpoints and BLOCKED schemes are never fetched as assets.
        return False

    def register(self, url: str, *, content_type: str = "", source: str = "html") -> str | None:
        """Register an asset for download. Returns its output path, or ``None``.

        ``None`` means the reference must be left exactly as it is.
        """
        normalised = normalise_url(url, force_trailing_slash=False)
        if not normalised:
            return None
        if not self.should_localise(normalised):
            if classify_url(normalised, self.site_hosts) is ResourceClass.EXTERNAL:
                self.stats.skipped_external += 1
            return None

        record = self.map.add_asset(normalised, content_type=content_type, source=source)
        self._seen.add(normalised)
        return record.output_path

    def register_network_resources(self, resources) -> int:
        """Register assets Chromium actually requested while rendering.

        This is what catches JavaScript-injected assets, which no amount of
        HTML parsing would find. What counts as an asset is decided by
        request_policy, the same rules the renderer uses to block traffic, so
        the two can never disagree.
        """
        from app.services.request_policy import RequestAction, classify_request

        added = 0
        for resource in resources:
            if resource.failed or (resource.status or 0) >= 400:
                continue
            action = classify_request(
                resource.url, resource.resource_type,
                download_media=self.options.download_media,
            )
            if action is not RequestAction.SAVE:
                continue
            if self.register(resource.url, content_type=resource.content_type, source="network"):
                added += 1
        return added

    #: Filenames webpack gives to code split out of a bundle and fetched on
    #: demand -- Elementor's image-carousel.<hash>.bundle.min.js, for example.
    _CHUNK_SUFFIXES = (".bundle.min.js", ".bundle.js", ".chunk.min.js", ".chunk.js")

    def register_lazy_chunks(self) -> int:
        """Register the on-demand script chunks that sit beside registered scripts.

        A webpack runtime loads these only when the page needs them, so they
        appear in no HTML and are fetched only if the render happened to trigger
        them. A chunk that is missing from the export breaks its widget -- a
        carousel shows its slides stacked and never moves -- so every chunk in
        the folder of a script the site uses is included. Returns the number
        added.
        """
        from urllib.parse import urljoin

        added = 0
        folders: set[Path] = set()
        for url in list(self.map.assets):
            if not url.lower().split("?", 1)[0].endswith(".js"):
                continue
            source = self._local_source(url)
            if source is None or source.parent in folders:
                continue
            folders.add(source.parent)
            try:
                siblings = sorted(source.parent.iterdir())
            except OSError:
                continue
            chunks = [
                s for s in siblings
                if s.name.lower().endswith(self._CHUNK_SUFFIXES) and s.is_file()
            ]
            # Builders ship each chunk minified and not; the minified set is the
            # one a production site loads, so the other is dead weight.
            minified = [s for s in chunks if s.name.lower().endswith(".min.js")]
            for sibling in minified or chunks:
                chunk_url = urljoin(url.split("?", 1)[0], sibling.name)
                if self.map.asset(chunk_url) is None and self.register(
                    chunk_url, content_type="application/javascript", source="chunk"
                ):
                    added += 1
        return added

    # -- downloading --------------------------------------------------------
    async def download_all(self, progress=None) -> AssetStats:
        """Download every registered asset, following stylesheets recursively."""
        limits = httpx.Limits(
            max_connections=self.concurrency * 2, max_keepalive_connections=self.concurrency
        )
        semaphore = asyncio.Semaphore(self.concurrency)

        async with httpx.AsyncClient(
            timeout=self.timeout, limits=limits, follow_redirects=True,
            headers={"User-Agent": "wp-static-converter/1.0"},
        ) as client:
            # Stylesheets can reveal new assets, so work in waves until the set
            # of known assets stops growing.
            processed: set[str] = set()
            wave = 0

            while True:
                pending = [u for u in self.map.assets if u not in processed]
                if not pending:
                    break
                wave += 1
                if wave > 8:
                    logger.warning("stopping stylesheet traversal after %d waves", wave)
                    break

                logger.info("asset wave %d: %d file(s)", wave, len(pending))
                processed.update(pending)

                total = len(pending)
                done = 0

                async def fetch(url: str) -> None:
                    nonlocal done
                    async with semaphore:
                        await self._download_one(client, url)
                    done += 1
                    if progress and (done % 5 == 0 or done == total):
                        progress(f"Collecting assets ({done}/{total})", done / total)

                await asyncio.gather(*(fetch(u) for u in pending), return_exceptions=True)

        logger.info(
            "assets: %d collected (%d straight from disk), %d failed, "
            "%d external preserved, %.1f MiB",
            self.stats.downloaded, self.stats.from_disk, self.stats.failed,
            self.stats.skipped_external, self.stats.bytes_written / 1048576,
        )
        return self.stats

    async def _download_one(self, client: httpx.AsyncClient, url: str) -> None:
        record = self.map.asset(url)
        if record is None or record.downloaded:
            return

        # Disk first: the restored install already holds most of these files.
        local = self._local_source(url)
        if local is not None and self._copy_local(local, record):
            return

        last_error = record.error or ""
        for attempt in range(1, self.retries + 2):
            try:
                if await self._stream_asset(client, url, record):
                    return
                last_error = record.error or "download failed"
                if record.retryable is False:
                    break
                if attempt <= self.retries:
                    await asyncio.sleep(0.5 * attempt)
            except (httpx.HTTPError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt <= self.retries:
                    await asyncio.sleep(0.5 * attempt)

        record.error = last_error or "download failed"
        self.stats.failed += 1
        self.stats.failures.append((url, record.error))
        logger.warning("asset failed: %s (%s)", url, record.error)

    def _local_source(self, url: str) -> Path | None:
        """The file on disk backing *url*, when the web server would just serve it.

        Nearly every asset in a WordPress export is a static file sitting in the
        restored install: an upload, a theme stylesheet, a plugin script. Asking
        the temporary PHP server for it over HTTP means a socket, a PHP request
        and a copy through the loopback stack, thousands of times over, when the
        bytes are already on this disk.

        Only files that genuinely exist and are not PHP qualify. Anything
        generated at request time still goes over HTTP, so correctness does not
        depend on this shortcut.
        """
        if self.document_root is None:
            return None

        parts = urlsplit(url)
        if parts.hostname not in {None, ""} and not classify_url(url, self.site_hosts) is ResourceClass.LOCAL:
            return None

        path = unquote(parts.path or "")
        if not path or path.endswith("/"):
            return None
        if path.lower().endswith((".php", ".phtml")):
            return None  # generated, not served as a file

        try:
            candidate = safe_join(self.document_root, path.lstrip("/"))
        except Exception:
            return None

        try:
            return candidate if candidate.is_file() else None
        except OSError:
            return None

    def _copy_local(self, source: Path, record) -> bool:
        """Copy an asset straight from the restored install. Returns success."""
        target = safe_join(self.output_dir, record.output_path)
        if target.is_dir():
            record.error = "a directory already occupies this path"
            record.retryable = False
            return False

        if not record.content_type:
            import mimetypes

            record.content_type = mimetypes.guess_type(source.name)[0] or ""

        if not _is_css(str(source), record.content_type):
            # Everything but stylesheets goes into the export unchanged, so on
            # the same drive a hard link does the job without copying a byte:
            # thousands of images cost milliseconds instead of minutes. Output
            # files are only ever replaced, never edited in place, so the
            # install's copy cannot be changed through the link.
            try:
                size = source.stat().st_size
                if size <= _MAX_ASSET_BYTES:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        target.unlink()
                    os.link(source, target)
                    record.downloaded = True
                    record.size = size
                    self.stats.downloaded += 1
                    self.stats.from_disk += 1
                    self.stats.bytes_written += size
                    self.stats.record_kind(kind_for(record.url, record.content_type))
                    return True
            except OSError:
                pass  # another drive, or links unsupported: copy instead

        try:
            data = source.read_bytes()
        except OSError as exc:
            logger.debug("local copy failed for %s: %s", source, exc)
            return False

        if len(data) > _MAX_ASSET_BYTES:
            record.error = f"asset is larger than {_MAX_ASSET_BYTES // 1048576} MiB"
            record.retryable = False
            return False

        if _is_css(str(source), record.content_type):
            # Stylesheets still need their URLs rewritten and their own
            # dependencies queued, exactly as over HTTP.
            data = self._localise_stylesheet_sync(record.url, data, record)

        atomic_write_bytes(target, data)

        record.downloaded = True
        record.size = len(data)
        record.digest = hashlib.sha1(data).hexdigest()

        self.stats.downloaded += 1
        self.stats.from_disk += 1
        self.stats.bytes_written += len(data)
        self.stats.record_kind(kind_for(record.url, record.content_type))
        return True

    def _localise_stylesheet_sync(self, css_url: str, content: bytes, record) -> bytes:
        """Synchronous twin of :meth:`_localise_stylesheet`, for disk copies."""
        try:
            text = content.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = content.decode("latin-1")
            encoding = "latin-1"

        for dependency in extract_css_urls(text, css_url):
            self.register(dependency, source="css")

        import posixpath

        css_dir = posixpath.dirname(record.output_path)

        def resolve(absolute: str) -> str | None:
            target = self.map.asset_path(absolute)
            if target is None:
                return None
            return posixpath.relpath(target, start=css_dir or ".")

        rewritten, count = rewrite_css_urls(text, css_url, resolve)
        if count:
            self.stats.stylesheets_rewritten += 1
        return rewritten.encode(encoding, errors="replace")

    async def _stream_asset(self, client: httpx.AsyncClient, url: str, record) -> bool:
        """Fetch one asset, streaming it to disk. Returns True on success.

        Streaming rather than reading ``response.content`` matters as soon as a
        site has real media in it: a WordPress backup can easily hold multi-
        gigabyte video, and buffering each asset whole would size the tool's
        memory use to the largest file in the library. Only stylesheets are
        held in memory, because they have to be parsed and rewritten, and they
        are small.
        """
        target = safe_join(self.output_dir, record.output_path)
        if target.is_dir():
            # A page already owns this path as a directory. Writing a file of
            # the same name is impossible on Windows and would shadow the page
            # elsewhere.
            record.error = "a directory already occupies this path"
            record.retryable = False
            return False
        target.parent.mkdir(parents=True, exist_ok=True)

        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                record.error = f"HTTP {response.status_code}"
                # A 404 will not become a 200 on retry; a 5xx might.
                record.retryable = response.status_code >= 500
                return False

            content_type = response.headers.get("content-type", "")
            record.content_type = record.content_type or content_type

            # Reject an oversized asset from its header, before any of it is
            # transferred, when the server tells us the size up front.
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > _MAX_ASSET_BYTES:
                record.error = (
                    f"asset is {int(declared) // 1048576} MiB, over the "
                    f"{_MAX_ASSET_BYTES // 1048576} MiB limit"
                )
                record.retryable = False
                return False

            if _is_css(url, content_type):
                body = await response.aread()
                if len(body) > _MAX_CSS_BYTES:
                    record.error = f"stylesheet is larger than {_MAX_CSS_BYTES // 1048576} MiB"
                    record.retryable = False
                    return False
                body = await self._localise_stylesheet(url, body, record)
                atomic_write_bytes(target, body)
                size, digest = len(body), hashlib.sha1(body).hexdigest()
            else:
                size, digest = await self._write_stream(response, target, record)
                if size is None:
                    return False

        record.downloaded = True
        record.size = size
        record.digest = digest

        self.stats.downloaded += 1
        self.stats.bytes_written += size
        self.stats.record_kind(kind_for(url, record.content_type))
        return True

    async def _write_stream(self, response, target, record):
        """Write a streaming response to *target*, enforcing the size cap."""
        digest = hashlib.sha1()
        size = 0
        temporary = target.with_name(target.name + ".part")

        try:
            with temporary.open("wb") as out:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > _MAX_ASSET_BYTES:
                        out.close()
                        temporary.unlink(missing_ok=True)
                        record.error = (
                            f"asset exceeded the {_MAX_ASSET_BYTES // 1048576} MiB limit "
                            "while downloading"
                        )
                        record.retryable = False
                        return None, ""
                    digest.update(chunk)
                    out.write(chunk)
            temporary.replace(target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

        return size, digest.hexdigest()

    async def _localise_stylesheet(self, css_url: str, content: bytes, record) -> bytes:
        """Register a stylesheet's dependencies and rewrite its URLs.

        The rewritten paths are relative to the stylesheet's own location in the
        export, not to the page including it, which is how CSS resolves ``url()``.
        """
        try:
            text = content.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            # Legacy stylesheets are occasionally latin-1; never fail on one.
            text = content.decode("latin-1")
            encoding = "latin-1"

        for dependency in extract_css_urls(text, css_url):
            self.register(dependency, source="css")

        import posixpath

        css_dir = posixpath.dirname(record.output_path)

        def resolve(absolute: str) -> str | None:
            target = self.map.asset_path(absolute)
            if target is None:
                return None  # external or excluded: leave the URL alone
            return posixpath.relpath(target, start=css_dir or ".")

        rewritten, count = rewrite_css_urls(text, css_url, resolve)
        if count:
            self.stats.stylesheets_rewritten += 1
        return rewritten.encode(encoding, errors="replace")


def _is_css(url: str, content_type: str) -> bool:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime in _CSS_TYPES:
        return True
    from urllib.parse import urlsplit
    import posixpath

    return posixpath.splitext(urlsplit(url).path)[1].lower() == ".css"


async def write_inline_stylesheet_assets(
    manager: AssetManager, css_text: str, page_url: str
) -> None:
    """Register assets referenced from an inline ``<style>`` block."""
    for dependency in extract_css_urls(css_text, page_url):
        manager.register(dependency, source="css")
