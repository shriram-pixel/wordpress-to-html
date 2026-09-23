"""Turn an extracted ``.wpress`` tree into a running-ready WordPress install.

What an All-in-One WP Migration archive actually contains
---------------------------------------------------------
This is the detail that shapes the whole module. A ``.wpress`` backup is **not**
a full WordPress installation. By default it holds:

* ``database.sql``   -- a mysqldump-style dump of every table
* ``package.json``   -- site metadata: the original site URL, the WordPress
                        version, the plugin list, the export options
* ``multisite.json`` -- present only for multisite exports
* ``wp-content/``    -- themes, plugins, uploads, mu-plugins, languages

It does **not** contain WordPress core: there is no ``wp-admin``, no
``wp-includes`` and no ``index.php``. So restoring means fetching a matching
WordPress core, laying the archive's ``wp-content`` over it, generating a fresh
``wp-config.php`` pointed at the job's private database, and importing the dump.

Archives made with "export everything" *do* sometimes include core files, so
both shapes are detected and handled.

URL rewriting
-------------
The restored site must answer on ``http://127.0.0.1:<port>`` instead of its
original domain. Doing that with a textual search-and-replace over the SQL dump
is the classic way to destroy a WordPress database, because PHP-serialized
values encode byte lengths (see :mod:`app.utils.phpserialize`). Instead the dump
is imported unchanged and the replacement is then applied row by row, parsing
and re-serializing any value that needs it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from app.utils.filesystem import atomic_write_text
from app.utils.phpserialize import PhpSerializationError, dumps, loads, replace_in_serialized
from app.utils.security import is_within

logger = logging.getLogger(__name__)

WORDPRESS_DOWNLOAD = "https://wordpress.org/wordpress-{version}.zip"
WORDPRESS_LATEST = "https://wordpress.org/latest.zip"
_USER_AGENT = "wp-static-converter/1.0 (+local tool)"

ProgressCallback = Callable[[str, float], None]


class RestoreError(RuntimeError):
    """The archive could not be turned into a usable WordPress install."""


# ---------------------------------------------------------------------------
# Layout detection
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ArchiveLayout:
    """What was found inside an extracted ``.wpress`` tree."""

    root: Path
    sql_dump: Path | None = None
    package_json: Path | None = None
    multisite_json: Path | None = None
    wp_content: Path | None = None
    has_core: bool = False
    """True when the archive also carries wp-admin/wp-includes."""

    site_url: str | None = None
    home_url: str | None = None
    wordpress_version: str | None = None
    site_name: str | None = None
    stylesheet: str | None = None
    """The active theme's folder, as package.json records it."""
    template: str | None = None
    """Its parent theme's folder, for a child theme."""
    plugins: list[str] = field(default_factory=list)
    """The plugins that were active on the source site."""
    is_multisite: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def uploads(self) -> Path | None:
        if self.wp_content is None:
            return None
        candidate = self.wp_content / "uploads"
        return candidate if candidate.is_dir() else None


#: What a real ``wp-content`` holds. Used to score candidates, because the
#: name alone proves nothing: caching plugins mirror the whole path inside
#: their own folders, so a backup can contain
#: ``cache/min/1/wp-content/themes/...`` holding a handful of minified files.
#: Restoring that instead of the site leaves an install with no theme, which
#: still renders -- with WordPress's default theme, looking nothing like the
#: original.
_CONTENT_MARKERS = ("themes", "plugins", "uploads", "mu-plugins")


def _content_score(path: Path) -> int:
    """How much of a real wp-content this directory looks like."""
    if not path.is_dir():
        return 0
    return sum(1 for name in _CONTENT_MARKERS if (path / name).is_dir())


def _find_wp_content(root: Path) -> Path | None:
    """The directory holding the site's themes, plugins and uploads.

    Some exports wrap them in ``wp-content``; others put them at the top
    level. Whichever it is, the one that *contains the site* wins over one
    that merely carries the right name.
    """
    candidates: list[tuple[int, int, Path]] = []
    for depth in range(0, 4):
        pattern = "wp-content" if depth == 0 else "/".join(["*"] * depth) + "/wp-content"
        for candidate in root.glob(pattern):
            if candidate.is_dir():
                candidates.append((_content_score(candidate), -depth, candidate))

    best = max(candidates) if candidates else None
    root_score = _content_score(root)   # an archive with no wrapper at all

    if best and best[0] >= max(root_score, 1):
        return best[2]
    if root_score:
        return root
    if best:
        return best[2]

    # Nothing named wp-content and nothing at the top level: look a little
    # deeper for the markers themselves.
    for depth in range(1, 4):
        for marker in _CONTENT_MARKERS:
            for found in root.glob("/".join(["*"] * depth) + f"/{marker}"):
                if found.is_dir():
                    return found.parent
    return None


def inspect_archive(extracted_root: Path) -> ArchiveLayout:
    """Work out the shape of an extracted archive and read its metadata."""
    root = Path(extracted_root)
    layout = ArchiveLayout(root=root)

    # The interesting files are at the top level, but a few archives nest one
    # directory deep, so search shallowly rather than assuming.
    def shallow_find(name: str, max_depth: int = 3) -> Path | None:
        direct = root / name
        if direct.exists():
            return direct
        for depth in range(1, max_depth + 1):
            for candidate in root.glob("/".join(["*"] * depth) + f"/{name}"):
                return candidate
        return None

    layout.sql_dump = shallow_find("database.sql")
    if layout.sql_dump is None:
        # Some versions name it differently or gzip it.
        for pattern in ("*.sql", "database.sql.gz", "*.sql.gz"):
            matches = sorted(root.rglob(pattern), key=lambda p: -p.stat().st_size)
            if matches:
                layout.sql_dump = matches[0]
                break

    layout.package_json = shallow_find("package.json")
    layout.multisite_json = shallow_find("multisite.json")

    layout.wp_content = _find_wp_content(root)

    # When the archive has no wp-content wrapper, wp_content *is* the extraction
    # root, so the install root to probe for core is the root itself -- not its
    # parent, which would be the job workspace.
    if layout.wp_content and layout.wp_content.name == "wp-content":
        base = layout.wp_content.parent
    else:
        base = root
    layout.has_core = (base / "wp-includes" / "version.php").is_file() or (
        base / "wp-admin"
    ).is_dir()

    _read_package_metadata(layout)

    if layout.multisite_json and layout.multisite_json.is_file():
        layout.is_multisite = True
        layout.warnings.append(
            "This is a WordPress multisite export. Only the primary site is "
            "restored and exported; sub-sites are not."
        )

    if layout.sql_dump is None:
        layout.warnings.append(
            "No database.sql was found in the archive. Without the database, "
            "WordPress cannot render any content."
        )
    if layout.wp_content is None:
        layout.warnings.append(
            "No wp-content directory was found. The site will render with a "
            "default theme and no uploads."
        )

    return layout


def _read_package_metadata(layout: ArchiveLayout) -> None:
    """Pull the original site URL and WordPress version out of package.json."""
    if not layout.package_json or not layout.package_json.is_file():
        return
    try:
        data = json.loads(layout.package_json.read_text(encoding="utf-8", errors="replace"))
    except (ValueError, OSError) as exc:
        layout.warnings.append(f"package.json could not be read: {exc}")
        return

    if not isinstance(data, dict):
        return

    layout.site_url = data.get("SiteURL") or data.get("siteurl") or None
    layout.home_url = data.get("HomeURL") or data.get("homeurl") or layout.site_url
    layout.site_name = data.get("Name") or data.get("name")

    # All-in-One WP Migration strips the theme and plugin options out of the
    # database but records them here, which makes package.json the only
    # reliable statement of what the site was actually running.
    layout.stylesheet = data.get("Stylesheet") or data.get("stylesheet") or None
    layout.template = data.get("Template") or data.get("template") or layout.stylesheet

    version = data.get("WordPress")
    if isinstance(version, dict):
        layout.wordpress_version = version.get("Version")
    elif isinstance(version, str):
        layout.wordpress_version = version
    layout.wordpress_version = layout.wordpress_version or data.get("Version")

    plugins = data.get("Plugins") or data.get("plugins")
    if isinstance(plugins, list):
        layout.plugins = [str(p) for p in plugins]
    elif isinstance(plugins, dict):
        layout.plugins = sorted(str(k) for k in plugins)


