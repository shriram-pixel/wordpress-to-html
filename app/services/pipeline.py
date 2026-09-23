"""The conversion pipeline: ``.wpress`` in, deployable static ZIP out.

Each stage is a method, each records progress and structured log events, and
each is written so a failure in one page or one asset costs that page or asset
rather than the whole job. Per-URL state is checkpointed in SQLite as it goes,
so a job that dies half-way can be resumed without re-rendering what already
succeeded.

Ordering note: HTML is processed *before* assets are downloaded. Processing the
rendered DOM is what discovers which assets exist and allocates their output
paths, so the markup can be written immediately and the files filled in
afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from app.config import ConversionOptions, Settings
from app.models.job import JobRecord, JobStatus, JobStore, UrlRecord, UrlState
from app.services import report_generator as reporting
from app.services import wordpress_restorer as restorer
from app.services.asset_manager import AssetManager
from app.services.browser_renderer import BrowserRenderer, _slug_for
from app.services.html_processor import DynamicFeatureDetector, HtmlProcessor
from app.services.runtime_provisioner import RuntimeUnavailable, ensure_runtimes
from app.services.static_validator import (
    StaticSiteServer,
    validate_in_browser,
    validate_output,
)
from app.services.url_discovery import (
    build_seed_list,
    fetch_manifest,
    filter_discovered_links,
)
from app.services import quality
from app.services.quality import template_signature
from app.services.url_rewriter import AssetMap
from app.utils import capacity
from app.utils.urls import host_variants, is_local_origin, is_raw_document
from app.services.visual_validator import VisualValidator, summarise as summarise_visual
from app.services.wordpress_runner import MysqlServer, PhpServerPool, find_free_port
from app.services.wpress_extractor import WpressError, get_extractor
from app.services.zip_builder import build_zip, verify_zip
from app.utils.filesystem import (
    JobWorkspace,
    atomic_write_bytes,
    atomic_write_text,
    directory_size,
    ensure_free_space,
    remove_tree,
    unique_path,
)
from app.utils.security import safe_join

logger = logging.getLogger(__name__)


class ConversionCancelled(Exception):
    """The job was cancelled by the user."""


def _as_cancellation(exc: Exception) -> Exception:
    """Map a stage's own "stop" exception onto the pipeline's."""
    if isinstance(exc, restorer.RewriteCancelled):
        return ConversionCancelled("cancelled by the user")
    return exc


class ConversionFailed(Exception):
    """The job cannot continue.

    Carries optional setup instructions for a missing-dependency failure.
    """

    def __init__(self, message: str, instructions: str = "") -> None:
        super().__init__(message)
        self.instructions = instructions


@dataclass
class PipelineContext:
    """Mutable state shared by the stages of one conversion."""

    job: JobRecord
    workspace: JobWorkspace
    settings: Settings
    options: ConversionOptions

    archive_path: Path | None = None
    layout: restorer.ArchiveLayout | None = None
    runtimes: object = None
    mysql: MysqlServer | None = None
    php: PhpServer | None = None
    database: str = ""
    table_prefix: str = "wp_"
    original_url: str = ""
    base_url: str = ""
    manifest: object = None
    site_hosts: set[str] = field(default_factory=set)

    asset_map: AssetMap = field(default_factory=AssetMap)
    asset_manager: AssetManager | None = None
    renderer: object = None
    """The live BrowserRenderer, so a cancel can close it immediately."""
    render_timings: list[dict] = field(default_factory=list)
    """One entry per rendered page: where its time went."""
    render_page_errors: dict[str, list[str]] = field(default_factory=dict)
    """Page URL -> JavaScript errors the *original* page threw while rendering.
    An export that throws the same error is faithful, not broken."""
    network_resources: dict[str, list] = field(default_factory=dict)
    """Page URL -> the resources Chromium fetched while rendering it. Kept so
    asset registration can happen after every page has been rendered."""
    detector: DynamicFeatureDetector = field(default_factory=DynamicFeatureDetector)
    report: reporting.ReportData = field(default_factory=reporting.ReportData)
    html_samples: list[str] = field(default_factory=list)

    def stop_servers(self) -> None:
        if self.php is not None:
            self.php.stop()
            self.php = None
        if self.mysql is not None:
            self.mysql.stop()
            self.mysql = None


class ConversionPipeline:
    """Runs one conversion from start to finish."""

    def __init__(
        self,
        job: JobRecord,
        store: JobStore,
        settings: Settings,
        *,
        cancel_check=None,
    ) -> None:
        self.job = job
        self.store = store
        self.settings = settings
        self.options = job.options
        self._cancel_check = cancel_check or (lambda: False)
        self._aborting = False

        self.workspace = JobWorkspace.create(settings.job_dir(job.id))
        self.context = PipelineContext(
            job=job, workspace=self.workspace, settings=settings, options=job.options
        )
        self.capacity = capacity.measure()
        if not self.options.render_concurrency:
            # "Auto": fix the number now and store it with the job, so a
            # resume renders with the same settings the pages were made with.
            self.options.render_concurrency = self.capacity.render_concurrency
            self.job.options = self.options
            try:
                self.store.update(self.job.id, options=self.options)
            except Exception:  # a read-only store must not stop the job
                logger.debug("could not persist the measured concurrency", exc_info=True)
        self.context.asset_map = AssetMap(
            flat=not job.options.preserve_url_structure,
            folder_links=job.options.folder_links,
        )
        self._timed_stage: tuple[str, float] | None = None
        self._stage_failures: list[dict] = []
        """Best-effort stages that crashed; reported as problems."""
        self._file_logger = self._setup_file_logging()

    # -- infrastructure -----------------------------------------------------
    def _setup_file_logging(self) -> logging.Handler:
        """Give each job its own log file, in the structured format."""
        handler = logging.FileHandler(self.workspace.log_file, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)-7s  %(name)-38s  %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        handler.setLevel(logging.INFO)
        logging.getLogger("app").addHandler(handler)
        return handler

    def _close_stage_timing(self) -> None:
        """Record how long the stage that is ending took.

        Without this, comparing two runs means reading two logs side by side,
        and a step that quietly got slower -- as the URL rewrite did when it
        gained escaped-URL patterns -- goes unnoticed.
        """
        if not self._timed_stage:
            return
        name, started = self._timed_stage
        seconds = round(time.monotonic() - started, 1)
        timings = self.context.report.stage_timings
        timings[name] = round(timings.get(name, 0.0) + seconds, 1)
        self._timed_stage = None

    def _teardown_file_logging(self) -> None:
        logging.getLogger("app").removeHandler(self._file_logger)
        self._file_logger.close()

    def _stage(self, status: JobStatus, message: str = "") -> None:
        self._close_stage_timing()
        self._timed_stage = (str(status), time.monotonic())
        self.job.status = status
        self.job.stage_progress = 0.0
        self.job.stage_detail = message
        self.store.update(
            self.job.id, status=status, stage_progress=0.0, stage_detail=message
        )
        # Timing anchors for the ETA shown in the UI and the terminal. The
        # "first progress" pair is filled in by _progress, not here: see
        # JobRecord.stage_eta_seconds for why the rate is measured from there.
        self._stage_first_progress_seen = False
        timing = {
            "stage_started_at": time.time(),
            "stage_first_progress_at": None,
            "stage_progress_start": None,
        }
        self.job.summary.update(timing)
        self.store.merge_summary(self.job.id, timing)
        self._log(str(status), message or str(status))

    def _progress(self, message: str, fraction: float) -> None:
        self._check_cancelled()
        self.job.stage_progress = max(0.0, min(1.0, fraction))
        self.job.stage_detail = message
        self.store.update(
            self.job.id, stage_progress=self.job.stage_progress, stage_detail=message
        )
        if not getattr(self, "_stage_first_progress_seen", False):
            self._stage_first_progress_seen = True
            timing = {
                "stage_first_progress_at": time.time(),
                "stage_progress_start": self.job.stage_progress,
            }
            self.job.summary.update(timing)
            self.store.merge_summary(self.job.id, timing)

    def _mark_run_start(self) -> None:
        """Anchor the overall ETA to this run, not to when the job was created.

        A resumed job may have been created yesterday; measuring from then would
        make both "elapsed" and "remaining" meaningless.
        """
        timing = {
            "run_started_at": time.time(),
            "run_progress_start": round(self.job.overall_progress, 4),
        }
        self.job.summary.update(timing)
        self.store.merge_summary(self.job.id, timing)

    def _log(self, stage: str, message: str, level: str = "INFO", detail: str = "") -> None:
        self.store.add_event(self.job.id, stage, message, level, detail or None)
        logger.log(
            {"ERROR": logging.ERROR, "WARN": logging.WARNING}.get(level, logging.INFO),
            "[%s] %s", stage, message,
        )

    def _check_cancelled(self) -> None:
        if self._cancel_check():
            raise ConversionCancelled("cancelled by the user")

    def _cancelled_now(self) -> bool:
        """Whether a failure was really a cancellation.

        Aborting closes the database, the PHP workers and the browser, so the
        work in flight fails on its way down. Those errors are the cancel, not
        a fault, and must not be reported as one.
        """
        return self._aborting or self._cancel_check()

    def abort(self) -> None:
        """Stop the work now, from another thread.

        Cancelling used to mean "raise at the next progress report", which on
        a silent step -- waiting for WordPress's first page, rewriting a large
        table, a page still loading -- left the user watching a button that
        appeared to do nothing for minutes. Shutting the database, the PHP
        pool and the browser down makes whatever is in flight fail at once,
        and the pipeline then unwinds through its normal cancelled path.
        """
        self._aborting = True
        self._log("CANCELLED", "stopping the servers and the browser", "WARN")
        try:
            self.context.stop_servers()
        except Exception:
            logger.debug("error stopping servers on abort", exc_info=True)
        renderer = self.context.renderer
        if renderer is not None:
            renderer.abort()

    # -- entry point --------------------------------------------------------
    def run(self, archive_path: Path) -> Path:
        """Run the whole pipeline. Returns the path to the finished ZIP."""
        context = self.context
        context.archive_path = Path(archive_path)
        context.report.job_id = self.job.id
        context.report.input_filename = self.job.filename
        context.report.input_bytes = context.archive_path.stat().st_size
        # Remembered so a resume can check the install against the backup.
        self.store.merge_summary(self.job.id, {"archive_path": str(context.archive_path.resolve())})
        context.report.started_at = time.time()

        self.job.started_at = time.time()
        self.store.update(self.job.id, started_at=self.job.started_at)
        self._mark_run_start()
        self._log("SETUP", f"this machine: {self.capacity.describe()}")

        try:
            self._stage_extract()
            self._stage_restore()
            self._stage_start_wordpress()
            self._stage_discover()
            asyncio.run(self._async_stages())
            zip_path = self._stage_package()
            self._finish(JobStatus.COMPLETED)
            return zip_path

        except (ConversionCancelled, restorer.RewriteCancelled):
            self._log("CANCELLED", "the job was cancelled", "WARN")
            self._finish(JobStatus.CANCELLED, "cancelled by the user")
            raise ConversionCancelled("cancelled by the user") from None
        except (ConversionFailed, RuntimeUnavailable) as exc:
            if self._cancelled_now():
                self._log("CANCELLED", "the job was cancelled", "WARN")
                self._finish(JobStatus.CANCELLED, "cancelled by the user")
                raise ConversionCancelled("cancelled by the user") from None
            instructions = getattr(exc, "instructions", "")
            self._log("FAILED", str(exc), "ERROR", instructions)
            self._finish(JobStatus.FAILED, str(exc), instructions)
            raise
        except Exception as exc:
            if self._cancelled_now():
                self._log("CANCELLED", "the job was cancelled", "WARN")
                self._finish(JobStatus.CANCELLED, "cancelled by the user")
                raise ConversionCancelled("cancelled by the user") from None
            logger.exception("unexpected failure in job %s", self.job.id)
            self._log("FAILED", f"{type(exc).__name__}: {exc}", "ERROR")
            self._finish(JobStatus.FAILED, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self.context.stop_servers()
            self._cleanup_workspace()
            self._teardown_file_logging()

    def run_resume(
        self,
        *,
        port: int,
        archive: Path | None = None,
        rerender: bool = False,
        folders: bool | None = None,
    ) -> Path:
        """Continue a job whose pages are already rendered.

        Rendering dominates a large conversion -- on a 600-page site it is over
        an hour -- and the captured DOM of every finished page is on disk in
        ``rendered/``. When a job dies or is stopped after that point there is
        no reason to pay for it twice.

        *port* must be the port the pages were originally rendered against: the
        captured HTML is full of absolute ``http://127.0.0.1:<port>/`` URLs, and
        the rewriting stage resolves them relative to the site's base URL. Bind
        anywhere else and nothing matches.
        """
        context = self.context
        workspace = self.workspace

        context.report.job_id = self.job.id
        context.report.input_filename = self.job.filename
        context.report.input_bytes = self.job.input_bytes
        context.report.started_at = self.job.started_at or time.time()

        rendered_dir = workspace.root / "rendered"
        captured = list(rendered_dir.glob("*.html")) if rendered_dir.is_dir() else []

        # What a resume actually needs is the restored install and its
        # database. Rendered pages are a bonus -- a job stopped before its first
        # page still skips the extraction and the import.
        has_database = (workspace.database / "data" / "mysql").is_dir()
        has_install = (workspace.wordpress / "index.php").is_file()
        if not (has_database and has_install):
            missing = [
                name for name, present in (("database", has_database), ("WordPress", has_install))
                if not present
            ]
            raise ConversionFailed(
                f"this job's {' and '.join(missing)} is no longer in its workspace, so there "
                "is nothing to resume from; run the conversion again from the start"
            )

        self._log("RESUME", f"resuming with {len(captured)} page(s) already rendered")

        # The page layout only affects HTML generation, so it can be chosen
        # again on a resume without re-rendering anything.
        if folders is not None and folders != self.options.preserve_url_structure:
            self.options.preserve_url_structure = folders
            self.job.options = self.options
            self.store.update(self.job.id, options=self.options)
        context.asset_map = AssetMap(
            flat=not self.options.preserve_url_structure,
            folder_links=self.options.folder_links,
        )
        self._log(
            "RESUME",
            "page layout: "
            + ("one folder per page (about/index.html)" if self.options.preserve_url_structure
               else "one file per page (about.html)"),
        )

        # Output from a previous attempt is set aside, never deleted. Generating
        # on top of it would mix layouts -- a folder about/ next to about.html --
        # and the ZIP would contain both.
        output = workspace.output
        if output.is_dir() and any(output.iterdir()):
            kept = output.with_name(f"output-previous-{time.strftime('%Y%m%d-%H%M%S')}")
            output.rename(kept)
            output.mkdir()
            self._log("RESUME", f"previous output kept as {kept.name}")

        # -- bring the environment back up ----------------------------------
        # Everything from here starts external processes. It is all inside one
        # try/finally so a failure at any point still shuts the database and PHP
        # server down -- a leaked mysqld keeps an exclusive lock on the data
        # directory, and the next resume attempt then fails to start at all.
        try:
            self._stage(JobStatus.RESTORING, "Restarting the temporary WordPress")
            self._mark_run_start()
            self._log("SETUP", f"this machine: {self.capacity.describe()}")
            # A resumed job may carry the finish time and error of the attempt
            # it is resuming; left in place they make elapsed time negative and
            # show a stale failure while it runs.
            self.job.finished_at = None
            self.job.error = None
            self.store.update(self.job.id, finished_at=None, error=None)
            context.runtimes = ensure_runtimes(
                self.settings.runtime_dir,
                auto_provision=self.settings.auto_provision_runtimes,
                php_override=self.settings.php_binary,
                mysqld_override=self.settings.mysqld_binary,
            )

            mysql = MysqlServer(
                runtime=context.runtimes.mysql, data_dir=workspace.database / "data"
            )
            mysql.start()
            context.mysql = mysql

            with mysql.connect() as conn, conn.cursor() as cur:
                cur.execute("SHOW DATABASES")
                databases = [row[0] for row in cur.fetchall() if row[0].startswith("wpsc_")]
            if not databases:
                raise ConversionFailed(
                    "the job's database is gone; run the conversion again from the start"
                )
            context.database = databases[0]

            context.base_url = f"http://127.0.0.1:{port}"
            context.site_hosts = {"127.0.0.1"}

            layout = restorer.inspect_archive(workspace.extracted)
            context.layout = layout

            # The wp-config written by the original run is the most reliable record
            # of the prefix: it preserves case, which information_schema does not on
            # a platform where the server lower-cases table names.
            context.table_prefix = _read_configured_prefix(workspace.wordpress) or ""
            if not context.table_prefix and layout.sql_dump:
                context.table_prefix = restorer.detect_table_prefix(layout.sql_dump)
            if not context.table_prefix:
                with mysql.connect(context.database) as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema=%s AND table_name LIKE %s LIMIT 1",
                        (context.database, "%options"),
                    )
                    row = cur.fetchone()
                context.table_prefix = row[0][: -len("options")] if row else "wp_"
            self._log("RESUME", f"database {context.database}, prefix {context.table_prefix!r}")

            original = restorer.read_option(
                mysql, context.database, context.table_prefix, "siteurl"
            )
            context.original_url = (original or "").rstrip("/")
            # A job stopped during the URL rewrite has siteurl already pointing
            # at its old loopback address; the real domain is in the summary.
            previous_local = None
            if context.original_url.startswith(("http://127.0.0.1:", "https://127.0.0.1:")):
                previous_local = context.original_url
                context.original_url = str(self.job.summary.get("original_url") or "").rstrip("/")
            context.report.original_url = context.original_url
            for variant in host_variants(context.original_url or ""):
                host = urlsplit(variant if "://" in variant else f"http:{variant}").hostname
                if host:
                    context.site_hosts.add(host.lower())

            # The database came back on a fresh random port, so the wp-config the
            # original run wrote now points at a server that no longer exists.
            # Rewriting it is what makes a resume work at all.
            restorer.write_wp_config(
                workspace.wordpress,
                db_name=context.database, db_user=mysql.user, db_password=mysql.password,
                db_host=f"{mysql.host}:{mysql.port}",
                table_prefix=context.table_prefix, site_url=context.base_url,
            )
            restorer.write_control_plugin(workspace.wordpress)
            self._rewrite_generated_css("RESUME")

            if not captured:
                # Stopped before its first page, possibly partway through the
                # URL rewrite. Finish the restore; every step is repeatable.
                self._finish_restore(mysql, previous_local, port)
            else:
                # Pages are already captured, so the URL rewrite and the
                # activation repair are done. The export settings are not
                # necessarily: a resume is how a job picks up a fix made since
                # it ran, and one of those settings decides whether Elementor
                # keeps a page's CSS in a file that may be missing. Applying
                # them again is cheap and idempotent -- and without it,
                # --rerender would re-render the pages exactly as wrongly as
                # the first time.
                self._record_restore_notes(
                    restorer.configure_for_static_export(
                        mysql, context.database, context.table_prefix, context.base_url
                    ),
                    "RESUME",
                )

            self._verify_core()
            self._verify_content(archive)

            php = PhpServerPool(
                runtime=context.runtimes.php,
                document_root=workspace.wordpress,
                port=port,
                workers=_php_workers_for(self.options.render_concurrency),
            )
            php.start()
            context.php = php
            ok, detail = php.wait_until_wordpress_responds(
                timeout=900, should_stop=self._cancel_check
            )
            self._check_cancelled()
            if not ok:
                raise ConversionFailed(f"the restored WordPress did not come back up: {detail[:600]}")
            self._log(
                "RESUME",
                f"WordPress serving again on {context.base_url} ({len(php.servers)} PHP workers)",
            )

            manifest = fetch_manifest(context.base_url)
            if manifest:
                context.manifest = manifest
                context.site_hosts |= manifest.hosts
                context.report.theme = manifest.theme or {}
                if manifest.active_plugins:
                    context.report.plugins = list(manifest.active_plugins)

            # -- retire URLs that can never render -------------------------------
            # Anything queued against the live domain is a duplicate of a page
            # already captured from the local server. Recording them as failures
            # would misrepresent the export.
            retired = 0
            requeued = 0
            for record in self.store.get_urls(self.job.id):
                if rerender and record.state in {UrlState.RENDERED, UrlState.WRITTEN} \
                        and is_local_origin(record.url, context.base_url):
                    # The captured DOM cannot be trusted -- typically because the
                    # install it was rendered from turned out to be damaged.
                    self.store.update_url(self.job.id, record.url, state=UrlState.PENDING)
                    requeued += 1
                    continue
                if record.state is UrlState.WRITTEN and _is_raw_document(record.url):
                    # A sitemap or feed is copied, not rendered: there is no
                    # captured DOM to regenerate it from, so fetch it again.
                    self.store.update_url(self.job.id, record.url, state=UrlState.PENDING)
                    continue
                if record.state is UrlState.WRITTEN:
                    # Roll back so the generation stage re-emits it: a previous
                    # attempt's output may have been incomplete or discarded,
                    # and the captured DOM is the source of truth, not the marker.
                    self.store.update_url(self.job.id, record.url, state=UrlState.RENDERED)
                    continue
                if record.state is UrlState.RENDERED:
                    continue
                if record.state is UrlState.FAILED and record.http_status \
                        and 400 <= record.http_status < 500:
                    # WordPress itself answered "not found" or "forbidden".
                    # Asking again gets the same answer, so it is not retried.
                    continue
                if not is_local_origin(record.url, context.base_url):
                    self.store.update_url(
                        self.job.id, record.url, state=UrlState.SKIPPED,
                        error="duplicate of a page already captured from the local server",
                    )
                    retired += 1
                else:
                    self.store.update_url(self.job.id, record.url, state=UrlState.PENDING)
            if retired:
                self._log(
                    "RESUME",
                    f"retired {retired} URL(s) that pointed at the live domain; they duplicate "
                    "pages already captured",
                )
            if requeued:
                self._log("RESUME", f"--rerender: {requeued} previously rendered page(s) queued again")

            if not self.store.url_counts(self.job.id):
                # Stopped before discovery ran: nothing is queued yet.
                self._stage_discover()

            counts = self.store.url_counts(self.job.id)
            outstanding = counts.get(str(UrlState.PENDING), 0)
            done = counts.get(str(UrlState.RENDERED), 0)
            self._log(
                "RESUME",
                f"{done} page(s) already rendered and reused; {outstanding} still to render",
            )

            # -- finish the remaining stages -------------------------------------
            # Rendering only runs if something is outstanding, and then only for
            # the pages still PENDING -- a job stopped at page 417 continues at
            # page 418, it does not start again at page 1.
            asyncio.run(self._resume_stages(render=outstanding > 0))
            zip_path = self._stage_package()
            self._finish(JobStatus.COMPLETED)
            return zip_path
        except restorer.RewriteCancelled:
            self._log("CANCELLED", "the job was cancelled", "WARN")
            self._finish(JobStatus.CANCELLED, "cancelled by the user")
            raise ConversionCancelled("cancelled by the user") from None
        except (ConversionFailed, RuntimeUnavailable) as exc:
            if self._cancelled_now():
                self._log("CANCELLED", "the job was cancelled", "WARN")
                self._finish(JobStatus.CANCELLED, "cancelled by the user")
                raise ConversionCancelled("cancelled by the user") from None
            self._log("FAILED", str(exc), "ERROR", getattr(exc, "instructions", ""))
            self._finish(JobStatus.FAILED, str(exc), getattr(exc, "instructions", ""))
            raise
        except Exception as exc:
            if self._cancelled_now():
                self._log("CANCELLED", "the job was cancelled", "WARN")
                self._finish(JobStatus.CANCELLED, "cancelled by the user")
                raise ConversionCancelled("cancelled by the user") from None
            logger.exception("resume failed for job %s", self.job.id)
            self._log("FAILED", f"{type(exc).__name__}: {exc}", "ERROR")
            self._finish(JobStatus.FAILED, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            context.stop_servers()
            self._teardown_file_logging()

    async def _resume_stages(self, *, render: bool = False) -> None:
        """Everything from rendering onwards, rendering only what is outstanding."""
        if render:
            await self._stage_render()
        await self._finishing_stages()

    async def _async_stages(self) -> None:
        """The stages that need an event loop, sharing one browser where possible."""
        await self._stage_render()
        await self._finishing_stages()

    async def _finishing_stages(self) -> None:
        """HTML, assets, extras and validation -- then the job always packages.

        Once pages are rendered the hour of work is done, so nothing after it
        may throw it away. HTML generation already survives a bad page; the
        stages after it are best effort: a crash in one is recorded as a
        problem and the job carries on to the ZIP.
        """
        self._stage_generate_html()
        await self._best_effort("collecting assets", self._stage_download_assets())
        await self._best_effort("writing sitemap and robots.txt", self._write_site_extras)
        await self._best_effort("validation", self._stage_validate())

    async def _best_effort(self, name: str, work) -> None:
        try:
            if asyncio.iscoroutine(work):
                await work
            else:
                work()
        except ConversionCancelled:
            raise
        except Exception as exc:
            logger.exception("%s failed in job %s", name, self.job.id)
            message = f"{name} stopped with an error and was skipped: {type(exc).__name__}: {exc}"[:400]
            self._log("PROBLEM", message, "ERROR")
            self._stage_failures.append({"kind": "stage-error", "message": message, "examples": []})

    def _finish(self, status: JobStatus, error: str = "", instructions: str = "") -> None:
        self._close_stage_timing()
        if self.context.report.stage_timings:
            self._log(
                "TIMING",
                "; ".join(
                    f"{name.lower().replace('_', ' ')} {value:.0f}s"
                    for name, value in self.context.report.stage_timings.items()
                ),
            )
        # Capture how far the job actually got before the status flips to a
        # terminal one, so a failure reports its real progress.
        reached = self.job.overall_progress
        self.job.status = status
        self.job.finished_at = time.time()
        context = self.context
        context.report.finished_at = self.job.finished_at
        context.report.status = str(status)
        if error:
            context.report.warnings.append(error)
        if instructions:
            context.report.notes.append(instructions)

        try:
            reporting.write_report(context.report, self.workspace.report)
        except Exception as exc:
            logger.warning("could not write the conversion report: %s", exc)

        self.store.update(
            self.job.id,
            status=status,
            finished_at=self.job.finished_at,
            stage_progress=1.0 if status is JobStatus.COMPLETED else self.job.stage_progress,
            error=error or None,
        )
        summary = self._summary()
        summary["progress_at_end"] = 1.0 if status is JobStatus.COMPLETED else round(reached, 4)
        self.store.merge_summary(self.job.id, summary)

    def _summary(self) -> dict:
        report = self.context.report
        return {
            "pages": report.html_files,
            "urls_discovered": report.urls_discovered,
            "urls_rendered": report.urls_rendered,
            "urls_failed": report.urls_failed,
            "assets": sum(report.assets_by_kind.values()),
            "assets_by_kind": report.assets_by_kind,
            "broken_links": len(report.broken_links),
            "missing_assets": len(report.missing_assets),
            "console_errors": len(report.console_errors),
            "dynamic_features": [f["name"] for f in report.dynamic_features],
            "builders": report.builders,
            "theme": report.theme.get("name") if report.theme else None,
            "original_url": report.original_url,
            "zip_name": report.zip_name,
            "zip_bytes": report.zip_bytes,
            "visual": report.visual,
            "stage_timings": report.stage_timings,
            "render_timings": report.render_timings,
            "render_spread": report.render_spread,
            "problems": len((report.quality or {}).get("problems", [])),
            "source_issues": len((report.quality or {}).get("source_issues", [])),
            "repaired": (report.quality or {}).get("repaired_count", 0),
        }

    def _verify_content(self, archive: Path | None) -> None:
        """Check the install against the backup and restore anything missing."""
        source = archive or (
            Path(self.job.summary["archive_path"]) if self.job.summary.get("archive_path") else None
        )
        if source is None or not Path(source).is_file():
            self._log(
                "RESUME",
                "the original .wpress was not given, so the install could not be checked "
                "against it (pass --archive to enable this)",
                "WARN",
            )
            return
        self._log("RESUME", f"checking the install against {Path(source).name}")
        restored = restorer.repair_content_from_archive(Path(source), self.workspace.wordpress)
        if restored:
            self._log(
                "RESUME",
                f"restored {restored:,} file(s) that were missing from the install, from the "
                "backup (only missing files -- nothing overwritten or deleted)",
                "WARN",
            )
        else:
            self._log("RESUME", "install matches the backup")

    def _verify_core(self) -> None:
        """Make sure WordPress core is complete before PHP is asked to run it."""
        core = restorer.core_root_for(self.workspace.wordpress, self.settings.runtime_dir)
        if core is None:
            self._log(
                "WP", "could not locate the cached WordPress core to verify the install", "WARN"
            )
            return
        restored = restorer.repair_core_files(self.workspace.wordpress, core)
        if restored:
            self._log(
                "WP",
                f"restored {restored} missing WordPress core file(s) from the clean cache "
                "(core only -- site content untouched)",
                "WARN",
            )

    def _cleanup_workspace(self) -> None:
        """Reclaim the large intermediate directories -- only when explicitly asked.

        Never after a failure. The restored WordPress and its database are
        exactly what is needed to diagnose a failed job, and exactly what lets
        it be resumed instead of re-extracted and re-imported from scratch;
        deleting them on the way out turned a ten-minute resume into a full
        rerun with nothing left to inspect.
        """
        if self.settings.keep_job_workspace:
            return
        if self.job.status is not JobStatus.COMPLETED:
            logger.info("keeping the workspace of an unfinished job so it can be resumed")
            return
        for directory in (self.workspace.extracted, self.workspace.wordpress,
                          self.workspace.database):
            remove_tree(directory)

    # =====================================================================
    # Stage 1: extraction
    # =====================================================================
    def _stage_extract(self) -> None:
        context = self.context
        self._stage(JobStatus.EXTRACTING, "Reading the .wpress archive")

        archive = context.archive_path
        assert archive is not None

        # Extracted tree + rendered output + ZIP is roughly three times the
        # archive; fail now with a clear number rather than half-way through.
        ensure_free_space(self.workspace.root, archive.stat().st_size * 3)

        extractor = get_extractor("auto")
        try:
            extractor.validate(archive)
        except WpressError as exc:
            raise ConversionFailed(
                f"{self.job.filename} is not a readable .wpress archive: {exc}",
                instructions=(
                    "Check that the file is an All-in-One WP Migration export and that "
                    "the upload completed. A .wpress file is not a ZIP: renaming it "
                    "will not help."
                ),
            ) from exc

        def on_progress(done: int, total: int, current: str) -> None:
            self._progress(f"Extracting {current[:60]}", done / total if total else 0.0)

        try:
            result = extractor.extract(archive, self.workspace.extracted, progress=on_progress)
        except WpressError as exc:
            raise ConversionFailed(f"the archive could not be extracted: {exc}") from exc

        context.report.extracted_files = result.files_written
        context.report.extracted_bytes = result.bytes_written
        context.report.extraction_warnings = list(result.warnings)

        self._log(
            "EXTRACT",
            f"extracted {result.files_written:,} files "
            f"({result.bytes_written / 1048576:.1f} MiB) in {result.duration_seconds:.1f}s",
        )
        if result.entries_skipped:
            self._log(
                "EXTRACT", f"{result.entries_skipped} unsafe or unwritable entries were skipped",
                "WARN",
            )

    # =====================================================================
    # Stage 2: restoration
    # =====================================================================
    def _stage_restore(self) -> None:
        context = self.context
        self._stage(JobStatus.RESTORING, "Inspecting the backup")

        layout = restorer.inspect_archive(self.workspace.extracted)
        context.layout = layout

        for warning in layout.warnings:
            self._log("RESTORE", warning, "WARN")
            context.report.warnings.append(warning)

        if layout.sql_dump is None:
            raise ConversionFailed(
                "the archive contains no database, so there is no content to render",
                instructions=(
                    "Re-export the site with All-in-One WP Migration and make sure the "
                    "database is included (it is by default)."
                ),
            )

        context.report.site_name = layout.site_name or ""
        context.report.plugins = list(layout.plugins)

        # -- runtimes --------------------------------------------------------
        self._progress("Checking PHP and the database runtime", 0.05)
        try:
            context.runtimes = ensure_runtimes(
                self.settings.runtime_dir,
                php_version=self.settings.php_version,
                mariadb_version=self.settings.mariadb_version,
                auto_provision=self.settings.auto_provision_runtimes,
                php_override=self.settings.php_binary,
                mysqld_override=self.settings.mysqld_binary,
                progress=lambda message, fraction: self._progress(message, 0.05 + fraction * 0.15),
            )
        except RuntimeUnavailable as exc:
            raise ConversionFailed(str(exc), getattr(exc, "instructions", "")) from exc

        context.report.php_version = context.runtimes.php.version
        context.report.database_version = (
            f"{context.runtimes.mysql.flavour} {context.runtimes.mysql.version}"
        )
        self._log(
            "RESTORE",
            f"using PHP {context.runtimes.php.version} ({context.runtimes.php.source}) "
            f"and {context.report.database_version} ({context.runtimes.mysql.source})",
        )

        # -- WordPress core ---------------------------------------------------
        self._progress("Preparing WordPress core", 0.25)
        core = restorer.provision_wordpress_core(
            layout.wordpress_version,
            self.settings.runtime_dir,
            progress=lambda message, fraction: self._progress(message, 0.25 + fraction * 0.15),
        )
        context.report.wordpress_version = (
            restorer.read_core_version(core) or layout.wordpress_version or ""
        )

        self._progress("Assembling the WordPress files", 0.45)
        # Move rather than copy. wp-content is nearly all of a backup, so
        # copying it would make the job hold two full copies of the site's
        # media at the same time -- the difference between needing five times
        # the archive size on disk and needing three. The extracted tree is
        # rebuilt from the .wpress if the job is ever run again.
        restorer.build_wordpress_tree(
            layout, self.workspace.wordpress, core,
            progress=lambda message, fraction: self._progress(message, 0.45 + fraction * 0.15),
            consume_archive=True,
        )

        # -- database ---------------------------------------------------------
        self._progress("Starting the temporary database", 0.62)
        mysql = MysqlServer(
            runtime=context.runtimes.mysql, data_dir=self.workspace.database / "data"
        )
        mysql.start()
        context.mysql = mysql

        context.database = f"wpsc_{self.job.id[:12]}"
        mysql.create_database(context.database)

        context.table_prefix = restorer.detect_table_prefix(layout.sql_dump)
        context.report.table_prefix = context.table_prefix
        self._log("RESTORE", f"detected the table prefix {context.table_prefix!r}")

        self._progress("Importing the database", 0.68)
        restorer.import_sql_dump(
            layout.sql_dump, mysql, context.database,
            client_binary=context.runtimes.mysql.client_binary,
            progress=lambda message, fraction: self._progress(message, 0.68 + fraction * 0.18),
            # One connection loads a multi-gigabyte dump on a single core.
            workers=max(1, min(4, self.capacity.cpus)),
            should_stop=self._cancel_check,
        )
        self._log("RESTORE", f"imported {layout.sql_dump.name}")
        self._release_extracted_tree()

        # -- URLs and configuration -------------------------------------------
        self._progress("Rewriting the site URL", 0.88)
        port = find_free_port()
        context.base_url = f"http://127.0.0.1:{port}"
        context.site_hosts = {"127.0.0.1"}

        original = restorer.detect_site_url(mysql, context.database, context.table_prefix, layout)
        if original:
            context.original_url = original
            context.report.original_url = original
            host = urlsplit(original).hostname
            if host:
                context.site_hosts.add(host.lower())
        else:
            context.report.warnings.append(
                "The original site URL could not be determined, so absolute links to "
                "the original domain may remain in the export."
            )

        restorer.write_wp_config(
            self.workspace.wordpress,
            db_name=context.database, db_user=mysql.user, db_password=mysql.password,
            db_host=f"{mysql.host}:{mysql.port}",
            table_prefix=context.table_prefix, site_url=context.base_url,
        )
        restorer.write_control_plugin(self.workspace.wordpress)

        if original:
            # Every spelling of the site's own address, not just the canonical
            # one. A site whose siteurl is https://www.example.com still has
            # plenty of https://example.com in its content, and anything left
            # unreplaced points the export -- and the crawler -- at the live
            # domain.
            replacements = _site_url_replacements(
                {variant: (f"//127.0.0.1:{port}" if variant.startswith("//") else context.base_url)
                 for variant in host_variants(original)}
            )

            report = restorer.replace_urls_in_database(
                mysql, context.database, replacements,
                progress=lambda message, fraction: self._progress(message, 0.88 + fraction * 0.1),
                should_stop=self._cancel_check,
            )
            self._log(
                "RESTORE",
                f"rewrote {report.replacements:,} URL reference(s) across "
                f"{report.rows_updated:,} row(s), preserving serialized data",
            )
            if report.errors:
                for error in report.errors[:5]:
                    self._log("RESTORE", f"URL rewrite skipped a table: {error}", "WARN")
            self._rewrite_generated_css("RESTORE")

        # Rebuild the theme and plugin activation the export left out. Without
        # this an All-in-One WP Migration backup restores with no active theme
        # and no active plugins, and renders every page blank.
        notes = restorer.repair_activation_state(
            mysql, context.database, context.table_prefix, self.workspace.wordpress,
            recorded=context.layout,
        )
        notes += restorer.configure_for_static_export(
            mysql, context.database, context.table_prefix, context.base_url
        )
        notes += restorer.deactivate_problem_plugins(mysql, context.database, context.table_prefix)
        self._record_restore_notes(notes, "RESTORE")

        self.job.stage_progress = 1.0

    def _rewrite_generated_css(self, tag: str) -> None:
        """Point page-builder CSS cached on disk at the local server."""
        context = self.context
        if not context.original_url or not context.base_url:
            return
        local = urlsplit(context.base_url).netloc
        files, count = restorer.rewrite_generated_css(
            self.workspace.wordpress,
            _site_url_replacements(
                {variant: (f"//{local}" if variant.startswith("//") else context.base_url)
                 for variant in host_variants(context.original_url)}
            ),
        )
        if files:
            self._log(tag, f"re-pointed {count:,} URL(s) in {files:,} cached page-builder CSS file(s)")

    def _finish_restore(self, mysql, previous_local: str | None, port: int) -> None:
        """Redo the post-import steps of the restore on a resumed job.

        Rows the interrupted rewrite already reached point at the old loopback
        port; the rest still point at the live domain. Both are sent to the new
        port, then activation and static-export settings are applied again.
        """
        context = self.context
        replacements: dict[str, str] = {}
        if context.original_url:
            for variant in host_variants(context.original_url):
                replacements[variant] = (
                    f"//127.0.0.1:{port}" if variant.startswith("//") else context.base_url
                )
        if previous_local and previous_local != context.base_url:
            old = urlsplit(previous_local)
            for scheme in ("http", "https"):
                replacements[f"{scheme}://{old.netloc}"] = context.base_url
            replacements[f"//{old.netloc}"] = f"//127.0.0.1:{port}"
        if replacements:
            ordered = _site_url_replacements(replacements)
            self._progress("Finishing the URL rewrite", 0.9)
            report = restorer.replace_urls_in_database(
                mysql, context.database, ordered,
                progress=lambda message, fraction: self._progress(message, 0.9 + fraction * 0.08),
                should_stop=self._cancel_check,
            )
            self._log(
                "RESUME",
                f"rewrote {report.replacements:,} URL reference(s) across "
                f"{report.rows_updated:,} row(s), preserving serialized data",
            )
            for error in report.errors[:5]:
                self._log("RESUME", f"URL rewrite skipped a table: {error}", "WARN")

        notes = restorer.repair_activation_state(
            mysql, context.database, context.table_prefix, self.workspace.wordpress,
            recorded=context.layout,
        )
        notes += restorer.configure_for_static_export(
            mysql, context.database, context.table_prefix, context.base_url
        )
        notes += restorer.deactivate_problem_plugins(mysql, context.database, context.table_prefix)
        self._record_restore_notes(notes, "RESUME")

    def _record_restore_notes(self, notes: list[str], tag: str) -> None:
        """Log what the restore repaired, and promote the serious findings.

        A note prefixed MISSING-THEME means the export cannot look like the
        original site. That is not something to leave in a list of footnotes:
        everything downstream looks fine -- pages render, links resolve, the
        screenshots even match, because both sides use the same wrong theme.
        """
        for note in notes:
            if note.startswith("MISSING-THEME: "):
                message = note[len("MISSING-THEME: "):]
                self._log("PROBLEM", message, "ERROR")
                self.context.report.warnings.append(message)
                self._stage_failures.append(
                    {"kind": "missing-theme", "message": message, "examples": []}
                )
            else:
                self._log(tag, note)
                self.context.report.notes.append(note)

    def _release_extracted_tree(self) -> None:
        """Delete what is left of the extracted archive once it is no longer needed.

        By this point wp-content has been moved into the WordPress tree and the
        database has been imported, so the remainder is a duplicate of the
        .wpress the user still has. On a multi-gigabyte backup that is the
        single largest saving available, and it happens before rendering --
        which is when the output directory starts growing.
        """
        extracted = self.workspace.extracted
        if not extracted.exists():
            return
        try:
            freed = directory_size(extracted)
        except OSError:
            freed = 0

        remove_tree(extracted)
        extracted.mkdir(exist_ok=True)

        if freed > 1024 * 1024:
            self._log(
                "RESTORE",
                f"released {freed / 1048576:.0f} MiB of extracted files now that "
                "WordPress is assembled",
            )

    # =====================================================================
    # Stage 3: start WordPress
    # =====================================================================
    def _stage_start_wordpress(self) -> None:
        context = self.context
        self._stage(JobStatus.STARTING_WORDPRESS, "Starting the temporary WordPress server")

        port = int(urlsplit(context.base_url).port or find_free_port())
        self._verify_core()
        php = PhpServerPool(
            runtime=context.runtimes.php,
            document_root=self.workspace.wordpress,
            port=port,
            workers=_php_workers_for(self.options.render_concurrency),
        )
        php.start()
        context.php = php

        self._progress("Waiting for WordPress to respond", 0.4)
        ok, detail = php.wait_until_wordpress_responds(
            timeout=900, should_stop=self._cancel_check
        )
        self._check_cancelled()
        if not ok:
            raise ConversionFailed(
                f"the restored WordPress did not respond: {detail[:800]}",
                instructions=(
                    "This usually means the database import was incomplete, or a plugin "
                    "in the backup is fatally erroring. The job log in logs/ has the "
                    "PHP output."
                ),
            )

        self._log("WP", f"WordPress is serving on {context.base_url} ({detail})")
        self.job.stage_progress = 1.0

    # =====================================================================
    # Stage 4: URL discovery
    # =====================================================================
    def _stage_discover(self) -> None:
        context = self.context
        self._stage(JobStatus.DISCOVERING_URLS, "Discovering pages")

        manifest = fetch_manifest(context.base_url)
        context.manifest = manifest

        if manifest:
            context.site_hosts |= manifest.hosts
            context.report.theme = manifest.theme or {}
            if manifest.active_plugins:
                context.report.plugins = list(manifest.active_plugins)
            context.report.counts.update(
                {name: int(info.get("count", 0)) for name, info in manifest.post_types.items()}
            )
            self._log(
                "DISCOVER",
                f"theme {manifest.theme.get('name', 'unknown')}, "
                f"{len(manifest.post_types)} public post type(s), "
                f"{len(manifest.taxonomies)} taxonomy/taxonomies",
            )
        else:
            context.report.warnings.append(
                "The site manifest could not be read, so custom post types may have "
                "been missed. Pages found by sitemap and by following links are "
                "unaffected."
            )

        self._progress("Reading the database and sitemaps", 0.4)
        records = build_seed_list(
            context.base_url, manifest, self.options,
            server=context.mysql, database=context.database,
            table_prefix=context.table_prefix,
        )

        if len(records) > self.settings.max_urls:
            self._log(
                "DISCOVER",
                f"capping the crawl at {self.settings.max_urls:,} URLs "
                f"(found {len(records):,})",
                "WARN",
            )
            context.report.warnings.append(
                f"Discovery found {len(records):,} URLs; the crawl was capped at "
                f"{self.settings.max_urls:,}. Raise WPSC_MAX_URLS to export them all."
            )
            records = records[: self.settings.max_urls]

        added = self.store.add_urls(self.job.id, records)
        context.report.urls_discovered = added

        by_source: dict[str, int] = {}
        for record in records:
            by_source[record.source] = by_source.get(record.source, 0) + 1
        self._log(
            "DISCOVER",
            f"discovered {added:,} URLs ("
            + ", ".join(f"{count} from {source}" for source, count in sorted(by_source.items()))
            + ")",
        )
        self.job.stage_progress = 1.0

    # =====================================================================
    # Stage 5: rendering
    # =====================================================================
    async def _stage_render(self) -> None:
        context = self.context
        self._stage(JobStatus.RENDERING, "Rendering pages in Chromium")

        # A resumed job re-queues anything left mid-flight.
        requeued = self.store.reset_stuck_urls(self.job.id)
        if requeued:
            self._log("RENDER", f"re-queued {requeued} page(s) left over from a previous run")

        raw_dir = self.workspace.root / "rendered"
        raw_dir.mkdir(exist_ok=True)

        screenshot_dir = self.workspace.screenshots if self.options.screenshot_comparison else None
        visual_budget = self.options.visual_sample_limit or 10**9
        shot_count = 0
        shots_by_template: dict[str, int] = {}

        known = {record.url for record in self.store.get_urls(self.job.id)}

        async with BrowserRenderer(
            self.options,
            screenshot_dir=screenshot_dir,
            base_url=context.base_url,
            timeout_ms=self.settings.page_timeout_ms,
            retries=self.settings.page_retries,
            document_root=self.workspace.wordpress,
        ) as renderer:
            context.renderer = renderer

            for depth in range(self.options.max_crawl_depth + 1):
                pending = [
                    record for record in self.store.get_urls(self.job.id, UrlState.PENDING)
                    if record.depth == depth
                ]
                if not pending:
                    continue

                self._log("RENDER", f"depth {depth}: {len(pending)} page(s) to render")
                lock = asyncio.Lock()

                async def render_one(record: UrlRecord) -> None:
                    nonlocal shot_count
                    self._check_cancelled()

                    self.store.update_url(
                        self.job.id, record.url,
                        state=UrlState.RENDERING, attempts=record.attempts + 1,
                    )

                    if _is_raw_document(record.url):
                        await self._fetch_raw_document(record)
                        return

                    def want_screenshot(html: str, url: str = record.url) -> bool:
                        # One or two pages of each template, decided once the
                        # page has rendered: 25 near-identical product pages
                        # say less about a site than its home, contact and
                        # archive pages do.
                        nonlocal shot_count
                        if screenshot_dir is None or shot_count >= visual_budget:
                            return False
                        signature = template_signature(html)
                        is_home = urlsplit(url).path in {"", "/"}
                        if not is_home and shots_by_template.get(signature, 0) >= 2:
                            return False
                        shots_by_template[signature] = shots_by_template.get(signature, 0) + 1
                        shot_count += 1
                        return True

                    page = await renderer.render(record.url, capture_screenshots=want_screenshot)

                    if not page.ok:
                        reason = (
                            page.page_errors[0] if page.page_errors
                            else f"HTTP {page.status}"
                        )
                        self.store.update_url(
                            self.job.id, record.url,
                            state=UrlState.FAILED, http_status=page.status, error=reason[:500],
                        )
                        self._log("PAGE", f"{_short(record.url, context.base_url)} failed: {reason[:160]}", "WARN")
                        context.report.failed_urls.append({"url": record.url, "error": reason[:300]})
                        return

                    # Persist the raw DOM so processing, resume and debugging do
                    # not need the site running again.
                    output_path = context.asset_map.add_page(record.url)
                    raw_path = raw_dir / (_safe_name(record.url, context.base_url) + ".html")
                    atomic_write_bytes(raw_path, page.html.encode("utf-8", errors="surrogatepass"))
                    _save_resources(raw_path, page.resources, page.page_errors)

                    async with lock:
                        if len(context.html_samples) < 12:
                            context.html_samples.append(page.html[:200_000])
                        # Keep what the browser actually fetched: this is how
                        # JavaScript-injected assets are caught later, once the
                        # asset manager exists.
                        context.network_resources[record.url] = list(page.resources)
                        context.render_page_errors[record.url] = list(page.page_errors)
                        if page.timings:
                            context.render_timings.append(page.timings)

                    self.store.update_url(
                        self.job.id, record.url,
                        state=UrlState.RENDERED, http_status=page.status,
                        output_path=output_path, title=(page.title or "")[:250], error=None,
                    )

                    # Queue internal links for the next depth.
                    if self.options.follow_internal_links and depth < self.options.max_crawl_depth:
                        async with lock:
                            if len(known) < self.settings.max_urls:
                                new_records = filter_discovered_links(
                                    page.links, context.site_hosts,
                                    known=known, depth=depth + 1, options=self.options,
                                    base_url=context.base_url,
                                )
                                if new_records:
                                    room = self.settings.max_urls - len(known) + len(new_records)
                                    new_records = new_records[: max(0, room)]
                                    self.store.add_urls(self.job.id, new_records)

                    counts = self.store.url_counts(self.job.id)
                    total = sum(counts.values()) or 1
                    done = counts.get(str(UrlState.RENDERED), 0) + counts.get(str(UrlState.FAILED), 0)
                    self._progress(
                        f"Rendered {done}/{total}: {_short(record.url, context.base_url)}",
                        done / total,
                    )
                    self._log(
                        "PAGE",
                        f"{_short(record.url, context.base_url)} ok "
                        f"({page.duration_seconds:.1f}s"
                        + (f", retry {page.attempts - 1}" if page.attempts > 1 else "")
                        + ")",
                    )

                # Bounded concurrency is enforced inside the renderer.
                await asyncio.gather(
                    *(render_one(record) for record in pending), return_exceptions=True
                )

        counts = self.store.url_counts(self.job.id)
        context.report.urls_rendered = counts.get(str(UrlState.RENDERED), 0)
        context.report.urls_failed = counts.get(str(UrlState.FAILED), 0)
        context.report.urls_discovered = sum(counts.values())

        if context.report.urls_rendered == 0:
            raise ConversionFailed(
                "no pages could be rendered; the export would be empty",
                instructions="The job log in logs/ records why each page failed.",
            )

        self._log(
            "RENDER",
            f"rendered {context.report.urls_rendered:,} page(s), "
            f"{context.report.urls_failed} failed",
        )
        breakdown = _average_timings(context.render_timings)
        context.report.render_spread = _timing_spread(context.render_timings)
        if breakdown:
            context.report.render_timings = breakdown
            self._log(
                "RENDER",
                "average time per page: "
                + ", ".join(f"{name} {value:.1f}s" for name, value in breakdown.items()),
            )

    async def _fetch_raw_document(self, record: UrlRecord) -> None:
        """Copy a non-HTML document (sitemap, robots, feed) into the export."""
        import httpx

        context = self.context
        output_path = context.asset_map.add_page(record.url)

        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                response = await client.get(record.url)
            if response.status_code >= 400:
                raise httpx.HTTPError(f"HTTP {response.status_code}")
        except Exception as exc:
            self.store.update_url(
                self.job.id, record.url, state=UrlState.FAILED, error=str(exc)[:300]
            )
            self._log("PAGE", f"{_short(record.url, context.base_url)} failed: {exc}", "WARN")
            context.report.failed_urls.append({"url": record.url, "error": str(exc)[:300]})
            return

        body = response.content
        # Sitemaps and feeds carry absolute URLs to the render server; point
        # them at the deployed site's own paths instead.
        #
        # Every spelling, not just the canonical one. A Yoast sitemap links its
        # XSL stylesheet protocol-relatively -- href="//127.0.0.1:58770/..." --
        # which contains no "http:" and so survived a replacement of the base
        # URL alone, shipping the render server's address to the live site.
        if context.base_url:
            for variant in sorted(host_variants(context.base_url), key=len, reverse=True):
                body = body.replace(variant.encode(), b"")

        atomic_write_bytes(safe_join(self.workspace.output, output_path), body)
        self.store.update_url(
            self.job.id, record.url, state=UrlState.WRITTEN,
            http_status=response.status_code, output_path=output_path,
        )
        self._log("DOC", f"{_short(record.url, context.base_url)} copied verbatim")

    # =====================================================================
    # Stage 6: HTML generation
    # =====================================================================
    def _stage_generate_html(self) -> None:
        context = self.context
        self._stage(JobStatus.GENERATING_HTML, "Rewriting pages for static hosting")

        raw_dir = self.workspace.root / "rendered"
        records = self.store.get_urls(self.job.id, UrlState.RENDERED)

        # Every page must be registered before any is rewritten, so a link from
        # the first page to the last resolves correctly.
        for record in records:
            context.asset_map.add_page(record.url)

        context.asset_manager = AssetManager(
            context.base_url, self.workspace.output, context.asset_map, self.options,
            site_hosts=context.site_hosts,
            concurrency=self.settings.asset_concurrency or self.capacity.asset_concurrency,
            timeout=self.settings.asset_timeout_seconds,
            retries=self.settings.asset_retries,
            document_root=self.workspace.wordpress,
        )

        # Assets Chromium fetched, including anything injected by script. A
        # resumed job did not render these pages in this process, so read the
        # lists saved beside each captured page.
        for record in records:
            if record.url not in context.network_resources:
                raw_path = raw_dir / (_safe_name(record.url, context.base_url) + ".html")
                saved = _load_resources(raw_path)
                if saved:
                    context.network_resources[record.url] = saved
                if record.url not in context.render_page_errors:
                    errors = _load_page_errors(raw_path)
                    if errors:
                        context.render_page_errors[record.url] = errors
        for resources in context.network_resources.values():
            context.asset_manager.register_network_resources(resources)
        # Script chunks a page builder loads only on demand (a carousel's code
        # when a carousel scrolls into view). Pages captured before resource
        # lists were saved have no record of them at all.
        chunks = context.asset_manager.register_lazy_chunks()
        if chunks:
            self._log("ASSETS", f"included {chunks} on-demand script chunk(s) from the install")

        processor = HtmlProcessor(
            context.base_url, context.asset_map, context.asset_manager, self.options,
            site_hosts=context.site_hosts, detector=context.detector,
        )

        written = 0
        for index, record in enumerate(records):
            self._check_cancelled()

            raw_path = raw_dir / (_safe_name(record.url, context.base_url) + ".html")
            if not raw_path.is_file():
                self._log("HTML", f"missing captured DOM for {record.url}", "WARN")
                continue

            output_path = context.asset_map.page_path(record.url) or record.output_path
            if not output_path:
                continue

            # One page must never cost the whole job: a page that cannot be
            # read, rewritten or written is recorded as failed and the rest
            # carry on. The quality check then reports it.
            try:
                html = raw_path.read_text(encoding="utf-8", errors="replace")
                processed = processor.process(record.url, html, output_path)
                target = safe_join(self.workspace.output, output_path)
                atomic_write_text(target, processed.html)
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"[:300]
                self._log("HTML", f"could not write {_short(record.url, context.base_url)}: {reason}", "WARN")
                self.store.update_url(self.job.id, record.url, state=UrlState.FAILED, error=reason)
                context.report.failed_urls.append({"url": record.url, "error": reason})
                context.report.warnings.append(
                    f"The page {_short(record.url, context.base_url)} could not be written: {reason}"
                )
                continue
            written += 1

            self.store.update_url(
                self.job.id, record.url, state=UrlState.WRITTEN, output_path=output_path
            )
            context.report.external_preserved += processed.preserved_external

            if index % 10 == 0 or index == len(records) - 1:
                self._progress(
                    f"Rewrote {index + 1}/{len(records)} pages", (index + 1) / len(records)
                )

        context.report.html_files = written
        context.report.dynamic_features = context.detector.report()
        context.report.builders = reporting.detect_builders(context.html_samples)

        self._log("HTML", f"wrote {written:,} static HTML file(s)")
        if context.report.builders:
            self._log("HTML", f"page builders detected: {', '.join(context.report.builders)}")
        for feature in context.report.dynamic_features:
            self._log(
                "DYNAMIC",
                f"{feature['name']} on {feature['page_count']} page(s): {feature['limitation']}",
                "WARN",
            )

    # =====================================================================
    # Stage 7: assets
    # =====================================================================
    async def _stage_download_assets(self) -> None:
        context = self.context
        self._stage(JobStatus.DOWNLOADING_ASSETS, "Collecting assets")

        manager = context.asset_manager
        stats = await manager.download_all(progress=self._progress)

        context.report.assets_by_kind = dict(stats.by_kind)
        context.report.asset_bytes = stats.bytes_written
        context.report.asset_failures = list(stats.failures)
        context.report.external_preserved += stats.skipped_external

        self._log(
            "ASSETS",
            f"collected {stats.downloaded:,} asset(s) "
            f"({stats.from_disk:,} copied from the backup's files, "
            f"{stats.downloaded - stats.from_disk:,} generated by WordPress; "
            f"{stats.bytes_written / 1048576:.1f} MiB), {stats.failed} not found, "
            f"{stats.skipped_external:,} external reference(s) preserved",
        )
        for url, reason in stats.failures[:10]:
            self._log("ASSETS", f"missing: {_short(url, context.base_url)} ({reason})", "WARN")

    # =====================================================================
    # Extra files
    # =====================================================================
    def _write_site_extras(self) -> None:
        """robots.txt, a sitemap and a favicon for the exported site."""
        context = self.context
        output = self.workspace.output

        if self.options.generate_robots_txt and not (output / "robots.txt").exists():
            atomic_write_text(
                output / "robots.txt",
                "User-agent: *\nAllow: /\n\nSitemap: /sitemap.xml\n",
            )

        if self.options.generate_sitemap:
            pages = sorted(
                path for path in context.asset_map.pages.values() if path.endswith(".html")
            )
            entries = "\n".join(
                "  <url><loc>{}</loc></url>".format(
                    "/" + (path[: -len("index.html")] if path.endswith("index.html") else path)
                )
                for path in pages
            )
            atomic_write_text(
                output / "sitemap.xml",
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                f"{entries}\n</urlset>\n",
            )
            self._log("OUTPUT", f"generated sitemap.xml with {len(pages):,} URLs")

        # Promote a favicon to the root if the site has one but the root does not.
        favicon = output / "favicon.ico"
        if not favicon.exists():
            for candidate in output.rglob("favicon.*"):
                if candidate.is_file():
                    try:
                        shutil.copy2(candidate, favicon)
                    except OSError:
                        pass
                    break

    # =====================================================================
    # Stage 8: validation
    # =====================================================================
    async def _stage_validate(self) -> None:
        """Check the export, repair what can be repaired, and judge the rest.

        Every check that finds a file missing from the export first looks for
        it in the restored install; if it is there it is copied in and checked
        again. Only what cannot be fixed that way is reported -- as a *problem*
        when the conversion is at fault, or a *source issue* when the original
        site has the same fault.
        """
        from app.services.static_validator import ValidationReport

        context = self.context
        self._stage(JobStatus.VALIDATING, "Validating the static site")
        output, install = self.workspace.output, self.workspace.wordpress
        assessment = quality.Assessment()
        validation = None
        browser = None
        sampled_pages: list[str] = []

        if self.options.validate_links:
            self._progress("Checking links and assets", 0.05)
            validation = await asyncio.to_thread(
                validate_output, output, workers=self.capacity.check_workers
            )
            targets = [quality.output_target(m.source_file, m.reference) for m in validation.missing_assets]
            copied = quality.repair_from_install(output, install, targets)
            if copied:
                assessment.repaired += copied
                self._log("REPAIR", f"copied {len(copied)} missing file(s) in from the install")
                validation = await asyncio.to_thread(
                validate_output, output, workers=self.capacity.check_workers
            )
            context.report.validation = validation.summary()
            context.report.broken_links = [
                {"source_file": b.source_file, "reference": b.reference, "reason": b.reason}
                for b in validation.broken_links
            ]
            context.report.missing_assets = [
                {"source_file": b.source_file, "reference": b.reference, "kind": b.kind}
                for b in validation.missing_assets
            ]
            self._log(
                "VALIDATE",
                f"checked {validation.references_checked:,} references: "
                f"{validation.broken_link_count} broken link(s), "
                f"{validation.missing_asset_count} missing asset(s), "
                f"{len(validation.case_mismatches)} case mismatch(es)",
                "INFO" if validation.is_clean else "WARN",
            )
            if validation.case_mismatches:
                # Worth its own message: these resolve on Windows and 404 on
                # the Linux host the export is going to, so a clean-looking
                # conversion would ship broken images.
                examples = ", ".join(
                    f"{m.source_file}: {m.reference} ({m.reason})"
                    for m in validation.case_mismatches[:3]
                )
                self._stage_failures.append({
                    "kind": "case-mismatch",
                    "message": (
                        f"{len(validation.case_mismatches)} reference(s) differ from the "
                        "file's name only in capitalisation. They work on Windows and "
                        "will 404 on a Linux web server."
                    ),
                    "examples": [
                        f"{m.source_file}: {m.reference} ({m.reason})"
                        for m in validation.case_mismatches[:10]
                    ],
                })
                self._log("QUALITY", f"case mismatches: {examples}", "WARN")

        if self.options.check_console_errors:
            self._progress("Loading representative pages in a browser", 0.25)
            pages = sampled_pages = await asyncio.to_thread(self._representative_pages, 25, 3)
            browser = ValidationReport()
            await validate_in_browser(output, pages, self.options, browser, limit=len(pages))

            # Anything the browser asked for and did not get, but the install
            # has, is a file only JavaScript knew about. Copy it and look again.
            wanted = {self._static_target(f): f["page"] for f in browser.failed_requests
                      if f.get("status") == 404}
            copied = quality.repair_from_install(output, install, [t for t in wanted if t])
            if copied:
                assessment.repaired += copied
                affected = sorted({wanted[t] for t in copied})
                self._log(
                    "REPAIR",
                    f"copied {len(copied)} file(s) the pages requested at run time; "
                    f"re-checking {len(affected)} page(s)",
                )
                again = ValidationReport()
                await validate_in_browser(output, affected, self.options, again, limit=len(affected))
                for name in ("console_errors", "page_errors", "failed_requests"):
                    kept = [e for e in getattr(browser, name) if e["page"] not in affected]
                    setattr(browser, name, kept + getattr(again, name))

            context.report.console_errors = browser.console_errors
            if validation is not None:
                validation.console_errors = browser.console_errors
                validation.page_errors = browser.page_errors
                validation.failed_requests = browser.failed_requests
                validation.pages_checked_in_browser = browser.pages_checked_in_browser
                context.report.validation = validation.summary()
            self._log(
                "VALIDATE",
                f"browser check on {browser.pages_checked_in_browser} page(s) covering every "
                f"template: {len(browser.page_errors)} script error(s), "
                f"{len(browser.failed_requests)} failed request(s)",
                "INFO" if not browser.page_errors else "WARN",
            )

        self._progress("Comparing each page with what the browser captured", 0.45)
        sample = sampled_pages or await asyncio.to_thread(self._representative_pages, 12, 2)
        structural = await asyncio.to_thread(self._compare_structure, sample)

        self._progress("Looking for links to servers that will not exist", 0.5)
        leftovers = await asyncio.to_thread(
            quality.scan_leftovers, output, context.site_hosts,
            # A folder link is only a fault when the export was asked to name
            # its files, for opening from a disk rather than a web server.
            flag_folder_links=not self.options.folder_links,
        )

        self._assess(assessment, validation, browser, leftovers)
        if structural:
            assessment.add_problem(
                "missing-content",
                f"{len(structural)} page(s) are missing content that the browser captured",
                structural,
            )

        if self.options.screenshot_comparison:
            await self._compare_screenshots()
            low = [p for p in context.report.visual.get("lowest_scoring", [])
                   if p.get("desktop") is not None and p["desktop"] < 85]
            if low:
                context.report.warnings.append(
                    f"{len(low)} compared page(s) look noticeably different from the original "
                    "(below 85% after aligning). Open their diff images in screenshots/: "
                    + ", ".join(p["output_path"] for p in low[:5])
                )

        context.report.quality = assessment.as_dict()
        if assessment.problems:
            self._log(
                "QUALITY",
                f"completed with {len(assessment.problems)} problem(s): "
                + "; ".join(p["message"] for p in assessment.problems[:3]),
                "WARN",
            )
        else:
            self._log("QUALITY", "no conversion problems found"
                      + (f" ({len(assessment.source_issues)} issue(s) in the source site itself)"
                         if assessment.source_issues else ""))

    def _compare_structure(self, pages: list[str]) -> list[str]:
        """Check each sampled page against the DOM the browser captured.

        Links resolving and pixels matching still leave a gap: a section that
        failed to survive the rewrite, or a gallery that lost its images. This
        counts what a reader would see -- images, links, headings, list items,
        tables, forms and text -- in the captured page and in the exported one.
        """
        context = self.context
        raw_dir = self.workspace.root / "rendered"
        by_path = {
            path: url for url, path in context.asset_map.pages.items() if path.endswith(".html")
        }

        differences: list[str] = []
        for path in pages:
            url = by_path.get(path)
            exported = self.workspace.output / path
            if not url or not exported.is_file():
                continue
            captured_file = raw_dir / (_safe_name(url, context.base_url) + ".html")
            if not captured_file.is_file():
                continue
            try:
                captured = quality.structural_signature(
                    captured_file.read_text(encoding="utf-8", errors="replace")
                )
                produced = quality.structural_signature(
                    exported.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                continue
            for line in quality.compare_structure(captured, produced):
                differences.append(f"{path}: {line}")
        if differences:
            self._log(
                "VALIDATE",
                f"{len(differences)} structural difference(s) between captured and exported pages",
                "WARN",
            )
        else:
            self._log("VALIDATE", f"{len(pages)} page(s) match the captured original structurally")
        return differences

    def _static_target(self, failed: dict) -> str | None:
        """The output path a request to the validation server was for."""
        from urllib.parse import unquote

        base = failed.get("base_url") or ""
        url = failed.get("url") or ""
        if not base or not url.startswith(base + "/"):
            return None
        return unquote(urlsplit(url).path).lstrip("/") or None

    def _representative_pages(self, limit: int, per_template: int) -> list[str]:
        """Exported pages chosen to cover every template the site uses."""
        signatures: dict[str, str] = {}
        for path in sorted(set(self.context.asset_map.pages.values())):
            if not path.endswith(".html"):
                continue
            file = self.workspace.output / path
            try:
                signatures[path] = template_signature(file.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        return quality.pick_representative(signatures, limit, per_template)

    def _assess(self, assessment, validation, browser, leftovers) -> None:
        """Sort what validation left into conversion problems and source issues."""
        from app.utils.urls import url_to_output_path

        context = self.context
        install = self.workspace.wordpress
        flat = not self.options.preserve_url_structure

        # Pages WordPress itself answered 4xx for: links to them are the
        # site's own dead links, faithfully reproduced.
        dead_pages = {
            url_to_output_path(r.url, flat=flat)
            for r in self.store.get_urls(self.job.id, UrlState.FAILED)
            if r.http_status and 400 <= r.http_status < 500
        }

        if validation is not None:
            ours, theirs = [], []
            for b in validation.missing_assets:
                target = quality.output_target(b.source_file, b.reference)
                (ours if target and quality.in_install(install, target) else theirs).append(
                    f"{b.source_file}: {b.reference}")
            if ours:
                assessment.add_problem(
                    "missing-asset", f"{len(ours)} file(s) are in the backup but missing from the export", ours)
            if theirs:
                assessment.add_source_issue(
                    "missing-in-backup",
                    f"{len(theirs)} reference(s) to files that are not in the backup either "
                    "(broken on the original site too)", theirs)

            ours, theirs = [], []
            for b in validation.broken_links:
                target = quality.output_target(b.source_file, b.reference)
                (theirs if target in dead_pages else ours).append(f"{b.source_file}: {b.reference}")
            if ours:
                assessment.add_problem(
                    "broken-link", f"{len(ours)} link(s) point at pages missing from the export", ours)
            if theirs:
                assessment.add_source_issue(
                    "dead-link", f"{len(theirs)} link(s) to pages that do not exist on the original "
                    "site either", theirs)

        if browser is not None:
            ours, theirs = [], []
            for f in browser.failed_requests:
                if f.get("status") != 404:
                    continue  # aborted or blocked requests are not the export's fault
                target = self._static_target(f)
                if not target:
                    continue
                (ours if quality.in_install(install, target) else theirs).append(f"{f['page']}: {target}")
            if ours:
                assessment.add_problem(
                    "runtime-404", f"{len(ours)} file(s) requested by page scripts are missing "
                    "from the export", ours)
            if theirs:
                assessment.add_source_issue(
                    "runtime-404-source", f"{len(theirs)} file(s) requested by page scripts do not "
                    "exist in the backup", theirs)

            original_errors = {
                _normalise_error(e) for errors in context.render_page_errors.values() for e in errors
            }
            new_errors = [
                f"{e['page']}: {e['text'][:200]}" for e in browser.page_errors
                if _normalise_error(e["text"]) not in original_errors
            ]
            if new_errors:
                assessment.add_problem(
                    "script-error", f"{len(new_errors)} JavaScript error(s) in the export that the "
                    "original did not throw", new_errors)

        if leftovers.live_domain:
            assessment.add_problem(
                "live-domain", f"{len(leftovers.live_domain)} link(s) still point at the live site",
                [f"{x['file']}: {x['reference']}" for x in leftovers.live_domain])
        if leftovers.temp_server:
            assessment.add_problem(
                "temp-server", f"{len(leftovers.temp_server)} file(s) still reference the "
                "temporary render server", [f"{x['file']}: {x['reference']}" for x in leftovers.temp_server])
        if leftovers.folder_links:
            assessment.add_problem(
                "folder-link", f"{len(leftovers.folder_links)} page link(s) end in a folder instead "
                "of naming the file", [f"{x['file']}: {x['reference']}" for x in leftovers.folder_links])

    async def _compare_screenshots(self) -> None:
        context = self.context
        self._progress("Comparing screenshots", 0.7)

        candidates: list[tuple[str, str, str]] = []
        for url, output_path in sorted(context.asset_map.pages.items()):
            slug = _slug_for(url, context.base_url)
            if (self.workspace.screenshots / f"{slug}.original.desktop.png").is_file():
                candidates.append((url, output_path, slug))

        if not candidates:
            self._log("VALIDATE", "no original screenshots were captured; skipping comparison")
            return

        limit = self.options.visual_sample_limit or len(candidates)
        candidates = candidates[:limit]

        with StaticSiteServer(self.workspace.output) as server:
            validator = VisualValidator(self.workspace.screenshots, self.options)
            comparisons = await validator.compare_pages(
                server.base_url, candidates, self.options,
                progress=lambda message, fraction: self._progress(message, 0.7 + fraction * 0.3),
            )

        context.report.visual = summarise_visual(comparisons)
        self._log(
            "VALIDATE",
            f"compared {len(comparisons)} page(s): desktop "
            f"{context.report.visual.get('desktop_average', 0)}% average similarity "
            "(a diagnostic, not a pass mark)",
        )

    # =====================================================================
    # Stage 9: packaging
    # =====================================================================
    def _stage_package(self) -> Path:
        context = self.context
        self._stage(JobStatus.ZIPPING, "Packaging the static site")

        if self._stage_failures:
            context.report.quality = dict(context.report.quality or {})
            context.report.quality["problems"] = (
                list(context.report.quality.get("problems", [])) + self._stage_failures
            )

        # The report travels with the job, not inside the deliverable.
        reporting.write_report(context.report, self.workspace.report)

        name = self.options.zip_name or _zip_name_for(
            self.job.filename,
            flat=not self.options.preserve_url_structure,
            when=self.job.started_at,
        )
        # Never replace an existing ZIP: a previous run's result is the user's.
        zip_path = unique_path(self.workspace.root / name)

        result = build_zip(self.workspace.output, zip_path, progress=self._progress)

        problems = verify_zip(zip_path, source_dir=self.workspace.output)
        for problem in problems:
            self._log("ZIP", problem, "WARN")
            context.report.warnings.append(f"Packaging: {problem}")

        context.report.zip_name = zip_path.name
        context.report.zip_bytes = result.bytes_compressed
        context.report.zip_files = result.files

        self._log(
            "ZIP",
            f"packaged {result.files:,} file(s) into {zip_path.name} "
            f"({result.bytes_compressed / 1048576:.1f} MiB)",
        )

        self._publish_to_exports(zip_path)
        return zip_path

    def _publish_to_exports(self, zip_path: Path) -> None:
        """Put the finished ZIP where every job's ZIP goes.

        Ten conversions otherwise leave ten ZIPs in ten job folders named after
        job ids, which is a poor place to look for a deliverable. A hard link
        where the filesystem allows one, so collecting them costs no disk; a
        copy across volumes, where it necessarily does.
        """
        exports = self.settings.exports
        try:
            exports.mkdir(parents=True, exist_ok=True)
            published = unique_path(exports / zip_path.name)
            try:
                os.link(zip_path, published)
                how = "linked"
            except OSError:
                # Different volume, or a filesystem without hard links.
                shutil.copy2(zip_path, published)
                how = "copied"
            self.context.report.export_path = str(published)
            self.store.merge_summary(self.job.id, {"export_path": str(published)})
            self._log("ZIP", f"{how} to {published}")
        except OSError as exc:
            # The ZIP is already safe in the job workspace; failing to collect
            # a second reference to it must not fail the conversion.
            self._log("ZIP", f"could not copy the ZIP to {exports}: {exc}", "WARN")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _php_workers_for(render_concurrency: int) -> int:
    """How many PHP workers a given number of concurrent page renders needs.

    More than one per page, deliberately. A page that requests another page
    from its own site needs a *second* idle worker to serve that request; with
    exactly one worker per page every such request deadlocks. Measured on this
    machine: four self-requesting pages deadlocked with four workers and all
    completed in 0.7s with six.
    """
    return max(6, render_concurrency * 2 + 2)


def _read_configured_prefix(wordpress_root: Path) -> str:
    """Read ``$table_prefix`` out of an existing wp-config.php.

    Preferred over querying information_schema when resuming, because a server
    configured to lower-case table names (the Windows default) reports them
    that way, and a prefix such as ``SERVMASK_PREFIX_`` would come back
    mangled.
    """
    import re as _re

    config = Path(wordpress_root) / "wp-config.php"
    if not config.is_file():
        return ""
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = _re.search(r"\$table_prefix\s*=\s*['\"]([A-Za-z0-9_]*)['\"]", text)
    return match.group(1) if match else ""


def _average_timings(entries: list[dict]) -> dict[str, float]:
    """Mean seconds per phase across every rendered page."""
    if not entries:
        return {}
    totals: dict[str, float] = {}
    for entry in entries:
        for name, value in entry.items():
            totals[name] = totals.get(name, 0.0) + float(value)
    average = {name: round(total / len(entries), 2) for name, total in totals.items()}
    average["total"] = round(sum(average.values()), 2)
    return average


def _timing_spread(entries: list[dict]) -> dict[str, float]:
    """How page times are distributed, not just their mean.

    A mean of ten seconds can be five hundred steady pages or four hundred
    fast ones and a hundred that time out, and the two want opposite fixes:
    the first is the site, the second is a handful of pages. The mean cannot
    tell them apart, so record the shape as well.
    """
    totals = sorted(sum(float(v) for v in entry.values()) for entry in entries)
    if not totals:
        return {}

    def at(fraction: float) -> float:
        return round(totals[min(len(totals) - 1, int(len(totals) * fraction))], 2)

    slowest = totals[int(len(totals) * 0.9):]
    return {
        "pages": len(totals),
        "fastest": round(totals[0], 2),
        "median": at(0.5),
        "p90": at(0.9),
        "slowest": round(totals[-1], 2),
        # What finishing the slow tail early would actually be worth, which is
        # the number that decides whether chasing it is worth anyone's time.
        "slowest_tenth_share": round(100 * sum(slowest) / sum(totals), 1) if totals else 0.0,
    }


def _normalise_error(text: str) -> str:
    """A JavaScript error with addresses and line numbers removed, so the same
    error thrown by the original and by the export compares equal."""
    import re

    text = re.sub(r"https?://[^\s)'\"]+", "<url>", text or "")
    return re.sub(r":\d+(:\d+)?", "", text).strip()[:300]


def _resources_path(raw_path: Path) -> Path:
    return raw_path.with_name(raw_path.stem + ".resources.json")


def _save_resources(raw_path: Path, resources, page_errors=()) -> None:
    """Keep what the browser fetched, and what the page threw, beside its DOM.

    The errors matter as much as the files: an export is only at fault for a
    JavaScript error the *original* page did not also throw, and without this
    a resumed job has nothing to compare against and reports the site's own
    errors as conversion problems.
    """
    import json

    payload = {
        "resources": [
            {"url": r.url, "status": r.status, "content_type": r.content_type,
             "resource_type": r.resource_type, "failed": r.failed}
            for r in resources
        ],
        "page_errors": list(page_errors or []),
    }
    try:
        atomic_write_text(_resources_path(raw_path), json.dumps(payload))
    except OSError as exc:
        logger.warning("could not save the capture record for %s: %s", raw_path.name, exc)


def _read_capture(raw_path: Path) -> dict:
    import json

    path = _resources_path(raw_path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("could not read %s: %s", path.name, exc)
        return {}
    # Files written before page errors were saved hold a bare list.
    if isinstance(data, list):
        return {"resources": data, "page_errors": []}
    return data if isinstance(data, dict) else {}


def _load_resources(raw_path: Path) -> list:
    from app.services.browser_renderer import NetworkResource

    rows = _read_capture(raw_path).get("resources") or []
    try:
        return [NetworkResource(**row) for row in rows]
    except TypeError as exc:
        logger.warning("unusable resource list beside %s: %s", raw_path.name, exc)
        return []


def _load_page_errors(raw_path: Path) -> list[str]:
    return [str(e) for e in (_read_capture(raw_path).get("page_errors") or [])]


def _is_raw_document(url: str) -> bool:
    """Whether the URL should be fetched byte-for-byte instead of rendered.

    Chromium does not hand back an XML document when asked for its DOM: it
    returns the HTML of its own built-in XML viewer. Capturing a sitemap that
    way would replace valid XML with a page of viewer markup, so these are
    fetched over plain HTTP and written through unchanged.

    The list of extensions lives with the other URL rules, so discovery and
    rendering cannot disagree about what a document is -- two copies of one
    rule is how mariadb-dump ended up being looked for in the wrong place.
    """
    return is_raw_document(url)


def _short(url: str, base_url: str) -> str:
    """A URL trimmed to its site-relative path, for readable log lines."""
    if base_url and url.startswith(base_url):
        return url[len(base_url):] or "/"
    return url


def _safe_name(url: str, base_url: str) -> str:
    """Stable filename for a captured DOM."""
    return _slug_for(url, base_url)


def _site_url_replacements(plain: dict[str, str]) -> dict[str, str]:
    r"""The URL replacements to apply to the database, longest first.

    Page builders store their layouts as JSON, where every slash is escaped:
    Elementor keeps a footer link as ``https:\/\/example.com\/contact-us\/``.
    Replacing only the plain spelling leaves every such link pointing at the
    live domain, so each replacement is also applied in its escaped form.
    """
    replacements = dict(plain)
    for search, replace in plain.items():
        replacements[search.replace("/", "\\/")] = replace.replace("/", "\\/")
    return dict(sorted(replacements.items(), key=lambda kv: len(kv[0]), reverse=True))


def _zip_name_for(filename: str, *, flat: bool = False, when: float | None = None) -> str:
    """ZIP name: which backup, which page layout, and which run produced it.

    The run's date and time are in the name because the same backup is
    converted more than once -- after a fix, with different options, to compare
    a change -- and two runs of the same site must not produce two files called
    the same thing. Sorting the export folder by name then also sorts it by
    when each ZIP was made.
    """
    stem = Path(filename).stem or "website"
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(when if when else time.time()))
    return f"{stem}-static{'-flat' if flat else ''}-{stamp}.zip"
