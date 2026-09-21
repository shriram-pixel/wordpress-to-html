"""Rewrite a rendered page into a self-contained static document.

The captured DOM is treated as the truth. Nothing is reformatted, minified,
re-indented or "cleaned up" structurally: classes, IDs, ``data-*`` attributes,
ARIA attributes, inline styles and builder-generated wrappers are exactly what
make the page look and behave the way it does, and every one of them is left
alone. The only changes made are:

* references pointed at local files instead of the original domain
* server-side-only endpoints neutralised or annotated
* the small set of tags that exist purely to serve a live WordPress removed

Everything else -- including all JavaScript -- is preserved. A static export
that strips the theme's scripts loses its menus, sliders and mobile navigation,
which defeats the purpose.

Dynamic features are *detected and reported*, never silently faked. A contact
form that needs PHP keeps its markup and appearance, and the conversion report
says plainly that submitting it will not work without a backend.
"""

from __future__ import annotations

import logging
import posixpath
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment

from app.config import ConversionOptions, ExternalResourcePolicy
from app.services.asset_manager import AssetManager
from app.services.url_rewriter import AssetMap, extract_css_urls, rewrite_css_urls
from app.utils.urls import (
    ResourceClass,
    classify_url,
    is_local_origin,
    is_probably_asset,
    join_srcset,
    normalise_url,
    relative_href,
    split_srcset,
    to_local_origin,
    url_to_output_path,
)

logger = logging.getLogger(__name__)


def _best_parser() -> str:
    """Prefer lxml, fall back to the stdlib parser.

    lxml is considerably faster and more forgiving of real-world markup, but it
    needs a C toolchain when no wheel exists for the running Python. The stdlib
    parser handles everything here correctly, just more slowly, so its absence
    degrades speed rather than breaking the export.
    """
    try:
        import lxml  # noqa: F401

        return "lxml"
    except ImportError:
        return "html.parser"


HTML_PARSER = _best_parser()


# ---------------------------------------------------------------------------
# Dynamic feature detection
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class DynamicFeature:
    """Something on the page that cannot work without a server."""

    name: str
    category: str
    """``form``, `search``, ``comments``, ``ajax``, ``rest``, ``commerce``, ``auth``."""
    detail: str = ""
    pages: set[str] = field(default_factory=set)
    limitation: str = ""

    def merge(self, other: "DynamicFeature") -> None:
        self.pages |= other.pages


#: Signatures for the plugins and features worth naming explicitly in the
#: report. Matching is on markup that the plugin reliably emits.
_FEATURE_SIGNATURES: tuple[tuple[str, str, str, str], ...] = (
    # (css selector, feature name, category, limitation)
    ("form.wpcf7-form, div.wpcf7", "Contact Form 7", "form",
     "Form submission posts to wp-admin/admin-ajax.php and needs PHP."),
    ("div.wpforms-container, form.wpforms-form", "WPForms", "form",
     "Form submission posts to WordPress and needs PHP."),
    ("div.gform_wrapper, form[id^='gform_']", "Gravity Forms", "form",
     "Form submission posts to WordPress and needs PHP."),
    ("form.elementor-form", "Elementor Forms", "form",
     "Form submission posts to admin-ajax.php and needs PHP."),
    ("div.nf-form-cont, form.ninja-forms-form", "Ninja Forms", "form",
     "Form submission posts to WordPress and needs PHP."),
    ("div.frm_forms, form.frm-show-form", "Formidable Forms", "form",
     "Form submission posts to WordPress and needs PHP."),
    ("form.search-form, input[type='search'], form[role='search']", "WordPress search", "search",
     "Search is a database query; a static export has no search backend."),
    ("#comments, #respond, form#commentform", "Comments", "comments",
     "Posting and displaying new comments requires PHP. Existing comments are "
     "exported as part of the page."),
    ("form.woocommerce-cart-form, div.woocommerce, form.cart", "WooCommerce", "commerce",
     "Cart, checkout and payment are server-side and cannot be made static."),
    ("form#loginform, div.login", "WordPress login", "auth",
     "Authentication requires PHP."),
    ("div.mc4wp-form, form.mc4wp-form", "Mailchimp for WordPress", "form",
     "Newsletter signup posts to WordPress and needs PHP."),
)

