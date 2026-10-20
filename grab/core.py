"""Core download engine built on top of yt-dlp.

The engine is UI agnostic: both the CLI and the web server drive the exact
same :class:`DownloadManager`, so behaviour is identical in both front ends.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadError

try:  # pragma: no cover - availability depends on the yt-dlp version
    from yt_dlp.utils import DownloadCancelled
except ImportError:  # pragma: no cover
    DownloadCancelled = DownloadError


# --------------------------------------------------------------------------- #
# ffmpeg handling
# --------------------------------------------------------------------------- #
def ensure_ffmpeg() -> str | None:
    """Locate ffmpeg, falling back to the bundled static build if needed.

    Returns the ffmpeg path, or ``None`` when no ffmpeg is available at all.
    """
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import static_ffmpeg  # type: ignore

        static_ffmpeg.add_paths()
    except Exception:
        return None
    return shutil.which("ffmpeg")


def ffmpeg_available() -> bool:
    return ensure_ffmpeg() is not None


# --------------------------------------------------------------------------- #
# options
# --------------------------------------------------------------------------- #
FORMAT_CHOICES = ("auto", "video", "audio", "best")


def parse_rate(text: str | None) -> int | None:
    """Parse a human rate limit such as ``500K`` or ``2M`` into bytes/second."""
    if not text:
        return None
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*([kmg]?)(?:i?b)?(?:/s|bps)?\s*", text, re.IGNORECASE
    )
    if not match:
        raise ValueError(f"invalid rate limit: {text!r} (use e.g. 500K, 2M)")
    multiplier = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[match.group(2).lower()]
    return int(float(match.group(1)) * multiplier)


@dataclass
class DownloadOptions:
    """Everything that tweaks a single download."""

    output: Path = Path(".")
    format: str = "auto"  # auto | video | audio | best
    quality: int | None = None  # e.g. 1080
    audio_format: str = "m4a"  # m4a | mp3 | opus | flac | wav
    playlist: bool = False
    subtitles: bool = False
    cookies: str | None = None
    proxy: str | None = None
    rate_limit: str | None = None

    def normalised(self) -> DownloadOptions:
        fmt = self.format if self.format in FORMAT_CHOICES else "auto"
        return DownloadOptions(
            output=Path(self.output).expanduser(),
            format=fmt,
            quality=self.quality,
            audio_format=self.audio_format,
            playlist=self.playlist,
            subtitles=self.subtitles,
            cookies=self.cookies,
            proxy=self.proxy,
            rate_limit=self.rate_limit,
        )


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
STATUSES = ("queued", "fetching", "downloading", "processing", "finished", "error", "cancelled")
ACTIVE_STATUSES = ("queued", "fetching", "downloading", "processing")


@dataclass
class Job:
    """Mutable, thread-safe record of one download."""

    id: str
    url: str
    options: DownloadOptions
    status: str = "queued"
    stage: str = "queued"
    progress: float = 0.0
    speed: float | None = None
    eta: int | None = None
    total: int | None = None
    downloaded: int = 0
    filename: str | None = None
    title: str | None = None
    thumbnail: str | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    ydl: Any = field(default=None, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def elapsed(self) -> float:
        if self.started is None:
            return 0.0
        return (self.finished or time.time()) - self.started

    def update(self, **kwargs: Any) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def to_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.id,
                "url": self.url,
                "status": self.status,
                "stage": self.stage,
                "progress": round(self.progress, 2),
                "speed": self.speed,
                "eta": self.eta,
                "total": self.total,
                "downloaded": self.downloaded,
                "filename": self.filename,
                "title": self.title,
                "thumbnail": self.thumbnail,
                "error": self.error,
                "warnings": list(self.warnings),
                "files": list(self.files),
                "options": {
                    "output": str(self.options.output),
                    "format": self.options.format,
                    "quality": self.options.quality,
                    "audio_format": self.options.audio_format,
                    "playlist": self.options.playlist,
                },
                "created": self.created,
                "started": self.started,
                "finished": self.finished,
                "elapsed": round(self.elapsed, 2),
                "active": self.active,
            }


# --------------------------------------------------------------------------- #
# yt-dlp option building
# --------------------------------------------------------------------------- #
def _selector(options: DownloadOptions, has_ffmpeg: bool) -> str:
    if options.format == "audio":
        return "bestaudio/best"
    if options.format == "best":
        return "bestvideo*+bestaudio/best"
    # auto / video
    if options.quality and has_ffmpeg:
        limit = int(options.quality)
        return (
            f"bestvideo[height<={limit}]+bestaudio/best[height<={limit}]/"
            f"bestvideo[height<={limit}]/best"
        )
    if options.quality:
        return f"best[height<={options.quality}]/best"
    if has_ffmpeg:
        return "bestvideo*+bestaudio/best"
    return "best"


def build_opts(
    options: DownloadOptions,
    *,
    progress_hook: Callable[[dict[str, Any]], None],
    postprocessor_hook: Callable[[dict[str, Any]], None] | None = None,
    match_filter: Callable[..., Any] | None = None,
    has_ffmpeg: bool = True,
) -> dict[str, Any]:
    """Translate :class:`DownloadOptions` into a yt-dlp options dict."""
    opts: dict[str, Any] = {
        "format": _selector(options, has_ffmpeg),
        "outtmpl": str(options.output / "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": not options.playlist,
        "ignoreerrors": False,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "concurrent_fragment_downloads": 4,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [progress_hook],
        "socket_timeout": 30,
        "retry_sleep": {"http": "exponential", "extract": "exponential"},
        # Keep titles readable but filesystem-safe on every platform.
        "restrictfilenames": False,
        "windowsfilenames": True,
        "trim_file_name": 200,
    }
    if postprocessor_hook is not None:
        opts["postprocessor_hooks"] = [postprocessor_hook]
    if match_filter is not None:
        # yt-dlp calls this right after metadata extraction, which lets a cancel
        # land before a single byte is transferred.
        opts["match_filter"] = match_filter

    if has_ffmpeg:
        opts["merge_output_format"] = "mp4"

    if options.format == "audio" and has_ffmpeg:
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": options.audio_format,
                "preferredquality": "0",
            }
        ]
        opts["format_sort"] = ["proto"]

    if options.subtitles:
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = ["en.*", "en"]
        opts["subtitlesformat"] = "best"

    if options.cookies:
        opts["cookiefile"] = options.cookies
    if options.proxy:
        opts["proxy"] = options.proxy

    rate = parse_rate(options.rate_limit)
    if rate:
        opts["ratelimit"] = rate

    return opts


# --------------------------------------------------------------------------- #
# info extraction
# --------------------------------------------------------------------------- #
def extract_info(url: str, options: DownloadOptions | None = None) -> dict[str, Any]:
    """Fetch metadata for a URL without downloading anything."""
    cfg = (options or DownloadOptions()).normalised()
    opts = build_opts(
        cfg,
        progress_hook=lambda _d: None,
        has_ffmpeg=ffmpeg_available(),
    )
    opts.pop("postprocessors", None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            # Fully resolved: fills in thumbnails, durations, uploader, entries.
            info = ydl.extract_info(url, download=False)
        except Exception:  # noqa: BLE001 - fall back to raw metadata rather than fail
            info = ydl.extract_info(url, download=False, process=False)
    return summarise(info)


def summarise(info: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten a yt-dlp info dict into something safe to hand to a UI."""
    if not info:
        return {}
    entries = info.get("entries")
    if entries is not None:
        entries = [e for e in entries if e]
        first = entries[0] if entries else {}
        return {
            "id": info.get("id"),
            "title": info.get("title") or "Playlist",
            "uploader": info.get("uploader") or info.get("channel"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail") or first.get("thumbnail"),
            "webpage_url": info.get("webpage_url") or info.get("url"),
            "is_playlist": True,
            "count": len(entries),
            "entries": [
                {
                    "id": e.get("id"),
                    "title": e.get("title"),
                    "duration": e.get("duration"),
                    "thumbnail": e.get("thumbnail"),
                }
                for e in entries[:50]
            ],
            "extractor": info.get("extractor_key") or info.get("extractor"),
        }

    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel") or info.get("creator"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or info.get("url"),
        "is_playlist": False,
        "count": 1,
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "description": (info.get("description") or "")[:400],
        "filesize": info.get("filesize") or info.get("filesize_approx"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "format_count": len(info.get("formats") or []),
    }


def collect_files(info: dict[str, Any] | None) -> list[str]:
    """Walk an extracted info dict and return every final file path."""
    found: list[str] = []

    def walk(node: dict[str, Any] | None) -> None:
        if not node:
            return
        entries = node.get("entries")
        if entries:
            for entry in entries:
                walk(entry)
            return
        path = node.get("filepath")
        if not path and node.get("requested_downloads"):
            path = node["requested_downloads"][-1].get("filepath")
        if path:
            found.append(str(path))

    walk(info)
    return found


# --------------------------------------------------------------------------- #
# download manager
# --------------------------------------------------------------------------- #
class DownloadManager:
    """Runs downloads on a small thread pool and tracks their progress."""

    def __init__(self, max_workers: int = 3, history_limit: int = 200) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="grab")
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._history_limit = history_limit

    # -- job bookkeeping ---------------------------------------------------- #
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order) if i in self._jobs]

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is None:
            return None
        if not job.active:
            return job
        job.cancel.set()
        if job.status == "fetching":
            # No progress hook fires while metadata is being extracted, so the
            # abort will land at the first hook yt-dlp calls.
            job.update(stage="cancelling")
        ydl = job.ydl
        if ydl is not None and hasattr(ydl, "cancel"):
            with contextlib.suppress(Exception):
                ydl.cancel()
        return job

    def wait(self, timeout: float | None = None) -> None:
        self._pool.shutdown(wait=True, timeout=timeout)

    def shutdown(self) -> None:
        for job in self.jobs():
            if job.active:
                job.cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- submitting --------------------------------------------------------- #
    def start(self, url: str, options: DownloadOptions | None = None) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            url=url.strip(),
            options=(options or DownloadOptions()).normalised(),
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._trim()
        self._pool.submit(self._run, job)
        return job

    def _trim(self) -> None:
        while len(self._order) > self._history_limit:
            old_id = self._order[0]
            old = self._jobs.get(old_id)
            if old is not None and old.active:
                break
            self._order.pop(0)
            self._jobs.pop(old_id, None)

    # -- running ------------------------------------------------------------ #
    def _run(self, job: Job) -> None:
        cfg = job.options
        job.update(status="fetching", stage="fetching", started=time.time())

        has_ffmpeg = ffmpeg_available()
        if cfg.format in ("audio", "auto", "video", "best") and not has_ffmpeg:
            job.warnings.append(
                "ffmpeg was not found - formats are limited to a single pre-merged stream."
            )

        def hook(data: dict[str, Any]) -> None:
            if job.cancel.is_set():
                raise DownloadCancelled("cancelled by user")

            status = data.get("status")
            info = data.get("info_dict") or {}
            if info.get("title"):
                job.update(title=info.get("title"), thumbnail=info.get("thumbnail"))

            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                downloaded = data.get("downloaded_bytes") or 0
                percent = (downloaded / total * 100) if total else None

                # Blend per-file progress when we are walking a playlist.
                index = info.get("playlist_index")
                count = info.get("playlist_count")
                if percent is not None and index and count:
                    percent = ((index - 1) + percent / 100) / count * 100
                elif percent is None and index and count:
                    percent = (index - 1) / count * 100

                job.update(
                    status="downloading",
                    stage="downloading",
                    progress=percent if percent is not None else job.progress,
                    downloaded=downloaded,
                    total=total,
                    speed=data.get("speed"),
                    eta=data.get("eta"),
                    filename=data.get("filename"),
                )
            elif status == "finished":
                job.update(
                    status="processing",
                    stage="processing",
                    progress=100.0 if not info.get("playlist_count") else job.progress,
                    filename=data.get("filename"),
                    speed=None,
                    eta=None,
                )

        def pp_hook(data: dict[str, Any]) -> None:
            if job.cancel.is_set():
                raise DownloadCancelled("cancelled by user")
            if data.get("status") == "started":
                job.update(stage="processing", status="processing", speed=None, eta=None)

        def match_filter(_info: dict[str, Any], *, incomplete: bool = False) -> None:
            if job.cancel.is_set():
                raise DownloadCancelled("cancelled by user")

        opts = build_opts(
            cfg,
            progress_hook=hook,
            postprocessor_hook=pp_hook,
            match_filter=match_filter,
            has_ffmpeg=has_ffmpeg,
        )

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                job.update(ydl=ydl)
                info = ydl.extract_info(job.url, download=True)
            job.update(
                files=collect_files(info),
                title=job.title or (info or {}).get("title"),
                thumbnail=job.thumbnail or (info or {}).get("thumbnail"),
            )
            if job.cancel.is_set():
                job.update(status="cancelled", stage="cancelled", finished=time.time())
            else:
                job.update(
                    status="finished",
                    stage="finished",
                    progress=100.0,
                    speed=None,
                    eta=None,
                    finished=time.time(),
                )
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
            message = _friendly_error(exc)
            if job.cancel.is_set():
                job.update(status="cancelled", stage="cancelled", error=None, finished=time.time())
            else:
                job.update(status="error", stage="error", error=message, finished=time.time())
        finally:
            job.update(ydl=None)


def _friendly_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    if isinstance(exc, DownloadError):
        text = re.sub(r"^ERROR:\s*", "", text)
    return text[:600]
