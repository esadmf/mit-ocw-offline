from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

import config
from db import SessionLocal, init_db
from db.models import Asset, Course

app = FastAPI(title="MIT OCW Offline")
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.on_event("startup")
def _startup():
    config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    # Mount the storage directory so templates can reference /files/...
    if not any(r.path == "/files" for r in app.routes):
        app.mount("/files", StaticFiles(directory=str(config.STORAGE_DIR)), name="files")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """
    Returns 200 + JSON when the service is fully operational.
    Returns 503 if the database is unreachable.
    Designed to be polled by external monitoring / dashboard tools.
    """
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        course_count = db.query(Course).count()
        completed_count = db.query(Course).filter_by(status="completed").count()
        storage_ok = config.STORAGE_DIR.exists()
        return {
            "status": "healthy",
            "database": "connected",
            "courses_in_catalog": course_count,
            "courses_downloaded": completed_count,
            "storage_mounted": storage_ok,
        }
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "database": str(exc)},
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = "",
    status: str = "",
    department: str = "",
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

    courses = query.order_by(Course.title).all()

    total = db.query(Course).count()
    completed = db.query(Course).filter_by(status="completed").count()
    pending = db.query(Course).filter_by(status="pending").count()

    departments = sorted(
        {c.department for c in db.query(Course).all() if c.department}
    )
    db.close()

    return TEMPLATES.TemplateResponse(
        "index.html",
        {
            "request": request,
            "courses": courses,
            "total": total,
            "completed": completed,
            "pending": pending,
            "departments": departments,
            "q": q,
            "status_filter": status,
            "department_filter": department,
        },
    )


@app.get("/course/{slug}", response_class=HTMLResponse)
async def course_detail(request: Request, slug: str):
    db = SessionLocal()
    course = db.query(Course).filter_by(slug=slug).first()
    if not course:
        db.close()
        return HTMLResponse("Course not found", status_code=404)

    assets = db.query(Asset).filter_by(course_id=course.id, status="completed").all()
    pdfs = [a for a in assets if a.asset_type == "pdf"]
    videos = [a for a in assets if a.asset_type == "video"]
    archives = [a for a in assets if a.asset_type == "archive"]
    other = [a for a in assets if a.asset_type not in ("pdf", "video", "archive", "html")]

    section_pages: list[Path] = []
    if course.local_path:
        pages_dir = Path(course.local_path) / "pages"
        if pages_dir.exists():
            section_pages = sorted(p for p in pages_dir.glob("*.html") if p.name != "index.html")

    db.close()
    return TEMPLATES.TemplateResponse(
        "course.html",
        {
            "request": request,
            "course": course,
            "pdfs": pdfs,
            "videos": videos,
            "archives": archives,
            "other": other,
            "section_pages": section_pages,
        },
    )


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
