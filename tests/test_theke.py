"""Tests for the Theke CLI (config / DB / CLI skeleton)."""

import io
import json
import logging
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


def test_config_queue_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(None)
    assert cfg.queue_auto_approve is False
    assert cfg.languages == ["de"]
    assert cfg.name_template == "{title} ({year})"


def test_config_queue_keys_from_file(tmp_path):
    path = tmp_path / "q.json"
    write_config(path, {"queue_auto_approve": True, "languages": ["de", "en"],
                        "name_template": "{title} [{year}]"})
    cfg = load_config(str(path))
    assert cfg.queue_auto_approve is True
    assert cfg.languages == ["de", "en"]
    assert cfg.name_template == "{title} [{year}]"


def test_config_fiction_topics_default_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_config(None).fiction_topics == []


def test_config_fiction_topics_from_file(tmp_path):
    path = tmp_path / "ft.json"
    write_config(path, {"fiction_topics": ["Mein Regio-Krimi", "Dorf-Saga"]})
    assert load_config(str(path)).fiction_topics == ["Mein Regio-Krimi", "Dorf-Saga"]


def test_config_languages_wrong_type_is_error(tmp_path):
    path = tmp_path / "ql.json"
    write_config(path, {"languages": "de"})   # must be a list, not a string
    with pytest.raises(ConfigError, match="languages"):
        load_config(str(path))


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
    monkeypatch.setattr(theke, "cmd_fetch", lambda conn, cfg, args: {"ok": True})
    db = str(tmp_path / "t.db")
    conn = db_connect(db, migrations=[])
    try:
        assert main(["--json", "--db", db, "fetch"]) == 3
        assert "error" in json.loads(capsys.readouterr().out)
    finally:
        conn.close()


def test_cli_unknown_command_exits_2(capsys):
    assert main(["frobnicate"]) == 2


def test_cli_missing_command_exits_2(capsys):
    assert main([]) == 2


# -- default sub-actions -----------------------------------------------------

def _parse(argv):
    """Parse argv through the default-action injection, as main() does."""
    import theke
    parser = theke.build_parser()
    return parser.parse_args(theke._inject_default_action(parser, argv))


def test_default_action_enrich_bare_runs_run():
    args = _parse(["enrich"])
    assert args.command == "enrich"
    assert args.enrich_cmd == "run"
    assert args.force is False  # run's own default, seeded by argparse


def test_default_action_explicit_subaction_preserved():
    assert _parse(["enrich", "report"]).enrich_cmd == "report"


def test_default_action_match_bare_with_flag():
    args = _parse(["match", "--tmdb", "603"])
    assert args.command == "match"
    assert args.match_cmd == "run"
    assert args.tmdb == "603"


def test_default_action_after_global_options():
    args = _parse(["--json", "--db", "x.db", "enrich"])
    assert args.enrich_cmd == "run"
    assert args.json is True
    assert args.db == "x.db"


def test_default_action_global_option_equals_form():
    assert _parse(["--db=x.db", "enrich"]).enrich_cmd == "run"


def test_default_action_injection_is_explicit():
    import theke
    parser = theke.build_parser()
    assert theke._inject_default_action(parser, ["enrich"]) == ["enrich", "run"]
    assert theke._inject_default_action(parser, ["match", "--tmdb", "603"]) == ["match", "run", "--tmdb", "603"]


def test_default_action_leaves_explicit_subaction_and_help():
    import theke
    parser = theke.build_parser()
    assert theke._inject_default_action(parser, ["enrich", "report"]) == ["enrich", "report"]
    assert theke._inject_default_action(parser, ["match", "-h"]) == ["match", "-h"]


def test_default_action_untouched_for_commands_without_default():
    import theke
    parser = theke.build_parser()
    assert theke._inject_default_action(parser, ["fetch", "--force"]) == ["fetch", "--force"]


# -- short option aliases ---------------------------------------------------

def test_short_global_options():
    args = _parse(["-c", "cfg.json", "-d", "x.db", "-j", "fetch"])
    assert args.config == "cfg.json"
    assert args.db == "x.db"
    assert args.json is True


def test_short_queue_delete_cluster():
    args = _parse(["queue", "delete", "-cdf"])
    assert args.cancelled is True
    assert args.done is True
    assert args.failed is True
    assert args.all is False


