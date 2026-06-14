"""Theke -- self-hosted media manager CLI.

All logic lives in this package module (split into more files later if ever
needed). Sections: config / DB / CLI.
"""

import argparse
import dataclasses
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, time, timezone

CONFIG_DEFAULT_PATH = "theke.json"


# -- config ------------------------------------------------------------------

class ConfigError(Exception):
    """Invalid or unreadable configuration."""


@dataclass
class Config:
    """Effective configuration; defaults < config file < CLI parameters."""
    db_path: str = "theke.db"


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
# migration when it lands (phase 2 adds the mediathek table as entry 1).
MIGRATIONS: list[tuple[str, ...]] = []


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


# -- cli ----------------------------------------------------------------------
# Stable grammar and exit codes: the GUI drives the CLI and parses the --json
# output (exactly one JSON object on stdout per call).

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3


def cmd_config(conn, cfg, args) -> dict:
    """Show the effective configuration (after precedence resolution)."""
    return dataclasses.asdict(cfg)


# Pipeline stages register here: name -> (handler, help text, needs_db).
# Each handler gets the DB connection (open if needs_db, else None), the
# effective Config and the parsed args, and returns the result as a
# JSON-serializable dict.
COMMANDS = {
    "config": (cmd_config, "show the effective configuration", False),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="theke",       description="Self-hosted media manager for German public broadcasters")
    parser.add_argument("--config", metavar="PATH",      help=f"config file (default: {CONFIG_DEFAULT_PATH})")
    parser.add_argument("--db",     metavar="PATH",      help="database file (overrides db_path from config)")
    parser.add_argument("--json",   action="store_true", help="machine-readable output: one JSON object on stdout")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    for name, (_, help_text, _) in COMMANDS.items():
        sub.add_parser(name, help=help_text)
    return parser


def main(argv=None) -> int:
    """CLI entry point; returns the process exit code."""

    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse handles usage errors and --help
        return EXIT_USAGE if exc.code else EXIT_OK

    try:
        cfg = load_config(args.config, overrides={"db_path": args.db})
        handler, _, needs_db = COMMANDS[args.command]
        conn = db_connect(cfg.db_path) if needs_db else None
        try:
            result = handler(conn, cfg, args)
        finally:
            if conn is not None:
                conn.close()
    except (ConfigError, DbError, DbLockedError) as exc:
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
