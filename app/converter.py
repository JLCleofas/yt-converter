"""
converter.py

Handles all yt-dlp conversion logic. Responsibilities:
  - URL validation (YouTube only)
  - Async subprocess execution (no shell=True)
  - Concurrency limiting
  - Temp file lifecycle (creation, registration, TTL cleanup)
  - Structured result / error reporting
"""

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

YOUTUBE_URL_RE = re.compile(
    r"^https?://(www\.)?(youtube\.com/watch\?.*v=[\w-]+|youtu\.be/[\w-]+)"
)

YTDLP_BINARY = "yt-dlp"
FFMPEG_BINARY = "ffmpeg"  # must be on PATH inside the container

MAX_CONCURRENT_JOBS = 3
FILE_TTL_SECONDS = 600  # 10 minutes — files are deleted after this window


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class ConversionJob:
    job_id: str
    url: str
    status: JobStatus = JobStatus.PENDING
    output_path: Optional[Path] = None
    error: Optional[str] = None
    # internal: asyncio.Task reference so we can await / cancel if needed
    _task: Optional[asyncio.Task] = field(default=None, repr=False)


@dataclass
class ConversionResult:
    """Returned by Converter.convert() once the job completes."""
    job_id: str
    output_path: Path


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConversionError(Exception):
    """Raised for expected, user-facing failure modes."""


class InvalidURLError(ConversionError):
    """The supplied URL is not an accepted YouTube URL."""


class ConverterBusyError(ConversionError):
    """Too many concurrent jobs are running."""


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------


