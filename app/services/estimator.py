"""How long a batch will take, and why.

Two separate questions, deliberately kept apart:

* **Sizing** -- how many conversions run at once and how many pages each
  renders. This is arithmetic on cores, memory and free disk, and it is the
  same arithmetic a real batch uses, because ``run_batch`` imports it from
  here. A planner that answers differently from the run it is planning is
  worse than no planner.
* **Timing** -- how long that takes. This is an *extrapolation* from one
  measured conversion, and it is only as good as its assumptions. Every one of
  them is a field of :class:`Workload` rather than a number buried in a
  formula, so the interface can show them and the user can argue with them.

The measured baseline is the aungmetals conversion: 622 pages, 2.9 GB backup,
1 h 47 m on a 4-thread Intel i3-2100. Rendering was 57.5 minutes of it with 5
pages in flight, which is where ``SECONDS_PER_PAGE`` comes from, and the other
50 minutes are extraction, the database import and restore, HTML generation,
asset collection, checking and zipping.
"""

from __future__ import annotations

import math
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.utils.capacity import Capacity, derive, measure

_GIB = 1024 ** 3

# --------------------------------------------------------------------- sizing

#: A conversion's working set is the extracted WordPress, the generated site
#: and the ZIP: about three times the backup it started from.
DISK_PER_BACKUP = 3

#: Never plan to use the last of the disk. Filling it mid-render loses every
#: hour a job has spent, and takes its neighbours down with it.
DISK_RESERVE = 10 * _GIB

#: One conversion runs its own database, PHP pool and browser. 3.6 GB is the
#: measured working set; 8 GB leaves room for a page-builder site's peaks.
MEMORY_PER_JOB = 8 * _GIB

#: However many cores a machine has, more than this at once stops helping:
#: each conversion is mostly one WordPress being driven through its pages.
MAX_PARALLEL = 8

# --------------------------------------------------------------------- timing

#: Core-seconds of rendering work for one page, from the measured conversion:
#: 57.5 minutes with 5 pages in flight, over 622 pages.
SECONDS_PER_PAGE = 27.7

#: Minutes per site outside rendering: extract 4, restore 24, start 4, HTML 9,
#: assets 2, checks 4, zip 2. Little of it scales with pages.
FIXED_MINUTES = 50.0

#: The reference machine everything above was measured on. A server with
#: faster cores does the same work in less time; ``cpu_speed`` says how much.
REFERENCE = "Intel i3-2100 (4 threads, 2011), Windows"


#: Machines worth comparing against. Sized to what a host actually sells,
#: because the question behind the comparison is always "is the bigger one
#: worth it?" -- and until the render ceiling scaled with the machine, the
#: honest answer above sixteen cores was no.
SERVER_PRESETS: tuple[tuple[str, int, int], ...] = (
    ("4 vCPU / 16 GB", 4, 16),
    ("8 vCPU / 32 GB", 8, 32),
    ("16 vCPU / 64 GB", 16, 64),
    ("32 vCPU / 128 GB", 32, 128),
    ("64 vCPU / 256 GB", 64, 256),
)


@dataclass
class Machine:
    """A machine to plan for: this one, or one being considered."""

    cpus: int
    memory_gb: float
    free_disk_gb: float
    measured: bool = False
    """True when these came from the host rather than from a form."""
    available_gb: float = 0.0
    """Memory free right now; 0 means "assume all of it", which is right for a
    server being considered and wrong for a PC with a browser open. How many
    pages render at once depends on it, so a conversion here uses five where a
    fresh machine of the same size would use six."""

    @classmethod
    def here(cls, jobs_dir: Path | None = None) -> "Machine":
        capacity = measure()
        try:
            free = shutil.disk_usage(jobs_dir or Path.cwd()).free
        except OSError:
            free = 0
        return cls(
            cpus=capacity.cpus,
            memory_gb=round(capacity.total_memory / _GIB, 1),
            free_disk_gb=round(free / _GIB, 1),
            measured=True,
            available_gb=round(capacity.available_memory / _GIB, 1),
        )

    def capacity(self) -> Capacity:
        return derive(self.cpus, int(self.memory_gb * _GIB),
                      int(self.available_gb * _GIB))


