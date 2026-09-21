"""``.wpress`` archive reading, writing and extraction.

Format
------
``.wpress`` is the container All-in-One WP Migration writes. It is a flat,
uncompressed, tar-like stream of ``header + payload`` pairs terminated by a
zero-filled header:

===============  ======  ======  =========================================
Field            Offset  Length  Contents
===============  ======  ======  =========================================
Name                  0     255  filename only, no path, NUL padded
Size                255      14  ASCII decimal byte-length of the payload
Mtime               269      12  ASCII decimal unix mtime
Prefix              281    4096  directory path, no trailing slash
===============  ======  ======  =========================================

Total header size is 4377 bytes. EOF is a header block of 4377 NUL bytes.
There is no compression, no checksum and no central directory, so the only way
to enumerate an archive is to walk it.

This module is a clean-room Python implementation derived from reading the
reference Go extractor (``fifthsegment/Wpress-Extractor``, MIT, itself based on
``yani-/wpress``). It deliberately differs from that implementation in ways
that matter for running it as a service:

* **Path traversal is blocked.** The Go reader builds its destination with
  ``path.Clean("./" + prefix + "/" + name)``, which happily writes outside the
  working directory when a hostile archive supplies a ``..`` prefix. Every
  member here goes through :func:`app.utils.security.sanitise_archive_path`.
* **The destination is explicit** rather than the process working directory.
* **Reads are buffered at 1 MiB** rather than 512 bytes.
* **Truncated archives are recoverable**: a short final member is reported as a
  warning and the files already recovered are kept, instead of failing the run.
* **Progress is reported** so a long extraction can drive the job UI.
* **No Go toolchain or bundled binary is required.**
"""

from __future__ import annotations

import abc
import dataclasses
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath

