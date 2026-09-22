"""Shared harness that restores the demo fixture and serves it.

Restoring a WordPress install takes around half a minute, so the integration
tests share one running site rather than rebuilding it per test. The same
harness is used by the scratch scripts during development.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.config import ConversionOptions
from app.services import wordpress_restorer as restorer
from app.services.runtime_provisioner import RuntimeSet, ensure_runtimes
from app.services.wordpress_runner import MysqlServer, PhpServer, find_free_port
from app.services.wpress_extractor import get_extractor

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "demo-site.wpress"
ORIGINAL_URL = "https://northwind-studio.example"


@dataclass
class RestoredSite:
    """A running, restored WordPress plus everything needed to inspect it."""

    workspace: Path
    base_url: str
    mysql: MysqlServer
    php: PhpServer
    database: str
    table_prefix: str
    original_url: str
    layout: restorer.ArchiveLayout
    runtimes: RuntimeSet

    def stop(self) -> None:
        self.php.stop()
        self.mysql.stop()

    def cleanup(self, remove: bool = True) -> None:
        self.stop()
        if remove:
            shutil.rmtree(self.workspace, ignore_errors=True)


def restore_demo_site(workspace: Path | None = None, *, quiet: bool = True) -> RestoredSite:
    """Extract, restore and serve the demo fixture. Returns once it responds."""
    if not FIXTURE.is_file():
        raise FileNotFoundError(
            f"{FIXTURE} is missing. Build it first with:\n"
            f"    python scripts/make_fixture.py"
        )

    if quiet:
        logging.getLogger("app.services.wordpress_restorer").setLevel(logging.WARNING)

    workspace = Path(workspace or tempfile.mkdtemp(prefix="wpsc-test-"))
    workspace.mkdir(parents=True, exist_ok=True)

    runtimes = ensure_runtimes(PROJECT_ROOT / "runtime")

    extracted = workspace / "extracted"
    get_extractor().extract(FIXTURE, extracted)
    layout = restorer.inspect_archive(extracted)

    core = restorer.provision_wordpress_core(layout.wordpress_version, PROJECT_ROOT / "runtime")
    wp_root = workspace / "wordpress"
    restorer.build_wordpress_tree(layout, wp_root, core)

    mysql = MysqlServer(runtime=runtimes.mysql, data_dir=workspace / "mysql")
    mysql.start()
    database = "wpsc_test"
    mysql.create_database(database)

    prefix = restorer.detect_table_prefix(layout.sql_dump)
    restorer.import_sql_dump(
        layout.sql_dump, mysql, database, client_binary=runtimes.mysql.client_binary
    )

    original = restorer.detect_site_url(mysql, database, prefix, layout) or ORIGINAL_URL

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    restorer.write_wp_config(
        wp_root,
        db_name=database, db_user="root", db_password="",
        db_host=f"127.0.0.1:{mysql.port}",
        table_prefix=prefix, site_url=base_url,
    )
    restorer.write_control_plugin(wp_root)
    restorer.replace_urls_in_database(mysql, database, {original: base_url})

    # The same repair the pipeline performs, and for the same reason: an
    # All-in-One WP Migration export omits stylesheet, template and
    # active_plugins, so a restore that skips this step has no theme and no
    # plugins -- a site that answers 200 with an empty body.
    #
    # The harness did skip it, and the integration suite passed anyway,
    # because the fixture committed to the repository predates the change that
    # made make_fixture.py omit those options as faithfully as AI1WM does. A
    # freshly built fixture exposed it immediately. Restoring here the way the
    # pipeline restores is what keeps the suite honest.
    restorer.repair_activation_state(mysql, database, prefix, wp_root, recorded=layout)
    restorer.configure_for_static_export(mysql, database, prefix, base_url)

    php = PhpServer(runtime=runtimes.php, document_root=wp_root, port=port, workers=4)
    php.start()
    ok, detail = php.wait_until_wordpress_responds(timeout=180)
    if not ok:
        php.stop()
        mysql.stop()
        raise RuntimeError(f"the restored demo site did not come up: {detail}")

    return RestoredSite(
        workspace=workspace,
        base_url=base_url,
        mysql=mysql,
        php=php,
        database=database,
        table_prefix=prefix,
        original_url=original,
        layout=layout,
        runtimes=runtimes,
    )


def default_options(**overrides) -> ConversionOptions:
    """Conversion options tuned for fast tests."""
    base = {
        "render_concurrency": 2,
        "screenshot_comparison": False,
        "mobile_validation": False,
        "extra_settle_ms": 250,
    }
    base.update(overrides)
    return ConversionOptions(**base)