# ---------------------------------------------------------------------------
# WordPress core
# ---------------------------------------------------------------------------
def _download(url: str, destination: Path, progress: ProgressCallback | None, label: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response:
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                last_tick = 0.0
                with temporary.open("wb") as out:
                    while chunk := response.read(1 << 20):
                        out.write(chunk)
                        done += len(chunk)
                        now = time.monotonic()
                        if progress and now - last_tick > 0.3:
                            last_tick = now
                            progress(f"{label}: {done // 1048576} MB", (done / total) if total else 0.0)
            temporary.replace(destination)
            return destination
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            logger.warning("%s attempt %d/3 failed: %s", label, attempt, exc)
            time.sleep(2 * attempt)

    raise RestoreError(f"could not download {label}: {last_error}")


def provision_wordpress_core(
    version: str | None,
    cache_dir: Path,
    progress: ProgressCallback | None = None,
) -> Path:
    """Return a directory holding WordPress core, downloading it if needed.

    The archive's own WordPress version is preferred: running a site's themes
    and plugins against a much newer core can change or break the rendered
    markup, which is the one thing this tool must not do.
    """
    cache_dir = Path(cache_dir)
    normalised = None
    if version and re.fullmatch(r"\d+\.\d+(\.\d+)?", version.strip()):
        normalised = version.strip()

    label = normalised or "latest"
    target = cache_dir / "wordpress" / label
    core_root = target / "wordpress"

    if (core_root / "wp-includes" / "version.php").is_file():
        logger.info("using cached WordPress core %s", label)
        return core_root

    url = WORDPRESS_DOWNLOAD.format(version=normalised) if normalised else WORDPRESS_LATEST
    archive_path = cache_dir / "downloads" / f"wordpress-{label}.zip"

    if not archive_path.is_file():
        try:
            _download(url, archive_path, progress, f"WordPress {label}")
        except RestoreError:
            if normalised is None:
                raise
            # That exact version may have been pulled from wordpress.org.
            logger.warning("WordPress %s is unavailable; falling back to latest", normalised)
            label, target = "latest", cache_dir / "wordpress" / "latest"
            core_root = target / "wordpress"
            archive_path = cache_dir / "downloads" / "wordpress-latest.zip"
            if not archive_path.is_file():
                _download(WORDPRESS_LATEST, archive_path, progress, "WordPress latest")

    if progress:
        progress(f"Unpacking WordPress {label}", 0.9)

    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            if not is_within(target, target / member.filename):
                raise RestoreError(f"WordPress archive member escapes the cache: {member.filename}")
        archive.extractall(target)

    if not (core_root / "wp-includes" / "version.php").is_file():
        raise RestoreError(f"the WordPress download did not unpack as expected under {target}")
    return core_root


def read_core_version(core_root: Path) -> str | None:
    """Read ``$wp_version`` out of an unpacked core."""
    version_file = Path(core_root) / "wp-includes" / "version.php"
    if not version_file.is_file():
        return None
    match = re.search(
        r"\$wp_version\s*=\s*'([^']+)'", version_file.read_text(encoding="utf-8", errors="replace")
    )
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Building the install tree
# ---------------------------------------------------------------------------
def build_wordpress_tree(
    layout: ArchiveLayout,
    destination: Path,
    core_root: Path,
    progress: ProgressCallback | None = None,
    *,
    consume_archive: bool = False,
) -> Path:
    """Assemble core + the archive's ``wp-content`` into *destination*.

    With *consume_archive* the extracted files are **moved** rather than copied.
    That matters on real sites: ``wp-content`` is nearly all of a backup, so
    copying it means the job holds two full copies of the site's media at once.
    A move on the same volume is a rename -- instant, and free. The extracted
    tree is destroyed in the process, which is safe because the pipeline always
    re-extracts from the original ``.wpress`` when a job is re-run.

    WordPress core itself is always copied: it comes from a cache shared by
    every job and must survive.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    transfer = _move_tree if consume_archive else _copy_tree
    verb = "moving" if consume_archive else "copying"

    # The archive's own metadata must never be transferred into the install.
    #
    # This matters most for backups that have no wp-content wrapper -- some
    # exports put uploads/, plugins/ and themes/ straight at the top level, so
    # wp_content resolves to the extraction root. Without this guard the move
    # would carry database.sql into wp-content/ before it has been imported,
    # and the restore would fail looking for a file it had just relocated.
    metadata = {"database.sql", "package.json", "multisite.json"}

    if progress:
        progress("Preparing WordPress core", 0.1)

    if layout.has_core:
        # The archive brought its own core; use it verbatim so any core
        # modifications the site relied on are preserved.
        source_root = layout.wp_content.parent if layout.wp_content else layout.root
        logger.info("archive includes WordPress core; %s it from %s", verb, source_root)
        transfer(source_root, destination, skip_names=metadata)
    else:
        logger.info("archive has no core; laying wp-content over WordPress core")
        _copy_tree(Path(core_root), destination)

        if progress:
            progress(f"{verb.capitalize()} themes, plugins and uploads", 0.5)
        if layout.wp_content and layout.wp_content.is_dir():
            target_content = destination / "wp-content"
            # The stock core ships twentytwenty* themes and akismet; the
            # archive's wp-content is layered on top rather than replacing the
            # directory, so a site relying on a bundled theme still renders.
            transfer(layout.wp_content, target_content, skip_names=metadata)

    # A wp-config from the original host points at credentials that do not
    # exist here and often hard-codes the live domain.
    for stale in ("wp-config.php", ".htaccess", "web.config"):
        candidate = destination / stale
        if candidate.exists():
            candidate.rename(candidate.with_suffix(candidate.suffix + ".original"))

    if not (destination / "index.php").is_file():
        raise RestoreError(
            f"the assembled WordPress tree at {destination} has no index.php; "
            "the archive may be incomplete"
        )

    if progress:
        progress("WordPress files in place", 1.0)
    return destination


def _copy_tree(source: Path, destination: Path, skip_names: set[str] | None = None) -> None:
    """Copy *source* over *destination*, merging directories.

    ``shutil.copytree(dirs_exist_ok=True)`` does this, but it aborts the whole
    copy on the first unreadable file; a backup from a different OS regularly
    contains one. Errors are collected and logged instead.
    """
    skip_names = skip_names or set()
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    failures = 0

    for item in source.rglob("*"):
        if item.name in skip_names:
            continue
        try:
            relative = item.relative_to(source)
        except ValueError:
            continue
        target = destination / relative
        if not is_within(destination, target):
            logger.warning("skipping %s: it would escape the install directory", relative)
            continue
        try:
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
        except OSError as exc:
            failures += 1
            if failures <= 10:
                logger.warning("could not copy %s: %s", relative, exc)

    if failures:
        logger.warning("%d file(s) could not be copied into the WordPress tree", failures)


def repair_core_files(wordpress_root: Path, core_root: Path) -> int:
    """Copy back any WordPress core file missing from an install. Returns the count.

    Core -- everything outside ``wp-content`` -- is never part of a backup: it
    always comes from the official WordPress download cached in the runtime
    directory, so restoring a missing file from there is safe and loses
    nothing. Only files that are absent are copied; nothing is overwritten and
    nothing is deleted.

    This runs before WordPress is started because a partial core does not fail
    loudly at build time. An install found with an empty ``wp-admin`` answered
    every request with "There has been a critical error" -- ``wp-settings.php``
    requires ``wp-admin/includes/plugin.php`` on every page load -- while the
    build log showed no problem at all.
    """
    wordpress_root, core_root = Path(wordpress_root), Path(core_root)
    if not core_root.is_dir():
        return 0

    restored = 0
    for source in core_root.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(core_root)
        if relative.parts and relative.parts[0] == "wp-content":
            continue  # the site's own content is never touched
        target = wordpress_root / relative
        if target.exists():
            continue
        if not is_within(wordpress_root, target):
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            restored += 1
        except OSError as exc:
            logger.warning("could not restore core file %s: %s", relative, exc)

    if restored:
        logger.warning("restored %d missing WordPress core file(s) from the clean cache", restored)
    return restored


_ARCHIVE_METADATA = frozenset({"database.sql", "package.json", "multisite.json"})
_CORE_TOP_LEVEL = frozenset({"wp-admin", "wp-includes"})


def repair_content_from_archive(archive_path: Path, wordpress_root: Path) -> int:
    """Restore any file from the backup that is missing from the install.

    The ``.wpress`` is the source of truth for everything under ``wp-content``,
    so re-reading a missing file from it loses nothing. Only absent files are
    written: nothing is overwritten and nothing is deleted. Returns the count.

    Checks the install against the archive rather than trusting it, because an
    install can be damaged without any error at build time -- an interrupted
    cleanup once emptied most of a site's plugin directories, and WordPress
    then silently skipped Elementor on every page.
    """
    from app.services.wpress_extractor import WpressArchive
    from app.utils.security import safe_join

    wordpress_root = Path(wordpress_root)
    content_root = wordpress_root / "wp-content"
    archive = WpressArchive(archive_path)

    restored = failed = 0
    with archive.path.open("rb") as source:
        for entry in archive.iter_entries():
            try:
                relative = entry.safe_path
            except Exception:
                continue
            parts = relative.parts
            if len(parts) == 1 and parts[0] in _ARCHIVE_METADATA:
                continue

            # Archives either wrap content in wp-content/ or put it at the top.
            if parts[0] == "wp-content" or parts[0] in _CORE_TOP_LEVEL:
                base, rel = wordpress_root, relative
            else:
                base, rel = content_root, relative
            try:
                target = safe_join(base, str(rel))
            except Exception:
                continue
            if target.exists():
                continue

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                source.seek(entry.offset)
                remaining = entry.size
                with target.open("wb") as out:
                    while remaining > 0:
                        chunk = source.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        out.write(chunk)
                        remaining -= len(chunk)
                restored += 1
            except OSError as exc:
                failed += 1
                if failed <= 10:
                    logger.warning("could not restore %s: %s", relative, exc)

    if restored:
        logger.warning("restored %d file(s) missing from the install, from the backup", restored)
    return restored


def core_root_for(wordpress_root: Path, cache_dir: Path) -> Path | None:
    """The cached clean core matching an install's WordPress version."""
    version_file = Path(wordpress_root) / "wp-includes" / "version.php"
    if not version_file.is_file():
        return None
    match = re.search(
        r"\$wp_version\s*=\s*'([^']+)'",
        version_file.read_text(encoding="utf-8", errors="replace"),
    )
    if not match:
        return None
    candidate = Path(cache_dir) / "wordpress" / match.group(1) / "wordpress"
    return candidate if candidate.is_dir() else None


def _move_tree(source: Path, destination: Path, skip_names: set[str] | None = None) -> None:
    """Merge-move *source* into *destination*, falling back to copy when needed.

    Whole directories are renamed when their target does not exist, which is a
    metadata operation regardless of how much media is inside. Where the target
    does exist -- ``wp-content/themes`` already holds the core's bundled themes
    -- the merge recurses so nothing is lost.

    ``os.replace`` only works within a volume, so a cross-device layout falls
    back to copying. Correctness never depends on the fast path.
    """
    skip_names = skip_names or set()
    source, destination = Path(source), Path(destination)
    if not source.is_dir():
        return
    destination.mkdir(parents=True, exist_ok=True)

    failures = 0
    for item in sorted(source.iterdir()):
        if item.name in skip_names:
            continue
        target = destination / item.name
        if not is_within(destination, target):
            logger.warning("skipping %s: it would escape the install directory", item.name)
            continue

        try:
            if item.is_dir():
                if target.exists():
                    # skip_names applies to the archive's own top level only.
                    # Passing it down skipped every plugin's package.json too,
                    # leaving them behind in extracted/ to be deleted with it.
                    _move_tree(item, target)
                    # Remove the now-empty source directory.
                    try:
                        item.rmdir()
                    except OSError:
                        pass
                else:
                    os.replace(item, target)
            else:
                os.replace(item, target)
        except OSError:
            # Different volume, a locked file, or a name the OS rejects.
            try:
                if item.is_dir():
                    _copy_tree(item, target, skip_names)
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    shutil.copy2(item, target)
                    item.unlink(missing_ok=True)
            except OSError as exc:
                failures += 1
                if failures <= 10:
                    logger.warning("could not move %s: %s", item.name, exc)

    if failures:
        logger.warning("%d entr(ies) could not be moved into the WordPress tree", failures)


# ---------------------------------------------------------------------------
# wp-config.php and the control mu-plugin
# ---------------------------------------------------------------------------
_WP_CONFIG_TEMPLATE = """<?php
/**
 * Generated by wp-static-converter for a temporary, local-only WordPress.
 *
 * This file is never included in the exported ZIP. The database it points at
 * is a throwaway instance bound to loopback that exists only while the
 * conversion runs.
 */

define( 'DB_NAME',     '{db_name}' );
define( 'DB_USER',     '{db_user}' );
define( 'DB_PASSWORD', '{db_password}' );
define( 'DB_HOST',     '{db_host}' );
define( 'DB_CHARSET',  'utf8mb4' );
define( 'DB_COLLATE',  '' );

$table_prefix = '{table_prefix}';

/* The site answers here for the duration of the crawl. Defining these
   overrides whatever the database holds, so a stale siteurl cannot send the
   crawler back to the live domain. */
define( 'WP_HOME',    '{site_url}' );
define( 'WP_SITEURL', '{site_url}' );

/* Keep the render clean and deterministic. */
define( 'WP_DEBUG',         false );
define( 'WP_DEBUG_DISPLAY', false );
define( 'WP_DEBUG_LOG',     false );
define( 'SCRIPT_DEBUG',     false );

/* No outbound calls, no update checks, no cron during the crawl: they are slow
   and can block a page render for the full HTTP timeout. */
define( 'WP_CRON_LOCK_TIMEOUT', 1 );
define( 'DISABLE_WP_CRON', true );
define( 'AUTOMATIC_UPDATER_DISABLED', true );
define( 'WP_AUTO_UPDATE_CORE', false );
define( 'WP_INSTALLING', false );

/* Caching plugins would serve stale HTML or write to unavailable paths. */
define( 'WP_CACHE', false );

define( 'FS_METHOD', 'direct' );
define( 'WP_MEMORY_LIMIT',     '512M' );
define( 'WP_MAX_MEMORY_LIMIT', '512M' );

/* Salts are regenerated per job; nothing here outlives the conversion. */
{salts}

if ( ! defined( 'ABSPATH' ) ) {{
    define( 'ABSPATH', __DIR__ . '/' );
}}

require_once ABSPATH . 'wp-settings.php';
"""

#: Loaded by WordPress before regular plugins, so it can correct behaviour that
#: would otherwise send the crawler to the live domain or break rendering.
_CONTROL_MU_PLUGIN = r"""<?php
/**
 * Plugin Name: Static Export Controller
 * Description: Local-only adjustments that let this site be crawled faithfully.
 * Author: wp-static-converter
 *
 * Generated per job. Never included in the exported ZIP.
 *
 * Every filter here exists to stop the temporary site sending the crawler
 * somewhere it cannot follow, without altering the markup the theme produces.
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/* ---------------------------------------------------------------------------
 * 1. Pin every URL WordPress generates to the local server.
 * ------------------------------------------------------------------------ */
$wpsc_local_url = defined( 'WP_HOME' ) ? WP_HOME : 'http://127.0.0.1';

foreach ( array( 'option_siteurl', 'option_home', 'pre_option_siteurl', 'pre_option_home' ) as $wpsc_filter ) {
    add_filter( $wpsc_filter, function () use ( $wpsc_local_url ) {
        return $wpsc_local_url;
    }, PHP_INT_MAX );
}

/* ---------------------------------------------------------------------------
 * 2. Stop redirects that would bounce the crawler off the local server.
 *    Canonical redirects to the original domain are the usual culprit; so are
 *    plugins that force HTTPS or a www prefix.
 * ------------------------------------------------------------------------ */
remove_filter( 'template_redirect', 'redirect_canonical' );
add_filter( 'redirect_canonical', '__return_false', PHP_INT_MAX );

add_filter( 'wp_redirect', function ( $location ) use ( $wpsc_local_url ) {
    if ( ! $location ) {
        return $location;
    }
    $host       = wp_parse_url( $location, PHP_URL_HOST );
    $local_host = wp_parse_url( $wpsc_local_url, PHP_URL_HOST );

    // An off-site redirect during a crawl means a plugin is enforcing the live
    // domain. Rewrite it back to the local server so the page still renders.
    if ( $host && $local_host && $host !== $local_host ) {
        $path = (string) wp_parse_url( $location, PHP_URL_PATH );
        $qs   = wp_parse_url( $location, PHP_URL_QUERY );
        return $wpsc_local_url . $path . ( $qs ? '?' . $qs : '' );
    }
    return $location;
}, PHP_INT_MAX );

/* ---------------------------------------------------------------------------
 * 3. Never force SSL: the local server speaks plain HTTP.
 * ------------------------------------------------------------------------ */
add_filter( 'force_ssl_admin', '__return_false', PHP_INT_MAX );
add_filter( 'https_ssl_verify', '__return_false', PHP_INT_MAX );

/* ---------------------------------------------------------------------------
 * 4. Block outbound HTTP. Update checks, licence pings and font fetches add
 *    many seconds per page and can hang the render entirely.
 * ------------------------------------------------------------------------ */
add_filter( 'pre_http_request', function ( $preempt, $args, $url ) use ( $wpsc_local_url ) {
    $local_host = wp_parse_url( $wpsc_local_url, PHP_URL_HOST );
    $host       = wp_parse_url( $url, PHP_URL_HOST );

    if ( $host && $local_host && $host === $local_host ) {
        return $preempt;  // loopback requests are fine
    }
    return new WP_Error( 'wpsc_offline', 'Outbound HTTP disabled during static export.' );
}, 10, 3 );

/* ---------------------------------------------------------------------------
 * 5. Keep the admin bar and update nags out of the rendered markup.
 * ------------------------------------------------------------------------ */
add_filter( 'show_admin_bar', '__return_false', PHP_INT_MAX );

/* ---------------------------------------------------------------------------
 * 6. Neutralise page caches. A cache would serve HTML generated for the old
 *    domain, silently reintroducing live-site URLs into the export.
 * ------------------------------------------------------------------------ */
add_filter( 'wp_cache_enabled', '__return_false', PHP_INT_MAX );
if ( ! defined( 'DONOTCACHEPAGE' ) )   { define( 'DONOTCACHEPAGE', true ); }
if ( ! defined( 'DONOTCACHEOBJECT' ) ) { define( 'DONOTCACHEOBJECT', true ); }
if ( ! defined( 'DONOTCACHEDB' ) )     { define( 'DONOTCACHEDB', true ); }
if ( ! defined( 'DONOTMINIFY' ) )      { define( 'DONOTMINIFY', false ); }

/* ---------------------------------------------------------------------------
 * 7. Expose what the crawler needs to know about this site, as JSON on a
 *    dedicated route. This is how URL discovery reads post types, taxonomies
 *    and counts without guessing at the database schema.
 * ------------------------------------------------------------------------ */
/**
 * Post types that are page-builder machinery rather than site content.
 *
 * Elementor and its add-ons register their saved templates as *public* post
 * types, so a naive "every public post type" query picks up the default kit,
 * the header template and the footer template. Requesting one of those returns
 * the home page, so exporting them produces byte-identical duplicates of the
 * home page at meaningless URLs. The same is true of the block editor's
 * reusable blocks and template parts.
 */
function wpsc_is_builder_internal_type( $name ) {
    $exact = array(
        'elementor_library', 'e-landing-page', 'e-floating-buttons',
        'elementskit_content', 'elementskit_template', 'elementskit_widget',
        'wpr_templates', 'wpr_mega_menu',
        'fusion_template', 'fusion_element',
        'ct_template', 'oxy_user_library',
        'brizy_template',
        'jet-engine', 'jet-theme-core', 'jet-popup', 'jet-menu',
        'tve_form_type', 'tve_lead_shortcode',
        'wp_block', 'wp_template', 'wp_template_part', 'wp_navigation',
        'wp_global_styles', 'custom_css', 'customize_changeset',
        'oembed_cache', 'user_request', 'nav_menu_item', 'revision',
        'attachment',
    );
    if ( in_array( $name, $exact, true ) ) {
        return true;
    }
    foreach ( array( 'elementor_', 'elementskit_', 'wpr_', 'brizy_', 'jet-', 'vc_', 'ct_' ) as $prefix ) {
        if ( strpos( $name, $prefix ) === 0 ) {
            return true;
        }
    }
    return false;
}

add_action( 'wp_loaded', function () {
    // 'wp_loaded' rather than 'init': plugins and themes register their custom
    // post types and taxonomies during 'init', so anything reading that
    // registry has to run after the whole of 'init' has finished. Hooking
    // 'init' here would report only WordPress's built-in types.
    if ( ! isset( $_GET['wpsc_manifest'] ) ) {
        return;
    }

    $post_types = array();
    foreach ( get_post_types( array( 'public' => true ), 'objects' ) as $type ) {
        if ( wpsc_is_builder_internal_type( $type->name ) ) {
            continue;
        }
        $post_types[ $type->name ] = array(
            'label'        => $type->label,
            'has_archive'  => (bool) $type->has_archive,
            'archive_link' => $type->has_archive ? get_post_type_archive_link( $type->name ) : null,
            'count'        => (int) wp_count_posts( $type->name )->publish,
        );
    }

    $taxonomies = array();
    foreach ( get_taxonomies( array( 'public' => true ), 'objects' ) as $tax ) {
        $taxonomies[ $tax->name ] = array(
            'label'        => $tax->label,
            'hierarchical' => (bool) $tax->hierarchical,
        );
    }

    $theme = wp_get_theme();

    wp_send_json( array(
        'home'            => home_url( '/' ),
        'site'            => site_url( '/' ),
        'wp_version'      => get_bloginfo( 'version' ),
        'charset'         => get_bloginfo( 'charset' ),
        'name'            => get_bloginfo( 'name' ),
        'permalink'       => get_option( 'permalink_structure' ),
        'posts_per_page'  => (int) get_option( 'posts_per_page' ),
        'show_on_front'   => get_option( 'show_on_front' ),
        'page_on_front'   => (int) get_option( 'page_on_front' ),
        'page_for_posts'  => (int) get_option( 'page_for_posts' ),
        'theme'           => array(
            'name'     => $theme->get( 'Name' ),
            'version'  => $theme->get( 'Version' ),
            'template' => $theme->get_template(),
        ),
        'active_plugins'  => (array) get_option( 'active_plugins', array() ),
        'post_types'      => $post_types,
        'taxonomies'      => $taxonomies,
    ) );
} );

/* ---------------------------------------------------------------------------
 * 8. Enumerate real permalinks.
 *
 * Reconstructing permalinks from the database means reimplementing WordPress's
 * rewrite engine, and it gets the interesting cases wrong: a custom post type
 * registered with rewrite => array('slug' => 'projects') does not live at
 * /project/<slug>/, a custom taxonomy can have any base, and hierarchical
 * pages nest. Asking WordPress itself removes the whole class of guesses.
 *
 * Paginated, because a large site has too many URLs for one response.
 * ------------------------------------------------------------------------ */
add_action( 'wp_loaded', function () {
    if ( ! isset( $_GET['wpsc_urls'] ) ) {
        return;
    }

    $offset = max( 0, (int) ( $_GET['offset'] ?? 0 ) );
    $limit  = min( 2000, max( 1, (int) ( $_GET['limit'] ?? 500 ) ) );

    $urls = array();

    // --- content -----------------------------------------------------------
    $types = array();
    foreach ( get_post_types( array( 'public' => true ), 'names' ) as $name ) {
        if ( ! wpsc_is_builder_internal_type( $name ) ) {
            $types[] = $name;
        }
    }
    if ( empty( $types ) ) {
        $types = array( 'post', 'page' );
    }

    $query = new WP_Query( array(
        'post_type'              => $types,
        'post_status'            => 'publish',
        'has_password'           => false,
        'posts_per_page'         => $limit,
        'offset'                 => $offset,
        'orderby'                => 'ID',
        'order'                  => 'ASC',
        'ignore_sticky_posts'    => true,
        'no_found_rows'          => false,
        'update_post_meta_cache' => false,
        'update_post_term_cache' => false,
    ) );

    foreach ( $query->posts as $post ) {
        $link = get_permalink( $post );
        if ( $link ) {
            $urls[] = array(
                'loc'   => $link,
                'kind'  => $post->post_type === 'page' ? 'page'
                           : ( $post->post_type === 'post' ? 'post' : 'custom_post_type' ),
                'title' => $post->post_title,
            );
        }
    }

    $has_more = ( $offset + $limit ) < (int) $query->found_posts;

    // Archives and terms are cheap and finite, so they come with the first page.
    if ( $offset === 0 ) {
        foreach ( get_post_types( array( 'public' => true ), 'objects' ) as $type ) {
            if ( wpsc_is_builder_internal_type( $type->name ) ) {
                continue;
            }
            if ( $type->has_archive ) {
                $link = get_post_type_archive_link( $type->name );
                if ( $link ) {
                    $urls[] = array( 'loc' => $link, 'kind' => 'archive', 'title' => $type->label );
                }
            }
        }

        foreach ( get_taxonomies( array( 'public' => true ), 'names' ) as $taxonomy ) {
            if ( in_array( $taxonomy, array( 'post_format' ), true ) ) {
                continue;
            }
            $terms = get_terms( array(
                'taxonomy'   => $taxonomy,
                'hide_empty' => true,
                'number'     => 2000,
            ) );
            if ( is_wp_error( $terms ) ) {
                continue;
            }
            foreach ( $terms as $term ) {
                $link = get_term_link( $term );
                if ( ! is_wp_error( $link ) ) {
                    $kind = $taxonomy === 'category' ? 'category'
                            : ( $taxonomy === 'post_tag' ? 'tag' : 'taxonomy' );
                    $urls[] = array( 'loc' => $link, 'kind' => $kind, 'title' => $term->name );
                }
            }
        }

        foreach ( get_users( array( 'has_published_posts' => true, 'number' => 500 ) ) as $user ) {
            $link = get_author_posts_url( $user->ID );
            if ( $link ) {
                $urls[] = array( 'loc' => $link, 'kind' => 'author', 'title' => $user->display_name );
            }
        }

        // Paginated blog/archive pages, derived from the real post count so the
        // crawler does not have to probe for a 404 to find the last page.
        $per_page = max( 1, (int) get_option( 'posts_per_page' ) );
        $posts_page = get_option( 'page_for_posts' );
        $blog_base = $posts_page ? get_permalink( $posts_page ) : home_url( '/' );
        $published = (int) wp_count_posts( 'post' )->publish;
        $pages = (int) ceil( $published / $per_page );
        for ( $page = 2; $page <= min( $pages, 500 ); $page++ ) {
            $urls[] = array(
                'loc'   => trailingslashit( $blog_base ) . 'page/' . $page . '/',
                'kind'  => 'pagination',
                'title' => 'Page ' . $page,
            );
        }
    }

    wp_send_json( array(
        'urls'     => $urls,
        'offset'   => $offset,
        'limit'    => $limit,
        'total'    => (int) $query->found_posts,
        'has_more' => $has_more,
    ) );
} );
"""


def _generate_salts() -> str:
    """Fresh, random salts. They live only as long as the job does."""
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+[]{}<>?"
    keys = (
        "AUTH_KEY", "SECURE_AUTH_KEY", "LOGGED_IN_KEY", "NONCE_KEY",
        "AUTH_SALT", "SECURE_AUTH_SALT", "LOGGED_IN_SALT", "NONCE_SALT",
    )
    lines = []
    for key in keys:
        value = "".join(secrets.choice(alphabet) for _ in range(64)).replace("'", "-")
        lines.append(f"define( '{key}', '{value}' );")
    return "\n".join(lines)


def write_wp_config(
    wordpress_root: Path,
    *,
    db_name: str,
    db_user: str,
    db_password: str,
    db_host: str,
    table_prefix: str,
    site_url: str,
) -> Path:
    """Write a fresh ``wp-config.php`` for the temporary install."""
    if not re.fullmatch(r"[A-Za-z0-9_]{0,64}", table_prefix):
        raise RestoreError(f"refusing to use an unsafe table prefix: {table_prefix!r}")

    content = _WP_CONFIG_TEMPLATE.format(
        db_name=db_name,
        db_user=db_user,
        db_password=db_password.replace("\\", "\\\\").replace("'", "\\'"),
        db_host=db_host,
        table_prefix=table_prefix,
        site_url=site_url.rstrip("/"),
        salts=_generate_salts(),
    )
    path = Path(wordpress_root) / "wp-config.php"
    atomic_write_text(path, content)
    logger.info("wrote wp-config.php (prefix %r, database %r)", table_prefix, db_name)
    return path


#: Folders where plugins cache CSS they generated from the database. The URLs
#: baked into these files are not in the database, so a database rewrite never
#: reaches them.
_GENERATED_CSS_DIRS = ("wp-content/uploads/elementor/css",)


def rewrite_generated_css(wordpress_root: Path, replacements: dict[str, str]) -> tuple[int, int]:
    """Apply *replacements* to page-builder CSS cached on disk.

    Elementor writes each page's styles -- including every background image --
    to ``uploads/elementor/css/post-N.css`` with absolute URLs to the site it
    was generated on. Left alone, those backgrounds load from the live domain:
    blank when it is offline, and fetched from production when it is not.

    Files are edited in place; nothing is deleted. Returns (files changed,
    replacements made).
    """
    files_changed = replacements_made = 0
    ordered = sorted(replacements.items(), key=lambda kv: len(kv[0]), reverse=True)
    for relative in _GENERATED_CSS_DIRS:
        folder = wordpress_root / relative
        if not folder.is_dir():
            continue
        for css in folder.rglob("*.css"):
            try:
                text = css.read_text(encoding="utf-8", errors="surrogateescape")
            except OSError:
                continue
            count = 0
            for search, replace in ordered:
                hits = text.count(search)
                if hits:
                    text = text.replace(search, replace)
                    count += hits
            if count:
                try:
                    css.write_text(text, encoding="utf-8", errors="surrogateescape")
                except OSError as exc:
                    logger.warning("could not update %s: %s", css, exc)
                    continue
                files_changed += 1
                replacements_made += count
    return files_changed, replacements_made


def write_control_plugin(wordpress_root: Path) -> Path:
    """Install the mu-plugin that keeps the crawl on the local server."""
    mu_dir = Path(wordpress_root) / "wp-content" / "mu-plugins"
    mu_dir.mkdir(parents=True, exist_ok=True)
    path = mu_dir / "000-wpsc-static-export.php"
    atomic_write_text(path, _CONTROL_MU_PLUGIN)
    return path


# ---------------------------------------------------------------------------
# SQL import
# ---------------------------------------------------------------------------
def detect_table_prefix(sql_dump: Path, fallback: str = "wp_") -> str:
    """Infer the table prefix from the dump's ``CREATE TABLE`` statements.

    The prefix is whatever precedes a core table name such as ``options`` or
    ``posts``. Reading only the head of the file keeps this cheap on a dump of
    several gigabytes.
    """
    pattern = re.compile(
        rb"CREATE TABLE (?:IF NOT EXISTS )?[`\"]?([A-Za-z0-9_]+?)"
        rb"(options|posts|users|terms|postmeta|comments)[`\"]?\s*\(",
        re.IGNORECASE,
    )
    counts: dict[str, int] = {}

    try:
        with Path(sql_dump).open("rb") as fh:
            # 8 MiB is comfortably past the CREATE TABLE section of any dump.
            head = fh.read(8 * 1024 * 1024)
    except OSError as exc:
        logger.warning("could not read the dump to detect its prefix: %s", exc)
        return fallback

    for match in pattern.finditer(head):
        prefix = match.group(1).decode("ascii", errors="replace")
        counts[prefix] = counts.get(prefix, 0) + 1

    if not counts:
        # Prefix-less installs are legal, as is an unusual table set.
        if re.search(rb"CREATE TABLE (?:IF NOT EXISTS )?[`\"]?(options|posts)[`\"]?\s*\(", head, re.I):
            return ""
        logger.warning("could not detect a table prefix; assuming %r", fallback)
        return fallback

    best = max(counts.items(), key=lambda item: item[1])[0]
    logger.info("detected table prefix %r", best)
    return best


def import_sql_dump(
    sql_dump: Path,
    server,
    database: str,
    *,
    client_binary: Path | None = None,
    progress: ProgressCallback | None = None,
    workers: int = 1,
    should_stop=None,
) -> int:
    """Import *sql_dump* into *database*. Returns the number of statements run.

    The vendor ``mysql``/``mariadb`` client is used when available because it
    parses dump syntax natively and is far faster on large files. A pure-Python
    splitter is used otherwise, so the tool still works against a server whose
    client binaries are missing.
    """
    sql_dump = Path(sql_dump)
    if not sql_dump.is_file():
        raise RestoreError(f"no SQL dump at {sql_dump}")

    size = sql_dump.stat().st_size
    logger.info("importing %s (%.1f MiB) into %s", sql_dump.name, size / 1048576, database)

    if client_binary and Path(client_binary).is_file():
        client = Path(client_binary)
        # Several connections only pay off on a dump big enough to matter.
        if workers > 1 and size > 64 * 1024 * 1024:
            try:
                return _import_in_parallel(
                    sql_dump, server, database, client, workers, progress, should_stop
                )
            except (RestoreError, OSError) as exc:
                logger.warning(
                    "parallel import failed (%s); retrying through one connection", exc
                )
        try:
            return _import_with_client(sql_dump, server, database, client, progress)
        except RestoreError as exc:
            logger.warning("client import failed (%s); falling back to the Python importer", exc)

    return _import_with_python(sql_dump, server, database, progress)


#: The statement a dump line starts, and the table it touches. Dumps escape
#: newlines inside values, so a statement never spans lines by accident --
#: which is what makes routing by line safe and fast. A line that does not end
#: a statement is still buffered until it does.
_STATEMENT_TABLE = re.compile(
    rb"^\s*(?:INSERT(?:\s+IGNORE)?\s+INTO|REPLACE\s+INTO|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?"
    rb"|DROP\s+TABLE(?:\s+IF\s+EXISTS)?|ALTER\s+TABLE|LOCK\s+TABLES|TRUNCATE(?:\s+TABLE)?)"
    rb"\s+`?([^`\s(;]+)`?",
    re.IGNORECASE,
)


#: Tables whose *contents* no static export can use: request logs, firewall
#: hits, scheduled-task history, session stores. On a busy site these are
#: routinely most of the dump. Their structure is still created, so any plugin
#: that queries them during rendering finds an empty table rather than an error.
_SKIP_CONTENT_TABLES = re.compile(
    rb"(actionscheduler_(logs|actions)|wfhits|wflogs|wfblocks7|wffilemods|wfnotifications"
    rb"|wfstatus|wfissues|wfconfig|wp_statistics_(visitor|visit|pages|search)"
    rb"|litespeed_(url|img_optm|crawler)|redirection_(logs|404)|wc_sessions|woocommerce_sessions"
    rb"|wp_session|popularpostsdata|popularpostssummary|aiowps_events|aiowps_failed_logins"
    rb"|wpforms_tasks_meta|simple_history|edd_sessions)",
    re.IGNORECASE,
)


def _skips_content(table: bytes | None) -> bool:
    return bool(table and _SKIP_CONTENT_TABLES.search(table))


def _table_of(line: bytes) -> bytes | None:
    match = _STATEMENT_TABLE.match(line)
    return match.group(1).lower() if match else None


def _import_in_parallel(
    sql_dump: Path, server, database: str, client: Path, workers: int,
    progress: ProgressCallback | None, should_stop=None,
) -> int:
    """Import a dump through several database connections at once.

    One connection means one CPU core doing the work while the rest idle, and
    on a multi-gigabyte WordPress dump that is the single longest step of a
    conversion. Each table is sent to one client and always the same one, so
    the statements for a table still arrive in their original order; different
    tables simply load side by side. Statements that belong to no table -- the
    session settings a dump opens with -- go to every client.
    """
    import subprocess
    import tempfile

    total = max(1, sql_dump.stat().st_size)
    processes, errors, stdins = [], [], []
    try:
        for _ in range(workers):
            err = tempfile.TemporaryFile()
            proc = subprocess.Popen(
                _client_command(server, database, client),
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err,
                shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            processes.append(proc)
            errors.append(err)
            stdins.append(proc.stdin)

        assigned: dict[bytes, int] = {}
        pending = bytearray()
        sent = 0
        skipped_bytes = 0
        next_worker = 0

        with sql_dump.open("rb") as source:
            for line in source:
                sent += len(line)
                pending += line
                if not line.rstrip().endswith(b";"):
                    continue  # statement continues on the next line

                statement = bytes(pending)
                pending.clear()

                table = _table_of(statement)
                if table is not None and _skips_content(table) and not statement.lstrip()[:6].upper().startswith((b"CREATE", b"DROP")):
                    skipped_bytes += len(statement)
                    continue
                if table is None:
                    for stdin in stdins:      # session settings, comments, USE
                        stdin.write(statement)
                else:
                    if table not in assigned:
                        # Round robin rather than a hash: it spreads a dump of
                        # a few big tables evenly, which hashing does not.
                        assigned[table] = next_worker % workers
                        next_worker += 1
                    stdins[assigned[table]].write(statement)

                if progress and sent % (8 * 1024 * 1024) < 4096:
                    progress(f"Importing database ({sent / 1048576:,.0f} of "
                             f"{total / 1048576:,.0f} MiB)", sent / total)
                if should_stop is not None and should_stop():
                    raise RewriteCancelled("cancelled during the database import")

            if pending:
                for stdin in stdins:
                    stdin.write(bytes(pending))

        for stdin in stdins:
            stdin.close()
        for proc in processes:
            proc.wait(timeout=3 * 3600)

        problems = []
        for proc, err in zip(processes, errors):
            err.seek(0)
            text = err.read().decode("utf-8", errors="replace")
            for line in text.splitlines()[:10]:
                if line.strip() and "insecure passwordless login" not in line:
                    logger.warning("import: %s", line.strip())
            if proc.returncode != 0 and "ERROR" in text.upper():
                problems.append(text[-600:])
        if problems:
            raise RestoreError("the database client reported: " + " | ".join(problems))

        if skipped_bytes:
            logger.info(
                "import: skipped %.0f MiB of plugin log and session tables "
                "(their structure was still created)", skipped_bytes / 1048576,
            )
        if progress:
            progress("Database imported", 1.0)
        return -1
    except BaseException:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
        raise
    finally:
        for err in errors:
            err.close()


def _client_command(server, database: str, client: Path) -> list[str]:
    command = [
        str(client),
        f"--host={server.host}",
        f"--port={server.port}",
        f"--user={server.user}",
        "--binary-mode",           # dumps can contain binary blobs
        "--default-character-set=utf8mb4",
        "--max-allowed-packet=512M",
        "--force",                 # one bad statement must not abort the import
        # A dump is internally consistent, so checking every row against
        # unique and foreign keys as it lands is wasted work. Autocommit stays
        # on: turning it off would roll everything back when the client exits.
        "--init-command=SET SESSION unique_checks=0, foreign_key_checks=0",
        database,
    ]
    if server.password:
        command.insert(4, f"--password={server.password}")
    return command


def _import_with_client(
    sql_dump: Path, server, database: str, client: Path, progress: ProgressCallback | None
) -> int:
    import subprocess
    import tempfile

    command = [
        str(client),
        f"--host={server.host}",
        f"--port={server.port}",
        f"--user={server.user}",
        "--binary-mode",           # dumps can contain binary blobs
        "--default-character-set=utf8mb4",
        "--max-allowed-packet=512M",
        "--force",                 # one bad statement must not abort the import
        # A dump is internally consistent, so checking every row against
        # unique and foreign keys as it lands is wasted work. Autocommit stays
        # on: turning it off would roll everything back when the client exits.
        "--init-command=SET SESSION unique_checks=0, foreign_key_checks=0",
        database,
    ]
    if server.password:
        command.insert(4, f"--password={server.password}")

    total = max(1, sql_dump.stat().st_size)
    sent = 0
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        # Fed in chunks rather than handed over as a file, so the import can
        # report how far it has got: on a multi-gigabyte dump this is the
        # longest silent wait in the whole job otherwise.
        proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=out, stderr=err, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            with sql_dump.open("rb") as source:
                while chunk := source.read(8 * 1024 * 1024):
                    proc.stdin.write(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(f"Importing database ({sent / 1048576:,.0f} of "
                                 f"{total / 1048576:,.0f} MiB)", sent / total)
            proc.stdin.close()
            proc.wait(timeout=3 * 3600)
        except BrokenPipeError:
            proc.wait(timeout=60)
        except BaseException:
            proc.kill()
            raise
        err.seek(0)
        stderr = err.read().decode("utf-8", errors="replace")

    if proc.returncode != 0 and "ERROR" in stderr.upper():
        raise RestoreError(f"the database client reported: {stderr[-1500:]}")
    if stderr.strip():
        # --force turns errors into warnings; surface them without failing.
        for line in stderr.splitlines()[:20]:
            if line.strip() and "insecure passwordless login" not in line:
                logger.warning("import: %s", line.strip())

    if progress:
        progress("Database imported", 1.0)
    return -1  # the client does not report a statement count


_DELIMITER_RE = re.compile(rb"^\s*DELIMITER\s+(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


def _iter_sql_statements(handle, chunk_size: int = 4 * 1024 * 1024):
    """Yield complete SQL statements from a dump.

    Splitting on ``;`` alone is wrong: a semicolon inside a quoted string, a
    comment or an escape sequence is not a terminator, and post content is full
    of them. This tracks quoting state properly.
    """
    buffer = b""
    in_single = in_double = in_backtick = False
    in_line_comment = in_block_comment = False
    escaped = False

    while True:
        chunk = handle.read(chunk_size)
        if not chunk:
            break
        buffer += chunk

        start = 0
        index = 0
        while index < len(buffer):
            char = buffer[index:index + 1]

            if in_line_comment:
                if char == b"\n":
                    in_line_comment = False
                index += 1
                continue
            if in_block_comment:
                if char == b"*" and buffer[index + 1:index + 2] == b"/":
                    in_block_comment = False
                    index += 2
                    continue
                index += 1
                continue
            if escaped:
                escaped = False
                index += 1
                continue

            if in_single or in_double:
                if char == b"\\":
                    escaped = True
                elif in_single and char == b"'":
                    in_single = False
                elif in_double and char == b'"':
                    in_double = False
                index += 1
                continue
            if in_backtick:
                if char == b"`":
                    in_backtick = False
                index += 1
                continue

            if char == b"'":
                in_single = True
            elif char == b'"':
                in_double = True
            elif char == b"`":
                in_backtick = True
            elif char == b"-" and buffer[index + 1:index + 2] == b"-" and buffer[index + 2:index + 3] in (b" ", b"\t", b"\n", b""):
                in_line_comment = True
            elif char == b"#":
                in_line_comment = True
            elif char == b"/" and buffer[index + 1:index + 2] == b"*":
                in_block_comment = True
                index += 2
                continue
            elif char == b";":
                statement = buffer[start:index].strip()
                if statement:
                    yield statement
                start = index + 1
            index += 1

        buffer = buffer[start:]
        # Guard against a pathological single statement eating all memory.
        if len(buffer) > 512 * 1024 * 1024:
            raise RestoreError("encountered a SQL statement larger than 512 MiB; refusing to buffer it")

    tail = buffer.strip()
    if tail:
        yield tail


def _import_with_python(
    sql_dump: Path, server, database: str, progress: ProgressCallback | None
) -> int:
    total_size = sql_dump.stat().st_size
    executed = 0
    failed = 0

    with server.connect(database) as conn, conn.cursor() as cur:
        # Match what a mysqldump expects of its session.
        for setup in (
            "SET FOREIGN_KEY_CHECKS=0",
            "SET UNIQUE_CHECKS=0",
            "SET sql_mode='NO_ENGINE_SUBSTITUTION'",
            "SET NAMES utf8mb4",
        ):
            cur.execute(setup)

        last_tick = 0.0
        with sql_dump.open("rb") as fh:
            for statement in _iter_sql_statements(fh):
                text = statement.decode("utf-8", errors="surrogateescape")
                if not text.strip() or text.strip().upper().startswith("DELIMITER"):
                    continue
                try:
                    cur.execute(text)
                    executed += 1
                except Exception as exc:
                    failed += 1
                    if failed <= 10:
                        logger.warning("skipping a failed statement: %s", str(exc)[:200])

                if progress:
                    now = time.monotonic()
                    if now - last_tick > 0.5:
                        last_tick = now
                        progress(
                            f"Importing database: {executed} statements",
                            min(0.99, fh.tell() / total_size) if total_size else 0.0,
                        )

        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        cur.execute("SET UNIQUE_CHECKS=1")

    if failed:
        logger.warning("%d SQL statement(s) failed during import", failed)
    logger.info("imported %d statements", executed)
    return executed


# ---------------------------------------------------------------------------
# Post-import configuration and URL replacement
# ---------------------------------------------------------------------------
_TEXT_COLUMN_TYPES = {
    "char", "varchar", "tinytext", "text", "mediumtext", "longtext",
    "blob", "tinyblob", "mediumblob", "longblob", "json",
}


def read_option(server, database: str, table_prefix: str, name: str) -> str | None:
    """Read one row from ``wp_options``."""
    try:
        with server.connect(database) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT option_value FROM `{table_prefix}options` WHERE option_name=%s LIMIT 1",
                (name,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        logger.debug("could not read option %s: %s", name, exc)
        return None


def set_option(server, database: str, table_prefix: str, name: str, value: str) -> None:
    with server.connect(database) as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO `{table_prefix}options` (option_name, option_value, autoload) "
            "VALUES (%s,%s,'yes') ON DUPLICATE KEY UPDATE option_value=VALUES(option_value)",
            (name, value),
        )


def detect_site_url(server, database: str, table_prefix: str, layout: ArchiveLayout) -> str | None:
    """The URL the site was served from originally.

    ``wp_options`` is authoritative; ``package.json`` is the fallback for a
    dump whose options table did not import cleanly.
    """
    for option in ("siteurl", "home"):
        value = read_option(server, database, table_prefix, option)
        if value and value.startswith(("http://", "https://")):
            logger.info("detected original site URL from %s: %s", option, value)
            return value.rstrip("/")

    for candidate in (layout.site_url, layout.home_url):
        if candidate and candidate.startswith(("http://", "https://")):
            logger.info("detected original site URL from package.json: %s", candidate)
            return candidate.rstrip("/")

    logger.warning("could not determine the original site URL")
    return None


@dataclass(slots=True)
class ReplacementReport:
    """What a serialized-safe replacement pass changed."""

    tables_scanned: int = 0
    rows_updated: int = 0
    replacements: int = 0
    skipped_tables: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class RewriteCancelled(Exception):
    """The caller asked the URL rewrite to stop."""


def replace_urls_in_database(
    server,
    database: str,
    replacements: dict[str, str],
    *,
    progress: ProgressCallback | None = None,
    should_stop=None,
) -> ReplacementReport:
    """Apply *replacements* across every text column, safe for serialized data.

    Rows are matched with ``LIKE`` on the search term so only rows that can
    possibly change are read, then rewritten individually through
    :func:`app.utils.phpserialize.replace_in_serialized`.
    """
    report = ReplacementReport()
    if not replacements:
        return report

    with server.connect(database) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s AND table_type='BASE TABLE'",
                (database,),
            )
            tables = [row[0] for row in cur.fetchall()]

        for index, table in enumerate(tables):
            if should_stop is not None and should_stop():
                raise RewriteCancelled("cancelled during the URL rewrite")
            if progress:
                progress(f"Rewriting URLs in {table}", (index + 1) / max(1, len(tables)))
            try:
                _replace_in_table(conn, database, table, replacements, report, should_stop)
                report.tables_scanned += 1
            except Exception as exc:
                report.errors.append(f"{table}: {exc}")
                report.skipped_tables.append(table)
                logger.warning("URL replacement skipped table %s: %s", table, exc)

    logger.info(
        "URL rewrite: %d replacements across %d rows in %d tables",
        report.replacements, report.rows_updated, report.tables_scanned,
    )
    return report


#: Marks a value as PHP-serialized: a string, array or object whose byte
#: length is written next to it. Replacing a URL inside such a value changes
#: that length, so those rows have to be rewritten one at a time in Python.
#: Everything else -- post content, Elementor's JSON, plain settings -- can be
#: rewritten by the database itself, which is far faster than fetching every
#: row into this process and sending it back.
_SERIALIZED_MARKER = r'(s|a|O):[0-9]+:("|{)'


def _replace_in_table(conn, database: str, table: str, replacements: dict[str, str],
                      report: ReplacementReport, should_stop=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, column_key FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (database, table),
        )
        columns = cur.fetchall()

    text_columns = [c[0] for c in columns if c[1].lower() in _TEXT_COLUMN_TYPES]
    if not text_columns:
        return

    primary_keys = [c[0] for c in columns if c[2] == "PRI"]
    if not primary_keys:
        # Without a key there is no safe way to address an individual row.
        report.skipped_tables.append(table)
        logger.debug("table %s has no primary key; leaving it untouched", table)
        return

    skip_revisions = _skip_revisions(conn, table, [c[0] for c in columns])

    # -- the fast path: let the database rewrite its own plain text ---------
    for column in text_columns:
        for search, replace in replacements.items():
            if should_stop is not None and should_stop():
                raise RewriteCancelled("cancelled during the URL rewrite")
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE `{table}` SET `{column}` = REPLACE(`{column}`, %s, %s) "
                        f"WHERE `{column}` LIKE %s AND `{column}` NOT REGEXP %s{skip_revisions}",
                        (search, replace, f"%{search}%", _SERIALIZED_MARKER),
                    )
                    report.rows_updated += cur.rowcount or 0
                    report.replacements += cur.rowcount or 0
            except Exception as exc:
                # Fall back to the row-by-row path for this column rather than
                # losing the table: correctness first, speed second.
                logger.debug("bulk replace failed on %s.%s: %s", table, column, exc)

    # -- the careful path: serialized values, row by row --------------------
    key_list = ", ".join(f"`{k}`" for k in primary_keys)
    conditions: list[str] = []
    like_params: list[str] = []
    for column in text_columns:
        for needle in replacements:
            conditions.append(f"(`{column}` LIKE %s AND `{column}` REGEXP %s)")
            like_params.extend([f"%{needle}%", _SERIALIZED_MARKER])
    where_any = " OR ".join(conditions)

    select_columns = ", ".join(f"`{c}`" for c in text_columns)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {key_list}, {select_columns} FROM `{table}` "
            f"WHERE ({where_any}){skip_revisions}",
            like_params,
        )
        rows = cur.fetchall()

    if not rows:
        return

    key_count = len(primary_keys)
    where_clause = " AND ".join(f"`{k}`=%s" for k in primary_keys)

    with conn.cursor() as cur:
        for number, row in enumerate(rows):
            # The biggest tables hold hundreds of thousands of rows and take
            # minutes; without a check here a cancel would not be noticed
            # until the table finished.
            if should_stop is not None and number % 200 == 0 and should_stop():
                raise RewriteCancelled("cancelled during the URL rewrite")
            keys = row[:key_count]
            values = row[key_count:]

            updates: dict[str, object] = {}
            for column, value in zip(text_columns, values):
                if value is None or isinstance(value, (int, float)):
                    continue
                new_value, count = replace_in_serialized(value, replacements)
                if count:
                    updates[column] = new_value
                    report.replacements += count

            if updates:
                assignments = ", ".join(f"`{c}`=%s" for c in updates)
                cur.execute(
                    f"UPDATE `{table}` SET {assignments} WHERE {where_clause}",
                    (*updates.values(), *keys),
                )
                report.rows_updated += 1


def _skip_revisions(conn, table: str, column_names: list[str]) -> str:
    """An extra WHERE clause that leaves post revisions out of the URL rewrite.

    Revisions are never rendered, yet on a page-builder site they are most of
    the database -- every save keeps a full copy of the layout. Rewriting them
    is the bulk of the work and changes nothing in the export.
    """
    lowered = table.lower()
    if lowered.endswith("postmeta") and "post_id" in column_names:
        posts = table[: -len("postmeta")] + "posts"
        if _table_exists(conn, posts):
            return (f" AND `post_id` NOT IN (SELECT `ID` FROM `{posts}` "
                    "WHERE `post_type` = 'revision')")
    elif lowered.endswith("posts") and "post_type" in column_names:
        return " AND `post_type` <> 'revision'"
    return ""


def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES LIKE %s", (table,))
        return cur.fetchone() is not None


#: Options All-in-One WP Migration strips from its export.
#:
#: Its exporter leaves these out because its own importer writes them on the
#: destination site. Importing the dump directly, as this tool does, therefore
#: produces a WordPress with no active theme and no active plugins -- which
#: renders every page as an empty document. Nothing in the dump signals that
#: this has happened; the tables are all present and the site returns HTTP 200.
_ACTIVATION_OPTIONS = ("stylesheet", "template", "active_plugins")


def _intended_theme(server, database: str, table_prefix: str,
                    installed: dict[str, dict]) -> tuple[str | None, str]:
    """The theme the source site was using, from what the database remembers.

    ``theme_mods_<slug>`` rows name every theme that was ever configured, and
    ``current_theme`` holds the display name of the one in use. Neither needs
    the theme's files to be present, which is exactly the case that matters.
    """
    current = (read_option(server, database, table_prefix, "current_theme") or "").strip()

    slugs: list[str] = []
    try:
        with server.connect(database) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT option_name FROM `{table_prefix}options` "
                "WHERE option_name LIKE 'theme_mods_%'"
            )
            slugs = [name[len("theme_mods_"):] for (name,) in cur.fetchall()]
    except Exception as exc:
        logger.debug("could not read theme_mods options: %s", exc)

    # The display name is the strongest signal: match it against the slugs.
    if current:
        simplified = re.sub(r"[^a-z0-9]", "", current.lower())
        for slug in slugs:
            if re.sub(r"[^a-z0-9]", "", slug.lower()) == simplified:
                return slug, current
        for slug, headers in installed.items():
            if headers.get("theme_name", "").strip().lower() == current.lower():
                return slug, current

    # Otherwise prefer a configured theme that is not a bundled default.
    custom = [s for s in slugs if not s.startswith("twenty")]
    if custom:
        return custom[0], current or custom[0]
    return (slugs[0], current or slugs[0]) if slugs else (None, current)


