"""Theke -- self-hosted media manager CLI.

All logic lives in this package module (split into more files later if ever
needed). Sections: config / DB / CLI.
"""

import argparse
import dataclasses
import json
import sqlite3
import sys
from dataclasses import dataclass

CONFIG_DEFAULT_PATH = "theke.json"


# --------------------------------------------------------------------- config

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


# ------------------------------------------------------------------------- db
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


# ------------------------------------------------------------------------ cli
# Stable grammar and exit codes: the Delphi GUI drives the CLI and parses the
# --json output (exactly one JSON object on stdout per call).

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3


def cmd_config(conn, cfg, args) -> dict:
    """Show the effective configuration (after precedence resolution)."""
    return dataclasses.asdict(cfg)


# Pipeline stages register here: name -> (handler, help text). Each handler
# gets the open DB connection, the effective Config and the parsed args, and
# returns the result as a JSON-serializable dict.
COMMANDS = {
    "config": (cmd_config, "show the effective configuration"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="theke",
        description="Self-hosted media manager for German public broadcasters")
    parser.add_argument("--config", metavar="PATH",
                        help=f"config file (default: {CONFIG_DEFAULT_PATH})")
    parser.add_argument("--db", metavar="PATH",
                        help="database file (overrides db_path from config)")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output: one JSON object on stdout")
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="command")
    for name, (_, help_text) in COMMANDS.items():
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
        conn = db_connect(cfg.db_path)
        try:
            result = COMMANDS[args.command][0](conn, cfg, args)
        finally:
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
