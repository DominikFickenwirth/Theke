"""Theke -- self-hosted media manager CLI.

All logic lives in this package module (split into more files later if ever
needed). Sections: config / DB / CLI.
"""

import argparse
import dataclasses
import hashlib
import io
import json
import logging
import lzma
import sqlite3
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time, timezone

from theke.classify import classify, CLASSIFY_COLS

CONFIG_DEFAULT_PATH = "theke.json"

# Progress and diagnostics go to this logger; main() routes it to stderr so the
# stdout result (the single JSON object) stays clean. Tests capture it via
# pytest's caplog, so no progress argument needs to be threaded through the code.
log = logging.getLogger("theke")


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
# migration when it lands. Entry 1 (phase 2) is the film-list mirror schema;
# entry 2 (phase 3) adds the classify columns (extracted metadata).
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
        "status":          "0",
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


def _upsert_films(conn, films, batch=5000) -> int:
    """Upsert film rows in batches; return the number of rows written. Logs a
    running count every 50k rows so a big import shows progress."""
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
            log.info("imported %d films", count)

    for film in films:
        rows.append(film)
        if len(rows) >= batch:
            flush()
    flush()
    return count


def _store_meta(conn, meta):
    db_set_meta(conn, "filmliste_id",      meta.get("id", ""))
    db_set_meta(conn, "filmliste_created", meta.get("erstellt_am", ""))


def import_films(conn, films, meta) -> dict:
    """Import a list (full or diff alike): upsert the new/changed films and store
    the list metadata in one transaction, so an abort rolls back cleanly and a
    re-run is idempotent. Entries no longer in the source are kept -- the mirror
    only grows or updates, never deletes."""
    log.info("importing into the database")
    conn.execute("BEGIN")
    try:
        imported = _upsert_films(conn, films)
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


def _load_list(url):
    """Download, stream-decompress and parse a list; return (meta, films)."""
    log.info("downloading %s", url.rsplit("/", 1)[-1])
    raw = http_get(url)
    log.info("download done (%.1f MB), unpacking", len(raw) / (1 << 20))
    stream = lzma.open(io.BytesIO(raw), "rt", encoding="utf-8")
    films = parse_filmliste(stream)
    return next(films), films  # metadata is the first yield


def _do_full(conn, cfg):
    meta, films = _load_list(cfg.filmliste_url)
    return {"action": "full", **import_films(conn, films, meta)}


def _do_diff(conn, cfg):
    try:
        meta, films = _load_list(cfg.filmliste_diff_url)
    except Exception:
        return None  # download/parse failed -> caller falls back to full
    return {"action": "diff", **import_films(conn, films, meta)}


def cmd_mirror(conn, cfg, args: argparse.Namespace) -> dict:
    """Refresh the film-list mirror (MediathekView update logic)"""
    if args.force or db_get_meta(conn, "filmliste_id") is None:
        return _do_full(conn, cfg)
    log.info("checking the server list id")
    if fetch_list_id(cfg) == db_get_meta(conn, "filmliste_id"):
        return {"action": "skip"}
    if can_use_diff(db_get_meta(conn, "filmliste_created")):
        result = _do_diff(conn, cfg)
        if result and result["imported"]:
            return result
        return _do_full(conn, cfg)  # diff failed or was empty
    return _do_full(conn, cfg)


# -- classify -----------------------------------------------------------------
# Extract structured metadata from the free-text fields and flip status 0 -> 1.

_CLASSIFY_READ = (
    "SELECT mediathek_id, sender, topic, title, description, duration "
    "FROM mediathek WHERE status='0'"
)

_UPDATE_SQL = (
    "UPDATE mediathek SET {sets}, status='1' WHERE mediathek_id=:mediathek_id"
).format(sets=", ".join(f"{c}=:{c}" for c in CLASSIFY_COLS))


def cmd_classify(conn, cfg, args: argparse.Namespace) -> dict:
    """Dispatch a classify action: `run` writes the classify columns; the others
    (`report`/`audit`/`show`/`dist`) are read-only inspection tools."""
    match args.classify_cmd:
        case "run":    return _classify_run(conn, args)
        case "report": return _classify_report_cmd(conn, args)
        case _: raise DbError(f"unhandled classify action: {args.classify_cmd}")


