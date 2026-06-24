"""
main.py

FastAPI application entry point. Responsibilities:
  - App lifespan (startup / shutdown of Converter)
  - REST API routes: POST /api/convert, GET /api/status/{job_id}, GET /api/download/{job_id}
  - Static file serving for the frontend (index.html)
  - Global exception handling
  - Request/response Pydantic models
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl, field_validator

from config import Settings
from converter import (
    ConversionJob,
    Converter,
    ConverterBusyError,
    InvalidURLError,
    JobStatus,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------

settings = Settings()
converter = Converter(
    tmp_dir=settings.tmp_dir,
    max_concurrent=settings.max_concurrent_jobs,
    file_ttl=settings.file_ttl_seconds,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await converter.start()
    logger.info("Application startup complete.")
    yield
    await converter.shutdown()
    logger.info("Application shutdown complete.")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="yt-mp3",
    description="Convert YouTube videos to MP3.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ConvertRequest(BaseModel):
    url: HttpUrl

    @field_validator("url", mode="before")
    @classmethod
    def coerce_url_to_str(cls, v):
        return str(v)


class ConvertResponse(BaseModel):
    job_id: str
    status: JobStatus


class StatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    error: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/api/convert", response_model=ConvertResponse, status_code=202)
async def convert(request: ConvertRequest):
    """
    Accept a YouTube URL and schedule an MP3 conversion.
    Returns immediately with a job_id — poll /api/status/{job_id} for progress.
    """
    try:
        job: ConversionJob = await converter.convert(str(request.url))
    except InvalidURLError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ConverterBusyError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    return ConvertResponse(job_id=job.job_id, status=job.status)


@app.get("/api/status/{job_id}", response_model=StatusResponse)
async def status(job_id: str):
    """
    Poll the status of a conversion job.
    Returns: pending | running | done | failed
    """
    job = converter.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    return StatusResponse(job_id=job.job_id, status=job.status, error=job.error)


@app.get("/api/download/{job_id}")
async def download(job_id: str, background_tasks: BackgroundTasks):
    """
    Stream the converted MP3 to the browser and delete the file afterward.
    Only available when job status is 'done'.
    """
    job = converter.get_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    if job.status == JobStatus.FAILED:
        raise HTTPException(status_code=410, detail=job.error or "Conversion failed.")

    if job.status != JobStatus.DONE:
        raise HTTPException(
            status_code=409,
            detail=f"Job is not ready yet (status: {job.status}).",
        )

    if job.output_path is None or not job.output_path.exists():
        raise HTTPException(status_code=410, detail="File has expired or was already downloaded.")

    filename = _safe_filename(job_id)
    background_tasks.add_task(converter.delete_file, job)

    return FileResponse(
        path=job.output_path,
        media_type="audio/mpeg",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Static files (serves index.html for the frontend)
# Mounted last so API routes take priority.
# ---------------------------------------------------------------------------

_static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_filename(job_id: str) -> str:
    """
    Generate a download filename. In a future iteration this could fetch
    the video title from yt-dlp metadata. For now, a stable unique name.
    """
    return f"yt-mp3-{job_id[:8]}.mp3"