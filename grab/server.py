"""HTTP server exposing the download engine as a small web app."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .core import FORMAT_CHOICES, DownloadManager, DownloadOptions, extract_info

STATIC_DIR = Path(__file__).parent / "static"
MAX_OUTPUT_ROOT = Path.home() / "Downloads"


class InfoRequest(BaseModel):
    url: str = Field(..., min_length=3, description="URL to inspect")


class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=3, description="URL to download")
    format: str = "auto"
    quality: int | None = Field(None, ge=144, le=4320)
    audio_format: str = "m4a"
    playlist: bool = False
    subtitles: bool = False
    output: str | None = None
    rate_limit: str | None = None


def _safe_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required.")
    if "://" not in url:
        url = f"https://{url}"
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Only http and https URLs are supported.")
    return url


def _resolve_output(output: str | None) -> Path:
    """Only allow saving inside the download root (home/Downloads by default)."""
    if not output:
        root = MAX_OUTPUT_ROOT if MAX_OUTPUT_ROOT.exists() else Path.cwd()
        return root
    candidate = Path(output).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    candidate = candidate.resolve()
    roots = {MAX_OUTPUT_ROOT.resolve() if MAX_OUTPUT_ROOT.exists() else None, Path.cwd().resolve()}
    roots.discard(None)
    if not any(candidate == root or root in candidate.parents for root in roots):
        raise HTTPException(
            status_code=400,
            detail="Output must live inside your Downloads folder or the project directory.",
        )
    return candidate


def create_app(manager: DownloadManager | None = None) -> FastAPI:
    manager = manager or DownloadManager(max_workers=3)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        manager.shutdown()

    app = FastAPI(
        title="grab",
        version=__version__,
        description="Download videos from the web.",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # -- static front end --------------------------------------------------- #
    index_file = STATIC_DIR / "index.html"

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        if not index_file.exists():
            raise HTTPException(status_code=404, detail="UI not built.")
        return FileResponse(index_file)

    # -- api ---------------------------------------------------------------- #
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.post("/api/info")
    async def info(payload: InfoRequest) -> dict[str, Any]:
        url = _safe_url(payload.url)
        try:
            data = extract_info(url, DownloadOptions(playlist=True))
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)[:400]) from exc
        if not data:
            raise HTTPException(status_code=422, detail="No media found at that URL.")
        return data

    @app.post("/api/download")
    async def download(payload: DownloadRequest) -> dict[str, Any]:
        url = _safe_url(payload.url)
        if payload.format not in FORMAT_CHOICES:
            raise HTTPException(status_code=400, detail=f"format must be one of {FORMAT_CHOICES}")
        output = _resolve_output(payload.output)
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=400, detail=f"Cannot create output folder: {exc}"
            ) from exc

        options = DownloadOptions(
            output=output,
            format=payload.format,
            quality=payload.quality,
            audio_format=payload.audio_format,
            playlist=payload.playlist,
            subtitles=payload.subtitles,
            rate_limit=payload.rate_limit,
        )
        job = manager.start(url, options)
        return job.to_dict()

    @app.get("/api/jobs")
    async def jobs() -> list[dict[str, Any]]:
        return [job.to_dict() for job in manager.jobs()]

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str) -> dict[str, Any]:
        found = manager.get(job_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        return found.to_dict()

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: str) -> dict[str, Any]:
        found = manager.cancel(job_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        return found.to_dict()

    @app.get("/api/jobs/{job_id}/file")
    async def file(job_id: str, index: int = Query(0, ge=0)) -> FileResponse:
        found = manager.get(job_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        files = found.to_dict()["files"]
        if not files:
            raise HTTPException(status_code=409, detail="This download has no file yet.")
        if index >= len(files):
            raise HTTPException(status_code=404, detail="No such file in this job.")
        path = Path(files[index])
        if not path.exists():
            raise HTTPException(status_code=410, detail="The file no longer exists on disk.")
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    @app.exception_handler(Exception)
    async def _unhandled(_request, exc: Exception):  # pragma: no cover - safety net
        return JSONResponse(status_code=500, content={"detail": str(exc)[:400]})

    app.state.manager = manager
    return app
