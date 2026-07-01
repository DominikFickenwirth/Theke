# Theke CLI image -- Python + FFmpeg. Runs the scheduler (`theke run`) by
# default; every path/secret comes from the mounted /config/theke.json and
# THEKE_* env vars. Built on the NAS from the repo root (see docker-compose.yml).
FROM python:3.11-slim-bookworm

# - ffmpeg for remux, including ffprobe for codec info extraction
# - tzdata for scheduling in local time via TZ
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

# - UTF-8 stdout: film-list metadata may exceed CP-1252
# - unbuffered: scheduler progress reaches the container log promptly
ENV PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY theke/ ./theke/
RUN pip install --no-cache-dir .

# theke seeds /config/theke.json with defaults on first run (missing --config
# path); paths point at the mounted volumes via THEKE_* env (see compose). The
# process is PID 1, so `docker stop` reaches it as SIGTERM for a clean pass stop.
CMD ["theke", "--config", "/config/theke.json", "--db", "/config/theke.db", "run"]
