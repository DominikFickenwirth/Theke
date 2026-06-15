"""Theke -- self-hosted media manager CLI.

All logic lives in this package module (split into more files later if ever
needed). Sections: config / DB / CLI.
"""

import argparse
import dataclasses
import hashlib
import io
import json
import lzma
import sqlite3
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time, timezone

CONFIG_DEFAULT_PATH = "theke.json"


# -- config ------------------------------------------------------------------

class ConfigError(Exception):
    """Invalid or unreadable configuration."""


@dataclass
class Config:
    """Effective configuration; defaults < config file < CLI parameters."""
    db_path:            str = "theke.db"
    filmliste_url:      str = "https://liste.mediathekview.de/Filmliste-akt.xz"
    filmliste_diff_url: str = "https://liste.mediathekview.de/Filmliste-diff.xz"
    filmliste_id_url:   str = "https://liste.mediathekview.de/filmliste.id"


def load_config(path: str | None, overrides: dict | None = None) -> Config:
    """Load the JSON config file and apply CLI overrides (None = not set).

    An explicitly given path must exist; the default path may be absent.
    Unknown keys and wrong value types are errors (typo protection).
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

    for key, value in data.items():
        if key not in fields:
            raise ConfigError(f"unknown config key in {path}: {key}")
        if not isinstance(value, fields[key]):
            raise ConfigError(
                f"config key {key} must be of type {fields[key].__name__}")
    for key, value in (overrides or {}).items():
        if value is not None:
            data[key] = value
    return Config(**data)


# -- db -----------------------------------------------------------------------
# Thin SQLite layer; keep all SQLite specifics here so the backend could be
# swapped later. Single-user design: one process at a time owns the DB.

class DbError(Exception):
    """Database problem other than locking (e.g. schema newer than code)."""


class DbLockedError(Exception):
    """Another process holds the database."""


# One tuple of SQL statements per schema version; each stage appends its own
# migration when it lands. Entry 1 (phase 2) is the film-list mirror schema.
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


# -- mirror -------------------------------------------------------------------
# Download the MediathekView film list and import it into the mediathek table.
# The list is an XZ-compressed, flat JSON object with duplicate keys
# ("Filmliste" twice, then many "X"); each "X" is a 20-field array.

# Field order of an "X" array, exactly as in the MV format (DatenFilm). The
# rtmp_* slots are dead legacy fields and never become columns.
FIELDS = [
    "sender", "thema", "titel", "datum", "zeit", "dauer", "groesse_mb",
    "beschreibung", "url", "website", "url_untertitel", "url_rtmp",
    "url_klein", "url_rtmp_klein", "url_hd", "url_rtmp_hd", "datum_l",
    "url_history", "geo", "neu",
]


def film_id(sender, thema, url, website) -> str:
    """Film identity exactly like MediathekView's DatenFilm.getSha256():
    SHA-256 over sender + thema + url + website, each UTF-16LE-encoded."""
    digest = hashlib.sha256()
    for part in (sender, thema, url, website):
        digest.update(part.encode("utf-16-le"))
    return digest.hexdigest()


def decode_rel_url(base: str, encoded: str) -> str:
    """Decode MV's relative URL scheme "offset|suffix" = base[:offset] + suffix.
    Empty -> empty; no "|" (or non-numeric offset) -> taken verbatim."""
    if not encoded:
        return ""
    sep = encoded.find("|")
    if sep == -1:
        return encoded
    try:
        cut = int(encoded[:sep])
    except ValueError:
        return encoded
    return base[:cut] + encoded[sep + 1:]


def parse_duration(value: str):
    """"HH:MM:SS" -> seconds, or None if absent/unparseable."""
    try:
        hours, minutes, seconds = (int(p) for p in value.split(":"))
        return hours * 3600 + minutes * 60 + seconds
    except (ValueError, AttributeError):
        return None


def to_int(value: str):
    """Parse an integer field, or None if absent/unparseable."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def parse_date(datum: str, zeit: str, datum_l: str):
    """Broadcast time as ISO "YYYY-MM-DD HH:MM:SS".

    The German wall-clock strings are authoritative and timezone-free; the
    datum_l epoch is only a fallback (read as UTC) when no date string exists.
    Converting datum_l to the right wall clock would need the Europe/Berlin
    zone, unavailable in the stdlib on Windows.
    """
    if datum:
        try:
            stamp = datetime.strptime(
                f"{datum} {zeit or '00:00:00'}", "%d.%m.%Y %H:%M:%S")
            return stamp.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    if datum_l:
        try:
            return datetime.fromtimestamp(
                int(datum_l), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _build_film(values, last_sender, last_thema) -> dict:
    """Turn one "X" array into a DB-shaped row dict (column names already)."""
    raw = dict(zip(FIELDS, values))
    sender = raw.get("sender") or last_sender
    thema = raw.get("thema") or last_thema
    url = raw.get("url", "")
    return {
        "mediathek_id":    film_id(sender, thema, url, raw.get("website", "")),
        "status":          "0" if raw.get("neu") == "true" else "1",
        "sender":          sender,
        "topic":           thema,
        "title":           raw.get("titel", ""),
        "description":     raw.get("beschreibung", ""),
        "date":            parse_date(raw.get("datum", ""), raw.get("zeit", ""), raw.get("datum_l", "")),
        "duration":        parse_duration(raw.get("dauer", "")),
        "size_mb":         to_int(raw.get("groesse_mb", "")),
        "url_video":       url,
        "url_video_small": decode_rel_url(url, raw.get("url_klein", "")),
        "url_video_hd":    decode_rel_url(url, raw.get("url_hd", "")),
        "url_subtitle":    decode_rel_url(url, raw.get("url_untertitel", "")),
        "url_website":     raw.get("website", ""),
        "url_history":     decode_rel_url(url, raw.get("url_history", "")),
        "geo":             raw.get("geo", ""),
    }


def parse_filmliste(stream, chunk_size=1 << 16):
    """Stream the decompressed MV film list.

    Yields the metadata dict first, then one DB-shaped dict per film. We
    raw_decode one member value at a time over a refilled buffer so memory
    stays flat for the ~500k films. sender/thema inherit from the previous
    film when their field is empty.
    """
    decoder = json.JSONDecoder()
    buf = ""
    pos = 0

    def refill():
        nonlocal buf, pos
        chunk = stream.read(chunk_size)
        if not chunk:
            return False
        buf = buf[pos:] + chunk
        pos = 0
        return True

    def seek(stops):
        # advance past whitespace and any char in `stops`; refill as needed.
        nonlocal pos
        while True:
            while pos < len(buf) and (buf[pos].isspace() or buf[pos] in stops):
                pos += 1
            if pos < len(buf):
                return True
            if not refill():
                return False

    def decode():
        # raw_decode one JSON value at pos, refilling while it looks incomplete.
        nonlocal pos
        while True:
            try:
                value, pos = decoder.raw_decode(buf, pos)
                return value
            except json.JSONDecodeError:
                if not refill():
                    raise

    if not seek(""):
        return
    pos += 1  # consume the opening '{'

    metadata = None
    seen_filmliste = 0
    last_sender = ""
    last_thema = ""
    while seek(","):
        if buf[pos] == "}":
            break
        key = decode()
        seek(":")
        value = decode()
        if key == "Filmliste":
            seen_filmliste += 1
            if seen_filmliste == 1:  # first = metadata, second = column names
                metadata = {
                    "erstellt_am": value[1] if len(value) > 1 else "",
                    "id":          value[4] if len(value) > 4 else "",
                }
                yield metadata
        elif key == "X":
            if metadata is None:  # malformed list without a header
                metadata = {"erstellt_am": "", "id": ""}
                yield metadata
            film = _build_film(value, last_sender, last_thema)
            last_sender = film["sender"]
            last_thema = film["topic"]
            yield film


# Columns written on every import. language/tmdb_id/imdb_id/match_confidence are
# owned by phase 3 and never touched here, so an ID assignment survives every
# refresh.
MIRROR_COLS = [
    "mediathek_id", "status", "sender", "topic", "title", "description",
    "date", "duration", "size_mb", "url_video", "url_video_small",
    "url_video_hd", "url_subtitle", "url_website", "url_history", "geo",
]

_UPSERT_SQL = (
    "INSERT INTO mediathek ({cols}) VALUES ({vals}) "
    "ON CONFLICT(mediathek_id) DO UPDATE SET {sets}"
).format(
    cols=", ".join(MIRROR_COLS),
    vals=", ".join(":" + c for c in MIRROR_COLS),
    sets=", ".join(f"{c}=excluded.{c}" for c in MIRROR_COLS if c != "mediathek_id"),
)


# Progress is reported through a callback so the logic stays decoupled from I/O
# (tests pass a collector). The CLI wires it to stderr; stdout stays the result.
def _noop(_msg):
    pass


def _upsert_films(conn, films, batch=5000, progress=_noop) -> int:
    """Upsert film rows in batches; return the number of rows written. Reports a
    running count to `progress` every 50k rows so a big import shows progress."""
    count = 0
    reported = 0
    rows = []

    def flush():
        nonlocal count, reported
        if not rows:
            return
        conn.executemany(_UPSERT_SQL, rows)
        count += len(rows)
        rows.clear()
        if count - reported >= 50000:
            reported = count
            progress(f"imported {count} films")

    for film in films:
        rows.append(film)
        if len(rows) >= batch:
            flush()
    flush()
    return count


def _store_meta(conn, meta):
    db_set_meta(conn, "filmliste_id",      meta.get("id", ""))
    db_set_meta(conn, "filmliste_created", meta.get("erstellt_am", ""))


def import_films(conn, films, meta, progress=_noop) -> dict:
    """Import a list (full or diff alike): upsert the new/changed films and store
    the list metadata in one transaction, so an abort rolls back cleanly and a
    re-run is idempotent. Entries no longer in the source are kept -- the mirror
    only grows or updates, never deletes."""
    progress("importing into the database")
    conn.execute("BEGIN")
    try:
        imported = _upsert_films(conn, films, progress=progress)
        _store_meta(conn, meta)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"imported": imported}