class Converter:
    """
    Async-safe wrapper around yt-dlp.

    Usage (in FastAPI lifespan):
        converter = Converter(tmp_dir=Path("/tmp/yt-mp3"))
        await converter.start()
        ...
        await converter.shutdown()
    """

    def __init__(
        self,
        tmp_dir: Path,
        max_concurrent: int = MAX_CONCURRENT_JOBS,
        file_ttl: int = FILE_TTL_SECONDS,
    ) -> None:
        self._tmp_dir = tmp_dir
        self._max_concurrent = max_concurrent
        self._file_ttl = file_ttl

        self._semaphore: asyncio.Semaphore  # initialised in start()
        self._jobs: dict[str, ConversionJob] = {}
        self._cleanup_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Call once at application startup."""
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._cleanup_task = asyncio.create_task(self._ttl_cleanup_loop())
        logger.info("Converter started. tmp_dir=%s", self._tmp_dir)

    async def shutdown(self) -> None:
        """Call once at application shutdown."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("Converter shut down.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_job(self, job_id: str) -> Optional[ConversionJob]:
        return self._jobs.get(job_id)

    async def convert(self, url: str) -> ConversionJob:
        """
        Validate URL, create a job, and schedule async conversion.

        Returns the ConversionJob immediately (status=PENDING/RUNNING).
        Callers poll get_job(job_id) to track progress.
        """
        self._validate_url(url)
        self._check_capacity()

        job_id = uuid.uuid4().hex
        job = ConversionJob(job_id=job_id, url=url)
        self._jobs[job_id] = job

        job._task = asyncio.create_task(self._run_job(job))
        logger.info("Job %s created for URL %s", job_id, url)
        return job

    # ------------------------------------------------------------------
    # Internal: job execution
    # ------------------------------------------------------------------

    async def _run_job(self, job: ConversionJob) -> None:
        output_path = self._tmp_dir / f"{job.job_id}.mp3"

        async with self._semaphore:
            job.status = JobStatus.RUNNING
            logger.info("Job %s running", job.job_id)

            try:
                await self._exec_ytdlp(job.url, output_path)
                job.output_path = output_path
                job.status = JobStatus.DONE
                logger.info("Job %s done → %s", job.job_id, output_path)

            except ConversionError as exc:
                job.status = JobStatus.FAILED
                job.error = str(exc)
                logger.warning("Job %s failed: %s", job.job_id, exc)

            except Exception as exc:  # noqa: BLE001
                job.status = JobStatus.FAILED
                job.error = "Unexpected internal error."
                logger.exception("Job %s unexpected error: %s", job.job_id, exc)

    async def _exec_ytdlp(self, url: str, output_path: Path) -> None:
        """
        Invoke yt-dlp as an async subprocess.

        Extracts audio and re-encodes to MP3 via ffmpeg.
        Never uses shell=True — args are always a list.
        """
        args = [
            YTDLP_BINARY,
            "--no-playlist",           # never accidentally grab a whole playlist
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",    # best VBR quality
            "--ffmpeg-location", FFMPEG_BINARY,
            "--output", str(output_path.with_suffix("")),  # yt-dlp appends extension
            "--no-progress",           # cleaner logs inside Docker
            "--", url,                 # "--" prevents URL being parsed as a flag
        ]

        logger.debug("Executing: %s", args)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_detail = stderr.decode(errors="replace").strip()
            logger.warning("yt-dlp stderr:\n%s", error_detail)
            raise ConversionError(self._friendly_error(error_detail))

    # ------------------------------------------------------------------
    # Internal: temp file TTL cleanup
    # ------------------------------------------------------------------

    async def _ttl_cleanup_loop(self) -> None:
        """
        Periodically scan for DONE jobs whose output files have exceeded
        the TTL and delete them. Runs every 60 seconds.
        """
        while True:
            try:
                await asyncio.sleep(60)
                await self._purge_expired_files()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Error in TTL cleanup loop — continuing.")

    async def _purge_expired_files(self) -> None:
        import time

        now = time.time()
        to_remove: list[str] = []

        for job_id, job in self._jobs.items():
            if job.status is not JobStatus.DONE:
                continue
            if job.output_path is None or not job.output_path.exists():
                to_remove.append(job_id)
                continue

            age = now - job.output_path.stat().st_mtime
            if age > self._file_ttl:
                job.output_path.unlink(missing_ok=True)
                logger.info("TTL expired: deleted %s", job.output_path)
                to_remove.append(job_id)

        for job_id in to_remove:
            self._jobs.pop(job_id, None)

    def delete_file(self, job: ConversionJob) -> None:
        """
        Immediately delete the output file after it has been served.
        Called by the download route's BackgroundTask.
        """
        if job.output_path and job.output_path.exists():
            job.output_path.unlink(missing_ok=True)
            logger.info("Served and deleted: %s", job.output_path)
        self._jobs.pop(job.job_id, None)

    # ------------------------------------------------------------------
    # Internal: validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_url(url: str) -> None:
        if not isinstance(url, str) or not url.strip():
            raise InvalidURLError("URL must be a non-empty string.")
        if not YOUTUBE_URL_RE.match(url.strip()):
            raise InvalidURLError(
                "Only YouTube URLs (youtube.com/watch?v=... or youtu.be/...) are accepted."
            )

    def _check_capacity(self) -> None:
        running = sum(
            1 for j in self._jobs.values() if j.status in (JobStatus.PENDING, JobStatus.RUNNING)
        )
        if running >= self._max_concurrent:
            raise ConverterBusyError(
                f"Maximum concurrent conversions ({self._max_concurrent}) reached. "
                "Please try again shortly."
            )

    @staticmethod
    def _friendly_error(stderr: str) -> str:
        """
        Map common yt-dlp error strings to user-friendly messages.
        Avoids leaking internal paths or command details to the API response.
        """
        lower = stderr.lower()
        if "video unavailable" in lower:
            return "Video is unavailable (private, deleted, or region-locked)."
        if "sign in" in lower or "login" in lower:
            return "Video requires authentication and cannot be downloaded."
        if "copyright" in lower:
            return "Video is blocked due to a copyright claim."
        if "unable to extract" in lower or "no video formats" in lower:
            return "Could not extract audio from this video."
        return "Conversion failed. The video may be unavailable or unsupported."