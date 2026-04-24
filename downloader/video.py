"""
yt-dlp wrapper for downloading YouTube videos attached to OCW courses.
"""
import asyncio
from pathlib import Path
from typing import Optional

import yt_dlp
from rich.console import Console
from sqlalchemy.orm import Session

import config
from db.models import Asset

console = Console()


def _yt_opts(output_dir: Path, video_id: str) -> dict:
    return {
        "format": config.VIDEO_FORMAT,
        "outtmpl": str(output_dir / f"{video_id}.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        # Write subtitle files if available
        "writesubtitles": True,
        "subtitleslangs": ["en"],
        "skip_unavailable_fragments": True,
    }


def _download_one(url: str, video_id: str, output_dir: Path) -> Optional[Path]:
    """Synchronous yt-dlp download; returns destination path or None."""
    # Check for any existing file with this video_id
    for ext in ("mp4", "webm", "mkv", "m4v"):
        existing = output_dir / f"{video_id}.{ext}"
        if existing.exists() and config.SKIP_EXISTING and existing.stat().st_size > 0:
            return existing

    try:
        with yt_dlp.YoutubeDL(_yt_opts(output_dir, video_id)) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                ext = info.get("ext", "mp4")
                path = output_dir / f"{video_id}.{ext}"
                return path if path.exists() else None
    except yt_dlp.utils.DownloadError as exc:
        console.print(f"    [yellow]Video unavailable ({video_id}): {exc}[/yellow]")
    except Exception as exc:
        console.print(f"    [red]Video error ({video_id}): {exc}[/red]")
    return None


async def download_youtube_videos(
    course_id: int,
    video_ids: list[str],
    output_dir: Path,
    db: Session,
):
    """Download a list of YouTube videos for a course, updating the DB."""
    output_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"  Downloading {len(video_ids)} videos…")

    for idx, video_id in enumerate(video_ids, 1):
        url = f"https://www.youtube.com/watch?v={video_id}"
        console.print(f"    [{idx}/{len(video_ids)}] {video_id}")

        # Run blocking yt-dlp in a thread so the event loop stays responsive
        dest = await asyncio.to_thread(_download_one, url, video_id, output_dir)

        asset = db.query(Asset).filter_by(course_id=course_id, url=url).first()
        if asset:
            if dest and dest.exists():
                asset.status = "completed"
                asset.local_path = str(dest)
                asset.filename = dest.name
                asset.size_bytes = dest.stat().st_size
            else:
                asset.status = "failed"
            db.commit()

        # Small delay to be kind to YouTube's rate limiter
        await asyncio.sleep(2)