# Network is touched in exactly one place; tests monkeypatch http_get.
USER_AGENT = "theke"


def http_get(url: str) -> bytes:
    """Fetch a URL and return the raw response bytes."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request) as response:
        return response.read()


def can_use_diff(lastcreated, now=None) -> bool:
    """A diff is usable only if the local list was created after today 07:00
    UTC (MediathekView's FilmListMetaData.canUseDiffList)."""
    try:
        stamp = datetime.strptime(
            lastcreated, "%d.%m.%Y, %H:%M").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    now = now or datetime.now(timezone.utc)
    cutoff = datetime.combine(now.date(), time(7, 0), tzinfo=timezone.utc)
    return stamp > cutoff


def fetch_list_id(cfg):
    """Server's filmliste.id (hash of the current server list), or None if the
    small request fails (then we re-download to be safe)."""
    try:
        return http_get(cfg.filmliste_id_url).decode("utf-8").strip()
    except Exception:
        return None


def _load_list(url, progress=_noop):
    """Download, stream-decompress and parse a list; return (meta, films)."""
    progress(f"downloading {url.rsplit('/', 1)[-1]}")
    raw = http_get(url)
    progress(f"download done ({len(raw) / (1 << 20):.1f} MB), unpacking")
    stream = lzma.open(io.BytesIO(raw), "rt", encoding="utf-8")
    films = parse_filmliste(stream)
    return next(films), films  # metadata is the first yield


def _do_full(conn, cfg, progress=_noop):
    meta, films = _load_list(cfg.filmliste_url, progress)
    return {"action": "full", **import_films(conn, films, meta, progress)}


def _do_diff(conn, cfg, progress=_noop):
    try:
        meta, films = _load_list(cfg.filmliste_diff_url, progress)
    except Exception:
        return None  # download/parse failed -> caller falls back to full
    return {"action": "diff", **import_films(conn, films, meta, progress)}


def cmd_mirror(conn, cfg, args: argparse.Namespace, progress=_noop) -> dict:
    """Refresh the film-list mirror (MediathekView update logic)"""
    if args.force or db_get_meta(conn, "filmliste_id") is None:
        return _do_full(conn, cfg, progress)
    progress("checking the server list id")
    if fetch_list_id(cfg) == db_get_meta(conn, "filmliste_id"):
        return {"action": "skip"}
    if can_use_diff(db_get_meta(conn, "filmliste_created")):
        result = _do_diff(conn, cfg, progress)
        if result and result["imported"]:
            return result
        return _do_full(conn, cfg, progress)  # diff failed or was empty
    return _do_full(conn, cfg, progress)


# -- cli ----------------------------------------------------------------------
# Stable grammar and exit codes: the GUI drives the CLI and parses the --json
# output (exactly one JSON object on stdout per call).

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3


def cmd_config(cfg) -> dict:
    """Show the effective configuration (after precedence resolution)."""
    return dataclasses.asdict(cfg)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="theke",       description="Self-hosted media manager for German public broadcasters")
    parser.add_argument("--config", metavar="PATH",      help=f"config file (default: {CONFIG_DEFAULT_PATH})")
    parser.add_argument("--db",     metavar="PATH",      help="database file (overrides db_path from config)")
    parser.add_argument("--json",   action="store_true", help="machine-readable output: one JSON object on stdout")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    sub.add_parser("config", help="show the effective configuration")

    mirror = sub.add_parser("mirror", help="refresh the film-list mirror (~30 s)",
                            description="Refresh the film-list mirror; a full "
                                        "download and import takes about 30 "
                                        "seconds. Progress is printed to stderr.")
    mirror.add_argument("--force", action="store_true", help="always download the full list")

    return parser


def _stderr_progress(msg):
    """Plain-text progress sink: one line per step on stderr, flushed live, so it
    never pollutes the stdout result (the single JSON object in --json mode)."""
    print(f"-> {msg}", file=sys.stderr, flush=True)


def main(argv=None) -> int:
    """CLI entry point; returns the process exit code."""

    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse handles usage errors and --help
        return EXIT_USAGE if exc.code else EXIT_OK

    try:
        cfg = load_config(args.config, overrides={"db_path": args.db})
        match args.command:
            case "config":
                result = cmd_config(cfg)
            case "mirror":
                conn = db_connect(cfg.db_path)
                try:     result = cmd_mirror(conn, cfg, args, _stderr_progress)
                finally: conn.close()
            case _: raise DbError(f"unhandled command: {args.command}")

    except Exception as exc:  # any failure becomes one clean error, never a traceback
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCKED if isinstance(exc, DbLockedError) else EXIT_ERROR

    if args.json:
        print(json.dumps(result))
    else:
        for key, value in result.items():
            print(f"{key} = {value}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
