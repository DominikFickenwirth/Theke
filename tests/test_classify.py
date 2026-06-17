"""Tests for the classify stage (phase 3, part 1): metadata extraction."""

import json
from types import SimpleNamespace

import pytest

from theke import *
from theke.classify import classify, CLASSIFY_COLS


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


# -- pure classify(): expected values are hand-derived from the sender ---------
# broadcaster conventions, not produced by the extractor.

def test_classify_returns_exactly_the_classify_columns():
    r = classify("ARD", "Tatort", "Der Fall", "", 5400)
    assert set(r) == set(CLASSIFY_COLS)


def test_ard_metazeile_in_description():
    # ARD school: "<Kategorie> <Land> <Jahr>" prefix in the description, no comma.
    r = classify("ARD", "Filmmittwoch im Ersten", "Der Fall",
                 "Spielfilm Deutschland/USA 2003 Ein spannender Kriminalfall.", 5400)
    assert r["category"] == "Spielfilm"
    assert r["year"] == 2003
    assert r["country"] == "Deutschland/USA"
    assert r["clean_title"] == "Der Fall"
    assert r["series_name"] == "Filmmittwoch im Ersten"
    assert r["language"] == "de"
    assert r["flags"] == ""
    assert r["season"] is None and r["episode"] is None
    assert r["classify_confidence"] == 0.9


def test_four_digit_season_is_broadcast_year_not_a_season():
    # "(S2025/E221)" on a daily show: 2025 is the year, E the running number.
    r = classify("ZDF", "heute", "heute 19:00 Uhr (S2025/E221)", "", 900)
    assert r["year"] == 2025
    assert r["season"] is None
    assert r["episode"] is None
    assert r["clean_title"] == "heute 19:00 Uhr"


def test_real_season_episode():
    r = classify("ZDFinfo", "Insider", "Die Story (S02/E06)", "", 1500)
    assert r["season"] == 2
    assert r["episode"] == 6
    assert r["clean_title"] == "Die Story"
    assert r["year"] is None


def test_3sat_title_metazeile_with_director_prefix():
    # 3Sat school: metazeile in the title after " - ", with comma; the country
    # sits after the last comma ("von <Regisseur>, <Land> <Jahr>").
    r = classify("3Sat", "Dokumentarfilm",
                 "Der Lauf der Dinge - Dokumentarfilm von Regina Schilling, Deutschland 2023",
                 "", 5400)
    assert r["category"] == "Dokumentarfilm"
    assert r["year"] == 2023
    assert r["country"] == "Deutschland"
    assert r["clean_title"] == "Der Lauf der Dinge"
    assert r["classify_confidence"] == 0.9


def test_mehrteiler_part_of_total():
    # "(1/2)" = part 1 of 2 (not a real season/episode).
    r = classify("ARD", "Reihe", "Der große Sturm (1/2)", "", 5400)
    assert r["episode"] == 1
    assert r["episode_count"] == 2
    assert r["season"] is None
    assert r["clean_title"] == "Der große Sturm"


def test_kika_leading_episode_number():
    r = classify("KiKA", "Schafe", "4. Die Schafe sind los", "", 600)
    assert r["episode"] == 4
    assert r["clean_title"] == "Die Schafe sind los"


def test_srf_form_b_season_episode():
    r = classify("SRF", "Tatort", "Blutgeld (Staffel 2, Folge 1)", "", 5400)
    assert r["season"] == 2
    assert r["episode"] == 1
    assert r["clean_title"] == "Blutgeld"


def test_flag_audio_description():
    r = classify("ARD", "Tatort", "Tatort (Audiodeskription)", "", 5400)
    assert r["flags"] == "A"
    assert r["clean_title"] == "Tatort"


def test_flag_sign_language_both_spellings():
    ard = classify("ARD", "Tagesschau", "Tagesschau (Gebärdensprache)", "", 900)
    orf = classify("ORF", "ZIB", "Zeit im Bild (ÖGS)", "", 900)
    assert ard["flags"] == "S"
    assert orf["flags"] == "S"


def test_flag_burned_in_subtitles():
    r = classify("ARD", "Film", "Der Film (mit Untertitel)", "", 5400)
    assert r["flags"] == "U"


def test_flag_trailer_from_topic():
    r = classify("ZDF", "Vorschau", "Der Schwarm", "", 60)
    assert r["flags"] == "T"
    assert r["clean_title"] == "Der Schwarm"


def test_flags_combination_is_alphabetical():
    r = classify("ARD", "Film", "Der Film (Audiodeskription) (mit Untertitel)", "", 5400)
    assert r["flags"] == "AU"


def test_language_original_version():
    r = classify("ARD", "Film", "Le Havre (Originalversion)", "", 5400)
    assert r["language"] == "ov"
    assert r["clean_title"] == "Le Havre"