def test_short_fetch_force():
    assert _parse(["fetch", "-f"]).force is True


def test_short_enrich_reset_status_only():
    assert _parse(["enrich", "reset", "-s"]).status_only is True


def test_short_enrich_report():
    args = _parse(["enrich", "report", "-s", "ARD", "-m", "5", "-l", "-d", "-b"])
    assert args.sender == "ARD"
    assert args.min_rows == 5
    assert args.live is True
    assert args.diff is True
    assert args.by_confidence is True


def test_short_enrich_audit():
    args = _parse(["enrich", "audit", "-s", "ZDF", "-c", "country-shape", "-l", "3"])
    assert args.sender == "ZDF"
    assert args.check == "country-shape"
    assert args.limit == 3


def test_short_enrich_show():
    args = _parse(["enrich", "show", "-s", "ARD", "-m", "0.2", "-M", "0.9", "-l", "7"])
    assert args.sender == "ARD"
    assert args.min_conf == 0.2
    assert args.max_conf == 0.9
    assert args.limit == 7


def test_short_enrich_dist():
    args = _parse(["enrich", "dist", "-s", "ARD", "-f", "category", "-l", "10"])
    assert args.sender == "ARD"
    assert args.field == "category"
    assert args.limit == 10


def test_short_match_run():
    args = _parse(["match", "run", "-t", "603", "-T", "series", "-s", "1", "-e", "2", "-d", "-m", "0.5"])
    assert args.tmdb == "603"
    assert args.type == "series"
    assert args.season == 1
    assert args.episode == 2
    assert args.dry_run is True
    assert args.min_conf == 0.5


def test_short_match_show():
    args = _parse(["match", "show", "-t", "603", "-T", "movie", "-m", "0.3", "-l", "5"])
    assert args.tmdb == "603"
    assert args.type == "movie"
    assert args.min_conf == 0.3
    assert args.limit == 5


def test_short_match_reset_status_only():
    assert _parse(["match", "reset", "-s"]).status_only is True


def test_short_queue_add():
    args = _parse(["queue", "add", "-t", "603", "-m", "7"])
    assert args.tmdb == ["603"]
    assert args.mediathek_id == ["7"]


def test_short_queue_list_status():
    assert _parse(["queue", "list", "-s", "proposed"]).status == "proposed"


def test_short_queue_approve():
    args = _parse(["queue", "approve", "-a", "-f"])
    assert args.all is True
    assert args.force is True


def test_short_queue_cancel_all():
    assert _parse(["queue", "cancel", "-a"]).all is True


def test_cli_bare_enrich_dispatches_run(tmp_path, monkeypatch):
    import theke
    seen = {}
    def fake_run(conn, cfg, args):
        seen["cmd"] = args.enrich_cmd
        return {"enriched": 0}
    monkeypatch.setattr(theke, "_enrich_run", fake_run)
    db = str(tmp_path / "t.db")
    assert main(["--db", db, "enrich"]) == 0
    assert seen["cmd"] == "run"


# -- fetch: conversions -----------------------------------------------------

def test_film_id_matches_utf16le_sha256_spec():
    # sha256("ARDTatorthttp://v/1.mp4http://w/1".encode("utf-16-le"))
    assert film_id("ARD", "Tatort", "http://v/1.mp4", "http://w/1") == \
        "e149d5775b9318a7f9f8b5d731c28f8f027443f1c010999ca0d19f9393afdf70"


def test_film_id_is_order_sensitive():
    assert film_id("a", "b", "c", "d") != film_id("b", "a", "c", "d")


def test_decode_rel_url_relative():
    # first 20 chars of base ("http://example.com/p") + the suffix
    assert decode_rel_url("http://example.com/path/video.mp4", "20|small.mp4") \
        == "http://example.com/psmall.mp4"


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
    # epoch 1700000000 as UTC wall clock
    assert parse_date("", "", "1700000000") == "2023-11-14 22:13:20"


def test_parse_date_all_empty_is_none():
    assert parse_date("", "", "") is None


def test_parse_date_garbage_is_none():
    assert parse_date("not-a-date", "xx", "nope") is None


