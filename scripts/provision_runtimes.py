"""Download the portable PHP and MariaDB the converter needs.

Invoked by setup.ps1; also usable directly:

    python -m scripts.provision_runtimes
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.services.runtime_provisioner import (  # noqa: E402
    RuntimeUnavailable,
    ensure_runtimes,
)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="    %(message)s")
    settings = get_settings()

    def progress(message: str, fraction: float) -> None:
        bar_width = 28
        filled = int(bar_width * max(0.0, min(1.0, fraction)))
        bar = "#" * filled + "." * (bar_width - filled)
        print(f"\r    [{bar}] {message[:52]:<52}", end="", flush=True)

    try:
        runtimes = ensure_runtimes(
            settings.runtime_dir,
            php_version=settings.php_version,
            mariadb_version=settings.mariadb_version,
            auto_provision=True,
            php_override=settings.php_binary,
            mysqld_override=settings.mysqld_binary,
            progress=progress,
        )
    except RuntimeUnavailable as exc:
        print()
        print(f"    Could not provide the runtimes: {exc}")
        if exc.instructions:
            print()
            for line in exc.instructions.splitlines():
                print(f"    {line}")
        return 1

    print()
    print(f"    PHP {runtimes.php.version} ({runtimes.php.source}) at {runtimes.php.binary}")
    print(
        f"    {runtimes.mysql.flavour} {runtimes.mysql.version} "
        f"({runtimes.mysql.source}) at {runtimes.mysql.server_binary}"
    )
    if runtimes.php.missing_extensions:
        print(f"    Note: PHP is missing {', '.join(runtimes.php.missing_extensions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
