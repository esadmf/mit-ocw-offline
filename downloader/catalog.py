"""
Fetch the MIT OCW course catalog via sitemap and extract per-course metadata.
"""
import asyncio
import json
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from rich.console import Console

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

COURSE_URL_RE = re.compile(r"https://ocw\.mit\.edu/courses/[a-z0-9][a-z0-9\-]+/?$")

# OCW sub-sitemap URLs embed the course slug in their path, e.g.:
#   https://ocw.mit.edu/courses/6-0001-intro-to-python/sitemap.xml
# Extracting the slug from the URL avoids fetching every sub-sitemap.
SLUG_IN_SITEMAP_URL_RE = re.compile(r"ocw\.mit\.edu/courses/([a-z0-9][a-z0-9-]+)/")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _get(client: httpx.AsyncClient, url: str, retries: int = config.REQUEST_RETRIES) -> Optional[str]:
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
    """Return (sub_sitemap_locs, course_urls) from a sitemap or sitemap index."""
    soup = BeautifulSoup(xml, "xml")
    sitemaps = soup.find_all("sitemap")
    if sitemaps:
        locs = [s.find("loc").text.strip() for s in sitemaps if s.find("loc")]
        return locs, []
    courses = [
        loc.text.strip().rstrip("/")
        for loc in soup.find_all("loc")
        if COURSE_URL_RE.match(loc.text.strip())
    ]
    return [], courses


async def _collect_course_urls(client: httpx.AsyncClient) -> list[str]:
    console.print("[bold]Fetching OCW sitemap…[/bold]")
    xml = await _get(client, config.OCW_SITEMAP_URL)
    if not xml:
        console.print("[red]Could not fetch sitemap.[/red]")
        return []

    sub_sitemaps, courses = _parse_sitemap(xml)

    if sub_sitemaps:
        # Strategy 1: extract slug directly from each sub-sitemap URL.
        # This costs zero extra HTTP requests.
        for sm_url in sub_sitemaps:
            m = SLUG_IN_SITEMAP_URL_RE.search(sm_url)
            if m:
                courses.append(f"{config.OCW_BASE_URL}/courses/{m.group(1)}")

        if courses:
            console.print(f"  Extracted {len(courses)} course slugs from sitemap index directly.")
        else:
            # Strategy 2: sub-sitemap URLs don't embed the slug — fetch them
            # concurrently (no per-request delay; sitemaps are lightweight XML).
            console.print(f"  Fetching {len(sub_sitemaps)} sub-sitemaps concurrently…")
            sem = asyncio.Semaphore(config.MAX_CONCURRENT_REQUESTS * 3)

            async def fetch_sub(sm_url: str) -> list[str]:
                async with sem:
                    sub_xml = await _get(client, sm_url)
                    return _parse_sitemap(sub_xml)[1] if sub_xml else []

            results = await asyncio.gather(*[fetch_sub(u) for u in sub_sitemaps])
            for found in results:
                courses.extend(found)

    courses = list(dict.fromkeys(courses))

    if not courses:
        console.print("[yellow]No courses found — trying search page fallback…[/yellow]")
        courses = await _fallback_search_crawl(client)

    return courses


async def _fallback_search_crawl(client: httpx.AsyncClient) -> list[str]:
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
# Slug-based metadata (free — no HTTP request needed)
# ---------------------------------------------------------------------------

def _meta_from_slug(slug: str) -> dict:
    """Extract whatever we can from the slug alone: course number, term, year."""
    meta: dict = {}
    parts = slug.split("-")
    if len(parts) >= 2 and re.match(r"^\d", parts[0]) and re.match(r"^\d", parts[1]):
        meta["course_number"] = f"{parts[0]}.{parts[1]}"
    m = re.search(r"\b(19|20)\d{2}\b", slug)
    if m:
        meta["year"] = int(m.group())
    for term in ("fall", "spring", "summer", "january"):
        if f"-{term}-" in slug or slug.endswith(f"-{term}"):
            meta["term"] = term.capitalize()
            break
    return meta


# ---------------------------------------------------------------------------
# Full metadata extraction (requires fetching the course page)
# ---------------------------------------------------------------------------

def _extract_metadata(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    slug = url.rstrip("/").split("/")[-1]
    meta: dict = {"url": url, "slug": slug}
    meta.update(_meta_from_slug(slug))

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

    og = lambda prop: (soup.find("meta", property=prop) or {}).get("content", "") or ""
    if not meta.get("title"):
        meta["title"] = og("og:title").replace(" | MIT OpenCourseWare", "").strip() or None
    if not meta.get("description"):
        meta["description"] = og("og:description") or None
    meta["image_url"] = og("og:image") or None

    if not meta.get("title"):
        t = soup.find("title")
        if t:
            meta["title"] = t.text.replace(" | MIT OpenCourseWare", "").strip()

    body_text = soup.get_text(" ", strip=True)
    for level in ("Undergraduate", "Graduate"):
        if level.lower() in body_text.lower():
            meta["level"] = level
            break

    return meta


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def fetch_catalog(limit: Optional[int] = None, skip_metadata: bool = False):
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

        # Insert in batches of 100 and commit each batch so courses appear in
        # the UI progressively rather than all at once at the end.
        new_count = 0
        batch_size = 100
        for i in range(0, len(course_urls), batch_size):
            for url in course_urls[i : i + batch_size]:
                slug = url.rstrip("/").split("/")[-1]
                if not db.query(Course).filter_by(slug=slug).first():
                    course = Course(slug=slug, url=url, **_meta_from_slug(slug))
                    db.add(course)
                    new_count += 1
            db.commit()

        console.print(f"  {new_count} new courses added to catalog.")

        if skip_metadata:
            console.print("[green]Done (metadata skipped).[/green]")
            db.close()
            return

        # Enrich with full metadata (titles, descriptions, departments).
        # Commit every 20 courses so progress is visible in the UI.
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

        enrich_batch = 20
        for i in range(0, len(to_enrich), enrich_batch):
            await asyncio.gather(*[enrich(c) for c in to_enrich[i : i + enrich_batch]])
            db.commit()
            console.print(f"  Metadata: {min(i + enrich_batch, len(to_enrich))}/{len(to_enrich)}")

    console.print("[bold green]Catalog fetch complete.[/bold green]")
    db.close()
