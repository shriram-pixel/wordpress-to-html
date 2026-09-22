"""Two measured adjustments: progress weights, and the asset cache size.

Neither changes what a conversion produces. The weights decide only what the
progress bar and the estimate of time remaining show; the cache decides how
much of the restored site is held in memory while rendering rather than
re-read from disk.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.job import STAGE_WEIGHTS, JobStatus  # noqa: E402
from app.services import browser_renderer as br  # noqa: E402

# Measured shares of a whole conversion, from two real sites.
AUNGMETALS = {
    JobStatus.EXTRACTING: 0.040, JobStatus.RESTORING: 0.227,
    JobStatus.STARTING_WORDPRESS: 0.038, JobStatus.DISCOVERING_URLS: 0.002,
    JobStatus.RENDERING: 0.539, JobStatus.GENERATING_HTML: 0.080,
    JobStatus.DOWNLOADING_ASSETS: 0.022, JobStatus.VALIDATING: 0.037,
    JobStatus.ZIPPING: 0.014,
}


def test_the_weights_still_add_up_to_a_whole_job():
    """A bar that reaches 80% or 120% is worse than no bar."""
    assert sum(STAGE_WEIGHTS.values()) == pytest.approx(1.0, abs=0.001)


def test_every_stage_has_a_weight():
    for status in JobStatus:
        if status.is_terminal or status is JobStatus.QUEUED:
            continue
        assert status in STAGE_WEIGHTS, f"{status} would silently score zero"


def test_no_stage_is_off_by_more_than_a_factor_of_three():
    """The old values had restoring at 0.07 for a stage taking 0.23, and
    assets at 0.20 for one taking 0.02 -- a tenfold error in each direction."""
    for stage, measured in AUNGMETALS.items():
        weight = STAGE_WEIGHTS[stage]
        if measured < 0.01:
            continue                      # discovery is noise either way
        ratio = weight / measured
        assert 0.33 <= ratio <= 3.0, f"{stage}: weight {weight} vs measured {measured}"


def test_rendering_dominates_as_it_does_in_reality():
    assert STAGE_WEIGHTS[JobStatus.RENDERING] > 0.5
    assert STAGE_WEIGHTS[JobStatus.RENDERING] == max(STAGE_WEIGHTS.values())


def test_restoring_outweighs_collecting_assets():
    """It did not, which is why the bar crawled and then jumped."""
    assert STAGE_WEIGHTS[JobStatus.RESTORING] > STAGE_WEIGHTS[JobStatus.DOWNLOADING_ASSETS]


# ------------------------------------------------------------ asset cache ---

@pytest.mark.parametrize("total_gb, expected_mb", [
    (4, 192),      # below the floor: unchanged from the fixed size
    (8, 192),
    (16, 328),     # this developer machine
    (32, 655),
    (64, 1024),    # the ceiling
    (256, 1024),
])
def test_the_cache_scales_with_memory_but_is_bounded(monkeypatch, total_gb, expected_mb):
    monkeypatch.setattr(br, "memory_bytes",
                        lambda: (total_gb * 1024 ** 3, total_gb * 1024 ** 3))

    assert round(br.default_asset_cache_bytes() / 1048576) == expected_mb


def test_an_unreadable_memory_probe_falls_back_to_the_old_fixed_size(monkeypatch):
    monkeypatch.setattr(br, "memory_bytes", lambda: (0, 0))

    assert br.default_asset_cache_bytes() == br._ASSET_CACHE_FLOOR


def test_the_cache_is_never_larger_than_the_ceiling(monkeypatch):
    monkeypatch.setattr(br, "memory_bytes", lambda: (2048 * 1024 ** 3, 0))

    assert br.default_asset_cache_bytes() == br._ASSET_CACHE_CEILING


def test_an_explicit_size_still_wins(tmp_path):
    """Callers -- and tests -- must be able to pin it."""
    files = br._DiskFiles(tmp_path, max_cached_bytes=1234)

    assert files.max_cached_bytes == 1234


def test_a_file_is_served_and_then_cached(tmp_path):
    """The behaviour the size governs, unchanged on either platform."""
    (tmp_path / "style.css").write_bytes(b"body{color:red}")

    files = br._DiskFiles(tmp_path)

    class Request:
        method = "GET"
        url = "http://127.0.0.1:9/style.css"

    first = files.lookup(Request(), "http://127.0.0.1:9")
    assert first is not None
    body, content_type = first
    assert body == b"body{color:red}"
    assert content_type.startswith("text/css")     # charset varies by platform
    assert files.lookup(Request(), "http://127.0.0.1:9") == first
    assert files._cached_bytes == len(b"body{color:red}")