from app.utils.security import (
    PathTraversalError,
    UnsafeArchivePath,
    safe_join,
    sanitise_archive_path,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------
HEADER_SIZE = 4377
NAME_SIZE = 255
SIZE_SIZE = 14
MTIME_SIZE = 12
PREFIX_SIZE = 4096

NAME_OFFSET = 0
SIZE_OFFSET = NAME_OFFSET + NAME_SIZE            # 255
MTIME_OFFSET = SIZE_OFFSET + SIZE_SIZE           # 269
PREFIX_OFFSET = MTIME_OFFSET + MTIME_SIZE        # 281

EOF_BLOCK = b"\x00" * HEADER_SIZE
_COPY_CHUNK = 1024 * 1024

# A single member larger than this is treated as a corrupt size field rather
# than a real file; no WordPress asset legitimately reaches a terabyte.
_MAX_MEMBER_BYTES = 1 << 40


class WpressError(Exception):
    """Base class for archive-level failures."""


class InvalidWpressArchive(WpressError):
    """The file is not a readable ``.wpress`` archive."""


class CorruptWpressArchive(WpressError):
    """The archive structure broke part-way through."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclasses.dataclass(slots=True)
class WpressEntry:
    """One member of the archive, as described by its header block."""

    name: str
    prefix: str
    size: int
    mtime: int
    offset: int
    """Byte offset of the payload (i.e. just past this member's header)."""

    @property
    def archive_path(self) -> str:
        """The path as the archive claims it, unsanitised. For reporting only."""
        return f"{self.prefix}/{self.name}" if self.prefix else self.name

    @property
    def safe_path(self) -> PurePosixPath:
        """The sanitised relative path this member may be written to."""
        return sanitise_archive_path(self.prefix, self.name)


@dataclasses.dataclass(slots=True)
class ExtractionResult:
    """Outcome of extracting an archive."""

    destination: Path
    files_written: int = 0
    bytes_written: int = 0
    entries_skipped: int = 0
    warnings: list[str] = dataclasses.field(default_factory=list)
    duration_seconds: float = 0.0
    truncated: bool = False

    def add_warning(self, message: str) -> None:
        logger.warning("wpress: %s", message)
        self.warnings.append(message)


ProgressCallback = Callable[[int, int, str], None]
"""``(bytes_done, bytes_total, current_path)``. ``bytes_total`` is the archive
size, which is an exact proxy for progress since there is no compression."""


# ---------------------------------------------------------------------------
# Header codec
# ---------------------------------------------------------------------------
def _decode_field(raw: bytes) -> str:
    """Decode a NUL-padded header field.

    Filenames in real archives are UTF-8 but are not guaranteed to be; decoding
    with ``surrogateescape`` keeps byte-exact round-tripping for odd names
    instead of raising and aborting the whole extraction.
    """
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="surrogateescape")


def _decode_int(raw: bytes, field: str) -> int:
    text = raw.split(b"\x00", 1)[0].strip().decode("ascii", errors="replace")
    if not text:
        return 0
    try:
        return int(text)
    except ValueError as exc:
        raise CorruptWpressArchive(f"header field {field!r} is not a number: {text!r}") from exc


def parse_header(block: bytes, offset: int) -> WpressEntry | None:
    """Parse one 4377-byte header block. Returns ``None`` at the EOF marker."""
    if len(block) != HEADER_SIZE:
        raise CorruptWpressArchive(
            f"short header block at offset {offset}: got {len(block)} of {HEADER_SIZE} bytes"
        )
    if block == EOF_BLOCK:
        return None

    name = _decode_field(block[NAME_OFFSET:NAME_OFFSET + NAME_SIZE])
    size = _decode_int(block[SIZE_OFFSET:SIZE_OFFSET + SIZE_SIZE], "size")
    mtime = _decode_int(block[MTIME_OFFSET:MTIME_OFFSET + MTIME_SIZE], "mtime")
    prefix = _decode_field(block[PREFIX_OFFSET:PREFIX_OFFSET + PREFIX_SIZE])

    if not name:
        raise CorruptWpressArchive(f"header at offset {offset} has an empty filename")
    if size < 0 or size > _MAX_MEMBER_BYTES:
        raise CorruptWpressArchive(
            f"header at offset {offset} declares an implausible size: {size}"
        )

    return WpressEntry(
        name=name,
        prefix=prefix,
        size=size,
        mtime=mtime,
        offset=offset + HEADER_SIZE,
    )


def build_header(name: str, size: int, mtime: int, prefix: str) -> bytes:
    """Encode a header block. Used by :class:`WpressWriter` and the test suite."""
    name_b = name.encode("utf-8", errors="surrogateescape")
    prefix_b = prefix.encode("utf-8", errors="surrogateescape")
    if len(name_b) > NAME_SIZE:
        raise ValueError(f"filename exceeds {NAME_SIZE} bytes: {name!r}")
    if len(prefix_b) > PREFIX_SIZE:
        raise ValueError(f"prefix exceeds {PREFIX_SIZE} bytes: {prefix!r}")

    size_b = str(int(size)).encode("ascii")
    mtime_b = str(int(mtime)).encode("ascii")
    if len(size_b) > SIZE_SIZE:
        raise ValueError(f"file is too large for the .wpress size field: {size}")
    if len(mtime_b) > MTIME_SIZE:
        raise ValueError(f"mtime does not fit the .wpress mtime field: {mtime}")

    return b"".join((
        name_b.ljust(NAME_SIZE, b"\x00"),
        size_b.ljust(SIZE_SIZE, b"\x00"),
        mtime_b.ljust(MTIME_SIZE, b"\x00"),
        prefix_b.ljust(PREFIX_SIZE, b"\x00"),
    ))


# ---------------------------------------------------------------------------
# Archive reader
# ---------------------------------------------------------------------------
class WpressArchive:
    """Reader over a ``.wpress`` file."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise InvalidWpressArchive(f"not a file: {self.path}")
        self.size = self.path.stat().st_size
        if self.size < HEADER_SIZE:
            raise InvalidWpressArchive(
                f"file is only {self.size} bytes; a .wpress archive is at least {HEADER_SIZE}"
            )

    # -- validation ---------------------------------------------------------
    def looks_valid(self) -> bool:
        """Cheap structural probe: can we read a sane first header?

        This asks only whether the file is *shaped* like a ``.wpress``. It
        deliberately does not require the first payload to fit inside the file,
        because a backup whose download was cut short is still worth extracting
        as far as it goes -- that case is reported as truncation instead. What
        it does check is that the filename field holds something that could
        plausibly be a filename, which is what separates a damaged archive from
        a JPEG that was renamed.
        """
        try:
            with self.path.open("rb") as fh:
                block = fh.read(HEADER_SIZE)
            entry = parse_header(block, 0)
        except (WpressError, OSError):
            return False
        if entry is None:
            return False  # an archive that is immediately EOF carries nothing

        # Random bytes occasionally survive the numeric checks in parse_header;
        # a control character in the name does not occur in a real archive.
        if any(ord(ch) < 32 for ch in entry.name):
            return False
        if any(ord(ch) < 32 for ch in entry.prefix):
            return False
        return True

    def is_truncated(self) -> bool:
        """Whether the first member's payload runs past the end of the file."""
        try:
            with self.path.open("rb") as fh:
                entry = parse_header(fh.read(HEADER_SIZE), 0)
        except (WpressError, OSError):
            return False
        return entry is not None and entry.offset + entry.size > self.size

    # -- enumeration --------------------------------------------------------
    def iter_entries(self, *, tolerate_truncation: bool = True) -> Iterator[WpressEntry]:
        """Walk every member header without reading payloads."""
        with self.path.open("rb") as fh:
            offset = 0
            while True:
                block = fh.read(HEADER_SIZE)
                if len(block) < HEADER_SIZE:
                    if not block:
                        # Clean end without an explicit EOF marker: tolerated,
                        # some writers omit it.
                        return
                    if tolerate_truncation:
                        logger.warning(
                            "wpress: archive ends mid-header at offset %d (%d stray bytes)",
                            offset, len(block),
                        )
                        return
                    raise CorruptWpressArchive(f"archive ends mid-header at offset {offset}")

                entry = parse_header(block, offset)
                if entry is None:
                    return

                yield entry

                next_offset = entry.offset + entry.size
                if next_offset > self.size:
                    if tolerate_truncation:
                        logger.warning(
                            "wpress: member %s runs past end of archive", entry.archive_path
                        )
                        return
                    raise CorruptWpressArchive(
                        f"member {entry.archive_path!r} runs past the end of the archive"
                    )
                fh.seek(next_offset)
                offset = next_offset

    def count_files(self) -> int:
        return sum(1 for _ in self.iter_entries())

    def read_member(self, entry: WpressEntry) -> bytes:
        """Read one member's payload into memory. Use only for small files."""
        with self.path.open("rb") as fh:
            fh.seek(entry.offset)
            data = fh.read(entry.size)
        if len(data) != entry.size:
            raise CorruptWpressArchive(
                f"member {entry.archive_path!r} is truncated: "
                f"expected {entry.size} bytes, read {len(data)}"
            )
        return data

    def find(self, predicate: Callable[[WpressEntry], bool]) -> WpressEntry | None:
        for entry in self.iter_entries():
            if predicate(entry):
                return entry
        return None


# ---------------------------------------------------------------------------
# Extractor abstraction
# ---------------------------------------------------------------------------
class WpressExtractor(abc.ABC):
    """Replaceable ``.wpress`` extraction backend.

    The rest of the pipeline only ever depends on this interface, so a future
    backend (a bundled Go binary, a C extension, a streaming network source)
    can be swapped in without touching the restorer, runner or renderer.
    """

    name: str = "abstract"

    @abc.abstractmethod
    def extract(
        self,
        archive_path: Path,
        destination: Path,
        *,
        progress: ProgressCallback | None = None,
    ) -> ExtractionResult:
        """Extract *archive_path* into *destination*, which is created if absent."""

    @abc.abstractmethod
    def validate(self, archive_path: Path) -> None:
        """Raise :class:`WpressError` if the archive is not usable."""

    def is_available(self) -> bool:
        """Whether this backend can run on the current machine."""
        return True


class PurePythonWpressExtractor(WpressExtractor):
    """Default backend. No external dependencies, identical on Windows/Linux."""

    name = "pure-python"

    def __init__(self, *, chunk_size: int = _COPY_CHUNK, preserve_mtime: bool = True) -> None:
        self.chunk_size = chunk_size
        self.preserve_mtime = preserve_mtime

    def validate(self, archive_path: Path) -> None:
        archive = WpressArchive(archive_path)
        if not archive.looks_valid():
            raise InvalidWpressArchive(
                f"{Path(archive_path).name} does not look like an All-in-One WP Migration "
                ".wpress archive (its first 4377-byte header block did not parse)."
            )

    def extract(
        self,
        archive_path: Path,
        destination: Path,
        *,
        progress: ProgressCallback | None = None,
    ) -> ExtractionResult:
        started = time.monotonic()
        archive = WpressArchive(archive_path)
        self.validate(archive_path)

        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        result = ExtractionResult(destination=destination)

        total = archive.size
        last_report = 0.0

        with archive.path.open("rb") as fh:
            offset = 0
            while True:
                block = fh.read(HEADER_SIZE)
                if len(block) < HEADER_SIZE:
                    if block:
                        result.truncated = True
                        result.add_warning(
                            f"archive ends mid-header after {result.files_written} files; "
                            "the remainder could not be recovered"
                        )
                    break

                try:
                    entry = parse_header(block, offset)
                except CorruptWpressArchive as exc:
                    result.truncated = True
                    result.add_warning(f"stopping early: {exc}")
                    break

                if entry is None:
                    break  # normal EOF marker

                try:
                    target = safe_join(destination, str(entry.safe_path))
                except (PathTraversalError, UnsafeArchivePath) as exc:
                    # Skip the member but keep going: one hostile or malformed
                    # entry should not cost the user the whole backup.
                    result.entries_skipped += 1
                    result.add_warning(
                        f"skipped unsafe archive member {entry.archive_path!r}: {exc}"
                    )
                    offset = entry.offset + entry.size
                    fh.seek(offset)
                    continue

                if self._write_member(fh, entry, target, result) is None:
                    break  # truncated payload; _write_member recorded the warning

                offset = entry.offset + entry.size
                fh.seek(offset)

                if progress is not None:
                    now = time.monotonic()
                    if now - last_report > 0.2 or offset >= total:
                        last_report = now
                        progress(min(offset, total), total, str(entry.safe_path))

        result.duration_seconds = time.monotonic() - started
        if result.files_written == 0:
            detail = (
                " Its first entry claims more data than the file contains, so the "
                "backup is most likely an incomplete download."
                if result.truncated else ""
            )
            raise CorruptWpressArchive(
                f"{Path(archive_path).name} contained no extractable files.{detail}"
            )
        logger.info(
            "wpress: extracted %d files (%.1f MiB) in %.1fs",
            result.files_written, result.bytes_written / 1048576, result.duration_seconds,
        )
        return result

    # -- internals ----------------------------------------------------------
    def _write_member(self, fh, entry: WpressEntry, target: Path, result: ExtractionResult):
        """Stream one payload to disk. Returns ``None`` when the archive ended early."""
        target.parent.mkdir(parents=True, exist_ok=True)
        remaining = entry.size
        partial = False

        try:
            with target.open("wb") as out:
                while remaining > 0:
                    chunk = fh.read(min(self.chunk_size, remaining))
                    if not chunk:
                        result.truncated = True
                        result.add_warning(
                            f"member {entry.archive_path!r} is truncated: "
                            f"{remaining} of {entry.size} bytes missing"
                        )
                        partial = True
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
        except OSError as exc:
            # A path the OS still rejects (too long, bad codepoint) costs us one
            # file, not the job.
            result.entries_skipped += 1
            result.add_warning(f"could not write {entry.archive_path!r}: {exc}")
            fh.seek(entry.offset + entry.size)
            return True

        if partial:
            # A half-written file is worse than none: it looks complete to the
            # validator and to anyone who deploys the export. Remove it and
            # report the archive as truncated.
            try:
                target.unlink()
            except OSError:
                pass
            return None

        if self.preserve_mtime and entry.mtime > 0:
            try:
                os.utime(target, (entry.mtime, entry.mtime))
            except OSError:
                pass  # cosmetic only

        result.files_written += 1
        result.bytes_written += entry.size
        return True


class BinaryWpressExtractor(WpressExtractor):
    """Adapter around the prebuilt ``fifthsegment/Wpress-Extractor`` binary.

    Provided so the reference implementation can be used as a cross-check or a
    drop-in replacement. It is **not** the default because that binary always
    extracts into the process working directory and performs no path-traversal
    validation, so this adapter runs it inside the destination directory and
    re-validates the resulting tree.
    """

    name = "binary"

    def __init__(self, binary_path: str | os.PathLike[str] | None = None) -> None:
        self.binary_path = Path(binary_path) if binary_path else self._discover()

    @staticmethod
    def _discover() -> Path | None:
        for candidate in ("wpress-extractor", "wpress-extractor.exe", "wpress_extractor"):
            found = shutil.which(candidate)
            if found:
                return Path(found)
        bundled = Path(__file__).resolve().parents[2] / "extractor"
        for candidate in ("wpress-extractor.exe", "wpress_extractor"):
            if (bundled / candidate).is_file():
                return bundled / candidate
        return None

    def is_available(self) -> bool:
        return self.binary_path is not None and Path(self.binary_path).is_file()

    def validate(self, archive_path: Path) -> None:
        PurePythonWpressExtractor().validate(archive_path)

    def extract(
        self,
        archive_path: Path,
        destination: Path,
        *,
        progress: ProgressCallback | None = None,
    ) -> ExtractionResult:
        if not self.is_available():
            raise WpressError(
                "the external wpress-extractor binary was not found; "
                "install it on PATH or drop it in the extractor/ directory"
            )
        started = time.monotonic()
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        result = ExtractionResult(destination=destination)

        # The binary takes exactly one positional argument and writes to CWD.
        # No shell is used, so the archive name cannot inject a command.
        proc = subprocess.run(
            [str(self.binary_path), str(Path(archive_path).resolve())],
            cwd=str(destination),
            capture_output=True,
            text=True,
            timeout=60 * 60,
            shell=False,
        )
        if proc.returncode != 0:
            raise WpressError(
                f"wpress-extractor exited with {proc.returncode}: {proc.stderr.strip()[:500]}"
            )

        # Re-validate containment: the binary does not do this itself.
        from app.utils.security import is_within

        for path in destination.rglob("*"):
            if path.is_file():
                if not is_within(destination, path):
                    result.add_warning(f"external extractor wrote outside the workspace: {path}")
                    continue
                result.files_written += 1
                result.bytes_written += path.stat().st_size

        result.duration_seconds = time.monotonic() - started
        if result.files_written == 0:
            raise CorruptWpressArchive("the external extractor produced no files")
        return result


# ---------------------------------------------------------------------------
# Writer (fixtures, round-trip tests, re-packing)
# ---------------------------------------------------------------------------
class WpressWriter:
    """Create a ``.wpress`` archive. Used to build deterministic test fixtures."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._fh = None

    def __enter__(self) -> "WpressWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("wb")
        return self

    def __exit__(self, *exc_info) -> None:
        if self._fh is not None:
            self._fh.write(EOF_BLOCK)
            self._fh.close()
            self._fh = None

    def add_bytes(self, archive_path: str, data: bytes, mtime: int | None = None) -> None:
        """Add *data* at the archive-relative ``dir/name`` path."""
        if self._fh is None:
            raise RuntimeError("use WpressWriter as a context manager")
        posix = PurePosixPath(archive_path.replace("\\", "/"))
        name = posix.name
        prefix = str(posix.parent) if str(posix.parent) != "." else "."
        self._fh.write(build_header(name, len(data), mtime or int(time.time()), prefix))
        self._fh.write(data)

    def add_file(self, source: Path, archive_path: str) -> None:
        source = Path(source)
        self.add_bytes(archive_path, source.read_bytes(), int(source.stat().st_mtime))

    def add_tree(self, root: Path, archive_prefix: str = ".") -> int:
        """Recursively add every file under *root*. Returns the file count."""
        root = Path(root)
        count = 0
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            target = rel if archive_prefix in {".", ""} else f"{archive_prefix}/{rel}"
            self.add_file(path, target)
            count += 1
        return count


def get_extractor(backend: str = "auto") -> WpressExtractor:
    """Factory used by the pipeline. ``auto`` prefers the dependency-free backend."""
    backend = (backend or "auto").lower()
    if backend in {"auto", "python", "pure-python"}:
        return PurePythonWpressExtractor()
    if backend == "binary":
        return BinaryWpressExtractor()
    raise ValueError(f"unknown wpress extraction backend: {backend!r}")
