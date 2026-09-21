"""Locate or provision the PHP and MySQL runtimes the pipeline needs.

Rendering a WordPress site faithfully means *running* WordPress, which means
PHP and a MySQL-compatible database. Requiring the user to install and
configure Apache/PHP/MySQL by hand would defeat the point of the tool, so this
module resolves them in three escalating steps:

1. **Detect.** Look for a usable PHP and mysqld already on the machine: on
   ``PATH`` first, then in the standard XAMPP / Laragon / WAMP / MAMP /
   Chocolatey / Scoop locations, then in our own runtime cache.
2. **Provision.** If nothing is found and provisioning is enabled, download the
   official portable Windows builds (PHP from windows.php.net, MariaDB from
   archive.mariadb.org) into a shared cache directory and unpack them. No
   installer runs, nothing is put on ``PATH``, and nothing outside the cache
   directory is touched.
3. **Explain.** If provisioning is off or fails, raise a
   :class:`RuntimeUnavailable` carrying concrete, copy-pasteable setup
   instructions rather than a stack trace.

No web server is needed: PHP's built-in server (``php -S``) serves WordPress
perfectly well for a local, single-purpose crawl, and it removes Apache/Nginx
from the dependency list entirely.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

PHP_RELEASES_URL = "https://windows.php.net/downloads/releases/releases.json"
PHP_ARCHIVE_BASE = "https://windows.php.net/downloads/releases"
PHP_ARCHIVE_FALLBACK = "https://windows.php.net/downloads/releases/archives"
MARIADB_URL_TEMPLATE = (
    "https://archive.mariadb.org/mariadb-{version}/winx64-packages/mariadb-{version}-winx64.zip"
)

_USER_AGENT = "wp-static-converter/1.0 (+local tool)"

#: PHP extensions WordPress needs for a faithful render. ``mysqli`` is
#: non-negotiable; the image extensions matter because a missing GD makes
#: WordPress skip image sizes and changes the rendered markup.
REQUIRED_PHP_EXTENSIONS = ("mysqli", "mbstring", "gd", "curl", "openssl", "zip", "exif", "fileinfo")

ProgressCallback = Callable[[str, float], None]
"""``(message, fraction_0_to_1)``."""


class RuntimeUnavailable(RuntimeError):
    """A required runtime is missing and could not be provisioned.

    Carries user-facing setup instructions in :attr:`instructions`.
    """

    def __init__(self, message: str, instructions: str = "") -> None:
        super().__init__(message)
        self.instructions = instructions


@dataclass(slots=True)
class PhpRuntime:
    """A usable PHP CLI, plus what we know about it."""

    binary: Path
    version: str
    extensions: frozenset[str]
    ini_path: Path | None
    source: str
    """``path``, ``xampp``, ``laragon``, ``provisioned``..."""

    @property
    def missing_extensions(self) -> list[str]:
        return [e for e in REQUIRED_PHP_EXTENSIONS if e not in self.extensions]

    @property
    def extension_dir(self) -> Path | None:
        candidate = self.binary.parent / "ext"
        return candidate if candidate.is_dir() else None


@dataclass(slots=True)
class MysqlRuntime:
    """A usable MySQL/MariaDB server binary and its companion tools."""

    server_binary: Path
    version: str
    source: str
    install_db_binary: Path | None = None
    """``mariadb-install-db.exe`` / ``mysql_install_db``, when the distribution
    provides one. Absent on MySQL for Windows, which uses ``--initialize``."""
    client_binary: Path | None = None
    admin_binary: Path | None = None
    flavour: str = "mariadb"
    """``mariadb`` or ``mysql``; they differ in how a data directory is made."""

    @property
    def base_dir(self) -> Path:
        return self.server_binary.parent.parent


@dataclass(slots=True)
class RuntimeSet:
    php: PhpRuntime
    mysql: MysqlRuntime


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
_WINDOWS_PHP_HINTS = (
    r"C:\xampp\php",
    r"C:\laragon\bin\php",
    r"C:\wamp64\bin\php",
    r"C:\wamp\bin\php",
    r"C:\MAMP\bin\php",
    r"C:\tools\php",
    r"C:\php",
    r"C:\Program Files\php",
    r"C:\ProgramData\chocolatey\lib\php\tools",
)

_WINDOWS_MYSQL_HINTS = (
    r"C:\xampp\mysql",
    r"C:\laragon\bin\mysql",
    r"C:\wamp64\bin\mysql",
    r"C:\wamp\bin\mysql",
    r"C:\MAMP\bin\mysql",
    r"C:\Program Files\MariaDB",
    r"C:\Program Files\MySQL",
)

_POSIX_PHP_HINTS = ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/opt/lampp/bin")
_POSIX_MYSQL_HINTS = ("/usr/sbin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/opt/lampp/bin")

_IS_WINDOWS = platform.system() == "Windows"
_EXE = ".exe" if _IS_WINDOWS else ""


def _run(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    """Run a probe command with no shell, so no argument can inject one.

    Probes run from the system temp directory rather than the project root, so
    a runtime that only works because of a relative path in its configuration
    fails here instead of much later inside a job.
    """
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, shell=False,
        cwd=tempfile.gettempdir(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0,
    )


def _expand_glob_dirs(patterns: Iterable[str]) -> list[Path]:
    """Expand hint directories, including versioned subdirectories."""
    found: list[Path] = []
    for pattern in patterns:
        base = Path(pattern)
        if base.is_dir():
            found.append(base)
        parent = base.parent
        if parent.is_dir():
            # Handles C:\laragon\bin\php\php-8.2.12-Win32-vs16-x64 and
            # C:\Program Files\MariaDB\MariaDB 11.4
            try:
                for child in sorted(parent.iterdir(), reverse=True):
                    if child.is_dir() and child.name.lower().startswith(base.name.lower()):
                        found.append(child)
            except OSError:
                pass
        if base.is_dir():
            try:
                for child in sorted(base.iterdir(), reverse=True):
                    if child.is_dir():
                        found.append(child)
            except OSError:
                pass
    return found


def probe_php(binary: Path, source: str = "unknown") -> PhpRuntime | None:
    """Return a :class:`PhpRuntime` if *binary* is a usable PHP CLI."""
    binary = Path(binary)
    if not binary.is_file():
        return None
    try:
        version_proc = _run([str(binary), "-v"])
        if version_proc.returncode != 0:
            return None
        match = re.search(r"PHP (\d+\.\d+\.\d+)", version_proc.stdout)
        if not match:
            return None
        version = match.group(1)

        major, minor = (int(p) for p in version.split(".")[:2])
        if (major, minor) < (7, 4):
            logger.debug("php at %s is too old (%s)", binary, version)
            return None

        mod_proc = _run([str(binary), "-m"])
        extensions = frozenset(
            line.strip().lower()
            for line in mod_proc.stdout.splitlines()
            if line.strip() and not line.startswith("[")
        )

        # ``php -i`` prints "Loaded Configuration File => path" while ``php --ini``
        # uses a colon; accept either separator.
        ini_match = re.search(
            r"Loaded Configuration File\s*(?:=>|:)\s*(.+)", _run([str(binary), "-i"]).stdout
        )
        ini_path = None
        if ini_match:
            candidate = ini_match.group(1).strip()
            if candidate and candidate != "(none)":
                ini_path = Path(candidate)

        return PhpRuntime(binary, version, extensions, ini_path, source)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("php probe failed for %s: %s", binary, exc)
        return None


def probe_mysql(server_binary: Path, source: str = "unknown") -> MysqlRuntime | None:
    """Return a :class:`MysqlRuntime` if *server_binary* is a usable server."""
    server_binary = Path(server_binary)
    if not server_binary.is_file():
        return None
    try:
        proc = _run([str(server_binary), "--version"])
        if proc.returncode != 0:
            return None
        text = proc.stdout + proc.stderr
        match = re.search(r"Ver\s+(\d+\.\d+\.\d+)", text)
        version = match.group(1) if match else "unknown"
        flavour = "mariadb" if "mariadb" in text.lower() else "mysql"

        bindir = server_binary.parent

        def first(*names: str) -> Path | None:
            for name in names:
                candidate = bindir / f"{name}{_EXE}"
                if candidate.is_file():
                    return candidate
            return None

        return MysqlRuntime(
            server_binary=server_binary,
            version=version,
            source=source,
            flavour=flavour,
            install_db_binary=first("mariadb-install-db", "mysql_install_db"),
            client_binary=first("mariadb", "mysql"),
            admin_binary=first("mariadb-admin", "mysqladmin"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("mysql probe failed for %s: %s", server_binary, exc)
        return None


def find_php(explicit: str | None = None, cache_dir: Path | None = None) -> PhpRuntime | None:
    """Search for a usable PHP CLI, best candidate first."""
    if explicit:
        runtime = probe_php(Path(explicit), source="configured")
        if runtime:
            return runtime
        logger.warning("configured PHP binary is not usable: %s", explicit)

    # Our own provisioned copy wins over system installs: we know its
    # extensions and php.ini are configured for WordPress.
    if cache_dir:
        for candidate in sorted((cache_dir / "php").glob("*/php" + _EXE), reverse=True):
            runtime = probe_php(candidate, source="provisioned")
            if runtime:
                return runtime

    on_path = shutil.which("php")
    if on_path:
        runtime = probe_php(Path(on_path), source="path")
        if runtime and not runtime.missing_extensions:
            return runtime
        best_effort = runtime
    else:
        best_effort = None

    hints = _WINDOWS_PHP_HINTS if _IS_WINDOWS else _POSIX_PHP_HINTS
    for directory in _expand_glob_dirs(hints):
        candidate = directory / f"php{_EXE}"
        runtime = probe_php(candidate, source=_source_label(directory))
        if runtime and not runtime.missing_extensions:
            return runtime
        best_effort = best_effort or runtime

    # A PHP missing an optional extension still beats no PHP at all.
    return best_effort


def find_mysql(explicit: str | None = None, cache_dir: Path | None = None) -> MysqlRuntime | None:
    """Search for a usable MySQL/MariaDB server binary."""
    if explicit:
        runtime = probe_mysql(Path(explicit), source="configured")
        if runtime:
            return runtime
        logger.warning("configured mysqld binary is not usable: %s", explicit)

    if cache_dir:
        # The vendor zip nests as mariadb/<version>/mariadb-<version>-winx64/bin,
        # so match both that shape and a flattened one.
        for pattern in (
            f"mariadb/*/bin/mysqld{_EXE}",
            f"mariadb/*/*/bin/mysqld{_EXE}",
            f"mariadb/*/bin/mariadbd{_EXE}",
            f"mariadb/*/*/bin/mariadbd{_EXE}",
        ):
            for candidate in sorted(cache_dir.glob(pattern), reverse=True):
                runtime = probe_mysql(candidate, source="provisioned")
                if runtime:
                    return runtime

    for name in ("mysqld", "mariadbd"):
        on_path = shutil.which(name)
        if on_path:
            runtime = probe_mysql(Path(on_path), source="path")
            if runtime:
                return runtime

    hints = _WINDOWS_MYSQL_HINTS if _IS_WINDOWS else _POSIX_MYSQL_HINTS
    for directory in _expand_glob_dirs(hints):
        for relative in (f"bin/mysqld{_EXE}", f"bin/mariadbd{_EXE}", f"mysqld{_EXE}", f"mariadbd{_EXE}"):
            runtime = probe_mysql(directory / relative, source=_source_label(directory))
            if runtime:
                return runtime
    return None


def _source_label(directory: Path) -> str:
    text = str(directory).lower()
    for marker in ("xampp", "laragon", "wamp", "mamp", "chocolatey", "scoop", "homebrew"):
        if marker in text:
            return marker
    return "system"


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------
def _download(url: str, destination: Path, progress: ProgressCallback | None = None,
              label: str = "download") -> Path:
    """Stream *url* to *destination*, resuming is not attempted but retries are."""
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
                        if progress and (now - last_tick > 0.3):
                            last_tick = now
                            fraction = (done / total) if total else 0.0
                            progress(f"{label}: {done // 1048576} MB", fraction)
            temporary.replace(destination)
            return destination
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
            logger.warning("%s attempt %d/3 failed: %s", label, attempt, exc)
            if temporary.exists():
                temporary.unlink(missing_ok=True)
            time.sleep(2 * attempt)

    raise RuntimeUnavailable(
        f"could not download {label} from {url}: {last_error}",
        instructions=(
            "The machine could not reach the download server. Either connect it to the "
            "internet, or install PHP and MariaDB manually and point the tool at them "
            "with WPSC_PHP_BINARY / WPSC_MYSQLD_BINARY in your .env file."
        ),
    )


def _safe_extract_zip(zip_path: Path, destination: Path) -> None:
    """Unpack a zip, refusing any member that would escape *destination*.

    These archives come from trusted vendors, but the extraction code is shared
    with untrusted input handling and a zip-slip check costs nothing.
    """
    from app.utils.security import is_within

    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = destination / member.filename
            if not is_within(destination, target):
                raise RuntimeUnavailable(
                    f"refusing to unpack {member.filename!r}: it escapes the runtime cache"
                )
        archive.extractall(destination)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------
def provision_php(cache_dir: Path, version: str = "8.2",
                  progress: ProgressCallback | None = None) -> PhpRuntime:
    """Download and unpack a portable PHP for Windows into *cache_dir*."""
    if not _IS_WINDOWS:
        raise RuntimeUnavailable(
            "automatic PHP provisioning is implemented for Windows only",
            instructions=(
                "Install PHP with your package manager, for example:\n"
                "  sudo apt install php-cli php-mysqli php-gd php-curl php-mbstring php-zip\n"
                "  brew install php"
            ),
        )

    if progress:
        progress("Looking up PHP releases", 0.0)

    try:
        request = urllib.request.Request(PHP_RELEASES_URL, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            releases = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeUnavailable(
            f"could not read the PHP release index: {exc}",
            instructions=_php_manual_instructions(),
        ) from exc

    branch = releases.get(version)
    if branch is None:
        # Fall back to the newest branch that still supports WordPress well.
        for candidate in ("8.2", "8.1", "8.3", "8.0"):
            if candidate in releases:
                branch, version = releases[candidate], candidate
                break
    if branch is None:
        raise RuntimeUnavailable(
            f"no PHP {version} build is published for Windows",
            instructions=_php_manual_instructions(),
        )

    # Non-thread-safe is the right build for the CLI and its built-in server.
    zip_name = None
    for key, value in branch.items():
        if key.startswith("nts-") and key.endswith("-x64") and isinstance(value, dict):
            zip_name = value.get("zip", {}).get("path")
            if zip_name:
                break
    if not zip_name:
        raise RuntimeUnavailable(
            f"the PHP {version} release carries no 64-bit non-thread-safe zip",
            instructions=_php_manual_instructions(),
        )

    full_version = branch.get("version", version)
    target_dir = cache_dir / "php" / full_version
    binary = target_dir / f"php{_EXE}"

    if not binary.is_file():
        archive_path = cache_dir / "downloads" / zip_name
        if not archive_path.is_file():
            _download(f"{PHP_ARCHIVE_BASE}/{zip_name}", archive_path, progress, f"PHP {full_version}")
        if progress:
            progress(f"Unpacking PHP {full_version}", 0.9)
        _safe_extract_zip(archive_path, target_dir)

    _write_php_ini(target_dir)

    runtime = probe_php(binary, source="provisioned")
    if runtime is None:
        raise RuntimeUnavailable(
            f"the downloaded PHP at {binary} would not run",
            instructions=(
                "PHP for Windows needs the Microsoft Visual C++ Redistributable.\n"
                "Install it from https://aka.ms/vs/17/release/vc_redist.x64.exe and retry."
            ),
        )
    if progress:
        progress(f"PHP {runtime.version} ready", 1.0)
    return runtime


def _write_php_ini(php_dir: Path) -> None:
    """Write a php.ini tuned for running WordPress under the built-in server.

    The shipped ``php.ini-development`` leaves every extension commented out,
    so without this WordPress cannot even connect to its database.
    """
    # Absolute paths throughout: PHP resolves a relative extension_dir against
    # the current working directory, and the pipeline runs PHP from the
    # WordPress root, not from here.
    php_dir = Path(php_dir).resolve()
    ini_path = php_dir / "php.ini"
    ext_dir = php_dir / "ext"

    extensions = [
        "mysqli", "mbstring", "gd", "curl", "openssl", "zip",
        "exif", "fileinfo", "intl", "sodium", "pdo_mysql",
    ]
    available = []
    for name in extensions:
        if not ext_dir.is_dir() or (ext_dir / f"php_{name}.dll").is_file():
            available.append(name)

    lines = [
        "; Generated by wp-static-converter for local WordPress rendering.",
        "; This file configures the tool's private PHP copy only.",
        f'extension_dir = "{ext_dir}"',
        "",
        *[f"extension={name}" for name in available],
        "",
        "; WordPress imports and renders large pages; be generous.",
        "memory_limit = 512M",
        "max_execution_time = 300",
        "post_max_size = 256M",
        "upload_max_filesize = 256M",
        "max_input_vars = 5000",
        "default_socket_timeout = 120",
        "",
        "; Surface problems in the log, never in the rendered HTML: a PHP notice",
        "; printed into the page would be captured into the static output.",
        "display_errors = Off",
        "display_startup_errors = Off",
        "log_errors = On",
        "error_reporting = E_ALL & ~E_DEPRECATED & ~E_NOTICE & ~E_WARNING",
        "",
        "date.timezone = UTC",
        "cgi.fix_pathinfo = 1",
        "opcache.enable = 0",
    ]
    ini_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def provision_mariadb(cache_dir: Path, version: str = "11.4.4",
                      progress: ProgressCallback | None = None) -> MysqlRuntime:
    """Download and unpack a portable MariaDB for Windows into *cache_dir*."""
    if not _IS_WINDOWS:
        raise RuntimeUnavailable(
            "automatic MariaDB provisioning is implemented for Windows only",
            instructions=(
                "Install MariaDB or MySQL with your package manager, for example:\n"
                "  sudo apt install mariadb-server\n"
                "  brew install mariadb"
            ),
        )

    target_dir = cache_dir / "mariadb" / version
    server = _find_server_in(target_dir)

    if server is None:
        zip_name = f"mariadb-{version}-winx64.zip"
        archive_path = cache_dir / "downloads" / zip_name
        if not archive_path.is_file():
            _download(
                MARIADB_URL_TEMPLATE.format(version=version),
                archive_path, progress, f"MariaDB {version}",
            )
        if progress:
            progress(f"Unpacking MariaDB {version}", 0.9)
        _safe_extract_zip(archive_path, target_dir)
        server = _find_server_in(target_dir)

    if server is None:
        raise RuntimeUnavailable(
            f"the MariaDB download did not contain a server binary under {target_dir}",
            instructions=_mysql_manual_instructions(),
        )

    runtime = probe_mysql(server, source="provisioned")
    if runtime is None:
        raise RuntimeUnavailable(
            f"the downloaded MariaDB at {server} would not run",
            instructions=_mysql_manual_instructions(),
        )
    if progress:
        progress(f"MariaDB {runtime.version} ready", 1.0)
    return runtime


def _find_server_in(root: Path) -> Path | None:
    """The MariaDB zip nests everything under mariadb-<version>-winx64/."""
    if not root.is_dir():
        return None
    for pattern in (f"bin/mysqld{_EXE}", f"*/bin/mysqld{_EXE}",
                    f"bin/mariadbd{_EXE}", f"*/bin/mariadbd{_EXE}"):
        for candidate in sorted(root.glob(pattern)):
            if candidate.is_file():
                return candidate
    return None


def _php_manual_instructions() -> str:
    return (
        "Install PHP 8.1+ manually:\n"
        "  1. Download the Windows x64 'Non Thread Safe' zip from "
        "https://windows.php.net/download/\n"
        "  2. Unpack it to C:\\php\n"
        "  3. Copy php.ini-development to php.ini and enable the extensions "
        "mysqli, mbstring, gd, curl, openssl, zip, exif and fileinfo\n"
        "  4. Either add C:\\php to PATH, or set WPSC_PHP_BINARY=C:\\php\\php.exe in .env\n"
        "Installing XAMPP or Laragon also works: this tool detects both automatically."
    )


def _mysql_manual_instructions() -> str:
    return (
        "Install MariaDB or MySQL manually:\n"
        "  1. Download the Windows x64 zip from https://mariadb.org/download/\n"
        "  2. Unpack it, for example to C:\\mariadb\n"
        "  3. Set WPSC_MYSQLD_BINARY=C:\\mariadb\\bin\\mysqld.exe in .env\n"
        "Installing XAMPP or Laragon also works: this tool detects both automatically."
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def ensure_runtimes(
    cache_dir: Path,
    *,
    php_version: str = "8.2",
    mariadb_version: str = "11.4.4",
    auto_provision: bool = True,
    php_override: str | None = None,
    mysqld_override: str | None = None,
    progress: ProgressCallback | None = None,
) -> RuntimeSet:
    """Return a PHP and a MySQL runtime, provisioning them if allowed."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    php = find_php(php_override, cache_dir)
    if php is not None and php.missing_extensions:
        logger.warning(
            "PHP at %s is missing extensions %s", php.binary, ", ".join(php.missing_extensions)
        )
        # mysqli is fatal; the rest only degrade fidelity.
        if "mysqli" in php.missing_extensions:
            logger.info("ignoring PHP at %s: no mysqli", php.binary)
            php = None

    if php is None:
        if not auto_provision:
            raise RuntimeUnavailable(
                "no usable PHP installation was found",
                instructions=_php_manual_instructions(),
            )
        php = provision_php(cache_dir, php_version, progress)

    mysql = find_mysql(mysqld_override, cache_dir)
    if mysql is None:
        if not auto_provision:
            raise RuntimeUnavailable(
                "no usable MySQL/MariaDB installation was found",
                instructions=_mysql_manual_instructions(),
            )
        mysql = provision_mariadb(cache_dir, mariadb_version, progress)

    logger.info(
        "runtimes ready: PHP %s (%s) at %s; %s %s (%s) at %s",
        php.version, php.source, php.binary,
        mysql.flavour, mysql.version, mysql.source, mysql.server_binary,
    )
    return RuntimeSet(php=php, mysql=mysql)