# -- fetch: parse_filmliste -------------------------------------------------

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
    rows = [make_x(sender="ARD", thema="Sport", url="u", website="w"),
            make_x(url="u", website="w")]            # inherits ARD / Sport
    _, films = parse(mv_text(["", "", "", "", ""], rows))
    # id of the second film == sha256("ARDSportuw".encode("utf-16-le"))
    assert films[1]["mediathek_id"] == \
        "693ce7b99d4ac82947edbc4f97530825b8eddf69c13f81828388abaafb8250a1"


def test_parse_status_always_unenriched():
    # mirror marks every row status '0' (unenriched); the source 'neu' flag no
    # longer drives status -- enrich flips it to '1'.
    rows = [make_x(titel="new", neu="true"),
            make_x(titel="old", neu="false"),
            make_x(titel="blank")]
    _, films = parse(mv_text(["", "", "", "", ""], rows))
    assert [f["status"] for f in films] == ["0", "0", "0"]


def test_parse_decodes_hd_and_small_urls():
    row = make_x(url="http://example.com/path/video.mp4", url_hd="20|hd.mp4",
                 url_klein="")
    _, films = parse(mv_text(["", "", "", "", ""], [row]))
    # first 20 chars of the base url ("http://example.com/p") + the suffix
    assert films[0]["url_video_hd"] == "http://example.com/phd.mp4"
    assert films[0]["url_video_small"] == ""


def test_parse_large_entry_across_chunk_boundary():
    big = "X" * 5000  # one piece of test data, used as input and expectation
    row = make_x(sender="ARD", titel=big, url="u")
    _, films = parse(mv_text(["", "", "", "", ""], [row]), chunk_size=8)
    assert films[0]["title"] == big
    assert films[0]["sender"] == "ARD"


def test_parse_metadata_only_no_films():
    meta, films = parse(mv_text(["", "x", "", "", "theid"], []))
    assert meta["id"] == "theid"
    assert films == []


# -- fetch: migration + full import -----------------------------------------

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
        assert user_version(conn) == 5   # phase 2 + phase 3 cols + rename + phase 5 queue
    finally:
        conn.close()


QUEUE_COLS = {
    "id", "status", "mediathek_id", "tmdb_id", "name", "language",
    "resolution", "remux", "error", "created_at", "updated_at",
}


def test_migration_creates_queue_table(tmp_path):
    conn = open_db(tmp_path)
    try:
        assert "queue" in table_names(conn)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(queue)")}
        assert cols == QUEUE_COLS
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
        result = import_films(conn, films, meta)
        assert result["imported"] == 2
        rows = film_rows(conn)
        assert [r["title"] for r in rows] == ["A", "B"]
        assert rows[0]["sender"] == "ARD"
        assert rows[0]["status"] == "0"
        assert rows[0]["duration"] == 60
        assert rows[0]["date"] == "2026-06-14 20:15:00"
        assert rows[1]["status"] == "0"   # every mirrored row is unenriched
    finally:
        conn.close()


def test_full_import_is_idempotent(tmp_path):
    conn = open_db(tmp_path)
    try:
        rows = [make_x(sender="ARD", titel="A", url="u"),
                make_x(sender="ZDF", titel="B", url="v")]
        import_films(conn, make_list(rows)[1], make_list(rows)[0])
        meta, films = make_list(rows)
        result = import_films(conn, films, meta)
        assert result["imported"] == 2
        assert len(film_rows(conn)) == 2
    finally:
        conn.close()


def test_full_import_updates_changed_film(tmp_path):
    conn = open_db(tmp_path)
    try:
        base = dict(sender="ARD", thema="T", url="u", website="w")
        import_films(conn, *reversed(make_list([make_x(titel="old", **base)])))
        meta, films = make_list([make_x(titel="new", **base)])
        import_films(conn, films, meta)
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
        import_films(conn, films, meta)
        conn.execute("UPDATE mediathek SET tmdb_id='123', imdb_id='tt9', "
                     "language='de', match_confidence=0.9")
        meta, films = make_list([make_x(titel="new", **base)])
        import_films(conn, films, meta)
        row = film_rows(conn)[0]
        assert row["title"] == "new"          # mirror column updated
        assert row["tmdb_id"] == "123"        # phase-3 assignment preserved
        assert row["imdb_id"] == "tt9"
        assert row["language"] == "de"
        assert row["match_confidence"] == 0.9
    finally:
        conn.close()


