"""The time calculator must agree with the run it is predicting.

The sizing half is shared with ``run_batch`` on purpose: a planner that says
"four at a time" while the batch runs two is worse than no planner, because
someone will plan an overnight window around it. The timing half is an
extrapolation from a single measured conversion, so what is tested here is
that it is *consistent and honest* -- that it scales the way the machine does,
warns when it is guessing, and never silently promises time the schedule
cannot deliver.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.estimator import (  # noqa: E402
    Estimate,
    Machine,
    Workload,
    estimate,
)


def desktop() -> Machine:
    return Machine(cpus=4, memory_gb=16, free_disk_gb=500)


def server(cpus: int = 16, memory_gb: int = 64) -> Machine:
    return Machine(cpus=cpus, memory_gb=memory_gb, free_disk_gb=500)


def test_one_site_reproduces_the_conversion_it_was_calibrated_from():
    """aungmetals: 622 pages in 107 minutes, with 5 pages rendering at once.

    The machine is described as it was that day -- 16 GB installed and about
    6 GB free -- because how many pages render at once depends on free memory,
    and that is what makes it five rather than six.
    """
    that_day = Machine(cpus=4, memory_gb=15.9, free_disk_gb=500, available_gb=6.1)
    result = estimate(that_day, Workload(sites=1))

    assert result.pages_per_job == 5, "the concurrency the run actually used"
    assert result.total_minutes == pytest.approx(107, abs=2), result.total_minutes
    assert result.parallel == 1, "a 4-core desktop converts one site at a time"


def test_a_single_conversion_is_not_held_to_a_sharing_rule():
    """Nothing to share with, so cores-per-job must not cap it.

    It did, and the calculator said 2 h 2 m for the run that took 1 h 47 m.
    """
    alone = estimate(desktop(), Workload(sites=1))

    assert alone.pages_per_job == desktop().capacity().render_concurrency
    assert alone.pages_per_job > desktop().cpus // 4


def test_but_a_batch_still_shares_the_machine():
    """The fix above must not hand every job the whole machine again."""
    shared = estimate(server(16, 64), Workload(sites=10))

    assert shared.parallel == 4
    assert shared.pages_per_job == 4, "a slice, not the whole page budget"
    assert shared.pages_in_flight <= 16


def test_more_cores_means_more_at_once_and_less_time():
    small = estimate(server(8, 32), Workload(sites=10))
    large = estimate(server(32, 128), Workload(sites=10))

    assert large.parallel > small.parallel
    assert large.total_minutes < small.total_minutes


def test_ten_identical_sites_run_in_rounds_not_a_smooth_stream():
    """Ten sites in four slots is three rounds, and the last carries two.

    Dividing ten by four would promise 2.5 rounds of time that the schedule
    cannot deliver -- the sites are the same size, so nothing staggers.
    """
    result = estimate(server(16, 64), Workload(sites=10))

    assert result.parallel == 4
    assert result.waves == 3
    assert result.total_minutes == pytest.approx(3 * result.per_site_minutes, rel=0.01)


def test_faster_cores_scale_the_whole_estimate():
    same = estimate(server(), Workload(sites=4, cpu_speed=1.0))
    faster = estimate(server(), Workload(sites=4, cpu_speed=2.0))

    assert faster.total_minutes == pytest.approx(same.total_minutes / 2, rel=0.01)
    assert faster.parallel == same.parallel, "core speed is not core count"


def test_fewer_pages_costs_less_rendering_but_the_same_restore():
    small = estimate(server(), Workload(sites=1, pages_per_site=50))
    big = estimate(server(), Workload(sites=1, pages_per_site=622))

    assert small.render_minutes < big.render_minutes
    assert small.fixed_minutes == big.fixed_minutes, "restoring is not per page"


def test_a_small_disk_narrows_the_batch_and_says_so():
    cramped = Machine(cpus=32, memory_gb=128, free_disk_gb=40)
    result = estimate(cramped, Workload(sites=10, backup_gb=4))

    assert result.parallel == 2, "40 GB less the reserve holds two 12 GB jobs"
    assert result.limit == "limited by free disk"


def test_a_full_disk_warns_rather_than_quietly_predicting_success():
    full = Machine(cpus=16, memory_gb=64, free_disk_gb=5)
    result = estimate(full, Workload(sites=3, backup_gb=3))

    assert result.warnings
    assert any("fail part-way" in w for w in result.warnings)


def test_overriding_parallel_is_obeyed_and_questioned():
    result = estimate(Machine(cpus=16, memory_gb=64, free_disk_gb=40),
                      Workload(sites=10, backup_gb=4, parallel=8))

    assert result.parallel == 8
    assert result.limit == "set by hand to 8"
    assert any("more than this machine" in w for w in result.warnings)


def test_oversubscribing_the_cores_is_flagged_not_rewarded():
    """20 pages on 4 cores is arithmetic, not physics: say so."""
    result = estimate(desktop(), Workload(sites=1, parallel=5, pages_at_once=4))

    assert result.pages_in_flight == 20
    assert any("take turns" in w for w in result.warnings)


def test_dropping_screenshots_saves_a_little_not_a_lot():
    with_shots = estimate(server(), Workload(sites=1))
    without = estimate(server(), Workload(sites=1, screenshots=False))

    saved = 1 - without.total_minutes / with_shots.total_minutes
    assert 0.03 < saved < 0.12, f"expected a modest saving, got {saved:.0%}"


def test_the_calculator_and_the_batch_size_a_run_identically():
    """The whole point of sharing the code: they cannot drift apart."""
    import run_batch
    from app.utils.capacity import derive

    machine = derive(16, 64 * 1024 ** 3)
    backups = [Path(f"s{i}.wpress") for i in range(10)]

    run_batch._backup_size = lambda _: 3 * 1024 ** 3
    run_batch.shutil.disk_usage = lambda _: type(
        "U", (), {"total": 0, "used": 0, "free": 500 * 1024 ** 3}
    )()
    plan = run_batch.plan_batch(machine, Path("."), backups)

    predicted = estimate(Machine(cpus=16, memory_gb=64, free_disk_gb=500),
                         Workload(sites=10, backup_gb=3))

    assert (plan.parallel, plan.per_job) == (predicted.parallel, predicted.pages_per_job)


def test_an_estimate_serialises_for_the_interface():
    result = estimate(server(), Workload(sites=10))
    data = result.as_dict()

    assert isinstance(result, Estimate)
    assert data["total_hours"] == pytest.approx(data["total_minutes"] / 60, rel=0.01)
    assert set(data) >= {"parallel", "pages_per_job", "per_site_minutes", "warnings"}


# ----------------------------------------------------------- comparison ---
# A single estimate cannot answer "is the bigger server worth it?". The
# comparison can, and it is the reason the render ceiling had to scale: until
# it did, a 64-core machine returned the same time as a 16-core one.

def test_comparison_covers_a_range_of_servers():
    from app.services.estimator import SERVER_PRESETS, compare

    rows = compare(Workload(sites=10))

    assert len(rows) == len(SERVER_PRESETS)
    assert [r["label"] for r in rows] == [p[0] for p in SERVER_PRESETS]


def test_bigger_servers_are_genuinely_faster():
    """The regression that matters: two sizes must not return one time."""
    from app.services.estimator import compare

    times = [r["total_minutes"] for r in compare(Workload(sites=10))]

    assert times == sorted(times, reverse=True), "more machine must mean less time"
    assert len(set(times)) == len(times), "no two sizes may give the same answer"


def test_this_machine_leads_the_comparison_and_is_marked():
    from app.services.estimator import compare

    rows = compare(Workload(sites=10), desktop())

    assert rows[0]["label"] == "this machine"
    assert rows[0]["measured"] is True
    assert all(r["measured"] is False for r in rows[1:])


def test_this_machine_is_never_re_timed_at_an_invented_speed():
    """Its time is measured. Scaling it would compare it against itself.

    Without this the popup showed two different totals for one machine: the
    headline scaled, the comparison row not.
    """
    from app.services.estimator import compare

    slow = compare(Workload(sites=4, cpu_speed=1.0), desktop())[0]
    fast = compare(Workload(sites=4, cpu_speed=2.0), desktop())[0]

    assert slow["total_minutes"] == fast["total_minutes"]


def test_the_chosen_speed_does_apply_to_the_servers_compared():
    from app.services.estimator import compare

    slow = compare(Workload(sites=4, cpu_speed=1.0), desktop())[1]
    fast = compare(Workload(sites=4, cpu_speed=2.0), desktop())[1]

    assert fast["total_minutes"] == pytest.approx(slow["total_minutes"] / 2, rel=0.01)


def test_the_comparison_agrees_with_a_direct_estimate():
    """The row for a machine must equal asking about that machine."""
    from app.services.estimator import compare

    row = next(r for r in compare(Workload(sites=10)) if r["label"] == "16 vCPU / 64 GB")
    direct = estimate(Machine(cpus=16, memory_gb=64, free_disk_gb=500),
                      Workload(sites=10))

    assert row["total_minutes"] == direct.total_minutes
    assert row["parallel"] == direct.parallel


# ------------------------------------------------- the interface's sizing ---
# Selecting ten backups in the browser must convert them as fast as the same
# ten from the command line. They used to disagree: the web app allowed two
# jobs on any machine with eight cores, whatever its size.

def test_the_interface_and_the_batch_agree_on_how_many_jobs_at_once():
    from app.services.estimator import size_batch
    from app.utils.capacity import derive

    for cpus, memory_gb in ((4, 16), (8, 32), (16, 64), (32, 128)):
        machine = derive(cpus, memory_gb * 1024 ** 3)

        # What app/main.py works out for the web interface, which has no
        # batch to measure and so leaves disk out of it.
        web, web_pages, _ = size_batch(machine, free_disk=0, largest_backup=0)
        # What run_batch.py works out with plenty of disk.
        cli, cli_pages, _ = size_batch(
            machine, free_disk=500 * 1024 ** 3, largest_backup=3 * 1024 ** 3)

        assert web == cli, f"{cpus} cores: interface {web}, command line {cli}"
        assert web_pages == cli_pages


def test_parallel_jobs_in_the_interface_scale_past_two():
    """The old rule returned two on a 32-core server and on an 8-core one."""
    from app.services.estimator import size_batch
    from app.utils.capacity import derive

    small, _, _ = size_batch(derive(8, 32 * 1024 ** 3), 0, 0)
    large, _, _ = size_batch(derive(32, 128 * 1024 ** 3), 0, 0)

    assert small == 2
    assert large == 8, "a 32-core server must run more than two conversions"