def _read_theme_headers(style_css: Path) -> dict[str, str]:
    """Parse the header block at the top of a theme's ``style.css``."""
    try:
        head = style_css.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return {}
    headers: dict[str, str] = {}
    for field_name in ("Theme Name", "Template", "Version"):
        match = re.search(rf"^[ \t/*#@]*{field_name}\s*:\s*(.+)$", head, re.IGNORECASE | re.MULTILINE)
        if match:
            headers[field_name.lower().replace(" ", "_")] = match.group(1).strip()
    return headers


def _no_plugins_recorded(raw: object) -> bool:
    """Whether the backup carries no usable list of active plugins."""
    if not raw:
        return True
    text = str(raw).strip()
    if text in {"a:0:{}", "[]", "{}"}:
        return True
    try:
        value = loads(text)
    except (PhpSerializationError, ValueError, TypeError):
        return False
    return not value


def _discover_plugin_entrypoints(plugins_dir: Path) -> list[str]:
    """Every installed plugin, as WordPress records it in ``active_plugins``.

    That is ``directory/main-file.php`` for a normal plugin and ``file.php``
    for a single-file one. A plugin's main file is the one carrying a
    ``Plugin Name:`` header.
    """
    entries: list[str] = []
    if not plugins_dir.is_dir():
        return entries

    def has_header(path: Path) -> bool:
        try:
            return "plugin name:" in path.read_text(
                encoding="utf-8", errors="replace"
            )[:8192].lower()
        except OSError:
            return False

    for item in sorted(plugins_dir.iterdir()):
        if item.is_file() and item.suffix.lower() == ".php":
            if has_header(item):
                entries.append(item.name)
            continue
        if not item.is_dir():
            continue

        candidates = sorted(item.glob("*.php"))
        # The main file is usually named after its directory; check that first
        # so a plugin shipping several headed files is recorded correctly.
        preferred = item / f"{item.name}.php"
        if preferred in candidates:
            candidates.insert(0, candidates.pop(candidates.index(preferred)))

        for candidate in candidates:
            if has_header(candidate):
                entries.append(f"{item.name}/{candidate.name}")
                break

    return entries


