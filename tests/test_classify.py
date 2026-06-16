"""Tests for the classify stage (phase 3, part 1): metadata extraction."""

import json

import pytest

from theke import *


# -- helpers -----------------------------------------------------------------

def user_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def column_names(conn, table="mediathek"):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


# columns the classify migration adds (language already exists from phase 2)
NEW_COLS = {
    "clean_title", "series_name", "season", "episode", "episode_count",
    "category", "year", "country", "flags", "classify_confidence",
}


# -- migration ---------------------------------------------------------------

def test_classify_migration_adds_columns_on_fresh_db(tmp_path):
    conn = db_connect(str(tmp_path / "theke.db"))  # real MIGRATIONS
    try:
        assert user_version(conn) == 2               # phase 2 + phase 3
        assert NEW_COLS <= column_names(conn)
    finally:
        conn.close()


def test_classify_migration_upgrades_v1_db(tmp_path):
    db = str(tmp_path / "theke.db")
    db_connect(db, migrations=MIGRATIONS[:1]).close()   # stop at phase-2 schema
    conn = db_connect(db)                               # apply phase-3 migration
    try:
        assert user_version(conn) == 2
        cols = column_names(conn)
        assert NEW_COLS <= cols
        # a row written under v1 has the new columns, all NULL
        conn.execute("INSERT INTO mediathek (status, mediathek_id) VALUES ('0','x')")
        row = conn.execute("SELECT * FROM mediathek").fetchone()
        for col in NEW_COLS:
            assert row[col] is None
    finally:
        conn.close()
