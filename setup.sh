#!/usr/bin/env bash
# One-time setup on a Linux server (Ubuntu/Debian).
#
#   sudo ./setup.sh            install system packages, then set the tool up
#   ./setup.sh --no-system     skip the apt part (when packages are already in)
#
# Mirrors setup.ps1, with the two differences that matter on Linux:
#   * PHP and MariaDB come from the distribution rather than a portable
#     download, so the .env points at them explicitly;
#   * Ubuntu's AppArmor profile confines MariaDB to /var/lib/mysql, which stops
#     a per-job database directory from working at all. That is handled below.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

SKIP_SYSTEM=0
[[ "${1:-}" == "--no-system" ]] && SKIP_SYSTEM=1

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m==> %s\033[0m\n' "$*" >&2; exit 1; }

# Who will actually run conversions. Under sudo that is the invoking user, not
# root: a virtualenv, a Chromium download or a /srv directory created as root
# is unusable by them afterwards, and the failure comes much later, as a
# permission error with no obvious cause.
RUN_USER="${SUDO_USER:-$(id -un)}"
as_user() {
  if [[ $EUID -eq 0 && "$RUN_USER" != "root" ]]; then
    sudo -u "$RUN_USER" -H "$@"
  else
    "$@"
  fi
}

# ------------------------------------------------------------------ python --
# Two features decide this: enum.StrEnum needs 3.11, and shutil.rmtree(onexc=)
# needs 3.12. Ubuntu 24.04 ships 3.12, which is why it is the recommended
# release; 22.04 ships 3.10 and cannot import the application at all. Checking
# here costs a second and turns an obscure ImportError into a clear message.
find_python() {
  local candidate
  for candidate in python3.14 python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

# ---------------------------------------------------------------- packages --
if [[ $SKIP_SYSTEM -eq 0 ]]; then
  if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo, or pass --no-system if the packages are already installed." >&2
    exit 1
  fi
  say "Installing system packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    php-cli php-mysql php-mbstring php-xml php-gd php-zip php-curl php-intl php-bcmath \
    php-opcache \
    mariadb-server mariadb-client \
    unzip curl ca-certificates \
    fonts-liberation fonts-dejavu fonts-noto fonts-noto-cjk fonts-noto-color-emoji

  # php-opcache above is not a nicety: without it PHP recompiles WordPress on
  # every request, which is most of the time a page-builder page takes to
  # render. The tool passes -d opcache.enable=1, but that is a no-op if the
  # extension is not installed.

  # Wanted by real sites rather than by the tool, so a missing package here
  # must not stop the setup: imagick because WordPress prefers it to GD and
  # picks different image sizes without it, redis/memcached because a backup
  # that still contains wp-content/object-cache.php fatals the whole site if
  # its extension is absent.
  apt-get install -y --no-install-recommends \
    php-imagick php-redis php-memcached php-soap 2>/dev/null || \
    warn "some optional PHP extensions were unavailable; sites needing them may fail"

  # Every page renders with the fonts this machine has. Without these, text
  # falls back to a substitute face: different line breaks, different layout,
  # and screenshots that do not match the original.
  fc-cache -f >/dev/null 2>&1 || true

  say "Stopping the system MariaDB service"
  # Each job starts its own database on a random port; the system instance is
  # not used and only competes for memory.
  systemctl stop mariadb 2>/dev/null || true
  systemctl disable mariadb 2>/dev/null || true

  if [[ -f /etc/apparmor.d/usr.sbin.mariadbd ]]; then
    say "Relaxing the AppArmor profile for MariaDB"
    # The profile allows /var/lib/mysql only. A conversion runs its database
    # inside the job workspace, which AppArmor would deny, and the job would
    # fail with a permission error that names no permission.
    mkdir -p /etc/apparmor.d/disable
    ln -sf /etc/apparmor.d/usr.sbin.mariadbd /etc/apparmor.d/disable/
    apparmor_parser -R /etc/apparmor.d/usr.sbin.mariadbd 2>/dev/null || true
  fi

  say "Raising the open-file limit"
  # A job runs a database, a dozen PHP workers and a browser at once.
  grep -q 'wpsc nofile' /etc/security/limits.conf 2>/dev/null || cat >> /etc/security/limits.conf <<'LIMITS'
# wpsc nofile
*  soft  nofile  65535
*  hard  nofile  65535
LIMITS
fi

# ------------------------------------------------------------ python setup --
PYTHON="$(find_python)" || die "This tool needs Python 3.12 or newer, and none was found.
    Ubuntu 24.04 ships 3.12 and is the recommended release.
    On 22.04 (which ships 3.10):
        sudo add-apt-repository -y ppa:deadsnakes/ppa
        sudo apt update && sudo apt install -y python3.12 python3.12-venv
    then run this script again."

say "Using $PYTHON ($("$PYTHON" -V 2>&1))"

if [[ $EUID -eq 0 && "$RUN_USER" != "root" ]]; then
  # Cloned under sudo, this whole tree is root-owned and the run user cannot
  # create .venv in it, let alone a job workspace.
  chown -R "$RUN_USER" "$ROOT"
fi

say "Creating the virtual environment"
as_user "$PYTHON" -m venv .venv
as_user .venv/bin/pip install --quiet --upgrade pip wheel
as_user .venv/bin/pip install --quiet -r requirements.txt

say "Installing Chromium for Playwright"
# Split deliberately: the shared libraries are a system package and need root,
# but the browser itself is downloaded into ~/.cache/ms-playwright and is found
# relative to the home directory of whoever runs a conversion. Installing it as
# root puts it in /root/.cache, where the run user will never find it -- and
# the doctor, also run as root, would report Chromium present.
if [[ $EUID -eq 0 && $SKIP_SYSTEM -eq 0 ]]; then
  .venv/bin/playwright install-deps chromium
fi
as_user .venv/bin/playwright install chromium || \
  warn "run 'sudo .venv/bin/playwright install-deps chromium' if pages fail to render"

# ------------------------------------------------------------------- .env ---
PHP_BIN="$(command -v php || true)"
MYSQLD_BIN="$(command -v mariadbd || command -v mysqld || ls /usr/sbin/mariadbd /usr/sbin/mysqld 2>/dev/null | head -1 || true)"

if [[ -z "$PHP_BIN" || -z "$MYSQLD_BIN" ]]; then
  warn "PHP or MariaDB was not found; set WPSC_PHP_BINARY / WPSC_MYSQLD_BINARY in .env by hand."
fi

if [[ ! -f .env ]]; then
  say "Writing .env"
  cat > .env <<ENV
# Linux server settings. See .env.example for every option.
WPSC_HOST=127.0.0.1
WPSC_PORT=8000

# Where backups are read from and jobs are written. Give the jobs directory a
# fast disk with room for 3x the size of every backup running at once.
WPSC_BACKUP_DIRS=/srv/backups
WPSC_JOBS_DIR=/srv/wpsc-jobs

# Use the distribution's binaries rather than downloading portable ones.
WPSC_PHP_BINARY=${PHP_BIN}
WPSC_MYSQLD_BINARY=${MYSQLD_BIN}
WPSC_AUTO_PROVISION_RUNTIMES=false

# 0 measures this machine and decides. Set a number to hold it back.
WPSC_RENDER_CONCURRENCY=0
WPSC_ASSET_CONCURRENCY=0
WPSC_MAX_PARALLEL_JOBS=0
ENV
else
  warn ".env already exists; leaving it alone."
fi

if mkdir -p /srv/backups /srv/wpsc-jobs /srv/exports 2>/dev/null; then
  # Created by root under sudo, these would be read-only to the person who
  # actually runs the conversions, and the job database could not be created.
  [[ $EUID -eq 0 ]] && chown "$RUN_USER" /srv/backups /srv/wpsc-jobs /srv/exports
else
  warn "could not create /srv/backups, /srv/wpsc-jobs and /srv/exports; create them yourself"
fi

# ------------------------------------------------------------------ checks --
# As the run user, not root: the checks must see what a conversion will see.
say "Checking the dependencies the tool needs"
as_user .venv/bin/python scripts/doctor.py || warn "the doctor reported problems; fix them before converting"

say "Running the fast test suite"
as_user .venv/bin/python -m pytest -q -m "not integration"

cat <<'DONE'

Setup finished.

  Convert one site:
      .venv/bin/python convert.py /srv/backups/site.wpress -o /srv/exports

  Convert everything in /srv/backups, four at a time:
      .venv/bin/python run_batch.py /srv/backups -o /srv/exports --parallel 4

  Web interface (loopback only; reach it over an SSH tunnel):
      .venv/bin/python app.py
DONE
