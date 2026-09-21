"""Tests for asset path allocation, CSS rewriting, HTML rewriting and packaging."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.config import ConversionOptions, ExternalResourcePolicy
from app.services.html_processor import DynamicFeatureDetector, HtmlProcessor
from app.services.report_generator import detect_builders
from app.services.static_validator import validate_output
from app.services.url_rewriter import AssetMap, extract_css_urls, rewrite_css_urls
from app.services.zip_builder import build_zip, should_exclude, verify_zip

BASE = "http://127.0.0.1:8080"
HOSTS = {"127.0.0.1"}


# ---------------------------------------------------------------------------
# AssetMap
# ---------------------------------------------------------------------------
def test_pages_and_assets_get_stable_paths():
    m = AssetMap(site_hosts=HOSTS)
    assert m.add_page(BASE + "/") == "index.html"
    assert m.add_page(BASE + "/about/") == "about/index.html"
    assert m.add_asset(BASE + "/wp-content/uploads/a.png").output_path == \
        "wp-content/uploads/a.png"

    # Registering twice returns the same allocation.
    assert m.add_page(BASE + "/about/") == "about/index.html"


def test_cache_busting_query_is_ignored_for_naming():
    m = AssetMap(site_hosts=HOSTS)
    assert m.add_asset(BASE + "/style.css?ver=6.4").output_path == "style.css"


def test_meaningful_query_produces_distinct_files():
    m = AssetMap(site_hosts=HOSTS)
    a = m.add_asset(BASE + "/img.php?size=large").output_path
    b = m.add_asset(BASE + "/img.php?size=small").output_path
    assert a != b


def test_colliding_paths_are_disambiguated_deterministically():
    first = AssetMap(site_hosts=HOSTS)
    first.add_asset(BASE + "/a/thing")
    one = first.add_asset(BASE + "/a/thing?x=1").output_path

    second = AssetMap(site_hosts=HOSTS)
    second.add_asset(BASE + "/a/thing")
    two = second.add_asset(BASE + "/a/thing?x=1").output_path

    assert one == two, "path allocation must be reproducible across runs"


def test_case_only_differences_do_not_collide_on_windows():
    m = AssetMap(site_hosts=HOSTS)
    lower = m.add_asset(BASE + "/img/logo.png").output_path
    upper = m.add_asset(BASE + "/img/LOGO.png").output_path
    assert lower.lower() != upper.lower(), "these would overwrite each other on NTFS"


def test_external_assets_are_namespaced():
    m = AssetMap(site_hosts=HOSTS)
    path = m.add_asset("https://fonts.gstatic.com/s/x/font.woff2").output_path
    assert path.startswith("external/fonts.gstatic.com/")


def test_href_between_pages_links_to_the_folder():
    """The default: the same addresses WordPress used, for a web server."""
    m = AssetMap(site_hosts=HOSTS)
    m.add_page(BASE + "/")
    m.add_page(BASE + "/blog/post/")
    assert m.href_for(BASE + "/", "blog/post/index.html") == "../../"
    assert m.href_for(BASE + "/blog/post/", "index.html") == "blog/post/"


def test_href_can_name_the_file_instead():
    """For an export opened from a disk, where nothing resolves a folder."""
    m = AssetMap(site_hosts=HOSTS, folder_links=False)
    m.add_page(BASE + "/")
    m.add_page(BASE + "/blog/post/")
    assert m.href_for(BASE + "/", "blog/post/index.html") == "../../index.html"
    assert m.href_for(BASE + "/blog/post/", "index.html") == "blog/post/index.html"


def test_href_preserves_a_fragment():
    m = AssetMap(site_hosts=HOSTS)
    m.add_page(BASE + "/about/")
    assert m.href_for(BASE + "/about/#team", "index.html") == "about/#team"


@pytest.mark.parametrize(
    "path,flat,expected",
    [
        ("/", True, "index.html"),
        ("/about-us/", True, "about-us.html"),
        ("/about/team/", True, "about/team.html"),
        ("/blog/?paged=2", True, "blog/page/2.html"),
        ("/a/b.html", True, "a/b.html"),
        ("/", False, "index.html"),
        ("/about-us/", False, "about-us/index.html"),
        ("/about/team/", False, "about/team/index.html"),
    ],
)
def test_page_layouts(path: str, flat: bool, expected: str):
    m = AssetMap(site_hosts=HOSTS, flat=flat)
    assert m.add_page(BASE + path) == expected


def test_flat_layout_is_unaffected_by_the_link_style():
    """Single-file pages are already files; there is no folder to link to."""
    for folder_links in (True, False):
        m = AssetMap(site_hosts=HOSTS, flat=True, folder_links=folder_links)
        m.add_page(BASE + "/")
        m.add_page(BASE + "/about-us/")
        assert m.href_for(BASE + "/about-us/", "index.html") == "about-us.html"


def test_flat_layout_links_between_pages():
    m = AssetMap(site_hosts=HOSTS, flat=True)
    for path in ("/", "/about-us/", "/about/team/"):
        m.add_page(BASE + path)
    assert m.href_for(BASE + "/", "about/team.html") == "../index.html"
    assert m.href_for(BASE + "/about-us/", "about/team.html") == "../about-us.html"
    assert m.href_for(BASE + "/about/team/", "index.html") == "about/team.html"


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
CSS = """
@import "reset.css";
@import url('theme.css');
.a { background: url(img/a.png); }
.b { background-image: url("../img/b.jpg"); }
@font-face { src: url('f.woff2') format("woff2"), url(f.woff) format("woff"); }
.c { background: url( 'sp ace.png' ); }
.d { background: image-set("x@2x.png" 2x); }
.e { background: url(data:image/png;base64,AAA); }
.f { background: url(https://cdn.example.com/z.png); }
.g { cursor: url(c.cur), pointer; }
"""
CSS_BASE = BASE + "/wp-content/themes/t/style.css"


def test_extract_css_urls_finds_every_form():
    found = extract_css_urls(CSS, CSS_BASE)
    names = {u.rsplit("/", 1)[-1] for u in found}
    # Unquoted url() is the most common form in minified theme CSS.
    assert {"reset.css", "theme.css", "a.png", "b.jpg", "f.woff2", "f.woff",
            "x@2x.png", "c.cur"} <= names
    assert "z.png" in names           # external is discovered
    assert not any("base64" in u for u in found)   # data: URIs are not URLs to fetch


def test_rewrite_css_leaves_external_and_data_uris_alone():
    out, count = rewrite_css_urls(
        CSS, CSS_BASE, lambda u: "L/" + u.rsplit("/", 1)[-1] if "127.0.0.1" in u else None
    )
    assert count == 9
    assert "data:image/png;base64,AAA" in out
    assert "https://cdn.example.com/z.png" in out
    assert 'url("L/a.png")' in out
    assert 'url("L/f.woff")' in out


def test_rewrite_css_quotes_the_replacement():
    out, _ = rewrite_css_urls(".a{background:url(a.png)}", CSS_BASE, lambda u: "has space.png")
    assert 'url("has space.png")' in out


# ---------------------------------------------------------------------------
# HTML processing
# ---------------------------------------------------------------------------
class _FakeAssetManager:
    """Records registrations and maps a URL to a plausible output path."""

    def __init__(self, asset_map: AssetMap, external_policy=ExternalResourcePolicy.PRESERVE):
        self.map = asset_map
        self.registered: list[str] = []
        self.external_policy = external_policy

    def register(self, url: str, *, content_type: str = "", source: str = "html"):
        from app.utils.urls import ResourceClass, classify_url, normalise_url

        url = normalise_url(url, force_trailing_slash=False)
        if not url:
            return None
        if classify_url(url, HOSTS) is not ResourceClass.LOCAL:
            return None
        self.registered.append(url)
        return self.map.add_asset(url, content_type=content_type, source=source).output_path


def _process(html: str, output_path: str = "about/index.html", pages=("/", "/about/")):
    asset_map = AssetMap(site_hosts=HOSTS)
    for page in pages:
        asset_map.add_page(BASE + page)
    manager = _FakeAssetManager(asset_map)
    processor = HtmlProcessor(
        BASE, asset_map, manager, ConversionOptions(),
        site_hosts=HOSTS, detector=DynamicFeatureDetector(),
    )
    return processor.process(BASE + "/about/", html, output_path), manager, processor


def test_images_srcset_and_links_are_localised():
    html = f"""<html><head>
      <link rel="stylesheet" href="{BASE}/wp-content/themes/t/style.css?ver=1">
    </head><body>
      <img src="{BASE}/wp-content/uploads/a.png"
           srcset="{BASE}/wp-content/uploads/a-300.png 300w, {BASE}/wp-content/uploads/a-600.png 600w">
      <a href="{BASE}/">Home</a>
      <a href="https://external.example/page">External</a>
      <div style="background-image:url({BASE}/wp-content/uploads/bg.jpg)"></div>
    </body></html>"""
    result, manager, _ = _process(html)

    assert "../wp-content/uploads/a.png" in result.html
    assert "../wp-content/themes/t/style.css" in result.html
    assert "a-300.png 300w" in result.html
    assert 'href="../"' in result.html, "a page link must point at the page's folder"
    assert "https://external.example/page" in result.html, "external links must be preserved"
    assert result.preserved_external >= 1
    assert "../wp-content/uploads/bg.jpg" in result.html


def test_javascript_and_classes_are_preserved():
    html = """<html><body>
      <div class="elementor-widget et_pb_row" data-settings='{"a":1}' aria-label="x" id="keep">
        <script>window.THEME = {slider: true};</script>
      </div>
    </body></html>"""
    result, _, _ = _process(html)

    assert "elementor-widget et_pb_row" in result.html
    assert 'data-settings=' in result.html
    assert 'aria-label="x"' in result.html
    assert 'id="keep"' in result.html
    assert "window.THEME" in result.html, "front-end JavaScript must not be removed"


def test_absolute_urls_inside_inline_scripts_are_rewritten():
    """wp_localize_script embeds absolute URLs that would point at a dead port."""
    html = (
        "<html><body><script>"
        'var cfg={"js":"' + BASE + '/wp-content/plugins/p/app.js",'
        '"esc":"' + BASE.replace("/", "\\/") + '\\/wp-content\\/plugins\\/p\\/b.js"};'
        "</script></body></html>"
    )
    result, manager, _ = _process(html)

    assert BASE not in result.html, "the temporary server URL leaked into the output"
    assert "../wp-content/plugins/p/app.js" in result.html
    assert "..\\/wp-content\\/plugins\\/p\\/b.js" in result.html, \
        "JSON-escaped URLs must stay JSON-escaped"
    assert any("app.js" in u for u in manager.registered)


def test_import_maps_are_rewritten():
    html = (
        '<html><head><script type="importmap">'
        '{"imports":{"@wordpress/interactivity":"' + BASE + '/wp-includes/js/i.min.js"}}'
        "</script></head><body></body></html>"
    )
    result, _, _ = _process(html)
    assert BASE not in result.html
    assert "wp-includes/js/i.min.js" in result.html


def test_runtime_injected_scripts_are_removed():
    """Their injector is preserved and re-creates them; keeping both double-loads."""
    html = ('<html><body><script src="/a.js"></script>'
            '<script data-wpsc-injected="1" src="/b.js"></script></body></html>')
    result, _, _ = _process(html)
    assert "/a.js" in result.html or "a.js" in result.html
    assert "b.js" not in result.html
    assert "data-wpsc-injected" not in result.html


def test_forms_keep_their_markup_but_lose_the_dead_endpoint():
    html = f"""<html><body>
      <form class="wpcf7-form" method="post" action="{BASE}/wp-admin/admin-post.php">
        <input type="text" name="n"><button>Send</button>
      </form>
    </body></html>"""
    result, _, processor = _process(html)

    assert 'class="wpcf7-form"' in result.html
    assert "<button>Send</button>" in result.html
    assert 'action=""' in result.html
    assert "data-wpsc-static" in result.html

    names = {f["name"] for f in processor.detector.report()}
    assert "Contact Form 7" in names


def test_form_posting_to_a_third_party_is_left_working():
    html = ('<html><body><form action="https://mailchimp.example/subscribe" method="post">'
            "</form></body></html>")
    result, _, _ = _process(html)
    assert "https://mailchimp.example/subscribe" in result.html


def test_admin_links_are_neutralised():
    from bs4 import BeautifulSoup

    html = f'<html><body><a href="{BASE}/wp-admin/">Dashboard</a></body></html>'
    result, _, _ = _process(html)

    anchor = BeautifulSoup(result.html, "html.parser").find("a")
    assert anchor["href"] == "#", "an admin link must not point at the dead install"
    assert anchor.get("data-wpsc-removed") == "wordpress-admin"
    assert anchor.get_text() == "Dashboard", "the visible text stays, so layout is unchanged"


def test_wp_json_link_tag_is_dropped():
    html = f'<html><head><link rel="https://api.w.org/" href="{BASE}/wp-json/"></head></html>'
    result, _, _ = _process(html)
    assert "api.w.org" not in result.html


def test_base_tag_is_consumed_not_left_behind():
    html = f'<html><head><base href="{BASE}/sub/"></head><body><img src="a.png"></body></html>'
    result, manager, _ = _process(html)
    assert "<base" not in result.html
    assert any(u.endswith("/sub/a.png") for u in manager.registered)


def test_dynamic_feature_detection_reports_what_it_finds():
    html = """<html><body>
      <form role="search"><input type="search"></form>
      <div id="comments"></div>
      <script>fetch('/wp-json/wp/v2/posts');</script>
    </body></html>"""
    _, _, processor = _process(html)
    names = {f["name"] for f in processor.detector.report()}
    assert "WordPress search" in names
    assert "Comments" in names
    assert "WordPress REST API" in names


def test_builder_detection():
    assert "Elementor" in detect_builders(['<div class="elementor-widget x">'])
    assert "Divi" in detect_builders(['<div class="et_pb_section">'])
    assert "WPBakery Page Builder" in detect_builders(['<div class="vc_row">'])
    assert detect_builders(["<div>plain</div>"]) == []


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------
@pytest.fixture
def built_site(tmp_path: Path) -> Path:
    site = tmp_path / "output"
    (site / "about").mkdir(parents=True)
    (site / "wp-content" / "uploads").mkdir(parents=True)
    (site / "wp-includes" / "js").mkdir(parents=True)

    (site / "index.html").write_text(
        '<html><body><a href="about/">About</a>'
        '<img src="wp-content/uploads/a.png">'
        '<script src="wp-includes/js/core.js"></script></body></html>',
        encoding="utf-8",
    )
    (site / "about" / "index.html").write_text(
        '<html><body><a href="../">Home</a></body></html>', encoding="utf-8"
    )
    (site / "wp-content" / "uploads" / "a.png").write_bytes(b"\x89PNG\r\n")
    (site / "wp-includes" / "js" / "core.js").write_text("console.log(1)", encoding="utf-8")
    (site / "robots.txt").write_text("User-agent: *\n", encoding="utf-8")
    return site


def test_validator_reports_a_clean_site(built_site: Path):
    report = validate_output(built_site)
    assert report.html_files == 2
    assert report.broken_links == []
    assert report.missing_assets == []


def test_validator_finds_a_missing_asset(built_site: Path):
    (built_site / "wp-content" / "uploads" / "a.png").unlink()
    report = validate_output(built_site)
    assert len(report.missing_assets) == 1
    assert "a.png" in report.missing_assets[0].reference


def test_validator_finds_a_broken_link(built_site: Path):
    (built_site / "index.html").write_text(
        '<html><body><a href="nowhere/">x</a></body></html>', encoding="utf-8"
    )
    report = validate_output(built_site)
    assert len(report.broken_links) == 1


def test_validator_does_not_flag_parent_links_as_broken(built_site: Path):
    """'../' resolves to the export root, whose index.html does exist."""
    report = validate_output(built_site)
    assert not any(b.reference == "../" for b in report.broken_links)


def test_zip_contains_wordpress_front_end_assets(built_site: Path, tmp_path: Path):
    """wp-includes holds real front-end CSS and JS and must be packaged."""
    archive = tmp_path / "site.zip"
    build_zip(built_site, archive)
    names = set(zipfile.ZipFile(archive).namelist())

    assert "index.html" in names
    assert "wp-includes/js/core.js" in names
    assert "wp-content/uploads/a.png" in names


@pytest.mark.parametrize(
    "name",
    ["wp-config.php", "index.php", "database.sql", ".env", "debug.log", "php.ini",
     "wp-content/mu-plugins/x.php", ".htaccess"],
)
def test_server_side_and_secret_files_are_excluded(name: str):
    assert should_exclude(name) is not None


@pytest.mark.parametrize(
    "name",
    ["index.html", "about/index.html", "wp-content/uploads/a.png",
     "wp-includes/js/dist/script-modules/interactivity/index.min.js",
     "wp-includes/blocks/navigation/style.min.css", "robots.txt", "sitemap.xml"],
)
def test_deliverable_files_are_kept(name: str):
    assert should_exclude(name) is None


def test_secrets_never_reach_the_archive(built_site: Path, tmp_path: Path):
    (built_site / "wp-config.php").write_text("<?php define('DB_PASSWORD','hunter2');", encoding="utf-8")
    (built_site / "dump.sql").write_text("INSERT INTO wp_users ...", encoding="utf-8")

    archive = tmp_path / "site.zip"
    result = build_zip(built_site, archive)
    names = set(zipfile.ZipFile(archive).namelist())

    assert "wp-config.php" not in names
    assert "dump.sql" not in names
    assert len(result.excluded) == 2
    assert verify_zip(archive) == []


def test_verify_detects_a_file_left_out_of_the_archive(built_site: Path, tmp_path: Path):
    """Guards the bug where an over-broad rule silently dropped real assets."""
    archive = tmp_path / "site.zip"
    build_zip(built_site, archive)

    # Add a file after packaging to simulate one that was wrongly skipped.
    (built_site / "wp-content" / "uploads" / "late.png").write_bytes(b"\x89PNG")

    problems = verify_zip(archive, source_dir=built_site)
    assert any("late.png" in p for p in problems)


def test_verify_flags_a_missing_index(tmp_path: Path):
    site = tmp_path / "out"
    site.mkdir()
    (site / "page.html").write_text("<html></html>", encoding="utf-8")
    archive = tmp_path / "x.zip"
    build_zip(site, archive)
    assert any("index.html" in p for p in verify_zip(archive))


def test_empty_output_is_refused(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        build_zip(empty, tmp_path / "x.zip")


# ---------------------------------------------------------------------------
# Links written with the site's public domain
# ---------------------------------------------------------------------------
LIVE_HOSTS = HOSTS | {"example.com", "www.example.com"}


def _process_live(html: str, output_path: str = "about/index.html", folder_links: bool = True):
    asset_map = AssetMap(site_hosts=LIVE_HOSTS, folder_links=folder_links)
    for page in ("/", "/about/", "/contact-us/"):
        asset_map.add_page(BASE + page)
    processor = HtmlProcessor(
        BASE, asset_map, _FakeAssetManager(asset_map), ConversionOptions(),
        site_hosts=LIVE_HOSTS, detector=DynamicFeatureDetector(),
    )
    return processor.process(BASE + "/about/", html, output_path)


def test_public_domain_links_resolve_to_exported_pages():
    """Builder footers and buttons keep https://example.com/... in their data."""
    html = """<html><body>
      <a class="elementskit-btn" href="https://example.com/contact-us/">contact us</a>
      <a href="https://www.example.com/">Home</a>
      <a href="//example.com/contact-us/#form">Form</a>
    </body></html>"""
    result = _process_live(html)

    assert 'href="../contact-us/"' in result.html
    assert 'href="../"' in result.html
    assert 'href="../contact-us/#form"' in result.html
    assert "example.com" not in result.html, "a link still points at the live domain"

    named = _process_live(html, folder_links=False)
    assert 'href="../contact-us/index.html"' in named.html
    assert 'href="../index.html"' in named.html


def test_unexported_internal_page_uses_the_same_link_style():
    """A link to a page that was not exported still looks like every other link."""
    html = ('<html><body><a href="/missing-page/">x</a>'
            '<a href="https://example.com/gone/">y</a></body></html>')
    result = _process_live(html)
    assert 'href="../missing-page/"' in result.html
    assert 'href="../gone/"' in result.html

    named = _process_live(html, folder_links=False)
    assert 'href="../missing-page/index.html"' in named.html
    assert 'href="../gone/index.html"' in named.html


def test_url_replacements_cover_json_escaped_urls():
    from app.services.pipeline import _site_url_replacements

    out = _site_url_replacements({"https://example.com": "http://127.0.0.1:8080"})
    assert out["https://example.com"] == "http://127.0.0.1:8080"
    assert out[r"https:\/\/example.com"] == r"http:\/\/127.0.0.1:8080"


def test_generated_css_is_repointed_in_place(tmp_path):
    from app.services.wordpress_restorer import rewrite_generated_css

    folder = tmp_path / "wp-content" / "uploads" / "elementor" / "css"
    folder.mkdir(parents=True)
    css = folder / "post-9.css"
    css.write_text('.hero{background-image:url("https://example.com/wp-content/uploads/a.jpg")}')
    untouched = folder / "post-1.css"
    untouched.write_text(".x{color:red}")

    files, count = rewrite_generated_css(tmp_path, {"https://example.com": BASE})

    assert (files, count) == (1, 1)
    assert css.read_text() == f'.hero{{background-image:url("{BASE}/wp-content/uploads/a.jpg")}}'
    assert untouched.read_text() == ".x{color:red}"


# ---------------------------------------------------------------------------
# Assets that only a render reveals
# ---------------------------------------------------------------------------
def test_on_demand_script_chunks_are_included(tmp_path):
    """A carousel's code is fetched only when a carousel runs; it must ship."""
    from app.services.asset_manager import AssetManager

    js = tmp_path / "wp-content" / "plugins" / "elementor" / "assets" / "js"
    js.mkdir(parents=True)
    for name in ("webpack.runtime.min.js", "image-carousel.78b8.bundle.min.js",
                 "image-carousel.6b6c.bundle.js", "readme.txt"):
        (js / name).write_text("x")

    asset_map = AssetMap(site_hosts=HOSTS)
    manager = AssetManager(BASE, tmp_path / "out", asset_map, ConversionOptions(),
                           site_hosts=HOSTS, document_root=tmp_path)
    manager.register(BASE + "/wp-content/plugins/elementor/assets/js/webpack.runtime.min.js?ver=1")

    assert manager.register_lazy_chunks() == 1
    names = {url.rsplit("/", 1)[1] for url in asset_map.assets}
    assert "image-carousel.78b8.bundle.min.js" in names
    assert "image-carousel.6b6c.bundle.js" not in names, "the unminified twin is not loaded"
    assert manager.register_lazy_chunks() == 0, "registering twice must not duplicate"


def test_resource_lists_survive_a_resume(tmp_path):
    from app.services.browser_renderer import NetworkResource
    from app.services.pipeline import _load_resources, _save_resources

    raw = tmp_path / "home.html"
    raw.write_text("<html></html>")
    _save_resources(raw, [NetworkResource(url=BASE + "/a.js", status=200,
                                          content_type="text/javascript", resource_type="script")])

    loaded = _load_resources(raw)
    assert [(r.url, r.resource_type, r.status) for r in loaded] == [(BASE + "/a.js", "script", 200)]
    assert _load_resources(tmp_path / "never-rendered.html") == []


# ---------------------------------------------------------------------------
# Page paths that real sites produce by mistake
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path,expected",
    [
        ("/a286-foil/%20/", "a286-foil/index.html"),        # a level that is only a space
        ("/x./", "x/index.html"),                            # trailing dot: invalid on Windows
        ("/con/", "_con/index.html"),                        # reserved device name
        ("/a%3Ab/", "a_b/index.html"),                       # forbidden character
        ("/sheets-and%20plates/", "sheets-and plates/index.html"),
    ],
)
def test_page_paths_are_always_creatable(path: str, expected: str):
    from app.utils.urls import url_to_output_path

    assert url_to_output_path(BASE + path) == expected


def test_links_to_folders_with_spaces_are_encoded():
    m = AssetMap(site_hosts=HOSTS)
    m.add_page(BASE + "/")
    m.add_page(BASE + "/sheets-and%20plates/")
    assert m.href_for(BASE + "/sheets-and%20plates/", "index.html") == "sheets-and%20plates/"
    named = AssetMap(site_hosts=HOSTS, folder_links=False)
    named.add_page(BASE + "/sheets-and%20plates/")
    assert named.href_for(BASE + "/sheets-and%20plates/", "index.html") ==         "sheets-and%20plates/index.html"
