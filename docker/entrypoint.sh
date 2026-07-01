#!/bin/sh
# Seed a starter config on first run so the container has something to read; the
# user then edits the mounted /config/theke.json (and sets THEKE_TMDB_API_KEY).
# Afterwards exec the CMD so the process gets PID 1 (clean SIGTERM on stop).
set -e

CONFIG=/config/theke.json
if [ ! -f "$CONFIG" ]; then
    echo "theke: no $CONFIG found -- writing a starter config; edit it and restart" >&2
    mkdir -p /config
    cp /app/theke.example.json "$CONFIG"
fi

exec "$@"
