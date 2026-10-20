"""Command line interface for grab."""

from __future__ import annotations

import contextlib
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .core import (
    FORMAT_CHOICES,
    DownloadManager,
    DownloadOptions,
    Job,
    ensure_ffmpeg,
    extract_info,
    parse_rate,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="grab - download videos from the web, from the terminal or your browser.",
    epilog="Examples:\n  grab https://youtu.be/dQw4w9WgXcQ\n"
    "  grab -f audio --audio-format mp3 URL\n  grab --web",
)


def _console_kwargs() -> dict:
    """Keep output sane on legacy Windows consoles and redirected streams.

    stdout/stderr are reconfigured to UTF-8 (never ``strict``) so a non-ASCII
    character can never crash the CLI on a cp1252 console or when piping.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                if stream.isatty():
                    stream.reconfigure(errors="replace")
                else:
                    stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - never let console setup kill the app
            pass
    try:
        from rich.console import detect_legacy_windows

        return {"legacy_windows": detect_legacy_windows()}
    except Exception:  # noqa: BLE001
        return {}


_LEGACY = bool(_console_kwargs().get("legacy_windows"))
console = Console(**_console_kwargs())
err_console = Console(stderr=True, style="bold red", **_console_kwargs())

_TOP_LEVEL_FLAGS = {"--web", "--host", "--port", "--no-browser", "-h", "--help", "-V", "--version"}
_KNOWN_SUBCOMMANDS = {"download", "info", "web", "help"}


def _subcommands() -> set[str]:
    """Command names registered on the typer app (evaluated lazily)."""
    names = set(_KNOWN_SUBCOMMANDS)
    for info in app.registered_commands:
        name = info.name or (info.callback.__name__ if info.callback else "")
        if name:
            names.add(name)
    return names


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _fmt_duration(seconds: Any) -> str:
    if not seconds:
        return "-"
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "-"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _fmt_size(size: Any) -> str:
    if not size:
        return "-"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size:.0f} B"
        size /= 1024
    return f"{size:.1f} TB"


def _parse_quality(value: int | None) -> int | None:
    if value is None:
        return None
    if value <= 0:
        raise typer.BadParameter("quality must be a positive height in pixels")
    return value


# --------------------------------------------------------------------------- #
# top-level callback (supports `grab --web`)
# --------------------------------------------------------------------------- #
@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    web: bool = typer.Option(
        False, "--web", help="Start the web UI instead of downloading.", is_flag=True
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Web UI bind address."),
    port: int = typer.Option(8765, "--port", help="Web UI port."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser tab."),
    version: bool = typer.Option(False, "--version", "-V", help="Show the version and exit."),
) -> None:
    if version:
        console.print(f"grab [bold green]{__version__}[/]")
        raise typer.Exit()
    if web:
        _run_web(host=host, port=port, open_browser=not no_browser)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
@app.command()
def download(
    url: str = typer.Argument(..., help="The URL of the video (or playlist) to download."),
    output: Path = typer.Option(Path("."), "-o", "--output", help="Directory to save into."),
    fmt: str = typer.Option(
        "auto",
        "-f",
        "--format",
        help="What to fetch: auto, video, audio or best.",
        case_sensitive=False,
    ),
    quality: int | None = typer.Option(
        None, "-q", "--quality", help="Maximum height in pixels, e.g. 1080."
    ),
    audio_format: str = typer.Option(
        "m4a", "--audio-format", help="Audio codec: m4a, mp3, opus, flac or wav."
    ),
    playlist: bool = typer.Option(False, "--playlist", help="Download the whole playlist."),
    subtitles: bool = typer.Option(False, "--subs", help="Also save subtitles when available."),
    cookies: Path | None = typer.Option(
        None, "--cookies", help="Netscape cookie file, useful for private videos."
    ),
    proxy: str | None = typer.Option(None, "--proxy", help="Proxy URL to route requests through."),
    rate_limit: str | None = typer.Option(
        None, "--rate-limit", help="Cap download speed, e.g. 500K or 2M."
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Only print the final result."),
) -> None:
    """Download a video or audio track."""
    fmt = fmt.lower()
    if fmt not in FORMAT_CHOICES:
        err_console.print(f"unknown format {fmt!r} - choose one of: {', '.join(FORMAT_CHOICES)}")
        raise typer.Exit(2)
    if audio_format not in ("m4a", "mp3", "opus", "flac", "wav", "aac", "vorbis"):
        err_console.print(f"unsupported audio format: {audio_format}")
        raise typer.Exit(2)
    try:
        quality = _parse_quality(quality)
        parse_rate(rate_limit)
    except (typer.BadParameter, ValueError) as exc:
        err_console.print(str(exc))
        raise typer.Exit(2) from exc

    if not url.startswith(("http://", "https://")) and "://" not in url:
        if Path(url).exists():
            err_console.print("That is a local file - grab downloads URLs.")
            raise typer.Exit(2)
        url = f"https://{url}"

    options = DownloadOptions(
        output=output.expanduser(),
        format=fmt,
        quality=quality,
        audio_format=audio_format,
        playlist=playlist,
        subtitles=subtitles,
        cookies=str(cookies) if cookies else None,
        proxy=proxy,
        rate_limit=rate_limit,
    )

    if not ensure_ffmpeg():
        err_console.print(
            "[yellow]ffmpeg not found[/] - falling back to single-file streams. "
            "Install ffmpeg for merged HD video and audio extraction."
        )

    manager = DownloadManager(max_workers=1)
    job = manager.start(url, options)

    try:
        _render_job(job, quiet=quiet)
    except KeyboardInterrupt:
        manager.cancel(job.id)
        time.sleep(0.3)
        err_console.print("\nCancelled.")
        raise typer.Exit(130) from None
    finally:
        manager.shutdown()

    job = manager.get(job.id) or job
    _print_result(job)
    if job.status != "finished":
        raise typer.Exit(1)


def _render_job(job: Job, quiet: bool = False) -> None:
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        ProgressColumn,
        SpinnerColumn,
        Task,
        TaskProgressColumn,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )
    from rich.text import Text

    class _AsciiSpinner(ProgressColumn):
        """Braille frames cannot be encoded on some Windows codepages."""

        frames = "|/-\\"

        def render(self, task: Task) -> Text:
            if task.finished:
                return Text(" ")
            return Text(self.frames[int(task.get_time() * 8) % 4], style="progress.spinner")

    class _AsciiBar(ProgressColumn):
        def __init__(self, width: int = 34) -> None:
            super().__init__()
            self.width = width

        def render(self, task: Task) -> Text:
            if not task.total:
                return Text("-" * self.width, style="bar.back")
            ratio = min(1.0, max(0.0, task.completed / task.total))
            done = int(ratio * self.width)
            text = Text("=" * done, style="bar.complete")
            text.append(" " * (self.width - done), style="bar.back")
            return text

    if quiet:
        while job.active:
            time.sleep(0.2)
        return

    if _LEGACY:
        columns: list[Any] = [
            _AsciiSpinner(),
            TextColumn("[bold]{task.description}"),
            _AsciiBar(34),
            TaskProgressColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        ]
    else:
        columns = [
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=34),
            TaskProgressColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        ]

    progress = Progress(*columns, console=console, transient=False)
    with progress:
        task = progress.add_task("Starting...", total=None)
        last_stage = ""
        while True:
            data = job.to_dict()
            description = data["title"] or data["url"]
            if data["stage"] in ("fetching", "queued"):
                description = f"[dim]{data['stage'].capitalize()}[/dim] {description}"
            progress.update(
                task,
                description=description[:70],
                completed=data["downloaded"] or 0,
                total=data["total"],
            )
            if data["stage"] == "processing" and last_stage != "processing":
                progress.update(task, description=f"[cyan]Processing[/cyan] {description}"[:70])
            last_stage = data["stage"]
            if not data["active"]:
                break
            time.sleep(0.15)


def _print_result(job: Job) -> None:
    data = job.to_dict()
    if data["status"] == "finished":
        lines = []
        for path in data["files"] or [data["filename"] or ""]:
            size = Path(path).stat().st_size if Path(path).exists() else None
            lines.append(f"[green]saved[/] {path}  [dim]({_fmt_size(size)})[/]")
        if data["warnings"]:
            lines.append(f"[yellow]note[/] {'; '.join(data['warnings'])}")
        console.print(Panel("\n".join(lines) or "Done.", title="download complete", expand=False))
    elif data["status"] == "cancelled":
        console.print("[yellow]Cancelled.[/]")
    else:
        err_console.print(f"Failed: {data['error'] or 'unknown error'}")


# --------------------------------------------------------------------------- #
# info
# --------------------------------------------------------------------------- #
@app.command()
def info(
    url: str = typer.Argument(..., help="The URL to inspect."),
    formats: bool = typer.Option(False, "--formats", "-F", help="List the available formats."),
) -> None:
    """Show metadata for a URL without downloading it."""
    with console.status(f"Fetching [bold]{url}[/] ..."):
        try:
            data = extract_info(url)
        except Exception as exc:
            err_console.print(f"Could not read that URL: {exc}")
            raise typer.Exit(1) from exc

    if not data:
        err_console.print("No information returned for that URL.")
        raise typer.Exit(1)

    meta = Table(show_header=False, box=None, padding=(0, 2))
    meta.add_row("[bold]Title[/]", str(data.get("title") or "-"))
    meta.add_row("[bold]Channel[/]", str(data.get("uploader") or "-"))
    meta.add_row("[bold]Duration[/]", _fmt_duration(data.get("duration")))
    meta.add_row("[bold]Type[/]", "Playlist" if data.get("is_playlist") else "Video")
    meta.add_row("[bold]Items[/]", str(data.get("count") or 1))
    if data.get("extractor"):
        meta.add_row("[bold]Source[/]", str(data["extractor"]))
    if data.get("filesize"):
        meta.add_row("[bold]Size[/]", _fmt_size(data["filesize"]))
    console.print(Panel(meta, title="metadata", expand=False))

    if data.get("is_playlist"):
        table = Table(title="playlist items", box=None)
        table.add_column("#", justify="right", style="dim")
        table.add_column("Title")
        table.add_column("Length", justify="right")
        for index, entry in enumerate(data.get("entries") or [], start=1):
            table.add_row(
                str(index), str(entry.get("title") or "-"), _fmt_duration(entry.get("duration"))
            )
        console.print(table)

    if formats:
        _show_formats(url)


def _show_formats(url: str) -> None:
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        raw = ydl.extract_info(url, download=False, process=False)
    if not raw:
        return
    table = Table(title="formats", box=None)
    table.add_column("ID", style="cyan")
    table.add_column("Ext")
    table.add_column("Resolution")
    table.add_column("FPS", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Note", overflow="ignore", max_width=32)
    for fmt in raw.get("formats") or []:
        height = fmt.get("height")
        table.add_row(
            str(fmt.get("format_id")),
            str(fmt.get("ext") or "-"),
            f"{height}p" if height else (fmt.get("format_note") or "-"),
            str(fmt.get("fps") or "-"),
            _fmt_size(fmt.get("filesize") or fmt.get("filesize_approx")),
            str(fmt.get("format_note") or ""),
        )
    console.print(table)


# --------------------------------------------------------------------------- #
# web
# --------------------------------------------------------------------------- #
def _run_web(host: str, port: int, open_browser: bool = True) -> None:
    import uvicorn

    from .server import create_app

    banner = (
        f"[bold green]grab[/] web UI ready\n  local:   http://{host}:{port}\n  press Ctrl+C to stop"
    )
    console.print(Panel(banner, expand=False))
    if open_browser:
        threading.Timer(1.0, lambda: _open_browser(f"http://{host}:{port}")).start()
    try:
        uvicorn.run(create_app(), host=host, port=port, log_level="warning")
    except KeyboardInterrupt:
        console.print("\n[dim]Server stopped.[/]")


def _open_browser(url: str) -> None:
    with contextlib.suppress(Exception):
        webbrowser.open(url)


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8765, "--port", help="Port to listen on."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser tab."),
) -> None:
    """Start the web UI."""
    _run_web(host=host, port=port, open_browser=not no_browser)


@app.command()
def doctor() -> None:
    """Check that grab has everything it needs to work."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    import yt_dlp

    table.add_row("[bold]grab[/]", __version__)
    table.add_row("[bold]yt-dlp[/]", yt_dlp.version.__version__)
    table.add_row("[bold]python[/]", sys.version.split()[0])
    ffmpeg = ensure_ffmpeg()
    table.add_row(
        "[bold]ffmpeg[/]",
        f"[green]{ffmpeg}[/]" if ffmpeg else "[red]missing - HD merging disabled[/]",
    )
    console.print(Panel(table, title="environment", expand=False))


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    """Entry point that lets `grab <url>` behave like `grab download <url>`."""
    argv = sys.argv[1:]
    if argv:
        first = argv[0]
        if first not in _subcommands() and first not in _TOP_LEVEL_FLAGS:
            argv = ["download", *argv]
    try:
        app(args=argv)
    except KeyboardInterrupt:
        err_console.print("\nInterrupted.")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
