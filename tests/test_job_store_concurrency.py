"""Several conversions share one job database; none may lose a write.

A batch runs each site as its own process, and every one of them updates its
progress about once a second. SQLite in WAL mode allows a single writer at a
time, so without a busy timeout the loser of a collision fails immediately
with "database is locked" -- which on a batch of ten sites kills jobs at
random, hours in, for no reason the log explains.
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest


def _write_many(database: str, index: int, errors) -> None:
    """Create a job and update it repeatedly, as a real conversion does."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.config import ConversionOptions
    from app.models.job import JobStore

    store = JobStore(Path(database))
    try:
        job = store.create(f"site-{index}.wpress", ConversionOptions(), 1000)
        for step in range(60):
            store.update(job.id, stage_progress=step / 60, stage_detail=f"page {step}")
            store.add_event(job.id, "RENDER", f"rendered page {step}")
    except Exception as exc:  # noqa: BLE001 - reported to the parent
        errors.put(f"worker {index}: {type(exc).__name__}: {exc}")
    finally:
        store.close()


@pytest.mark.slow
def test_parallel_conversions_can_share_the_job_database(tmp_path: Path):
    database = tmp_path / "jobs.sqlite3"

    # Create the schema before forking, so the test measures contention
    # rather than four processes racing to create the same tables.
    from app.models.job import JobStore

    JobStore(database).close()

    context = mp.get_context("spawn")   # matches Windows, and is explicit on Linux
    errors = context.Queue()
    workers = [
        context.Process(target=_write_many, args=(str(database), index, errors))
        for index in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=180)

    failures = []
    while not errors.empty():
        failures.append(errors.get())
    assert not failures, f"writes were lost under concurrency: {failures}"

    store = JobStore(database)
    try:
        assert len(store.list(limit=50)) == 4, "every conversion must be recorded"
    finally:
        store.close()
