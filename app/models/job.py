"""Job model and its SQLite-backed store.

A conversion can run for an hour on a large site, so job state has to survive a
restart of the web process. Everything needed to resume lives in SQLite plus
the job's own workspace directory:

* ``jobs``       -- one row per conversion, including its serialised options,
                    current stage, progress and summary counters.
* ``job_events`` -- the append-only structured log shown in the UI.
* ``job_urls``   -- the per-URL checkpoint table that makes resume and retry
                    possible: each discovered URL carries its own state, so a
                    resumed job re-renders only what is still outstanding.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.config import ConversionOptions


class JobStatus(StrEnum):
    """Lifecycle stages, in the order they normally occur."""

    QUEUED = "QUEUED"
    EXTRACTING = "EXTRACTING"
    RESTORING = "RESTORING"
    STARTING_WORDPRESS = "STARTING_WORDPRESS"
    DISCOVERING_URLS = "DISCOVERING_URLS"
    RENDERING = "RENDERING"
    DOWNLOADING_ASSETS = "DOWNLOADING_ASSETS"
    GENERATING_HTML = "GENERATING_HTML"
    VALIDATING = "VALIDATING"
    ZIPPING = "ZIPPING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


#: Weight of each stage in the overall progress bar. Rendering dominates.
#: What share of a job each stage takes, used for the progress bar and the
#: estimate of time remaining. Measured, not guessed:
#:
#:                     weighted   aungmetals   tinitamfg
#:     extracting          0.05      4.0%          --
#:     restoring           0.15     22.7%         4.7%
#:     starting WP         0.04      3.8%          --
#:     discovering         0.01      0.2%         0.7%
#:     rendering           0.55     53.9%        61.4%
#:     generating HTML     0.08      8.0%         8.3%
#:     collecting assets   0.03      2.2%         4.4%
#:     validating          0.07      3.7%        17.3%
#:     zipping             0.02      1.4%         3.2%
#:
#: The two sites disagree -- restoring is a 1.9 GB import on one and 115 MB on
#: the other -- so these sit between them rather than matching either. What
#: matters is that they are the right order of magnitude: the old values had
#: restoring at 0.07 when it really takes a fifth of a job, and collecting
#: assets at 0.20 when hard links and disk serving cut it to a fiftieth. The
#: bar therefore crawled through the restore and jumped at the end, and the
#: estimate of time remaining, which extrapolates from progress, read high for
#: most of the run.
STAGE_WEIGHTS: dict[JobStatus, float] = {
    JobStatus.QUEUED: 0.0,
    JobStatus.EXTRACTING: 0.05,
    JobStatus.RESTORING: 0.15,
    JobStatus.STARTING_WORDPRESS: 0.04,
    JobStatus.DISCOVERING_URLS: 0.01,
    JobStatus.RENDERING: 0.55,
    # Generation runs before downloading: processing the rendered DOM is what
    # discovers which assets exist, so the order here matches execution and
    # keeps the progress bar monotonic.
    JobStatus.GENERATING_HTML: 0.08,
    JobStatus.DOWNLOADING_ASSETS: 0.03,
    JobStatus.VALIDATING: 0.07,
    JobStatus.ZIPPING: 0.02,
}


def format_duration(seconds: float | None) -> str:
    """``95`` -> ``1m 35s``; ``None`` -> ``estimating...``."""
    if seconds is None:
        return "estimating..."
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


#: Short, plain labels for the terminal and the UI.
STAGE_LABELS: dict[str, str] = {
    "QUEUED": "Queued",
    "EXTRACTING": "Extracting the backup",
    "RESTORING": "Restoring WordPress",
    "STARTING_WORDPRESS": "Starting WordPress",
    "DISCOVERING_URLS": "Discovering pages",
    "RENDERING": "Rendering pages",
    "GENERATING_HTML": "Generating HTML",
    "DOWNLOADING_ASSETS": "Collecting assets",
    "VALIDATING": "Validating",
    "ZIPPING": "Packaging the ZIP",
    "COMPLETED": "Completed",
    "FAILED": "Failed",
    "CANCELLED": "Cancelled",
}


class UrlState(StrEnum):
    """Per-URL checkpoint state."""

    PENDING = "PENDING"
    RENDERING = "RENDERING"
    RENDERED = "RENDERED"
    WRITTEN = "WRITTEN"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(slots=True)
class UrlRecord:
    """One discovered URL and how far it got."""

    url: str
    source: str = "unknown"
    """Where it was discovered: ``database``, ``sitemap``, ``link``, ``seed``."""
    kind: str = "page"
    """``page``, ``post``, ``category``, ``tag``, ``author``, ``archive``..."""
    depth: int = 0
    state: UrlState = UrlState.PENDING
    attempts: int = 0
    output_path: str | None = None
    http_status: int | None = None
    error: str | None = None
    title: str | None = None


@dataclass(slots=True)
class JobRecord:
    """Everything the API and the worker need to know about one conversion."""

    id: str
    filename: str
    status: JobStatus = JobStatus.QUEUED
    options: ConversionOptions = field(default_factory=ConversionOptions)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    stage_progress: float = 0.0
    """0..1 within the current stage."""
    stage_detail: str = ""

    input_bytes: int = 0
    error: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    """Free-form counters accumulated by the pipeline and rendered in the report."""

    @property
    def overall_progress(self) -> float:
        """0..1 across the whole pipeline, weighting stages by typical cost."""
        if self.status is JobStatus.COMPLETED:
            return 1.0
        if self.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
            # A terminal failure is not progress. _completed_weight() would sum
            # every stage here, because FAILED is not itself in the weights
            # table, and report a job that died during extraction as 100% done.
            recorded = self.summary.get("progress_at_end")
            return float(recorded) if recorded is not None else 0.0
        return min(1.0, self._completed_weight() + STAGE_WEIGHTS.get(self.status, 0.0) * self.stage_progress)

    def _completed_weight(self) -> float:
        total = 0.0
        for stage, weight in STAGE_WEIGHTS.items():
            if stage is self.status:
                break
            total += weight
        return total

    @property
    def duration_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    @property
    def run_elapsed_seconds(self) -> float:
        """Time since this run (or resume) began -- not since the job was created."""
        started = self.summary.get("run_started_at") or self.started_at
        if not started:
            return 0.0
        return max(0.0, (self.finished_at or time.time()) - float(started))

    @property
    def stage_elapsed_seconds(self) -> float:
        started = self.summary.get("stage_started_at")
        if not started or self.status.is_terminal:
            return 0.0
        return max(0.0, time.time() - float(started))

    @property
    def stage_eta_seconds(self) -> float | None:
        """Seconds left in the current stage, from the rate it is actually making.

        The rate is measured from the stage's *first* progress report, not from
        when the stage began: a resumed render opens at, say, 58 of 183 pages
        done, and counting those as work done in the first second would promise
        a finish time that is wildly too early.
        """
        if self.status.is_terminal:
            return None
        first_at = self.summary.get("stage_first_progress_at")
        first_value = self.summary.get("stage_progress_start")
        if first_at is None or first_value is None:
            return None
        elapsed = time.time() - float(first_at)
        advanced = self.stage_progress - float(first_value)
        if elapsed < 5 or advanced < 0.02:
            return None
        rate = advanced / elapsed
        return max(0.0, (1.0 - self.stage_progress) / rate)

    @property
    def eta_seconds(self) -> float | None:
        """Rough seconds until the whole job finishes.

        Extrapolated from overall progress since this run started. Stage weights
        are estimates, so this is approximate by nature and presented that way.
        """
        if self.status.is_terminal:
            return None
        started = self.summary.get("run_started_at")
        start_value = self.summary.get("run_progress_start", 0.0)
        if not started:
            return None
        elapsed = time.time() - float(started)
        advanced = self.overall_progress - float(start_value)
        if elapsed < 30 or advanced < 0.05:
            return None
        rate = advanced / elapsed
        return max(0.0, (1.0 - self.overall_progress) / rate)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "status": str(self.status),
            "options": self.options.model_dump(mode="json"),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stage_progress": round(self.stage_progress, 4),
            "stage_detail": self.stage_detail,
            "overall_progress": round(self.overall_progress, 4),
            "input_bytes": self.input_bytes,
            "duration_seconds": round(self.duration_seconds, 2),
            "run_elapsed_seconds": round(self.run_elapsed_seconds, 1),
            "stage_elapsed_seconds": round(self.stage_elapsed_seconds, 1),
            "stage_eta_seconds": (
                round(self.stage_eta_seconds) if self.stage_eta_seconds is not None else None
            ),
            "eta_seconds": round(self.eta_seconds) if self.eta_seconds is not None else None,
            "error": self.error,
            "summary": self.summary,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    filename        TEXT NOT NULL,
    status          TEXT NOT NULL,
    options_json    TEXT NOT NULL,
    created_at      REAL NOT NULL,
    started_at      REAL,
    finished_at     REAL,
    stage_progress  REAL NOT NULL DEFAULT 0,
    stage_detail    TEXT NOT NULL DEFAULT '',
    input_bytes     INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    summary_json    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS job_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   TEXT NOT NULL,
    ts       REAL NOT NULL,
    level    TEXT NOT NULL,
    stage    TEXT NOT NULL,
    message  TEXT NOT NULL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id, id);

CREATE TABLE IF NOT EXISTS job_urls (
    job_id      TEXT NOT NULL,
    url         TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'unknown',
    kind        TEXT NOT NULL DEFAULT 'page',
    depth       INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'PENDING',
    attempts    INTEGER NOT NULL DEFAULT 0,
    output_path TEXT,
    http_status INTEGER,
    error       TEXT,
    title       TEXT,
    PRIMARY KEY (job_id, url)
);
CREATE INDEX IF NOT EXISTS idx_urls_state ON job_urls(job_id, state);
"""


