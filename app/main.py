"""FastAPI application: the web UI and the job API.

Run it with::

    python app.py
    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import downloads, jobs
from app.config import PROJECT_ROOT, get_settings
from app.models.job import JobStore
from app.services.job_manager import JobManager
from app.utils import capacity

logger = logging.getLogger(__name__)

FRONTEND_DIR = PROJECT_ROOT / "frontend"


def configure_logging(level: int = logging.INFO) -> None:
    """Console logging that works on a Windows terminal.

    The default Windows console codepage is cp1252, and a single non-ASCII
    character in a log line (a site title, a filename) raises
    UnicodeEncodeError and loses the message. Reconfiguring the stream to UTF-8
    with replacement avoids that without needing the user to change anything.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-7s  %(name)-34s  %(message)s", "%H:%M:%S")
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # These are chatty at INFO and drown out the pipeline's own progress.
    for noisy in ("httpx", "httpcore", "urllib3", "PIL", "asyncio", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_directories()

    store = JobStore(settings.database_path)
    machine = capacity.measure()
    workers = settings.max_parallel_jobs or machine.parallel_jobs
    logger.info("this machine: %s; %d job(s) at a time", machine.describe(), workers)
    manager = JobManager(settings, store, max_workers=workers)

    app.state.settings = settings
    app.state.store = store
    app.state.manager = manager

    recovered = manager.recover_orphans()
    if recovered:
        logger.warning("%d job(s) interrupted by a previous run were marked failed", recovered)

    if settings.allow_external_bind and settings.host not in {"127.0.0.1", "localhost"}:
        logger.warning(
            "listening on %s: uploaded backups, their databases and the generated "
            "sites will be reachable from the network. Bind to 127.0.0.1 unless you "
            "intend that.", settings.host,
        )

    logger.info("wp-static-converter ready on http://%s:%d", settings.host, settings.port)
    try:
        yield
    finally:
        manager.shutdown(wait=False)
        store.close()


app = FastAPI(
    title="WordPress to Static HTML converter",
    description=(
        "Convert an All-in-One WP Migration .wpress backup into a deployable "
        "static website, by restoring it locally and capturing what a real "
        "browser renders."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(jobs.router)
app.include_router(downloads.router)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """Return a useful message rather than a bare 500, without leaking paths."""
    from app.utils.security import scrub_windows_path

    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": scrub_windows_path(f"{type(exc).__name__}: {exc}")},
    )


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    icon = FRONTEND_DIR / "static" / "favicon.svg"
    if icon.is_file():
        return FileResponse(icon, media_type="image/svg+xml")
    return FileResponse(FRONTEND_DIR / "index.html")


if (FRONTEND_DIR / "static").is_dir():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")


def main() -> int:
    """Entry point for ``python app.py``."""
    import uvicorn

    configure_logging()
    from app.utils.console import disable_quick_edit

    # A click in the window would otherwise pause the whole job.
    disable_quick_edit()
    settings = get_settings()

    host = settings.host
    if settings.allow_external_bind and host == "127.0.0.1":
        host = "0.0.0.0"

    uvicorn.run(
        "app.main:app",
        host=host,
        port=settings.port,
        log_config=None,
        access_log=False,
        # A conversion can occupy a worker for a long time; a reload loop or a
        # second worker would fight over the same SQLite store.
        workers=1,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
