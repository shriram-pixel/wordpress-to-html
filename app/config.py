"""Application and per-job configuration.

Two distinct things live here:

* :class:`Settings` -- process-wide settings, read from the environment / .env,
  covering where jobs live, where portable runtimes are cached, and the limits
  that apply to every job.
* :class:`ConversionOptions` -- the per-job knobs the web UI exposes. These are
  submitted with each job and persisted alongside it so a resumed job uses the
  options it started with.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ExternalResourcePolicy(StrEnum):
    """What to do with assets hosted on domains other than the source site."""

    PRESERVE = "preserve"
    """Leave the absolute URL in place. The default: mirroring third-party
    services is usually both unnecessary and legally murky."""

    DOWNLOAD = "download"
    """Fetch and localise them too. Use only when you have the right to."""

    BLOCK = "block"
    """Strip the reference entirely."""


class Settings(BaseSettings):
    """Process-wide settings. Override any field via environment variable."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="WPSC_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- server -------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000
    # Binding beyond loopback exposes uploaded backups and their databases to
    # the network; it is opt-in and warned about at startup.
    allow_external_bind: bool = False

    # -- storage ------------------------------------------------------------
    jobs_dir: Path = PROJECT_ROOT / "jobs"
    runtime_dir: Path = PROJECT_ROOT / "runtime"
    database_path: Path = PROJECT_ROOT / "jobs" / "jobs.sqlite3"

    export_dir: Path | None = None
    """Where finished ZIPs are collected, so ten conversions do not leave ten
    ZIPs in ten job folders. Empty means ``<jobs_dir>/exports``. The copy is a
    hard link where the filesystem allows one, so it costs no extra disk."""

    max_upload_bytes: int = 20 * 1024 * 1024 * 1024   # 20 GiB

    backup_dirs: str = ""
    """Extra folders the UI searches for .wpress files, separated by ``;``.
    Downloads, Desktop and Documents are always searched. Example:
    ``WPSC_BACKUP_DIRS=C:\\bkp;D:\\backups``"""

    def backup_search_dirs(self) -> list[Path]:
        """Folders searched for backups that can be converted in place."""
        home = Path.home()
        candidates = [Path(p.strip()) for p in self.backup_dirs.split(";") if p.strip()]
        candidates += [home / "Downloads", home / "Desktop", home / "Documents"]
        seen: set[str] = set()
        result: list[Path] = []
        for path in candidates:
            key = str(path).lower()
            if key not in seen and path.is_dir():
                seen.add(key)
                result.append(path)
        return result

    @property
    def local_files_allowed(self) -> bool:
        """Whether the UI may convert a file straight from this machine's disk.

        Only when the server is reachable from this machine alone. Exposed on a
        network, the same feature would let anyone who can reach the page make
        the server read files from its disk.
        """
        return not self.allow_external_bind and self.host in {"127.0.0.1", "localhost", "::1"}
    keep_job_workspace: bool = True
    """Keep ``extracted/`` and the temporary WordPress after a job finishes.
    Turn off to reclaim disk; the output ZIP and report are always kept."""

    # -- runtimes -----------------------------------------------------------
    php_version: str = "8.2"
    mariadb_version: str = "11.4.4"
    auto_provision_runtimes: bool = True
    """Download portable PHP/MariaDB when none is found. When false, a missing
    runtime produces setup instructions instead of a download."""
    php_binary: str | None = None      # explicit override
    mysqld_binary: str | None = None   # explicit override

    # -- pipeline limits ----------------------------------------------------
    render_concurrency: int = 0
    """0 (the default) measures the machine and picks a number; see
    app.utils.capacity."""
    asset_concurrency: int = 0
    """0 measures the machine; see app.utils.capacity."""
    max_parallel_jobs: int = 0
    """Conversions to run at the same time. 0 measures the machine: two only
    on a big machine, because each job runs its own database, PHP pool and
    browser."""

    max_urls: int = 5000
    """Hard ceiling on discovered URLs, so a calendar or faceted archive cannot
    expand into an unbounded crawl."""
    page_timeout_ms: int = 45_000
    page_retries: int = 2
    asset_timeout_seconds: float = 60.0
    asset_retries: int = 2

    # -- feature defaults ---------------------------------------------------
    external_policy: ExternalResourcePolicy = ExternalResourcePolicy.PRESERVE

    @field_validator("jobs_dir", "runtime_dir", "database_path", "export_dir",
                     mode="before")
    @classmethod
    def _expand(cls, value):
        if value is None:
            return value
        return Path(os.path.expandvars(str(value))).expanduser()

    @model_validator(mode="after")
    def _database_follows_jobs_dir(self) -> "Settings":
        # Keep the job list next to the jobs it describes. Otherwise moving
        # WPSC_JOBS_DIR to another drive silently splits the web UI and the
        # command line onto two separate job lists that cannot see each other.
        if "database_path" not in self.model_fields_set:
            self.database_path = self.jobs_dir / "jobs.sqlite3"
        if self.export_dir is None:
            self.export_dir = self.jobs_dir / "exports"
        return self

    @property
    def exports(self) -> Path:
        """Where finished ZIPs are collected. Always a path, never None."""
        return self.export_dir or (self.jobs_dir / "exports")

    def ensure_directories(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.exports.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        """Workspace root for one job. Every job is fully isolated."""
        return self.jobs_dir / job_id


class ConversionOptions(BaseModel):
    """Per-job options. These map one-to-one onto the checkboxes in the web UI."""

    # -- what to export -----------------------------------------------------
    preserve_url_structure: bool = True
    """Folder per page (the default): ``/about/`` becomes ``about/index.html``, so
    the site keeps exactly the same URLs on a web server. Off gives one file per
    page instead -- ``about.html`` -- which changes the URLs to ``/about.html``."""

    folder_links: bool = True
    """How a link to another page is written, when pages are folders.

    On (the default) links point at the folder -- ``contact-us/`` -- which is
    what WordPress itself produces and what a web server expects; the server
    then serves the folder's index.html. Off writes the file explicitly --
    ``contact-us/index.html`` -- which is what a site opened straight from a
    disk, a USB stick or a file share needs, because there is no server to
    resolve the folder. Ignored when pages are single files (about.html)."""

    include_posts: bool = True
    include_pages: bool = True
    include_custom_post_types: bool = True
    include_categories: bool = True
    include_tags: bool = False
    include_author_archives: bool = False
    include_date_archives: bool = False
    include_custom_taxonomies: bool = True
    include_pagination: bool = True
    include_feeds: bool = False

    follow_internal_links: bool = True
    """Add internal links found in rendered pages to the queue."""
    max_crawl_depth: int = 3

    # -- rendering ----------------------------------------------------------
    capture_lazy_assets: bool = True
    capture_javascript: bool = True
    render_concurrency: int = Field(default=0, ge=0, le=32)
    """Pages rendered at once. 0 means "as many as this machine can take",
    measured from its cores and memory when the job starts."""
    extra_settle_ms: int = 600
    """Quiet period required after the last network activity and scroll."""

    # -- assets -------------------------------------------------------------
    external_policy: ExternalResourcePolicy = ExternalResourcePolicy.PRESERVE
    download_media: bool = True
    download_documents: bool = True
    """Localise linked PDFs/office documents that live on the source site."""

    # -- validation ---------------------------------------------------------
    validate_links: bool = True
    check_console_errors: bool = True
    screenshot_comparison: bool = True
    mobile_validation: bool = True
    visual_sample_limit: int = 25
    """Screenshot comparison is the slowest check; cap it on large sites.
    0 means compare every page."""

    # -- output -------------------------------------------------------------
    generate_sitemap: bool = True
    generate_robots_txt: bool = True
    zip_name: str | None = None

    desktop_viewport: tuple[int, int] = (1920, 1080)
    mobile_viewport: tuple[int, int] = (390, 844)

    @field_validator("max_crawl_depth")
    @classmethod
    def _sane_depth(cls, value: int) -> int:
        return max(0, min(value, 10))


_settings: Settings | None = None


def get_settings() -> Settings:
    """Cached settings accessor used by the API layer and the worker."""
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_directories()
    return _settings