def find_chromium() -> tuple[bool, str | None, bool]:
    """Locate Playwright's Chromium without starting Playwright.

    Returns ``(playwright_installed, executable_path, usable)``.

    The obvious implementation -- ``with sync_playwright() as p:
    p.chromium.executable_path`` -- throws when it is called from a thread that
    already has a running asyncio event loop, which is exactly the situation
    inside a FastAPI request handler. Doing that made the health endpoint
    report Chromium as missing on a machine where it was installed and
    working. Inspecting the browser cache directly has no such constraint and
    is faster besides.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, None, False

    roots: list[Path] = []
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override not in {"0"}:
        roots.append(Path(override))
    elif override == "0":
        # Browsers were installed next to the package itself.
        import playwright as _playwright

        roots.append(Path(_playwright.__file__).parent / ".local-browsers")

    if _IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "ms-playwright")
    elif platform.system() == "Darwin":
        roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    else:
        roots.append(Path.home() / ".cache" / "ms-playwright")

    relative = (
        "chrome-win64/chrome.exe" if _IS_WINDOWS
        else "chrome-mac/Chromium.app/Contents/MacOS/Chromium" if platform.system() == "Darwin"
        else "chrome-linux/chrome"
    )

    for root in roots:
        if not root.is_dir():
            continue
        # Newest build wins; both "chromium-1234" and "chromium_headless_shell-*"
        # satisfy a headless launch.
        for pattern in ("chromium-*", "chromium_headless_shell-*"):
            for build in sorted(root.glob(pattern), reverse=True):
                candidate = build / relative
                if candidate.is_file():
                    return True, str(candidate), True
                # Layouts vary between versions; fall back to a shallow search.
                for found in build.rglob("chrome.exe" if _IS_WINDOWS else "chrome"):
                    if found.is_file():
                        return True, str(found), True

    return True, None, False


def diagnose(cache_dir: Path, php_override: str | None = None,
             mysqld_override: str | None = None) -> dict[str, object]:
    """Report on every dependency, for ``scripts/doctor.py`` and ``/api/health``."""
    php = find_php(php_override, cache_dir)
    mysql = find_mysql(mysqld_override, cache_dir)

    playwright_installed, chromium_path, chromium_ok = find_chromium()

    return {
        "platform": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "php": {
            "found": php is not None,
            "version": php.version if php else None,
            "binary": str(php.binary) if php else None,
            "source": php.source if php else None,
            "missing_extensions": php.missing_extensions if php else REQUIRED_PHP_EXTENSIONS,
            "instructions": None if php else _php_manual_instructions(),
        },
        "mysql": {
            "found": mysql is not None,
            "flavour": mysql.flavour if mysql else None,
            "version": mysql.version if mysql else None,
            "binary": str(mysql.server_binary) if mysql else None,
            "source": mysql.source if mysql else None,
            "instructions": None if mysql else _mysql_manual_instructions(),
        },
        "playwright": {
            "installed": playwright_installed,
            "chromium": chromium_path,
            "chromium_ready": chromium_ok,
            "instructions": None if chromium_ok else "Run: python -m playwright install chromium",
        },
        "wpress_extractor": {
            "backend": "pure-python (built in, no external binary required)",
            "ready": True,
        },
    }
