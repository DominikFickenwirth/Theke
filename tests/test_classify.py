"""Tests for the classify stage (phase 3, part 1): metadata extraction."""

import json
from types import SimpleNamespace

import pytest

from theke import *
from theke import _build_show_where
from theke.classify import classify, CLASSIFY_COLS


# -- helpers -----------------------------------------------------------------

def user_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def column_names(conn, table="mediathek"):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


# columns the classify migrations add (language already exists from phase 2);
# genre/slot land in the phase-3 part-2 migration (schema version 3).
NEW_COLS = {
    "clean_title", "series_name", "season", "episode", "episode_count",
    "category", "year", "country", "flags", "classify_confidence",
    "genre", "slot",
}


# -- migration ---------------------------------------------------------------

def test_classify_migration_adds_columns_on_fresh_db(tmp_path):
    conn = db_connect(str(tmp_path / "theke.db"))  # real MIGRATIONS
    try:
        assert user_version(conn) == 3               # phase 2 + phase 3 (two steps)
        assert NEW_COLS <= column_names(conn)
    finally:
        conn.close()


def test_classify_migration_upgrades_v1_db(tmp_path):
    db = str(tmp_path / "theke.db")
    db_connect(db, migrations=MIGRATIONS[:1]).close()   # stop at phase-2 schema
    conn = db_connect(db)                               # apply phase-3 migrations
    try:
        assert user_version(conn) == 3
        cols = column_names(conn)
        assert NEW_COLS <= cols
        # a row written under v1 has the new columns, all NULL
        conn.execute("INSERT INTO mediathek (status, mediathek_id) VALUES ('0','x')")
        row = conn.execute("SELECT * FROM mediathek").fetchone()
        for col in NEW_COLS:
            assert row[col] is None
    finally:
        conn.close()


def test_classify_migration_upgrades_v2_db_adds_genre_slot(tmp_path):
    db = str(tmp_path / "theke.db")
    db_connect(db, migrations=MIGRATIONS[:2]).close()   # stop at phase-3 part-1
    conn = db_connect(db)                               # apply the genre/slot step
    try:
        assert user_version(conn) == 3
        assert {"genre", "slot"} <= column_names(conn)
        conn.execute("INSERT INTO mediathek (status, mediathek_id) VALUES ('0','x')")
        row = conn.execute("SELECT * FROM mediathek").fetchone()
        assert row["genre"] is None and row["slot"] is None
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


def test_metazeile_rejected_when_country_is_a_sentence_fragment():
    # B8: META matches a CATWORD inside running description text, but the country
    # slot is a sentence fragment ("ueber den ... aus dem Jahr"), not a country.
    # The country-shape filter rejects the whole metazeile.
    r = classify("ARD", "Wetter", "Wetter heute",
                 "Eine Reportage über den Klimawandel aus dem Jahr 2019.", 2700)
    assert r["category"] == "unklar"   # falls back to the duration prior
    assert r["country"] is None
    assert r["year"] is None
    assert r["classify_confidence"] == 0.2


def test_metazeile_rejected_when_country_is_a_broadcast_date():
    # B6: "<...> Magazin vom <Datum>" must not turn the date into country/year.
    r = classify("3Sat", "Slowenien Magazin",
                 "Slowenien Magazin vom 21. September 2023", "", 1500)
    assert r["category"] == "Beitrag/Episode"   # duration prior, not "Magazin"
    assert r["country"] is None
    assert r["year"] is None


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
            "genre_pct": 0.0, "slot_pct": 0.0, "events_pct": 0.0,
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
            "genre_pct": 0.0, "slot_pct": 0.0, "events_pct": 0.0,
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


