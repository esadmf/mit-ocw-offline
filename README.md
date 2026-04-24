# MIT OCW Offline

A self-hosted tool that downloads [MIT OpenCourseWare](https://ocw.mit.edu) courses — including lecture notes, problem sets, and videos — and serves them through a local web UI for offline access.

MIT OCW content is freely available for personal offline use under the [Creative Commons BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) license.

---

## Features

- Downloads the full OCW course catalog (~2,400+ courses)
- Fetches PDFs, lecture notes, problem sets, and all linked assets
- Downloads videos via [yt-dlp](https://github.com/yt-dlp/yt-dlp) at up to 1080p
- Tracks download progress in a local SQLite database
- Resumes interrupted downloads — safe to stop and restart
- Clean web UI for browsing and searching your offline library
- All data lives on a mounted volume — move the container to any host without re-downloading

---

## Requirements

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/install/) (included with Docker Desktop)

---

## Quick start

```bash
git clone <repo-url>
cd mit-ocw-offline

# 1. Create your local config
cp .env.example .env

# 2. Edit .env — at minimum set DATA_PATH to where you want files stored
#    Windows example:  DATA_PATH=D:/NAS/ocw
#    Linux example:    DATA_PATH=/mnt/array/ocw

# 3. Build and start the web server
docker compose up -d --build

# 4. Populate the course catalog
docker compose run --rm worker fetch-catalog

# 5. Download a course to verify everything works
docker compose run --rm worker list
docker compose run --rm worker download <slug>

# 6. Open the UI
#    http://localhost:8080
```

---

## Configuration

All configuration lives in `.env`. Copy `.env.example` to get started — it contains every available option with descriptions. You never need to edit `docker-compose.yml` directly.

| Variable | Default | Description |
|---|---|---|
| `DATA_PATH` | `./data` | Host path for the database and all downloaded files. Point this at your NAS or storage array. |
| `OCW_PORT` | `8080` | Host port the web UI is served on. |
| `OCW_REQUEST_DELAY` | `1.0` | Seconds to wait between HTTP requests to OCW. Keep at `0.5` or above to be a good citizen. |
| `OCW_CONCURRENT` | `3` | Number of parallel HTTP connections during a download. |
| `OCW_DOWNLOAD_VIDEOS` | `true` | Set to `false` to skip video downloads entirely. |
| `OCW_MAX_FILE_MB` | `5000` | Skip any single file larger than this many MB. |

---

## CLI reference

All commands are run via the `worker` service so they share the same data volume as the server.

### Catalog

```bash
# Fetch the full course catalog (URLs + metadata)
docker compose run --rm worker fetch-catalog

# Fetch only a small batch — useful for testing
docker compose run --rm worker fetch-catalog --limit 10

# Fetch URLs only, skip per-course metadata pages (faster)
docker compose run --rm worker fetch-catalog --skip-metadata
```

### Browsing the catalog

```bash
# List courses (default: 25 rows)
docker compose run --rm worker list

# Filter by status or department
docker compose run --rm worker list --status pending
docker compose run --rm worker list --department physics --limit 50
```

### Downloading

```bash
# Download a single course by slug
docker compose run --rm worker download <slug>

# Force re-download of an already-completed course
docker compose run --rm worker download <slug> --force

# Download all pending courses (1 at a time by default)
docker compose run --rm worker download-all

# Download all pending courses, 2 at a time
docker compose run --rm worker download-all --workers 2

# Download only courses in a specific department
docker compose run --rm worker download-all --subject "electrical engineering"

# Re-run any previously failed downloads
docker compose run --rm worker download-all --status failed
```

### Status

```bash
# Overall progress summary
docker compose run --rm worker status

# Detail for a specific course
docker compose run --rm worker status <slug>
```

---

## Data layout

Everything is stored under `DATA_PATH` on the host (mounted as `/data` in the container):

```
/data
├── ocw.db              ← SQLite database (catalog, download status, asset index)
└── storage/
    └── <course-slug>/
        ├── meta.json   ← Course metadata
        ├── pages/      ← Downloaded HTML pages
        ├── assets/
        │   ├── pdf/    ← Lecture notes, problem sets, exams
        │   ├── image/
        │   └── archive/
        └── videos/     ← Downloaded MP4s (one file per YouTube video)
```

The container itself is stateless. Moving to a new host only requires pointing `DATA_PATH` at the same storage location — no re-downloading needed.

---

## Running without Docker

If you prefer to run directly with Python:

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt

python cli.py init
python cli.py fetch-catalog --limit 10
python cli.py download <slug>
python cli.py serve
```

> **Note:** You will need [ffmpeg](https://ffmpeg.org/download.html) installed and on your `PATH` for video downloads to work.

---

## Project structure

```
mit-ocw-offline/
├── cli.py                  ← CLI entry point (all commands)
├── config.py               ← Settings (reads from environment / .env)
├── db/
│   ├── __init__.py         ← SQLAlchemy engine + session factory
│   └── models.py           ← Course and Asset models
├── downloader/
│   ├── catalog.py          ← Sitemap crawler + metadata extraction
│   ├── crawler.py          ← Per-course page and asset downloader
│   └── video.py            ← yt-dlp wrapper for YouTube videos
├── server/
│   ├── main.py             ← FastAPI application
│   └── templates/          ← Jinja2 HTML templates
├── Dockerfile
├── docker-compose.yml
├── .env.example            ← Template — copy to .env and edit
└── requirements.txt
```

---

## License

This tool is for **personal offline use only**.

MIT OpenCourseWare course content is © MIT, licensed under [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International](https://creativecommons.org/licenses/by-nc-sa/4.0/). See [MIT OCW Terms of Use](https://ocw.mit.edu/terms/) for full details.
