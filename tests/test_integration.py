"""End-to-end tests against a genuinely restored WordPress.

These need PHP, MariaDB, Chromium and the demo fixture, so they are marked
``integration`` and skipped when any of those is unavailable::

    pytest -m integration
    pytest -m "not integration"     # the default fast suite
"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Restoration
# ---------------------------------------------------------------------------
def test_site_is_restored_and_serving(restored_site):
    import httpx

    response = httpx.get(restored_site.base_url + "/", timeout=60)
    assert response.status_code == 200
    assert "Northwind Studio" in response.text
    assert "northwind-studio.example" not in response.text, \
        "the original domain leaked into the rendered page"


def test_serialized_options_survive_the_url_rewrite(restored_site):
    """The whole point of the serialized-safe replacement."""
    from app.services.wordpress_restorer import read_option
    from app.utils.phpserialize import is_serialized, loads

    raw = read_option(
        restored_site.mysql, restored_site.database, restored_site.table_prefix,
        "wpsc_demo_serialized",
    )
    assert raw is not None
    assert is_serialized(raw), "a textual replacement corrupted the serialized option"

    value = loads(raw)
    assert value["home"].decode() == restored_site.base_url
    assert value["nested"][b"count" if b"count" in value["nested"] else "count"] == 7


def test_theme_mods_still_parse(restored_site):
    """Theme mods hold the logo and menu locations; corruption is visible."""
    from app.services.wordpress_restorer import read_option
    from app.utils.phpserialize import is_serialized

    theme = restored_site.layout
    for suffix in ("theme_mods_twentytwentyfive", "theme_mods_twentytwentyfour"):
        raw = read_option(restored_site.mysql, restored_site.database,
                          restored_site.table_prefix, suffix)
        if raw:
            assert is_serialized(raw)
            return
    pytest.skip("no theme_mods option present in this fixture")


def test_manifest_reports_custom_post_types(restored_site):
    from app.services.url_discovery import fetch_manifest

    manifest = fetch_manifest(restored_site.base_url)
    assert manifest is not None
    assert "project" in manifest.post_types, \
        "custom post types must be visible, which needs the wp_loaded hook"
    assert "discipline" in manifest.taxonomies
    assert manifest.theme.get("name")


def test_permalinks_come_from_wordpress_not_from_guesswork(restored_site):
    """The CPT uses rewrite slug 'projects'; guessing would produce /project/."""
    from app.config import ConversionOptions
    from app.services.url_discovery import discover_from_wordpress

    records = discover_from_wordpress(restored_site.base_url, ConversionOptions())
    assert records is not None

    urls = {r.url for r in records}
    assert any("/projects/aurora-rebrand/" in u for u in urls)
    assert not any("/project/aurora-rebrand/" in u for u in urls)


@pytest.mark.parametrize(
    "path", ["/", "/about/", "/services/", "/contact/", "/journal/", "/projects/",
             "/category/process/", "/about/accessibility/"],
)
def test_expected_pages_respond(restored_site, path: str):
    import httpx

    response = httpx.get(restored_site.base_url + path, timeout=60, follow_redirects=True)
    assert response.status_code == 200, f"{path} returned {response.status_code}"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_rendering_captures_a_real_dom(restored_site):
    from app.services.browser_renderer import BrowserRenderer
    from tests.harness import default_options

    async def run():
        async with BrowserRenderer(default_options(), base_url=restored_site.base_url,
                                   timeout_ms=45_000) as renderer:
            return await renderer.render(restored_site.base_url + "/")

    page = asyncio.run(run())

    assert page.ok
    assert page.status == 200
    assert "<html" in page.html.lower()
    assert "srcset=" in page.html, "WordPress responsive images should be present"
    assert page.links, "no links were extracted"
    assert not [r for r in page.resources if r.failed], "a resource failed to load"
    assert not page.page_errors, f"the original page threw: {page.page_errors}"


# ---------------------------------------------------------------------------
# Whole pipeline
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def converted(tmp_path_factory, demo_fixture):
    """Run one full conversion and share its results across the assertions."""
    import shutil

    from app.config import ConversionOptions, Settings
    from app.models.job import JobStore
    from app.services.pipeline import ConversionPipeline
    from app.services.runtime_provisioner import find_mysql, find_php

    root = Path(__file__).resolve().parents[1]
    if find_php(None, root / "runtime") is None or find_mysql(None, root / "runtime") is None:
        pytest.skip("PHP and/or MariaDB are unavailable")

    workspace = tmp_path_factory.mktemp("conversion")
    settings = Settings(
        jobs_dir=workspace / "jobs",
        runtime_dir=root / "runtime",
        database_path=workspace / "jobs.sqlite3",
    )
    settings.ensure_directories()

    store = JobStore(settings.database_path)
    options = ConversionOptions(
        # The assertions below check folder paths (about/index.html); flat is
        # the default, so ask for folders explicitly.
        preserve_url_structure=True,
        render_concurrency=3,
        include_tags=True,
        screenshot_comparison=False,   # covered separately; keeps this test quick
        mobile_validation=False,
        extra_settle_ms=250,
    )

    job = store.create("demo-site.wpress", options, demo_fixture.stat().st_size)
    job_dir = settings.job_dir(job.id)
    (job_dir / "input").mkdir(parents=True, exist_ok=True)
    archive = job_dir / "demo-site.wpress"
    shutil.copy2(demo_fixture, archive)

    zip_path = ConversionPipeline(job, store, settings).run(archive)

    result = {
        "zip": zip_path,
        "job": store.get(job.id),
        "output": job_dir / "output",
        "report": job_dir / "report" / "conversion-report.html",
        "store": store,
        "job_id": job.id,
    }
    yield result
    store.close()


def test_conversion_completes(converted):
    from app.models.job import JobStatus

    job = converted["job"]
    assert job.status is JobStatus.COMPLETED
    assert job.overall_progress == 1.0
    assert job.error is None


def test_every_page_rendered(converted):
    summary = converted["job"].summary
    assert summary["urls_rendered"] > 10
    assert summary["urls_failed"] == 0


def test_export_has_no_broken_links_or_missing_assets(converted):
    summary = converted["job"].summary
    assert summary["broken_links"] == 0, "the export links to files that do not exist"
    assert summary["missing_assets"] == 0


def test_no_javascript_errors_in_the_export(converted):
    assert converted["job"].summary["console_errors"] == 0


def test_original_domain_does_not_appear_anywhere(converted):
    """Nothing in the deliverable may depend on the source site being online."""
    offenders = []
    for path in converted["output"].rglob("*"):
        if path.is_file() and path.suffix.lower() in {".html", ".css", ".js", ".xml"}:
            if "northwind-studio.example" in path.read_text(encoding="utf-8", errors="replace"):
                offenders.append(path.name)
    assert not offenders, f"the original domain survives in {offenders[:5]}"


def test_temporary_render_url_does_not_leak(converted):
    """The loopback URL of the throwaway server must not survive either."""
    import re

    pattern = re.compile(r"127\.0\.0\.1:\d{4,5}")
    offenders = [
        path.name
        for path in converted["output"].rglob("*")
        if path.is_file() and path.suffix.lower() in {".html", ".css", ".js"}
        and pattern.search(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not offenders, f"the temporary server URL survives in {offenders[:5]}"


@pytest.mark.parametrize(
    "relative",
    ["index.html", "about/index.html", "services/index.html", "contact/index.html",
     "journal/index.html", "projects/index.html", "robots.txt", "sitemap.xml"],
)
def test_expected_files_exist(converted, relative: str):
    assert (converted["output"] / relative).is_file()


def test_url_structure_is_preserved(converted):
    assert (converted["output"] / "about" / "accessibility" / "index.html").is_file(), \
        "nested pages should keep their hierarchy"


def test_zip_is_complete_and_clean(converted):
    from app.services.zip_builder import verify_zip

    assert verify_zip(converted["zip"], source_dir=converted["output"]) == []

    names = set(zipfile.ZipFile(converted["zip"]).namelist())
    assert "index.html" in names
    assert any(n.endswith(".css") for n in names)
    assert any(n.endswith(".js") for n in names)
    assert not any(n.endswith(".php") for n in names)
    assert not any("wp-config" in n for n in names)
    assert not any(n.endswith(".sql") for n in names)


def test_wordpress_front_end_assets_are_packaged(converted):
    """wp-includes carries the Interactivity API that drives the nav block."""
    names = set(zipfile.ZipFile(converted["zip"]).namelist())
    assert any(n.startswith("wp-includes/") for n in names), \
        "core front-end assets were dropped; the exported menus would break"


def test_report_is_written_and_honest(converted):
    html = converted["report"].read_text(encoding="utf-8")
    assert "Conversion report" in html or "conversion" in html.lower()
    assert "Static HTML limitations" in html
    # The demo site has comments and a REST-calling script, so something must
    # be reported rather than the export being presented as lossless.
    assert converted["job"].summary["dynamic_features"]


def test_per_url_checkpoints_were_recorded(converted):
    from app.models.job import UrlState

    counts = converted["store"].url_counts(converted["job_id"])
    assert counts.get(str(UrlState.WRITTEN), 0) > 10
    assert counts.get(str(UrlState.FAILED), 0) == 0


def test_exported_site_works_when_served_standalone(converted):
    """The acceptance criterion: it runs on a plain static server."""
    import httpx

    from app.services.static_validator import StaticSiteServer

    with StaticSiteServer(converted["output"]) as server:
        home = httpx.get(server.base_url + "/index.html", timeout=30)
        assert home.status_code == 200
        assert "Northwind Studio" in home.text

        about = httpx.get(server.base_url + "/about/", timeout=30)
        assert about.status_code == 200


def test_exported_site_renders_without_errors_in_a_browser(converted):
    from app.services.browser_renderer import BrowserRenderer
    from app.services.static_validator import StaticSiteServer
    from tests.harness import default_options

    async def run(base_url: str):
        async with BrowserRenderer(default_options(), base_url=base_url,
                                   timeout_ms=30_000, retries=0) as renderer:
            return [
                await renderer.render(f"{base_url}/index.html"),
                await renderer.render(f"{base_url}/about/index.html"),
            ]

    with StaticSiteServer(converted["output"]) as server:
        pages = asyncio.run(run(server.base_url))

    for page in pages:
        assert page.ok
        assert not page.page_errors, f"{page.url} threw {page.page_errors}"
        failed = [r for r in page.resources if r.failed or (r.status or 0) >= 400]
        assert not failed, f"{page.url} failed to load {[r.url for r in failed]}"
