FROM python:3.12-slim

# ffmpeg is required by yt-dlp to merge separate video + audio streams
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# All persistent data (database + downloaded courses) lives here.
# Mount a volume or bind-mount at /data to keep it across container rebuilds.
ENV OCW_DATA_DIR=/data
ENV OCW_HOST=0.0.0.0

VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["python", "cli.py"]
CMD ["serve", "--host", "0.0.0.0"]
