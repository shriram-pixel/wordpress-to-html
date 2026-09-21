"""Render pages with Chromium and capture the final DOM.

This is the stage that makes the export faithful. Rather than reimplementing
Elementor, Divi, WPBakery, Gutenberg or any other builder, the restored site is
rendered by a real browser and the resulting DOM is captured after JavaScript
has run. Whatever WordPress, the theme and the builder produced is what gets
saved.

Readiness
---------
Knowing *when* a page is finished is the hard part. ``networkidle`` alone is
unreliable: analytics beacons, chat widgets, video embeds and open WebSockets
keep connections alive indefinitely, so waiting for it either returns too early
or times out. Several signals are combined instead:

* ``domcontentloaded`` -- the document is parsed
* ``load``             -- subresources referenced by the initial HTML are in
* a quiet period with no *new* in-flight requests, tracked directly
* fonts finished loading (``document.fonts.ready``)
* images decoded, including ones inserted by script
* the DOM no longer mutating
* a settle delay after scrolling, for lazy-loaded content

Each has a bounded timeout, so a page that never goes quiet is still captured
rather than failing the job.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import ConversionOptions
from urllib.parse import urlsplit

from app.utils.urls import is_local_origin

logger = logging.getLogger(__name__)

#: Injected before any page script runs. Neutralises the handful of things that
#: make a capture non-deterministic or that would record a visitor's state.
_INIT_SCRIPT = r"""
(() => {
  // Report a normal, non-automated browser: some themes and plugins change
  // their markup (or refuse to render) when they detect automation.
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  } catch (e) {}

  // Freeze time-dependent randomness so two captures of the same page differ
  // only where the content genuinely differs. This makes the visual diff
  // meaningful instead of noisy.
  const seed = 0x2545f491;
  let state = seed;
  Math.random = function () {
    state ^= state << 13; state ^= state >>> 17; state ^= state << 5;
    return ((state >>> 0) % 1000000) / 1000000;
  };

  // Stop pages that ask for permissions or open dialogs during capture.
  window.alert = function () {};
  window.confirm = function () { return true; };
  window.prompt = function () { return null; };

  // Mark <script> elements that are inserted by *other scripts* rather than by
  // the HTML parser.
  //
  // Capturing the final DOM serialises those injected scripts into the saved
  // HTML. On the exported page they would then load immediately, a second
  // time, and out of order -- because the code that injected them is still
  // present and injects them again. WordPress's emoji loader is the clearest
  // example: its inline detector sets window._wpemojiSettings.supports and
  // only then appends wp-emoji-release.min.js, so a serialised copy of that
  // script runs before the data it depends on exists and throws.
  //
  // Parser-inserted nodes never travel through these DOM methods, so patching
  // them distinguishes the two cases exactly. A MutationObserver could not:
  // it reports parser insertions identically.
  const markInjected = (node) => {
    try {
      if (!node || node.nodeType !== 1) return;
      if (node.tagName === 'SCRIPT') node.setAttribute('data-wpsc-injected', '1');
      if (node.querySelectorAll) {
        node.querySelectorAll('script').forEach((s) => s.setAttribute('data-wpsc-injected', '1'));
      }
    } catch (e) {}
  };

  ['appendChild', 'insertBefore', 'replaceChild'].forEach((name) => {
    const original = Node.prototype[name];
    if (!original) return;
    Node.prototype[name] = function (...args) {
      markInjected(args[0]);
      return original.apply(this, args);
    };
  });

  ['append', 'prepend', 'after', 'before', 'replaceWith'].forEach((name) => {
    const original = Element.prototype[name];
    if (!original) return;
    Element.prototype[name] = function (...args) {
      args.forEach(markInjected);
      return original.apply(this, args);
    };
  });

  // Record lazy-loading libraries' work so nothing is missed: many of them
  // swap data-src into src only when an element scrolls into view.
  window.__wpscLazyNudge = function () {
    const selectors = [
      'img[data-src]', 'img[data-lazy-src]', 'img[data-original]',
      'iframe[data-src]', 'video[data-src]', 'source[data-src]',
      '[data-bg]', '[data-background-image]', '[data-bgset]'
    ];
    document.querySelectorAll(selectors.join(',')).forEach((el) => {
      const src = el.getAttribute('data-src') || el.getAttribute('data-lazy-src') ||
                  el.getAttribute('data-original');
      if (src && !el.getAttribute('src')) { el.setAttribute('src', src); }

      const srcset = el.getAttribute('data-srcset') || el.getAttribute('data-lazy-srcset');
      if (srcset && !el.getAttribute('srcset')) { el.setAttribute('srcset', srcset); }

      const bg = el.getAttribute('data-bg') || el.getAttribute('data-background-image');
      if (bg && !el.style.backgroundImage) { el.style.backgroundImage = 'url(' + bg + ')'; }
    });
  };
})();
"""

#: Runs in the page to work out whether it has settled.
_READINESS_SCRIPT = r"""
() => {
  const images = Array.from(document.images);
  const pending = images.filter((img) => !img.complete && img.loading !== 'lazy');
  return {
    readyState: document.readyState,
    fontsReady: document.fonts ? document.fonts.status === 'loaded' : true,
    pendingImages: pending.length,
    totalImages: images.length,
    height: document.documentElement.scrollHeight,
    nodes: document.getElementsByTagName('*').length
  };
}
"""


#: Hosts that exist only to observe visitors. Blocking them during capture is
#: the single largest speed win available on a real site.
#:
#: They are never part of the export -- external resources keep their original
#: URLs and are not downloaded -- so fetching them while rendering buys nothing.
#: Worse, tag managers and analytics keep firing beacons indefinitely, so the
#: page never goes network-quiet and every capture burns its full settle budget.
#: On a 600-page Elementor site that was the difference between ~30s and ~7s per
#: page.
#:
#: Fonts, stylesheets and images are deliberately *not* here: they affect layout
#: and therefore the screenshot.
_TRACKING_HOSTS = frozenset({
    "google-analytics.com", "www.google-analytics.com", "ssl.google-analytics.com",
    "googletagmanager.com", "www.googletagmanager.com",
    "googletagservices.com", "googlesyndication.com", "pagead2.googlesyndication.com",
    "doubleclick.net", "stats.g.doubleclick.net", "analytics.google.com",
    "connect.facebook.net", "facebook.com", "www.facebook.com", "facebook.net",
    "hotjar.com", "static.hotjar.com", "script.hotjar.com", "in.hotjar.com",
    "clarity.ms", "www.clarity.ms",
    "segment.com", "cdn.segment.com", "api.segment.io",
    "mixpanel.com", "cdn.mxpnl.com", "api.mixpanel.com",
    "matomo.cloud", "cdn.matomo.cloud",
    "newrelic.com", "js-agent.newrelic.com", "bam.nr-data.net",
    "sentry.io", "browser.sentry-cdn.com",
    "intercom.io", "widget.intercom.io", "js.intercomcdn.com",
    "crisp.chat", "client.crisp.chat",
    "tawk.to", "embed.tawk.to",
    "drift.com", "js.driftt.com",
    "hubspot.com", "js.hs-scripts.com", "js.hsadspixel.net", "track.hubspot.com",
    "zdassets.com", "static.zdassets.com",
    "cloudflareinsights.com", "static.cloudflareinsights.com",
    "tiktok.com", "analytics.tiktok.com",
    "linkedin.com", "snap.licdn.com", "px.ads.linkedin.com",
    "bing.com", "bat.bing.com",
    "criteo.com", "criteo.net", "taboola.com", "outbrain.com",
    "addthis.com", "sharethis.com", "addtoany.com",
    "quantserve.com", "scorecardresearch.com",
    "adservice.google.com", "adservice.google.co.in",
})

#: Chromium request types that never affect what a page looks like.
_TRACKING_RESOURCE_TYPES = frozenset({"ping", "beacon", "csp_report"})


def _is_tracking_request(url: str, resource_type: str) -> bool:
    """Whether a request is pure telemetry and safe to block during capture."""
    if resource_type in _TRACKING_RESOURCE_TYPES:
        return True
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    if host in _TRACKING_HOSTS:
        return True
    # Match subdomains without matching an unrelated host that merely ends in
    # the same letters.
    return any(host.endswith("." + blocked) for blocked in _TRACKING_HOSTS)


#: Fixed, because Python's guess comes from the Windows registry there, where
#: .js or .css is sometimes text/plain -- and a browser refuses a stylesheet or
#: module script served that way.
_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".ico": "image/x-icon", ".bmp": "image/bmp",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
    ".otf": "font/otf", ".eot": "application/vnd.ms-fontobject",
    ".mp4": "video/mp4", ".webm": "video/webm", ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".pdf": "application/pdf", ".xml": "application/xml; charset=utf-8",
    ".txt": "text/plain; charset=utf-8", ".html": "text/html; charset=utf-8",
}


class _DiskFiles:
    """Static files of the restored install, read from disk for the browser.

    Only plain files are served this way -- never PHP, never anything outside
    the install -- so everything WordPress generates still comes from WordPress.
    Small files are kept in memory: a site's theme and plugin assets are shared
    by every page, and re-reading them hundreds of times is wasted work.
    """

    _NEVER = (".php", ".phtml", ".htaccess", ".ini", ".log", ".sql")

    def __init__(self, root: Path, *, max_cached_bytes: int = 192 * 1024 * 1024) -> None:
        self.root = root
        self.max_cached_bytes = max_cached_bytes
        self._cache: dict[str, tuple[bytes, str]] = {}
        self._cached_bytes = 0

    def lookup(self, request, base_url: str) -> tuple[bytes, str] | None:
        import mimetypes
        from urllib.parse import unquote

        if request.method != "GET" or not base_url or not request.url.startswith(base_url + "/"):
            return None
        path = unquote(urlsplit(request.url).path or "")
        if not path or path.endswith("/") or path.lower().endswith(self._NEVER):
            return None
        cached = self._cache.get(path)
        if cached is not None:
            return cached
        candidate = (self.root / path.lstrip("/")).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None  # a ../ escape; let the server refuse it
        if candidate.name.lower() == "wp-config.php" or not candidate.is_file():
            return None
        try:
            body = candidate.read_bytes()
        except OSError:
            return None
        content_type = (
            _CONTENT_TYPES.get(candidate.suffix.lower())
            or mimetypes.guess_type(candidate.name)[0]
            or "application/octet-stream"
        )
        result = (body, content_type)
        if len(body) <= 2 * 1024 * 1024 and self._cached_bytes + len(body) <= self.max_cached_bytes:
            self._cache[path] = result
            self._cached_bytes += len(body)
        return result


@dataclass(slots=True)
class NetworkResource:
    """One resource the browser actually requested while rendering."""

    url: str
    status: int | None = None
    content_type: str = ""
    resource_type: str = ""
    """Chromium's classification: document, stylesheet, script, image, font..."""
    from_cache: bool = False
    failed: bool = False
    failure: str = ""
    size: int = 0


