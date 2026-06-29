# -- core (config / db / net -- the leaf infrastructure layer) ----------------
# The package's bottom layer: effective config, the SQLite layer, and the single
# HTTP primitive. They live together because they ARE one layer in the dependency
# graph -- dependency-free (stdlib only, no theke imports) and shared across the
# feature modules. Keeping them here lets match/files/fetch reach infrastructure
# without importing the package back into themselves (the cycle this layer was
# extracted to break). Re-exported from theke for `from theke import Config,
# db_connect, ...`.
#
# Do NOT add `from __future__ import annotations`: load_config reads
# dataclasses.fields(Config).type and isinstance-checks against it, so the field
# annotations must stay real type objects, not strings.

import dataclasses
import json
import sqlite3
import sys
import urllib.request
from dataclasses import dataclass


# -- config -------------------------------------------------------------------

CONFIG_DEFAULT_PATH = "theke.json"


class ConfigError(Exception):
    """Invalid or unreadable configuration."""


@dataclass
class Config:
    """Effective configuration; defaults < config file < CLI parameters."""
    db_path:            str = "theke.db"
    filmliste_url:      str = "https://liste.mediathekview.de/Filmliste-akt.xz"
    filmliste_diff_url: str = "https://liste.mediathekview.de/Filmliste-diff.xz"
    filmliste_id_url:   str = "https://liste.mediathekview.de/filmliste.id"
    tmdb_api_key:         str   = ""
    tmdb_api_url:         str   = "https://api.themoviedb.org/3"
    tmdb_language:        str   = "de-DE"
    match_min_confidence: float = 0.6
    match_year_tolerance: int   = 2
    queue_auto_approve:   bool  = False
    languages:            list  = dataclasses.field(default_factory=lambda: ["de"])
    fiction_topics:       list  = dataclasses.field(default_factory=list)
    subtitle_formats:     list  = dataclasses.field(default_factory=lambda: ["srt", "ass", "ttml"])
    ffmpeg_path:          str   = "ffmpeg"
    download_retries:     int   = 3
    download_timeout:     int   = 60
    download_stall_timeout: int = 120
    temp_path:            str   = ""
    video_ext:            str   = "mp4"
    audio_ext:            str   = "aac"
    library_path:         str   = "movies/{Title} ({Year})/{Title} ({Year}).mp4"