def _classify_run(conn, args) -> dict:
    """Classify mediathek rows into the classify columns and flip status 0 -> 1.
    By default only unclassified rows (status '0'); --force reprocesses all."""
    sql = _CLASSIFY_READ if not args.force else _CLASSIFY_READ.replace(
        " WHERE status='0'", "")
    log.info("classifying rows")
    conn.execute("BEGIN")
    try:
        count = _classify_rows(conn, conn.execute(sql))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"classified": count}


def _classify_rows(conn, rows, batch=5000) -> int:
    """Stream rows through classify(), write updates in batches; log every 50k."""
    count = 0
    reported = 0
    params = []

    def flush():
        nonlocal count, reported
        if not params:
            return
        conn.executemany(_UPDATE_SQL, params)
        count += len(params)
        params.clear()
        if count - reported >= 50000:
            reported = count
            log.info("classified %d rows", count)

    for row in rows:
        meta = classify(row["sender"], row["topic"], row["title"],
                        row["description"], row["duration"])
        meta["mediathek_id"] = row["mediathek_id"]
        params.append(meta)
        if len(params) >= batch:
            flush()
    flush()
    return count


# -- classify coverage report (read-only) -------------------------------------
# Per-sender coverage of the classify fields, for iterating the algorithm. Two
# sources, one tally: --analyze reads the stored columns, --dry-run runs
# classify() live (writing nothing). Both expose the same keys per row.

REPORT_MIN_ROWS = 1000   # senders below this are omitted (long tail of one-offs)

_REPORT_FIELDS = ["year", "country", "se", "cat", "unklar",
                  "flag_a", "flag_s", "flag_u", "flag_t"]

# Per-confidence-level buckets for --by-confidence: deterministic levels emitted
# by classify._confidence (0.9/0.8/0.5/0.2). Counted always, summarized only when
# requested, so the default report shape stays stable.
_CONF_LEVELS = [("c90", 0.9), ("c80", 0.8), ("c50", 0.5), ("c20", 0.2)]


def _split_senders(value):
    """Comma-separated --sender value -> list of senders, or None when unset."""
    if not value:
        return None
    return [s.strip() for s in value.split(",") if s.strip()]


def _sender_clause(senders):
    """WHERE fragment + params restricting to the given senders ('' when None)."""
    if not senders:
        return "", []
    return f"WHERE sender IN ({','.join('?' * len(senders))})", list(senders)


def _new_counter() -> dict:
    return dict.fromkeys(["n"] + _REPORT_FIELDS + [k for k, _ in _CONF_LEVELS], 0)


def _tally(counter, row):
    """Increment a sender's coverage counters from a row/dict (None-safe). Works
    on both a sqlite3.Row (stored columns) and a classify() result dict."""
    counter["n"] += 1
    if row["year"] is not None:    counter["year"] += 1
    if row["country"] is not None: counter["country"] += 1
    if row["season"] is not None or row["episode"] is not None: counter["se"] += 1
    conf = row["classify_confidence"]
    if conf is not None and conf >= 0.8: counter["cat"] += 1   # category from a real signal
    if row["category"] == "unklar": counter["unklar"] += 1
    if conf is not None:
        for key, level in _CONF_LEVELS:
            if round(conf, 2) == level: counter[key] += 1
    flags = row["flags"] or ""
    for letter in "asut":
        if letter.upper() in flags: counter["flag_" + letter] += 1


def _summarize(counter, by_confidence=False) -> dict:
    n = counter["n"]
    out = {"n": n}
    out.update({f + "_pct": round(100 * counter[f] / n, 1) for f in _REPORT_FIELDS})
    if by_confidence:
        out.update({k + "_pct": round(100 * counter[k] / n, 1) for k, _ in _CONF_LEVELS})
    return out


def classify_report(conn, live: bool, min_rows=REPORT_MIN_ROWS, senders=None,
                    by_confidence=False) -> dict:
    """Per-sender classify coverage. live=False summarizes the stored columns;
    live=True runs classify() over the rows without writing. `senders` limits the
    scan to a list of senders; `by_confidence` adds per-confidence-level columns.
    Read-only -> no transaction."""
    acc = {}
    where, params = _sender_clause(senders)
    if live:
        rows = conn.execute("SELECT mediathek_id, sender, topic, title, "
                            "description, duration FROM mediathek " + where, params)
        for r in rows:
            meta = classify(r["sender"], r["topic"], r["title"],
                            r["description"], r["duration"])
            _tally(acc.setdefault(r["sender"], _new_counter()), meta)
    else:
        rows = conn.execute("SELECT sender, year, country, season, episode, "
                            "category, classify_confidence, flags FROM mediathek "
                            + where, params)
        for r in rows:
            _tally(acc.setdefault(r["sender"], _new_counter()), r)
    return {s: _summarize(c, by_confidence) for s, c in acc.items() if c["n"] >= min_rows}


