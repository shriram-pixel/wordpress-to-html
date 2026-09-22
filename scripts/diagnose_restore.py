"""Why does the restored demo site render nothing?

Restores the test fixture exactly as the integration suite does, then prints
the handful of facts that separate the plausible explanations for a page that
returns HTTP 200 with an empty body:

* did ``wp-content`` arrive, and does it hold the theme and plugin?
* what do the database's ``stylesheet``, ``template`` and ``active_plugins``
  options say -- the three All-in-One WP Migration omits, and which the fixture
  omits too, on purpose?
* what did PHP actually write to its error log?

Run it anywhere the integration suite runs:

    .venv/bin/python scripts/diagnose_restore.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")


def show(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 58 - len(title)))


def listing(directory: Path, limit: int = 12) -> None:
    if not directory.is_dir():
        print(f"    MISSING: {directory}")
        return
    entries = sorted(p.name for p in directory.iterdir())
    print(f"    {directory} ({len(entries)} entries)")
    for name in entries[:limit]:
        print(f"      {name}")
    if len(entries) > limit:
        print(f"      ... and {len(entries) - limit} more")


def main() -> int:
    import httpx

    from tests.harness import restore_demo_site

    site = restore_demo_site(quiet=False)
    try:
        wp_root = site.workspace / "wordpress"

        show("what the archive looked like")
        print(f"    has_core   : {site.layout.has_core}")
        print(f"    wp_content : {site.layout.wp_content}")
        print(f"    stylesheet recorded in package.json: {site.layout.stylesheet!r}")
        print(f"    template   recorded in package.json: {site.layout.template!r}")

        show("what landed in the install")
        listing(wp_root / "wp-content")
        listing(wp_root / "wp-content" / "themes")
        listing(wp_root / "wp-content" / "plugins")
        listing(wp_root / "wp-content" / "mu-plugins")

        show("what the database says (the three AI1WM omits)")
        with site.mysql.connect(site.database) as conn:
            with conn.cursor() as cur:
                for option in ("stylesheet", "template", "active_plugins",
                               "home", "siteurl", "permalink_structure"):
                    cur.execute(
                        f"SELECT option_value FROM `{site.table_prefix}options` "
                        "WHERE option_name = %s", (option,)
                    )
                    row = cur.fetchone()
                    value = row[0] if row else "(absent)"
                    print(f"    {option:<20} {str(value)[:110]}")

        show("what the front page returns")
        response = httpx.get(site.base_url + "/", timeout=60)
        body = response.text
        print(f"    HTTP {response.status_code}, {len(body)} byte(s)")
        print(f"    content-type: {response.headers.get('content-type')}")
        if body:
            print("    first 400 characters:")
            print("      " + body[:400].replace("\n", "\n      "))

        show("what PHP logged")
        for log in sorted(site.workspace.glob("php-server-*.log")):
            text = log.read_text(errors="replace").strip()
            print(f"    {log.name}: {len(text)} byte(s)")
            if text:
                print("      " + text[-1500:].replace("\n", "\n      "))

        show("what MariaDB logged")
        error_log = site.workspace / "mysql-error.log"
        if error_log.is_file():
            tail = error_log.read_text(errors="replace")[-800:]
            print("      " + tail.replace("\n", "\n      "))

        return 0
    finally:
        site.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