def repair_activation_state(  # noqa: PLR0912 - one decision per source of truth
    server, database: str, table_prefix: str, wordpress_root: Path
, recorded: ArchiveLayout | None = None) -> list[str]:
    """Restore the active theme and plugin list that the export left out.

    Returns human-readable notes for the conversion report. Does nothing when
    the dump already carries the options, so a backup produced by a plain
    ``mysqldump`` is untouched.
    """
    notes: list[str] = []
    wordpress_root = Path(wordpress_root)

    present = {
        name: read_option(server, database, table_prefix, name)
        for name in _ACTIVATION_OPTIONS
    }

    # ---- theme ------------------------------------------------------------
    themes_dir = wordpress_root / "wp-content" / "themes"
    installed = {
        d.name: _read_theme_headers(d / "style.css")
        for d in sorted(themes_dir.iterdir())
        if d.is_dir() and (d / "style.css").is_file()
    } if themes_dir.is_dir() else {}

    intended, intended_label = _intended_theme(server, database, table_prefix, installed)
    if recorded is not None and recorded.stylesheet:
        # package.json states the theme outright; nothing guesses better.
        intended, intended_label = recorded.stylesheet, recorded.stylesheet
    if intended and intended not in installed:
        # The site's own theme is not in the backup. Every page will render
        # with a stand-in theme, so the export cannot look like the original:
        # the header, footer, fonts and colours are the theme's, and any
        # content type the theme registers will 404. This is the loudest
        # signal the restore can give, because everything downstream looks
        # superficially fine -- pages render, links resolve, screenshots even
        # match, since both sides use the same wrong theme.
        notes.append(
            f"MISSING-THEME: the backup does not contain the site's theme "
            f"({intended_label or intended}). Only these themes are present: "
            f"{', '.join(sorted(installed)) or 'none'}. The export will not look like the "
            "original site. Re-export the backup with themes included "
            "(in All-in-One WP Migration, do not tick 'Do not export themes')."
        )

    stylesheet = present.get("stylesheet")
    if not stylesheet or stylesheet not in installed:
        chosen = None

        # A theme_mods_<slug> option names the slug outright, and its presence
        # means that theme was actually configured on the source site.
        try:
            with server.connect(database) as conn, conn.cursor() as cur:
                cur.execute(
                    f"SELECT option_name FROM `{table_prefix}options` "
                    "WHERE option_name LIKE 'theme_mods_%'"
                )
                for (option_name,) in cur.fetchall():
                    slug = option_name[len("theme_mods_"):]
                    if slug in installed:
                        chosen = slug
                        break
        except Exception as exc:
            logger.debug("could not read theme_mods options: %s", exc)

        if not chosen and recorded is not None and recorded.stylesheet in installed:
            chosen = recorded.stylesheet

        # Otherwise match the human-readable name WordPress also stores.
        if not chosen:
            current = (read_option(server, database, table_prefix, "current_theme") or "").strip()
            if current:
                for slug, headers in installed.items():
                    if headers.get("theme_name", "").strip().lower() == current.lower():
                        chosen = slug
                        break

        # Last resort: any installed theme, preferring a non-default one.
        if not chosen and installed:
            non_default = [s for s in installed if not s.startswith("twenty")]
            chosen = (non_default or list(installed))[0]

        if chosen:
            parent = installed[chosen].get("template") or chosen
            if parent not in installed:
                parent = chosen
            set_option(server, database, table_prefix, "stylesheet", chosen)
            set_option(server, database, table_prefix, "template", parent)
            label = installed[chosen].get("theme_name") or chosen
            notes.append(
                f"The backup did not record which theme was active, so it was set to "
                f"{label} ({chosen})"
                + (f" with parent theme {parent}" if parent != chosen else "")
                + ". All-in-One WP Migration omits this option from its exports."
            )
            logger.info("activated theme %r (template %r)", chosen, parent)
        else:
            notes.append(
                "No usable theme was found in the backup, so pages will render "
                "with whatever WordPress falls back to."
            )

    # ---- plugins ----------------------------------------------------------
    # An empty list counts as "not recorded". All-in-One WP Migration strips
    # the option, and some exports leave it as a serialized empty array; a
    # site whose header, footer and page layouts are Elementor templates
    # renders as a bare theme without its plugins, which looks like a
    # conversion fault but is a restore one.
    if _no_plugins_recorded(present.get("active_plugins")):
        entries = _discover_plugin_entrypoints(wordpress_root / "wp-content" / "plugins")

        # A plugin the owner recently switched off should stay off.
        deactivated: set[str] = set()
        raw = read_option(server, database, table_prefix, "recently_activated")
        if raw:
            try:
                parsed = loads(raw)
                if isinstance(parsed, dict):
                    deactivated = {
                        (k.decode() if isinstance(k, bytes) else str(k)) for k in parsed
                    }
            except PhpSerializationError:
                pass

        activate = [e for e in entries if e not in deactivated]

        # The backup records exactly which plugins were running. Activating
        # everything installed instead switches on plugins the site had
        # deliberately disabled -- an abandoned page-builder add-on years out
        # of step with the builder itself can stop whole sections rendering.
        if recorded is not None and recorded.plugins:
            wanted = {str(p).strip().lstrip("/") for p in recorded.plugins if p}
            by_directory = {e.split("/", 1)[0]: e for e in entries}
            matched = [e for e in entries if e in wanted]
            for name in wanted:
                directory = name.split("/", 1)[0]
                if name not in entries and directory in by_directory:
                    # Same plugin, different main file (a version suffix, or a
                    # renamed entry point).
                    matched.append(by_directory[directory])
            if matched:
                activate = [e for e in dict.fromkeys(matched) if e not in deactivated]
        if activate:
            serialised = dumps({index: value for index, value in enumerate(activate)})
            set_option(
                server, database, table_prefix, "active_plugins",
                serialised.decode("utf-8", "surrogateescape"),
            )
            from_record = bool(recorded is not None and recorded.plugins)
            notes.append(
                (f"The database recorded no active plugins; the backup's package.json "
                 f"lists {len(activate)} that were running, and those were activated"
                 if from_record else
                 f"Neither the database nor the backup's metadata recorded which plugins "
                 f"were active, so all {len(activate)} installed plugin(s) were activated")
                + (f" ({len(deactivated)} recently-deactivated one(s) left off)"
                   if deactivated else "")
                + ". A page builder such as Elementor cannot render its pages "
                "unless it is running."
            )
            logger.info("activated %d plugin(s)", len(activate))

    return notes


