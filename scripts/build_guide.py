"""Render docs/user-guide.html to the PDF that ships with the tool.

The PDF used to have no source in the repository, so updating it meant
recreating it from scratch and hoping it matched. Keeping the HTML and
building from it means the guide can be edited, reviewed in a diff, and
regenerated in a second:

    .venv/bin/python scripts/build_guide.py

Chromium does the printing -- the same browser the conversion pipeline
already depends on, so this adds nothing to install.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "user-guide.html"
OUTPUT = ROOT / "docs" / "WordPress-to-HTML-User-Guide.pdf"


async def render(source: Path, output: Path) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        await page.goto(source.as_uri(), wait_until="load")
        await page.pdf(
            path=str(output),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=(
                '<div style="width:100%;font:8pt \'Segoe UI\',sans-serif;color:#5a6879;'
                'padding:0 16mm;display:flex;justify-content:space-between">'
                "<span>WordPress to Static HTML — command guide</span>"
                '<span class="pageNumber"></span></div>'
            ),
            margin={"top": "14mm", "bottom": "16mm", "left": "0", "right": "0"},
        )
        await browser.close()


def main() -> int:
    if not SOURCE.is_file():
        print(f"error: {SOURCE} is missing", file=sys.stderr)
        return 2

    # The previous PDF is kept until the new one is written, so a failed render
    # never leaves the repository without a guide.
    backup = None
    if OUTPUT.is_file():
        backup = OUTPUT.with_suffix(".pdf.previous")
        shutil.copy2(OUTPUT, backup)

    try:
        asyncio.run(render(SOURCE, OUTPUT))
    except Exception as exc:
        if backup and backup.is_file():
            shutil.copy2(backup, OUTPUT)
        print(f"error: could not render the guide: {exc}", file=sys.stderr)
        return 1

    size = OUTPUT.stat().st_size
    print(f"  wrote {OUTPUT.relative_to(ROOT)}  ({size / 1024:.0f} KB)")
    if backup:
        print(f"  previous version kept at {backup.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