#: Endpoints whose presence in markup or script means a live backend is assumed.
_ENDPOINT_PATTERNS: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (re.compile(r"admin-ajax\.php"), "AJAX requests (admin-ajax.php)", "ajax",
     "Scripts call WordPress's AJAX endpoint, which will not respond in a static export."),
    (re.compile(r"/wp-json/"), "WordPress REST API", "rest",
     "Scripts call the REST API, which will not respond in a static export."),
    (re.compile(r"admin-post\.php"), "Form handler (admin-post.php)", "form",
     "Form submissions post to WordPress and need PHP."),
    (re.compile(r"wp-login\.php"), "WordPress login", "auth",
     "Authentication requires PHP."),
    (re.compile(r"wp-comments-post\.php"), "Comment submission", "comments",
     "Posting comments requires PHP."),
)


class DynamicFeatureDetector:
    """Accumulates dynamic features across every processed page."""

    def __init__(self) -> None:
        self.features: dict[str, DynamicFeature] = {}

    def scan(self, soup: BeautifulSoup, page_url: str, html: str) -> None:
        for selector, name, category, limitation in _FEATURE_SIGNATURES:
            try:
                if soup.select_one(selector) is not None:
                    self._add(name, category, limitation, page_url)
            except Exception:
                # A malformed selector must never break a conversion.
                continue

        # Any form pointing somewhere server-side, including bespoke ones.
        for form in soup.find_all("form"):
            action = (form.get("action") or "").strip()
            if action and any(
                marker in action
                for marker in ("admin-post.php", "admin-ajax.php", "wp-comments-post.php", ".php")
            ):
                self._add(
                    "Custom form posting to a PHP endpoint", "form",
                    f"A form posts to {action[:120]}, which needs a server.", page_url,
                )

        for pattern, name, category, limitation in _ENDPOINT_PATTERNS:
            if pattern.search(html):
                self._add(name, category, limitation, page_url)

    def _add(self, name: str, category: str, limitation: str, page_url: str) -> None:
        existing = self.features.get(name)
        if existing is None:
            self.features[name] = DynamicFeature(
                name=name, category=category, limitation=limitation, pages={page_url}
            )
        else:
            existing.pages.add(page_url)

    def report(self) -> list[dict]:
        return [
            {
                "name": feature.name,
                "category": feature.category,
                "limitation": feature.limitation,
                "page_count": len(feature.pages),
                "examples": sorted(feature.pages)[:5],
            }
            for feature in sorted(self.features.values(), key=lambda f: (f.category, f.name))
        ]


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ProcessedPage:
    html: str
    output_path: str
    internal_links: list[str] = field(default_factory=list)
    rewritten: int = 0
    preserved_external: int = 0
    dynamic_endpoints: int = 0
    warnings: list[str] = field(default_factory=list)


#: Attributes that carry a single URL, by tag.
_URL_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "img": ("src", "data-src", "data-lazy-src", "data-original", "data-echo"),
    "source": ("src", "data-src"),
    "video": ("src", "poster", "data-src"),
    "audio": ("src", "data-src"),
    "embed": ("src",),
    "iframe": ("src", "data-src", "data-lazy-src"),
    "track": ("src",),
    "object": ("data",),
    "input": ("src",),
    "script": ("src",),
    "use": ("href", "xlink:href"),
    "image": ("href", "xlink:href"),
}

#: ``srcset``-style attributes, which hold several URLs with descriptors.
_SRCSET_ATTRIBUTES = ("srcset", "data-srcset", "data-lazy-srcset", "imagesrcset")

#: ``<link rel>`` values whose href points at an asset to localise.
_ASSET_LINK_RELS = {
    "stylesheet", "icon", "shortcut icon", "apple-touch-icon",
    "apple-touch-icon-precomposed", "mask-icon", "manifest", "preload",
    "prefetch", "modulepreload", "apple-touch-startup-image",
}

