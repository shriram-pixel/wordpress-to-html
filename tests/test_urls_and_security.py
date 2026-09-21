"""Tests for URL handling and the security primitives."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.utils.security import (
    PathTraversalError,
    UnsafeArchivePath,
    safe_join,
    safe_output_path,
    sanitise_archive_path,
    sanitise_component,
    scrub_windows_path,
)
from app.utils.urls import (
    ResourceClass,
    classify_url,
    guess_extension,
    is_probably_asset,
    join_srcset,
    normalise_url,
    relative_href,
    same_site,
    split_srcset,
    strip_tracking_params,
    url_to_output_path,
)

HOSTS = {"example.com"}


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://example.com", "https://example.com/"),
        ("https://example.com/about", "https://example.com/about/"),
        ("https://example.com/about/", "https://example.com/about/"),
        ("https://EXAMPLE.COM/About/", "https://example.com/About/"),
        ("https://example.com:443/a/", "https://example.com/a/"),
        ("http://example.com:80/a/", "http://example.com/a/"),
        ("https://example.com/a/./b/../c/", "https://example.com/a/c/"),
        ("https://example.com/x.html", "https://example.com/x.html"),
        ("https://example.com/a/?utm_source=x&b=2", "https://example.com/a/?b=2"),
        ("https://example.com/a/#frag", "https://example.com/a/"),
        ("https://example.com/./", "https://example.com/"),
    ],
)
def test_normalisation(raw: str, expected: str):
    assert normalise_url(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "#anchor", "javascript:void(0)", "mailto:a@b.c", "tel:+1", "data:text/plain,x",
            "about:blank", "ftp://example.com/x"],
)
def test_non_addressable_urls_normalise_to_empty(raw: str):
    assert normalise_url(raw) == ""


def test_relative_urls_resolve_against_base():
    base = "https://example.com/blog/post/"
    assert normalise_url("../other/", base) == "https://example.com/blog/other/"
    assert normalise_url("/top/", base) == "https://example.com/top/"
    assert normalise_url("img.png", base) == "https://example.com/blog/post/img.png"


def test_protocol_relative_inherits_scheme():
    assert normalise_url("//cdn.example.com/x.js", "https://example.com/") == \
        "https://cdn.example.com/x.js"


def test_trailing_slash_can_be_disabled():
    assert normalise_url("https://example.com/a", force_trailing_slash=False) == \
        "https://example.com/a"


def test_query_ordering_is_canonical():
    assert normalise_url("https://example.com/?b=2&a=1") == normalise_url("https://example.com/?a=1&b=2")


def test_tracking_parameters_are_stripped_but_content_ones_kept():
    assert strip_tracking_params("utm_source=a&fbclid=b") == ""
    assert "page_id=7" in strip_tracking_params("page_id=7&utm_medium=x")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/about/", ResourceClass.LOCAL),
        ("https://www.example.com/about/", ResourceClass.LOCAL),
        ("https://cdn.other.com/a.js", ResourceClass.EXTERNAL),
        ("https://example.com/wp-json/wp/v2/posts", ResourceClass.DYNAMIC),
        ("https://example.com/wp-admin/", ResourceClass.DYNAMIC),
        ("https://example.com/wp-login.php", ResourceClass.DYNAMIC),
        ("https://example.com/wp-admin/admin-ajax.php", ResourceClass.DYNAMIC),
        ("https://example.com/xmlrpc.php", ResourceClass.DYNAMIC),
        # The same path on someone else's domain is just external.
        ("https://other.com/wp-json/x", ResourceClass.EXTERNAL),
    ],
)
def test_classification(url: str, expected: ResourceClass):
    assert classify_url(normalise_url(url), HOSTS) is expected


def test_www_and_bare_host_are_the_same_site():
    assert same_site("https://www.example.com/x", HOSTS)
    assert same_site("https://example.com/x", {"www.example.com"})
    assert not same_site("https://notexample.com/x", HOSTS)


# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/", "index.html"),
        ("https://example.com/about/", "about/index.html"),
        ("https://example.com/a/b/c/", "a/b/c/index.html"),
        ("https://example.com/page.html", "page.html"),
        ("https://example.com/blog/?paged=2", "blog/page/2/index.html"),
        ("https://example.com/?page_id=7", "page_id-7/index.html"),
        ("https://example.com/sitemap.xml", "sitemap.xml"),
        ("https://example.com/feed.php", "feed/index.html"),
    ],
)
def test_url_to_output_path(url: str, expected: str):
    assert url_to_output_path(normalise_url(url)) == expected


@pytest.mark.parametrize(
    "source,target,expected",
    [
        ("index.html", "about/index.html", "about/"),
        ("about/index.html", "index.html", "../"),
        ("a/b/index.html", "c/index.html", "../../c/"),
        ("about/index.html", "wp-content/x.png", "../wp-content/x.png"),
        ("index.html", "index.html", "./"),
    ],
)
def test_relative_href(source: str, target: str, expected: str):
    assert relative_href(source, target) == expected


def test_is_probably_asset():
    assert is_probably_asset("https://example.com/a.png")
    assert is_probably_asset("https://example.com/a.woff2")
    assert not is_probably_asset("https://example.com/about/")
    assert not is_probably_asset("https://example.com/a.html")


def test_guess_extension_prefers_the_url():
    assert guess_extension("https://x/a.png", "text/css") == ".png"
    assert guess_extension("https://x/style", "text/css") == ".css"
    assert guess_extension("https://x/thing", "") == ""


# ---------------------------------------------------------------------------
# srcset
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a.jpg 1x, b.jpg 2x", [("a.jpg", "1x"), ("b.jpg", "2x")]),
        ("only.jpg", [("only.jpg", "")]),
        ("a.jpg, b.jpg", [("a.jpg", ""), ("b.jpg", "")]),
        # A comma inside a filename is legal and must not split the candidate.
        ("hero-1,200x800.jpg 2x, s.jpg 1x", [("hero-1,200x800.jpg", "2x"), ("s.jpg", "1x")]),
        ("  a.jpg   300w ,\n b.jpg 600w ", [("a.jpg", "300w"), ("b.jpg", "600w")]),
        ("", []),
    ],
)
def test_split_srcset(raw: str, expected: list):
    assert split_srcset(raw) == expected


def test_srcset_round_trips():
    raw = "a.jpg 300w, b.jpg 600w"
    assert join_srcset(split_srcset(raw)) == raw


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "prefix,name,expected",
    [
        ("../../../../etc", "passwd", "etc/passwd"),
        ("wp-content/uploads", "a.jpg", "wp-content/uploads/a.jpg"),
        ("C:\\Windows\\System32", "evil.dll", "Windows/System32/evil.dll"),
        ("/absolute/path", "f.txt", "absolute/path/f.txt"),
        (".", "root.txt", "root.txt"),
        ("a/./b", "c.txt", "a/b/c.txt"),
    ],
)
def test_sanitise_archive_path(prefix: str, name: str, expected: str):
    assert str(sanitise_archive_path(prefix, name)) == expected


def test_empty_name_is_rejected():
    with pytest.raises(UnsafeArchivePath):
        sanitise_archive_path("wp-content", "")


def test_sanitise_component_handles_hostile_names():
    assert "/" not in sanitise_component("a/b")
    assert "\\" not in sanitise_component("a\\b")
    assert sanitise_component("..") == "_"
    assert sanitise_component("") == "_"
    assert sanitise_component("CON.txt").startswith("_")
    assert not sanitise_component("trailing.  ").endswith(" ")
    assert len(sanitise_component("x" * 500)) <= 200


def test_long_name_keeps_its_extension():
    assert sanitise_component("y" * 500 + ".jpg").endswith(".jpg")


def test_safe_join_refuses_escape(tmp_path: Path):
    with pytest.raises(PathTraversalError):
        safe_join(tmp_path, "../../outside.txt")
    assert safe_join(tmp_path, "a/b.txt") == tmp_path / "a" / "b.txt"


@pytest.mark.parametrize(
    "url_path,expected",
    [("/", "index.html"), ("/about/", "about/index.html"), ("/a/b.html", "a/b.html"),
     ("/../../etc/passwd", "etc/passwd/index.html")],
)
def test_safe_output_path(tmp_path: Path, url_path: str, expected: str):
    result = safe_output_path(tmp_path, url_path)
    assert result.relative_to(tmp_path).as_posix() == expected


@pytest.mark.parametrize(
    "text,secret",
    [
        (r"failed at C:\Users\Someone\secret\file.txt while reading", "Someone"),
        (r"cannot open D:/jobs/abc/wp-config.php", "wp-config"),
        ("no such file: /home/bob/backups/site.wpress", "bob"),
        ("denied: /Users/alice/Documents/x.sql", "alice"),
    ],
)
def test_scrub_hides_host_paths(text: str, secret: str):
    """Error text reaches the browser; it must not disclose the host layout."""
    scrubbed = scrub_windows_path(text)
    assert secret not in scrubbed
    assert "<path>" in scrubbed


def test_scrub_leaves_ordinary_text_alone():
    message = "the archive is corrupt at offset 1234"
    assert scrub_windows_path(message) == message
