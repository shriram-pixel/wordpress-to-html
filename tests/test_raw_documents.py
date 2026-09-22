"""A sitemap must not ship the render server's address, in any spelling."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.quality import scan_leftovers  # noqa: E402
from app.utils.urls import host_variants  # noqa: E402

SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<?xml-stylesheet type="text/xsl" '
    'href="//127.0.0.1:58770/wp-content/plugins/wordpress-seo/css/main-sitemap.xsl"?>'
    '<sitemapindex><sitemap><loc>http://127.0.0.1:58770/page-sitemap.xml</loc>'
    '</sitemap></sitemapindex>'
)


def strip_base(body: bytes, base_url: str) -> bytes:
    """What _fetch_raw_document does to a copied document."""
    for variant in sorted(host_variants(base_url), key=len, reverse=True):
        body = body.replace(variant.encode(), b"")
    return body


def test_every_spelling_of_the_render_server_is_removed():
    """Replacing only the canonical form left the protocol-relative one.

    A Yoast sitemap references its stylesheet as href="//host:port/..." --
    no "http:" anywhere in it -- so it survived and reached the export.
    """
    cleaned = strip_base(SITEMAP.encode(), "http://127.0.0.1:58770").decode()

    assert "127.0.0.1" not in cleaned
    assert 'href="/wp-content/plugins/wordpress-seo/css/main-sitemap.xsl"' in cleaned
    assert "<loc>/page-sitemap.xml</loc>" in cleaned


def test_the_scan_reads_sitemaps_and_robots_too(tmp_path):
    """The scan only looked at pages, so nothing reported the leak."""
    (tmp_path / "wp-sitemap.xml").write_text(SITEMAP, encoding="utf-8")
    (tmp_path / "robots.txt").write_text(
        "Sitemap: http://127.0.0.1:58770/wp-sitemap.xml\n", encoding="utf-8")

    found = scan_leftovers(tmp_path, {"example.com"})

    files = {entry["file"] for entry in found.temp_server}
    assert "wp-sitemap.xml" in files
    assert "robots.txt" in files


def test_a_clean_export_still_reports_nothing(tmp_path):
    (tmp_path / "wp-sitemap.xml").write_text(
        strip_base(SITEMAP.encode(), "http://127.0.0.1:58770").decode(), encoding="utf-8")

    assert not scan_leftovers(tmp_path, {"example.com"}).temp_server