def test_language_english_marker():
    r = classify("ARD", "Film", "London Calling (engl.)", "", 3600)
    assert r["language"] == "en"
    assert r["clean_title"] == "London Calling"


def test_arte_topic_taxonomy_category():
    # ARTE.DE: genre comes from the two-level topic taxonomy, not a metazeile;
    # series_name stays empty (topic is a genre, not a show name).
    r = classify("ARTE.DE", "Kino - Filme", "Le Havre", "", 5400)
    assert r["category"] == "Spielfilm"
    assert r["language"] == "de"
    assert r["series_name"] is None
    assert r["clean_title"] == "Le Havre"
    assert r["classify_confidence"] == 0.9


def test_unklar_when_no_category_signal():
    # Long non-fiction without any metazeile/taxonomy -> honest low-confidence.
    r = classify("ARD", "Hallo Niedersachsen", "Hallo Niedersachsen vom 14.06.",
                 "Aktuelle Nachrichten aus der Region.", 2700)
    assert r["category"] == "unklar"
    assert r["classify_confidence"] == 0.2


# -- cmd_classify: DB write side ---------------------------------------------

def open_db(tmp_path):
    return db_connect(str(tmp_path / "theke.db"))


def insert_row(conn, mediathek_id, sender="ARD", topic="", title="",
               description="", duration=0, status="0"):
    conn.execute(
        "INSERT INTO mediathek (status, mediathek_id, sender, topic, title, "
        "description, duration) VALUES (?,?,?,?,?,?,?)",
        (status, mediathek_id, sender, topic, title, description, duration))


def args(force=False):
    return SimpleNamespace(classify_cmd="run", force=force)


def insert_classified(conn, mediathek_id, sender="ARD", **cols):
    """Insert a row with classify columns already set (for --analyze tests)."""
    base = dict(status="1", mediathek_id=mediathek_id, sender=sender, **cols)
    keys = list(base)
    conn.execute(f"INSERT INTO mediathek ({','.join(keys)}) "
                 f"VALUES ({','.join(':' + k for k in keys)})", base)


def test_cmd_classify_fills_columns_and_flips_status(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", sender="ARD", topic="Filmmittwoch im Ersten",
                   title="Der Fall (Audiodeskription)",
                   description="Spielfilm Deutschland 2003 Ein Fall.", duration=5400)
        result = cmd_classify(conn, Config(), args())
        assert result == {"classified": 1}
        row = conn.execute("SELECT * FROM mediathek WHERE mediathek_id='a'").fetchone()
        assert row["status"] == "1"
        assert row["category"] == "Spielfilm"
        assert row["year"] == 2003
        assert row["country"] == "Deutschland"
        assert row["flags"] == "A"
        assert row["clean_title"] == "Der Fall"
        assert row["classify_confidence"] == 0.9
    finally:
        conn.close()


