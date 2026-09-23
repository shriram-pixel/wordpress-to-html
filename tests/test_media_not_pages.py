"""A link to a file is not a link to a page.

Sites link straight at PDFs and images -- a brochure, a certificate, a gallery
lightbox. Those links were queued for rendering, and Chromium cannot render
either kind:

* given a PDF it begins a download, the navigation fails, and after three
  attempts the page is recorded as failed -- so every page linking to it is
  reported as pointing at a missing page;
* given an image it displays its own viewer, a black page holding one ``<img>``,
  which was written into the export as if it were a page of the site.

One real conversion queued 25 PDFs and 31 images this way: 25 render failures,
31 pages of viewer markup, and 164 broken links in the report. None of them
needed rendering -- both kinds are collected as assets when a page references
them.

Sitemaps, feeds and robots.txt are the exception, and the tests below pin that
too: they are documents of the site rather than assets of a page, and are
fetched and written through unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import ConversionOptions  # noqa: E402
from app.services.url_discovery import filter_discovered_links  # noqa: E402
from app.utils.urls import is_raw_document, is_renderable  # noqa: E402

HOSTS = {"127.0.0.1", "example.com"}
BASE = "http://127.0.0.1:8000"


def discovered(*links: str) -> list[str]:
    """The URLs that would actually be queued for rendering."""
    records = filter_discovered_links(
        links, HOSTS, known=set(), depth=1, options=ConversionOptions(), base_url=BASE
    )
    return [r.url for r in records]


@pytest.mark.parametrize("url", [
    f"{BASE}/wp-content/uploads/2026/03/team-chart.pdf",
    f"{BASE}/wp-content/uploads/2025/09/certificate.JPG",
    f"{BASE}/wp-content/uploads/2025/08/photo.jpeg",
    f"{BASE}/wp-content/uploads/brochure.docx",
    f"{BASE}/wp-content/uploads/video.mp4",
    f"{BASE}/wp-content/themes/x/style.css",
    f"{BASE}/wp-content/plugins/y/script.js",
    f"{BASE}/fonts/body.woff2",
])
def test_a_file_is_never_queued_as_a_page(url):
    assert not is_renderable(url)
    assert discovered(url) == []


@pytest.mark.parametrize("url", [
    f"{BASE}/",
    f"{BASE}/about-us/",
    f"{BASE}/products/heat-exchangers/",
    f"{BASE}/index.html",
    f"{BASE}/?page_id=12",
])
def test_pages_are_still_queued(url):
    assert is_renderable(url)
    assert discovered(url) == [url]


@pytest.mark.parametrize("url", [
    f"{BASE}/wp-sitemap.xml",
    f"{BASE}/robots.txt",
    f"{BASE}/feed/atom.xml",
    f"{BASE}/manifest.webmanifest",
])
def test_documents_of_the_site_are_kept(url):
    """Not assets of a page: these are exported, fetched byte-for-byte."""
    assert is_raw_document(url)
    assert is_renderable(url), "dropping these would empty the sitemap"
    assert discovered(url) == [url]


def test_a_mixed_page_keeps_only_its_pages():
    """What a real gallery page offers the crawler."""
    queued = discovered(
        f"{BASE}/certificates/",
        f"{BASE}/wp-content/uploads/2025/09/iso-14001.jpg",
        f"{BASE}/wp-content/uploads/2026/03/team-chart.pdf",
        f"{BASE}/contact-us/",
        f"{BASE}/wp-sitemap.xml",
    )

    assert queued == [f"{BASE}/certificates/", f"{BASE}/contact-us/",
                      f"{BASE}/wp-sitemap.xml"]


def test_capitals_do_not_smuggle_a_file_through():
    """Windows-authored content links to Logo.PNG as readily as logo.png."""
    assert not is_renderable(f"{BASE}/wp-content/uploads/Logo.PNG")
    assert discovered(f"{BASE}/wp-content/uploads/Logo.PNG") == []


def test_a_query_string_does_not_either():
    assert not is_renderable(f"{BASE}/wp-content/uploads/a.pdf?ver=2")
    assert discovered(f"{BASE}/wp-content/uploads/a.pdf?ver=2") == []


def test_the_pipeline_and_discovery_agree_on_what_a_document_is():
    """Two copies of one rule is how the database tools went missing."""
    from app.services.pipeline import _is_raw_document

    for url in (f"{BASE}/wp-sitemap.xml", f"{BASE}/robots.txt",
                f"{BASE}/about/", f"{BASE}/a.pdf"):
        assert _is_raw_document(url) == is_raw_document(url)