# A refresh overwrites a row in place (same mediathek_id) only resets status to
# '0' when a enrich-relevant column changed; sender/topic are baked into the id
# so only title/description/duration can actually differ on an overwrite.
_REIMPORT_BASE = dict(sender="ARD", thema="T", titel="A", url="u", website="w",
                      beschreibung="d", dauer="00:01:00")


def test_overwrite_preserves_status_when_only_nonenrich_changes(tmp_path):
    conn = open_db(tmp_path)
    try:
        import_films(conn, *reversed(make_list([make_x(**_REIMPORT_BASE)])))
        conn.execute("UPDATE mediathek SET status='1'")          # enriched
        changed = dict(_REIMPORT_BASE, groesse_mb="999", geo="DE-AT")
        import_films(conn, *reversed(make_list([make_x(**changed)])))
        rows = film_rows(conn)
        assert len(rows) == 1                # same id -> overwrite, not a new row
        assert rows[0]["size_mb"] == 999     # mirror column updated
        assert rows[0]["status"] == "1"      # enrichment preserved
    finally:
        conn.close()


@pytest.mark.parametrize("field,new", [
    ("titel",        "B"),
    ("beschreibung", "other"),
    ("dauer",        "00:02:00"),
])
def test_overwrite_resets_status_when_enrich_column_changes(tmp_path, field, new):
    conn = open_db(tmp_path)
    try:
        import_films(conn, *reversed(make_list([make_x(**_REIMPORT_BASE)])))
        conn.execute("UPDATE mediathek SET status='1'")
        changed = dict(_REIMPORT_BASE, **{field: new})
        import_films(conn, *reversed(make_list([make_x(**changed)])))
        rows = film_rows(conn)
        assert len(rows) == 1                # same id -> overwrite, not a new row
        assert rows[0]["status"] == "0"      # enrich input changed -> re-enrich
    finally:
        conn.close()


def test_overwrite_noop_preserves_status(tmp_path):
    conn = open_db(tmp_path)
    try:
        import_films(conn, *reversed(make_list([make_x(**_REIMPORT_BASE)])))
        conn.execute("UPDATE mediathek SET status='1'")
        import_films(conn, *reversed(make_list([make_x(**_REIMPORT_BASE)])))
        rows = film_rows(conn)
        assert len(rows) == 1
        assert rows[0]["status"] == "1"      # idempotent refresh does not re-enrich
    finally:
        conn.close()


def test_import_keeps_vanished(tmp_path):
    conn = open_db(tmp_path)
    try:
        both = [make_x(sender="ARD", titel="A", url="a"),
                make_x(sender="ZDF", titel="B", url="b")]
        import_films(conn, *reversed(make_list(both)))
        meta, films = make_list([make_x(sender="ARD", titel="A", url="a")])
        import_films(conn, films, meta)                  # B no longer listed
        assert [r["title"] for r in film_rows(conn)] == ["A", "B"]  # B kept
    finally:
        conn.close()


def test_full_import_sets_meta(tmp_path):
    conn = open_db(tmp_path)
    try:
        meta, films = make_list([make_x(sender="ARD", titel="A", url="a")],
                                list_id="abc123", created="02.03.2026, 07:30")
        import_films(conn, films, meta)
        assert db_get_meta(conn, "filmliste_id") == "abc123"
        assert db_get_meta(conn, "filmliste_created") == "02.03.2026, 07:30"
    finally:
        conn.close()


def test_get_meta_missing_key_is_none(tmp_path):
    conn = open_db(tmp_path)
    try:
        assert db_get_meta(conn, "nope") is None
    finally:
        conn.close()


# -- fetch: diff import + update decision ------------------------------------

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
        import_films(conn, *reversed(make_list([make_x(titel="A", **a),
                                               make_x(sender="ZDF", titel="B",
                                                      url="b")])))
        meta, films = make_list([make_x(titel="A2", **a),         # update A
                                 make_x(sender="ARTE", titel="C",  # new
                                        url="c")])
        result = import_films(conn, films, meta)
        assert result["imported"] == 2
        rows = {r["title"] for r in film_rows(conn)}
        assert rows == {"A2", "B", "C"}             # B (not in diff) survives
    finally:
        conn.close()


