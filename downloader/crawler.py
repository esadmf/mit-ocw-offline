"""
Download all pages and assets for a single MIT OCW course.
"""
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from rich.console import Console

import config
from db import SessionLocal
from db.models import Asset, Course

console = Console()

HEADERS = {
    "User-Agent": (
        "MIT-OCW-Offline-Archiver/1.0 "
        "(personal offline archival; https://ocw.mit.edu/terms)"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
}

# File extensions → asset_type
EXT_TYPE: dict[str, str] = {
    ".pdf": "pdf",
    ".zip": "archive", ".tar": "archive", ".gz": "archive",
    ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".gif": "image", ".svg": "image", ".webp": "image",
    ".mp4": "video", ".webm": "video", ".avi": "video", ".mov": "video",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize(name: str, max_len: int = 180) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    safe = re.sub(r"[_\s]+", "_", safe).strip("_. ")
    return safe[:max_len] or "file"


def _ext_type(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext, t in EXT_TYPE.items():
        if path.endswith(ext):
            return t
    return "other"


def _youtube_ids(html: str) -> list[str]:
    patterns = [
        r"youtube\.com/embed/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})",
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/v/([a-zA-Z0-9_-]{11})",
        r'"videoId"\s*:\s*"([a-zA-Z0-9_-]{11})"',
    ]
    ids: list[str] = []
    for p in patterns:
        ids.extend(re.findall(p, html))
    return list(dict.fromkeys(ids))


def _section_links(soup: BeautifulSoup, course_url: str) -> list[str]:
    """Collect all sub-page URLs that belong to this course."""
    base_path = urlparse(course_url).path.rstrip("/")
    seen: set[str] = set()
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].split("#")[0].split("?")[0]
        if not href:
            continue
        if href.startswith("/"):
            full = config.OCW_BASE_URL + href
        elif href.startswith("http"):
            full = href
        else:
            full = urljoin(course_url, href)
        full = full.rstrip("/")
        parsed = urlparse(full)
        if (
            parsed.netloc == "ocw.mit.edu"
            and parsed.path.startswith(base_path + "/")
            and full not in seen
        ):
            seen.add(full)
            links.append(full)
    return links


def _asset_links(soup: BeautifulSoup, page_url: str) -> list[tuple[str, str]]:
    """Return (absolute_url, asset_type) for all downloadable assets on a page."""
    seen: set[str] = set()
    results: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].split("#")[0]
        if not href:
            continue
        if href.startswith("/"):
            full = config.OCW_BASE_URL + href
        elif href.startswith("http"):
            full = href
        else:
            full = urljoin(page_url, href)
        if full in seen:
            continue
        t = _ext_type(full)
        if t != "other" or full.startswith(config.OCW_BASE_URL):
            seen.add(full)
            results.append((full, t))
    return results


# ---------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------

async def _download_file(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
) -> Optional[int]:
    if config.SKIP_EXISTING and dest.exists() and dest.stat().st_size > 0:
        return dest.stat().st_size
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with client.stream("GET", url, follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            cl = r.headers.get("content-length")
            if cl and int(cl) > config.MAX_FILE_SIZE_MB * 1024 * 1024:
                console.print(f"    [yellow]Skipping oversized file: {url}[/yellow]")
                return None
            size = 0
            with open(dest, "wb") as fh:
                async for chunk in r.aiter_bytes(65536):
                    fh.write(chunk)
                    size += len(chunk)
            return size
    except Exception as exc:
        console.print(f"    [red]Download failed {url}: {exc}[/red]")
        if dest.exists():
            dest.unlink(missing_ok=True)
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def download_course(slug: str):
    db = SessionLocal()
    course = db.query(Course).filter_by(slug=slug).first()

    if not course:
        console.print(f"[red]Course not found in catalog: {slug}[/red]")
        db.close()
        return

    if course.status == "completed" and config.SKIP_EXISTING:
        console.print(f"[yellow]Already downloaded: {slug}  (use --force to re-download)[/yellow]")
        db.close()
        return

    course_dir = config.STORAGE_DIR / slug
    course_dir.mkdir(parents=True, exist_ok=True)
    pages_dir = course_dir / "pages"
    pages_dir.mkdir(exist_ok=True)

    course.status = "downloading"
    course.download_started_at = datetime.utcnow()
    course.local_path = str(course_dir)
    db.commit()

    console.print(f"\n[bold cyan]▶ {course.title or slug}[/bold cyan]")
    youtube_ids: list[str] = []
    all_assets: list[tuple[str, str]] = []

    async with httpx.AsyncClient(headers=HEADERS) as client:
        # -- Main course page ------------------------------------------------
        try:
            resp = await client.get(course.url, follow_redirects=True,
                                    timeout=config.REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            main_html = resp.text
        except Exception as exc:
            course.status = "failed"
            course.download_error = str(exc)
            db.commit()
            db.close()
            console.print(f"  [red]Failed to fetch course page: {exc}[/red]")
            return

        (pages_dir / "index.html").write_text(main_html, encoding="utf-8", errors="replace")
        (course_dir / "meta.json").write_text(
            json.dumps({
                "title": course.title, "url": course.url, "slug": course.slug,
                "department": course.department, "course_number": course.course_number,
                "level": course.level, "term": course.term, "year": course.year,
                "description": course.description,
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        main_soup = BeautifulSoup(main_html, "lxml")
        youtube_ids.extend(_youtube_ids(main_html))
        all_assets.extend(_asset_links(main_soup, course.url))

        # -- Section pages ---------------------------------------------------
        section_urls = _section_links(main_soup, course.url)
        console.print(f"  {len(section_urls)} course sections found")

        sem = asyncio.Semaphore(config.MAX_CONCURRENT_REQUESTS)

        async def fetch_section(url: str) -> tuple[str, str]:
            async with sem:
                await asyncio.sleep(config.REQUEST_DELAY_SECONDS)
                try:
                    r = await client.get(url, follow_redirects=True,
                                         timeout=config.REQUEST_TIMEOUT_SECONDS)
                    return url, r.text
                except Exception as exc:
                    console.print(f"    [yellow]Section fetch failed {url}: {exc}[/yellow]")
                    return url, ""

        results = await asyncio.gather(*[fetch_section(u) for u in section_urls])

        for url, html in results:
            if not html:
                continue
            section_name = _sanitize(url.rstrip("/").split("/")[-1] or "index")
            (pages_dir / f"{section_name}.html").write_text(
                html, encoding="utf-8", errors="replace"
            )
            section_soup = BeautifulSoup(html, "lxml")
            all_assets.extend(_asset_links(section_soup, url))
            youtube_ids.extend(_youtube_ids(html))

        # -- Deduplicate assets ----------------------------------------------
        seen_urls: set[str] = set()
        unique_assets: list[tuple[str, str]] = []
        for asset_url, asset_type in all_assets:
            if asset_url not in seen_urls:
                seen_urls.add(asset_url)
                unique_assets.append((asset_url, asset_type))

        unique_yt = list(dict.fromkeys(youtube_ids))
        console.print(
            f"  {len(unique_assets)} downloadable assets, "
            f"{len(unique_yt)} YouTube videos"
        )

        # -- Register assets in DB -------------------------------------------
        for asset_url, asset_type in unique_assets:
            if not db.query(Asset).filter_by(course_id=course.id, url=asset_url).first():
                db.add(Asset(course_id=course.id, url=asset_url, asset_type=asset_type))
        db.commit()

        # -- Download assets -------------------------------------------------
        async def pull_asset(asset: Asset):
            async with sem:
                await asyncio.sleep(config.REQUEST_DELAY_SECONDS * 0.5)
                parsed = urlparse(asset.url)
                raw_name = Path(parsed.path).name or "file"
                filename = _sanitize(raw_name)
                dest = course_dir / "assets" / asset.asset_type / filename
                size = await _download_file(client, asset.url, dest)
                if size is not None:
                    asset.status = "completed"
                    asset.local_path = str(dest)
                    asset.filename = filename
                    asset.size_bytes = size
                else:
                    asset.status = "failed"

        pending_assets = (
            db.query(Asset)
            .filter_by(course_id=course.id, status="pending")
            .filter(Asset.asset_type != "video")
            .all()
        )

        batch = config.MAX_CONCURRENT_REQUESTS * 2
        for i in range(0, len(pending_assets), batch):
            await asyncio.gather(*[pull_asset(a) for a in pending_assets[i : i + batch]])
            db.commit()

        # -- Register YouTube videos -----------------------------------------
        for vid_id in unique_yt:
            yt_url = f"https://www.youtube.com/watch?v={vid_id}"
            if not db.query(Asset).filter_by(course_id=course.id, url=yt_url).first():
                db.add(Asset(
                    course_id=course.id, url=yt_url,
                    asset_type="video", status="pending",
                ))
        db.commit()

        # -- Download videos -------------------------------------------------
        if config.DOWNLOAD_VIDEOS and unique_yt:
            from downloader.video import download_youtube_videos
            videos_dir = course_dir / "videos"
            videos_dir.mkdir(exist_ok=True)
            await download_youtube_videos(course.id, unique_yt, videos_dir, db)

        # -- Finalise --------------------------------------------------------
        completed = db.query(Asset).filter_by(course_id=course.id, status="completed").all()
        course.status = "completed"
        course.download_completed_at = datetime.utcnow()
        course.page_count = len(section_urls) + 1
        course.asset_count = sum(1 for a in completed if a.asset_type != "video")
        course.video_count = sum(1 for a in completed if a.asset_type == "video")
        course.total_size_bytes = sum(a.size_bytes or 0 for a in completed)
        db.commit()

    size_gb = (course.total_size_bytes or 0) / 1_073_741_824
    console.print(
        f"  [green]✓ Done[/green] — "
        f"{course.page_count} pages, {course.asset_count} files, "
        f"{course.video_count} videos, {size_gb:.2f} GB"
    )
    db.close()
