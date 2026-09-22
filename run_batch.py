"""Convert every ``.wpress`` backup in a folder, several at a time.

One site failing must never stop the batch, and a batch that has already run
must be cheap to resume: both are what separate an overnight run from a
babysitting exercise. Each conversion is a separate ``convert.py`` process, so
a crash costs one site rather than the run, and each writes its own log.

    python run_batch.py /srv/backups -o /srv/exports --parallel 4
    python run_batch.py /srv/backups -o /srv/exports --parallel 4 --retry

Results land in ``summary.csv`` next to the exports: one row per site with its
pages, problems, timing and ZIP path, so the ones needing attention are
obvious without reading nine logs.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import get_settings  # noqa: E402
from app.services import estimator  # noqa: E402
from app.utils.capacity import Capacity, measure  # noqa: E402
from app.utils.filesystem import human_bytes  # noqa: E402

_print_lock = threading.Lock()

_GIB = 1024 ** 3

# The sizing rules live with the estimator, so the planner in the web
# interface answers with the numbers this script will actually use.
_DISK_PER_BACKUP = estimator.DISK_PER_BACKUP
_DISK_RESERVE = estimator.DISK_RESERVE
_MEMORY_PER_JOB = estimator.MEMORY_PER_JOB


def say(message: str) -> None:
    with _print_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


@dataclass
class Result:
    backup: str
    status: str = "pending"
    seconds: float = 0.0
    pages: int = 0
    problems: int = 0
    source_issues: int = 0
    visual_desktop: float = 0.0
    zip_path: str = ""
    job_id: str = ""
    detail: str = ""


@dataclass
class Batch:
    results: list[Result] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, result: Result) -> None:
        with self.lock:
            self.results.append(result)


@dataclass
class Plan:
    """How many conversions to run at once, and why that many."""

    parallel: int
    per_job: int
    by_cpu: int
    by_memory: int
    by_disk: int
    free_disk: int
    largest_backup: int
    requested: bool = False

    @property
    def limit(self) -> str:
        if self.requested:
            return "asked for with --parallel"
        # Ties name the cheapest thing to believe: cores are a fact, free disk
        # is a guess about what the run will need.
        smallest = min(self.by_cpu, self.by_memory, self.by_disk)
        if self.by_cpu == smallest:
            return "limited by CPU cores"
        if self.by_memory == smallest:
            return "limited by memory"
        return "limited by free disk"

    def describe(self) -> str:
        return (f"{self.limit} (cores allow {self.by_cpu}, memory {self.by_memory}, "
                f"disk {self.by_disk}: {human_bytes(self.free_disk)} free, "
                f"{human_bytes(self.largest_backup * _DISK_PER_BACKUP)} needed per job)")

    @property
    def warning(self) -> str:
        """The sentence to print when the plan is about to hurt, else ''."""
        if self.by_disk < 1:
            return (f"only {human_bytes(self.free_disk)} free where jobs are written, and the "
                    f"largest backup needs about "
                    f"{human_bytes(self.largest_backup * _DISK_PER_BACKUP)}. Free space or "
                    f"point --jobs-dir at a bigger disk, or jobs will fail part-way.")
        if self.requested and self.parallel > min(self.by_memory, self.by_disk):
            worry = "disk" if self.by_disk < self.by_memory else "memory"
            return (f"--parallel {self.parallel} is more than this machine's {worry} supports "
                    f"({min(self.by_memory, self.by_disk)}); jobs may fail part-way.")
        return ""


def _backup_size(path: Path) -> int:
    """Bytes, or 0 for a backup that cannot be read -- that is one site's
    failure later, and must not stop the batch being planned now."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def plan_batch(machine: Capacity, jobs_dir: Path, backups: list[Path],
               requested: int = 0, pages: int = 0) -> Plan:
    """Decide how many conversions run at once, from cores, memory and disk.

    Disk is the constraint that is easy to forget and expensive to get wrong.
    Each conversion checks for its own free space when it starts, but four
    starting together each see the *same* free space and all agree there is
    room -- then fill the drive an hour in, when every one of them has work to
    lose. Sizing the batch against the largest backup, once, avoids that.
    """
    largest = max((_backup_size(b) for b in backups), default=0)
    try:
        free = shutil.disk_usage(jobs_dir).free
    except OSError:
        free = 0

    parallel, per_job, limits = estimator.size_batch(
        machine, free, largest, requested, pages
    )

    return Plan(parallel=parallel, per_job=per_job, by_cpu=limits["cpu"],
                by_memory=limits["memory"], by_disk=limits["disk"],
                free_disk=free, largest_backup=largest, requested=bool(requested))


