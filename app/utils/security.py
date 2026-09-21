"""Security primitives shared by every stage of the pipeline.

A ``.wpress`` archive is *untrusted input*: the header block stores a 4096-byte
``prefix`` (directory) and a 255-byte ``name`` that were written by whatever
machine produced the backup.  Nothing stops a hostile archive from claiming a
prefix of ``../../../../Windows/System32``.  The reference Go extractor
(fifthsegment/Wpress-Extractor) does ``path.Clean("./" + prefix + "/" + name)``
which does **not** prevent escaping the destination directory, so every path
coming out of an archive is normalised and re-validated here instead.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

__all__ = [
    "PathTraversalError",
    "UnsafeArchivePath",
    "safe_join",
    "sanitise_archive_path",
    "sanitise_component",
    "is_within",
    "safe_output_path",
]


class PathTraversalError(ValueError):
    """Raised when a path would escape its designated root directory."""


class UnsafeArchivePath(ValueError):
    """Raised when an archive member's path cannot be made safe at all."""


# Characters Windows forbids in a path component, plus control characters.
_WINDOWS_FORBIDDEN = re.compile(r'[<>:"|?*\x00-\x1f]')

# Device names Windows still reserves, with or without an extension.
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# NTFS alternate data streams / trailing dots and spaces.
_TRAILING_JUNK = re.compile(r"[. ]+$")

_MAX_COMPONENT = 200          # leave head-room under the 255-byte NTFS limit
_MAX_TOTAL_PATH = 30_000      # sanity ceiling; real limit handled by the OS


def sanitise_component(component: str, *, fallback: str = "_") -> str:
    """Make a single path component safe to create on Windows *and* POSIX.

    Returns ``fallback`` when the component is empty, a traversal token, or is
    reduced to nothing by sanitisation.
    """
    if not component:
        return fallback

    # Normalise unicode so visually-identical names collapse to one file and
    # so decomposed sequences cannot smuggle separators past the checks below.
    component = unicodedata.normalize("NFC", component)

    # Strip anything that looks like a directory separator: an archive member
    # name (as opposed to its prefix) must never contain one.
    component = component.replace("/", "_").replace("\\", "_")

    component = _WINDOWS_FORBIDDEN.sub("_", component)
    component = _TRAILING_JUNK.sub("", component)

    if component in {"", ".", ".."}:
        return fallback

    stem = component.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        component = f"_{component}"

    if len(component) > _MAX_COMPONENT:
        # Preserve the extension, which matters for MIME sniffing later on.
        root, dot, ext = component.rpartition(".")
        if dot and len(ext) <= 16:
            keep = _MAX_COMPONENT - len(ext) - 1
            component = f"{root[:keep]}.{ext}"
        else:
            component = component[:_MAX_COMPONENT]

    return component or fallback


def sanitise_archive_path(prefix: str, name: str) -> PurePosixPath:
    """Turn an archive header's ``prefix`` + ``name`` into a safe relative path.

    The result is always relative, never contains ``..``, never starts from a
    drive letter or root, and every component is legal on Windows.

    Raises :class:`UnsafeArchivePath` if nothing usable survives.
    """
    prefix = (prefix or "").strip()
    name = (name or "").strip()

    if not name:
        raise UnsafeArchivePath("archive member has an empty filename")

    # All-in-One WP Migration writes POSIX prefixes, but backups produced on
    # Windows hosts can carry backslashes, so treat both as separators.
    raw = prefix.replace("\\", "/")

    # Drop a drive letter ("C:/foo") or UNC root ("//server/share") outright.
    if re.match(r"^[A-Za-z]:", raw):
        raw = raw[2:]
    raw = raw.lstrip("/")

    parts: list[str] = []
    for chunk in raw.split("/"):
        chunk = chunk.strip()
        if chunk in {"", ".", ".."}:
            # ".." is dropped rather than resolved: resolving it would let a
            # crafted prefix climb out of the extraction root.
            continue
        parts.append(sanitise_component(chunk))

    parts.append(sanitise_component(name))

    safe = PurePosixPath(*parts)
    if len(str(safe)) > _MAX_TOTAL_PATH:
        raise UnsafeArchivePath(f"archive member path is absurdly long: {len(str(safe))} bytes")
    return safe


def is_within(root: Path, candidate: Path) -> bool:
    """True when *candidate* resolves to a location inside *root*.

    Only the part of the path that already exists is resolved, and the rest is
    appended unresolved. That keeps the symlink protection -- a symlinked
    parent still resolves to wherever it really points -- while giving a
    stable answer for a path whose folders are being created at that moment by
    another thread, which Windows can otherwise report under a different name.
    """
    try:
        root_r = root.resolve(strict=False)
        node, missing = candidate, []
        while not node.exists() and node.parent != node:
            missing.append(node.name)
            node = node.parent
        cand_r = node.resolve(strict=False).joinpath(*reversed(missing))
    except (OSError, RuntimeError):
        return False
    try:
        cand_r.relative_to(root_r)
    except ValueError:
        # Windows compares paths case-insensitively, and a drive can report a
        # different case for the same folder.
        return os.path.normcase(str(cand_r)).startswith(
            os.path.normcase(str(root_r)) + os.sep
        )
    return True


def safe_join(root: Path, *relative: str | os.PathLike[str]) -> Path:
    """Join *relative* onto *root*, refusing anything that escapes *root*.

    This is the only sanctioned way to turn untrusted path fragments into a
    filesystem destination.
    """
    root = Path(root)
    candidate = root.joinpath(*[os.fspath(r) for r in relative])

    # Reject symlinked parents that point outside the root, which is how a
    # two-stage archive (symlink first, file second) would break containment.
    if not is_within(root, candidate):
        raise PathTraversalError(
            f"refusing to write outside the job workspace: {candidate!s} is not under {root!s}"
        )
    return candidate


def safe_output_path(root: Path, url_path: str, *, index_name: str = "index.html") -> Path:
    """Map a site-relative URL path onto a safe file path beneath *root*.

    ``/about/``      -> ``root/about/index.html``
    ``/``            -> ``root/index.html``
    ``/a/b.html``    -> ``root/a/b.html``
    """
    cleaned = (url_path or "/").split("#", 1)[0].split("?", 1)[0]
    cleaned = cleaned.replace("\\", "/")

    parts = [sanitise_component(p) for p in cleaned.split("/") if p not in {"", ".", ".."}]

    if not parts:
        return safe_join(root, index_name)

    last = parts[-1]
    if cleaned.endswith("/") or "." not in last:
        parts.append(index_name)

    return safe_join(root, *parts)


#: A Windows drive path (C:\Users\...\file) or a POSIX home path (/home/bob/...).
#: Both separators are matched: a character class of ``[\\/]`` is easy to write
#: as ``[\/]`` by mistake, which silently matches only forward slashes and lets
#: every Windows path through.
_HOST_PATH = re.compile(
    r"""(?:[A-Za-z]:[\\/]|(?:/home/|/Users/|\\\\))[^\s"'<>|,;)]{2,}"""
)


def scrub_windows_path(text: str) -> str:
    """Replace host filesystem paths in user-facing text with ``<path>``.

    Error messages travel to the browser, and a stack trace or OS error
    routinely embeds the full path of the job workspace. That discloses the
    operator's username and directory layout for no benefit.
    """
    return _HOST_PATH.sub("<path>", text)