class JobStore:
    """Thread-safe SQLite store.

    The worker runs in a background thread while FastAPI serves status polls
    from the event loop thread, so every statement goes through one lock and a
    single connection opened with ``check_same_thread=False``. WAL mode keeps
    the status polls from blocking behind the worker's writes.
    """

    def __init__(self, database_path: Path) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # Several conversions share this file, each writing progress every
            # second or so. WAL allows one writer at a time; without a timeout
            # the loser of a collision fails immediately with "database is
            # locked" rather than waiting the moment it takes, which on a
            # batch of ten sites means jobs dying at random.
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- jobs ---------------------------------------------------------------
    def create(self, filename: str, options: ConversionOptions, input_bytes: int = 0) -> JobRecord:
        job = JobRecord(
            id=uuid.uuid4().hex[:16],
            filename=filename,
            options=options,
            input_bytes=input_bytes,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, filename, status, options_json, created_at, input_bytes) "
                "VALUES (?,?,?,?,?,?)",
                (
                    job.id,
                    job.filename,
                    str(job.status),
                    job.options.model_dump_json(),
                    job.created_at,
                    job.input_bytes,
                ),
            )
            self._conn.commit()
        return job

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list(self, limit: int = 50) -> list[JobRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def update(self, job_id: str, **fields: Any) -> None:
        """Patch job columns. ``options`` and ``summary`` are serialised here."""
        if not fields:
            return
        if "options" in fields:
            fields["options_json"] = fields.pop("options").model_dump_json()
        if "summary" in fields:
            fields["summary_json"] = json.dumps(fields.pop("summary"), default=str)
        if "status" in fields:
            fields["status"] = str(fields["status"])

        columns = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {columns} WHERE id=?", (*fields.values(), job_id)
            )
            self._conn.commit()

    def merge_summary(self, job_id: str, values: dict[str, Any]) -> None:
        """Shallow-merge counters into the job's summary blob."""
        with self._lock:
            row = self._conn.execute("SELECT summary_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            current = json.loads(row["summary_json"]) if row else {}
            current.update(values)
            self._conn.execute(
                "UPDATE jobs SET summary_json=? WHERE id=?",
                (json.dumps(current, default=str), job_id),
            )
            self._conn.commit()

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM job_urls WHERE job_id=?", (job_id,))
            self._conn.execute("DELETE FROM job_events WHERE job_id=?", (job_id,))
            self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            self._conn.commit()

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            filename=row["filename"],
            status=JobStatus(row["status"]),
            options=ConversionOptions.model_validate_json(row["options_json"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            stage_progress=row["stage_progress"],
            stage_detail=row["stage_detail"] or "",
            input_bytes=row["input_bytes"],
            error=row["error"],
            summary=json.loads(row["summary_json"] or "{}"),
        )

    # -- events -------------------------------------------------------------
    def add_event(
        self, job_id: str, stage: str, message: str, level: str = "INFO", detail: str | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO job_events (job_id, ts, level, stage, message, detail) VALUES (?,?,?,?,?,?)",
                (job_id, time.time(), level, stage, message, detail),
            )
            self._conn.commit()

    def last_event_time(self, job_id: str) -> float | None:
        """When the job last logged anything -- a liveness signal across processes."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) AS ts FROM job_events WHERE job_id=?", (job_id,)
            ).fetchone()
        return float(row["ts"]) if row and row["ts"] is not None else None

    def events(self, job_id: str, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM job_events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                (job_id, after_id, limit),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "time": datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%H:%M:%S"),
                "level": r["level"],
                "stage": r["stage"],
                "message": r["message"],
                "detail": r["detail"],
            }
            for r in rows
        ]

    # -- URL checkpoints ----------------------------------------------------
    def add_urls(self, job_id: str, records: Iterable[UrlRecord]) -> int:
        """Insert URLs, ignoring ones already known. Returns the number added."""
        rows = [
            (job_id, r.url, r.source, r.kind, r.depth, str(r.state), r.attempts)
            for r in records
        ]
        if not rows:
            return 0
        with self._lock:
            before = self._conn.execute(
                "SELECT COUNT(*) c FROM job_urls WHERE job_id=?", (job_id,)
            ).fetchone()["c"]
            self._conn.executemany(
                "INSERT OR IGNORE INTO job_urls (job_id,url,source,kind,depth,state,attempts) "
                "VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            self._conn.commit()
            after = self._conn.execute(
                "SELECT COUNT(*) c FROM job_urls WHERE job_id=?", (job_id,)
            ).fetchone()["c"]
        return after - before

    def update_url(self, job_id: str, url: str, **fields: Any) -> None:
        if not fields:
            return
        if "state" in fields:
            fields["state"] = str(fields["state"])
        columns = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE job_urls SET {columns} WHERE job_id=? AND url=?",
                (*fields.values(), job_id, url),
            )
            self._conn.commit()

    def get_urls(self, job_id: str, state: UrlState | None = None) -> list[UrlRecord]:
        query = "SELECT * FROM job_urls WHERE job_id=?"
        params: list[Any] = [job_id]
        if state is not None:
            query += " AND state=?"
            params.append(str(state))
        query += " ORDER BY depth, url"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            UrlRecord(
                url=r["url"],
                source=r["source"],
                kind=r["kind"],
                depth=r["depth"],
                state=UrlState(r["state"]),
                attempts=r["attempts"],
                output_path=r["output_path"],
                http_status=r["http_status"],
                error=r["error"],
                title=r["title"],
            )
            for r in rows
        ]

    def url_counts(self, job_id: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) c FROM job_urls WHERE job_id=? GROUP BY state", (job_id,)
            ).fetchall()
        return {r["state"]: r["c"] for r in rows}

    def reset_stuck_urls(self, job_id: str) -> int:
        """On resume, return any URL left mid-render to the pending queue."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE job_urls SET state=? WHERE job_id=? AND state=?",
                (str(UrlState.PENDING), job_id, str(UrlState.RENDERING)),
            )
            self._conn.commit()
        return cur.rowcount