def configure_for_static_export(server, database: str, table_prefix: str, site_url: str) -> list[str]:
    """Apply the option changes a faithful, crawlable render needs.

    Returns a list of human-readable notes for the conversion report.
    """
    notes: list[str] = []

    set_option(server, database, table_prefix, "siteurl", site_url)
    set_option(server, database, table_prefix, "home", site_url)

    permalink = read_option(server, database, table_prefix, "permalink_structure")
    if not permalink:
        # A site on "plain" permalinks serves ?p=123 URLs, which map to ugly but
        # valid static paths. Leave it alone: changing it would alter every URL
        # in the export and break inbound links the user already has.
        notes.append(
            "The site uses plain permalinks (?p=123). Exported paths follow that "
            "structure; enable pretty permalinks before backing up if you want "
            "clean directories."
        )

    set_option(server, database, table_prefix, "blog_public", "1")

    # Make Elementor write its CSS into the page instead of into files.
    #
    # By default it keeps each page's styles in
    # wp-content/uploads/elementor/css/post-N.css and generates them on demand.
    # A backup taken after that cache was cleared contains none of them, and
    # the ones it never regenerates -- theme-builder templates, in particular --
    # are requested by the browser and answered with WordPress's "not found"
    # page. What reaches the export is a stylesheet-shaped file full of HTML,
    # and the page silently loses its layout: on one real site the home page
    # lost its hero background entirely, scoring 91% against the original for
    # no reason the report could name.
    #
    # Printing the CSS inline removes the whole class of problem: the styles
    # are in the DOM the renderer captures, so there is no file to miss. It
    # costs some duplication between pages, which compresses away in the ZIP.
    set_option(server, database, table_prefix, "elementor_css_print_method", "internal")

    # And clear Elementor's record of what it has already generated.
    #
    # Setting the print method alone was not enough: each post carries an
    # ``_elementor_css`` meta saying "this page's stylesheet exists and is
    # current", and while that is there Elementor enqueues the file rather
    # than printing anything. The file, meanwhile, was never in the backup --
    # uploads/elementor/css is a cache, and a backup taken after it was
    # cleared contains none of it. The database claimed a file that did not
    # exist, so nothing regenerated it and nothing reported it missing.
    #
    # Deleting the record is what the "Regenerate CSS" button in Elementor's
    # own settings does. With it gone and the print method internal, the
    # styles are rebuilt into the page that needs them.
    cleared = 0
    try:
        with server.connect(database) as conn, conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM `{table_prefix}postmeta` WHERE meta_key = %s", ("_elementor_css",)
            )
            cleared = cur.rowcount or 0
            cur.execute(
                f"DELETE FROM `{table_prefix}options` WHERE option_name IN (%s, %s)",
                ("_elementor_global_css", "elementor_global_css"),
            )
    except Exception as exc:
        # A site without Elementor has no such rows, and a site with a
        # different schema is not worth failing a restore over.
        logger.debug("could not clear Elementor's CSS cache: %s", exc)

    if cleared:
        notes.append(
            f"Cleared Elementor's record of {cleared} generated stylesheet(s) and set it "
            "to print CSS into each page, so a page cannot lose its styling to a "
            "stylesheet the backup does not contain."
        )
    else:
        notes.append(
            "Elementor now prints its CSS into each page rather than into separate "
            "files, so a page cannot lose its styling to a stylesheet the backup "
            "does not contain."
        )

    # Drop the cached rewrite rules. They were generated on the source host and
    # can be stale in two ways that both produce 404s during the crawl: they
    # may encode the old domain's structure, and they may pre-date a custom
    # post type whose plugin was activated later. Deleting the option makes
    # WordPress regenerate the full rule set on the next request.
    try:
        with server.connect(database) as conn, conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM `{table_prefix}options` WHERE option_name=%s", ("rewrite_rules",)
            )
        notes.append("Regenerated WordPress rewrite rules for the local server.")
    except Exception as exc:
        logger.warning("could not clear the cached rewrite rules: %s", exc)

    return notes


