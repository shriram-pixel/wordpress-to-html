from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import ConversionOptions, ExternalResourcePolicy, get_settings  # noqa: E402
from app.models.job import JobStatus, JobStore  # noqa: E402
from app.services.pipeline import ConversionFailed, ConversionPipeline  # noqa: E402
from app.services.runtime_provisioner import RuntimeUnavailable  # noqa: E402
from app.utils.filesystem import human_bytes, unique_path  # noqa: E402


_STAGE_ORDER = [
    "EXTRACTING", "RESTORING", "STARTING_WORDPRESS", "DISCOVERING_URLS", "RENDERING",
    "GENERATING_HTML", "DOWNLOADING_ASSETS", "VALIDATING", "ZIPPING",
]


class ProgressReporter:
    """Print the current stage, its progress and time estimates while a job runs.

    Polls the job store from a background thread, so the pipeline needs no
    knowledge of the terminal. A line is printed whenever the stage changes and
    every ``interval`` seconds in between.
    """

    def __init__(self, store: JobStore, job_id: str, interval: float = 15.0) -> None:
        import threading

        self.store = store
        self.job_id = job_id
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="progress", daemon=True)

    def __enter__(self) -> "ProgressReporter":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        from app.models.job import STAGE_LABELS, format_duration

        last_stage = None
        last_print = 0.0
        while not self._stop.wait(2.0):
            try:
                job = self.store.get(self.job_id)
            except Exception:
                continue  # the store may be closing; never break the conversion
            if job is None or job.status.is_terminal:
                continue

            stage = str(job.status)
            now = time.monotonic()
            changed = stage != last_stage
            if not changed and now - last_print < self.interval:
                continue

            if changed:
                position = _STAGE_ORDER.index(stage) + 1 if stage in _STAGE_ORDER else 0
                print()
                print(f"  ==> [{position}/{len(_STAGE_ORDER)}] {STAGE_LABELS.get(stage, stage)}")
                last_stage = stage

            detail = (job.stage_detail or "").split(":", 1)[0].strip()
            print(
                f"      {int(job.stage_progress * 100):>3}% of stage"
                + (f"  |  {detail[:48]}" if detail else "")
                + f"  |  stage ETA {format_duration(job.stage_eta_seconds)}"
                + f"  |  job ~{format_duration(job.eta_seconds)} left"
                + f"  |  elapsed {format_duration(job.run_elapsed_seconds)}",
                flush=True,
            )
            last_print = now


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="convert.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "archive", type=Path, nargs="?",
        help="path to the .wpress backup (omit when using --resume)",
    )
    parser.add_argument(
        "--resume", metavar="JOB_ID",
        help="continue a job whose pages are already rendered, skipping extraction, "
             "the database import and rendering",
    )
    parser.add_argument(
        "--resume-port", type=int, metavar="PORT",
        help="the port the pages were originally rendered against; the captured HTML "
             "contains absolute URLs to it, so it must match",
    )
    parser.add_argument(
        "--archive", type=Path, metavar="WPRESS", dest="resume_archive",
        help="with --resume: the original .wpress, used to check the restored install and "
             "put back any file missing from it",
    )
    parser.add_argument(
        "--rerender", action="store_true",
        help="with --resume: render every page again instead of reusing captured pages",
    )
    parser.add_argument(
        "-o", "--output", type=Path,
        help="where to put the finished ZIP (default: inside the job workspace)",
    )

    layout = parser.add_argument_group("page layout")
    choice = layout.add_mutually_exclusive_group()
    choice.add_argument(
        "--folders", dest="folders", action="store_const", const=True,
        help="(default) one folder per page: about/index.html, keeping the site's exact "
             "URLs on a web server",
    )
    choice.add_argument(
        "--flat", dest="folders", action="store_const", const=False,
        help="one file per page instead: about.html",
    )

    layout.add_argument(
        "--file-links", dest="folder_links", action="store_false",
        help="write links as contact-us/index.html instead of contact-us/, so the "
             "export also works opened straight from a disk rather than a web server",
    )

    content = parser.add_argument_group("what to export")
    content.add_argument("--tags", action="store_true", help="include tag archives")
    content.add_argument("--authors", action="store_true", help="include author archives")
    content.add_argument("--dates", action="store_true", help="include date archives")
    content.add_argument("--feeds", action="store_true", help="include RSS feeds")
    content.add_argument(
        "--no-follow", action="store_true",
        help="do not follow internal links found while crawling",
    )
    content.add_argument(
        "--depth", type=int, default=3, metavar="N", help="maximum crawl depth (default 3)",
    )

    rendering = parser.add_argument_group("rendering and assets")
    rendering.add_argument(
        "-c", "--concurrency", type=int, default=0, metavar="N",
        help="browser pages rendered at once. Default: measured from this "
             "machine's cores and memory",
    )
    rendering.add_argument(
        "--no-media", action="store_true",
        help="do not download video and audio; usually the largest saving on disk",
    )
    rendering.add_argument(
        "--no-documents", action="store_true", help="do not download linked PDFs and documents",
    )
    rendering.add_argument(
        "--external", choices=[p.value for p in ExternalResourcePolicy], default="preserve",
        help="what to do with third-party resources (default preserve)",
    )

    validation = parser.add_argument_group("validation")
    validation.add_argument(
        "--screenshots", action="store_true",
        help="capture and compare screenshots (slow; off by default here)",
    )
    validation.add_argument("--no-validate", action="store_true", help="skip all validation")

    workspace = parser.add_argument_group("workspace")
    workspace.add_argument(
        "--jobs-dir", type=Path,
        help="where to put the job workspace; use a drive with room for ~3x the backup",
    )
    workspace.add_argument(
        "--keep", action="store_true",
        help="(default) keep the temporary WordPress and database after finishing",
    )
    workspace.add_argument(
        "--cleanup", action="store_true",
        help="after a SUCCESSFUL run, delete the temporary WordPress and database to "
             "reclaim disk; failed runs are always kept so they can be resumed",
    )
    workspace.add_argument("-q", "--quiet", action="store_true", help="only warnings and errors")
    return parser