def test_diff_import_preserves_phase3_ids(tmp_path):
    conn = open_db(tmp_path)
    try:
        a = dict(sender="ARD", thema="T", url="a", website="w")
        import_films(conn, *reversed(make_list([make_x(titel="A", **a)])))
        conn.execute("UPDATE mediathek SET tmdb_id='77'")
        import_films(conn, *reversed(make_list([make_x(titel="A2", **a)])))
        row = film_rows(conn)[0]
        assert row["title"] == "A2"
        assert row["tmdb_id"] == "77"
    finally:
        conn.close()


# -- fetch: cmd_fetch decision (mocked network) ----------------------------

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


def test_cmd_fetch_full_on_empty_db(tmp_path, monkeypatch):
    install_http(monkeypatch, {"FULL": xz_list([make_x(sender="ARD", titel="A",
                                                       url="a")], "id1")})
    conn = open_db(tmp_path)
    try:
        result = cmd_fetch(conn, CFG, args())
        assert result["action"] == "full"
        assert result["imported"] == 1
        assert db_get_meta(conn, "filmliste_id") == "id1"
    finally:
        conn.close()


def test_cmd_fetch_force_redownloads_full(tmp_path, monkeypatch):
    install_http(monkeypatch, {"FULL": xz_list([make_x(sender="ARD", titel="A",
                                                       url="a")], "id1",
                                               recent_created())})
    conn = open_db(tmp_path)
    try:
        cmd_fetch(conn, CFG, args())                 # seed local list
        result = cmd_fetch(conn, CFG, args(force=True))
        assert result["action"] == "full"             # despite fresh local list
    finally:
        conn.close()


def test_cmd_fetch_skip_when_id_unchanged(tmp_path, monkeypatch):
    conn = open_db(tmp_path)
    try:
        import_films(conn, [], {"id": "id1", "erstellt_am": "01.01.2020, 00:00"})
        install_http(monkeypatch, {"ID": b"id1\n"})
        result = cmd_fetch(conn, CFG, args())
        assert result == {"action": "skip"}
    finally:
        conn.close()


def test_cmd_fetch_skip_when_fresh_but_id_unchanged(tmp_path, monkeypatch):
    # Fresh local list (can_use_diff is true), but the server id is unchanged:
    # the id check must skip *before* a diff is fetched. Only "ID" is mocked, so
    # any diff/full download attempt would raise on the unmapped url.
    conn = open_db(tmp_path)
    try:
        import_films(conn, [], {"id": "id1", "erstellt_am": recent_created()})
        install_http(monkeypatch, {"ID": b"id1\n"})
        result = cmd_fetch(conn, CFG, args())
        assert result == {"action": "skip"}
    finally:
        conn.close()


def test_cmd_fetch_full_when_id_changed(tmp_path, monkeypatch):
    conn = open_db(tmp_path)
    try:
        import_films(conn, [], {"id": "id1", "erstellt_am": "01.01.2020, 00:00"})
        install_http(monkeypatch, {"ID": b"id2",
                                   "FULL": xz_list([make_x(sender="ARD",
                                                           titel="A", url="a")],
                                                   "id2")})
        result = cmd_fetch(conn, CFG, args())
        assert result["action"] == "full"
        assert db_get_meta(conn, "filmliste_id") == "id2"
    finally:
        conn.close()


def test_cmd_fetch_full_when_id_unreachable(tmp_path, monkeypatch):
    conn = open_db(tmp_path)
    try:
        import_films(conn, [], {"id": "id1", "erstellt_am": "01.01.2020, 00:00"})
        install_http(monkeypatch, {"ID": RuntimeError("boom"),
                                   "FULL": xz_list([make_x(sender="ARD",
                                                           titel="A", url="a")],
                                                   "id2")})
        result = cmd_fetch(conn, CFG, args())
        assert result["action"] == "full"
    finally:
        conn.close()


