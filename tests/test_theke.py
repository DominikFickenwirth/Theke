"""Tests for the Theke CLI (config / DB / CLI skeleton)."""

import io
import json
import lzma
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from theke import *


# -- config ------------------------------------------------------------------

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


# -- db ----------------------------------------------------------------------

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


# -- cli ---------------------------------------------------------------------

def test_cli_config_human_output(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    assert main(["--db", db, "config"]) == 0
    out = capsys.readouterr().out
    assert f"db_path = {db}" in out


def test_cli_config_json_output(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    assert main(["--json", "--db", db, "config"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["db_path"] == db
    assert result["filmliste_url"].endswith(".xz")  # mirror keys present too


def test_cli_db_flag_overrides_config_file(tmp_path, capsys):
    write_config(tmp_path / "c.json", {"db_path": str(tmp_path / "file.db")})
    db = str(tmp_path / "cli.db")
    args = ["--config", str(tmp_path / "c.json"), "--db", db, "--json", "config"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["db_path"] == db


def test_cli_config_does_not_touch_db(tmp_path):
    db = tmp_path / "t.db"
    assert main(["--db", str(db), "config"]) == 0
    assert not db.exists()


def test_cli_config_works_while_db_is_locked(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    conn = db_connect(db, migrations=[])
    try:
        assert main(["--json", "--db", db, "config"]) == 0
        assert json.loads(capsys.readouterr().out)["db_path"] == db
    finally:
        conn.close()


def test_cli_broken_config_human_error(tmp_path, capsys):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert main(["--config", str(path), "config"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "broken.json" in captured.err


def test_cli_broken_config_json_error(tmp_path, capsys):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert main(["--json", "--config", str(path), "config"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert "broken.json" in result["error"]


def test_cli_locked_db_exits_3(tmp_path, capsys, monkeypatch):
    import theke
    monkeypatch.setitem(
        theke.COMMANDS, "dummy",
        (lambda conn, cfg, args: {"ok": True}, "db-touching test command",
         True, []))
    db = str(tmp_path / "t.db")
    conn = db_connect(db, migrations=[])
    try:
        assert main(["--json", "--db", db, "dummy"]) == 3
        assert "error" in json.loads(capsys.readouterr().out)
    finally:
        conn.close()


def test_cli_unknown_command_exits_2(capsys):
    assert main(["frobnicate"]) == 2


def test_cli_missing_command_exits_2(capsys):
    assert main([]) == 2


# -- mirror: conversions -----------------------------------------------------

def test_film_id_matches_utf16le_sha256_spec():
    import hashlib
    parts = ("ARD", "Tatort", "http://v/1.mp4", "http://w/1")
    expected = hashlib.sha256("".join(parts).encode("utf-16-le")).hexdigest()
    assert film_id(*parts) == expected


def test_film_id_is_order_sensitive():
    assert film_id("a", "b", "c", "d") != film_id("b", "a", "c", "d")


def test_decode_rel_url_relative():
    base = "http://example.com/path/video.mp4"
    assert decode_rel_url(base, "20|small.mp4") == base[:20] + "small.mp4"


def test_decode_rel_url_empty_is_empty():
    assert decode_rel_url("http://x/y.mp4", "") == ""


def test_decode_rel_url_without_pipe_is_verbatim():
    assert decode_rel_url("http://x/y.mp4", "http://full/sub.vtt") == \
        "http://full/sub.vtt"


def test_decode_rel_url_broken_length_is_verbatim():
    # a non-numeric prefix is not the relative scheme -> keep as given
    assert decode_rel_url("http://x/y.mp4", "abc|tail") == "abc|tail"


def test_parse_duration_hhmmss():
    assert parse_duration("01:02:03") == 3723


def test_parse_duration_short_fields():
    assert parse_duration("00:00:30") == 30


def test_parse_duration_empty_is_none():
    assert parse_duration("") is None


def test_parse_duration_garbage_is_none():
    assert parse_duration("nonsense") is None


def test_to_int_plain():
    assert to_int("42") == 42


def test_to_int_empty_is_none():
    assert to_int("") is None


def test_to_int_garbage_is_none():
    assert to_int("12x") is None


def test_parse_date_prefers_datum_zeit():
    assert parse_date("14.06.2026", "20:15:00", "1700000000") == \
        "2026-06-14 20:15:00"


def test_parse_date_missing_time_defaults_midnight():
    assert parse_date("14.06.2026", "", "") == "2026-06-14 00:00:00"


def test_parse_date_falls_back_to_epoch_utc():
    expected = datetime.fromtimestamp(1700000000, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")
    assert parse_date("", "", "1700000000") == expected


def test_parse_date_all_empty_is_none():
    assert parse_date("", "", "") is None


def test_parse_date_garbage_is_none():
    assert parse_date("not-a-date", "xx", "nope") is None


# -- mirror: parse_filmliste -------------------------------------------------

# Column-name header (second "Filmliste"); content is ignored by the parser.
_COLNAMES = ["Sender", "Thema", "Titel", "Datum", "Zeit", "Dauer"]


def make_x(**vals):
    """Build a 20-field MV film row, fields addressed by their MV name."""
    row = [""] * len(FIELDS)
    for key, value in vals.items():
        row[FIELDS.index(key)] = value
    return row


def mv_text(meta, films):
    """Render the MV format: a flat object with duplicate keys."""
    parts = ['"Filmliste":' + json.dumps(meta),
             '"Filmliste":' + json.dumps(_COLNAMES)]
    for film in films:
        parts.append('"X":' + json.dumps(film))
    return "{" + ",".join(parts) + "}"


def parse(text, **kw):
    gen = parse_filmliste(io.StringIO(text), **kw)
    return next(gen), list(gen)  # (metadata, films)


def test_parse_yields_metadata_first():
    meta_in = ["loaded", "12.06.2026, 09:00", "3.1", "MServer", "idhash"]
    meta, films = parse(mv_text(meta_in, [make_x(sender="ARD", titel="A")]))
    assert meta["erstellt_am"] == "12.06.2026, 09:00"
    assert meta["id"] == "idhash"
    assert len(films) == 1


def test_parse_incomplete_metadata_yields_empty_strings():
    meta, _ = parse(mv_text(["only-loaded"], []))
    assert meta["erstellt_am"] == ""
    assert meta["id"] == ""


def test_parse_maps_columns_to_db_fields():
    row = make_x(sender="ARD", thema="Tatort", titel="Der Fall",
                 beschreibung="desc", geo="DE", url="http://v/1.mp4",
                 website="http://w/1")
    _, films = parse(mv_text(["", "", "", "", ""], [row]))
    f = films[0]
    assert f["sender"] == "ARD"
    assert f["topic"] == "Tatort"
    assert f["title"] == "Der Fall"
    assert f["description"] == "desc"
    assert f["geo"] == "DE"
    assert f["url_video"] == "http://v/1.mp4"
    assert f["url_website"] == "http://w/1"


def test_parse_field_inheritance_for_sender_and_topic():
    rows = [make_x(sender="ARD", thema="Sport", titel="1"),
            make_x(titel="2"),                       # inherit ARD / Sport
            make_x(sender="ZDF", titel="3"),         # inherit Sport only
            make_x(thema="News", titel="4")]         # inherit ZDF, new topic
    _, films = parse(mv_text(["", "", "", "", ""], rows))
    assert [f["sender"] for f in films] == ["ARD", "ARD", "ZDF", "ZDF"]
    assert [f["topic"] for f in films] == ["Sport", "Sport", "Sport", "News"]


def test_parse_film_id_uses_inherited_sender_and_topic():
    import hashlib
    rows = [make_x(sender="ARD", thema="Sport", url="u", website="w"),
            make_x(url="u", website="w")]            # inherits ARD / Sport
    _, films = parse(mv_text(["", "", "", "", ""], rows))
    expected = hashlib.sha256("ARDSportuw".encode("utf-16-le")).hexdigest()
    assert films[1]["mediathek_id"] == expected


def test_parse_status_new_and_old():
    rows = [make_x(titel="new", neu="true"),
            make_x(titel="old", neu="false"),
            make_x(titel="blank")]
    _, films = parse(mv_text(["", "", "", "", ""], rows))
    assert [f["status"] for f in films] == ["0", "1", "1"]


def test_parse_decodes_hd_and_small_urls():
    base = "http://example.com/path/video.mp4"
    row = make_x(url=base, url_hd="20|hd.mp4", url_klein="")
    _, films = parse(mv_text(["", "", "", "", ""], [row]))
    assert films[0]["url_video_hd"] == base[:20] + "hd.mp4"
    assert films[0]["url_video_small"] == ""


def test_parse_large_entry_across_chunk_boundary():
    row = make_x(sender="ARD", titel="X" * 5000, url="u")
    _, films = parse(mv_text(["", "", "", "", ""], [row]), chunk_size=8)
    assert films[0]["title"] == "X" * 5000
    assert films[0]["sender"] == "ARD"


def test_parse_metadata_only_no_films():
    meta, films = parse(mv_text(["", "x", "", "", "theid"], []))
    assert meta["id"] == "theid"
    assert films == []


# -- mirror: migration + full import -----------------------------------------

def open_db(tmp_path):
    return db_connect(str(tmp_path / "theke.db"))  # uses real MIGRATIONS


def make_list(rows, list_id="theid", created="01.01.2026, 00:00"):
    return parse(mv_text(["", created, "", "", list_id], rows))


def film_rows(conn):
    return conn.execute("SELECT * FROM mediathek ORDER BY title").fetchall()


def test_migration_creates_mediathek_and_meta(tmp_path):
    conn = open_db(tmp_path)
    try:
        assert {"mediathek", "meta"} <= table_names(conn)
        assert user_version(conn) == 1
    finally:
        conn.close()


def test_full_import_inserts_rows(tmp_path):
    conn = open_db(tmp_path)
    try:
        meta, films = make_list([
            make_x(sender="ARD", thema="T", titel="A", url="http://v/a",
                   website="http://w/a", dauer="00:01:00", neu="true",
                   datum="14.06.2026", zeit="20:15:00"),
            make_x(sender="ZDF", titel="B", url="http://v/b"),
        ])
        result = full_import(conn, films, meta)
        assert result["imported"] == 2
        rows = film_rows(conn)
        assert [r["title"] for r in rows] == ["A", "B"]
        assert rows[0]["sender"] == "ARD"
        assert rows[0]["status"] == "0"
        assert rows[0]["duration"] == 60
        assert rows[0]["date"] == "2026-06-14 20:15:00"
        assert rows[1]["status"] == "1"
    finally:
        conn.close()


def test_full_import_is_idempotent(tmp_path):
    conn = open_db(tmp_path)
    try:
        rows = [make_x(sender="ARD", titel="A", url="u"),
                make_x(sender="ZDF", titel="B", url="v")]
        full_import(conn, make_list(rows)[1], make_list(rows)[0])
        meta, films = make_list(rows)
        result = full_import(conn, films, meta)
        assert result["imported"] == 2
        assert len(film_rows(conn)) == 2
    finally:
        conn.close()


def test_full_import_updates_changed_film(tmp_path):
    conn = open_db(tmp_path)
    try:
        base = dict(sender="ARD", thema="T", url="u", website="w")
        full_import(conn, *reversed(make_list([make_x(titel="old", **base)])))
        meta, films = make_list([make_x(titel="new", **base)])
        full_import(conn, films, meta)
        rows = film_rows(conn)
        assert len(rows) == 1
        assert rows[0]["title"] == "new"
    finally:
        conn.close()


def test_full_import_preserves_phase3_ids(tmp_path):
    conn = open_db(tmp_path)
    try:
        base = dict(sender="ARD", thema="T", url="u", website="w")
        meta, films = make_list([make_x(titel="old", **base)])
        full_import(conn, films, meta)
        conn.execute("UPDATE mediathek SET tmdb_id='123', imdb_id='tt9', "
                     "language='de', match_confidence=0.9")
        meta, films = make_list([make_x(titel="new", **base)])
        full_import(conn, films, meta)
        row = film_rows(conn)[0]
        assert row["title"] == "new"          # mirror column updated
        assert row["tmdb_id"] == "123"        # phase-3 assignment preserved
        assert row["imdb_id"] == "tt9"
        assert row["language"] == "de"
        assert row["match_confidence"] == 0.9
    finally:
        conn.close()


def test_full_import_deletes_vanished(tmp_path):
    conn = open_db(tmp_path)
    try:
        both = [make_x(sender="ARD", titel="A", url="a"),
                make_x(sender="ZDF", titel="B", url="b")]
        full_import(conn, *reversed(make_list(both)))
        meta, films = make_list([make_x(sender="ARD", titel="A", url="a")])
        result = full_import(conn, films, meta)
        assert result["deleted"] == 1
        assert [r["title"] for r in film_rows(conn)] == ["A"]
    finally:
        conn.close()


def test_full_import_sets_meta(tmp_path):
    conn = open_db(tmp_path)
    try:
        meta, films = make_list([make_x(sender="ARD", titel="A", url="a")],
                                list_id="abc123", created="02.03.2026, 07:30")
        full_import(conn, films, meta)
        assert get_meta(conn, "filmliste_id") == "abc123"
        assert get_meta(conn, "filmliste_created") == "02.03.2026, 07:30"
    finally:
        conn.close()


def test_get_meta_missing_key_is_none(tmp_path):
    conn = open_db(tmp_path)
    try:
        assert get_meta(conn, "nope") is None
    finally:
        conn.close()


# -- mirror: diff import + update decision ------------------------------------

NOON_UTC = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)


def test_can_use_diff_after_cutoff():
    assert can_use_diff("14.06.2026, 08:00", now=NOON_UTC) is True


def test_can_use_diff_before_cutoff():
    assert can_use_diff("14.06.2026, 06:30", now=NOON_UTC) is False


def test_can_use_diff_at_cutoff_is_false():
    assert can_use_diff("14.06.2026, 07:00", now=NOON_UTC) is False


def test_can_use_diff_previous_day_is_false():
    assert can_use_diff("13.06.2026, 23:00", now=NOON_UTC) is False


def test_can_use_diff_garbage_is_false():
    assert can_use_diff("not a date", now=NOON_UTC) is False


def test_can_use_diff_none_is_false():
    assert can_use_diff(None, now=NOON_UTC) is False


def test_diff_import_merges_without_deleting(tmp_path):
    conn = open_db(tmp_path)
    try:
        a = dict(sender="ARD", thema="T", url="a", website="w")
        full_import(conn, *reversed(make_list([make_x(titel="A", **a),
                                               make_x(sender="ZDF", titel="B",
                                                      url="b")])))
        meta, films = make_list([make_x(titel="A2", **a),         # update A
                                 make_x(sender="ARTE", titel="C",  # new
                                        url="c")])
        result = diff_import(conn, films, meta)
        assert result["imported"] == 2
        rows = {r["title"] for r in film_rows(conn)}
        assert rows == {"A2", "B", "C"}             # B (not in diff) survives
    finally:
        conn.close()


def test_diff_import_preserves_phase3_ids(tmp_path):
    conn = open_db(tmp_path)
    try:
        a = dict(sender="ARD", thema="T", url="a", website="w")
        full_import(conn, *reversed(make_list([make_x(titel="A", **a)])))
        conn.execute("UPDATE mediathek SET tmdb_id='77'")
        diff_import(conn, *reversed(make_list([make_x(titel="A2", **a)])))
        row = film_rows(conn)[0]
        assert row["title"] == "A2"
        assert row["tmdb_id"] == "77"
    finally:
        conn.close()


# -- mirror: cmd_mirror decision (mocked network) ----------------------------

CFG = SimpleNamespace(filmliste_url="FULL", filmliste_diff_url="DIFF",
                      filmliste_id_url="ID")


def xz_list(rows, list_id="id", created="01.01.2020, 00:00"):
    text = mv_text(["", created, "", "", list_id], rows)
    return lzma.compress(text.encode("utf-8"))


def install_http(monkeypatch, mapping):
    import theke

    def fake_get(url):
        value = mapping.get(url)
        if value is None:
            raise RuntimeError(f"unexpected url: {url}")
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(theke, "http_get", fake_get)


def recent_created():
    return f"{datetime.now(timezone.utc):%d.%m.%Y}, 23:59"


def args(force=False):
    return SimpleNamespace(force=force)


def test_cmd_mirror_full_on_empty_db(tmp_path, monkeypatch):
    install_http(monkeypatch, {"FULL": xz_list([make_x(sender="ARD", titel="A",
                                                       url="a")], "id1")})
    conn = open_db(tmp_path)
    try:
        result = cmd_mirror(conn, CFG, args())
        assert result["action"] == "full"
        assert result["imported"] == 1
        assert get_meta(conn, "filmliste_id") == "id1"
    finally:
        conn.close()


def test_cmd_mirror_force_redownloads_full(tmp_path, monkeypatch):
    install_http(monkeypatch, {"FULL": xz_list([make_x(sender="ARD", titel="A",
                                                       url="a")], "id1",
                                               recent_created())})
    conn = open_db(tmp_path)
    try:
        cmd_mirror(conn, CFG, args())                 # seed local list
        result = cmd_mirror(conn, CFG, args(force=True))
        assert result["action"] == "full"             # despite fresh local list
    finally:
        conn.close()


def test_cmd_mirror_skip_when_id_matches_and_too_old(tmp_path, monkeypatch):
    conn = open_db(tmp_path)
    try:
        full_import(conn, [], {"id": "id1", "erstellt_am": "01.01.2020, 00:00"})
        install_http(monkeypatch, {"ID": b"id1\n"})
        result = cmd_mirror(conn, CFG, args())
        assert result == {"action": "skip"}
    finally:
        conn.close()


def test_cmd_mirror_full_when_id_changed(tmp_path, monkeypatch):
    conn = open_db(tmp_path)
    try:
        full_import(conn, [], {"id": "id1", "erstellt_am": "01.01.2020, 00:00"})
        install_http(monkeypatch, {"ID": b"id2",
                                   "FULL": xz_list([make_x(sender="ARD",
                                                           titel="A", url="a")],
                                                   "id2")})
        result = cmd_mirror(conn, CFG, args())
        assert result["action"] == "full"
        assert get_meta(conn, "filmliste_id") == "id2"
    finally:
        conn.close()


def test_cmd_mirror_full_when_id_unreachable(tmp_path, monkeypatch):
    conn = open_db(tmp_path)
    try:
        full_import(conn, [], {"id": "id1", "erstellt_am": "01.01.2020, 00:00"})
        install_http(monkeypatch, {"ID": RuntimeError("boom"),
                                   "FULL": xz_list([make_x(sender="ARD",
                                                           titel="A", url="a")],
                                                   "id2")})
        result = cmd_mirror(conn, CFG, args())
        assert result["action"] == "full"
    finally:
        conn.close()


def test_cmd_mirror_diff_when_fresh(tmp_path, monkeypatch):
    conn = open_db(tmp_path)
    try:
        full_import(conn, *reversed(make_list([make_x(sender="ARD", titel="A",
                                                      url="a")], created="x")))
        set_meta(conn, "filmliste_created", recent_created())
        install_http(monkeypatch, {"DIFF": xz_list([make_x(sender="ZDF",
                                                           titel="B", url="b")],
                                                   "id2", recent_created())})
        result = cmd_mirror(conn, CFG, args())
        assert result["action"] == "diff"
        assert result["imported"] == 1
        assert {r["title"] for r in film_rows(conn)} == {"A", "B"}
    finally:
        conn.close()


def test_cmd_mirror_empty_diff_falls_back_to_full(tmp_path, monkeypatch):
    conn = open_db(tmp_path)
    try:
        full_import(conn, *reversed(make_list([make_x(sender="ARD", titel="A",
                                                      url="a")], created="x")))
        set_meta(conn, "filmliste_created", recent_created())
        install_http(monkeypatch, {
            "DIFF": xz_list([], "id2", recent_created()),
            "FULL": xz_list([make_x(sender="ARD", titel="A", url="a"),
                             make_x(sender="ZDF", titel="B", url="b")], "id2"),
        })
        result = cmd_mirror(conn, CFG, args())
        assert result["action"] == "full"
        assert result["imported"] == 2
    finally:
        conn.close()


# -- mirror: theke mirror CLI end to end -------------------------------------

def one_film(list_id="id1", created="01.01.2020, 00:00"):
    return xz_list([make_x(sender="ARD", titel="A", url="a")], list_id, created)


def test_cli_mirror_full_json(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "t.db")
    install_http(monkeypatch, {Config().filmliste_url: one_film()})
    assert main(["--json", "--db", db, "mirror"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["action"] == "full"
    assert result["imported"] == 1
    assert result["deleted"] == 0


def test_cli_mirror_human_output(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "t.db")
    install_http(monkeypatch, {Config().filmliste_url: one_film()})
    assert main(["--db", db, "mirror"]) == 0
    assert "action = full" in capsys.readouterr().out


def test_cli_mirror_force_redownloads(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "t.db")
    # fresh local list: without --force the next run would attempt a diff (whose
    # URL is not mocked); --force must take the full path instead.
    install_http(monkeypatch, {Config().filmliste_url: one_film(
        created=recent_created())})
    assert main(["--db", db, "mirror"]) == 0
    capsys.readouterr()
    assert main(["--json", "--db", db, "mirror", "--force"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "full"


def test_cli_mirror_skip_on_unchanged_id(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "t.db")
    install_http(monkeypatch, {Config().filmliste_url: one_film(list_id="id1")})
    assert main(["--db", db, "mirror"]) == 0          # full, stores id1
    capsys.readouterr()
    install_http(monkeypatch, {Config().filmliste_id_url: b"id1\n"})
    assert main(["--json", "--db", db, "mirror"]) == 0
    assert json.loads(capsys.readouterr().out) == {"action": "skip"}


def test_cli_mirror_locked_db_exits_3(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "t.db")
    install_http(monkeypatch, {Config().filmliste_url: one_film()})
    conn = db_connect(db, migrations=[])
    try:
        assert main(["--json", "--db", db, "mirror"]) == 3
    finally:
        conn.close()
