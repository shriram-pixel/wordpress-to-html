"""The init command is built from what the binary says, not from the platform.

Two bugs came from assuming. ``mariadb-install-db`` is a C++ program on Windows
and a shell script on Linux; the .exe takes --default-user and rejects
--no-defaults, the script is the exact reverse. Guessing from platform.system()
was wrong in both directions, a day apart, and each time the error named the
database server rather than the flag.

So the program is asked. These tests feed it each variant's real help text.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import wordpress_runner as wr  # noqa: E402

WINDOWS_HELP = """mysql_install_db.exe  Ver 1.00 for Windows
Usage: mysql_install_db.exe [OPTIONS]
OPTIONS:
  -?, --help          Display this help message and exit.
  -d, --datadir=name  Data directory of the new database
  -S, --service=name  Name of the Windows service
  -D, --default-user  Create default user
  -N, --skip-networking
"""

POSIX_HELP = """Usage: mariadb-install-db [OPTIONS]
  --basedir=path       Path to the MariaDB installation directory.
  --datadir=path       Path to the MariaDB data directory.
  --no-defaults        Do not read any option files.
  --user=user_name     The login username to use for running mariadbd.
  --skip-name-resolve  Use IP addresses rather than hostnames.
"""


@pytest.fixture(autouse=True)
def no_cache():
    """The probe caches per binary; each test supplies its own help text."""
    wr._supported_options.cache_clear()
    yield
    wr._supported_options.cache_clear()


def with_help(monkeypatch, text: str, *, fails: bool = False):
    class Result:
        stdout, stderr, returncode = text, "", 0

    def fake_run(command, **kwargs):
        if fails:
            raise OSError("cannot execute")
        return Result()

    monkeypatch.setattr(wr.subprocess, "run", fake_run)


def build(monkeypatch, text: str, *, fails: bool = False) -> list[str]:
    with_help(monkeypatch, text, fails=fails)
    return wr._install_db_command(
        Path("mariadb-install-db"), Path("/job/data"), Path("/usr")
    )


def test_the_windows_installer_gets_its_own_options(monkeypatch):
    command = build(monkeypatch, WINDOWS_HELP)

    assert "--default-user" in command
    assert "--no-defaults" not in command, "the .exe exits on this"
    assert not any(a.startswith("--basedir") for a in command), "and on this"
    assert any(a.startswith("--datadir") for a in command)


def test_the_posix_script_gets_its_own_options(monkeypatch):
    command = build(monkeypatch, POSIX_HELP)

    assert "--no-defaults" in command
    assert any(a.startswith("--basedir") for a in command)
    assert "--default-user" not in command, "the script forwards it to mariadbd"
    assert any(a.startswith("--datadir") for a in command)


def test_no_defaults_comes_first_where_it_exists(monkeypatch):
    """It decides whether the other options are read from a config file."""
    command = build(monkeypatch, POSIX_HELP)

    assert command[1] == "--no-defaults"


def test_datadir_is_always_present(monkeypatch):
    """The one option every variant of every version accepts."""
    for text in (WINDOWS_HELP, POSIX_HELP, "Usage: something --help\n"):
        command = build(monkeypatch, text)
        assert any(a.startswith("--datadir=") for a in command), text[:30]


def test_a_program_that_will_not_answer_falls_back_to_the_platform(monkeypatch):
    """Still has to produce something workable, so the old rule remains."""
    command = build(monkeypatch, "", fails=True)

    assert any(a.startswith("--datadir") for a in command)
    if wr._IS_WINDOWS:
        assert "--default-user" in command and "--no-defaults" not in command
    else:
        assert "--no-defaults" in command and "--default-user" not in command


def test_an_unknown_future_variant_gets_only_what_it_advertises(monkeypatch):
    """A version that drops an option must not be handed it anyway."""
    command = build(monkeypatch, "Usage: x\n  --datadir=path  where\n")

    # Path renders with the host's separator; compare the same way.
    assert command == ["mariadb-install-db", f"--datadir={Path('/job/data')}"]
