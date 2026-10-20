# grab

A video downloader that runs entirely on your machine — with a **CLI** and an optional **web UI**
(`--web`), both driven by the same engine.

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) + ffmpeg, packaged with [uv](https://docs.astral.sh/uv/).

## Install / run

```bash
uv sync              # create the environment and install dependencies
uv run grab --help   # run the CLI
uv run grab doctor   # verify yt-dlp / ffmpeg are ready
```

Want it as a global `grab` command?

```bash
uv tool install .
grab --help
```

## CLI

```bash
# download a video (best quality, auto format selection)
uv run grab https://youtu.be/dQw4w9WgXcQ

# audio only, converted to mp3
uv run grab -f audio --audio-format mp3 https://youtu.be/dQw4w9WgXcQ

# cap height at 720p, save somewhere specific
uv run grab -f video -q 720 -o ~/Videos https://example.com/watch?v=abc

# whole playlist + subtitles, throttled to 2 MB/s
uv run grab --playlist --subs --rate-limit 2M <url>

# inspect first, download later
uv run grab info <url>
uv run grab info --formats <url>

# start the web UI
uv run grab --web                 # http://127.0.0.1:8765 (opens your browser)
uv run grab web --port 9000 --no-browser
```

Every flag from `grab --help` is also available under `grab download --help`.

| Flag | Description |
| --- | --- |
| `-f, --format` | `auto` \| `video` \| `audio` \| `best` |
| `-q, --quality` | maximum height in pixels (`1080`, `720`, …) |
| `--audio-format` | `m4a` \| `mp3` \| `opus` \| `flac` \| `wav` |
| `-o, --output` | destination directory (defaults to the current one) |
| `--playlist` | download every entry of a playlist |
| `--subs` | also save subtitles when available |
| `--cookies` | Netscape cookie file for private/members-only videos |
| `--proxy` | route requests through a proxy |
| `--rate-limit` | e.g. `500K`, `2M` |
| `--quiet` | print only the final result |

## Web UI

```bash
uv run grab --web
```

Paste a link → inspect the metadata → pick video/audio, height and codec → watch the live
progress bar → hit **save file**. Jobs keep running in the background while you queue more,
and each one can be cancelled mid-flight.

The server is FastAPI (`grab/server.py`), the front end is a single static page
(`grab/static/index.html`) — no build step, no node.

### API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/info` | metadata for a URL |
| `POST` | `/api/download` | queue a download |
| `GET` | `/api/jobs` | all jobs with live progress |
| `POST` | `/api/jobs/{id}/cancel` | cancel a running job |
| `GET` | `/api/jobs/{id}/file` | stream a finished file back |
| `GET` | `/api/health` | liveness/version |

Interactive docs live at `/api/docs`.

## How it works

```
grab/
├── pyproject.toml      # uv-managed project, `grab` console script
└── grab/
    ├── core.py         # DownloadManager, job model, yt-dlp option building
    ├── cli.py          # typer + rich CLI (progress bar, tables, doctor)
    ├── server.py       # FastAPI app wrapping the same DownloadManager
    └── static/
        └── index.html  # the web UI (vanilla JS, polls /api/jobs)
```

- **One engine, two faces.** The CLI and the web UI both drive `DownloadManager`, so progress,
  cancellation and format logic can never drift apart.
- **Jobs are threads, not processes.** A small `ThreadPoolExecutor` (3 slots in the web UI)
  runs downloads; progress hooks stream bytes/s, ETA and percent into a thread-safe job record.
- **Cancellation** sets an event that raises inside yt-dlp's progress hook and calls
  `YoutubeDL.cancel()`, so a job stops within a single chunk.
- **ffmpeg** is resolved from `PATH` first; if it's missing, `static-ffmpeg` supplies a bundled
  build on demand. Without ffmpeg at all, grab falls back to single-file streams instead of
  failing.
- **Format logic:** `auto` picks `bestvideo*+bestaudio` (merged to mp4) with an optional
  `height<=N` ceiling; `audio` picks `bestaudio` and runs an ffmpeg extraction to your codec.

## Troubleshooting

- **"ffmpeg was not found"** — install it (`winget install ffmpeg`, `brew install ffmpeg`,
  `apt install ffmpeg`) or just let `static-ffmpeg` fetch it on first use.
- **Login-required videos** — export cookies from your browser
  ([Get cookies.txt](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc))
  and pass `--cookies cookies.txt`.
- **HTTP 403 / bot checks** — update yt-dlp: `uv lock --upgrade-package yt-dlp && uv sync`.