#: ``<link rel>`` values that only make sense on a live WordPress.
_REMOVABLE_LINK_RELS = {
    "https://api.w.org/", "alternate", "edituri", "wlwmanifest", "pingback",
    "prev", "next", "shortlink", "dns-prefetch",
}

#: Attributes holding a URL that is a *link*, not an asset.
_LINK_ATTRIBUTES = ("href",)

_BACKGROUND_STYLE = re.compile(r"url\(", re.IGNORECASE)

#: ``/*# sourceURL=... */`` and ``/*# sourceMappingURL=... */`` devtools hints.
_SOURCE_ANNOTATION = re.compile(r"/\*#\s*source(?:Mapping)?URL=[^*]*\*/\s*", re.IGNORECASE)


class HtmlProcessor:
    """Rewrites one rendered page at a time into its static form."""

    def __init__(
        self,
        base_url: str,
        asset_map: AssetMap,
        asset_manager: AssetManager,
        options: ConversionOptions,
        *,
        site_hosts: set[str] | None = None,
        detector: DynamicFeatureDetector | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.map = asset_map
        self.assets = asset_manager
        self.options = options
        self.site_hosts = site_hosts or set()
        self.detector = detector or DynamicFeatureDetector()

    # -- entry point --------------------------------------------------------
    def process(self, page_url: str, html: str, output_path: str) -> ProcessedPage:
        soup = BeautifulSoup(html, HTML_PARSER)
        result = ProcessedPage(html="", output_path=output_path)

        # The captured DOM may carry a <base>, which would change how every
        # relative URL resolves. Resolve against it, then drop it.
        base_href = self._consume_base_tag(soup, page_url)

        self.detector.scan(soup, page_url, html)

        self._rewrite_elements(soup, base_href, output_path, result)
        self._rewrite_import_maps(soup, base_href, output_path, result)
        self._rewrite_inline_scripts(soup, output_path, result)
        self._rewrite_inline_styles(soup, base_href, output_path, result)
        self._rewrite_style_blocks(soup, base_href, output_path, result)
        self._clean_wordpress_artefacts(soup, result)
        self._neutralise_forms(soup, result)
        self._annotate(soup)

        result.html = self._sweep_residual_base_url(str(soup), output_path, result)
        return result

    def _sweep_residual_base_url(
        self, html: str, output_path: str, result: ProcessedPage
    ) -> str:
        """Last-resort removal of any surviving reference to the render server.

        Every earlier pass targets a specific place a URL can hide -- an
        attribute, a srcset, an import map, a CSS ``url()``, an inline script.
        Themes and plugins keep inventing new ones: a ``sourceURL`` comment, a
        bespoke ``data-`` attribute, a JSON island in a template.

        ``http://127.0.0.1:<port>`` is unambiguous: it is the address of a
        throwaway server that stops existing the moment the job ends, so any
        occurrence left in the output is wrong by definition. Rewriting what
        survives to a site-root-relative path is always an improvement on
        shipping a dead absolute URL, and the count is recorded so an
        unexpected leak shows up in the report rather than passing unnoticed.
        """
        if not self.base_url or self.base_url not in html:
            if not self.base_url or self.base_url.replace("/", r"\/") not in html:
                return html

        depth = output_path.count("/")
        prefix = "../" * depth if depth else "./"

        before = html
        html = html.replace(self.base_url + "/", prefix)
        html = html.replace(self.base_url, prefix.rstrip("/") or ".")

        escaped = self.base_url.replace("/", r"\/")
        html = html.replace(escaped + r"\/", prefix.replace("/", r"\/"))
        html = html.replace(escaped, (prefix.rstrip("/") or ".").replace("/", r"\/"))

        if html != before:
            result.warnings.append(
                "a reference to the temporary render server survived the targeted "
                "rewriting passes and was made relative by the final sweep"
            )
            result.rewritten += 1
        return html

    # -- helpers ------------------------------------------------------------
    def _consume_base_tag(self, soup: BeautifulSoup, page_url: str) -> str:
        base_tag = soup.find("base", href=True)
        if not base_tag:
            return page_url
        resolved = normalise_url(base_tag["href"], page_url, force_trailing_slash=False) or page_url
        base_tag.decompose()
        return resolved

    def _resolve(self, raw: str, base_href: str) -> str | None:
        """Absolute, normalised URL for a raw attribute value."""
        if not raw:
            return None
        raw = raw.strip()
        if not raw or raw.startswith(("data:", "blob:", "javascript:", "mailto:", "tel:", "#", "about:")):
            return None
        return normalise_url(raw, base_href, force_trailing_slash=False)

    def _asset_href(self, absolute: str, output_path: str) -> str | None:
        """Register an asset and return the href to reach it from this page."""
        target = self.assets.register(absolute)
        if target is None:
            return None
        return posixpath.relpath(target, start=posixpath.dirname(output_path) or ".")

    def _page_href(self, absolute: str, output_path: str) -> str | None:
        href = self.map.href_for(absolute, output_path)
        if href is None:
            # Pages are registered under the local render address, but the
            # markup often names them by the public domain -- a menu or footer
            # built in a page builder keeps https://example.com/contact-us/.
            local = self._local_form(absolute)
            if local != absolute:
                href = self.map.href_for(local, output_path)
        return href

    def _local_form(self, absolute: str) -> str:
        """*absolute* moved onto the local render address, if it is this site's."""
        if (
            self.base_url
            and not is_local_origin(absolute, self.base_url)
            and classify_url(absolute, self.site_hosts) is ResourceClass.LOCAL
        ):
            return to_local_origin(absolute, self.base_url)
        return absolute

    # -- element rewriting --------------------------------------------------
    def _rewrite_elements(
        self, soup: BeautifulSoup, base_href: str, output_path: str, result: ProcessedPage
    ) -> None:
        for element in soup.find_all(True):
            name = (element.name or "").lower()

            # --- single-URL asset attributes --------------------------------
            for attribute in _URL_ATTRIBUTES.get(name, ()):
                value = element.get(attribute)
                if not value or not isinstance(value, str):
                    continue
                absolute = self._resolve(value, base_href)
                if not absolute:
                    continue
                href = self._asset_href(absolute, output_path)
                if href:
                    element[attribute] = href
                    result.rewritten += 1
                else:
                    self._count_untouched(absolute, result)

            # --- srcset -----------------------------------------------------
            for attribute in _SRCSET_ATTRIBUTES:
                value = element.get(attribute)
                if not value or not isinstance(value, str):
                    continue
                entries = split_srcset(value)
                if not entries:
                    continue
                changed = False
                rewritten_entries: list[tuple[str, str]] = []
                for candidate_url, descriptor in entries:
                    absolute = self._resolve(candidate_url, base_href)
                    href = self._asset_href(absolute, output_path) if absolute else None
                    if href:
                        rewritten_entries.append((href, descriptor))
                        changed = True
                        result.rewritten += 1
                    else:
                        rewritten_entries.append((candidate_url, descriptor))
                        if absolute:
                            self._count_untouched(absolute, result)
                if changed:
                    element[attribute] = join_srcset(rewritten_entries)

            # --- <link> -----------------------------------------------------
            if name == "link":
                self._rewrite_link(element, base_href, output_path, result)
                continue

            # --- <a> and other link-carrying elements -----------------------
            if name in {"a", "area"}:
                self._rewrite_anchor(element, base_href, output_path, result)

            # --- <meta> refresh and og:image --------------------------------
            if name == "meta":
                self._rewrite_meta(element, base_href, output_path, result)

    def _rewrite_link(self, element, base_href: str, output_path: str, result: ProcessedPage) -> None:
        rel_values = element.get("rel") or []
        if isinstance(rel_values, str):
            rel_values = rel_values.split()
        rel = {r.lower() for r in rel_values}

        href = element.get("href")
        if not href or not isinstance(href, str):
            return

        # Links that only exist to advertise a live WordPress.
        if rel & {"edituri", "wlwmanifest", "pingback"}:
            element.decompose()
            return
        if "alternate" in rel and "wp-json" in href:
            element.decompose()
            return
        if "https://api.w.org/" in rel:
            element.decompose()
            return

        absolute = self._resolve(href, base_href)
        if not absolute:
            return

        if rel & _ASSET_LINK_RELS:
            localised = self._asset_href(absolute, output_path)
            if localised:
                element["href"] = localised
                result.rewritten += 1
            else:
                self._count_untouched(absolute, result)
            return

        # Feed links. WordPress advertises RSS and Atom feeds on every page.
        # When feeds are not part of the export those URLs have no destination,
        # and leaving them absolute points readers and feed readers back at the
        # temporary render server.
        link_type = (element.get("type") or "").lower()
        is_feed = "alternate" in rel and (
            "rss" in link_type or "atom" in link_type or "/feed/" in href
        )
        if is_feed and self._page_href(absolute, output_path) is None:
            element.decompose()
            return

        # canonical / prev / next / shortlink point at pages.
        page_href = self._page_href(absolute, output_path)
        if page_href:
            element["href"] = page_href
            result.rewritten += 1
        elif "canonical" in rel:
            # A canonical URL pointing at a page that was not exported is worse
            # than none: it would send search engines to a dead address.
            element.decompose()

    def _rewrite_anchor(self, element, base_href: str, output_path: str, result: ProcessedPage) -> None:
        href = element.get("href")
        if not href or not isinstance(href, str):
            return

        stripped = href.strip()
        if stripped.startswith("#") or stripped.lower().startswith(
            ("mailto:", "tel:", "javascript:", "sms:")
        ):
            return

        absolute = self._resolve(href, base_href)
        if not absolute:
            return

        classification = classify_url(absolute, self.site_hosts)

        if classification is ResourceClass.EXTERNAL:
            result.preserved_external += 1
            return

        if classification is ResourceClass.DYNAMIC:
            self._mark_dynamic_link(element, absolute, result)
            return

        # A page we exported. Resolving drops the "#section" part, so carry it
        # over: a link to another page's anchor must still land on it.
        page_href = self._page_href(absolute, output_path)
        if page_href:
            fragment = urlsplit(stripped).fragment
            if fragment and "#" not in page_href:
                page_href += f"#{fragment}"
            element["href"] = page_href
            result.rewritten += 1
            result.internal_links.append(absolute)
            return

        # A local URL that is really an asset: a linked PDF, image or archive.
        #
        # The extension test is essential. Without it, a link to a page that was
        # deliberately *not* exported -- an excluded tag archive, a
        # ?replytocom= comment link -- would be treated as an asset and fetched
        # verbatim. That downloads raw HTML straight into the export, skipping
        # the whole rewriting stage, so the file lands with its URLs still
        # pointing at the temporary render server.
        if is_probably_asset(absolute):
            asset_href = self._asset_href(absolute, output_path)
            if asset_href:
                element["href"] = asset_href
                result.rewritten += 1
                return

        # Local, but not exported: a page WordPress could not render, or one
        # the options excluded. Link to where it would be, naming the file like
        # every other page link, so the export has one consistent link style
        # and never a bare folder that opens a directory listing from disk.
        parts = urlsplit(absolute)
        if parts.query:
            element["href"] = parts.path + f"?{parts.query}"
        else:
            target = url_to_output_path(self._local_form(absolute), flat=self.map.flat)
            href = relative_href(output_path, target, pretty=self.map.folder_links)
            element["href"] = href + (f"#{parts.fragment}" if parts.fragment else "")
        result.internal_links.append(absolute)

    def _mark_dynamic_link(self, element, absolute: str, result: ProcessedPage) -> None:
        """Annotate a link to an endpoint that cannot exist statically."""
        result.dynamic_endpoints += 1
        path = urlsplit(absolute).path

        if "/wp-admin" in path or "wp-login.php" in path:
            # An admin link in a public export is a dead end and a small
            # information leak about the original install; drop the href but
            # keep the element so the layout does not shift.
            element["href"] = "#"
            element["data-wpsc-removed"] = "wordpress-admin"
            element["aria-disabled"] = "true"
        else:
            element["data-wpsc-dynamic"] = path

    def _rewrite_meta(self, element, base_href: str, output_path: str, result: ProcessedPage) -> None:
        property_name = (element.get("property") or element.get("name") or "").lower()

        if property_name in {"og:image", "og:image:url", "og:image:secure_url",
                             "twitter:image", "og:audio", "og:video"}:
            content = element.get("content")
            absolute = self._resolve(content, base_href) if content else None
            if absolute:
                href = self._asset_href(absolute, output_path)
                if href:
                    element["content"] = href
                    result.rewritten += 1
            return

        if property_name in {"og:url", "twitter:url"}:
            content = element.get("content")
            absolute = self._resolve(content, base_href) if content else None
            if absolute:
                page_href = self._page_href(absolute, output_path)
                if page_href:
                    element["content"] = page_href
                    result.rewritten += 1
            return

        if (element.get("http-equiv") or "").lower() == "refresh":
            content = element.get("content") or ""
            match = re.search(r"url\s*=\s*(.+)$", content, re.IGNORECASE)
            if match:
                absolute = self._resolve(match.group(1).strip().strip("'\""), base_href)
                if absolute:
                    page_href = self._page_href(absolute, output_path)
                    if page_href:
                        delay = content.split(";", 1)[0].strip()
                        element["content"] = f"{delay}; url={page_href}"
                        result.rewritten += 1

    # -- inline JavaScript --------------------------------------------------
    def _rewrite_inline_scripts(
        self, soup: BeautifulSoup, output_path: str, result: ProcessedPage
    ) -> None:
        """Rewrite absolute site URLs embedded inside inline ``<script>`` blocks.

        WordPress passes data to its scripts as inline JavaScript -- that is
        what ``wp_localize_script()`` emits -- and those objects are full of
        absolute URLs::

            window._wpemojiSettings = {"source":{"concatemoji":
                "http:\\/\\/127.0.0.1:59766\\/wp-includes\\/js\\/wp-emoji-release.min.js"}};

        Left alone, every one of them points at the temporary render server,
        which no longer exists once the site is deployed. The symptom is subtle:
        the page looks correct but scripts fail at runtime.

        The spec for this tool is explicit that script initialisation data must
        be *preserved*, so the data is kept intact and only its URLs are
        repointed. Both plain and JSON-escaped (``\\/``) spellings are handled,
        and each URL is routed through the same asset pipeline as the rest of
        the page, so a script's assets are downloaded like any other.
        """
        if not self.base_url:
            return

        page_dir = posixpath.dirname(output_path) or "."

        # Match the site's own absolute URLs in either spelling. The trailing
        # character class stops the match at whatever delimits the URL in the
        # surrounding JavaScript (quote, comma, brace, whitespace).
        escaped_base = re.escape(self.base_url)
        json_escaped_base = re.escape(self.base_url.replace("/", r"\/"))
        pattern = re.compile(
            rf"(?:{escaped_base}|{json_escaped_base})"
            rf"(?:\\?/[^\s\"'`<>,;){{}}\]\\]*(?:\\/[^\s\"'`<>,;){{}}\]\\]*)*)?"
        )

        for script in soup.find_all("script"):
            if script.get("src"):
                continue  # external file; its own src was already rewritten
            script_type = (script.get("type") or "").lower()
            if script_type in {"importmap", "application/ld+json"} and script_type == "importmap":
                continue  # already handled structurally

            text = script.string or script.get_text() or ""
            if not text or self.base_url.split("//", 1)[-1] not in text:
                continue

            def replace(match: re.Match) -> str:
                raw = match.group(0)
                was_escaped = r"\/" in raw
                plain = raw.replace("\\/", "/")

                absolute = normalise_url(plain, self.base_url, force_trailing_slash=False)
                if not absolute:
                    return raw

                classification = classify_url(absolute, self.site_hosts)

                if classification is ResourceClass.DYNAMIC:
                    # A REST or AJAX endpoint cannot be made to work. Leave the
                    # path so the script fails predictably against a relative
                    # URL rather than against a dead port on someone's laptop.
                    replacement = urlsplit(absolute).path
                    result.dynamic_endpoints += 1
                else:
                    # Pages first. A builder's inline configuration is full of
                    # permalinks -- Elementor emits them freely -- and
                    # registering one as an asset would download the page and
                    # try to write it as a *file* at the same path its
                    # directory already occupies, which fails outright on
                    # Windows and silently shadows the page elsewhere.
                    target = self.map.output_path_for(absolute)
                    if target is None and is_probably_asset(absolute):
                        target = self.assets.register(absolute, source="script")
                    if target is None:
                        return raw
                    replacement = posixpath.relpath(target, start=page_dir)

                result.rewritten += 1
                return replacement.replace("/", "\\/") if was_escaped else replacement

            rewritten = pattern.sub(replace, text)
            if rewritten != text:
                # Assigning to .string keeps the node a plain text child, which
                # is what BeautifulSoup writes back out verbatim.
                script.string = rewritten

    # -- script modules -----------------------------------------------------
    def _rewrite_import_maps(
        self, soup: BeautifulSoup, base_href: str, output_path: str, result: ProcessedPage
    ) -> None:
        """Rewrite the URLs inside ``<script type="importmap">``.

        WordPress 6.5 and later load the Interactivity API -- which powers the
        navigation block, the query block, search and the image lightbox -- as
        ES modules resolved through an import map. The map is JSON inside a
        script tag, so no attribute-based rewriting touches it, and leaving it
        alone means every one of those modules still points at the temporary
        render server. In the exported site those requests fail outright and
        the interactive blocks silently stop working.
        """
        import json

        for script in soup.find_all("script", attrs={"type": "importmap"}):
            raw = script.string or script.get_text() or ""
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                result.warnings.append("an import map could not be parsed and was left unchanged")
                continue

            changed = False

            def remap(mapping: dict) -> dict:
                nonlocal changed
                out = {}
                for specifier, target in mapping.items():
                    if isinstance(target, str):
                        absolute = self._resolve(target, base_href)
                        href = self._asset_href(absolute, output_path) if absolute else None
                        if href:
                            # Import-map targets must be a URL or start with
                            # "./", "../" or "/": a bare "wp-content/x.js" is
                            # treated as a specifier, not a path.
                            if not href.startswith((".", "/", "http")):
                                href = "./" + href
                            out[specifier] = href
                            changed = True
                            result.rewritten += 1
                            continue
                    out[specifier] = target
                return out

            if isinstance(data.get("imports"), dict):
                data["imports"] = remap(data["imports"])

            if isinstance(data.get("scopes"), dict):
                scopes = {}
                for scope, mapping in data["scopes"].items():
                    scopes[scope] = remap(mapping) if isinstance(mapping, dict) else mapping
                data["scopes"] = scopes

            if changed:
                script.string = json.dumps(data, separators=(",", ":"))

    # -- CSS ----------------------------------------------------------------
    def _rewrite_inline_styles(
        self, soup: BeautifulSoup, base_href: str, output_path: str, result: ProcessedPage
    ) -> None:
        """Rewrite ``url()`` inside ``style="..."`` attributes."""
        for element in soup.find_all(style=True):
            style = element.get("style")
            if not isinstance(style, str) or not _BACKGROUND_STYLE.search(style):
                continue

            def resolve(absolute: str) -> str | None:
                return self._asset_href(absolute, output_path)

            rewritten, count = rewrite_css_urls(style, base_href, resolve)
            if count:
                element["style"] = rewritten
                result.rewritten += count

    def _rewrite_style_blocks(
        self, soup: BeautifulSoup, base_href: str, output_path: str, result: ProcessedPage
    ) -> None:
        """Rewrite ``url()`` inside ``<style>`` elements.

        Inline CSS carries a large share of a builder's design -- Elementor and
        Gutenberg both emit per-page style blocks -- so its background images
        and fonts matter as much as those in external stylesheets.
        """
        for style_tag in soup.find_all("style"):
            css = style_tag.string or style_tag.get_text() or ""
            if not css or "url(" not in css.lower():
                continue

            for dependency in extract_css_urls(css, base_href):
                self.assets.register(dependency, source="css")

            def resolve(absolute: str) -> str | None:
                return self._asset_href(absolute, output_path)

            rewritten, count = rewrite_css_urls(css, base_href, resolve)

            # WordPress annotates the block styles it inlines with a
            # "/*# sourceURL=http://host/... */" comment for devtools. It has no
            # effect on rendering, but it embeds the temporary server's address
            # in every exported page, so it is stripped rather than rewritten.
            stripped = _SOURCE_ANNOTATION.sub("", rewritten)
            if stripped != rewritten:
                rewritten = stripped
                count += 1

            if count:
                style_tag.string = rewritten
                result.rewritten += count

    # -- cleanup ------------------------------------------------------------
    def _clean_wordpress_artefacts(self, soup: BeautifulSoup, result: ProcessedPage) -> None:
        """Remove only what is meaningless without a live WordPress.

        Deliberately conservative. Scripts and styles are never removed for
        being WordPress's: they are what make the page work.
        """
        # The admin bar is injected for a logged-in user and is never wanted.
        for selector in ("#wpadminbar", "#wp-admin-bar-root-default"):
            node = soup.select_one(selector)
            if node:
                node.decompose()

        # The admin bar's spacing rule leaves a gap once the bar is gone.
        for style_tag in soup.find_all("style", id="admin-bar-inline-css"):
            style_tag.decompose()

        # RSD/wlwmanifest and the generator tag advertise a live install.
        for meta in soup.find_all("meta", attrs={"name": "generator"}):
            content = (meta.get("content") or "").lower()
            if "wordpress" in content:
                meta.decompose()

        # Scripts that were injected at runtime by other scripts, tagged during
        # capture. Their injector is still present in this document and will
        # re-create them, so keeping the serialised copy means the script loads
        # twice and in the wrong order. Removing it restores the original
        # sequence rather than losing behaviour.
        for script in soup.find_all("script", attrs={"data-wpsc-injected": "1"}):
            script.decompose()
            result.rewritten += 1

        # The marker is never useful in the deliverable.
        for element in soup.find_all(attrs={"data-wpsc-injected": True}):
            del element["data-wpsc-injected"]

        # Editing-only markup left behind by the block editor.
        for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
            text = str(comment).strip()
            if text.startswith(("wp:", "/wp:")):
                comment.extract()

    def _neutralise_forms(self, soup: BeautifulSoup, result: ProcessedPage) -> None:
        """Keep every form's appearance, but stop it posting into the void.

        The markup, classes and styling are untouched so the page looks right.
        What changes is that the action no longer points at a PHP endpoint that
        will 404, and the form is annotated so the limitation is discoverable in
        the exported HTML as well as in the report.
        """
        for form in soup.find_all("form"):
            action = (form.get("action") or "").strip()
            if not action:
                continue

            absolute = normalise_url(action, self.base_url, force_trailing_slash=False)
            if not absolute:
                continue

            classification = classify_url(absolute, self.site_hosts)
            if classification is ResourceClass.EXTERNAL:
                # A form posting to a third-party service (Mailchimp, a form
                # SaaS) still works perfectly in a static export.
                continue

            path = urlsplit(absolute).path
            is_server_side = (
                classification is ResourceClass.DYNAMIC
                or path.endswith(".php")
                or "admin-post" in path
                or "admin-ajax" in path
            )

            if is_server_side or classification is ResourceClass.LOCAL:
                form["data-wpsc-original-action"] = action
                form["data-wpsc-static"] = "no-backend"
                # An empty action posts to the current page, which in a static
                # export simply reloads it rather than showing a 404.
                form["action"] = ""
                result.dynamic_endpoints += 1

    def _annotate(self, soup: BeautifulSoup) -> None:
        """Leave a short, honest note in the source about what this file is."""
        if soup.head is None:
            return
        note = Comment(
            " Static export generated by wp-static-converter. "
            "Server-side features (forms, search, comments) are preserved "
            "visually but require a backend to function. "
        )
        soup.head.insert(0, note)

    def _count_untouched(self, absolute: str, result: ProcessedPage) -> None:
        if classify_url(absolute, self.site_hosts) is ResourceClass.EXTERNAL:
            result.preserved_external += 1
