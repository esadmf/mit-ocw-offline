import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# When running in Docker set OCW_DATA_DIR=/data (the mounted volume).
# Locally it defaults to the project directory.
_data_env = os.environ.get("OCW_DATA_DIR")
DATA_DIR = Path(_data_env) if _data_env else BASE_DIR

STORAGE_DIR = DATA_DIR / "storage"

# PostgreSQL connection
_pg_host = os.environ.get("POSTGRES_HOST", "localhost")
_pg_port = os.environ.get("POSTGRES_PORT", "5432")
_pg_db   = os.environ.get("POSTGRES_DB", "ocw")
_pg_user = os.environ.get("POSTGRES_USER", "ocw")
_pg_pass = os.environ.get("POSTGRES_PASSWORD", "")
DATABASE_URL = f"postgresql://{_pg_user}:{_pg_pass}@{_pg_host}:{_pg_port}/{_pg_db}"

OCW_BASE_URL = "https://ocw.mit.edu"
OCW_SITEMAP_URL = "https://ocw.mit.edu/sitemap.xml"

# HTTP behaviour
REQUEST_DELAY_SECONDS = float(os.environ.get("OCW_REQUEST_DELAY", "1.0"))
MAX_CONCURRENT_REQUESTS = int(os.environ.get("OCW_CONCURRENT", "3"))
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 3

# Videos
DOWNLOAD_VIDEOS = os.environ.get("OCW_DOWNLOAD_VIDEOS", "true").lower() != "false"
VIDEO_FORMAT = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"

# Downloads
SKIP_EXISTING = True
MAX_FILE_SIZE_MB = int(os.environ.get("OCW_MAX_FILE_MB", "5000"))

# Server
SERVER_HOST = os.environ.get("OCW_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("OCW_PORT", "8080"))