def test_report_counts_genre_slot_events(tmp_path):
    conn = open_db(tmp_path)
    try:
        # four ARD rows: one with a genre, one with a slot, one an Events
        # category, one plain -> each new column at 25 %.
        insert_classified(conn, "a", genre="Natur", category="unklar",
                          classify_confidence=0.2, flags="")
        insert_classified(conn, "b", slot="hr Retro", category="unklar",
                          classify_confidence=0.2, flags="")
        insert_classified(conn, "c", series_name="Berlinale", category="Events",
                          classify_confidence=0.8, flags="")
        insert_classified(conn, "d", category="unklar", classify_confidence=0.2, flags="")
        st = classify_report(conn, live=False, min_rows=1)["ARD"]
        assert st["genre_pct"] == 25.0
        assert st["slot_pct"] == 25.0
        assert st["events_pct"] == 25.0
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


def test_report_diff_reports_per_field_churn(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", sender="ARD", topic="x", title="Der Fall",
                   description="Spielfilm Deutschland 2003.", duration=5400)
        # pretend an older classify() run stored a different category/year; the
        # other columns already match what classify() produces today
        conn.execute("UPDATE mediathek SET category='OldCat', year=1999, "
                     "clean_title='Der Fall', series_name='x', country='Deutschland', "
                     "classify_confidence=0.9, language='de', flags='' "
                     "WHERE mediathek_id='a'")
        ard = classify_report_diff(conn)["ARD"]
        assert ard["category"]["changed"] == 1
        assert ard["year"]["changed"] == 1
        assert ard["category"]["samples"][0] == {
            "id": "a", "before": "OldCat", "after": "Spielfilm"}
        assert ard["year"]["samples"][0] == {"id": "a", "before": 1999, "after": 2003}
        assert "clean_title" not in ard   # unchanged fields are omitted
        # read-only: stored columns are untouched
        assert conn.execute("SELECT category FROM mediathek WHERE mediathek_id='a'"
                            ).fetchone()["category"] == "OldCat"
    finally:
        conn.close()


def test_report_diff_senders_with_no_churn_are_omitted(tmp_path):
    conn = open_db(tmp_path)
    try:
        # stored columns already equal a live classify() pass -> no churn
        insert_row(conn, "a", sender="ARD", topic="x", title="A", duration=60)
        conn.execute("UPDATE mediathek SET category='Clip', series_name='x', "
                     "clean_title='A', language='de', flags='', "
                     "classify_confidence=0.5 WHERE mediathek_id='a'")  # Clip -> conf 0.5
        assert classify_report_diff(conn) == {}
    finally:
        conn.close()


