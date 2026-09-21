"""Shared pytest configuration and fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "demo-site.wpress"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: needs PHP, MariaDB and Chromium, and the demo .wpress fixture",
    )
    config.addinivalue_line("markers", "slow: takes more than a few seconds")


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def demo_fixture() -> Path:
    """The demo ``.wpress`` archive, skipping the test if it has not been built."""
    if not FIXTURE.is_file():
        pytest.skip(
            f"{FIXTURE.name} is missing; build it with: python scripts/make_fixture.py"
        )
    return FIXTURE


@pytest.fixture(scope="session")
def restored_site(demo_fixture):
    """A running, restored copy of the demo site, shared by the integration tests."""
    pytest.importorskip("pymysql")
    from app.services.runtime_provisioner import find_mysql, find_php

    runtime_dir = PROJECT_ROOT / "runtime"
    if find_php(None, runtime_dir) is None or find_mysql(None, runtime_dir) is None:
        pytest.skip("PHP and/or MariaDB are unavailable; run setup.ps1 first")

    from tests.harness import restore_demo_site

    site = restore_demo_site()
    try:
        yield site
    finally:
        site.cleanup()
