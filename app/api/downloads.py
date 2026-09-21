"""Download endpoints: the finished ZIP, the report, the log and screenshots.

Every path served here is resolved inside the job's own workspace and checked
for containment, because the job id and any filename reach this layer from the
network.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from app.api.jobs import _find_zip
from app.utils.filesystem import JobWorkspace
from app.utils.security import is_within, safe_join

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["downloads"])


def _workspace(request: Request, job_id: str) -> JobWorkspace:
    store = request.app.state.store
    if store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="no such job")
    workspace = JobWorkspace(request.app.state.settings.job_dir(job_id))
    if not workspace.root.is_dir():
        raise HTTPException(status_code=404, detail="this job's workspace no longer exists")
    return workspace


@router.get("/{job_id}/download")
async def download_zip(request: Request, job_id: str) -> FileResponse:
    """The finished static website as a ZIP."""
    workspace = _workspace(request, job_id)
    zip_path = _find_zip(workspace)

    if zip_path is None or not zip_path.is_file():
        job = request.app.state.store.get(job_id)
        raise HTTPException(
            status_code=409,
            detail=(
                f"no ZIP has been produced yet; the job is {job.status}"
                if job else "no ZIP is available"
            ),
        )

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=zip_path.name,
        headers={"Content-Disposition": f'attachment; filename="{zip_path.name}"'},
    )


@router.get("/{job_id}/report", response_class=HTMLResponse)
async def view_report(request: Request, job_id: str) -> FileResponse:
    """The conversion report, rendered in the browser."""
    workspace = _workspace(request, job_id)
    report = workspace.report / "conversion-report.html"
    if not report.is_file():
        raise HTTPException(status_code=404, detail="no report has been generated yet")
    return FileResponse(report, media_type="text/html")


@router.get("/{job_id}/report.json")
async def report_json(request: Request, job_id: str) -> FileResponse:
    workspace = _workspace(request, job_id)
    report = workspace.report / "conversion-report.json"
    if not report.is_file():
        raise HTTPException(status_code=404, detail="no report has been generated yet")
    return FileResponse(report, media_type="application/json")


@router.get("/{job_id}/log", response_class=PlainTextResponse)
async def download_log(request: Request, job_id: str, tail: int = 0) -> PlainTextResponse:
    """The job's log file, optionally only its last *tail* lines."""
    workspace = _workspace(request, job_id)
    if not workspace.log_file.is_file():
        raise HTTPException(status_code=404, detail="no log file for this job")

    text = workspace.log_file.read_text(encoding="utf-8", errors="replace")
    if tail > 0:
        text = "\n".join(text.splitlines()[-tail:])
    return PlainTextResponse(text)


@router.get("/{job_id}/screenshots/{name}")
async def screenshot(request: Request, job_id: str, name: str) -> FileResponse:
    """One screenshot or visual diff image from the job's comparison run."""
    workspace = _workspace(request, job_id)

    try:
        path = safe_join(workspace.screenshots, name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid screenshot name") from exc

    if not path.is_file() or not is_within(workspace.screenshots, path):
        raise HTTPException(status_code=404, detail="no such screenshot")
    if path.suffix.lower() != ".png":
        raise HTTPException(status_code=400, detail="only PNG screenshots are served")

    return FileResponse(path, media_type="image/png")


@router.get("/{job_id}/screenshots")
async def list_screenshots(request: Request, job_id: str) -> dict:
    """Group the captured screenshots by page, so the UI can show comparisons."""
    workspace = _workspace(request, job_id)
    if not workspace.screenshots.is_dir():
        return {"pages": []}

    grouped: dict[str, dict] = {}
    for path in sorted(workspace.screenshots.glob("*.png")):
        # <slug>.<original|static|diff>.<desktop|mobile>.png
        parts = path.stem.split(".")
        if len(parts) < 3:
            continue
        slug, role, viewport = ".".join(parts[:-2]), parts[-2], parts[-1]
        entry = grouped.setdefault(slug, {"slug": slug, "images": {}})
        entry["images"][f"{role}_{viewport}"] = path.name

    return {"pages": sorted(grouped.values(), key=lambda e: e["slug"])}
