"""Run conversions in the background, one worker per job.

A conversion takes minutes to hours, so it cannot run inside an HTTP request.
Jobs are queued and executed on a small thread pool while FastAPI keeps serving
status polls.

Threads rather than processes: each job spends nearly all its time waiting on
subprocesses (PHP, MariaDB, Chromium) and sockets, so the GIL is not the
constraint, and threads keep the SQLite store and log handlers simple. The
pipeline itself opens an event loop internally for the async stages.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.models.job import JobRecord, JobStatus, JobStore
from app.services.pipeline import ConversionCancelled, ConversionPipeline
from app.utils.filesystem import remove_tree

logger = logging.getLogger(__name__)


@dataclass
class RunningJob:
    job_id: str
    future: Future
    cancel_event: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.time)


class JobManager:
    """Owns the worker pool and the lifecycle of every conversion."""

    #: A job silent for this long is assumed to belong to a process that died.
    stale_after_seconds: float = 15 * 60

    def __init__(self, settings: Settings, store: JobStore, max_workers: int = 1) -> None:
        self.settings = settings
        self.store = store
        # One conversion at a time by default: each job runs its own MariaDB,
        # PHP server and Chromium, so running several at once on a laptop is a
        # reliable way to exhaust memory rather than to go faster.
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="wpsc-job")
        self._running: dict[str, RunningJob] = {}
        self._lock = threading.RLock()

    # -- submission ---------------------------------------------------------
    def submit(self, job: JobRecord, archive_path: Path) -> None:
        """Queue *job* for conversion."""
        with self._lock:
            if job.id in self._running:
                raise ValueError(f"job {job.id} is already running")

            cancel_event = threading.Event()
            future = self.executor.submit(self._run, job, Path(archive_path), cancel_event)
            self._running[job.id] = RunningJob(job.id, future, cancel_event)

        future.add_done_callback(lambda _f, job_id=job.id: self._forget(job_id))
        logger.info("queued job %s (%s)", job.id, job.filename)

    def resume(self, job: JobRecord, *, folders: bool | None = None) -> int:
        """Queue a stopped job to continue from its restored WordPress.

        Returns the port the temporary WordPress will be served on.
        """
        port = self.resume_port_for(job.id)
        archive = Path(str(job.summary.get("archive_path") or ""))
        archive_path = archive if archive.name and archive.is_file() else None
        with self._lock:
            if job.id in self._running:
                raise ValueError(f"job {job.id} is already running")
            cancel_event = threading.Event()
            future = self.executor.submit(
                self._run_resume, job, port, archive_path, folders, cancel_event
            )
            self._running[job.id] = RunningJob(job.id, future, cancel_event)

        future.add_done_callback(lambda _f, job_id=job.id: self._forget(job_id))
        logger.info("queued resume of job %s on port %d", job.id, port)
        return port

    def resume_port_for(self, job_id: str) -> int:
        """The port a resumed job must use.

        Captured pages hold absolute URLs to the port they were rendered from,
        so once any exist that port is fixed. Before that any free port works.
        """
        import re

        from app.services.wordpress_runner import find_free_port

        rendered = self.settings.job_dir(job_id) / "rendered"
        if rendered.is_dir() and any(rendered.glob("*.html")):
            for record in self.store.get_urls(job_id):
                match = re.match(r"https?://127\.0\.0\.1:(\d+)", record.url)
                if match:
                    return int(match.group(1))
        return find_free_port()

    def _run_resume(self, job, port, archive_path, folders, cancel_event) -> None:
        pipeline = ConversionPipeline(
            job, self.store, self.settings, cancel_check=cancel_event.is_set
        )
        try:
            pipeline.run_resume(port=port, archive=archive_path, folders=folders)
        except ConversionCancelled:
            logger.info("job %s was cancelled", job.id)
        except Exception:
            logger.exception("resume of job %s failed", job.id)

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._running.pop(job_id, None)

    def _run(self, job: JobRecord, archive_path: Path, cancel_event: threading.Event) -> None:
        pipeline = ConversionPipeline(
            job, self.store, self.settings, cancel_check=cancel_event.is_set
        )
        try:
            pipeline.run(archive_path)
        except ConversionCancelled:
            logger.info("job %s was cancelled", job.id)
        except Exception:
            # The pipeline has already recorded the failure and written a
            # report; the traceback goes to the job log.
            logger.exception("job %s failed", job.id)

    # -- control ------------------------------------------------------------
    def cancel(self, job_id: str) -> bool:
        """Ask a running job to stop at its next checkpoint."""
        with self._lock:
            running = self._running.get(job_id)
            if running is None:
                return False
            running.cancel_event.set()
            # If it has not started yet, cancelling the future is enough.
            if running.future.cancel():
                self.store.update(
                    job_id, status=JobStatus.CANCELLED, finished_at=time.time(),
                    error="cancelled before it started",
                )
                self._forget(job_id)
        logger.info("cancellation requested for job %s", job_id)
        return True

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._running

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._running)

    def delete_job(self, job_id: str) -> bool:
        """Cancel a job if needed, then remove its workspace and its rows."""
        self.cancel(job_id)

        deadline = time.monotonic() + 30
        while self.is_running(job_id) and time.monotonic() < deadline:
            time.sleep(0.25)

        workspace = self.settings.job_dir(job_id)
        if workspace.exists():
            remove_tree(workspace)
        self.store.delete(job_id)
        logger.info("deleted job %s", job_id)
        return True

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            for running in self._running.values():
                running.cancel_event.set()
        self.executor.shutdown(wait=wait, cancel_futures=True)

    # -- recovery -----------------------------------------------------------
    def recover_orphans(self) -> int:
        """Mark jobs left mid-run by a previous process as failed.

        Without this, a job whose process was killed would sit at RENDERING for
        ever and the UI would poll it indefinitely.
        """
        recovered = 0
        for job in self.store.list(limit=500):
            if job.status.is_terminal or job.status is JobStatus.QUEUED:
                continue
            if self.is_running(job.id):
                continue
            # The job list is shared with convert.py, so "not running in this
            # process" does not mean "abandoned": a command-line conversion may
            # be working on it right now. Only a job that has logged nothing for
            # a while is treated as dead.
            last = self.store.last_event_time(job.id)
            if last is not None and time.time() - last < self.stale_after_seconds:
                continue
            self.store.update(
                job.id,
                status=JobStatus.FAILED,
                finished_at=time.time(),
                error=(
                    "the application stopped while this job was running. "
                    "Start it again to convert the backup."
                ),
            )
            self.store.add_event(
                job.id, "RECOVERY",
                "marked as failed: the application restarted while this job was running",
                "WARN",
            )
            recovered += 1

        if recovered:
            logger.warning("marked %d interrupted job(s) as failed", recovered)
        return recovered