def test_cli_classify_report_diff_json(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_row(conn, "a", sender="ARD", topic="x", title="Der Fall",
                   description="Spielfilm Deutschland 2003.", duration=5400)
        conn.execute("UPDATE mediathek SET category='OldCat', year=1999, "
                     "clean_title='Der Fall', series_name='x', country='Deutschland', "
                     "classify_confidence=0.9, language='de', flags='' "
                     "WHERE mediathek_id='a'")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "classify", "report", "--diff"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "diff"
    assert out["senders"]["ARD"]["category"]["changed"] == 1


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


# -- classify audit (read-only findings scan) --------------------------------

def test_audit_bare_topic(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", sender="ARD", topic="Spielfilm", title="X")
        insert_row(conn, "b", sender="ARD", topic="Tatort", title="Y")   # real series
        res = classify_audit(conn, checks=["bare-topic"])
        assert res["ARD"]["bare-topic"]["count"] == 1
        assert res["ARD"]["bare-topic"]["examples"] == ["Spielfilm"]
    finally:
        conn.close()


def test_audit_topic_pipe(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", sender="HR", topic="hr Retro | Geschichte", title="X")
        res = classify_audit(conn, checks=["topic-pipe"])
        assert res["HR"]["topic-pipe"]["count"] == 1
        assert res["HR"]["topic-pipe"]["examples"] == ["hr Retro | Geschichte"]
    finally:
        conn.close()


def test_audit_topic_marker(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", sender="ORF", topic="ZIB (mit Gebärdensprache)", title="X")
        res = classify_audit(conn, checks=["topic-marker"])
        assert res["ORF"]["topic-marker"]["count"] == 1
        assert res["ORF"]["topic-marker"]["examples"] == ["ZIB (mit Gebärdensprache)"]
    finally:
        conn.close()


def test_audit_case_variants(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", sender="3Sat", topic="nano", title="X")
        insert_row(conn, "b", sender="3Sat", topic="NANO", title="Y")
        res = classify_audit(conn, checks=["case-variants"])
        assert res["3Sat"]["case-variants"]["count"] == 2          # both rows
        assert res["3Sat"]["case-variants"]["examples"] == ["NANO/nano"]
    finally:
        conn.close()


def test_audit_country_shape(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_classified(conn, "a", sender="ZDF", country="vom 3. Mai")   # date residue
        insert_classified(conn, "b", sender="ZDF", country="Deutschland")  # real country
        res = classify_audit(conn, checks=["country-shape"])
        assert res["ZDF"]["country-shape"]["count"] == 1
        assert res["ZDF"]["country-shape"]["examples"] == ["vom 3. Mai"]
    finally:
        conn.close()


def test_audit_title_credit(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_classified(conn, "a", sender="3Sat",
                          clean_title="Der Wald - Film von Hans Meiser")
        insert_classified(conn, "b", sender="3Sat", clean_title="Der Wald")
        res = classify_audit(conn, checks=["title-credit"])
        assert res["3Sat"]["title-credit"]["count"] == 1
        assert res["3Sat"]["title-credit"]["examples"] == ["Der Wald - Film von Hans Meiser"]
    finally:
        conn.close()


def test_audit_episodic_unparsed(tmp_path):
    conn = open_db(tmp_path)
    try:
        # raw, unclassified -> season/episode are NULL but the title looks episodic
        insert_row(conn, "a", sender="HR", topic="Die Reise", title="Die Reise, Folge 3")
        res = classify_audit(conn, checks=["episodic-unparsed"])
        assert res["HR"]["episodic-unparsed"]["count"] == 1
        assert res["HR"]["episodic-unparsed"]["examples"] == ["Die Reise, Folge 3"]
    finally:
        conn.close()


def test_audit_limit_caps_examples(tmp_path):
    conn = open_db(tmp_path)
    try:
        for i, w in enumerate(("Spielfilm", "Drama", "Krimi")):
            insert_row(conn, str(i), sender="ARD", topic=w, title="X")
        res = classify_audit(conn, checks=["bare-topic"], limit=2)
        assert res["ARD"]["bare-topic"]["count"] == 3          # all counted
        assert len(res["ARD"]["bare-topic"]["examples"]) == 2  # examples capped
    finally:
        conn.close()


def test_audit_unknown_check_raises(tmp_path):
    conn = open_db(tmp_path)
    try:
        with pytest.raises(Exception):
            classify_audit(conn, checks=["nope"])
    finally:
        conn.close()


def test_cli_audit_json(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_row(conn, "a", sender="ARD", topic="Spielfilm", title="X")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "classify", "audit", "--check", "bare-topic"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["senders"]["ARD"]["bare-topic"]["count"] == 1


def test_cli_audit_unknown_check_exits_1(tmp_path):
    db = str(tmp_path / "theke.db")
    db_connect(db).close()
    assert main(["--db", db, "classify", "audit", "--check", "nope"]) == 1


# -- classify show (read-only row sampler, structured filters) ----------------

def show_args(sender=None, like=None, eq=None, null=None, not_null=None,
              min_conf=None, max_conf=None, limit=20):
    return SimpleNamespace(classify_cmd="show", sender=sender, like=like, eq=eq,
                           null=null, not_null=not_null, min_conf=min_conf,
                           max_conf=max_conf, limit=limit)


def test_show_like_filter(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_classified(conn, "a", clean_title="Der Wald - Film von Hans")
        insert_classified(conn, "b", clean_title="Der Wald")
        where, params = _build_show_where(conn, show_args(like=[["clean_title", "%Film von %"]]))
        rows = classify_show(conn, where, params, 20)
        assert [r["mediathek_id"] for r in rows] == ["a"]
        assert rows[0]["clean_title"] == "Der Wald - Film von Hans"
    finally:
        conn.close()


def test_show_eq_with_sender_filter(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_classified(conn, "a", sender="ARD", category="Spielfilm")
        insert_classified(conn, "b", sender="ZDF", category="Spielfilm")
        insert_classified(conn, "c", sender="ARD", category="Doku")
        where, params = _build_show_where(conn, show_args(sender="ARD", eq=[["category", "Spielfilm"]]))
        rows = classify_show(conn, where, params, 20)
        assert [r["mediathek_id"] for r in rows] == ["a"]
    finally:
        conn.close()


def test_show_null_and_not_null(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_classified(conn, "a", year=2003)
        insert_classified(conn, "b")            # year stays NULL
        nullrows = classify_show(conn, *_build_show_where(conn, show_args(null=["year"])), 20)
        notnull = classify_show(conn, *_build_show_where(conn, show_args(not_null=["year"])), 20)
        assert [r["mediathek_id"] for r in nullrows] == ["b"]
        assert [r["mediathek_id"] for r in notnull] == ["a"]
    finally:
        conn.close()


def test_show_min_conf(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_classified(conn, "a", classify_confidence=0.9)
        insert_classified(conn, "b", classify_confidence=0.2)
        rows = classify_show(conn, *_build_show_where(conn, show_args(min_conf=0.5)), 20)
        assert [r["mediathek_id"] for r in rows] == ["a"]
    finally:
        conn.close()


def test_show_limit_caps_rows(tmp_path):
    conn = open_db(tmp_path)
    try:
        for i in range(3):
            insert_classified(conn, str(i), category="Spielfilm")
        rows = classify_show(conn, *_build_show_where(conn, show_args()), 2)
        assert len(rows) == 2
    finally:
        conn.close()


def test_show_unknown_field_raises(tmp_path):
    conn = open_db(tmp_path)
    try:
        with pytest.raises(Exception):
            _build_show_where(conn, show_args(like=[["nope", "x"]]))
    finally:
        conn.close()


def test_cli_show_json(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_classified(conn, "a", sender="3Sat", clean_title="Der Wald - Film von Hans")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "classify", "show",
                 "--like", "clean_title", "%Film von %"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["mediathek_id"] for r in out["rows"]] == ["a"]


def test_cli_show_unknown_field_exits_1(tmp_path):
    db = str(tmp_path / "theke.db")
    db_connect(db).close()
    assert main(["--db", db, "classify", "show", "--null", "nope"]) == 1


# -- classify dist (read-only field value distribution) ----------------------

def test_dist_counts_values_descending(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_classified(conn, "a", category="Spielfilm")
        insert_classified(conn, "b", category="Spielfilm")
        insert_classified(conn, "c", category="Doku")
        assert classify_dist(conn, "category") == [("Spielfilm", 2), ("Doku", 1)]
    finally:
        conn.close()


def test_dist_sender_filter(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_classified(conn, "a", sender="ARD", category="Spielfilm")
        insert_classified(conn, "b", sender="ZDF", category="Spielfilm")
        assert classify_dist(conn, "category", senders=["ARD"]) == [("Spielfilm", 1)]
    finally:
        conn.close()


def test_dist_limit_caps_entries(tmp_path):
    conn = open_db(tmp_path)
    try:
        for i, c in enumerate(("Spielfilm", "Doku", "Krimi")):
            insert_classified(conn, str(i), category=c)
        assert len(classify_dist(conn, "category", limit=2)) == 2
    finally:
        conn.close()


def test_dist_unknown_field_raises(tmp_path):
    conn = open_db(tmp_path)
    try:
        with pytest.raises(Exception):
            classify_dist(conn, "nope")
    finally:
        conn.close()


def test_cli_dist_json(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_classified(conn, "a", sender="ARD", category="Spielfilm")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "classify", "dist", "--field", "category"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["field"] == "category"
    assert out["values"] == [["Spielfilm", 1]]   # tuples serialize to JSON arrays


def test_cli_dist_unknown_field_exits_1(tmp_path):
    db = str(tmp_path / "theke.db")
    db_connect(db).close()
    assert main(["--db", db, "classify", "dist", "--field", "nope"]) == 1