def deactivate_problem_plugins(server, database: str, table_prefix: str) -> list[str]:
    """Switch off plugins that actively prevent a faithful local render.

    This is deliberately a very short list. Plugins are what produce the design,
    so the default is to leave them all running; only ones that serve cached
    HTML, force a domain or block non-live hosts are disabled, and each one is
    reported.
    """
    # Matched against the plugin's directory name.
    disruptive = {
        "wp-super-cache": "page cache: would serve HTML generated for the live domain",
        "w3-total-cache": "page cache: would serve HTML generated for the live domain",
        "wp-rocket": "page cache: would serve HTML generated for the live domain",
        "litespeed-cache": "page cache: requires LiteSpeed and would serve stale HTML",
        "wp-fastest-cache": "page cache: would serve HTML generated for the live domain",
        "cache-enabler": "page cache: would serve HTML generated for the live domain",
        "really-simple-ssl": "forces HTTPS, which the local HTTP server cannot satisfy",
        "wordfence": "blocks unrecognised hosts and slows every request",
        "better-wp-security": "blocks unrecognised hosts and can lock out the crawler",
        "ithemes-security-pro": "blocks unrecognised hosts and can lock out the crawler",
        "wps-hide-login": "rewrites URLs in ways that break a local crawl",
        "redirection": "may redirect local URLs back to the live domain",
    }

    # Plugins that do their work in the dashboard and contribute nothing to a
    # rendered page. Leaving them on costs time on every single page -- and on
    # a large site that is the difference between a page building in seconds
    # and timing out -- without changing a pixel of the result.
    admin_only = {
        "all-in-one-wp-migration": "backup tool: dashboard only",
        "updraftplus": "backup tool: dashboard only",
        "duplicator": "backup tool: dashboard only",
        "better-search-replace": "database tool: dashboard only",
        "wp-optimize": "database cleaner: dashboard only",
        "wp-sweep": "database cleaner: dashboard only",
        "duplicate-post": "editing helper: dashboard only",
        "wp-mail-smtp": "email delivery: nothing to render",
        "akismet": "comment spam checking: nothing to render",
        "classic-editor": "editing helper: dashboard only",
        "regenerate-thumbnails": "media tool: dashboard only",
        "wordpress-importer": "import tool: dashboard only",
        "query-monitor": "developer tool: adds output to every page",
    }
    disruptive.update(admin_only)

    raw = read_option(server, database, table_prefix, "active_plugins")
    if not raw:
        return []

    try:
        from app.utils.phpserialize import dumps, loads

        active = loads(raw)
    except Exception:
        return []

    if not isinstance(active, dict):
        return []

    kept: dict[object, object] = {}
    notes: list[str] = []

    for key, value in active.items():
        entry = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
        directory = entry.split("/", 1)[0]
        # Folder names often carry a version, as in
        # all-in-one-wp-migration-6.77, so match on the prefix.
        reason = disruptive.get(directory) or next(
            (why for name, why in disruptive.items()
             if directory.startswith(name + "-") or directory.startswith(name + ".")),
            None,
        )
        if reason:
            notes.append(f"Deactivated {directory} for the render ({reason}).")
        else:
            kept[key] = value

    if notes:
        # Re-index so the array stays a clean list, as WordPress expects.
        renumbered = {index: value for index, value in enumerate(kept.values())}
        set_option(
            server, database, table_prefix, "active_plugins",
            dumps(renumbered).decode("utf-8", "surrogateescape"),
        )

    return notes