@dataclass(slots=True)
class ConsoleMessage:
    level: str
    text: str
    location: str = ""


@dataclass(slots=True)
class RenderedPage:
    """Everything captured from one page render."""

    url: str
    final_url: str
    status: int | None
    html: str
    title: str = ""
    links: list[str] = field(default_factory=list)
    resources: list[NetworkResource] = field(default_factory=list)
    console_errors: list[ConsoleMessage] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    desktop_screenshot: Path | None = None
    mobile_screenshot: Path | None = None
    duration_seconds: float = 0.0
    attempts: int = 1
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400 and bool(self.html)


class BrowserRenderer:
    """Manages one Chromium instance and renders pages through it.

    Used as an async context manager::

        async with BrowserRenderer(options) as renderer:
            page = await renderer.render("http://127.0.0.1:8080/about/")
    """

    def __init__(
        self,
        options: ConversionOptions,
        *,
        screenshot_dir: Path | None = None,
        base_url: str = "",
        timeout_ms: int = 45_000,
        retries: int = 2,
        block_tracking: bool = True,
        document_root: Path | None = None,
    ) -> None:
        self.options = options
        self.document_root = Path(document_root).resolve() if document_root else None
        self._disk = _DiskFiles(self.document_root) if self.document_root else None
        self._served_from_disk = 0
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self.base_url = base_url.rstrip("/")
        self.timeout_ms = timeout_ms
        self.retries = retries
        self.block_tracking = block_tracking
        self._blocked = 0

        self._playwright = None
        self._browser = None
        self._context = None
        self._semaphore = asyncio.Semaphore(max(1, options.render_concurrency))

    # -- lifecycle ----------------------------------------------------------
    async def __aenter__(self) -> "BrowserRenderer":
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                # Background throttling would stall pages rendered in parallel.
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--force-color-profile=srgb",
                # Deterministic rendering for the visual comparison.
                "--font-render-hinting=none",
                "--disable-lcd-text",
            ],
        )
        width, height = self.options.desktop_viewport
        self._context = await self._browser.new_context(
            viewport={"width": width, "height": height},
            device_scale_factor=1,
            ignore_https_errors=True,
            java_script_enabled=self.options.capture_javascript,
            # A plain desktop UA: the site should render its desktop layout.
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            reduced_motion="reduce",   # animations settle immediately
            locale="en-US",
        )
        self._context.set_default_timeout(self.timeout_ms)
        await self._context.add_init_script(_INIT_SCRIPT)

        if self.block_tracking or self._disk is not None:
            async def _route(route, request):
                try:
                    if self.block_tracking and _is_tracking_request(request.url, request.resource_type):
                        self._blocked += 1
                        await route.abort()
                        return
                    # Static files straight from the restored install. Any route
                    # disables Chromium's HTTP cache, so without this every page
                    # re-fetches every stylesheet, script and image through the
                    # PHP workers -- queueing behind the page renders themselves.
                    served = self._disk.lookup(request, self.base_url) if self._disk else None
                    if served is not None:
                        body, content_type = served
                        self._served_from_disk += 1
                        await route.fulfill(
                            status=200, body=body,
                            headers={"Content-Type": content_type,
                                     "Access-Control-Allow-Origin": "*"},
                        )
                    else:
                        await route.continue_()
                except Exception:
                    # A route can outlive its page during navigation; losing the
                    # handler must never fail the render.
                    pass

            # Registered on the context, so every page shares one handler
            # instead of paying to install it per navigation.
            await self._context.route("**/*", _route)
        logger.info(
            "Chromium started (concurrency %d%s)",
            self.options.render_concurrency,
            ", analytics blocked" if self.block_tracking else "",
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception as exc:
                    logger.debug("error closing browser object: %s", exc)
        if self._blocked:
            logger.info("blocked %d tracking request(s) during rendering", self._blocked)
        if self._served_from_disk:
            logger.info("served %d static file(s) straight from disk", self._served_from_disk)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.debug("error stopping playwright: %s", exc)

    # -- rendering ----------------------------------------------------------
    async def render(self, url: str, *, capture_screenshots=False) -> RenderedPage:
        """Render *url*, retrying on transient failures.

        Refuses any address that is not the local render server. Page URLs are
        already forced onto the local origin when they are queued; this is the
        backstop that makes "the crawler never touches the live site" a
        guarantee rather than an assumption about the queue's contents.
        """
        if self.base_url and not is_local_origin(url, self.base_url):
            logger.error(
                "refusing to render %s: it is not on the local server (%s)", url, self.base_url
            )
            return RenderedPage(
                url=url, final_url=url, status=None, html="",
                page_errors=[
                    "refused: this URL is not on the local render server, and "
                    "fetching it would have contacted the live website"
                ],
            )

        async with self._semaphore:
            last_error: Exception | None = None
            for attempt in range(1, self.retries + 2):
                try:
                    page = await self._render_once(url, capture_screenshots, attempt)
                    page.attempts = attempt
                    return page
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "render attempt %d/%d failed for %s: %s",
                        attempt, self.retries + 1, url, str(exc)[:200],
                    )
                    if attempt <= self.retries:
                        await asyncio.sleep(min(2 ** attempt, 8))

            return RenderedPage(
                url=url, final_url=url, status=None, html="",
                attempts=self.retries + 1,
                page_errors=[f"{type(last_error).__name__}: {last_error}"],
            )

    async def _render_once(self, url: str, capture_screenshots, attempt: int) -> RenderedPage:
        started = time.monotonic()
        page = await self._context.new_page()

        resources: dict[str, NetworkResource] = {}
        console_errors: list[ConsoleMessage] = []
        page_errors: list[str] = []
        inflight: set[str] = set()
        last_activity = {"at": time.monotonic()}

        # -- instrumentation ------------------------------------------------
        def on_request(request) -> None:
            inflight.add(request.url)
            last_activity["at"] = time.monotonic()

        async def on_response(response) -> None:
            last_activity["at"] = time.monotonic()
            try:
                headers = await response.all_headers()
            except Exception:
                headers = {}
            entry = resources.get(response.url) or NetworkResource(url=response.url)
            entry.status = response.status
            entry.content_type = headers.get("content-type", "")
            entry.from_cache = response.from_service_worker
            try:
                entry.size = int(headers.get("content-length") or 0)
            except ValueError:
                entry.size = 0
            resources[response.url] = entry

        def on_request_finished(request) -> None:
            inflight.discard(request.url)
            last_activity["at"] = time.monotonic()
            entry = resources.get(request.url) or NetworkResource(url=request.url)
            entry.resource_type = request.resource_type
            resources[request.url] = entry

        def on_request_failed(request) -> None:
            inflight.discard(request.url)
            last_activity["at"] = time.monotonic()
            entry = resources.get(request.url) or NetworkResource(url=request.url)
            entry.resource_type = request.resource_type
            entry.failed = True
            entry.failure = (request.failure or "")[:200] if request.failure else "request failed"
            resources[request.url] = entry

        def on_console(message) -> None:
            if message.type in {"error", "warning"}:
                location = message.location or {}
                console_errors.append(ConsoleMessage(
                    level=message.type,
                    text=message.text[:500],
                    location=f"{location.get('url', '')}:{location.get('lineNumber', '')}",
                ))

        def on_page_error(error) -> None:
            page_errors.append(str(error)[:500])

        page.on("request", on_request)
        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        page.on("requestfinished", on_request_finished)
        page.on("requestfailed", on_request_failed)
        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.dismiss()))

        warnings: list[str] = []

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            status = response.status if response else None

            await self._wait_for_load(page, warnings)

            if self.options.capture_lazy_assets:
                await self._scroll_through(page)

            await self._wait_until_settled(page, inflight, last_activity, warnings)

            # One last nudge for lazy loaders that only react to a scroll event.
            try:
                await page.evaluate("() => window.__wpscLazyNudge && window.__wpscLazyNudge()")
                await page.wait_for_timeout(self.options.extra_settle_ms)
            except Exception:
                pass

            html = await page.content()
            title = await page.title()
            links = await self._collect_links(page)

            desktop_shot = mobile_shot = None
            # Either a flag, or a callback that decides from the rendered HTML
            # -- how the pipeline screenshots one page per template.
            take_shot = (
                capture_screenshots(html) if callable(capture_screenshots) else capture_screenshots
            )
            if take_shot and self.screenshot_dir:
                desktop_shot, mobile_shot = await self._capture_screenshots(page, url)

            return RenderedPage(
                url=url,
                final_url=page.url,
                status=status,
                html=html,
                title=title,
                links=links,
                resources=list(resources.values()),
                console_errors=console_errors,
                page_errors=page_errors,
                desktop_screenshot=desktop_shot,
                mobile_screenshot=mobile_shot,
                duration_seconds=time.monotonic() - started,
                warnings=warnings,
            )
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # -- readiness helpers --------------------------------------------------
    async def _wait_for_load(self, page, warnings: list[str]) -> None:
        """Wait for the ``load`` event, but do not fail the page if it never comes."""
        try:
            await page.wait_for_load_state("load", timeout=min(self.timeout_ms, 30_000))
        except Exception:
            warnings.append("the load event did not fire; captured after DOMContentLoaded")

    async def _wait_until_settled(
        self, page, inflight: set[str], last_activity: dict, warnings: list[str]
    ) -> None:
        """Wait for a quiet network, stable DOM, loaded fonts and decoded images.

        Rather than Playwright's ``networkidle`` -- which gives up on any page
        holding a long-lived connection -- this tracks in-flight requests
        directly and accepts a small number of stragglers.
        """
        deadline = time.monotonic() + (self.timeout_ms / 1000.0)
        quiet_required = max(0.35, self.options.extra_settle_ms / 1000.0)
        previous_signature: tuple | None = None
        stable_since: float | None = None

        while time.monotonic() < deadline:
            quiet_for = time.monotonic() - last_activity["at"]

            try:
                state: dict[str, Any] = await page.evaluate(_READINESS_SCRIPT)
            except Exception:
                # Navigation or a closed context mid-poll: nothing more to wait for.
                return

            signature = (state.get("height"), state.get("nodes"))
            if signature == previous_signature:
                stable_since = stable_since or time.monotonic()
            else:
                stable_since = None
                previous_signature = signature

            dom_stable = stable_since is not None and (time.monotonic() - stable_since) >= quiet_required
            network_quiet = quiet_for >= quiet_required and len(inflight) <= 2
            images_done = state.get("pendingImages", 0) == 0
            fonts_done = bool(state.get("fontsReady", True))

            if dom_stable and network_quiet and images_done and fonts_done:
                return

            await asyncio.sleep(0.15)

        warnings.append(
            f"page did not fully settle within {self.timeout_ms / 1000:.0f}s; "
            f"captured with {len(inflight)} request(s) still in flight"
        )

    async def _scroll_through(self, page) -> None:
        """Scroll the page so lazy-loaded content is triggered.

        Top to bottom in viewport-sized steps, then back to the top, which is
        what most lazy-loading libraries need in order to fire. Returning to the
        top also matters for the screenshot: a page captured mid-scroll shows a
        sticky header in the wrong state.
        """
        try:
            await page.evaluate(
                """
                async () => {
                  const step = Math.max(200, Math.floor(window.innerHeight * 0.8));
                  const pause = (ms) => new Promise((r) => setTimeout(r, ms));

                  let last = -1;
                  for (let y = 0; y < 200000; y += step) {
                    window.scrollTo(0, y);
                    await pause(60);
                    const height = document.documentElement.scrollHeight;
                    // Stop once the bottom is reached and the page stopped growing
                    // (infinite-scroll pages would otherwise never terminate).
                    if (y + window.innerHeight >= height) {
                      if (height === last) break;
                      last = height;
                    }
                  }

                  window.__wpscLazyNudge && window.__wpscLazyNudge();
                  await pause(120);

                  // Back to the top, in steps, so reveal-on-scroll animations
                  // that only trigger upward also run.
                  for (let y = document.documentElement.scrollHeight; y >= 0; y -= step * 2) {
                    window.scrollTo(0, y);
                    await pause(30);
                  }
                  window.scrollTo(0, 0);
                  await pause(120);
                }
                """
            )
        except Exception as exc:
            logger.debug("scroll pass failed: %s", exc)

    async def _collect_links(self, page) -> list[str]:
        """Absolute hrefs of every anchor in the rendered DOM."""
        try:
            return await page.evaluate(
                """
                () => Array.from(document.querySelectorAll('a[href]'))
                  .map((a) => a.href)
                  .filter((h) => h && !h.startsWith('javascript:') && !h.startsWith('mailto:'))
                """
            )
        except Exception:
            return []

    async def _capture_screenshots(self, page, url: str) -> tuple[Path | None, Path | None]:
        """Full-page desktop and mobile screenshots of the live page."""
        assert self.screenshot_dir is not None
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        slug = _slug_for(url, self.base_url)

        desktop_path = self.screenshot_dir / f"{slug}.original.desktop.png"
        mobile_path = self.screenshot_dir / f"{slug}.original.mobile.png"

        desktop_result = mobile_result = None
        try:
            await page.screenshot(path=str(desktop_path), full_page=True, animations="disabled")
            desktop_result = desktop_path
        except Exception as exc:
            logger.debug("desktop screenshot failed for %s: %s", url, exc)

        if self.options.mobile_validation:
            try:
                width, height = self.options.mobile_viewport
                await page.set_viewport_size({"width": width, "height": height})
                await page.wait_for_timeout(350)   # let responsive CSS apply
                await self._scroll_through(page)
                await page.screenshot(path=str(mobile_path), full_page=True, animations="disabled")
                mobile_result = mobile_path
            except Exception as exc:
                logger.debug("mobile screenshot failed for %s: %s", url, exc)
            finally:
                width, height = self.options.desktop_viewport
                try:
                    await page.set_viewport_size({"width": width, "height": height})
                except Exception:
                    pass

        return desktop_result, mobile_result


_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug_for(url: str, base_url: str = "") -> str:
    """Stable, filesystem-safe name for a URL's screenshots."""
    import hashlib
    from urllib.parse import urlsplit

    relative = url[len(base_url):] if base_url and url.startswith(base_url) else url
    parts = urlsplit(relative)
    text = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    slug = _SLUG_UNSAFE.sub("-", text).strip("-") or "index"
    if len(slug) > 80:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        slug = f"{slug[:70]}-{digest}"
    return slug
