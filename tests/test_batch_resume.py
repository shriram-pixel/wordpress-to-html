"""A stopped batch continues where it stopped.

Twenty backups, four running, sixteen to go, and the run is interrupted. The
four that were mid-conversion already have their WordPress extracted and their
database imported -- roughly half an hour each on a 3 GB backup. Starting them
over throws that away, so the batch looks for it and picks it up.

What it must *not* do is reuse a job that failed. A failure means something
went wrong; continuing from the half-built result is the least likely way to
get a different answer, and a fresh run costs less than a wrong export.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_batch  # noqa: E402
from app.config import ConversionOptions, Settings  # noqa: E402
from app.models.job import JobStatus, JobStore, UrlRecord  # noqa: E402


@pytest.fixture
def workspace(tmp_path):
    """A jobs directory with its own store, as a real run has."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    settings = Settings(jobs_dir=jobs, database_path=jobs / "jobs.sqlite3")
    return settings, jobs


def make_job(settings, jobs, name: str, status: JobStatus, *,
             with_install: bool = True, pages: int = 0, port: int = 0):
    """A job in the store, with as much of a workspace as the test needs."""
    store = JobStore(settings.database_path)
    job = store.create(name, ConversionOptions(), 1024)
    store.update(job.id, status=status)

    root = jobs / job.id
    if with_install:
        (root / "wordpress").mkdir(parents=True)
    for n in range(pages):
        (root / "rendered").mkdir(parents=True, exist_ok=True)
        (root / "rendered" / f"page{n}.html").write_text("<html></html>", encoding="utf-8")
        store.add_urls(job.id, [UrlRecord(url=f"http://127.0.0.1:{port}/page{n}/")])
    store.close()
    return job.id


def test_an_interrupted_site_is_picked_up(workspace):
    settings, jobs = workspace
    job_id = make_job(settings, jobs, "site.wpress", JobStatus.CANCELLED,
                      pages=3, port=54321)

    restart = run_batch.find_restart(Path("/bk/site.wpress"), jobs, settings)

    assert restart is not None
    assert restart.job_id == job_id
    assert restart.pages == 3
    assert restart.port == 54321, "captured pages fix the port they were rendered against"
    assert "3 page(s) already rendered" in restart.describe()


def test_a_site_stopped_before_rendering_needs_no_port(workspace):
    """Nothing holds a URL to the old port yet, so any free one will do."""
    settings, jobs = workspace
    make_job(settings, jobs, "site.wpress", JobStatus.CANCELLED, pages=0)

    restart = run_batch.find_restart(Path("/bk/site.wpress"), jobs, settings)

    assert restart is not None
    assert restart.pages == 0
    assert restart.port == 0, "the resume picks a fresh port"


def test_a_failed_site_starts_over(workspace):
    """It stopped because something was wrong; reusing it repeats the wrong."""
    settings, jobs = workspace
    make_job(settings, jobs, "site.wpress", JobStatus.FAILED, pages=5, port=1234)

    assert run_batch.find_restart(Path("/bk/site.wpress"), jobs, settings) is None


def test_a_completed_site_is_not_resumed(workspace):
    """The ZIP check handles those; resuming one would redo finished work."""
    settings, jobs = workspace
    make_job(settings, jobs, "site.wpress", JobStatus.COMPLETED, pages=5, port=1234)

    assert run_batch.find_restart(Path("/bk/site.wpress"), jobs, settings) is None


def test_a_workspace_that_was_deleted_is_not_resumed(workspace):
    """The job row survives a deleted folder; a resume against it would fail."""
    settings, jobs = workspace
    make_job(settings, jobs, "site.wpress", JobStatus.CANCELLED, with_install=False)

    assert run_batch.find_restart(Path("/bk/site.wpress"), jobs, settings) is None


def test_another_site_is_not_confused_for_this_one(workspace):
    settings, jobs = workspace
    make_job(settings, jobs, "other.wpress", JobStatus.CANCELLED, pages=2, port=999)

    assert run_batch.find_restart(Path("/bk/site.wpress"), jobs, settings) is None


def test_no_history_at_all_means_a_fresh_conversion(workspace):
    settings, jobs = workspace

    assert run_batch.find_restart(Path("/bk/site.wpress"), jobs, settings) is None


def test_the_resume_command_carries_the_job_the_port_and_the_archive(tmp_path, monkeypatch):
    """What the child process is actually told to do."""
    captured: dict = {}

    class Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        captured["command"] = command
        return Completed()

    monkeypatch.setattr(run_batch.subprocess, "run", fake_run)
    monkeypatch.setattr(run_batch, "latest_report", lambda *a, **k: {})

    logs = tmp_path / "logs"
    logs.mkdir()
    backup = tmp_path / "site.wpress"
    backup.write_bytes(b"x")

    restart = run_batch.Restart(job_id="abc123", port=54321, stage="rendering", pages=3)
    result = run_batch.convert(backup, tmp_path, tmp_path, ["-c", "4"], logs, restart)

    command = " ".join(captured["command"])
    assert "--resume abc123" in command
    assert "--resume-port 54321" in command
    assert "--rerender" in command, "a resume is how a site picks up a fix made since"
    assert str(backup) in command, "the archive is needed to verify the install"
    assert result.status == "resumed"
    assert result.job_id == "abc123"


def test_a_fresh_conversion_is_unchanged(tmp_path, monkeypatch):
    """Everything above must not alter the ordinary path."""
    captured: dict = {}

    class Completed:
        returncode = 0

    monkeypatch.setattr(run_batch.subprocess, "run",
                        lambda command, **k: (captured.setdefault("command", command), Completed())[1])
    monkeypatch.setattr(run_batch, "latest_report", lambda *a, **k: {})

    logs = tmp_path / "logs"
    logs.mkdir()
    backup = tmp_path / "site.wpress"
    backup.write_bytes(b"x")

    result = run_batch.convert(backup, tmp_path, tmp_path, ["-c", "4"], logs)

    command = " ".join(captured["command"])
    assert "--resume" not in command
    assert str(backup) in command
    assert result.status == "converted"