def options_from_args(args: argparse.Namespace) -> ConversionOptions:
    return ConversionOptions(
        preserve_url_structure=args.folders is not False,
        folder_links=args.folder_links,
        include_tags=args.tags,
        include_author_archives=args.authors,
        include_date_archives=args.dates,
        include_feeds=args.feeds,
        follow_internal_links=not args.no_follow,
        max_crawl_depth=args.depth,
        render_concurrency=args.concurrency,
        download_media=not args.no_media,
        download_documents=not args.no_documents,
        external_policy=ExternalResourcePolicy(args.external),
        screenshot_comparison=args.screenshots,
        mobile_validation=args.screenshots,
        validate_links=not args.no_validate,
        check_console_errors=not args.no_validate,
    )


def configure_logging(quiet: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("  %(message)s"))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.WARNING if quiet else logging.INFO)

    # The pipeline's own commentary is the useful part; everything else is noise.
    for noisy in ("httpx", "httpcore", "urllib3", "PIL", "asyncio", "playwright"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for chatty in (
        "app.services.wordpress_runner", "app.services.runtime_provisioner",
        "app.services.wpress_extractor", "app.services.asset_manager",
        "app.services.url_discovery", "app.services.static_validator",
    ):
        logging.getLogger(chatty).setLevel(logging.WARNING)


def resume_job(args: argparse.Namespace) -> int:
    """Finish a job that already has its pages captured on disk."""
    settings = get_settings()
    if args.jobs_dir:
        settings.jobs_dir = args.jobs_dir.expanduser().resolve()
        settings.database_path = settings.jobs_dir / "jobs.sqlite3"
    settings.keep_job_workspace = True
    settings.ensure_directories()

    store = JobStore(settings.database_path)
    job = store.get(args.resume)
    if job is None:
        print(f"error: no job {args.resume} in {settings.database_path}", file=sys.stderr)
        store.close()
        return 2

    port = args.resume_port
    if not port:
        rendered = settings.job_dir(job.id) / "rendered"
        if rendered.is_dir() and any(rendered.glob("*.html")):
            print("error: --resume-port is required (the port the pages were rendered against)",
                  file=sys.stderr)
            store.close()
            return 2
        # Stopped before its first page: nothing depends on the old port.
        from app.services.wordpress_runner import find_free_port
        port = find_free_port()

    if args.concurrency:
        job.options.render_concurrency = args.concurrency

    print()
    print(f"  resuming job {job.id}  ({job.filename})")
    print(f"  workspace: {settings.job_dir(job.id)}")
    print(f"  rendered against: http://127.0.0.1:{port}")
    print()

    started = time.monotonic()
    pipeline = ConversionPipeline(job, store, settings)
    summary: dict = {}
    try:
        with ProgressReporter(store, job.id):
            zip_path = pipeline.run_resume(
                port=port,
                archive=args.resume_archive.expanduser().resolve() if args.resume_archive else None,
                rerender=args.rerender,
                folders=args.folders,
            )
        record = store.get(job.id)
        summary = record.summary if record else {}
    except Exception as exc:
        print()
        print(f"  Resume failed: {type(exc).__name__}: {exc}")
        print(f"  Log: {pipeline.workspace.log_file}")
        return 1
    finally:
        store.close()

    if args.output:
        destination = args.output.expanduser().resolve()
        # A path without a suffix is a folder, whether or not it exists yet.
        # Treating a missing one as a file name once saved a ZIP as a file
        # literally called "exports".
        if destination.is_dir() or (not destination.exists() and not destination.suffix):
            destination.mkdir(parents=True, exist_ok=True)
            destination = destination / zip_path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Never replace a ZIP from an earlier run; add -2, -3, ... instead.
        destination = unique_path(destination)
        shutil.move(str(zip_path), str(destination))
        zip_path = destination

    _print_summary(summary, zip_path, pipeline.workspace.report, time.monotonic() - started)
    return 0


def _print_summary(summary: dict, zip_path: Path, report_dir: Path, elapsed: float) -> None:
    print()
    print("  " + "-" * 62)
    print(f"  Done in {elapsed:.0f}s")
    print()
    print(f"    Pages           {summary.get('pages', 0)}")
    print(f"    Assets          {summary.get('assets', 0)}")
    print(f"    Broken links    {summary.get('broken_links', 0)}")
    print(f"    Missing assets  {summary.get('missing_assets', 0)}")
    print(f"    Console errors  {summary.get('console_errors', 0)}")
    print(f"    Repaired        {summary.get('repaired', 0)} file(s) copied in from the backup")
    print(f"    Source issues   {summary.get('source_issues', 0)} (broken on the original site too)")
    if summary.get("problems"):
        print(f"  !! COMPLETED WITH {summary['problems']} PROBLEM(S) -- see the report's Quality check")

    features = summary.get("dynamic_features") or []
    if features:
        print()
        print("    Needs a server (preserved visually, will not function):")
        for name in features:
            print(f"      - {name}")

    print()
    print(f"  ZIP     {zip_path}  ({human_bytes(zip_path.stat().st_size)})")
    print(f"  Report  {report_dir / 'conversion-report.html'}")
    print()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.quiet)
    from app.utils.console import disable_quick_edit

    # A click in the window would otherwise pause the whole job.
    disable_quick_edit()

    if args.resume:
        return resume_job(args)

    if args.archive is None:
        print("error: give a .wpress path, or --resume JOB_ID", file=sys.stderr)
        return 2

    archive = args.archive.expanduser().resolve()
    if not archive.is_file():
        print(f"error: no such file: {archive}", file=sys.stderr)
        return 2
    if archive.suffix.lower() != ".wpress":
        print(
            f"warning: {archive.name} does not end in .wpress; continuing anyway",
            file=sys.stderr,
        )

    settings = get_settings()
    if args.jobs_dir:
        settings.jobs_dir = args.jobs_dir.expanduser().resolve()
        settings.database_path = settings.jobs_dir / "jobs.sqlite3"
    # Nothing is deleted unless asked for. Keeping the workspace is what makes a
    # stopped or failed job resumable instead of a full rerun.
    settings.keep_job_workspace = not args.cleanup
    settings.ensure_directories()

    size = archive.stat().st_size
    free = shutil.disk_usage(settings.jobs_dir).free

    print()
    print(f"  {archive.name}  ({human_bytes(size)})")
    print(f"  workspace: {settings.jobs_dir}")
    print(f"  free disk: {human_bytes(free)}")

    # The archive is read in place, so the working set is the WordPress tree,
    # the generated site and the ZIP -- about three times the backup.
    needed = size * 3
    if free < needed:
        print()
        print(f"  error: this needs about {human_bytes(needed)} of free space "
              f"and only {human_bytes(free)} is available.")
        print( "         Point --jobs-dir at a drive with more room, or pass --no-media")
        print( "         if the site's video and audio do not need to be exported.")
        print()
        return 1
    print()

    store = JobStore(settings.database_path)
    options = options_from_args(args)
    job = store.create(archive.name, options, size)

    started = time.monotonic()
    pipeline = ConversionPipeline(job, store, settings)
    summary: dict = {}

    try:
        with ProgressReporter(store, job.id):
            zip_path = pipeline.run(archive)
        # Read the summary while the store is still open; the finally block
        # below closes it.
        record = store.get(job.id)
        summary = record.summary if record else {}
    except (ConversionFailed, RuntimeUnavailable) as exc:
        print()
        print(f"  Conversion failed: {exc}")
        instructions = getattr(exc, "instructions", "")
        if instructions:
            print()
            for line in str(instructions).splitlines():
                print(f"    {line}")
        print()
        print(f"  Log: {pipeline.workspace.log_file}")
        print()
        return 1
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        return 130
    except Exception as exc:
        print(f"\n  Conversion failed: {type(exc).__name__}: {exc}")
        print(f"  Log: {pipeline.workspace.log_file}")
        return 1
    finally:
        store.close()

    if args.output:
        destination = args.output.expanduser().resolve()
        # A path without a suffix is a folder, whether or not it exists yet.
        # Treating a missing one as a file name once saved a ZIP as a file
        # literally called "exports".
        if destination.is_dir() or (not destination.exists() and not destination.suffix):
            destination.mkdir(parents=True, exist_ok=True)
            destination = destination / zip_path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Never replace a ZIP from an earlier run; add -2, -3, ... instead.
        destination = unique_path(destination)
        # A move within a volume is free; across volumes shutil falls back to
        # copying, which is why --output defaults to staying put.
        shutil.move(str(zip_path), str(destination))
        zip_path = destination

    report = pipeline.workspace.report / "conversion-report.html"

    print()
    print("  " + "-" * 62)
    print(f"  Done in {time.monotonic() - started:.0f}s")
    print()
    print(f"    Pages           {summary.get('pages', 0)}")
    print(f"    Assets          {summary.get('assets', 0)}")
    print(f"    Broken links    {summary.get('broken_links', 0)}")
    print(f"    Missing assets  {summary.get('missing_assets', 0)}")
    print(f"    Console errors  {summary.get('console_errors', 0)}")
    print(f"    Repaired        {summary.get('repaired', 0)} file(s) copied in from the backup")
    print(f"    Source issues   {summary.get('source_issues', 0)} (broken on the original site too)")
    if summary.get("problems"):
        print(f"  !! COMPLETED WITH {summary['problems']} PROBLEM(S) -- see the report's Quality check")

    features = summary.get("dynamic_features") or []
    if features:
        print()
        print("    Needs a server (preserved visually, will not function):")
        for name in features:
            print(f"      - {name}")

    print()
    print(f"  ZIP     {zip_path}  ({human_bytes(zip_path.stat().st_size)})")
    print(f"  Report  {report}")
    print()
    return 0 if job.status is not JobStatus.FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