def test_cmd_classify_default_scope_skips_already_classified(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", title="A")
        insert_row(conn, "b", title="B")
        assert cmd_classify(conn, Config(), args())["classified"] == 2
        # both rows are status '1' now -> a second default run does nothing
        assert cmd_classify(conn, Config(), args())["classified"] == 0
    finally:
        conn.close()


def test_cmd_classify_force_reprocesses_all(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", title="A")
        insert_row(conn, "b", title="B")
        cmd_classify(conn, Config(), args())
        assert cmd_classify(conn, Config(), args(force=True))["classified"] == 2
    finally:
        conn.close()


def test_cmd_classify_preserves_phase3_ids(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", sender="ARD", title="Der Fall")
        conn.execute("UPDATE mediathek SET tmdb_id='123', imdb_id='tt9', "
                     "match_confidence=0.8 WHERE mediathek_id='a'")
        cmd_classify(conn, Config(), args())
        row = conn.execute("SELECT * FROM mediathek WHERE mediathek_id='a'").fetchone()
        assert row["tmdb_id"] == "123"
        assert row["imdb_id"] == "tt9"
        assert row["match_confidence"] == 0.8
        assert row["clean_title"] == "Der Fall"   # classify still ran
    finally:
        conn.close()


def test_cli_classify_json_on_stdout_progress_on_stderr(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_row(conn, "a", title="A")
        insert_row(conn, "b", title="B")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "classify", "run"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"classified": 2}   # one parseable object
    assert captured.out.strip().count("\n") == 0           # ... and only that


# -- classify report (stored / --live), read-only ----------------------------

def test_analyze_report_from_stored_columns(tmp_path):
    conn = open_db(tmp_path)
    try:
        # two ARD rows: one fully classified film, one unclassified-ish "unklar"
        insert_classified(conn, "a", year=2003, country="Deutschland",
                          category="Spielfilm", classify_confidence=0.9, flags="A")
        insert_classified(conn, "b", category="unklar", classify_confidence=0.2,
                          flags="")
        report = classify_report(conn, live=False, min_rows=1)
        assert report == {"ARD": {
            "n": 2, "year_pct": 50.0, "country_pct": 50.0, "se_pct": 0.0,
            "cat_pct": 50.0, "unklar_pct": 50.0,
            "flag_a_pct": 50.0, "flag_s_pct": 0.0, "flag_u_pct": 0.0, "flag_t_pct": 0.0,
        }}
    finally:
        conn.close()


def test_dry_run_report_is_live_and_writes_nothing(tmp_path):
    conn = open_db(tmp_path)
    try:
        # raw, unclassified rows; classify() runs live over them
        insert_row(conn, "a", sender="ARD", topic="Filmmittwoch im Ersten",
                   title="Der Fall", description="Spielfilm Deutschland 2003 Ein Fall.",
                   duration=5400)                     # -> year+country+Spielfilm, conf 0.9
        insert_row(conn, "b", sender="ARD", topic="heute", title="heute",
                   description="", duration=900)       # -> Beitrag/Episode, conf 0.5
        report = classify_report(conn, live=True, min_rows=1)
        assert report == {"ARD": {
            "n": 2, "year_pct": 50.0, "country_pct": 50.0, "se_pct": 0.0,
            "cat_pct": 50.0, "unklar_pct": 0.0,
            "flag_a_pct": 0.0, "flag_s_pct": 0.0, "flag_u_pct": 0.0, "flag_t_pct": 0.0,
        }}
        # read-only: nothing was written
        rows = conn.execute("SELECT status, category, year, flags FROM mediathek").fetchall()
        for r in rows:
            assert r["status"] == "0"
            assert r["category"] is None
            assert r["year"] is None
            assert r["flags"] is None
    finally:
        conn.close()


def test_report_min_rows_filters_small_senders(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_classified(conn, "a", category="unklar", classify_confidence=0.2, flags="")
        assert classify_report(conn, live=False, min_rows=2) == {}   # only 1 ARD row
    finally:
        conn.close()


def test_report_sender_filter_narrows_to_named_senders(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_classified(conn, "a", sender="ARD", category="unklar",
                          classify_confidence=0.2, flags="")
        insert_classified(conn, "z", sender="ZDF", category="Spielfilm",
                          classify_confidence=0.9, flags="")
        report = classify_report(conn, live=False, min_rows=1, senders=["ZDF"])
        assert set(report) == {"ZDF"}
        assert report["ZDF"]["n"] == 1
    finally:
        conn.close()


def test_report_by_confidence_splits_category_into_per_level_columns(tmp_path):
    conn = open_db(tmp_path)
    try:
        # two ARD rows, one per confidence level (0.9 and 0.5)
        insert_classified(conn, "a", category="Spielfilm", classify_confidence=0.9, flags="")
        insert_classified(conn, "b", category="Beitrag/Episode", classify_confidence=0.5, flags="")
        st = classify_report(conn, live=False, min_rows=1, by_confidence=True)["ARD"]
        assert st["c90_pct"] == 50.0   # 1 of 2 rows at conf 0.9
        assert st["c80_pct"] == 0.0
        assert st["c50_pct"] == 50.0   # 1 of 2 rows at conf 0.5
        assert st["c20_pct"] == 0.0
        # the per-level columns are absent unless requested (stable default shape)
        assert "c90_pct" not in classify_report(conn, live=False, min_rows=1)["ARD"]
    finally:
        conn.close()


def test_cli_classify_report_by_confidence_json(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_classified(conn, "a", sender="ARD", category="Spielfilm",
                          classify_confidence=0.9, flags="")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "classify", "report",
                 "--by-confidence", "--min-rows", "0"]) == 0
    st = json.loads(capsys.readouterr().out)["senders"]["ARD"]
    assert st["c90_pct"] == 100.0


def test_cli_classify_report_json(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_row(conn, "a", title="A")
        insert_row(conn, "b", title="B")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "classify", "report"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "stored"           # below the default min_rows -> empty
    assert out["senders"] == {}


def test_cli_classify_report_live_json(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_row(conn, "a", title="A")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "classify", "report", "--live"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "live"


def test_cli_classify_report_min_rows_zero_shows_small_sender(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_classified(conn, "a", sender="ARD", category="unklar",
                          classify_confidence=0.2, flags="")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "classify", "report", "--min-rows", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["senders"]) == {"ARD"}    # the single-row sender is now visible


def test_cli_classify_requires_a_subcommand(tmp_path):
    db = str(tmp_path / "theke.db")
    db_connect(db).close()
    assert main(["--db", db, "classify"]) == 2  # nested subcommand is required
