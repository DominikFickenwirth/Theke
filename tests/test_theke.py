"""Tests for the Theke CLI (config / DB / CLI skeleton)."""

import json
import sqlite3

import pytest

from theke import (
    Config,
    ConfigError,
    DbError,
    DbLockedError,
    db_connect,
    greeting,
    load_config,
    main,
)


# ---------------------------------------------------------------- placeholder

def test_greeting_returns_hallo_welt():
    assert greeting() == "Hallo Welt"


def test_main_prints_greeting(capsys):
    main()
    assert capsys.readouterr().out.strip() == "Hallo Welt"


# --------------------------------------------------------------------- config

def write_config(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_config_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no theke.json here
    cfg = load_config(None)
    assert cfg == Config()
    assert cfg.db_path == "theke.db"


def test_config_default_path_is_picked_up(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_config(tmp_path / "theke.json", {"db_path": "other.db"})
    assert load_config(None).db_path == "other.db"


def test_config_explicit_file_overrides_defaults(tmp_path):
    path = tmp_path / "custom.json"
    write_config(path, {"db_path": "x/y.db"})
    assert load_config(str(path)).db_path == "x/y.db"


def test_config_explicit_missing_file_is_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "nope.json"))


def test_config_empty_object_keeps_defaults(tmp_path):
    path = tmp_path / "empty.json"
    write_config(path, {})
    assert load_config(str(path)) == Config()


def test_config_unknown_key_is_error(tmp_path):
    path = tmp_path / "typo.json"
    write_config(path, {"db_pathh": "x.db"})
    with pytest.raises(ConfigError, match="db_pathh"):
        load_config(str(path))


def test_config_wrong_type_is_error(tmp_path):
    path = tmp_path / "typed.json"
    write_config(path, {"db_path": 42})
    with pytest.raises(ConfigError, match="db_path"):
        load_config(str(path))


def test_config_broken_json_is_error(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="broken.json"):
        load_config(str(path))


def test_config_empty_file_is_error(tmp_path):
    path = tmp_path / "void.json"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(path))


def test_config_non_object_json_is_error(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ConfigError, match="object"):
        load_config(str(path))


def test_config_cli_overrides_file(tmp_path):
    path = tmp_path / "prec.json"
    write_config(path, {"db_path": "file.db"})
    cfg = load_config(str(path), overrides={"db_path": "cli.db"})
    assert cfg.db_path == "cli.db"


def test_config_none_override_keeps_file_value(tmp_path):
    path = tmp_path / "prec.json"
    write_config(path, {"db_path": "file.db"})
    cfg = load_config(str(path), overrides={"db_path": None})
    assert cfg.db_path == "file.db"


# ------------------------------------------------------------------------- db

DUMMY_MIGRATIONS = [
    ("CREATE TABLE a (x INTEGER)",),
    ("CREATE TABLE b (y INTEGER)", "CREATE INDEX b_y ON b (y)"),
]


def user_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row["name"] for row in rows}


def test_db_connect_creates_file_and_applies_settings(tmp_path):
    db = tmp_path / "t.db"
    conn = db_connect(str(db), migrations=[])
    try:
        assert db.exists()
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert user_version(conn) == 0
    finally:
        conn.close()


def test_db_migrations_apply_and_bump_version(tmp_path):
    conn = db_connect(str(tmp_path / "t.db"), migrations=DUMMY_MIGRATIONS)
    try:
        assert user_version(conn) == 2
        assert {"a", "b"} <= table_names(conn)
    finally:
        conn.close()


def test_db_migrations_are_idempotent_across_reconnects(tmp_path):
    db = str(tmp_path / "t.db")
    db_connect(db, migrations=DUMMY_MIGRATIONS).close()
    conn = db_connect(db, migrations=DUMMY_MIGRATIONS)
    try:
        assert user_version(conn) == 2
    finally:
        conn.close()


def test_db_only_new_migrations_run_on_upgrade(tmp_path):
    db = str(tmp_path / "t.db")
    db_connect(db, migrations=DUMMY_MIGRATIONS[:1]).close()
    conn = db_connect(db, migrations=DUMMY_MIGRATIONS)
    try:
        assert user_version(conn) == 2
        assert {"a", "b"} <= table_names(conn)
    finally:
        conn.close()


def test_db_failing_migration_rolls_back_everything(tmp_path):
    db = str(tmp_path / "t.db")
    bad = [("CREATE TABLE a (x INTEGER)",), ("CREATE TABLE b (",)]
    with pytest.raises(sqlite3.Error):
        db_connect(db, migrations=bad)
    conn = db_connect(db, migrations=[])
    try:
        assert user_version(conn) == 0
        assert "a" not in table_names(conn)
    finally:
        conn.close()


def test_db_newer_than_code_is_error(tmp_path):
    db = str(tmp_path / "t.db")
    db_connect(db, migrations=DUMMY_MIGRATIONS).close()
    with pytest.raises(DbError, match="newer"):
        db_connect(db, migrations=DUMMY_MIGRATIONS[:1])


def test_db_second_connection_is_rejected(tmp_path):
    db = str(tmp_path / "t.db")
    conn = db_connect(db, migrations=[])
    try:
        with pytest.raises(DbLockedError):
            db_connect(db, migrations=[])
    finally:
        conn.close()


def test_db_lock_is_released_on_close(tmp_path):
    db = str(tmp_path / "t.db")
    db_connect(db, migrations=[]).close()
    conn = db_connect(db, migrations=[])
    conn.close()
