"""Build a realistic ``.wpress`` test archive from a real WordPress install.

The conversion pipeline can only be trusted if it is tested against markup that
WordPress actually produced -- a hand-written HTML fixture would not exercise
block themes, srcset generation, serialized theme mods, menus or pagination.

So this script installs a genuine WordPress, seeds it with representative
content, exports it exactly the way All-in-One WP Migration does, and writes a
``.wpress`` archive:

    database.sql      mysqldump of every table
    package.json      site metadata (SiteURL, WordPress version, plugins)
    wp-content/       themes, plugins, uploads, mu-plugins

Usage:
    python scripts/make_fixture.py [--output tests/fixtures/demo-site.wpress]
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.runtime_provisioner import find_mysql, find_php, ensure_runtimes  # noqa: E402
from app.services.wordpress_restorer import provision_wordpress_core  # noqa: E402
from app.services.wordpress_runner import MysqlServer  # noqa: E402
from app.services.wpress_extractor import WpressWriter  # noqa: E402

logger = logging.getLogger("make_fixture")

#: Pinned, not "latest". A test fixture exists to hold the pipeline still while
#: it is checked; if it tracked WordPress releases, a green suite would go red
#: on a day nobody touched this code, and the fixture committed here would stop
#: matching the one CI builds -- which is exactly what happened: 7.1 in the
#: repository against 7.1.1 downloaded on the runner. Raise it deliberately,
#: and rebuild the committed fixture in the same commit.
FIXTURE_WORDPRESS_VERSION = "7.1"

ORIGINAL_SITE_URL = "https://northwind-studio.example"
"""A domain that does not resolve, on purpose: if any stage of the pipeline
fails to rewrite a URL, the resulting broken link is obvious rather than
silently fetching from a live site."""


DEMO_PLUGIN = """<?php
/**
 * Plugin Name: Northwind Demo Content
 * Description: Registers the custom post type and shortcode used by the demo site.
 * Version: 1.0.0
 */

add_action( 'init', function () {
    register_post_type( 'project', array(
        'labels'       => array( 'name' => 'Projects', 'singular_name' => 'Project' ),
        'public'       => true,
        'has_archive'  => true,
        'rewrite'      => array( 'slug' => 'projects' ),
        'show_in_rest' => true,
        'supports'     => array( 'title', 'editor', 'thumbnail', 'excerpt' ),
    ) );

    register_taxonomy( 'discipline', 'project', array(
        'labels'       => array( 'name' => 'Disciplines' ),
        'public'       => true,
        'hierarchical' => true,
        'rewrite'      => array( 'slug' => 'discipline' ),
    ) );
} );

/* A front-end script, so the export has real JavaScript to preserve. */
add_action( 'wp_enqueue_scripts', function () {
    wp_register_script( 'northwind-demo', plugins_url( 'demo.js', __FILE__ ), array(), '1.0.0', true );
    wp_localize_script( 'northwind-demo', 'NorthwindDemo', array(
        'ajaxUrl' => admin_url( 'admin-ajax.php' ),
        'restUrl' => rest_url( 'northwind/v1/ping' ),
        'nonce'   => wp_create_nonce( 'northwind' ),
    ) );
    wp_enqueue_script( 'northwind-demo' );

    wp_enqueue_style( 'northwind-demo', plugins_url( 'demo.css', __FILE__ ), array(), '1.0.0' );
} );

/* An AJAX endpoint, so the pipeline has a dynamic feature to detect and report. */
add_action( 'wp_ajax_nopriv_northwind_ping', function () {
    wp_send_json_success( array( 'pong' => true ) );
} );
"""

DEMO_JS = """/* Front-end behaviour for the demo site: a mobile nav toggle, a lazy-loaded
   image observer and an AJAX call. All three must survive the static export. */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    document.documentElement.classList.add('northwind-ready');

    // Mobile navigation toggle.
    var toggle = document.querySelector('.northwind-nav-toggle');
    var nav = document.querySelector('.northwind-nav');
    if (toggle && nav) {
      toggle.addEventListener('click', function () {
        var open = nav.classList.toggle('is-open');
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
    }

    // Reveal-on-scroll, the usual source of lazily inserted content.
    var reveals = document.querySelectorAll('[data-reveal]');
    if ('IntersectionObserver' in window && reveals.length) {
      var observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add('is-revealed');
            observer.unobserve(entry.target);
          }
        });
      }, { rootMargin: '100px' });
      reveals.forEach(function (el) { observer.observe(el); });
    }
  });
})();
"""

DEMO_CSS = """/* Demo styles, including a background image and a web font reference so the
   CSS URL rewriter has real work to do. */
:root {
  --northwind-ink: #16191d;
  --northwind-paper: #fbfaf7;
}

@font-face {
  font-family: 'NorthwindMono';
  src: url('fonts/northwind-mono.woff2') format('woff2');
  font-display: swap;
}

.northwind-hero {
  background-image: url('images/texture.png');
  background-size: cover;
  padding: 4rem 1.5rem;
}

.northwind-nav-toggle { display: none; }

