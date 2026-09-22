"""Repair what validation finds, and decide what is still wrong afterwards.

Validation used to be a report only: the browser check could see a carousel's
script 404 on every product page and the job still finished as a clean
success. This module closes that loop.

* :func:`repair_from_install` copies a file the export is missing but the
  restored install has -- the usual cause is an asset only JavaScript asks for.
* :func:`scan_leftovers` finds references that must never survive into a
  static export: the live domain, the temporary render server, and page links
  that end in a bare folder.
* :func:`classify` splits what remains into *problems* (the conversion got
  something wrong) and *source issues* (the original site has the same fault:
  a link to a page that does not exist, an image missing from the backup).

A job with problems still finishes -- the export is usually still useful --
but it says so, instead of reporting success.
"""

from __future__ import annotations

import logging
import posixpath
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

_NEVER_COPY = (".php", ".phtml", ".ini", ".log", ".sql", ".htaccess")


def output_target(source_file: str, reference: str) -> str | None:
    """The output-relative path a reference inside *source_file* points at."""
    path = unquote(urlsplit(reference).path or "")
    if not path:
        return None
    if path.startswith("/"):
        target = path.lstrip("/")
    else:
        target = posixpath.normpath(posixpath.join(posixpath.dirname(source_file), path))
    if target.startswith("..") or target in {".", ""}:
        return None
    return target


def repair_from_install(output_dir: Path, install_root: Path, targets) -> list[str]:
    """Copy each missing *target* from the restored install. Returns what was copied.

    Plain files only, and only from inside the install: nothing PHP, nothing a
    ``../`` could reach. Existing output files are never overwritten.
    """
    output_dir, install_root = Path(output_dir), Path(install_root).resolve()
    copied: list[str] = []
    for target in sorted({t for t in targets if t}):
        if target.lower().endswith(_NEVER_COPY) or posixpath.basename(target).lower() == "wp-config.php":
            continue
        destination = output_dir / target
        if destination.exists():
            continue
        source = (install_root / target).resolve()
        try:
            source.relative_to(install_root)
        except ValueError:
            continue
        if not source.is_file():
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        except OSError as exc:
            logger.warning("could not repair %s: %s", target, exc)
            continue
        copied.append(target)
    return copied


def in_install(install_root: Path, target: str) -> bool:
    candidate = (Path(install_root) / target).resolve()
    try:
        candidate.relative_to(Path(install_root).resolve())
    except ValueError:
        return False
    return candidate.is_file()


# ---------------------------------------------------------------------------
# Leftover references
# ---------------------------------------------------------------------------
_ATTRIBUTE = re.compile(r"""\b(?:href|src|action|poster|data-src|data-href)\s*=\s*["']([^"']+)["']""", re.I)
_CSS_URL = re.compile(r"""url\(\s*["']?([^"')]+)""", re.I)
_TEMP_SERVER = re.compile(r"(?:https?:)?(?:\\?/\\?/)(?:127\.0\.0\.1|localhost):\d{2,5}", re.I)


@dataclass
class Leftovers:
    live_domain: list[dict] = field(default_factory=list)
    temp_server: list[dict] = field(default_factory=list)
    folder_links: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.live_domain) + len(self.temp_server) + len(self.folder_links)


def scan_leftovers(
    output_dir: Path,
    site_hosts: set[str],
    *,
    max_examples: int = 200,
    flag_folder_links: bool = True,
) -> Leftovers:
    """Find references that tie the export to a server that will not exist.

    Only link-carrying attributes and CSS ``url()`` values are checked, so the
    site's own domain appearing in body text or a contact address is fine.
    """
    output_dir = Path(output_dir)
    hosts = {h.lower() for h in site_hosts if h and h not in {"127.0.0.1", "localhost"}}
    found = Leftovers()

    def note(bucket: list, relative: str, value: str) -> None:
        if len(bucket) < max_examples:
            bucket.append({"file": relative, "reference": value[:300]})

    for path in output_dir.rglob("*"):
        suffix = path.suffix.lower()
        # .xml and .txt are here because sitemaps, feeds and robots.txt are
        # copied rather than rendered, and took a different route through the
        # rewriting. A Yoast sitemap shipped the render server's address and
        # nothing reported it, because the scan only looked at pages.
        if suffix not in {".html", ".htm", ".css", ".js", ".xml", ".txt"} or not path.is_file():
            continue
        relative = path.relative_to(output_dir).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if _TEMP_SERVER.search(text):
            match = _TEMP_SERVER.search(text)
            note(found.temp_server, relative, text[max(0, match.start() - 40):match.end() + 40])

        if suffix in {".html", ".htm"}:
            values = _ATTRIBUTE.findall(text)
        elif suffix == ".css":
            values = _CSS_URL.findall(text)
        else:
            values = []
        for value in values:
            value = value.strip()
            parts = urlsplit(value if not value.startswith("//") else "http:" + value)
            host = (parts.hostname or "").lower()
            if host and host in hosts:
                note(found.live_domain, relative, value)
                continue
            if (
                flag_folder_links
                and suffix in {".html", ".htm"}
                and not host and not parts.scheme
                and value.endswith("/") and not value.startswith(("#", "mailto:", "tel:"))
                and _is_page_link_attribute(text, value)
            ):
                note(found.folder_links, relative, value)
    return found


