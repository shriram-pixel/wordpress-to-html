"""Produce ``conversion-report.html``: what was converted, and what will not work.

The report is deliberately candid. A static export of a WordPress site always
loses something -- forms need a backend, search needs a database, comments need
PHP -- and a report that hid that would leave the user to discover it after
deploying. Limitations are given their own prominent section rather than a
footnote.

The report is a single self-contained HTML file with no external assets, so it
survives being emailed or opened from disk.
"""

from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.utils.filesystem import atomic_write_text, human_bytes

logger = logging.getLogger(__name__)


@dataclass
class ReportData:
    """Everything the report renders. Populated by the pipeline as it runs."""

    job_id: str = ""
    input_filename: str = ""
    input_bytes: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    status: str = "COMPLETED"

    # source site
    original_url: str = ""
    site_name: str = ""
    wordpress_version: str = ""
    theme: dict = field(default_factory=dict)
    builders: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    table_prefix: str = ""
    php_version: str = ""
    database_version: str = ""

    # extraction
    extracted_files: int = 0
    extracted_bytes: int = 0
    extraction_warnings: list[str] = field(default_factory=list)

    # content
    counts: dict[str, int] = field(default_factory=dict)
    urls_discovered: int = 0
    urls_rendered: int = 0
    urls_failed: int = 0
    failed_urls: list[dict] = field(default_factory=list)

    # output
    html_files: int = 0
    assets_by_kind: dict[str, int] = field(default_factory=dict)
    asset_bytes: int = 0
    asset_failures: list[tuple[str, str]] = field(default_factory=list)
    external_preserved: int = 0

    # validation
    validation: dict = field(default_factory=dict)
    broken_links: list[dict] = field(default_factory=list)
    missing_assets: list[dict] = field(default_factory=list)
    console_errors: list[dict] = field(default_factory=list)
    visual: dict = field(default_factory=dict)
    stage_timings: dict = field(default_factory=dict)
    """Seconds spent in each stage of the job."""
    render_timings: dict = field(default_factory=dict)
    """Average seconds per page in each render phase."""
    quality: dict = field(default_factory=dict)
    """Problems, source issues and self-repairs; see app.services.quality."""

    # honesty
    dynamic_features: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # packaging
    zip_name: str = ""
    zip_bytes: int = 0
    zip_files: int = 0

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.finished_at or time.time()) - self.started_at)

    def to_json(self) -> str:
        from dataclasses import asdict

        return json.dumps(asdict(self), indent=2, default=str)


def _e(value: Any) -> str:
    """HTML-escape any value for safe interpolation."""
    return html.escape(str(value if value is not None else ""), quote=True)


def _page_label(url: str) -> str:
    """A page's site-relative path, for naming examples in the report.

    The URLs recorded during a job point at the temporary render server, whose
    host and random port mean nothing to the reader; only the path does.
    """
    from urllib.parse import urlsplit

    path = urlsplit(url).path or "/"
    return path if path != "/" else "/ (home)"


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


