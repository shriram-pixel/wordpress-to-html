"""Package the export, including only what belongs on a public web server.

The ZIP is the deliverable, so what is *left out* matters as much as what goes
in. The temporary WordPress, the database, ``wp-config.php`` with its
credentials, the job logs, the screenshots and every internal artefact stay
behind. Only the generated static site is packaged.

The output directory is built to contain exactly the deliverable, so packaging
is mostly a copy -- but an explicit deny-list runs anyway, because a single
leaked ``wp-config.php`` would publish database credentials.
"""

from __future__ import annotations

import fnmatch
import logging
import posixpath
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from app.utils.filesystem import iter_files

logger = logging.getLogger(__name__)

#: Never package these, whatever produced them.
#:
#: ``*.php`` covers wp-config.php and every other server-side file in one rule:
#: a static export has no PHP interpreter, so a .php file in the deliverable is
#: at best dead weight and at worst a credentials leak.
_DENY_GLOBS = (
    "*.php", "*.phtml", "*.php5", "*.phar",
    "*.sql", "*.sql.gz",
    ".env", ".env.*", "*.log", "*.pem", "*.key", "*.p12", "*.pfx",
    ".htpasswd", ".htaccess", "*.bak", "*.swp",
    "php.ini", "*.original",
    # Internal artefacts, in case the output directory is ever reused.
    "*.part", ".tmp-*",
)

#: Directories that must never appear in the deliverable.
#:
#: Note what is deliberately *absent*: ``wp-includes`` and ``wp-content``.
#: Excluding those looks tidy and is badly wrong -- WordPress serves real
#: front-end assets from them. ``wp-includes/js/dist/script-modules/`` holds
#: the Interactivity API that drives the navigation block, and
#: ``wp-includes/blocks/*/style.min.css`` holds core block styling. Dropping
#: them produces a ZIP that passes every check against the output directory and
#: then loses its mobile menu once deployed. Only genuinely server-side
#: directories belong here.
_DENY_DIRS = (
    "mu-plugins",
    "__wpsc__", "node_modules", ".git", ".svn", ".idea", ".vscode",
)

#: Extensions that gain nothing from deflate; stored instead, which is faster.
_ALREADY_COMPRESSED = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".ico",
    ".woff", ".woff2", ".mp4", ".webm", ".mp3", ".ogg", ".zip", ".gz", ".pdf",
})


@dataclass(slots=True)
class ZipResult:
    path: Path
    files: int = 0
    bytes_uncompressed: int = 0
    bytes_compressed: int = 0
    excluded: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        if not self.bytes_uncompressed:
            return 0.0
        return self.bytes_compressed / self.bytes_uncompressed


def should_exclude(relative_path: str) -> str | None:
    """Reason to exclude *relative_path*, or ``None`` to include it."""
    posix = relative_path.replace("\\", "/")
    name = posixpath.basename(posix)
    parts = posix.split("/")

    for directory in _DENY_DIRS:
        if directory in parts[:-1]:
            return f"inside {directory}/"

    for pattern in _DENY_GLOBS:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(posix, pattern):
            return f"matches {pattern}"

    return None


def build_zip(
    source_dir: Path,
    zip_path: Path,
    *,
    progress=None,
    compresslevel: int = 6,
) -> ZipResult:
    """Package *source_dir* into *zip_path*."""
    source_dir = Path(source_dir)
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    if not source_dir.is_dir():
        raise FileNotFoundError(f"nothing to package: {source_dir} does not exist")

    files = sorted(iter_files(source_dir), key=lambda p: p.as_posix().lower())
    if not files:
        raise ValueError(f"nothing to package: {source_dir} is empty")

    result = ZipResult(path=zip_path)
    total = len(files)

    # A fresh archive every time: appending to a stale one would ship files
    # from a previous run.
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=compresslevel,
        allowZip64=True,
    ) as archive:
        for index, path in enumerate(files):
            relative = path.relative_to(source_dir).as_posix()

            reason = should_exclude(relative)
            if reason:
                result.excluded.append(f"{relative} ({reason})")
                logger.debug("excluded from ZIP: %s -- %s", relative, reason)
                continue

            try:
                size = path.stat().st_size
                extension = path.suffix.lower()
                info = zipfile.ZipInfo.from_file(path, relative)
                # Normalise permissions: files extracted on a web server should
                # be readable, not carry whatever the local umask produced.
                info.external_attr = (0o644 << 16)
                info.compress_type = (
                    zipfile.ZIP_STORED if extension in _ALREADY_COMPRESSED
                    else zipfile.ZIP_DEFLATED
                )
                with path.open("rb") as source, archive.open(info, "w") as target:
                    while chunk := source.read(1 << 20):
                        target.write(chunk)

                result.files += 1
                result.bytes_uncompressed += size
            except OSError as exc:
                logger.warning("could not add %s to the ZIP: %s", relative, exc)
                result.excluded.append(f"{relative} (unreadable: {exc})")

            if progress and (index % 50 == 0 or index == total - 1):
                progress(f"Packaging ({index + 1}/{total})", (index + 1) / total)

    result.bytes_compressed = zip_path.stat().st_size

    logger.info(
        "packaged %d files into %s (%.1f MiB, %.0f%% of original)",
        result.files, zip_path.name, result.bytes_compressed / 1048576, result.ratio * 100,
    )
    if result.excluded:
        logger.info("%d path(s) excluded from the ZIP", len(result.excluded))

    return result


def verify_zip(
    zip_path: Path, *, expect_index: bool = True, source_dir: Path | None = None
) -> list[str]:
    """Check the archive opens, is intact, complete, and carries no secret.

    Returns a list of problems; empty means the archive is good.

    Passing *source_dir* enables the completeness check, which matters more
    than it sounds: link and asset validation runs against the output
    directory, so an over-broad exclusion rule can drop a stylesheet from the
    ZIP while every check still reports a clean export. Comparing the two sets
    is the only thing that catches that before the user deploys it.
    """
    problems: list[str] = []
    zip_path = Path(zip_path)

    if not zip_path.is_file():
        return [f"{zip_path} was not created"]

    try:
        with zipfile.ZipFile(zip_path) as archive:
            bad = archive.testzip()
            if bad is not None:
                problems.append(f"the archive is corrupt at {bad}")

            names = archive.namelist()
            if not names:
                problems.append("the archive is empty")

            if expect_index and "index.html" not in names:
                problems.append("the archive has no index.html at its root")

            for name in names:
                reason = should_exclude(name)
                if reason:
                    problems.append(f"{name} should not have been packaged ({reason})")

            if source_dir is not None:
                packaged = set(names)
                for path in iter_files(Path(source_dir)):
                    relative = path.relative_to(source_dir).as_posix()
                    if relative in packaged or should_exclude(relative):
                        continue
                    problems.append(
                        f"{relative} is part of the generated site but was left out of the ZIP"
                    )
    except zipfile.BadZipFile as exc:
        problems.append(f"the archive could not be opened: {exc}")

    return problems
