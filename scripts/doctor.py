"""Report on every dependency the converter needs, and how to fix what is missing.

    python scripts/doctor.py
    python scripts/doctor.py --json

Exit code 0 when the machine is ready to convert, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.services.runtime_provisioner import diagnose  # noqa: E402
from app.utils.filesystem import human_bytes  # noqa: E402

GREEN, YELLOW, RED, DIM, BOLD, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[90m", "\033[1m", "\033[0m"
)


def _supports_colour() -> bool:
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        try:
            import colorama  # noqa: F401

            return True
        except ImportError:
            # Windows 10 build 14393+ understands ANSI once it is enabled.
            try:
                import ctypes

                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
                return True
            except Exception:
                return False
    return True


COLOUR = _supports_colour()


def paint(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if COLOUR else text


def line(ok: bool | None, label: str, detail: str = "") -> None:
    if ok is True:
        mark, colour = "OK  ", GREEN
    elif ok is False:
        mark, colour = "MISS", RED
    else:
        mark, colour = "??  ", YELLOW
    print(f"  {paint(mark, colour)} {label:<30} {paint(detail, DIM) if detail else ''}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    settings = get_settings()
    report = diagnose(settings.runtime_dir, settings.php_binary, settings.mysqld_binary)

    ready = bool(
        report["php"]["found"]
        and report["mysql"]["found"]
        and report["playwright"]["chromium_ready"]
    )
    report["ready"] = ready

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0 if ready else 1

    print()
    print(paint("  Dependency check", BOLD))
    print(f"  {paint('-' * 63, DIM)}")
    print(f"  {paint('Platform', DIM)}  {report['platform']}  ·  Python {report['python']}")
    print()

    # -- Python packages ----------------------------------------------------
    for module, label in (
        ("fastapi", "FastAPI"), ("playwright", "Playwright"), ("bs4", "BeautifulSoup"),
        ("httpx", "httpx"), ("PIL", "Pillow"), ("numpy", "NumPy"), ("pymysql", "PyMySQL"),
    ):
        try:
            __import__(module)
            line(True, label)
        except ImportError:
            line(False, label, "pip install -r requirements.txt")

    try:
        import lxml  # noqa: F401

        line(True, "lxml", "faster HTML parsing")
    except ImportError:
        line(None, "lxml", "optional; falling back to the slower stdlib parser")

    print()

    # -- external runtimes --------------------------------------------------
    php = report["php"]
    line(php["found"], "PHP", f"{php['version']} ({php['source']})" if php["found"] else "")
    if php["found"] and php["missing_extensions"]:
        line(None, "  PHP extensions", "missing: " + ", ".join(php["missing_extensions"]))

    db = report["mysql"]
    line(db["found"], "MySQL / MariaDB",
         f"{db['flavour']} {db['version']} ({db['source']})" if db["server_found"] else "")
    if db["server_found"]:
        # A distribution keeps these in /usr/bin while the server is in
        # /usr/sbin. Without mariadb-install-db no job can start its database,
        # and without the client the import falls back to the slow Python path.
        line(bool(db["install_db_binary"]), "  mariadb-install-db",
             db["install_db_binary"] or "required to create a job's database")
        line(bool(db["client_binary"]) or None, "  database client",
             db["client_binary"] or "absent: imports will use the slower Python path")

    browser = report["playwright"]
    line(browser["chromium_ready"], "Chromium",
         browser["chromium"] or "python -m playwright install chromium")

    line(True, "wpress extractor", report["wpress_extractor"]["backend"])

    # -- disk ---------------------------------------------------------------
    print()
    probe = settings.jobs_dir if settings.jobs_dir.exists() else Path.cwd()
    usage = shutil.disk_usage(probe)
    enough = usage.free > 5 * 1024**3
    # The drive on Windows; the job directory itself on Linux, where the anchor
    # is always "/" and would say nothing about which volume /srv is on.
    where = probe.anchor if sys.platform == "win32" else probe
    line(enough or None, "Free disk space",
         f"{human_bytes(usage.free)} on {where}"
         + ("" if enough else "  (a conversion needs ~3x the .wpress size)"))

    # -- what to do ---------------------------------------------------------
    print()
    problems = [
        section["instructions"]
        for key in ("php", "mysql", "playwright")
        for section in [report[key]]
        if section.get("instructions")
    ]

    windows = sys.platform == "win32"
    if ready:
        print(f"  {paint('Ready to convert.', GREEN)}")
        print(f"  {paint('Start with:  .\\run.ps1' if windows else 'Start with:  .venv/bin/python app.py', DIM)}")
    else:
        print(f"  {paint('Not ready.', RED)} Fix the following:")
        for instruction in problems:
            print()
            for text in str(instruction).splitlines():
                print(f"    {text}")
        # Only true on Windows: provision_php refuses on every other platform,
        # so offering it to a Linux operator sends them looking for a script
        # that cannot help them.
        if windows:
            print()
            print("  PHP and MariaDB can also be fetched automatically: run .\\setup.ps1,")
            print("  or simply start a conversion and the tool will download them.")
        else:
            print()
            print("  On Ubuntu/Debian, ./setup.sh installs all of the above.")

    print()
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