_STYLES = """
:root {
  --bg: #fbfaf8; --panel: #ffffff; --ink: #1a1d21; --muted: #5f6672;
  --line: #e3e1dc; --accent: #2f6f4f; --warn: #a2620d; --bad: #a32b2b;
  --good: #2f6f4f; --chip: #f1efea;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14161a; --panel: #1b1e24; --ink: #e8eaed; --muted: #a0a7b4;
    --line: #2b2f37; --accent: #6fbf90; --warn: #e0a33f; --bad: #e8736e;
    --good: #6fbf90; --chip: #232830;
  }
}
:root[data-theme="dark"] {
  --bg: #14161a; --panel: #1b1e24; --ink: #e8eaed; --muted: #a0a7b4;
  --line: #2b2f37; --accent: #6fbf90; --warn: #e0a33f; --bad: #e8736e;
  --good: #6fbf90; --chip: #232830;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 1040px; margin: 0 auto; padding: 32px 16px 80px; }
header.top { border-bottom: 2px solid var(--line); padding-bottom: 20px; margin-bottom: 28px; }
h1 { font-size: 1.7rem; margin: 0 0 6px; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 0.95rem; }
h2 {
  font-size: 1.12rem; margin: 34px 0 12px; padding-bottom: 6px;
  border-bottom: 1px solid var(--line);
}
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 16px;
}
.card .n { font-size: 1.6rem; font-weight: 650; letter-spacing: -0.02em; }
.card .l { color: var(--muted); font-size: 0.82rem; text-transform: uppercase; letter-spacing: .04em; }
.card.good .n { color: var(--good); }
.card.warn .n { color: var(--warn); }
.card.bad  .n { color: var(--bad); }
table { width: 100%; border-collapse: collapse; background: var(--panel);
        border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line);
         font-size: 0.9rem; vertical-align: top; }
th { background: var(--chip); font-weight: 600; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
code, .mono { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: 0.86em;
              word-break: break-all; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { background: var(--chip); border: 1px solid var(--line); border-radius: 999px;
        padding: 3px 11px; font-size: 0.82rem; }
.note { background: var(--panel); border-left: 3px solid var(--accent);
        padding: 12px 16px; border-radius: 0 8px 8px 0; margin: 10px 0; }
.note.warn { border-left-color: var(--warn); }
.note.bad  { border-left-color: var(--bad); }
.note p:first-child { margin-top: 0; } .note p:last-child { margin-bottom: 0; }
.limitation { border: 1px solid var(--line); border-radius: 10px; padding: 12px 16px;
              margin-bottom: 10px; background: var(--panel); }
.limitation .name { font-weight: 640; }
.limitation .what { color: var(--muted); font-size: 0.9rem; margin-top: 3px; }
.ok { color: var(--good); font-weight: 600; }
.empty { color: var(--muted); font-style: italic; }
footer { margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--line);
         color: var(--muted); font-size: 0.85rem; }
details { margin: 8px 0; }
summary { cursor: pointer; color: var(--muted); font-size: 0.9rem; }
@media (max-width: 640px) { .wrap { padding: 20px 16px 60px; } h1 { font-size: 1.4rem; } }
"""


def _card(number: Any, label: str, tone: str = "") -> str:
    classes = f"card {tone}".strip()
    return f'<div class="{classes}"><div class="n">{_e(number)}</div><div class="l">{_e(label)}</div></div>'


def _rows(pairs: list[tuple[str, Any]]) -> str:
    return "".join(
        f"<tr><th>{_e(k)}</th><td>{v if isinstance(v, str) and v.startswith('<') else _e(v)}</td></tr>"
        for k, v in pairs if v not in (None, "", [], {})
    )


