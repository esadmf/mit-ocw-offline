import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, text

import config
from db import SessionLocal, init_db
from db.models import Asset, Course

app = FastAPI(title="MIT OCW Offline")
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")

# ---------------------------------------------------------------------------
# Background task state
_active_downloads: dict[str, asyncio.Task] = {}  # slug -> Task
_bulk_task: asyncio.Task | None = None            # bulk course download
_catalog_task: asyncio.Task | None = None         # catalog fetch
# ---------------------------------------------------------------------------


def _running_slugs() -> set[str]:
    return {s for s, t in _active_downloads.items() if not t.done()}


async def _bulk_download(statuses: list[str]):
    """Download every course whose status is in `statuses`, 2 at a time."""
    from downloader.crawler import download_course
    db = SessionLocal()
    slugs = [c.slug for c in db.query(Course).filter(Course.status.in_(statuses)).all()]
    db.close()
    sem = asyncio.Semaphore(2)
    async def guarded(slug: str):
        async with sem:
            await download_course(slug)
    await asyncio.gather(*[guarded(s) for s in slugs])


@app.on_event("startup")
def _startup():
    config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    db = SessionLocal()
    # Migrate: old "pending" (catalog-only, never requested) → "available".
    migrated = db.query(Course).filter_by(status="pending").all()
    for c in migrated:
        c.status = "available"
    # Anything left as "downloading" from a previous run never finished — reset it.
    stuck = db.query(Course).filter_by(status="downloading").all()
    for c in stuck:
        c.status = "available"
    if migrated or stuck:
        db.commit()
    db.close()
    if not any(r.path == "/files" for r in app.routes):
        app.mount("/files", StaticFiles(directory=str(config.STORAGE_DIR)), name="files")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        course_count = db.query(Course).count()
        completed_count = db.query(Course).filter_by(status="completed").count()
        return {
            "status": "healthy",
            "database": "connected",
            "courses_in_catalog": course_count,
            "courses_downloaded": completed_count,
            "storage_mounted": config.STORAGE_DIR.exists(),
        }
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "database": str(exc)})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Library (browse downloaded courses)
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = "",
    status: str = "",
    department: str = "",
    year: str = "",
    level: str = "",
    sort: str = "status",
):
    db = SessionLocal()
    query = db.query(Course)
    if q:
        like = f"%{q}%"
        query = query.filter(
            Course.title.ilike(like)
            | Course.description.ilike(like)
            | Course.department.ilike(like)
            | Course.course_number.ilike(like)
        )
    if status:
        query = query.filter(Course.status == status)
    if department:
        query = query.filter(Course.department == department)
    if year:
        query = query.filter(Course.year == int(year))
    if level:
        query = query.filter(Course.level == level)

    status_order = case(
        (Course.status == "completed",   0),
        (Course.status == "downloading", 1),
        (Course.status == "failed",      2),
        else_=3,
    )
    if sort == "year":
        courses = query.order_by(Course.year.desc().nulls_last(), Course.title).all()
    elif sort == "department":
        courses = query.order_by(Course.department.nulls_last(), Course.course_number, Course.title).all()
    elif sort == "title":
        courses = query.order_by(Course.title).all()
    elif sort == "number":
        courses = query.order_by(Course.course_number.nulls_last(), Course.title).all()
    else:  # "status" — downloaded first
        courses = query.order_by(status_order, Course.title).all()

    total      = db.query(Course).count()
    completed  = db.query(Course).filter_by(status="completed").count()
    available  = db.query(Course).filter_by(status="available").count()
    from sqlalchemy import distinct
    departments = sorted(
        r[0] for r in db.query(distinct(Course.department))
        .filter(Course.department.isnot(None)).all()
    )
    years = sorted(
        (r[0] for r in db.query(distinct(Course.year))
         .filter(Course.year.isnot(None)).all()),
        reverse=True,
    )
    levels = sorted(
        r[0] for r in db.query(distinct(Course.level))
        .filter(Course.level.isnot(None)).all()
    )
    db.close()

    return TEMPLATES.TemplateResponse(request, "index.html", {
        "courses": courses,
        "total": total,
        "completed": completed,
        "available": available,
        "departments": departments,
        "years": years,
        "levels": levels,
        "q": q,
        "status_filter": status,
        "department_filter": department,
        "year_filter": year,
        "level_filter": level,
        "sort": sort,
    })


# ---------------------------------------------------------------------------
# Catalog (browse + trigger downloads)
# ---------------------------------------------------------------------------

@app.get("/catalog", response_class=HTMLResponse)
async def catalog(
    request: Request,
    q: str = "",
    status: str = "",
    department: str = "",
    started: int = 0,
):
    db = SessionLocal()
    query = db.query(Course)
    if q:
        like = f"%{q}%"
        query = query.filter(
            Course.title.ilike(like)
            | Course.description.ilike(like)
            | Course.department.ilike(like)
            | Course.course_number.ilike(like)
        )
    if status:
        query = query.filter(Course.status == status)
    if department:
        query = query.filter(Course.department.ilike(f"%{department}%"))

    courses = query.order_by(Course.department, Course.course_number, Course.title).all()

    counts = {
        "total":       db.query(Course).count(),
        "available":   db.query(Course).filter_by(status="available").count(),
        "downloading": db.query(Course).filter_by(status="downloading").count(),
        "completed":   db.query(Course).filter_by(status="completed").count(),
        "failed":      db.query(Course).filter_by(status="failed").count(),
    }
    departments = sorted({c.department for c in db.query(Course).all() if c.department})
    db.close()

    global _bulk_task, _catalog_task
    bulk_running    = _bulk_task    is not None and not _bulk_task.done()
    catalog_running = _catalog_task is not None and not _catalog_task.done()

    return TEMPLATES.TemplateResponse(request, "catalog.html", {
        "courses": courses,
        "counts": counts,
        "departments": departments,
        "running_slugs": _running_slugs(),
        "bulk_running": bulk_running,
        "catalog_running": catalog_running,
        "q": q,
        "status_filter": status,
        "department_filter": department,
        "started": started,
    })


