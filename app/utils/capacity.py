"""Work out how much of this machine a conversion may use.

Every stage of a conversion has a different bottleneck, so one "number of
workers" does not fit all of them:

* **Rendering** runs Chromium pages. Each one costs real memory -- on a
  page-builder site with large images, several hundred megabytes at peak --
  and saturates a core while it lays the page out. Memory is the binding
  constraint, not cores.
* **PHP workers** are mostly idle, waiting on the database. They are cheap,
  and there must be more of them than there are pages rendering, because a
  page routinely requests another page from the same site while it renders.
* **HTML generation and link checking** are CPU-bound parsing, so they scale
  with cores.
* **Collecting assets** is disk work; a few more workers than cores keeps the
  drive busy without thrashing it.

The numbers below are deliberately conservative: a conversion that exhausts
memory takes the machine down with it, and the user is usually working on the
same computer.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_GIB = 1024 ** 3

#: Left for the operating system, the browser the user is working in, and the
#: job's own database and PHP workers.
_RESERVED_BYTES = 3 * _GIB

#: Peak memory of one Chromium page rendering a heavy page-builder page.
_BYTES_PER_RENDER = 600 * 1024 * 1024


def cpu_count() -> int:
    """Usable processors, honouring any CPU affinity this process was given."""
    try:
        if hasattr(os, "sched_getaffinity"):
            return max(1, len(os.sched_getaffinity(0)))
    except OSError:
        pass
    return max(1, os.cpu_count() or 1)


def memory_bytes() -> tuple[int, int]:
    """``(total, available)`` physical memory in bytes; zeros when unknown."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class _Status(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys), int(status.ullAvailPhys)
        else:
            page_size = os.sysconf("SC_PAGE_SIZE")
            total = os.sysconf("SC_PHYS_PAGES") * page_size
            available = total
            try:
                with open("/proc/meminfo", encoding="utf-8") as handle:
                    for line in handle:
                        if line.startswith("MemAvailable:"):
                            available = int(line.split()[1]) * 1024
                            break
            except OSError:
                pass
            return int(total), int(available)
    except Exception as exc:  # any probe failure falls back to "unknown"
        logger.debug("could not read memory size: %s", exc)
    return 0, 0


@dataclass(frozen=True)
class Capacity:
    """How many workers each stage may use on this machine."""

    cpus: int
    total_memory: int
    available_memory: int
    render_concurrency: int
    php_workers: int
    asset_concurrency: int
    html_workers: int
    check_workers: int
    parallel_jobs: int

    def describe(self) -> str:
        # Spelled out, because "15.9 GiB RAM (5.9 GiB free)" reads like disk
        # space to anyone watching a job that is filling a drive.
        return (
            f"{self.cpus} CPU(s), {self.total_memory / _GIB:.1f} GiB memory of which "
            f"{self.available_memory / _GIB:.1f} GiB is free (this is memory, not disk): "
            f"{self.render_concurrency} page(s) at a time, {self.php_workers} PHP worker(s), "
            f"{self.html_workers} HTML worker(s), {self.asset_concurrency} asset worker(s)"
        )


def _clamp(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, value)))


def measure(*, max_render: int = 12) -> Capacity:
    """Measure the machine and derive a worker count for each stage."""
    cpus = cpu_count()
    total, available = memory_bytes()

    # Rendering: whichever of cores and memory runs out first. Chromium pages
    # overlap network waits, so slightly more pages than cores is still a win.
    by_cpu = cpus * 1.5
    if total or available:
        # Whatever is free right now, but never less than 40% of the machine:
        # other applications release memory as the job runs, and a PC that is
        # momentarily busy should not throttle an hour of work.
        budget = max(available, total * 0.4) - _RESERVED_BYTES
        by_memory = max(1.0, budget / _BYTES_PER_RENDER)
    else:
        by_memory = by_cpu
    render = _clamp(min(by_cpu, by_memory), 1, max_render)

    return Capacity(
        cpus=cpus,
        total_memory=total,
        available_memory=available,
        render_concurrency=render,
        # Enough that a page requesting another page always finds a free
        # worker, plus headroom for the assets each page pulls.
        php_workers=_clamp(render * 2 + 2, 6, 24),
        asset_concurrency=_clamp(cpus * 4, 8, 32),
        html_workers=_clamp(cpus, 1, 8),
        check_workers=_clamp(cpus, 2, 8),
        # Two conversions at once only on a machine with cores and memory to
        # spare: each job runs its own database, PHP pool and browser.
        parallel_jobs=2 if cpus >= 8 and total >= 32 * _GIB else 1,
    )