def find_backups(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    return sorted(p for p in source.glob("*.wpress") if p.is_file())


def already_done(backup: Path, exports: Path) -> Path | None:
    """The ZIP a previous run produced for this backup, if any."""
    stem = backup.stem
    for candidate in sorted(exports.glob(f"{stem}-static*.zip")):
        if candidate.stat().st_size > 0:
            return candidate
    return None


def latest_report(jobs_dir: Path, backup: Path, started: float) -> dict:
    """The conversion report written by this run, if one was written."""
    newest, newest_time = None, started - 60
    for report in jobs_dir.glob("*/report/conversion-report.json"):
        try:
            stamp = report.stat().st_mtime
        except OSError:
            continue
        if stamp > newest_time:
            try:
                data = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("input_filename") == backup.name:
                newest, newest_time = data, stamp
    return newest or {}


def convert(backup: Path, exports: Path, jobs_dir: Path, extra: list[str], log_dir: Path) -> Result:
    result = Result(backup=backup.name)
    started = time.monotonic()
    wall = time.time()
    log_file = log_dir / f"{backup.stem}.log"

    command = [
        sys.executable, str(Path(__file__).with_name("convert.py")), str(backup),
        "--jobs-dir", str(jobs_dir), "-o", str(exports), *extra,
    ]
    try:
        # Inside the try: a backup that has become unreadable is this site's
        # failure, not the batch's. Left outside, it escapes future.result()
        # and ends the run with a traceback.
        say(f"start  {backup.name} ({backup.stat().st_size / 1073741824:.2f} GB)")
        with log_file.open("wb") as log:
            completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
        result.status = "converted" if completed.returncode == 0 else "failed"
        result.detail = "" if completed.returncode == 0 else f"exit code {completed.returncode}"
    except Exception as exc:  # the batch continues whatever one site does
        result.status = "failed"
        result.detail = f"{type(exc).__name__}: {exc}"

    result.seconds = round(time.monotonic() - started, 1)

    report = latest_report(jobs_dir, backup, wall)
    if report:
        quality = report.get("quality") or {}
        result.job_id = report.get("job_id", "")
        result.pages = report.get("urls_rendered", 0)
        result.problems = len(quality.get("problems", []))
        result.source_issues = len(quality.get("source_issues", []))
        result.visual_desktop = (report.get("visual") or {}).get("desktop_average", 0.0)
        name = report.get("zip_name") or ""
        if name:
            result.zip_path = str(exports / name)
    if result.status == "converted" and result.problems:
        result.status = "converted with problems"

    say(f"{result.status:22} {backup.name} in {result.seconds / 60:.1f} min"
        + (f" - {result.detail}" if result.detail else ""))
    return result


def write_summary(path: Path, results: list[Result]) -> None:
    rows = sorted((asdict(r) for r in results), key=lambda r: r["backup"])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(Result("x").__dict__))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="folder of .wpress backups, or one file")
    parser.add_argument("-o", "--output", type=Path, required=True, help="where the ZIPs go")
    parser.add_argument("--jobs-dir", type=Path, help="job workspaces (default: from .env)")
    parser.add_argument("--parallel", type=int, default=0,
                        help="conversions at once. Default: measured from this machine")
    parser.add_argument("--pages", type=int, default=0, metavar="N",
                        help="pages each conversion renders at once. Default: this "
                             "machine's page budget divided between the conversions")
    parser.add_argument("--retry", action="store_true",
                        help="convert sites that already have a ZIP again")
    parser.add_argument("--flat", action="store_true", help="one file per page (about.html)")
    parser.add_argument("--file-links", action="store_true",
                        help="write links as contact-us/index.html")
    parser.add_argument("--screenshots", action="store_true",
                        help="capture and compare screenshots (slower)")
    args = parser.parse_args(argv)

    settings = get_settings()
    jobs_dir = (args.jobs_dir or settings.jobs_dir).expanduser()
    exports = args.output.expanduser()
    exports.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    log_dir = exports / "logs"
    log_dir.mkdir(exist_ok=True)

    backups = find_backups(args.source.expanduser())
    if not backups:
        print(f"no .wpress files in {args.source}", file=sys.stderr)
        return 2

    pending, skipped = [], []
    for backup in backups:
        existing = None if args.retry else already_done(backup, exports)
        if existing:
            skipped.append(Result(backup=backup.name, status="skipped (already converted)",
                                  zip_path=str(existing)))
        else:
            pending.append(backup)

    machine = measure()
    plan = plan_batch(machine, jobs_dir, pending, args.parallel, args.pages)

    extra: list[str] = ["-c", str(plan.per_job)]
    if args.flat:
        extra.append("--flat")
    if args.file_links:
        extra.append("--file-links")
    if args.screenshots:
        extra.append("--screenshots")

    print(f"\n  {len(backups)} backup(s); {len(pending)} to convert, {len(skipped)} already done")
    print(f"  {machine.describe()}")
    print(f"  running {plan.parallel} conversion(s) at a time, {plan.per_job} page(s) each "
          f"({plan.parallel * plan.per_job} browser page(s) in total)")
    print(f"  {plan.describe()}")
    if plan.warning:
        print(f"\n  WARNING: {plan.warning}")
    print()

    batch = Batch(results=list(skipped))
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=plan.parallel) as pool:
            futures = {
                pool.submit(convert, backup, exports, jobs_dir, extra, log_dir): backup
                for backup in pending
            }
            for future in as_completed(futures):
                batch.add(future.result())
                write_summary(exports / "summary.csv", batch.results)
    except KeyboardInterrupt:
        say("stopping: finishing the conversions already running")

    write_summary(exports / "summary.csv", batch.results)

    done = [r for r in batch.results if r.status.startswith("converted")]
    problems = [r for r in batch.results if r.problems]
    failed = [r for r in batch.results if r.status == "failed"]

    print(f"\n  finished in {(time.monotonic() - started) / 60:.0f} min: "
          f"{len(done)} converted, {len(failed)} failed, {len(skipped)} skipped")
    if problems:
        print("\n  needs a look:")
        for r in problems:
            print(f"    {r.backup}: {r.problems} problem(s), {r.pages} pages")
    if failed:
        print("\n  failed:")
        for r in failed:
            print(f"    {r.backup}: {r.detail} (log: {log_dir / (Path(r.backup).stem + '.log')})")
    print(f"\n  summary: {exports / 'summary.csv'}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