# ---------------------------------------------------------------------------
# Catalog fetch trigger
# ---------------------------------------------------------------------------

@app.post("/api/fetch-catalog")
async def api_fetch_catalog(skip_metadata: bool = False):
    global _catalog_task
    if _catalog_task is not None and not _catalog_task.done():
        return RedirectResponse("/catalog", status_code=303)
    from downloader.catalog import fetch_catalog
    _catalog_task = asyncio.create_task(fetch_catalog(skip_metadata=skip_metadata))
    return RedirectResponse("/catalog", status_code=303)


# ---------------------------------------------------------------------------
# Download triggers (POST → redirect back to catalog)
# ---------------------------------------------------------------------------

@app.post("/api/download/{slug}")
async def api_download_one(slug: str, request: Request):
    global _active_downloads
    db = SessionLocal()
    course = db.query(Course).filter_by(slug=slug).first()
    db.close()

    if not course:
        return JSONResponse({"error": "not found"}, status_code=404)

    if slug in _running_slugs():
        # Already running — just bounce back
        return RedirectResponse("/catalog", status_code=303)

    from downloader.crawler import download_course
    _active_downloads[slug] = asyncio.create_task(download_course(slug))

    # Redirect back to wherever the request came from (catalog or course page)
    referer = request.headers.get("referer", "/catalog")
    return RedirectResponse(referer, status_code=303)


@app.post("/api/download-all")
async def api_download_all(include_failed: bool = False):
    global _bulk_task

    if _bulk_task is not None and not _bulk_task.done():
        return RedirectResponse("/catalog?started=0", status_code=303)

    statuses = ["available", "failed"] if include_failed else ["available"]

    db = SessionLocal()
    count = db.query(Course).filter(Course.status.in_(statuses)).count()
    db.close()

    _bulk_task = asyncio.create_task(_bulk_download(statuses))
    return RedirectResponse(f"/catalog?started={count}", status_code=303)


# ---------------------------------------------------------------------------
# Course detail
# ---------------------------------------------------------------------------

@app.get("/course/{slug}", response_class=HTMLResponse)
async def course_detail(request: Request, slug: str):
    db = SessionLocal()
    course = db.query(Course).filter_by(slug=slug).first()
    if not course:
        db.close()
        return HTMLResponse("Course not found", status_code=404)

    assets = db.query(Asset).filter_by(course_id=course.id, status="completed").all()
    pdfs     = [a for a in assets if a.asset_type == "pdf"]
    videos   = [a for a in assets if a.asset_type == "video"]
    archives = [a for a in assets if a.asset_type == "archive"]
    other    = [a for a in assets if a.asset_type not in ("pdf", "video", "archive", "html")]

    section_pages: list[Path] = []
    if course.local_path:
        pages_dir = Path(course.local_path) / "pages"
        if pages_dir.exists():
            section_pages = sorted(p for p in pages_dir.glob("*.html") if p.name != "index.html")

    # Auto-extract the OCW offline site zip if it exists but hasn't been extracted yet.
    site_url = None
    if course.local_path and course.status == "completed":
        from downloader.crawler import extract_site_zip
        course_dir = Path(course.local_path)
        if not (course_dir / "site" / "index.html").exists():
            await asyncio.to_thread(extract_site_zip, course_dir)
        if (course_dir / "site" / "index.html").exists():
            site_url = f"/files/{slug}/site/index.html"

    db.close()
    return TEMPLATES.TemplateResponse(request, "course.html", {
        "course": course,
        "pdfs": pdfs,
        "videos": videos,
        "archives": archives,
        "other": other,
        "section_pages": section_pages,
        "is_downloading": slug in _running_slugs(),
        "site_url": site_url,
    })


# ---------------------------------------------------------------------------
# Asset serving
# ---------------------------------------------------------------------------

@app.get("/serve/{slug}/{asset_type}/{filename:path}")
async def serve_asset(slug: str, asset_type: str, filename: str):
    path = config.STORAGE_DIR / slug / "assets" / asset_type / filename
    if path.exists():
        return FileResponse(str(path))
    return HTMLResponse("File not found", status_code=404)


@app.get("/video/{slug}/{filename:path}")
async def serve_video(slug: str, filename: str):
    path = config.STORAGE_DIR / slug / "videos" / filename
    if path.exists():
        return FileResponse(str(path), media_type="video/mp4")
    return HTMLResponse("File not found", status_code=404)


@app.get("/page/{slug}/{page_name}", response_class=HTMLResponse)
async def serve_page(slug: str, page_name: str):
    path = config.STORAGE_DIR / slug / "pages" / f"{page_name}.html"
    if path.exists():
        return HTMLResponse(path.read_text(encoding="utf-8", errors="replace"))
    return HTMLResponse("Page not found", status_code=404)