def test_cmd_fetch_diff_when_fresh(tmp_path, monkeypatch):
    conn = open_db(tmp_path)
    try:
        import_films(conn, *reversed(make_list([make_x(sender="ARD", titel="A",
                                                      url="a")], created="x")))
        db_set_meta(conn, "filmliste_created", recent_created())
        install_http(monkeypatch, {"ID": b"id2",  # differs from stored -> no skip
                                    "DIFF": xz_list([make_x(sender="ZDF",
                                                           titel="B", url="b")],
                                                   "id2", recent_created())})
        result = cmd_fetch(conn, CFG, args())
        assert result["action"] == "diff"
        assert result["imported"] == 1
        assert {r["title"] for r in film_rows(conn)} == {"A", "B"}
    finally:
        conn.close()


def test_cmd_fetch_empty_diff_falls_back_to_full(tmp_path, monkeypatch):
    conn = open_db(tmp_path)
    try:
        import_films(conn, *reversed(make_list([make_x(sender="ARD", titel="A",
                                                      url="a")], created="x")))
        db_set_meta(conn, "filmliste_created", recent_created())
        install_http(monkeypatch, {
            "ID": b"id2",  # differs from stored -> no skip
            "DIFF": xz_list([], "id2", recent_created()),
            "FULL": xz_list([make_x(sender="ARD", titel="A", url="a"),
                             make_x(sender="ZDF", titel="B", url="b")], "id2"),
        })
        result = cmd_fetch(conn, CFG, args())
        assert result["action"] == "full"
        assert result["imported"] == 2
    finally:
        conn.close()


def test_cmd_fetch_reports_progress(tmp_path, monkeypatch, caplog):
    install_http(monkeypatch, {"FULL": xz_list([make_x(sender="ARD", titel="A",
                                                       url="a")], "id1")})
    conn = open_db(tmp_path)
    try:
        with caplog.at_level(logging.INFO, logger="theke"):
            cmd_fetch(conn, CFG, args())
    finally:
        conn.close()
    assert any("download" in m for m in caplog.messages)  # download phase logged
    assert any("import" in m for m in caplog.messages)    # import phase logged


# -- fetch: theke fetch CLI end to end --------------------------------------

def one_film(list_id="id1", created="01.01.2020, 00:00"):
    return xz_list([make_x(sender="ARD", titel="A", url="a")], list_id, created)


def test_cli_fetch_full_json(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "t.db")
    install_http(monkeypatch, {Config().filmliste_url: one_film()})
    assert main(["--json", "--db", db, "fetch"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["action"] == "full"
    assert result["imported"] == 1


def test_cli_fetch_progress_goes_to_stderr_not_stdout(tmp_path, capsys, monkeypatch):
    # The --json contract: stdout is exactly one JSON object; progress (the work
    # visible during the ~30 s) must land on stderr instead.
    db = str(tmp_path / "t.db")
    install_http(monkeypatch, {Config().filmliste_url: one_film()})
    assert main(["--json", "--db", db, "fetch"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["action"] == "full"  # one parseable object
    assert captured.out.strip().count("\n") == 0          # ... and only that
    assert "-> downloading" in captured.err               # progress on stderr


def test_cli_fetch_human_output(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "t.db")
    install_http(monkeypatch, {Config().filmliste_url: one_film()})
    assert main(["--db", db, "fetch"]) == 0
    assert "action = full" in capsys.readouterr().out


def test_cli_fetch_force_redownloads(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "t.db")
    # fresh local list: without --force the next run would attempt a diff (whose
    # URL is not mocked); --force must take the full path instead.
    install_http(monkeypatch, {Config().filmliste_url: one_film(
        created=recent_created())})
    assert main(["--db", db, "fetch"]) == 0
    capsys.readouterr()
    assert main(["--json", "--db", db, "fetch", "--force"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "full"


def test_cli_fetch_skip_on_unchanged_id(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "t.db")
    install_http(monkeypatch, {Config().filmliste_url: one_film(list_id="id1")})
    assert main(["--db", db, "fetch"]) == 0          # full, stores id1
    capsys.readouterr()
    install_http(monkeypatch, {Config().filmliste_id_url: b"id1\n"})
    assert main(["--json", "--db", db, "fetch"]) == 0
    assert json.loads(capsys.readouterr().out) == {"action": "skip"}


def test_cli_fetch_locked_db_exits_3(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "t.db")
    install_http(monkeypatch, {Config().filmliste_url: one_film()})
    conn = db_connect(db, migrations=[])
    try:
        assert main(["--json", "--db", db, "fetch"]) == 3
    finally:
        conn.close()
