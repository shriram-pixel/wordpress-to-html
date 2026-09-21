"""The Linux paths through runtime detection and database startup.

These are the three faults that would have stopped every conversion on a
Debian-packaged MariaDB, none of which any Windows test could see:

* the server lives in ``/usr/sbin`` while its helpers live in ``/usr/bin``, so
  looking beside the server finds none of them;
* without ``mariadb-install-db`` the code fell back to ``--initialize-insecure``,
  which is MySQL-only and which MariaDB rejects;
* without ``--no-defaults`` the server reads ``/etc/mysql/my.cnf`` and tries to
  write a pid file into ``/run/mysqld``, which the run user cannot do.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import runtime_provisioner as rp  # noqa: E402
from app.services.runtime_provisioner import MysqlRuntime  # noqa: E402
from app.services.wordpress_runner import MysqlServer, ServerStartupError  # noqa: E402


def debian_layout(tmp_path: Path) -> tuple[Path, Path]:
    """/usr/sbin/mariadbd with its helpers over in /usr/bin, as Ubuntu ships."""
    sbin, bin_ = tmp_path / "usr" / "sbin", tmp_path / "usr" / "bin"
    sbin.mkdir(parents=True)
    bin_.mkdir(parents=True)
    (sbin / "mariadbd").write_text("#!/bin/sh\n")
    for helper in ("mariadb-install-db", "mariadb", "mariadb-admin"):
        (bin_ / helper).write_text("#!/bin/sh\n")
    return sbin / "mariadbd", bin_


def stub_version(monkeypatch, text: str = "mariadbd  Ver 10.11.2-MariaDB") -> None:
    class Result:
        returncode, stdout, stderr = 0, text, ""

    monkeypatch.setattr(rp, "_run", lambda *a, **k: Result())
    monkeypatch.setattr(rp, "_EXE", "")


def test_helpers_are_found_in_the_sibling_bin_directory(tmp_path, monkeypatch):
    server, bin_ = debian_layout(tmp_path)
    stub_version(monkeypatch)

    runtime = rp.probe_mysql(server, "system")

    assert runtime is not None
    assert runtime.flavour == "mariadb"
    assert runtime.install_db_binary == bin_ / "mariadb-install-db"
    assert runtime.client_binary == bin_ / "mariadb"
    assert runtime.admin_binary == bin_ / "mariadb-admin"


def test_helpers_beside_the_server_win_over_the_system_ones(tmp_path, monkeypatch):
    """Two MariaDB installations: the helpers matching this server come first."""
    server, _ = debian_layout(tmp_path)
    beside = server.parent / "mariadb-install-db"
    beside.write_text("#!/bin/sh\n")
    stub_version(monkeypatch)

    runtime = rp.probe_mysql(server, "system")

    assert runtime.install_db_binary == beside


def test_mariadb_without_its_helper_is_reported_unusable(tmp_path):
    """Better to say "not ready" than to fail on the first job of a batch."""
    server = tmp_path / "mariadbd"
    server.write_text("")
    runtime = MysqlRuntime(server_binary=server, version="10.11.2", source="system",
                           flavour="mariadb", install_db_binary=None)

    assert rp._mysql_usable(runtime) is False
    instructions = rp._mysql_instructions(runtime)
    assert "mariadb-install-db" in instructions
    assert "apt install" in instructions


def test_mysql_without_a_helper_is_fine():
    """MySQL really does use --initialize-insecure; only MariaDB cannot."""
    runtime = MysqlRuntime(server_binary=Path("mysqld"), version="8.0.36", source="system",
                           flavour="mysql", install_db_binary=None)

    assert rp._mysql_usable(runtime) is True
    assert rp._mysql_instructions(runtime) is None


def test_initialise_refuses_mariadb_without_its_helper(tmp_path):
    """The old code ran --initialize-insecure here and died confusingly."""
    runtime = MysqlRuntime(server_binary=tmp_path / "mariadbd", version="10.11.2",
                           source="system", flavour="mariadb", install_db_binary=None)
    server = MysqlServer(runtime=runtime, data_dir=tmp_path / "data")

    with pytest.raises(ServerStartupError) as failure:
        server.initialise()

    assert "mariadb-install-db" in str(failure.value)
    assert "MySQL-only" in str(failure.value)


def test_the_server_never_reads_the_system_configuration(tmp_path, monkeypatch):
    """--no-defaults first, and a pid file inside the job's own workspace."""
    captured: dict = {}

    class Process:
        pid = 1234

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return Process()

    runtime = MysqlRuntime(server_binary=Path("mariadbd"), version="10.11.2",
                           source="system", flavour="mariadb",
                           install_db_binary=Path("mariadb-install-db"))
    server = MysqlServer(runtime=runtime, data_dir=tmp_path / "job" / "data", port=3999)

    monkeypatch.setattr(MysqlServer, "initialise", lambda self: None)
    monkeypatch.setattr("app.services.wordpress_runner.subprocess.Popen", fake_popen)
    monkeypatch.setattr("app.services.wordpress_runner.wait_for_port",
                        lambda *a, **k: True)
    monkeypatch.setattr(MysqlServer, "_wait_until_queryable", lambda self, timeout=30: None)

    server.start()

    command = captured["command"]
    assert command[1] == "--no-defaults", "must come before every other option"
    pid_file = next(a for a in command if a.startswith("--pid-file="))
    assert str(tmp_path) in pid_file, "the pid file belongs to the job, not to /run/mysqld"