def render_report(data: ReportData) -> str:
    """Render the full report as a single self-contained HTML document."""
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    total_assets = sum(data.assets_by_kind.values())
    broken = len(data.broken_links)
    missing = len(data.missing_assets)
    console = len(data.console_errors)

    # -- headline cards -----------------------------------------------------
    cards = [
        _card(data.html_files, "HTML pages"),
        _card(f"{data.assets_by_kind.get('image', 0):,}", "Images"),
        _card(data.assets_by_kind.get("css", 0), "Stylesheets"),
        _card(data.assets_by_kind.get("js", 0), "Scripts"),
        _card(data.assets_by_kind.get("font", 0), "Fonts"),
        _card(broken, "Broken links", "good" if broken == 0 else "bad"),
        _card(missing, "Missing assets", "good" if missing == 0 else "bad"),
        _card(console, "Console errors", "good" if console == 0 else "warn"),
    ]

    # -- source site --------------------------------------------------------
    theme_text = ""
    if data.theme:
        theme_text = f"{data.theme.get('name', 'unknown')} {data.theme.get('version', '')}".strip()

    source_rows = _rows([
        ("Input file", data.input_filename),
        ("Input size", human_bytes(data.input_bytes)),
        ("Original site URL", f'<code>{_e(data.original_url)}</code>' if data.original_url else ""),
        ("Site name", data.site_name),
        ("WordPress version", data.wordpress_version),
        ("Theme", theme_text),
        ("Table prefix", f"<code>{_e(data.table_prefix)}</code>" if data.table_prefix else ""),
        ("Rendered with", f"PHP {data.php_version}, {data.database_version}"
            if data.php_version else ""),
        ("Files extracted", f"{data.extracted_files:,} ({human_bytes(data.extracted_bytes)})"),
    ])

    builders_html = (
        '<div class="chips">'
        + "".join(f'<span class="chip">{_e(b)}</span>' for b in data.builders)
        + "</div>"
        if data.builders
        else '<p class="empty">No page builder detected; the theme renders the pages directly.</p>'
    )

    plugins_html = (
        '<div class="chips">'
        + "".join(f'<span class="chip">{_e(p)}</span>' for p in sorted(data.plugins)[:60])
        + "</div>"
        + (f'<p class="sub">and {len(data.plugins) - 60} more</p>' if len(data.plugins) > 60 else "")
        if data.plugins
        else '<p class="empty">No active plugins detected.</p>'
    )

    # -- content ------------------------------------------------------------
    content_rows = "".join(
        f"<tr><td>{_e(name.replace('_', ' ').title())}</td><td class='num'>{count:,}</td></tr>"
        for name, count in sorted(data.counts.items())
    ) or '<tr><td colspan="2" class="empty">No content counts recorded.</td></tr>'

    asset_rows = "".join(
        f"<tr><td>{_e(kind.upper())}</td><td class='num'>{count:,}</td></tr>"
        for kind, count in sorted(data.assets_by_kind.items(), key=lambda kv: -kv[1])
    ) or '<tr><td colspan="2" class="empty">No assets downloaded.</td></tr>'

    # -- limitations --------------------------------------------------------
    if data.dynamic_features:
        limitations = "".join(
            f'<div class="limitation">'
            f'<div class="name">{_e(f["name"])}</div>'
            f'<div class="what">{_e(f["limitation"])}</div>'
            f'<div class="what">Found on {f["page_count"]} page(s), for example: '
            f'{_e(", ".join(_page_label(p) for p in f["examples"][:3]))}</div>'
            f"</div>"
            for f in data.dynamic_features
        )
        limitation_intro = (
            "<p>These features were detected in the exported pages. Their appearance and "
            "markup are preserved exactly, but each one needs a server to actually "
            "function. On a static host they will render correctly and do nothing when "
            "used.</p>"
        )
    else:
        limitations = ""
        limitation_intro = (
            '<p class="ok">No server-dependent features were detected. '
            "Everything on this site should work as a static export.</p>"
        )

    # -- problems -----------------------------------------------------------
    def problem_table(items: list[dict], columns: tuple[str, ...], keys: tuple[str, ...]) -> str:
        if not items:
            return '<p class="ok">None.</p>'
        head = "".join(f"<th>{_e(c)}</th>" for c in columns)
        body = "".join(
            "<tr>" + "".join(f'<td class="mono">{_e(item.get(k, ""))}</td>' for k in keys) + "</tr>"
            for item in items[:100]
        )
        more = (
            f'<p class="sub">Showing the first 100 of {len(items)}.</p>'
            if len(items) > 100 else ""
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{more}"

    broken_html = problem_table(
        data.broken_links, ("In page", "Link", "Reason"), ("source_file", "reference", "reason")
    )
    missing_html = problem_table(
        data.missing_assets, ("In file", "Asset", "Kind"), ("source_file", "reference", "kind")
    )
    console_html = problem_table(
        data.console_errors, ("Page", "Error"), ("page", "text")
    )
    failed_html = problem_table(
        data.failed_urls, ("URL", "Error"), ("url", "error")
    )

    # -- timings ------------------------------------------------------------
    _STAGE_NAMES = {
        "EXTRACTING": "Extract the backup",
        "RESTORING": "Restore WordPress and rewrite URLs",
        "STARTING_WORDPRESS": "Start WordPress",
        "DISCOVERING_URLS": "Find pages",
        "RENDERING": "Render pages in a browser",
        "GENERATING_HTML": "Generate the HTML files",
        "DOWNLOADING_ASSETS": "Collect assets",
        "VALIDATING": "Check the export",
        "ZIPPING": "Package the ZIP",
    }
    if data.stage_timings:
        total = sum(data.stage_timings.values()) or 1
        rows = "".join(
            f"<tr><td>{_e(_STAGE_NAMES.get(name, name))}</td>"
            f"<td class='num'>{_duration(seconds)}</td>"
            f"<td class='num'>{seconds / total * 100:.0f}%</td></tr>"
            for name, seconds in sorted(
                data.stage_timings.items(), key=lambda kv: kv[1], reverse=True
            )
        )
        timings_html = (
            "<table><thead><tr><th>Step</th><th class='num'>Time</th>"
            f"<th class='num'>Share</th></tr></thead><tbody>{rows}</tbody></table>"
        )
        if data.render_timings:
            phases = ", ".join(
                f"{name} {value:.1f}s" for name, value in data.render_timings.items()
                if name != "total"
            )
            timings_html += (
                f"<p class='sub'>Average per page while rendering: {_e(phases)} "
                f"(total {data.render_timings.get('total', 0):.1f}s per page).</p>"
            )
    else:
        timings_html = '<p class="empty">No step timings were recorded.</p>'

    # -- visual -------------------------------------------------------------
    if data.visual and data.visual.get("pages_compared"):
        visual = data.visual
        worst_rows = "".join(
            f"<tr><td class='mono'>{_e(p['output_path'])}</td>"
            f"<td class='num'>{_e(p['desktop'] if p['desktop'] is not None else '-')}</td>"
            f"<td class='num'>{_e(p['mobile'] if p['mobile'] is not None else '-')}</td>"
            f"<td>{_e(p.get('note', ''))}</td></tr>"
            for p in visual.get("lowest_scoring", [])
        )
        visual_html = f"""
        <div class="grid">
          {_card(f"{visual.get('desktop_average', 0)}%", "Desktop similarity")}
          {_card(f"{visual.get('mobile_average', 0)}%", "Mobile similarity")}
          {_card(visual.get("pages_compared", 0), "Pages compared")}
        </div>
        <div class="note warn"><p>{_e(visual.get('caveat', ''))}</p></div>
        <table><thead><tr><th>Page</th><th>Desktop</th><th>Mobile</th><th>Note</th></tr></thead>
        <tbody>{worst_rows}</tbody></table>"""
    else:
        visual_html = '<p class="empty">Screenshot comparison was not run for this job.</p>'

    # -- warnings -----------------------------------------------------------
    all_warnings = [*data.extraction_warnings, *data.warnings]
    warnings_html = (
        "".join(f'<div class="note warn"><p>{_e(w)}</p></div>' for w in all_warnings)
        if all_warnings else '<p class="ok">No warnings.</p>'
    )
    notes_html = (
        "".join(f'<div class="note"><p>{_e(n)}</p></div>' for n in data.notes)
        if data.notes else ""
    )

    status_tone = {"COMPLETED": "good", "FAILED": "bad"}.get(data.status, "warn")

    # -- quality verdict ----------------------------------------------------
    quality = data.quality or {}
    problems = quality.get("problems", [])
    source_issues = quality.get("source_issues", [])
    status_label = data.status
    if data.status == "COMPLETED" and problems:
        status_label, status_tone = f"COMPLETED WITH {len(problems)} PROBLEM(S)", "warn"

    def issue_list(items: list[dict], tone: str) -> str:
        blocks = []
        for item in items:
            examples = "".join(f'<li class="mono">{_e(x)}</li>' for x in item.get("examples", []))
            blocks.append(
                f'<div class="note {tone}"><p>{_e(item.get("message", ""))}</p>'
                + (f"<details><summary>Examples</summary><ul>{examples}</ul></details>" if examples else "")
                + "</div>"
            )
        return "".join(blocks)

    if quality:
        quality_html = (
            (issue_list(problems, "warn") if problems
             else '<p class="ok">No conversion problems found.</p>')
            + ('<h3>Issues in the source site</h3><p class="sub">These are broken on the original '
               f'WordPress site too; fix them there.</p>{issue_list(source_issues, "")}'
               if source_issues else "")
            + (f'<p class="sub">{quality.get("repaired_count", 0)} missing file(s) were found in the '
               "backup and copied into the export automatically.</p>"
               if quality.get("repaired_count") else "")
        )
    else:
        quality_html = '<p class="empty">Validation was not run for this job.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Conversion report - {_e(data.site_name or data.input_filename)}</title>
<style>{_STYLES}</style>
</head>
<body>
<div class="wrap">

<header class="top">
  <h1>WordPress to static HTML conversion</h1>
  <p class="sub">
    {_e(data.site_name or data.input_filename)} &middot;
    <span class="{status_tone}">{_e(status_label)}</span> &middot;
    took {_e(_duration(data.duration_seconds))} &middot;
    generated {_e(generated)}
  </p>
</header>

<div class="grid">{"".join(cards)}</div>

<h2>Quality check</h2>
{quality_html}

<h2>Static HTML limitations</h2>
{limitation_intro}
{limitations}

<h2>Source site</h2>
<table>{source_rows}</table>

<h2>Page builders</h2>
{builders_html}

<h2>Active plugins</h2>
{plugins_html}

<h2>Content exported</h2>
<div class="grid">
  {_card(data.urls_discovered, "URLs discovered")}
  {_card(data.urls_rendered, "Pages rendered", "good")}
  {_card(data.urls_failed, "Pages failed", "good" if data.urls_failed == 0 else "bad")}
  {_card(f"{total_assets:,}", "Assets localised")}
</div>
<table><thead><tr><th>Content type</th><th class="num">Count</th></tr></thead>
<tbody>{content_rows}</tbody></table>

<h2>Assets</h2>
<table><thead><tr><th>Kind</th><th class="num">Count</th></tr></thead>
<tbody>{asset_rows}</tbody></table>
<p class="sub">
  {human_bytes(data.asset_bytes)} downloaded.
  {data.external_preserved:,} external reference(s) were left pointing at their
  original host rather than mirrored.
</p>

<h2>Link and asset validation</h2>
<h3>Broken links</h3>
{broken_html}
<h3>Missing assets</h3>
{missing_html}

<h2>Browser validation</h2>
<h3>Console errors</h3>
{console_html}

<h2>Where the time went</h2>
{timings_html}

<h2>Visual comparison</h2>
{visual_html}

<h2>Pages that failed to render</h2>
{failed_html}

<h2>Warnings</h2>
{warnings_html}
{notes_html}

<h2>Package</h2>
<table>{_rows([
    ("File", data.zip_name),
    ("Size", human_bytes(data.zip_bytes)),
    ("Files", f"{data.zip_files:,}"),
    ("Job ID", data.job_id),
])}</table>
<div class="note">
  <p><strong>Deploying.</strong> Unzip the archive into your web root
  (<code>public_html</code>, <code>/var/www/html</code>) or drag it into
  Netlify, Cloudflare Pages, Vercel or GitHub Pages. No PHP, no database and no
  server configuration are required.</p>
</div>

<footer>
  Generated by wp-static-converter &middot; job {_e(data.job_id)} &middot;
  The exported site is a snapshot; re-run the conversion after changing content.
</footer>

</div>
</body>
</html>"""


def write_report(data: ReportData, report_dir: Path) -> tuple[Path, Path]:
    """Write ``conversion-report.html`` and its JSON companion."""
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    html_path = report_dir / "conversion-report.html"
    json_path = report_dir / "conversion-report.json"

    atomic_write_text(html_path, render_report(data))
    atomic_write_text(json_path, data.to_json())

    logger.info("wrote the conversion report to %s", html_path)
    return html_path, json_path


# ---------------------------------------------------------------------------
# Builder detection
# ---------------------------------------------------------------------------
#: Markers that identify a page builder from the rendered HTML. Detection is
#: for the report only: no builder is ever converted by hand.
_BUILDER_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Elementor", ("elementor-page", "elementor-widget", "/elementor/assets/")),
    ("WPBakery Page Builder", ("vc_row", "js_composer", "wpb_wrapper")),
    ("Divi", ("et_pb_section", "et_pb_row", "/themes/Divi/")),
    ("Beaver Builder", ("fl-builder-content", "fl-row", "/bb-plugin/")),
    ("Bricks", ("brxe-", "/bricks/assets/")),
    ("Oxygen", ("oxy-header", "ct-section", "/oxygen/component-framework/")),
    ("Gutenberg (block editor)", ("wp-block-", "is-layout-flow", "wp-container-")),
    ("Site Editor (block theme)", ("wp-site-blocks", "wp-elements-")),
    ("Visual Composer", ("vce-row", "/visualcomposer/")),
    ("Brizy", ("brz-root", "/brizy/")),
    ("Themify Builder", ("themify_builder_content", "/themify-builder/")),
    ("SiteOrigin Page Builder", ("siteorigin-panels", "panel-grid")),
    ("Avada Fusion Builder", ("fusion-builder-row", "fusion-fullwidth")),
    ("WooCommerce", ("woocommerce-page", "wc-block-")),
)


def detect_builders(html_samples: list[str]) -> list[str]:
    """Identify page builders from a sample of rendered pages."""
    found: list[str] = []
    combined = "\n".join(html_samples)
    for name, markers in _BUILDER_MARKERS:
        if any(marker in combined for marker in markers):
            found.append(name)

    # The block editor's classes appear on nearly every modern site; only report
    # it when no dedicated builder is present, otherwise it adds noise.
    if len(found) > 1:
        dedicated = [f for f in found if f not in
                     {"Gutenberg (block editor)", "Site Editor (block theme)"}]
        if dedicated:
            found = dedicated + [
                f for f in found
                if f in {"Site Editor (block theme)", "WooCommerce"} and f not in dedicated
            ]
    return found