def _is_page_link_attribute(text: str, value: str) -> bool:
    """Whether *value* appears as an href (a page link), not e.g. a src."""
    return re.search(r"""\bhref\s*=\s*["']""" + re.escape(value) + r"""["']""", text) is not None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
@dataclass
class Assessment:
    problems: list[dict] = field(default_factory=list)
    source_issues: list[dict] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)

    def add_problem(self, kind: str, message: str, examples=()) -> None:
        self.problems.append({"kind": kind, "message": message, "examples": list(examples)[:20]})

    def add_source_issue(self, kind: str, message: str, examples=()) -> None:
        self.source_issues.append({"kind": kind, "message": message, "examples": list(examples)[:20]})

    def as_dict(self) -> dict:
        return {
            "problems": self.problems,
            "source_issues": self.source_issues,
            "repaired": self.repaired[:200],
            "repaired_count": len(self.repaired),
        }


# ---------------------------------------------------------------------------
# Choosing representative pages
# ---------------------------------------------------------------------------
_BODY_CLASS = re.compile(r"""<body\b[^>]*\bclass\s*=\s*["']([^"']*)["']""", re.I)
_WIDGET = re.compile(r"""\belementor-widget-([a-z][a-z0-9-]*)""", re.I)
_BLOCK = re.compile(r"""\bwp-block-([a-z][a-z0-9-]*)""", re.I)


def template_signature(html: str) -> str:
    """A fingerprint of a page's layout, independent of its content.

    WordPress body classes name the template (``single-post``, ``archive``,
    ``page-template-full-width``) and builders mark each widget type they
    render. Tokens containing digits are dropped: they identify the page
    (``page-id-42``), not its layout. Two product pages built from the same
    template share a signature; the contact page, with its form, does not.
    """
    body = _BODY_CLASS.search(html)
    classes = sorted({
        token for token in (body.group(1).split() if body else [])
        if not any(ch.isdigit() for ch in token)
    })
    parts = sorted({m.lower() for m in _WIDGET.findall(html)} | {m.lower() for m in _BLOCK.findall(html)})
    return " ".join(classes) + " | " + " ".join(parts)


def pick_representative(pages: dict[str, str], limit: int, per_template: int = 2) -> list[str]:
    """Choose up to *limit* pages covering as many templates as possible.

    *pages* maps a page key to its signature. The home page comes first, then
    one page of every template (commonest templates first), then a second of
    each, and so on -- never more than *per_template* of the same layout.
    """
    groups: dict[str, list[str]] = {}
    for key in sorted(pages):
        groups.setdefault(pages[key], []).append(key)
    ordered = sorted(groups.values(), key=len, reverse=True)

    chosen: list[str] = [k for k in pages if k in {"index.html", "/"}][:1]
    for round_ in range(per_template):
        for members in ordered:
            candidates = [m for m in members if m not in chosen]
            if len([m for m in members if m in chosen]) <= round_ and candidates:
                chosen.append(candidates[0])
            if len(chosen) >= limit:
                return chosen[:limit]
    return chosen[:limit]


# ---------------------------------------------------------------------------
# Structural comparison
# ---------------------------------------------------------------------------
#: Elements worth counting: content a reader would miss. Scripts, styles and
#: meta links are left out because the export deliberately changes them.
_COUNTED_TAGS = ("img", "a", "h1", "h2", "h3", "li", "table", "form", "iframe", "video")


def structural_signature(html: str) -> dict:
    """Count the content elements of a page, plus the length of its text.

    A screenshot diff can miss a section that moved below the fold, and a link
    check only proves that files exist. Counting what a reader would actually
    see catches a whole block that failed to survive the rewrite.
    """
    from bs4 import BeautifulSoup

    from app.services.html_processor import HTML_PARSER

    soup = BeautifulSoup(html or "", HTML_PARSER)
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()

    signature = {name: len(soup.find_all(name)) for name in _COUNTED_TAGS}
    signature["text"] = len(" ".join((soup.get_text(" ") or "").split()))
    return signature


def compare_structure(captured: dict, exported: dict, *, tolerance: float = 0.05) -> list[str]:
    """Differences that mean the export lost content. Returns readable lines.

    Only losses are reported: an export with *more* of something is usually
    the page's own scripts having added markup during the capture.
    """
    problems: list[str] = []
    for name, before in captured.items():
        after = exported.get(name, 0)
        if after >= before:
            continue
        missing = before - after
        # A page with two images that ships one has lost half its content; a
        # page with fifty that ships forty-nine has lost a lazy-loaded one.
        allowed = int(before * (0.02 if name == "text" else tolerance))
        if missing > allowed:
            label = "characters of text" if name == "text" else f"<{name}> element(s)"
            problems.append(f"{missing:,} fewer {label} ({before:,} captured, {after:,} exported)")
    return problems