_REPORT_TABLE_COLS = [("year", "year"), ("country", "cntry"), ("se", "S/E"),
                      ("cat", "cat"), ("unklar", "unkl"), ("flag_a", "A"),
                      ("flag_s", "S"), ("flag_u", "U"), ("flag_t", "T")]
_CONF_TABLE_COLS = [("c90", "c.9"), ("c80", "c.8"), ("c50", "c.5"), ("c20", "c.2")]


def _print_report_table(report, mode, by_confidence=False):
    """One aligned line per sender, sorted by row count, to stdout (the result).
    With by_confidence the single cat column is replaced by per-level columns."""
    cols = _REPORT_TABLE_COLS
    if by_confidence:
        i = next(j for j, (f, _) in enumerate(cols) if f == "cat")
        cols = cols[:i] + _CONF_TABLE_COLS + cols[i + 1:]
    print(f"classify coverage ({mode}, % of rows)")
    print(f'{"SENDER":14}{"n":>8}' + "".join(f"{h:>7}" for _, h in cols))
    for sender, st in sorted(report.items(), key=lambda kv: -kv[1]["n"]):
        print(f'{sender:14}{st["n"]:>8}'
              + "".join(f'{st[f + "_pct"]:>7.1f}' for f, _ in cols))


def _classify_report_cmd(conn, args) -> dict:
    senders = _split_senders(args.sender)
    if args.live:
        log.info("running classify() live (no writes)")
    report = classify_report(conn, live=args.live, min_rows=args.min_rows,
                             senders=senders, by_confidence=args.by_confidence)
    mode = "live" if args.live else "stored"
    if args.json:
        return {"mode": mode, "senders": report}
    _print_report_table(report, mode, by_confidence=args.by_confidence)
    return {}


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

    classify = sub.add_parser("classify", help="extract metadata and inspect the result",
                              description="Extract structured metadata from the "
                                          "free-text fields (run) and inspect the "
                                          "result with read-only reports. Progress "
                                          "is printed to stderr.")
    csub = classify.add_subparsers(dest="classify_cmd", required=True, metavar="action")

    crun = csub.add_parser("run", help="classify mirrored rows (writes the classify columns)",
                           description="Extract structured metadata (title, "
                                       "series/season/episode, category, year, "
                                       "country, language, flags) into the classify "
                                       "columns and flip status 0 -> 1.")
    crun.add_argument("--force", action="store_true", help="reclassify all rows, not just unclassified")

    crep = csub.add_parser("report", help="read-only per-sender coverage report",
                           description="Per-sender coverage of the classify fields. "
                                       "Reads the stored columns by default; --live "
                                       "re-runs classify() without writing.")
    crep.add_argument("--sender",   metavar="X[,Y]",                          help="restrict to these senders (comma-separated)")
    crep.add_argument("--min-rows", type=int, default=REPORT_MIN_ROWS, metavar="N", help=f"omit senders with fewer rows (default {REPORT_MIN_ROWS}; 0 shows all)")
    crep.add_argument("--live",          action="store_true",                 help="run classify() live instead of reading the stored columns")
    crep.add_argument("--by-confidence", action="store_true",                 help="split the category column into per-confidence-level columns")

    return parser


def _setup_logging():
    """Route the theke logger to stderr, one prefixed line per record flushed
    live, so a long stage stays visible without polluting the stdout result (the
    single JSON object in --json mode). Rebuilt on every call so the handler
    always binds the current sys.stderr (which pytest's capsys swaps per test)."""
    log.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("-> %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def main(argv=None) -> int:
    """CLI entry point; returns the process exit code."""
    _setup_logging()

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
                try:     result = cmd_mirror(conn, cfg, args)
                finally: conn.close()
            case "classify":
                conn = db_connect(cfg.db_path)
                try:     result = cmd_classify(conn, cfg, args)
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
