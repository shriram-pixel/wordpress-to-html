"""Filesystem helpers: job workspaces, atomic writes, sizing, safe deletion."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Directory layout created for every job. Keeping the names in one place lets
#: the worker, API and ZIP builder agree without passing paths around.
JOB_SUBDIRS = (
    "input",
    "extracted",
    "wordpress",
    "database",
    "runtime",
    "output",
    "screenshots",
    "logs",
    "report",
)


@dataclass(frozen=True, slots=True)
class JobWorkspace:
    """The isolated directory tree belonging to one conversion."""

    root: Path

    @property
    def input(self) -> Path: return self.root / "input"
    @property
    def extracted(self) -> Path: return self.root / "extracted"
    @property
    def wordpress(self) -> Path: return self.root / "wordpress"
    @property
    def database(self) -> Path: return self.root / "database"
    @property
    def runtime(self) -> Path: return self.root / "runtime"
    @property
    def output(self) -> Path: return self.root / "output"
    @property
    def screenshots(self) -> Path: return self.root / "screenshots"
    @property
    def logs(self) -> Path: return self.root / "logs"
    @property
    def report(self) -> Path: return self.root / "report"

    @property
    def log_file(self) -> Path:
        return self.logs / f"job-{self.root.name}.log"

    @property
    def zip_path(self) -> Path:
        return self.root / "website-static.zip"

    @classmethod
    def create(cls, root: Path) -> "JobWorkspace":
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        for name in JOB_SUBDIRS:
            (root / name).mkdir(exist_ok=True)
        return cls(root)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write *data* to *path* via a temporary file and a rename.

    Used for generated HTML and downloaded assets so a crash mid-write cannot
    leave a half-written file that later looks complete to the validator.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        _silent_unlink(Path(tmp_name))
        raise


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding, errors="surrogatepass"))


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def directory_size(path: Path) -> int:
    """Total bytes of every regular file under *path*."""
    total = 0
    for entry in iter_files(path):
        try:
            total += entry.stat().st_size
        except OSError:
            continue
    return total


def iter_files(path: Path) -> Iterator[Path]:
    """Yield every regular file under *path*, skipping unreadable subtrees."""
    root = Path(path)
    if not root.exists():
        return
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda e: None):
        base = Path(dirpath)
        for name in filenames:
            candidate = base / name
            if candidate.is_file():
                yield candidate


def count_files(path: Path) -> int:
    return sum(1 for _ in iter_files(path))


def human_bytes(size: float) -> str:
    """Format a byte count the way the report and UI display it."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def file_digest(path: Path, algorithm: str = "sha256", chunk: int = 1 << 20) -> str:
    digest = hashlib.new(algorithm)
    with Path(path).open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _on_rm_error(func, path, _exc_info) -> None:
    """Make read-only files (common in extracted WordPress trees) deletable."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            logger.debug("could not remove %s: %s", path, exc)


def remove_tree(path: Path, *, missing_ok: bool = True) -> None:
    """Delete a directory tree, tolerating Windows read-only attributes."""
    path = Path(path)
    if not path.exists():
        if missing_ok:
            return
        raise FileNotFoundError(path)
    shutil.rmtree(path, onexc=_on_rm_error)


def ensure_free_space(path: Path, required_bytes: int) -> None:
    """Raise when *path*'s volume cannot hold *required_bytes*.

    A conversion writes roughly the archive size three times over (extracted
    tree, rendered output, ZIP), so running out of disk half-way is a real and
    very confusing failure mode. Fail early with a clear number instead.
    """
    path = Path(path)
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    if usage.free < required_bytes:
        raise OSError(
            f"not enough free disk space on {probe}: "
            f"{human_bytes(usage.free)} available, {human_bytes(required_bytes)} needed"
        )


def unique_path(path: Path) -> Path:
    """Return *path*, or ``name-2.ext``/``name-3.ext`` if it already exists."""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for index in range(2, 10_000):
        candidate = parent / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not find a free filename near {path}")


def sanitise_upload_name(filename: str) -> str:
    """Reduce an uploaded filename to a safe basename.

    Browsers can submit a full path, and the name is attacker-controlled, so
    only the final component is kept and then restricted to a safe alphabet.
    """
    from app.utils.security import sanitise_component

    base = os.path.basename(filename.replace("\\", "/")).strip()
    base = sanitise_component(base or "upload.wpress")
    if not base.lower().endswith(".wpress"):
        base = f"{Path(base).stem or 'upload'}.wpress"
    return base
