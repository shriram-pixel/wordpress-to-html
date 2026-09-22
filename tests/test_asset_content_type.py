"""An HTML page served in place of an asset must never be saved as one.

Elementor writes each page's stylesheet into uploads on demand, so a backup
often lacks some of them. WordPress answers the request with its "not found"
page, and PHP's built-in server returns it with status 200 -- which a check on
the status code alone accepts. Forty-eight of those were written as
``post-N.css`` in one real conversion, and the forty-eight pages they belonged
to lost their entire Elementor layout: the browser asked for a stylesheet, got
a document, and applied nothing.

A missing file is a much better outcome. It is reported, and the validation
stage can copy the real one in from the restored install.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.asset_manager import _is_html_response  # noqa: E402


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/wp-content/uploads/elementor/css/post-10926.css",
    "http://127.0.0.1:8080/wp-content/themes/x/script.js",
    "http://127.0.0.1:8080/wp-content/uploads/2025/09/photo.jpg",
    "http://127.0.0.1:8080/wp-content/uploads/brochure.pdf",
    "http://127.0.0.1:8080/fonts/body.woff2",
    "http://127.0.0.1:8080/wp-content/uploads/elementor/css/post-1.css?ver=3.2",
])
def test_a_page_served_as_an_asset_is_refused(url):
    assert _is_html_response(url, "text/html; charset=UTF-8")


def test_the_real_thing_is_accepted():
    css = "http://127.0.0.1:8080/wp-content/uploads/elementor/css/post-10926.css"

    assert not _is_html_response(css, "text/css")
    assert not _is_html_response(css, "text/css; charset=utf-8")


def test_a_page_request_is_left_alone():
    """Only asset extensions are judged; an actual page may be HTML."""
    assert not _is_html_response("http://127.0.0.1:8080/about-us/", "text/html")
    assert not _is_html_response("http://127.0.0.1:8080/index.html", "text/html")
    assert not _is_html_response("http://127.0.0.1:8080/feed/", "text/html")


def test_an_unknown_content_type_is_not_second_guessed():
    """Servers mislabel things; only an explicit HTML type counts."""
    css = "http://127.0.0.1:8080/style.css"

    assert not _is_html_response(css, "")
    assert not _is_html_response(css, "application/octet-stream")
    assert not _is_html_response(css, "text/plain")


def test_xhtml_counts_too():
    assert _is_html_response("http://127.0.0.1:8080/style.css", "application/xhtml+xml")
