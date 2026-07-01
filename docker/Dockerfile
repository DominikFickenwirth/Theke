# Theke CLI image -- Python + FFmpeg. Runs the scheduler (`theke run`) by
# default; every path/secret comes from the mounted /config/theke.json and
# THEKE_* env vars. Built on the NAS from the repo root (see docker-compose.yml).
FROM python:3.11-slim-bookworm

# FFmpeg (remux / probe -- the only runtime dep besides Python) and tzdata (the
# run_schedule "HH:MM" / "Weekday HH:MM" triggers fire in local time via TZ).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

# UTF-8 stdout: film-list metadata may exceed CP-1252 (see CLAUDE.md); unbuffered
# so scheduler progress reaches the container log promptly.
ENV PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY theke/ ./theke/
RUN pip install --no-cache-dir .

COPY docker/entrypoint.sh      /usr/local/bin/entrypoint.sh
COPY docker/theke.example.json /app/theke.example.json
RUN chmod +x /usr/local/bin/entrypoint.sh

# /config: theke.json  |  /data: theke.db + temp  |  /movies: the library
VOLUME ["/config", "/data", "/movies"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["theke", "--config", "/config/theke.json", "run"]
