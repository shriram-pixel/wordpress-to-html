"""A batch must size itself to the machine, disk included.

Cores and memory are the obvious limits and were always checked. Disk is the
one that bites: each conversion checks free space for itself when it starts,
so four conversions starting together all see the same free space, all agree
there is room, and then fill the drive an hour later -- when each of them has
an hour of work to lose. The sizing therefore happens once, for the batch,
against the largest backup in it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.utils.capacity import Capacity  # noqa: E402
from run_batch import _DISK_RESERVE, _GIB, plan_batch  # noqa: E402


def machine(cpus: int = 16, memory_gib: int = 64, render: int = 12) -> Capacity:
    return Capacity(
        cpus=cpus,
        total_memory=memory_gib * _GIB,
        available_memory=int(memory_gib * 0.9) * _GIB,
        render_concurrency=render,
        php_workers=24,
        asset_concurrency=32,
        html_workers=8,
        check_workers=8,
        parallel_jobs=2,
    )


@pytest.fixture
def backups(tmp_path, monkeypatch):
    """Empty files that report the sizes asked for.

    Writing real multi-gigabyte files to test arithmetic would be slower than
    the conversions themselves, so the size lookup is stubbed instead.
    """
    sizes: dict[Path, int] = {}
    monkeypatch.setattr("run_batch._backup_size", lambda path: sizes.get(path, 0))

    def make(*sizes_gib: float) -> list[Path]:
        made = []
        for index, size in enumerate(sizes_gib):
            path = tmp_path / f"site-{index}.wpress"
            path.touch()
            sizes[path] = int(size * _GIB)
            made.append(path)
        return made

    return make


def fake_disk(monkeypatch, free_gib: float) -> None:
    import shutil

    usage = shutil.disk_usage(Path.cwd())
    monkeypatch.setattr(
        "run_batch.shutil.disk_usage",
        lambda _: type(usage)(usage.total, usage.total - int(free_gib * _GIB),
                              int(free_gib * _GIB)),
    )


def test_cores_decide_on_a_machine_with_room_to_spare(tmp_path, monkeypatch, backups):
    fake_disk(monkeypatch, 500)
    plan = plan_batch(machine(cpus=16), tmp_path, backups(3, 3, 3))

    assert plan.parallel == 4            # 16 cores // 4
    assert plan.limit == "limited by CPU cores"
    assert not plan.warning


def test_a_small_disk_narrows_the_batch(tmp_path, monkeypatch, backups):
    # 40 GB free, less the 10 GB reserve, against a 4 GB backup needing 12 GB:
    # room for two conversions, however many cores the machine has.
    fake_disk(monkeypatch, 40)
    plan = plan_batch(machine(cpus=32), tmp_path, backups(4, 2, 2))

    assert plan.by_disk == 2
    assert plan.parallel == 2
    assert plan.limit == "limited by free disk"


def test_the_largest_backup_sets_the_disk_need(tmp_path, monkeypatch, backups):
    """Not the average: the queue may well run the biggest ones together."""
    fake_disk(monkeypatch, 100)
    plan = plan_batch(machine(), tmp_path, backups(0.5, 0.5, 8))

    assert plan.largest_backup == 8 * _GIB
    assert plan.by_disk == int((100 * _GIB - _DISK_RESERVE) // (8 * _GIB * 3))


def test_little_memory_narrows_the_batch(tmp_path, monkeypatch, backups):
    fake_disk(monkeypatch, 500)
    plan = plan_batch(machine(cpus=32, memory_gib=16), tmp_path, backups(2))

    assert plan.parallel == 1
    assert plan.limit == "limited by memory"


def test_a_full_disk_warns_rather_than_promising_a_run(tmp_path, monkeypatch, backups):
    fake_disk(monkeypatch, 11)   # one gigabyte past the reserve
    plan = plan_batch(machine(), tmp_path, backups(3))

    assert plan.by_disk == 0
    assert plan.parallel == 1, "still attempt one, so the failure is a real message"
    assert "free" in plan.warning and "--jobs-dir" in plan.warning


def test_an_explicit_parallel_is_obeyed_but_questioned(tmp_path, monkeypatch, backups):
    fake_disk(monkeypatch, 40)
    plan = plan_batch(machine(), tmp_path, backups(4), requested=6)

    assert plan.parallel == 6, "the operator's number wins"
    assert "more than this machine's disk supports" in plan.warning


# One conversion at a time gets the machine's whole page budget: there is
# nothing to share it with. Two or more get a slice each.
@pytest.mark.parametrize("parallel, expected_pages", [(1, 12), (2, 6), (3, 4), (4, 3), (8, 2)])
def test_each_job_gets_a_share_of_the_page_budget(tmp_path, monkeypatch, backups,
                                                  parallel, expected_pages):
    fake_disk(monkeypatch, 500)
    plan = plan_batch(machine(cpus=16, render=12), tmp_path, backups(1),
                      requested=parallel)

    assert plan.per_job == expected_pages
    assert plan.parallel * plan.per_job <= 24, "never more browsers than the box can hold"


def test_no_backups_left_to_convert_still_plans(tmp_path, monkeypatch):
    """A re-run where everything is already done must not divide by zero."""
    fake_disk(monkeypatch, 500)
    plan = plan_batch(machine(), tmp_path, [])

    assert plan.parallel >= 1 and plan.per_job >= 2


# --------------------------------------------------------------- scaling ---
# The render ceiling used to be a flat 12 however big the machine was, so a
# 64-core server rendered no more pages at once than a 16-core one and the
# extra cores were bought for nothing.

def test_a_desktop_is_still_held_to_twelve():
    """The cap exists to keep a PC usable while it works. That still holds."""
    from app.utils import capacity

    assert capacity._DESKTOP_RENDER_CAP == 12
    small = plan_for_machine(capacity, cpus=4, memory_gib=16)
    assert small.render_concurrency <= 12


def test_a_big_server_is_allowed_more_than_twelve():
    from app.utils import capacity

    big = plan_for_machine(capacity, cpus=32, memory_gib=128)
    assert big.render_concurrency > 12, "a 32-core server must use more than a desktop"
    assert big.render_concurrency <= capacity._RENDER_CEILING


def test_the_ceiling_still_bounds_an_enormous_machine():
    from app.utils import capacity

    huge = plan_for_machine(capacity, cpus=256, memory_gib=1024)
    assert huge.render_concurrency == capacity._RENDER_CEILING


def plan_for_machine(capacity, cpus: int, memory_gib: int):
    import unittest.mock as mock

    with mock.patch.object(capacity, "cpu_count", lambda: cpus), \
         mock.patch.object(capacity, "memory_bytes",
                           lambda: (memory_gib * _GIB, memory_gib * _GIB)):
        return capacity.measure()


def test_pages_per_job_can_be_overridden(tmp_path, monkeypatch, backups):
    """--pages: the operator has measured something the formula cannot see."""
    fake_disk(monkeypatch, 500)
    plan = plan_batch(machine(cpus=16), tmp_path, backups(3), requested=4, pages=8)

    assert plan.per_job == 8, "an explicit page count wins over the share"
    assert plan.parallel == 4
