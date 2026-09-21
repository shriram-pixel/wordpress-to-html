"""Cancelling must take effect now, not at the next checkpoint.

A conversion spends minutes at a time inside a single operation: waiting for
WordPress's first page, rewriting a large table, rendering a page. Cancelling
used to mean "raise at the next progress report", so the button appeared to do
nothing. These tests cover the three pieces that changed.
"""

from __future__ import annotations

import threading
import time

import pytest


# ---------------------------------------------------------------------------
# Waiting for WordPress
# ---------------------------------------------------------------------------
def test_waiting_for_wordpress_stops_when_cancelled():
    from app.services.wordpress_runner import _wait_for_wordpress

    # No server is listening, so without a stop signal this would poll until
    # its deadline. It must return as soon as the signal is set instead.
    started = time.monotonic()
    ok, detail = _wait_for_wordpress(
        "http://127.0.0.1:9",                    # discard port: nothing answers
        problem=lambda: None,
        tail_log=lambda: "",
        timeout=600,
        expected_status=(200,),
        should_stop=lambda: True,
    )

    assert ok is False
    assert "cancelled" in detail
    assert time.monotonic() - started < 5, "the wait should end immediately"


def test_waiting_for_wordpress_still_waits_without_a_stop_signal():
    from app.services.wordpress_runner import _wait_for_wordpress

    ok, detail = _wait_for_wordpress(
        "http://127.0.0.1:9", problem=lambda: None, tail_log=lambda: "",
        timeout=2, expected_status=(200,),
    )
    assert ok is False
    assert "cancelled" not in detail


# ---------------------------------------------------------------------------
# The job manager
# ---------------------------------------------------------------------------
class _FakePipeline:
    def __init__(self) -> None:
        self.aborted = threading.Event()

    def abort(self) -> None:
        self.aborted.set()


def test_cancel_aborts_the_running_pipeline(tmp_path):
    from app.config import Settings
    from app.models.job import JobStore
    from app.services.job_manager import JobManager, RunningJob

    settings = Settings(jobs_dir=tmp_path / "jobs", database_path=tmp_path / "jobs.sqlite3")
    settings.ensure_directories()
    store = JobStore(settings.database_path)
    manager = JobManager(settings, store)
    try:
        release = threading.Event()
        future = manager.executor.submit(release.wait, 30)
        pipeline = _FakePipeline()
        manager._running["job1"] = RunningJob("job1", future, pipeline=pipeline)

        assert manager.cancel("job1") is True
        assert pipeline.aborted.is_set(), "cancelling must stop the work, not only flag it"
    finally:
        release.set()
        manager.shutdown(wait=False)
        store.close()


def test_cancelling_an_unknown_job_is_harmless(tmp_path):
    from app.config import Settings
    from app.models.job import JobStore
    from app.services.job_manager import JobManager

    settings = Settings(jobs_dir=tmp_path / "jobs", database_path=tmp_path / "jobs.sqlite3")
    settings.ensure_directories()
    store = JobStore(settings.database_path)
    manager = JobManager(settings, store)
    try:
        assert manager.cancel("nope") is False
    finally:
        manager.shutdown(wait=False)
        store.close()


# ---------------------------------------------------------------------------
# The URL rewrite
# ---------------------------------------------------------------------------
def test_url_rewrite_stops_between_tables():
    from app.services.wordpress_restorer import RewriteCancelled, replace_urls_in_database

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args, **kwargs):
            self.rows = [("wp_posts",), ("wp_postmeta",)]

        def fetchall(self):
            return self.rows

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cursor()

    class _Server:
        def connect(self, database=None):
            return _Connection()

    with pytest.raises(RewriteCancelled):
        replace_urls_in_database(
            _Server(), "db", {"https://example.com": "http://127.0.0.1:1"},
            should_stop=lambda: True,
        )
