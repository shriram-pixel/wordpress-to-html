"""Job API: create, inspect, follow and cancel conversions."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.config import ConversionOptions
from app.models.job import JobStatus, UrlState
from app.utils.filesystem import JobWorkspace, ensure_free_space, sanitise_upload_name
from app.utils.security import safe_join

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["jobs"])

#: Read uploads in chunks so a multi-gigabyte backup never lands in memory.
_UPLOAD_CHUNK = 8 * 1024 * 1024


def _store(request: Request):
    return request.app.state.store


def _manager(request: Request):
    return request.app.state.manager


def _settings(request: Request):
    return request.app.state.settings


@router.get("/health")
async def health(request: Request) -> dict:
    """Dependency diagnostics, used by the UI and by ``scripts/doctor.py``."""
    from starlette.concurrency import run_in_threadpool

    from app.services.runtime_provisioner import diagnose

    settings = _settings(request)
    # Probing runtimes shells out to php and mysqld; keep that off the event
    # loop so a slow probe cannot stall every other request.
    report = await run_in_threadpool(
        diagnose, settings.runtime_dir, settings.php_binary, settings.mysqld_binary
    )
    report["ready"] = bool(
        report["php"]["found"] and report["mysql"]["found"] and report["playwright"]["chromium_ready"]
    )
    report["auto_provision"] = settings.auto_provision_runtimes
    report["active_jobs"] = _manager(request).active_count
    return report


@router.get("/options/defaults")
async def option_defaults() -> dict:
    """The default conversion options, so the UI does not duplicate them."""
    return ConversionOptions().model_dump(mode="json")


@router.post("/jobs", status_code=201)
async def create_job(
    request: Request,
    file: UploadFile = File(..., description="The .wpress backup to convert"),
    options: str = Form("{}", description="ConversionOptions as a JSON object"),
) -> dict:
    """Upload a ``.wpress`` backup and start converting it."""
    settings = _settings(request)
    store = _store(request)
    manager = _manager(request)

    try:
        parsed = json.loads(options) if options else {}
        if not isinstance(parsed, dict):
            raise ValueError("options must be a JSON object")
        conversion_options = ConversionOptions(**parsed)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid options: {exc}") from exc

    filename = sanitise_upload_name(file.filename or "upload.wpress")

    # Pre-flight the two limits that a large backup runs into, using the
    # declared size, so a 10 GB upload is refused in a second rather than after
    # twenty minutes of transfer. A conversion writes the archive, the extracted
    # tree, the generated site and the ZIP, so budget roughly four times the
    # upload before accepting it.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit():
        incoming = int(declared)
        if incoming > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"the upload is {incoming / 1024**3:.1f} GiB, over the "
                    f"{settings.max_upload_bytes / 1024**3:.0f} GiB limit. "
                    "Raise WPSC_MAX_UPLOAD_BYTES to allow it."
                ),
            )
        try:
            ensure_free_space(settings.jobs_dir, incoming * 4)
        except OSError as exc:
            raise HTTPException(status_code=507, detail=str(exc)) from exc

    job = store.create(filename, conversion_options, 0)
    workspace = JobWorkspace.create(settings.job_dir(job.id))
    destination = workspace.input / filename

    written = 0
    try:
        with destination.open("wb") as out:
            while chunk := await file.read(_UPLOAD_CHUNK):
                written += len(chunk)
                if written > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"the upload exceeds the {settings.max_upload_bytes // (1024**3)} GiB "
                            "limit; raise WPSC_MAX_UPLOAD_BYTES to allow it"
                        ),
                    )
                out.write(chunk)
    except HTTPException:
        shutil.rmtree(workspace.root, ignore_errors=True)
        store.delete(job.id)
        raise
    except Exception as exc:
        shutil.rmtree(workspace.root, ignore_errors=True)
        store.delete(job.id)
        raise HTTPException(status_code=500, detail=f"could not save the upload: {exc}") from exc
    finally:
        await file.close()

    if written == 0:
        shutil.rmtree(workspace.root, ignore_errors=True)
        store.delete(job.id)
        raise HTTPException(status_code=422, detail="the uploaded file was empty")

    store.update(job.id, input_bytes=written)
    job.input_bytes = written
    store.add_event(job.id, "QUEUED", f"received {filename} ({written / 1048576:.1f} MiB)")

    manager.submit(job, destination)

    return {"id": job.id, "status": str(job.status), "filename": filename, "bytes": written}


@router.get("/local-backups")
async def local_backups(request: Request) -> dict:
    """``.wpress`` files already on this computer, which can be converted in place.

    Converting from disk skips the upload entirely -- a browser can only send a
    file's *contents*, never its location, so an upload always means the
    server receives and stores a full second copy before work can start.
    """
    from starlette.concurrency import run_in_threadpool

    settings = _settings(request)
    if not settings.local_files_allowed:
        return {"allowed": False, "files": [], "folders": []}

    def scan() -> tuple[list[dict], list[str]]:
        folders = settings.backup_search_dirs()
        found: dict[str, dict] = {}
        for folder in folders:
            try:
                candidates = list(folder.glob("*.wpress")) + list(folder.glob("*/*.wpress"))
            except OSError:
                continue
            for path in candidates:
                try:
                    if not path.is_file():
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                found[str(path).lower()] = {
                    "path": str(path),
                    "name": path.name,
                    "folder": str(path.parent),
                    "bytes": stat.st_size,
                    "modified": stat.st_mtime,
                }
        files = sorted(found.values(), key=lambda item: item["modified"], reverse=True)
        return files, [str(f) for f in folders]

    files, folders = await run_in_threadpool(scan)
    return {"allowed": True, "files": files, "folders": folders}


@router.post("/jobs/local", status_code=201)
async def create_local_job(request: Request) -> dict:
    """Start converting a ``.wpress`` that is already on this computer.

    The file is read where it is: nothing is uploaded, copied or moved, and
    deleting the job later removes only its workspace, never this file.
    """
    from app.services.wpress_extractor import PurePythonWpressExtractor, WpressError

    settings = _settings(request)
    store = _store(request)
    manager = _manager(request)

    if not settings.local_files_allowed:
        raise HTTPException(
            status_code=403,
            detail=(
                "converting files from this computer's disk is only available when the "
                "app is reachable from this computer alone; upload the file instead"
            ),
        )

    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="expected a JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="expected a JSON object")

    raw_path = str(body.get("path") or "").strip().strip('"')
    if not raw_path:
        raise HTTPException(status_code=422, detail="no file path was given")

    archive = Path(raw_path).expanduser()
    if not archive.is_absolute():
        raise HTTPException(status_code=422, detail="give the full path, e.g. C:\\bkp\\site.wpress")
    try:
        archive = archive.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=f"no such file: {raw_path}") from exc
    if not archive.is_file():
        raise HTTPException(status_code=422, detail=f"not a file: {raw_path}")
    if archive.suffix.lower() != ".wpress":
        raise HTTPException(status_code=422, detail="only .wpress backups can be converted")

    # Check it really is a .wpress before creating a job for it.
    try:
        PurePythonWpressExtractor().validate(archive)
    except WpressError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        options_data = body.get("options") or {}
        if not isinstance(options_data, dict):
            raise ValueError("options must be a JSON object")
        conversion_options = ConversionOptions(**options_data)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid options: {exc}") from exc

    size = archive.stat().st_size
    try:
        # Read in place, so no copy of the archive: the working set is the
        # restored install, the generated site and the ZIP.
        ensure_free_space(settings.jobs_dir, size * 3)
    except OSError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc

    job = store.create(archive.name, conversion_options, size)
    JobWorkspace.create(settings.job_dir(job.id))
    store.add_event(
        job.id, "QUEUED",
        f"using {archive} in place ({size / 1048576:.1f} MiB) -- no upload needed",
    )
    manager.submit(job, archive)

    return {"id": job.id, "status": str(job.status), "filename": archive.name,
            "bytes": size, "source": str(archive)}


@router.get("/jobs")
async def list_jobs(request: Request, limit: int = 50) -> dict:
    jobs = _store(request).list(limit=min(max(1, limit), 200))
    return {"jobs": [job.to_dict() for job in jobs]}


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> dict:
    store = _store(request)
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")

    payload = job.to_dict()
    payload["url_counts"] = store.url_counts(job_id)
    payload["running"] = _manager(request).is_running(job_id)

    workspace = JobWorkspace(_settings(request).job_dir(job_id))
    payload["artifacts"] = {
        "zip": _find_zip(workspace) is not None,
        "report": (workspace.report / "conversion-report.html").is_file(),
        "log": workspace.log_file.is_file(),
    }
    return payload


@router.get("/jobs/{job_id}/logs")
async def get_logs(request: Request, job_id: str, after: int = 0, limit: int = 500) -> dict:
    """Structured job events, for the live log in the UI.

    Poll with the ``id`` of the last event received to get only what is new.
    """
    store = _store(request)
    if store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="no such job")

    events = store.events(job_id, after_id=after, limit=min(max(1, limit), 2000))
    return {
        "events": events,
        "last_id": events[-1]["id"] if events else after,
    }


@router.get("/jobs/{job_id}/urls")
async def get_urls(request: Request, job_id: str, state: str | None = None) -> dict:
    """Per-URL checkpoint state, which is what makes retries visible."""
    store = _store(request)
    if store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="no such job")

    parsed_state = None
    if state:
        try:
            parsed_state = UrlState(state.upper())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"unknown state: {state}") from exc

    records = store.get_urls(job_id, parsed_state)
    return {
        "counts": store.url_counts(job_id),
        "urls": [
            {
                "url": r.url, "kind": r.kind, "source": r.source, "state": str(r.state),
                "attempts": r.attempts, "output_path": r.output_path,
                "http_status": r.http_status, "error": r.error, "title": r.title,
            }
            for r in records[:2000]
        ],
    }


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str) -> dict:
    store = _store(request)
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    if job.status.is_terminal:
        raise HTTPException(status_code=409, detail=f"the job is already {job.status}")

    cancelled = _manager(request).cancel(job_id)
    if not cancelled:
        raise HTTPException(status_code=409, detail="the job is not running")
    return {"id": job_id, "cancelling": True}


@router.post("/jobs/{job_id}/resume")
async def resume_job(request: Request, job_id: str) -> dict:
    """Continue a failed or cancelled job from its restored WordPress.

    Extraction and the database import are not repeated, and pages already
    rendered are reused. Optional JSON body: ``{"folders": true|false}``.
    """
    store = _store(request)
    manager = _manager(request)
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    if manager.is_running(job_id):
        raise HTTPException(status_code=409, detail="the job is already running")
    if job.status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
        raise HTTPException(
            status_code=409, detail=f"only a failed or cancelled job can be resumed (it is {job.status})"
        )

    workspace = _settings(request).job_dir(job_id)
    if not ((workspace / "wordpress" / "index.php").is_file()
            and (workspace / "database" / "data" / "mysql").is_dir()):
        raise HTTPException(
            status_code=409,
            detail="this job stopped before WordPress was restored; start a new conversion instead",
        )

    folders = None
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("folders"), bool):
            folders = body["folders"]
    except ValueError:
        pass

    try:
        port = manager.resume(job, folders=folders)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    store.update(job_id, status=JobStatus.QUEUED, finished_at=None, error=None)
    store.add_event(job_id, "QUEUED", "resuming from the restored WordPress -- no re-import")
    return {"id": job_id, "resuming": True, "port": port}


@router.delete("/jobs/{job_id}")
async def delete_job(request: Request, job_id: str) -> dict:
    """Delete a job, its workspace and its output. Not reversible."""
    store = _store(request)
    if store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="no such job")
    _manager(request).delete_job(job_id)
    return {"id": job_id, "deleted": True}


def _find_zip(workspace: JobWorkspace) -> Path | None:
    """The finished archive, whatever it ended up being called."""
    if workspace.zip_path.is_file():
        return workspace.zip_path
    candidates = sorted(workspace.root.glob("*.zip"))
    return candidates[0] if candidates else None