def load_config(path: str | None, overrides: dict | None = None) -> Config:
    """Load the JSON config file and apply CLI overrides (None = not set).

    An explicitly given path must exist; the default path may be absent.
    Unknown keys are ignored with a warning on stderr (forward compatibility);
    wrong value types for known keys are errors (typo protection).
    """
    explicit = path is not None
    path = path or CONFIG_DEFAULT_PATH
    fields = {f.name: f.type for f in dataclasses.fields(Config)}
    data = {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        if explicit:
            raise ConfigError(f"config file not found: {path}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"config root in {path} must be a JSON object")

    known = {}
    for key, value in data.items():
        if key not in fields:
            print(f"warning: ignoring unknown config key in {path}: {key}",
                  file=sys.stderr)
            continue
        if not isinstance(value, fields[key]):
            raise ConfigError(
                f"config key {key} must be of type {fields[key].__name__}")
        known[key] = value
    for key, value in (overrides or {}).items():
        if value is not None:
            known[key] = value
    return Config(**known)


# -- db -----------------------------------------------------------------------
# Thin SQLite layer; keep all SQLite specifics here so the backend could be
# swapped later. Single-user design: one process at a time owns the DB.

class DbError(Exception):
    """Database problem other than locking (e.g. schema newer than code)."""


class DbLockedError(Exception):
    """Another process holds the database."""


# One tuple of SQL statements per schema version; each stage appends its own
# migration when it lands. Entry 1 (phase 2) is the film-list mirror schema;
# entry 2 (phase 3) adds the enrich columns (extracted metadata) -- it keeps the
# original column name classify_confidence so existing DBs upgrade cleanly; entry
# 4 renames that column to enrich_confidence (the classify -> enrich rename).
MIGRATIONS: list[tuple[str, ...]] = [
    (
        """CREATE TABLE mediathek (
            status           TEXT NOT NULL,
            mediathek_id     TEXT UNIQUE,
            sender           TEXT,
            topic            TEXT,
            title            TEXT,
            description      TEXT,
            date             DATE,
            duration         INTEGER,
            size_mb          INTEGER,
            url_video        TEXT,
            url_video_small  TEXT,
            url_video_hd     TEXT,
            url_subtitle     TEXT,
            url_website      TEXT,
            url_history      TEXT,
            geo              TEXT,
            language         TEXT DEFAULT '',
            tmdb_id          TEXT DEFAULT '',
            imdb_id          TEXT DEFAULT '',
            match_confidence REAL
        )""",
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)",
    ),
    (
        "ALTER TABLE mediathek ADD COLUMN clean_title         TEXT",
        "ALTER TABLE mediathek ADD COLUMN series_name         TEXT",
        "ALTER TABLE mediathek ADD COLUMN season              INTEGER",
        "ALTER TABLE mediathek ADD COLUMN episode             INTEGER",
        "ALTER TABLE mediathek ADD COLUMN episode_count       INTEGER",
        "ALTER TABLE mediathek ADD COLUMN category            TEXT",
        "ALTER TABLE mediathek ADD COLUMN year                INTEGER",
        "ALTER TABLE mediathek ADD COLUMN country             TEXT",
        "ALTER TABLE mediathek ADD COLUMN flags               TEXT",
        "ALTER TABLE mediathek ADD COLUMN classify_confidence REAL",
    ),
    (
        "ALTER TABLE mediathek ADD COLUMN genre TEXT",
        "ALTER TABLE mediathek ADD COLUMN slot  TEXT",
    ),
    (
        "ALTER TABLE mediathek RENAME COLUMN classify_confidence TO enrich_confidence",
    ),
    (  # phase 5: the download queue (review queue + download record in one).
       # No FK / no UNIQUE on mediathek_id: re-queue is allowed and a mediathek
       # row may be deleted under a queue entry; idempotency lives in _queue_add.
        """CREATE TABLE queue (
            id            INTEGER PRIMARY KEY,
            status        TEXT NOT NULL,
            mediathek_id  TEXT NOT NULL,
            tmdb_id       TEXT,
            name          TEXT NOT NULL,
            language      TEXT NOT NULL,
            resolution    TEXT NOT NULL,
            remux         TEXT NOT NULL DEFAULT 'AV',
            error         TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )""",
    ),
    (  # phase 6-8: everything queue download needs, resolved at add time so the
       # row is self-contained (no mediathek/config lookup at download): the
       # source media url, the subtitle url, and the full library destination.
        "ALTER TABLE queue ADD COLUMN url          TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE queue ADD COLUMN url_subtitle TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE queue ADD COLUMN path         TEXT NOT NULL DEFAULT ''",
    ),
    (  # drop the redundant filename stem: the same title/year info already lives
       # in `path` (the full library destination), so `name` carried nothing new.
        "ALTER TABLE queue DROP COLUMN name",
    ),
    (  # phase 9: the wishlist + library record in one, keyed by tmdb_id. status
       # 'W' wish / 'M' missing episode / 'L' in library; a finished download
       # records its tmdb_id here as 'L' (flipping a wish or inserting fresh).
        """CREATE TABLE library (
            tmdb_id    TEXT PRIMARY KEY,
            status     TEXT NOT NULL,
            title      TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
    ),
    (  # phase 9: capture the release year (from TMDB at wish time) and the
       # library folder (the directory a finished download landed in).
        "ALTER TABLE library ADD COLUMN year INTEGER",
        "ALTER TABLE library ADD COLUMN path TEXT",
    ),
]


def db_connect(db_path: str, migrations=None) -> sqlite3.Connection:
    """Open (or create) the DB, take the exclusive lock, run pending migrations.

    The exclusive lock is held until close; a second process fails immediately
    with DbLockedError instead of waiting.
    """
    if migrations is None:
        migrations = MIGRATIONS
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 0")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA locking_mode = EXCLUSIVE")
        conn.execute("BEGIN EXCLUSIVE")  # lock stays held after COMMIT
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(migrations):
            raise DbError(
                f"database schema (version {version}) is newer than this "
                f"Theke version (expects {len(migrations)})")
        try:
            for statements in migrations[version:]:
                for statement in statements:
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {len(migrations)}")
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
    except sqlite3.OperationalError as exc:
        conn.close()
        if "database is locked" in str(exc):
            raise DbLockedError(
                f"database is in use by another process: {db_path}") from None
        raise
    except BaseException:
        conn.close()
        raise
    return conn


def db_get_meta(conn, key):
    """Read a value from the meta table, or None if the key is absent."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def db_set_meta(conn, key, value):
    """Upsert a single key/value into the meta table."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


# -- net ----------------------------------------------------------------------
# The single place the package touches the network.
#
# PATCH POINT -- tests monkeypatch this as `theke.core.http_get`. Callers MUST
# reach it through the module object (`from theke import core; core.http_get(...)`
# or `import theke.core`), NEVER `from theke.core import http_get`: a direct name
# import binds the original function object, so a monkeypatch that rebinds the
# `theke.core.http_get` attribute would not be seen by the caller. Same for
# USER_AGENT.

USER_AGENT = "theke"


def http_get(url: str, timeout=None) -> bytes:
    """Fetch a URL and return the raw response bytes. `timeout` (seconds) bounds
    each blocking socket operation, so a dropped connection fails instead of
    hanging forever (None = no timeout)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()