@dataclass
class Workload:
    """What is being converted, and the assumptions used to time it."""

    sites: int = 10
    pages_per_site: int = 622
    backup_gb: float = 2.9

    #: 1.0 is the reference machine. A current server core is roughly 1.5-2x.
    cpu_speed: float = 1.0
    seconds_per_page: float = SECONDS_PER_PAGE
    fixed_minutes: float = FIXED_MINUTES

    #: 0 means "work it out"; anything else overrides the sizing.
    parallel: int = 0
    pages_at_once: int = 0

    screenshots: bool = True


@dataclass
class Estimate:
    """The answer, with enough of the working shown to be argued with."""

    parallel: int
    pages_per_job: int
    pages_in_flight: int
    limit: str

    render_minutes: float
    fixed_minutes: float
    per_site_minutes: float
    waves: int
    total_minutes: float

    disk_needed_gb: float
    disk_free_gb: float

    by_cpu: int
    by_memory: int
    by_disk: int
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["total_hours"] = round(self.total_minutes / 60, 2)
        return data


def size_batch(machine: Capacity, free_disk: int, largest_backup: int,
               parallel: int = 0, pages: int = 0) -> tuple[int, int, dict[str, int]]:
    """How many conversions at once, and how many pages each.

    Returns ``(parallel, pages_per_job, limits)``. Disk is the constraint that
    is easy to forget and expensive to get wrong: every conversion checks free
    space for itself when it starts, so four starting together all see the
    *same* free space and all agree there is room -- then fill the drive an
    hour in, when each of them has an hour of work to lose.
    """
    by_cpu = max(1, machine.cpus // 4)

    # Free memory on a busy machine, but never less than half of it: a server
    # momentarily holding cache should not halve an overnight batch.
    budget = max(machine.available_memory, machine.total_memory * 0.5) - 4 * _GIB
    by_memory = max(1, int(budget // MEMORY_PER_JOB)) if machine.total_memory else by_cpu

    if largest_backup and free_disk:
        by_disk = int((free_disk - DISK_RESERVE) // (largest_backup * DISK_PER_BACKUP))
    else:
        by_disk = by_cpu

    chosen = parallel or max(1, min(by_cpu, by_memory, max(by_disk, 1), MAX_PARALLEL))

    # Each conversion measures the whole machine when it starts, so without a
    # share every job in a batch claims the machine's full page budget: four
    # jobs each rendering twelve pages is forty-eight browsers on one box, and
    # every site ends up slower. Give each job a slice of the cores and of the
    # page budget, and never fewer than two pages.
    #
    # One conversion shares with nobody, so the sharing rule must not apply to
    # it: capping a lone job at cores-per-job gave four pages on a four-core
    # machine where a real run used five, and the calculator then predicted
    # 2 h 2 m for the conversion that had actually taken 1 h 47 m.
    if pages:
        per_job = pages
    elif chosen == 1:
        per_job = max(2, machine.render_concurrency)
    else:
        per_job = max(2, min(6,
                             max(1, machine.render_concurrency // chosen),
                             max(1, machine.cpus // chosen)))

    return chosen, per_job, {"cpu": by_cpu, "memory": by_memory, "disk": by_disk}


def estimate(machine: Machine, work: Workload) -> Estimate:
    """Time a batch on a machine. Neither has to exist."""
    capacity = machine.capacity()
    free_disk = int(machine.free_disk_gb * _GIB)
    largest = int(work.backup_gb * _GIB)

    parallel, per_job, limits = size_batch(
        capacity, free_disk, largest, work.parallel, work.pages_at_once
    )
    # You cannot run four conversions at once when there is one backup. Without
    # this, a single site on a big machine was given a quarter of the page
    # budget and predicted far slower than it would really be -- the sharing
    # rule applied to a job with nobody to share with.
    capped_by_count = False
    if not work.parallel and work.sites and parallel > work.sites:
        parallel, per_job, limits = size_batch(
            capacity, free_disk, largest, work.sites, work.pages_at_once
        )
        capped_by_count = True

    speed = max(0.1, work.cpu_speed)
    render = work.pages_per_site * work.seconds_per_page / per_job / 60 / speed
    fixed = work.fixed_minutes / speed
    if not work.screenshots:
        # Measured at roughly a tenth of a job: the capture itself, plus the
        # second pass at mobile width and the image comparison.
        render *= 0.93
        fixed *= 0.93

    per_site = render + fixed
    # Sites of equal size run in clean waves, not a smooth stream: with ten
    # sites and four slots the last wave carries two, and the other two slots
    # stand idle. Dividing the total by four would quietly promise time that
    # the schedule cannot deliver.
    waves = max(1, math.ceil(work.sites / parallel)) if work.sites else 0
    total = waves * per_site

    smallest = min(limits["cpu"], limits["memory"], limits["disk"])
    if work.parallel:
        limit = f"set by hand to {work.parallel}"
    elif capped_by_count:
        limit = f"only {work.sites} backup(s) to convert"
    elif limits["cpu"] == smallest:
        limit = "limited by CPU cores"
    elif limits["memory"] == smallest:
        limit = "limited by memory"
    else:
        limit = "limited by free disk"

    warnings: list[str] = []
    disk_needed = work.backup_gb * DISK_PER_BACKUP * parallel
    if limits["disk"] < 1 and machine.free_disk_gb:
        # It may still "fit" arithmetically while leaving nothing spare, which
        # is how a batch dies at three in the morning.
        warnings.append(
            f"Only {machine.free_disk_gb:.0f} GB is free where jobs are written. "
            f"One conversion of a {work.backup_gb:.1f} GB backup needs about "
            f"{work.backup_gb * DISK_PER_BACKUP:.0f} GB, with nothing to spare."
        )
    if machine.free_disk_gb and disk_needed > machine.free_disk_gb:
        warnings.append(
            f"{parallel} conversions at once need about {disk_needed:.0f} GB while "
            f"running, and only {machine.free_disk_gb:.0f} GB is free. Jobs would "
            f"fail part-way."
        )
    if work.parallel and work.parallel > min(limits["memory"], limits["disk"]):
        warnings.append(
            f"{work.parallel} at once is more than this machine's memory or disk "
            f"supports ({min(limits['memory'], limits['disk'])})."
        )
    if parallel * per_job > capacity.cpus * 2:
        warnings.append(
            f"{parallel * per_job} pages rendering on {capacity.cpus} core(s): they "
            f"will take turns, so the real time will be longer than this estimate."
        )
    if machine.measured and work.cpu_speed != 1.0:
        warnings.append(
            "Speeds are scaled by hand; the estimate no longer matches this machine."
        )

    return Estimate(
        parallel=parallel,
        pages_per_job=per_job,
        pages_in_flight=parallel * per_job,
        limit=limit,
        render_minutes=round(render, 1),
        fixed_minutes=round(fixed, 1),
        per_site_minutes=round(per_site, 1),
        waves=waves,
        total_minutes=round(total, 1),
        disk_needed_gb=round(disk_needed, 1),
        disk_free_gb=machine.free_disk_gb,
        by_cpu=limits["cpu"],
        by_memory=limits["memory"],
        by_disk=limits["disk"],
        warnings=warnings,
    )


def compare(work: Workload, here: Machine | None = None,
            free_disk_gb: float = 500.0) -> list[dict]:
    """The same workload on machines of several sizes.

    Answers the question a single estimate cannot: whether a bigger server is
    worth buying. Each row is a real estimate, not a scaled copy of the one
    above it -- that is what makes a ceiling visible when two different
    machines come back with the same time.

    ``this machine`` is always timed at its measured speed. Applying a chosen
    core-speed multiplier to the machine the measurements came from would be
    comparing it against itself and calling the difference a gain.
    """
    rows: list[dict] = []

    def add(label: str, machine: Machine, speed: float, measured: bool) -> None:
        scoped = Workload(**{**work.__dict__, "cpu_speed": speed})
        result = estimate(machine, scoped)
        rows.append({
            "label": label,
            "cpus": machine.cpus,
            "memory_gb": machine.memory_gb,
            "measured": measured,
            "pages_per_job": result.pages_per_job,
            "parallel": result.parallel,
            "pages_in_flight": result.pages_in_flight,
            "per_site_minutes": result.per_site_minutes,
            "total_minutes": result.total_minutes,
            "total_hours": round(result.total_minutes / 60, 2),
            "limit": result.limit,
        })

    if here:
        add("this machine", here, 1.0, True)

    for label, cpus, memory_gb in SERVER_PRESETS:
        if here and here.cpus == cpus and abs(here.memory_gb - memory_gb) < 2:
            continue                      # already the row above
        add(label, Machine(cpus=cpus, memory_gb=memory_gb,
                           free_disk_gb=free_disk_gb), work.cpu_speed, False)

    return rows
