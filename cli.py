"""
MIT OCW Offline — CLI entry point

Usage:
  python cli.py init
  python cli.py fetch-catalog [--limit N] [--skip-metadata]
  python cli.py list [--status STATUS] [--department DEPT] [--limit N]
  python cli.py download SLUG [--force]
  python cli.py download-all [--subject DEPT] [--workers N] [--status STATUS]
  python cli.py serve [--host HOST] [--port PORT]
  python cli.py status [SLUG]
"""
import asyncio
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

cli = typer.Typer(
    name="mit-ocw",
    help="MIT OpenCourseWare offline downloader and server.",
    no_args_is_help=True,
)
console = Console()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@cli.command()
def init():
    """Create the database and storage directory."""
    import config
    from db import init_db

    config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    console.print("[green]✓ Initialised.[/green]")
    console.print(f"  Database : {config.DB_PATH}")
    console.print(f"  Storage  : {config.STORAGE_DIR}")


# ---------------------------------------------------------------------------
# fetch-catalog
# ---------------------------------------------------------------------------

@cli.command("fetch-catalog")
def fetch_catalog(
    limit: Optional[int] = typer.Option(
        None, "--limit", "-n",
        help="Cap the number of courses fetched (useful for testing).",
    ),
    skip_metadata: bool = typer.Option(
        False, "--skip-metadata",
        help="Only store URLs; skip fetching per-course metadata pages.",
    ),
):
    """Fetch the full MIT OCW course catalog into the local database."""
    from db import init_db
    import config
    config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

    from downloader.catalog import fetch_catalog as _fc
    asyncio.run(_fc(limit=limit, skip_metadata=skip_metadata))


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@cli.command("list")
def list_courses(
    status_filter: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by status."),
    department: Optional[str] = typer.Option(None, "--department", "-d", help="Filter by department."),
    limit: int = typer.Option(25, "--limit", "-n", help="Max rows to show."),
):
    """List courses in the local catalog."""
    from db import SessionLocal
    from db.models import Course

    db = SessionLocal()
    q = db.query(Course)
    if status_filter:
        q = q.filter_by(status=status_filter)
    if department:
        q = q.filter(Course.department.ilike(f"%{department}%"))
    courses = q.order_by(Course.title).limit(limit).all()
    total = db.query(Course).count()
    db.close()

    table = Table(title=f"Courses (showing {len(courses)} of {total})")
    table.add_column("Slug", style="dim", max_width=38, no_wrap=True)
    table.add_column("Title", max_width=48)
    table.add_column("Dept", max_width=18)
    table.add_column("Status", max_width=12)

    for c in courses:
        table.add_row(c.slug, c.title or "—", c.department or "—", c.status)
    console.print(table)


# ---------------------------------------------------------------------------
# download (single course)
# ---------------------------------------------------------------------------

@cli.command()
def download(
    slug: str = typer.Argument(help="Course slug (from 'list' command or the OCW URL)."),
    force: bool = typer.Option(False, "--force", help="Re-download even if already completed."),
):
    """Download a single course by its slug."""
    import config
    from db import init_db
    config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

    if force:
        from db import SessionLocal
        from db.models import Course
        db = SessionLocal()
        course = db.query(Course).filter_by(slug=slug).first()
        if course:
            course.status = "pending"
            db.commit()
        db.close()

    from downloader.crawler import download_course
    asyncio.run(download_course(slug))


# ---------------------------------------------------------------------------
# download-all
# ---------------------------------------------------------------------------

@cli.command("download-all")
def download_all(
    subject: Optional[str] = typer.Option(
        None, "--subject",
        help="Only download courses whose department contains this string.",
    ),
    workers: int = typer.Option(
        1, "--workers", "-w",
        help="Number of courses to download in parallel (keep low to be polite).",
    ),
    status_filter: str = typer.Option(
        "pending", "--status",
        help="Only download courses with this status (default: pending).",
    ),
):
    """Download all (or filtered) courses from the catalog."""
    import config
    from db import SessionLocal, init_db
    from db.models import Course
    from downloader.crawler import download_course

    config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

    db = SessionLocal()
    q = db.query(Course).filter(Course.status == status_filter)
    if subject:
        q = q.filter(Course.department.ilike(f"%{subject}%"))
    slugs = [c.slug for c in q.all()]
    db.close()

    if not slugs:
        console.print("[yellow]No matching courses found.[/yellow]")
        raise typer.Exit()

    console.print(f"[bold]Downloading {len(slugs)} courses (workers={workers})…[/bold]")

    async def _run():
        sem = asyncio.Semaphore(workers)
        async def guarded(s: str):
            async with sem:
                await download_course(s)
        await asyncio.gather(*[guarded(s) for s in slugs])

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

@cli.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8080, "--port", "-p"),
):
    """Start the local web server."""
    import uvicorn
    console.print(f"[bold green]Server running at http://{host}:{port}[/bold green]")
    uvicorn.run("server.main:app", host=host, port=port, reload=False)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
def status(
    slug: Optional[str] = typer.Argument(None, help="Show detailed status for one course."),
):
    """Show overall download progress, or detail for a specific course."""
    from db import SessionLocal
    from db.models import Asset, Course

    db = SessionLocal()

    if slug:
        course = db.query(Course).filter_by(slug=slug).first()
        if not course:
            console.print(f"[red]Not found: {slug}[/red]")
            db.close()
            raise typer.Exit(1)

        assets = db.query(Asset).filter_by(course_id=course.id).all()
        console.print(f"[bold]{course.title or slug}[/bold]")
        console.print(f"  Status   : {course.status}")
        console.print(f"  URL      : {course.url}")
        if course.local_path:
            console.print(f"  Path     : {course.local_path}")
        if course.total_size_bytes:
            console.print(f"  Size     : {course.total_size_bytes / 1_073_741_824:.2f} GB")
        console.print(f"  Assets   : {len(assets)} total")
        for s in ("completed", "pending", "failed", "skipped"):
            n = sum(1 for a in assets if a.status == s)
            if n:
                console.print(f"    {s}: {n}")
        if course.download_error:
            console.print(f"  [red]Error: {course.download_error}[/red]")
    else:
        table = Table(title="Catalog Summary")
        table.add_column("Status")
        table.add_column("Courses", justify="right")
        for s in ("pending", "downloading", "completed", "failed"):
            n = db.query(Course).filter_by(status=s).count()
            table.add_row(s, str(n))
        console.print(table)

        total_size = db.query(Asset).filter_by(status="completed").with_entities(
            __import__("sqlalchemy").func.sum(Asset.size_bytes)
        ).scalar() or 0
        console.print(f"\nTotal downloaded: {total_size / 1_073_741_824:.2f} GB")

    db.close()


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
