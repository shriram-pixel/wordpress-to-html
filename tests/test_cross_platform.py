"""The two things that behaved differently on Linux and on Windows.

Both were found by auditing rather than by running, because nothing here has
run on Linux yet. Both are now made to behave identically on either platform,
which is the only way a test on one of them means anything about the other.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.static_validator import validate_output  # noqa: E402


def site(tmp_path: Path, page: str, files: dict[str, str]) -> Path:
    (tmp_path / "index.html").write_text(page, encoding="utf-8")
    for name, body in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return tmp_path


def test_a_reference_that_only_differs_in_case_is_a_defect(tmp_path):
    """Works on Windows, 404s on the Linux host the export is going to.

    The check lowercased both sides, so it called this valid and the report
    said the site was clean.
    """
    output = site(tmp_path,
                  '<html><body><img src="assets/Logo.PNG"></body></html>',
                  {"assets/logo.png": "x"})

    report = validate_output(output)

    assert len(report.case_mismatches) == 1
    mismatch = report.case_mismatches[0]
    assert mismatch.reference == "assets/Logo.PNG"
    assert "assets/logo.png" in mismatch.reason
    assert not report.missing_assets, "the file exists; only the spelling is wrong"
    assert not report.is_clean


def test_an_exact_match_is_still_clean(tmp_path):
    output = site(tmp_path,
                  '<html><body><img src="assets/logo.png"></body></html>',
                  {"assets/logo.png": "x"})

    report = validate_output(output)

    assert not report.case_mismatches
    assert not report.missing_assets
    assert report.is_clean


def test_a_genuinely_absent_file_is_still_missing_not_a_case_problem(tmp_path):
    """The two have different fixes, so they must not be conflated."""
    output = site(tmp_path,
                  '<html><body><img src="assets/nowhere.png"></body></html>',
                  {"assets/logo.png": "x"})

    report = validate_output(output)

    assert len(report.missing_assets) == 1
    assert not report.case_mismatches


def test_case_mismatches_reach_the_summary(tmp_path):
    output = site(tmp_path,
                  '<html><body><img src="Assets/logo.png"></body></html>',
                  {"assets/logo.png": "x"})

    assert validate_output(output).summary()["case_mismatches"] == 1


def test_the_worker_pool_never_forks(tmp_path):
    """Forking from this thread copies held locks and can deadlock on Linux.

    Windows always spawned; Linux defaulted to fork. Now both spawn, so a
    validation that passes on one platform means something on the other.
    """
    import inspect

    from app.services import static_validator

    source = inspect.getsource(static_validator._run_scan)
    assert 'get_context("spawn")' in source
    assert "mp_context=context" in source


def test_many_files_still_validate_with_workers(tmp_path):
    """Above the parallel threshold, so the pool is actually used."""
    files = {f"assets/file{n}.css": "body{}" for n in range(60)}
    links = "".join(f'<link rel="stylesheet" href="assets/file{n}.css">' for n in range(60))
    output = site(tmp_path, f"<html><head>{links}</head></html>", files)

    report = validate_output(output, workers=2)

    assert report.references_checked >= 60
    assert not report.missing_assets
    assert not report.case_mismatches