@media (max-width: 782px) {
  .northwind-nav-toggle { display: inline-flex; }
  .northwind-nav { display: none; }
  .northwind-nav.is-open { display: block; }
}

[data-reveal] { opacity: 0; transform: translateY(12px); transition: opacity .4s, transform .4s; }
[data-reveal].is-revealed { opacity: 1; transform: none; }
"""


def make_png(path: Path, width: int, height: int, colour: tuple[int, int, int], label: str) -> None:
    """Generate a placeholder image. Real pixels, so WordPress makes real sizes."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), colour)
    draw = ImageDraw.Draw(image)
    for x in range(0, width, 60):
        draw.line([(x, 0), (x, height)], fill=tuple(min(255, c + 18) for c in colour), width=2)
    draw.rectangle([10, 10, width - 10, height - 10], outline=(255, 255, 255), width=3)
    draw.text((28, 28), label, fill=(255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")


def build_fixture(output: Path, keep_workspace: bool = False) -> Path:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    project_root = Path(__file__).resolve().parents[1]
    cache = project_root / "runtime"

    runtimes = ensure_runtimes(cache, auto_provision=True)
    php, mysql_runtime = runtimes.php, runtimes.mysql
    logger.info("using PHP %s and %s %s", php.version, mysql_runtime.flavour, mysql_runtime.version)

    workspace = Path(tempfile.mkdtemp(prefix="wpsc-fixture-"))
    logger.info("building in %s", workspace)

    try:
        core = provision_wordpress_core(FIXTURE_WORDPRESS_VERSION, cache)
        site_root = workspace / "site"
        shutil.copytree(core, site_root)
        logger.info("copied WordPress core")

        # --- demo plugin, images, fonts --------------------------------------
        plugin_dir = site_root / "wp-content" / "plugins" / "northwind-demo"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "northwind-demo.php").write_text(DEMO_PLUGIN, encoding="utf-8")
        (plugin_dir / "demo.js").write_text(DEMO_JS, encoding="utf-8")
        (plugin_dir / "demo.css").write_text(DEMO_CSS, encoding="utf-8")
        make_png(plugin_dir / "images" / "texture.png", 400, 400, (32, 40, 52), "texture")

        # A real WOFF2 is not needed; the point is that the URL resolves and is
        # rewritten. A stub keeps the fixture small and dependency-free.
        font_path = plugin_dir / "fonts" / "northwind-mono.woff2"
        font_path.parent.mkdir(parents=True, exist_ok=True)
        font_path.write_bytes(b"wOF2" + b"\x00" * 60)

        uploads = site_root / "wp-content" / "uploads" / time.strftime("%Y/%m")
        make_png(uploads / "hero.png", 1600, 900, (40, 62, 88), "hero")
        make_png(uploads / "work-a.png", 1200, 800, (92, 58, 40), "work a")
        make_png(uploads / "work-b.png", 1200, 800, (48, 82, 60), "work b")
        make_png(uploads / "logo.png", 512, 512, (20, 22, 26), "logo")

        # --- database --------------------------------------------------------
        server = MysqlServer(runtime=mysql_runtime, data_dir=workspace / "mysql")
        server.start()
        database = "wpsc_fixture"
        server.create_database(database)

        config = f"""<?php
define( 'DB_NAME', '{database}' );
define( 'DB_USER', 'root' );
define( 'DB_PASSWORD', '' );
define( 'DB_HOST', '127.0.0.1:{server.port}' );
define( 'DB_CHARSET', 'utf8mb4' );
define( 'DB_COLLATE', '' );
$table_prefix = 'nw_';
define( 'WP_DEBUG', false );
define( 'WP_HOME', '{ORIGINAL_SITE_URL}' );
define( 'WP_SITEURL', '{ORIGINAL_SITE_URL}' );
define( 'AUTOMATIC_UPDATER_DISABLED', true );
if ( ! defined( 'ABSPATH' ) ) {{ define( 'ABSPATH', __DIR__ . '/' ); }}
require_once ABSPATH . 'wp-settings.php';
"""
        (site_root / "wp-config.php").write_text(config, encoding="utf-8")

        # Activate the demo plugin before seeding so its post type exists.
        activate = site_root / "activate.php"
        activate.write_text(
            "<?php\n"
            "define('WP_INSTALLING', true);\n"
            "require __DIR__ . '/wp-load.php';\n"
            "update_option('active_plugins', array('northwind-demo/northwind-demo.php'));\n"
            "echo \"activated\\n\";\n",
            encoding="utf-8",
        )

        def run_php(script: Path, *args: str) -> None:
            command = [str(php.binary)]
            if php.ini_path:
                command += ["-c", str(php.ini_path)]
            command += [str(script), *args]
            proc = subprocess.run(
                command, capture_output=True, text=True, timeout=900,
                cwd=str(site_root), shell=False,
            )
            output_text = (proc.stdout or "") + (proc.stderr or "")
            for line in output_text.splitlines():
                if line.strip():
                    logger.info("  php: %s", line.strip()[:200])
            if proc.returncode != 0:
                raise RuntimeError(f"{script.name} failed with code {proc.returncode}")

        seed_script = Path(__file__).resolve().parent / "seed_site.php"
        logger.info("installing and seeding WordPress")
        run_php(seed_script, str(site_root), ORIGINAL_SITE_URL)
        run_php(activate)
        # Re-run so the custom post type content is created with the CPT active.
        run_php(seed_script, str(site_root), ORIGINAL_SITE_URL)

        activate.unlink(missing_ok=True)

        # --- export ----------------------------------------------------------
        dump_path = workspace / "database.sql"
        # Found when the runtime was probed, which looks beside the server and
        # then in the places a distribution actually puts these tools. Looking
        # only next to the server found nothing on Ubuntu, where the server is
        # /usr/sbin/mariadbd and its tools are all in /usr/bin.
        dump_binary = mysql_runtime.dump_binary
        if dump_binary is None:
            raise RuntimeError(
                "no mariadb-dump/mysqldump found beside "
                f"{mysql_runtime.server_binary} or on PATH. "
                "On Debian/Ubuntu: sudo apt install mariadb-client"
            )

        logger.info("dumping the database with %s", dump_binary.name)
        with dump_path.open("wb") as out:
            proc = subprocess.run(
                [
                    str(dump_binary),
                    f"--host={server.host}", f"--port={server.port}",
                    f"--user={server.user}",
                    "--default-character-set=utf8mb4",
                    "--single-transaction", "--quick",
                    "--skip-lock-tables", "--no-tablespaces",
                    "--add-drop-table",
                    database,
                ],
                stdout=out, stderr=subprocess.PIPE, timeout=1800, shell=False,
            )
        if proc.returncode != 0:
            raise RuntimeError(f"database dump failed: {proc.stderr.decode(errors='replace')[:800]}")

        _make_dump_faithful_to_ai1wm(dump_path)

        server.stop()
        logger.info("dump is %.1f MiB", dump_path.stat().st_size / 1048576)

        # package.json, in the shape All-in-One WP Migration writes.
        package = {
            "Name": "Northwind Studio",
            "Version": "6.x",
            "WordPress": {"Version": _read_wp_version(site_root), "Content": "wp-content"},
            "Plugin": {"Version": "7.81"},
            "SiteURL": ORIGINAL_SITE_URL,
            "HomeURL": ORIGINAL_SITE_URL,
            "Database": {"Prefix": "nw_", "Charset": "utf8mb4"},
            "Plugins": ["northwind-demo/northwind-demo.php"],
            "Include": ["wp-content"],
            "Options": {"NoSpamComments": False},
            "ExportedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        package_path = workspace / "package.json"
        package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")

        # --- write the .wpress ------------------------------------------------
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        logger.info("writing %s", output)

        with WpressWriter(output) as writer:
            writer.add_file(dump_path, "database.sql")
            writer.add_file(package_path, "package.json")
            count = writer.add_tree(site_root / "wp-content", "wp-content")

        logger.info(
            "fixture written: %s (%.1f MiB, %d wp-content files)",
            output, output.stat().st_size / 1048576, count,
        )
        return output

    finally:
        if keep_workspace:
            logger.info("keeping workspace at %s", workspace)
        else:
            shutil.rmtree(workspace, ignore_errors=True)


#: Options All-in-One WP Migration strips out of its exports. Reproducing that
#: is what makes this fixture a fair test: a dump straight from mysqldump keeps
#: them, so a pipeline that never rebuilds the activation state still appears to
#: work -- right up until it meets a real backup and renders every page blank.
_AI1WM_OMITTED_OPTIONS = ("stylesheet", "template", "active_plugins")


def _make_dump_faithful_to_ai1wm(dump_path: Path) -> None:
    """Remove the option rows a real ``.wpress`` export does not contain.

    All-in-One WP Migration leaves the active theme and plugin list out because
    its own importer writes them on the destination. Any tool importing the
    dump directly has to reconstruct them.
    """
    import re as _re

    text = dump_path.read_text(encoding="utf-8", errors="surrogateescape")
    removed = 0

    for option in _AI1WM_OMITTED_OPTIONS:
        # Match one tuple inside a multi-row INSERT, or a whole single-row one.
        pattern = _re.compile(
            r"\((?:[^()']|'(?:[^'\\]|\\.)*')*?'"
            + _re.escape(option)
            + r"'(?:[^()']|'(?:[^'\\]|\\.)*')*?\),?"
        )
        text, count = pattern.subn("", text)
        removed += count

    # Tidy up any trailing comma a removal left before the statement terminator.
    text = _re.sub(r",\s*;", ";", text)
    text = _re.sub(r"VALUES\s*;", "VALUES ();", text)

    dump_path.write_text(text, encoding="utf-8", errors="surrogateescape")
    logger.info("stripped %d activation option row(s) to match a real .wpress export", removed)


def _read_wp_version(site_root: Path) -> str:
    import re

    text = (site_root / "wp-includes" / "version.php").read_text(encoding="utf-8", errors="replace")
    match = re.search(r"\$wp_version\s*=\s*'([^']+)'", text)
    return match.group(1) if match else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "demo-site.wpress"),
    )
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args()

    try:
        build_fixture(Path(args.output), args.keep_workspace)
    except Exception as exc:
        logger.error("fixture build failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
