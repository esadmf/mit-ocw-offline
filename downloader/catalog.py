"""
Fetch the MIT OCW course catalog via sitemap and extract per-course metadata.
"""
import asyncio
import json
import re
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from rich.console import Console
from rich.progress import (
    BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn,
)

import config
from db import SessionLocal
from db.models import Course

console = Console()

HEADERS = {
    "User-Agent": (
        "MIT-OCW-Offline-Archiver/1.0 "
        "(personal offline archival; https://ocw.mit.edu/terms)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

COURSE_URL_RE = re.compile(
    r"https://ocw\.mit\.edu/courses/[a-z0-9][a-z0-9\-]+/?$"
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _get(
    client: httpx.AsyncClient,
    url: str,
    retries: int = config.REQUEST_RETRIES,
) -> Optional[str]:
    for attempt in range(retries):
        try:
            r = await client.get(url, follow_redirects=True, timeout=config.REQUEST_TIMEOUT_SECONDS)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            if attempt == retries - 1:
                console.print(f"[red]Failed ({url}): {exc}[/red]")
                return None
            await asyncio.sleep(2 ** attempt)
    return None


# ---------------------------------------------------------------------------
# Sitemap parsing
# ---------------------------------------------------------------------------

def _parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """Return (sub_sitemaps, course_urls) found in an XML sitemap."""
    soup = BeautifulSoup(xml, "xml")

    # Sitemap index?
    sub = [loc.text.strip() for loc in soup.find_all("sitemap")]
    if sub:
        locs = [s.find("loc").text.strip() for s in soup.find_all("sitemap") if s.find("loc")]
        return locs, []

    courses = []
    for loc in soup.find_all("loc"):
        url = loc.text.strip()
        if COURSE_URL_RE.match(url):
            courses.append(url.rstrip("/"))
    return [], courses


async def _collect_course_urls(client: httpx.AsyncClient) -> list[str]:
    console.print("[bold]Fetching OCW sitemap…[/bold]")
    xml = await _get(client, config.OCW_SITEMAP_URL)
    if not xml:
        console.print("[red]Could not fetch sitemap.[/red]")
        return []

    sub_sitemaps, courses = _parse_sitemap(xml)

    if sub_sitemaps:
        console.print(f"  Sitemap index with {len(sub_sitemaps)} sub-sitemaps — fetching each…")
        for sm_url in sub_sitemaps:
            await asyncio.sleep(config.REQUEST_DELAY_SECONDS)
            sub_xml = await _get(client, sm_url)
            if sub_xml:
                _, found = _parse_sitemap(sub_xml)
                courses.extend(found)

    # Deduplicate
    courses = list(dict.fromkeys(courses))

    if not courses:
        console.print("[yellow]No courses found in sitemap — trying search page fallback…[/yellow]")
        courses = await _fallback_search_crawl(client)

    return courses


async def _fallback_search_crawl(client: httpx.AsyncClient) -> list[str]:
    """If sitemap yields nothing, scrape the OCW search/browse page."""
    url = f"{config.OCW_BASE_URL}/search/?s=department_course_numbers.sort_coursenum"
    html = await _get(client, url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    found = []
    for a in soup.find_all("a", href=re.compile(r"^/courses/[a-z0-9]")):
        full = config.OCW_BASE_URL + a["href"].rstrip("/")
        if COURSE_URL_RE.match(full) and full not in found:
            found.append(full)
    return found


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

def _extract_metadata(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    slug = url.rstrip("/").split("/")[-1]
    meta: dict = {"url": url, "slug": slug}

    # 1. JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = next((d for d in data if d.get("@type") in ("Course", "LearningResource")), {})
            if data.get("@type") in ("Course", "LearningResource"):
                meta.setdefault("title", data.get("name"))
                meta.setdefault("description", data.get("description"))
                if not meta.get("department") and isinstance(data.get("provider"), dict):
                    meta["department"] = data["provider"].get("name")
                break
        except (json.JSONDecodeError, AttributeError):
            continue

    # 2. Open Graph
    og = lambda prop: (soup.find("meta", property=prop) or {}).get("content", "") or ""
    if not meta.get("title"):
        meta["title"] = og("og:title").replace(" | MIT OpenCourseWare", "").strip() or None
    if not meta.get("description"):
        meta["description"] = og("og:description") or None
    meta["image_url"] = og("og:image") or None

    # 3. <title> tag
    if not meta.get("title"):
        t = soup.find("title")
        if t:
            meta["title"] = t.text.replace(" | MIT OpenCourseWare", "").strip()

    # 4. Derive course number and term/year from slug
    # Slug pattern: "6-0001-intro-to-cs-fall-2016"
    parts = slug.split("-")
    if len(parts) >= 2 and re.match(r"^\d", parts[0]) and re.match(r"^\d", parts[1]):
        meta.setdefault("course_number", f"{parts[0]}.{parts[1]}")

    m = re.search(r"\b(19|20)\d{2}\b", slug)
    if m:
        meta["year"] = int(m.group())

    for term in ("fall", "spring", "summer", "january"):
        if f"-{term}-" in slug or slug.endswith(f"-{term}"):
            meta["term"] = term.capitalize()
            break

    # 5. Level
    body_text = soup.get_text(" ", strip=True)
    for level in ("Undergraduate", "Graduate"):
        if level.lower() in body_text.lower():
            meta["level"] = level
            break

    return meta


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def fetch_catalog(
    limit: Optional[int] = None,
    skip_metadata: bool = False,
):
    """Populate the DB with all OCW courses (URLs first, then metadata)."""
    db = SessionLocal()

    async with httpx.AsyncClient(headers=HEADERS) as client:
        course_urls = await _collect_course_urls(client)

        if not course_urls:
            console.print("[red]No course URLs found. Aborting.[/red]")
            db.close()
            return

        if limit:
            course_urls = course_urls[:limit]

        console.print(f"[green]Found {len(course_urls)} courses.[/green]")

        # Insert any new courses
        new_count = 0
        for url in course_urls:
            slug = url.rstrip("/").split("/")[-1]
            if not db.query(Course).filter_by(slug=slug).first():
                db.add(Course(slug=slug, url=url))
                new_count += 1
        db.commit()
        console.print(f"  {new_count} new courses added to catalog.")

        if skip_metadata:
            console.print("[green]Done (metadata skipped).[/green]")
            db.close()
            return

        # Fetch metadata for courses that don't have a title yet
        to_enrich = db.query(Course).filter(Course.title.is_(None)).all()
        if not to_enrich:
            console.print("[green]All courses already have metadata.[/green]")
            db.close()
            return

        console.print(f"Fetching metadata for {len(to_enrich)} courses…")
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_REQUESTS)

        async def enrich(course: Course):
            async with semaphore:
                await asyncio.sleep(config.REQUEST_DELAY_SECONDS)
                html = await _get(client, course.url)
                if not html:
                    return
                extracted = _extract_metadata(course.url, html)
                for key, val in extracted.items():
                    if val is not None and hasattr(course, key):
                        setattr(course, key, val)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Enriching metadata…", total=len(to_enrich))
            batch_size = 20
            for i in range(0, len(to_enrich), batch_size):
                batch = to_enrich[i : i + batch_size]
                await asyncio.gather(*[enrich(c) for c in batch])
                db.commit()
                progress.advance(task, len(batch))

    console.print("[bold green]Catalog fetch complete.[/bold green]")
    db.close()
