"""Tests for the enrich stage (phase 3, part 1): metadata extraction."""

import json
from types import SimpleNamespace

import pytest

from theke import *
from theke import _build_show_where
from theke.enrich import enrich, ENRICH_COLS, FICTION_TOPICS


# -- helpers -----------------------------------------------------------------

def user_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def column_names(conn, table="mediathek"):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


# columns the enrich migrations add (language already exists from phase 2);
# genre/slot land in the phase-3 part-2 migration (schema version 3).
NEW_COLS = {
    "clean_title", "series_name", "season", "episode", "episode_count",
    "category", "year", "country", "flags", "enrich_confidence",
    "genre", "slot",
}


# -- migration ---------------------------------------------------------------

def test_enrich_migration_adds_columns_on_fresh_db(tmp_path):
    conn = db_connect(str(tmp_path / "theke.db"))  # real MIGRATIONS
    try:
        assert user_version(conn) == 10              # ... + phase 9 library (+year/path) + queue.year
        assert NEW_COLS <= column_names(conn)
    finally:
        conn.close()


def test_enrich_migration_upgrades_v1_db(tmp_path):
    db = str(tmp_path / "theke.db")
    db_connect(db, migrations=MIGRATIONS[:1]).close()   # stop at phase-2 schema
    conn = db_connect(db)                               # apply phase-3 migrations
    try:
        assert user_version(conn) == 10
        cols = column_names(conn)
        assert NEW_COLS <= cols
        # a row written under v1 has the new columns, all NULL
        conn.execute("INSERT INTO mediathek (status, mediathek_id) VALUES ('0','x')")
        row = conn.execute("SELECT * FROM mediathek").fetchone()
        for col in NEW_COLS:
            assert row[col] is None
    finally:
        conn.close()


def test_enrich_migration_upgrades_v2_db_adds_genre_slot(tmp_path):
    db = str(tmp_path / "theke.db")
    db_connect(db, migrations=MIGRATIONS[:2]).close()   # stop at phase-3 part-1
    conn = db_connect(db)                               # apply the genre/slot step
    try:
        assert user_version(conn) == 10
        assert {"genre", "slot"} <= column_names(conn)
        conn.execute("INSERT INTO mediathek (status, mediathek_id) VALUES ('0','x')")
        row = conn.execute("SELECT * FROM mediathek").fetchone()
        assert row["genre"] is None and row["slot"] is None
    finally:
        conn.close()


def test_enrich_migration_renames_confidence_column(tmp_path):
    # entry 4 renames classify_confidence -> enrich_confidence, keeping the data.
    db = str(tmp_path / "theke.db")
    conn = db_connect(db, migrations=MIGRATIONS[:3])     # v3: classify_confidence
    conn.execute("INSERT INTO mediathek (status, mediathek_id, classify_confidence) "
                 "VALUES ('1','x',0.9)")
    conn.close()
    conn = db_connect(db)                                # apply entry 4 (the rename)
    try:
        assert user_version(conn) == 10
        cols = column_names(conn)
        assert "enrich_confidence" in cols
        assert "classify_confidence" not in cols
        row = conn.execute("SELECT enrich_confidence FROM mediathek").fetchone()
        assert row["enrich_confidence"] == 0.9          # 0.9 written under the old name
    finally:
        conn.close()


# -- pure enrich(): expected values are hand-derived from the sender ---------
# broadcaster conventions, not produced by the extractor.

def test_enrich_returns_exactly_the_enrich_columns():
    r = enrich("ARD", "Tatort", "Der Fall", "", 5400)
    assert set(r) == set(ENRICH_COLS)


def test_ard_metazeile_in_description():
    # ARD school: "<Kategorie> <Land> <Jahr>" prefix in the description, no comma.
    r = enrich("ARD", "Filmmittwoch im Ersten", "Der Fall",
                 "Spielfilm Deutschland/USA 2003 Ein spannender Kriminalfall.", 5400)
    assert r["category"] == "Movie"        # Spielfilm -> medium Movie
    assert r["genre"] is None              # ... carries no genre
    assert r["year"] == 2003
    assert r["country"] == "Deutschland/USA"
    assert r["clean_title"] == "Der Fall"
    assert r["slot"] == "Filmmittwoch im Ersten"   # a programming strand -> slot
    assert r["series_name"] is None                # ... not a show name (round 11)
    assert r["language"] == "de"
    assert r["flags"] == ""
    assert r["season"] is None and r["episode"] is None
    assert r["enrich_confidence"] == 0.9


def test_four_digit_season_stays_a_season_not_the_year():
    # "(S2025/E221)" on a daily show: the 4-digit season is kept as the season,
    # never written to year (which is the production/release year, not broadcast).
    r = enrich("ZDF", "heute", "heute 19:00 Uhr (S2025/E221)", "", 900)
    assert r["season"] == 2025
    assert r["episode"] == 221
    assert r["year"] is None
    assert r["clean_title"] == "heute 19:00 Uhr"


def test_real_season_episode():
    r = enrich("ZDFinfo", "Insider", "Die Story (S02/E06)", "", 1500)
    assert r["season"] == 2
    assert r["episode"] == 6
    assert r["clean_title"] == "Die Story"
    assert r["year"] is None


def test_3sat_title_metazeile_with_director_prefix():
    # 3Sat school: metazeile in the title after " - ", with comma; the country
    # sits after the last comma ("von <Regisseur>, <Land> <Jahr>").
    r = enrich("3Sat", "Dokumentarfilm",
                 "Der Lauf der Dinge - Dokumentarfilm von Regina Schilling, Deutschland 2023",
                 "", 5400)
    assert r["category"] == "Movie"           # Dokumentarfilm -> Movie ...
    assert r["genre"] == "Documentary"        # ... + Documentary genre
    assert r["year"] == 2023
    assert r["country"] == "Deutschland"
    assert r["clean_title"] == "Der Lauf der Dinge"
    assert r["enrich_confidence"] == 0.9


def test_metazeile_rejected_when_country_is_a_sentence_fragment():
    # B8: META matches a CATWORD inside running description text, but the country
    # slot is a sentence fragment ("ueber den ... aus dem Jahr"), not a country.
    # The country-shape filter rejects the whole metazeile.
    r = enrich("ARD", "Wetter", "Wetter heute",
                 "Eine Reportage über den Klimawandel aus dem Jahr 2019.", 2700)
    assert r["category"] == "Episode"   # 45 min, no metazeile -> duration prior
    assert r["country"] is None
    assert r["year"] is None
    assert r["enrich_confidence"] == 0.5


def test_metazeile_rejected_when_country_is_a_broadcast_date():
    # B6: "<...> Magazin vom <Datum>" must not turn the date into country/year.
    r = enrich("3Sat", "Slowenien Magazin",
                 "Slowenien Magazin vom 21. September 2023", "", 1500)
    assert r["category"] == "Episode"   # duration prior (120-1800s), not "Magazin"
    assert r["country"] is None
    assert r["year"] is None


def test_mehrteiler_part_of_total():
    # "(1/2)" = part 1 of 2 (not a real season/episode).
    r = enrich("ARD", "Reihe", "Der große Sturm (1/2)", "", 5400)
    assert r["episode"] == 1
    assert r["episode_count"] == 2
    assert r["season"] is None
    assert r["clean_title"] == "Der große Sturm"


def test_kika_leading_episode_number():
    r = enrich("KiKA", "Schafe", "4. Die Schafe sind los", "", 600)
    assert r["episode"] == 4
    assert r["clean_title"] == "Die Schafe sind los"


def test_srf_form_b_season_episode():
    r = enrich("SRF", "Tatort", "Blutgeld (Staffel 2, Folge 1)", "", 5400)
    assert r["season"] == 2
    assert r["episode"] == 1
    assert r["clean_title"] == "Blutgeld"


def test_flag_audio_description():
    r = enrich("ARD", "Tatort", "Tatort (Audiodeskription)", "", 5400)
    assert r["flags"] == "A"
    assert r["clean_title"] == "Tatort"


def test_flag_sign_language_both_spellings():
    ard = enrich("ARD", "Tagesschau", "Tagesschau (Gebärdensprache)", "", 900)
    orf = enrich("ORF", "ZIB", "Zeit im Bild (ÖGS)", "", 900)
    assert ard["flags"] == "S"
    assert orf["flags"] == "S"


def test_flag_sign_language_marker_in_topic():
    # B9: the marker sits in the topic ("(mit Gebärdensprache)" / "(ÖGS)"); it
    # must set the flag and be stripped from the series_name, not stored verbatim.
    ard = enrich("ARD", "tagesschau (mit Gebärdensprache)", "Tagesschau", "", 900)
    orf = enrich("ORF", "Zeit im Bild (ÖGS)", "ZIB", "", 900)
    assert ard["flags"] == "S"
    assert ard["series_name"] == "tagesschau"
    assert orf["flags"] == "S"
    assert orf["series_name"] == "Zeit im Bild"


def test_flag_sign_language_suffix_without_parens():
    # B10: " in Gebärdensprache" as a bare suffix (no parens), in title and topic.
    r = enrich("SRF", "Tagesschau in Gebärdensprache",
                 "Tagesschau in Gebärdensprache", "", 900)
    assert r["flags"] == "S"
    assert r["clean_title"] == "Tagesschau"
    assert r["series_name"] == "Tagesschau"


def test_flag_simple_language_suffix():
    # B10: " in Einfacher Sprache" -> new flag E (simple-language edition); the
    # suffix is stripped from the title and the topic (-> series_name).
    r = enrich("tagesschau24", "Tagesschau in Einfacher Sprache",
                 "Tagesschau in Einfacher Sprache", "", 900)
    assert r["flags"] == "E"
    assert r["clean_title"] == "Tagesschau"
    assert r["series_name"] == "Tagesschau"


def test_flag_burned_in_subtitles():
    r = enrich("ARD", "Film", "Der Film (mit Untertitel)", "", 5400)
    assert r["flags"] == "U"


def test_flag_trailer_from_topic():
    r = enrich("ZDF", "Vorschau", "Der Schwarm", "", 60)
    assert r["flags"] == "T"
    assert r["clean_title"] == "Der Schwarm"


def test_flags_combination_is_alphabetical():
    r = enrich("ARD", "Film", "Der Film (Audiodeskription) (mit Untertitel)", "", 5400)
    assert r["flags"] == "AU"


def test_language_original_version():
    r = enrich("ARD", "Film", "Le Havre (Originalversion)", "", 5400)
    assert r["language"] == "ov"
    assert r["clean_title"] == "Le Havre"


def test_language_original_version_with_subtitles():
    # "Originalversion mit Untertitel": spoken language is the original (ov), the
    # subtitles are burned in (U) -- both must be set. The ARTE sender language
    # (here EN) is only the subtitle/UI language and must not stick as spoken.
    r = enrich("ARTE.EN", "Cinema - Films",
                 "Mysteries of Lisbon (Originalversion mit Untertitel)", "", 5400)
    assert r["language"] == "ov"
    assert r["flags"] == "U"
    assert r["clean_title"] == "Mysteries of Lisbon"


def test_burned_in_subtitles_alone_keeps_language():
    # Plain "mit Untertitel" is subtitles only (e.g. for the hard of hearing),
    # not an original-version marker: flag U, but language stays as is.
    r = enrich("ARD", "Film", "Der Film (mit Untertitel)", "", 5400)
    assert r["flags"] == "U"
    assert r["language"] == "de"


def test_language_english_marker():
    r = enrich("ARD", "Film", "London Calling (engl.)", "", 3600)
    assert r["language"] == "en"
    assert r["clean_title"] == "London Calling"


def test_arte_topic_taxonomy_category():
    # ARTE.DE: genre comes from the two-level topic taxonomy, not a metazeile;
    # series_name stays empty (topic is a genre, not a show name).
    r = enrich("ARTE.DE", "Kino - Filme", "Le Havre", "", 5400)
    assert r["category"] == "Movie"        # sub-label "Filme" -> Movie
    assert r["genre"] is None              # super-label "Kino" carries no genre
    assert r["language"] == "de"
    assert r["series_name"] is None
    assert r["clean_title"] == "Le Havre"
    assert r["enrich_confidence"] == 0.9


def test_arte_taxonomy_french():
    # ARTE.FR: the sub-label "Films" -> Spielfilm; series_name stays empty.
    r = enrich("ARTE.FR", "Cinéma - Films", "Le Havre", "", 5400)
    assert r["category"] == "Movie"
    assert r["language"] == "fr"
    assert r["series_name"] is None
    assert r["enrich_confidence"] == 0.9


def test_arte_taxonomy_french_super_label():
    # No medium sub-label, feature-length (>1800s): the duration prior cannot
    # decide a long-form medium -> category stays NULL (a film is protected). The
    # super-label "Histoire" still sets the genre; confidence reflects the NULL.
    r = enrich("ARTE.FR", "Histoire - XXe siècle", "Pompeji", "", 3600)
    assert r["category"] is None
    assert r["genre"] == "Documentary, History"
    assert r["language"] == "fr"
    assert r["enrich_confidence"] == 0.2


def test_arte_taxonomy_english():
    r = enrich("ARTE.EN", "Politics and society - Investigation and reports",
                 "Story", "", 3600)
    assert r["category"] is None         # no sub-label, >1800s -> NULL (prior)
    assert r["genre"] == "News"          # super-label -> News
    assert r["language"] == "en"
    assert r["enrich_confidence"] == 0.2


def test_arte_taxonomy_spanish():
    r = enrich("ARTE.ES", "Cine - Películas", "La pelicula", "", 5400)
    assert r["category"] == "Movie"
    assert r["language"] == "es"


def test_arte_taxonomy_italian():
    r = enrich("ARTE.IT", "Storia - XX° secolo", "Storia", "", 3600)
    assert r["category"] is None
    assert r["genre"] == "Documentary, History"
    assert r["language"] == "it"


def test_arte_taxonomy_polish():
    r = enrich("ARTE.PL", "Kino - Filmy", "Film", "", 5400)
    assert r["category"] == "Movie"
    assert r["language"] == "pl"


def test_unklar_when_no_category_signal():
    # A 45-min daily magazine without any metazeile/taxonomy -> Episode via the
    # duration prior (recurring TV, not a film), at low confidence.
    r = enrich("ARD", "Hallo Niedersachsen", "Hallo Niedersachsen vom 14.06.",
                 "Aktuelle Nachrichten aus der Region.", 2700)
    assert r["category"] == "Episode"
    assert r["enrich_confidence"] == 0.5


# -- B4: trailing "- <Format> von <Name>" credit in the title ----------------

def test_title_credit_trailing_film_von_is_stripped():
    r = enrich("3Sat", "Dokumentarfilm",
                 "Zwischen Acker und Opiumkontrolle - Film von Anja Schlegel", "", 2700)
    assert r["clean_title"] == "Zwischen Acker und Opiumkontrolle"
    assert r["country"] is None and r["year"] is None


def test_title_credit_trailing_reportage_von_is_stripped():
    r = enrich("3Sat", "Reportage",
                 "Vom Türsteher zum Herbergsvater - Reportage von Ralph Alexowitz", "", 2700)
    assert r["clean_title"] == "Vom Türsteher zum Herbergsvater"


def test_title_credit_midtitle_film_von_is_kept():
    # No " - <Format> von" suffix -> not a credit, keep the title verbatim.
    r = enrich("ARD", "Kino+", "Neuer Film von Tobias Obentheuer", "", 600)
    assert r["clean_title"] == "Neuer Film von Tobias Obentheuer"


# -- B5: episode notation without parentheses (guarded) ----------------------

def test_episode_teil_arabic():
    r = enrich("HR", "Ratgeber", "Kleider machen Leute - Teil 2", "", 1500)
    assert r["episode"] == 2
    assert r["clean_title"] == "Kleider machen Leute"


def test_episode_teil_roman():
    r = enrich("ARD", "Reihe", "Der große Krieg - Teil III", "", 3600)
    assert r["episode"] == 3
    assert r["clean_title"] == "Der große Krieg"


def test_episode_staffel_folge_without_parens():
    r = enrich("ARD", "Doku", "Der Fall Staffel 2, Folge 3", "", 3600)
    assert r["season"] == 2
    assert r["episode"] == 3
    assert r["clean_title"] == "Der Fall"


# -- round 8: ", Teil N" comma residue ---------------------------------------

def test_teil_with_leading_comma_and_subtitle_leaves_no_residue():
    # "<Title>, Teil N: <Subtitle>": the ", Teil N" part is removed whole, so no
    # ",:" residue is left (regression: was "Tauchwandern am Bodensee,: Quer ...").
    r = enrich("ARD", "Tauchwandern",
                 "Tauchwandern am Bodensee, Teil 2: Quer durch den Untersee", "", 1500)
    assert r["episode"] == 2
    assert r["clean_title"] == "Tauchwandern am Bodensee: Quer durch den Untersee"


def test_teil_with_leading_comma_at_end_leaves_no_trailing_comma():
    # "<Title>, Teil N" at the very end: no dangling trailing comma.
    r = enrich("SRF", "Jahr", "Jahresrückblick vom 28.12.2023, Teil 4", "", 1500)
    assert r["episode"] == 4
    assert r["clean_title"] == "Jahresrückblick vom 28.12.2023"


def test_staffel_folge_trailing_comma_is_trimmed():
    # "<Title>, Staffel N, Folge M": the STAFFOLGE strip leaves a trailing comma
    # that the final normalization must remove (was "Der Haustier-Check,").
    r = enrich("ZDF", "Der Haustier-Check",
                 "Der Haustier-Check, Staffel 1, Folge 3", "", 1500)
    assert r["season"] == 1
    assert r["episode"] == 3
    assert r["clean_title"] == "Der Haustier-Check"


def test_comma_in_plain_title_is_kept():
    # Guard: an ordinary comma (no Teil/Staffel marker) is part of the title and
    # must survive untouched.
    r = enrich("ARD", "Doku", "Stadt, Land, Fluss", "", 1500)
    assert r["clean_title"] == "Stadt, Land, Fluss"
    assert r["episode"] is None


# -- round 9: leading "Folge/Episode N[/M]: Subtitle" prefix -----------------

def test_folge_prefix_with_se_keeps_se_episode_and_strips_prefix():
    # "Folge <running>: <Subtitle> (Sxx/Exx)": the (S/E) is decisive for the
    # episode number (the leading "Folge 1135" is an absolute running count, not
    # the within-season episode), and the "Folge N:" prefix is stripped to the
    # real episode subtitle.
    r = enrich("MDR", "In aller Freundschaft",
                 "Folge 1135: Flammen der Hoffnung (S29/E05)", "", 2700)
    assert r["season"] == 29
    assert r["episode"] == 5
    assert r["episode_count"] is None
    assert r["clean_title"] == "Flammen der Hoffnung"


def test_folge_prefix_without_se_takes_the_running_number():
    # No (S/E): the leading "Folge N" IS the episode number, and the subtitle
    # becomes the clean_title.
    r = enrich("ARD", "Pop", "Folge 1090: Stargazing von Myles Smith", "", 300)
    assert r["episode"] == 1090
    assert r["clean_title"] == "Stargazing von Myles Smith"


def test_folge_prefix_with_count_sets_episode_count():
    # "Folge N/M: <Subtitle>": N -> episode, M -> episode_count.
    r = enrich("ARD-alpha", "C'est ça, la vie",
                 "Folge 2/26: Philippe, agent immobilier", "", 1500)
    assert r["episode"] == 2
    assert r["episode_count"] == 26
    assert r["clean_title"] == "Philippe, agent immobilier"


def test_folge_prefix_dash_separator():
    # The separator may be " - " (or " | ", " · "), not only ":".
    r = enrich("SWR", "Westweg", "Folge 665 - Auf dem Westweg", "", 1500)
    assert r["episode"] == 665
    assert r["clean_title"] == "Auf dem Westweg"


def test_episode_prefix_without_subtitle_clears_title():
    # A bare "Episode N" (no subtitle) carries no episode title: capture N as the
    # episode and leave clean_title empty (None) -- "Episode 684" is not a title.
    r = enrich("ARD", "Rote Rosen", "Episode 684", "", 2700)
    assert r["episode"] == 684
    assert r["clean_title"] is None


def test_folge_prefix_guard_sonderfolge_untouched():
    # Guard: "Sonderfolge" starts with "S", not "Folge <digit>" -- not a prefix.
    r = enrich("ARD", "Update", "Sonderfolge: Gespräch mit dem Mediziner", "", 1500)
    assert r["episode"] is None
    assert r["clean_title"] == "Sonderfolge: Gespräch mit dem Mediziner"


def test_folge_prefix_guard_verb_and_compound_untouched():
    # Guard: "Folge" as a verb (no number) and "Folger" (a substring) are never
    # treated as the episode prefix.
    r1 = enrich("ARD", "Doku", "Folge der Spur", "", 1500)
    assert r1["episode"] is None and r1["clean_title"] == "Folge der Spur"
    r2 = enrich("ARD", "Talk", 'Anne Folger: "Fußnoten"', "", 1500)
    assert r2["episode"] is None and r2["clean_title"] == 'Anne Folger: "Fußnoten"'


# -- round 10: "Staffel N" extraction ----------------------------------------

def test_staffel_title_suffix_sets_season_and_strips():
    # "<Title> - Staffel N (n/m)": the dash-introduced "Staffel N" is the season
    # and is stripped from the title; the "(n/m)" stays the Mehrteiler episode.
    r = enrich("ARTE.DE", "Séries et fictions - Serien",
                 "Vorstadtweiber - Staffel 2 (10/10)", "", 2700)
    assert r["season"] == 2
    assert r["episode"] == 10
    assert r["episode_count"] == 10
    assert r["clean_title"] == "Vorstadtweiber"


def test_staffel_midtitle_between_dashes_is_removed():
    # "<Title> - Staffel N - <Subtitle>": the season segment is removed from the
    # middle, leaving "<Title> - <Subtitle>".
    r = enrich("ARTE.DE", "Séries et fictions - Serien",
                 "Mord im Mittsommer - Staffel 1 - Fall 1: Tod im Fischernetz", "", 2700)
    assert r["season"] == 1
    assert r["clean_title"] == "Mord im Mittsommer - Fall 1: Tod im Fischernetz"


def test_staffel_without_leading_dash_is_kept():
    # Guard: "Staffel N" with NO dash before it is title content (a review/clip
    # channel), not a season marker -- untouched.
    r = enrich("ARD", "Serienkritik", "BLACK MIRROR Staffel 6: Verdammt gute Ideen", "", 600)
    assert r["season"] is None
    assert r["clean_title"] == "BLACK MIRROR Staffel 6: Verdammt gute Ideen"


def test_orf_topic_trailing_staffel_splits_series_and_season():
    # ORF convention "<Series> Staffel N" in the topic: the season is split off so
    # the series_name groups all seasons together ("Soko Donau", not "... Staffel 20").
    r = enrich("ORF", "Soko Donau Staffel 20", "Tod im Kloster", "", 2700)
    assert r["series_name"] == "Soko Donau"
    assert r["season"] == 20


def test_orf_topic_dash_staffel_leaves_no_dangling_separator():
    # "<Series> - Staffel N": stripping the trailing " Staffel N" must also drop
    # the now-dangling separator (was "TWIN TEAMS -").
    r = enrich("ORF", "TWIN TEAMS - Staffel 1", "Pilot", "", 2700)
    assert r["series_name"] == "TWIN TEAMS"
    assert r["season"] == 1


def test_reversed_staffel_in_series_is_kept():
    # Guard: a reversed "N. Staffel" (digit before "Staffel") at the end is not the
    # "Staffel N" suffix -- series_name and season are untouched.
    r = enrich("BR", "Dahoam is Dahoam - 1. Staffel", "Auftakt", "", 2700)
    assert r["series_name"] == "Dahoam is Dahoam - 1. Staffel"
    assert r["season"] is None


# -- round 11: programming-slot topics -> slot (not series_name) -------------

def test_film_strand_topic_becomes_slot_not_series():
    # A programming strand ("<film-type> im/in <channel>") is a Sendeplatz, not a
    # show: it belongs in slot, and series_name stays empty (the film title is in
    # clean_title).
    r = enrich("MDR", "Kurzfilme im MDR", "Der kurze Film", "", 1500)
    assert r["slot"] == "Kurzfilme im MDR"
    assert r["series_name"] is None


def test_film_strand_slot_keeps_fiction_movie_category():
    # Moving the strand to slot is category-neutral: a strand that is also a
    # fiction Reihe keeps its Movie lift (the lift keys off the raw topic).
    r = enrich("ARD", "Filme im Ersten", "Der Fall", "", 5400)
    assert r["slot"] == "Filme im Ersten"
    assert r["series_name"] is None
    assert r["category"] == "Movie"


def test_real_show_im_ersten_stays_series():
    # Guard: "Nuhr im Ersten" is a comedy SHOW (Dieter Nuhr), not a film strand --
    # the head is not a film-type word, so it stays a series_name.
    r = enrich("ARD", "Nuhr im Ersten", "Ausgabe 5", "", 2700)
    assert r["series_name"] == "Nuhr im Ersten"
    assert r["slot"] is None


def test_im_dritten_reich_is_not_a_slot():
    # Guard: "Kindheit im Dritten Reich" contains "im Dritten" but the placement
    # phrase is not end-anchored ("Reich" follows) and the head is not a film
    # word -- it stays a series_name.
    r = enrich("ARD", "Kindheit im Dritten Reich", "Teil 1", "", 2700)
    assert r["series_name"] == "Kindheit im Dritten Reich"
    assert r["slot"] is None


# -- round 12: Teil marker w/ explicit S/E, and "(Staffel N[, part])" --------

def test_teil_marker_stripped_even_with_explicit_se():
    # A leading "Teil N:" marker must be stripped from the title even when an
    # explicit (Sxx/Exx) already set the episode (the marker strip used to be
    # gated on episode being unset, leaving "Teil 3: ..." residue).
    r = enrich("ARD", "Doku", "Teil 3: Menschliche Knochen (S01/E03)", "", 3600)
    assert r["season"] == 1
    assert r["episode"] == 3
    assert r["clean_title"] == "Menschliche Knochen"


def test_teil_marker_with_count_does_not_add_mismatched_count_under_se():
    # "- Teil 1/2" alongside an explicit (S05/E05): the episode is the S/E one and
    # the marker is only stripped -- no mismatched episode_count from the "Teil 1/2".
    r = enrich("BR", "Doku", "Paul Breitner - Teil 1/2 (S05/E05)", "", 1500)
    assert r["episode"] == 5
    assert r["episode_count"] is None
    assert r["clean_title"] == "Paul Breitner"


def test_paren_staffel_sets_season_and_strips():
    # A trailing "(Staffel N)" parenthetical is a season marker -> set season and
    # remove it from the title.
    r = enrich("SRF", "Deville", "Trailer: Leo da Vinci (Staffel 1)", "", 90)
    assert r["season"] == 1
    assert r["clean_title"] == "Trailer: Leo da Vinci"


def test_paren_staffel_with_inner_part():
    # "(Staffel N, n/m)": season N, episode n, count m -- whole paren removed.
    r = enrich("ARD", "Expedition", "Das Expeditionsteam - Risse und Kanten (Staffel 4, 2/4)", "", 2700)
    assert r["season"] == 4
    assert r["episode"] == 2
    assert r["episode_count"] == 4
    assert r["clean_title"] == "Das Expeditionsteam - Risse und Kanten"


def test_teil_as_word_is_not_a_marker():
    # Guard: "Urteil" / "Teil" without a following number is an ordinary word,
    # never an episode marker.
    r = enrich("ARD", "Nachrichten", "Urteil im Schleuserprozess", "", 1500)
    assert r["episode"] is None
    assert r["clean_title"] == "Urteil im Schleuserprozess"


# -- round 13: "Sender // Series" topic split -------------------------------

def test_double_slash_sender_goes_to_slot():
    # A "<Sender> // <Series>" topic: the sender side is a slot, the series side is
    # the show -- same split as the "|" Dachmarke pipe, but with "//".
    r = enrich("ARD", "NDR//Aktuell", "Meldung", "", 900)
    assert r["slot"] == "NDR"
    assert r["series_name"] == "Aktuell"


def test_double_slash_das_erste_brand():
    # "Das Erste" (the ARD main channel) is recognized as the slot side.
    r = enrich("SR", "Das Erste // Plusminus", "Ausgabe", "", 1500)
    assert r["slot"] == "Das Erste"
    assert r["series_name"] == "Plusminus"


def test_double_slash_neither_side_sender_is_kept_whole():
    # Guard: when neither side is a sender/Dachmarke, do not split -- keep the
    # whole topic as the series_name (title // subtitle).
    r = enrich("ARD", "Pro // Contra", "Debatte", "", 1500)
    assert r["slot"] is None
    assert r["series_name"] == "Pro // Contra"


def test_episode_bare_part_of_total():
    r = enrich("ARD", "Doku", "Die Story 2/2", "", 3600)
    assert r["episode"] == 2
    assert r["episode_count"] == 2
    assert r["clean_title"] == "Die Story"


def test_no_episode_for_time_fraction():
    # "3 1/2 Stunden" is a title, not an episode number.
    r = enrich("ARD", "Doku", "3 1/2 Stunden", "", 3600)
    assert r["episode"] is None and r["episode_count"] is None


def test_no_episode_for_24_7():
    r = enrich("ARD", "Doku", "24/7", "", 3600)
    assert r["episode"] is None


def test_no_episode_for_broadcast_date():
    # ARTE date suffix "dd/mm/yyyy" must not parse as an episode.
    r = enrich("ARTE.FR", "Cinéma", "Invitation au voyage - 10/06/2026", "", 1500)
    assert r["episode"] is None and r["episode_count"] is None


# -- topic routing (B1+B2+B7): format/genre/slot/event vs. real series --------

def test_topic_routing_format_word_sets_category_not_series():
    # A topic that is itself a format word is not a series; it sets the category
    # and leaves series_name empty.
    r = enrich("3Sat", "Spielfilm", "Der Lauf der Dinge", "", 5400)
    assert r["category"] == "Movie"
    assert r["series_name"] is None
    assert r["enrich_confidence"] == 0.8


def test_topic_routing_format_rubric_phrase():
    # Multi-word format rubric -> generic Movie category, no series.
    r = enrich("ARD", "Filme in der ARD", "Tatort", "", 5400)
    assert r["category"] == "Movie"
    assert r["series_name"] is None


def test_topic_routing_genre_word_sets_genre_not_series():
    r = enrich("3Sat", "Natur", "Wildes Skandinavien", "", 2700)
    assert r["genre"] == "Documentary"   # Natur rubric -> Documentary
    assert r["series_name"] is None
    assert r["category"] == "Episode"    # genre is not a medium -> duration prior (45 min)


def test_topic_routing_genre_is_exact_not_substring():
    # "Sport" alone is a rubric; "Sport im Osten" is a real series -> not genre.
    r = enrich("ARD", "Sport im Osten", "Spieltag", "", 2700)
    assert r["genre"] is None
    assert r["series_name"] == "Sport im Osten"


def test_topic_routing_event_sets_events_category():
    r = enrich("3Sat", "Berlinale", "Eröffnungsgala", "", 3600)
    assert r["category"] == "Event"
    assert r["series_name"] == "Berlinale"
    assert r["enrich_confidence"] == 0.8


def test_topic_routing_pipe_dachmarke_front():
    r = enrich("HR", "hr Retro | hessenschau", "Sendung vom Montag", "", 1500)
    assert r["slot"] == "hr Retro"
    assert r["series_name"] == "hessenschau"


def test_topic_routing_pipe_dachmarke_back():
    r = enrich("rbtv", "buten un binnen | regionalmagazin", "Ausgabe", "", 1500)
    assert r["slot"] == "regionalmagazin"
    assert r["series_name"] == "buten un binnen"


def test_topic_routing_pipe_subtitle_is_not_split():
    # Neither side is a Dachmarke -> title|subtitle, keep the whole topic.
    topic = "Der Germanwings-Absturz | Chronologie eines Unglücks"
    r = enrich("ARD", topic, "Doku", "", 3600)
    assert r["slot"] is None
    assert r["series_name"] == topic


def test_topic_routing_container_clip_has_no_series():
    r = enrich("SRF", "Sport-Clip", "Tor des Tages", "", 60)
    assert r["series_name"] is None
    assert r["category"] == "Clip"   # short duration prior


def test_topic_routing_plain_series_unchanged():
    # Regression: an ordinary topic still becomes the series_name verbatim.
    r = enrich("ARD", "Tatort", "Der Fall", "", 5400)
    assert r["series_name"] == "Tatort"
    assert r["genre"] is None and r["slot"] is None


# -- category/genre split: medium (category) vs TMDB genre -------------------
# category in {Movie, Episode, Clip, Event, NULL}; genre is TMDB-only, multiple
# values comma-joined in canonical TMDB order.

def test_genre_word_maps_to_movie_plus_tmdb_genre():
    # A bare fiction genre word as topic -> Movie medium + the TMDB genre.
    r = enrich("ARD", "Krimi", "Der Kommissar", "", 5400)
    assert r["category"] == "Movie"
    assert r["genre"] == "Crime"
    assert r["series_name"] is None


def test_dokumentation_topic_maps_to_episode_documentary():
    r = enrich("3Sat", "Dokumentation", "Die Story", "", 2700)
    assert r["category"] == "Episode"
    assert r["genre"] == "Documentary"


def test_dokumentarfilm_topic_maps_to_movie_documentary():
    # -film words are a film medium even when they carry a genre.
    r = enrich("3Sat", "Dokumentarfilm", "Der Lauf der Dinge", "", 5400)
    assert r["category"] == "Movie"
    assert r["genre"] == "Documentary"


def test_konzert_maps_to_clip_music():
    # ARTE Concert super-label: Clip medium (TV-special) + Music genre.
    r = enrich("ARTE.DE", "ARTE Concert - Jazz", "Live in Berlin", "", 5400)
    assert r["category"] == "Clip"
    assert r["genre"] == "Music"


def test_arte_unknown_sub_short_falls_to_duration_prior():
    # An unknown medium sub-label no longer leaves NULL: a SHORT item (<1800s)
    # under a recognized super-label takes the duration prior (Episode). A sub-30-
    # min piece under a fiction rubric is never a feature, so a film is not at
    # risk. The fiction super-label still carries no genre.
    r = enrich("ARTE.DE", "Fernsehfilme und Serien - Kurz und witzig", "X", "", 1500)
    assert r["category"] == "Episode"
    assert r["genre"] is None


def test_arte_unknown_sub_long_stays_null():
    # GUARD: a feature-length (>1800s) item under a fiction super-label with an
    # unknown sub-label stays NULL -- the duration prior protects a possible film,
    # exactly as for a non-ARTE row.
    r = enrich("ARTE.DE", "Fernsehfilme und Serien - Kurz und witzig", "X", "", 5400)
    assert r["category"] is None


def test_arte_unknown_sub_short_nonfiction_keeps_genre():
    # A short item under a non-fiction super-label: Episode via the prior, with
    # the super-label's genre preserved (News here).
    r = enrich("ARTE.FR", "Info et société - Décryptages", "Le sujet", "", 1500)
    assert r["category"] == "Episode"
    assert r["genre"] == "News"


def test_arte_series_sub_label_maps_to_episode():
    r = enrich("ARTE.FR", "Séries et fictions - Séries", "Episode 1", "", 2700)
    assert r["category"] == "Episode"


def test_rubric_multi_genre_is_canonical_order():
    # Maerchen -> two TMDB genres, joined in canonical TMDB order (Family<Fantasy).
    r = enrich("ARD", "Märchen", "Schneewittchen", "", 3000)
    assert r["genre"] == "Family, Fantasy"


def test_news_rubric_maps_to_news():
    r = enrich("DW", "Wirtschaft", "Marktbericht", "", 600)
    assert r["genre"] == "News"


def test_duration_prior_clip_episode_null():
    # No category signal: <120s Clip, 120-3600s Episode, >=3600s NULL. The Episode
    # ceiling is 60 min: 30-60 min long-form with no other signal is recurring TV
    # (docs, magazines, regional shows), not a film; >=60 min stays NULL so a
    # feature film (60-90 min TV films, longer features) is never mislabelled.
    assert enrich("ARD", "Beiträge", "A", "", 60)["category"] == "Clip"
    assert enrich("ARD", "Beiträge", "A", "", 900)["category"] == "Episode"
    assert enrich("ARD", "Beiträge", "A", "", 2700)["category"] == "Episode"
    assert enrich("ARD", "Beiträge", "A", "", 3599)["category"] == "Episode"
    assert enrich("ARD", "Beiträge", "A", "", 3600)["category"] is None


def test_season_episode_implies_episode_over_duration_prior():
    # No category signal, but season+episode are both set: the S/E notation is
    # decisive -- a long episode (>1800s) must not fall through to a NULL
    # category (the duration prior would yield None here).
    r = enrich("ZDFneo", "The Rookie", "Feuergefecht (S4/E7)", "", 2394)
    assert r["season"] == 4
    assert r["episode"] == 7
    assert r["category"] == "Episode"


def test_bare_episode_number_implies_episode():
    # A running episode number alone (a "Folge/Episode N" prefix, no season) is a
    # series signal: a long episode (>1800s) becomes Episode, not NULL. Telenovela
    # "Sturm der Liebe - Episode 332" is the canonical case.
    r = enrich("ONE", "Sturm der Liebe", "Episode 332", "", 2863)
    assert r["episode"] == 332
    assert r["category"] == "Episode"


def test_bare_season_number_implies_episode():
    # A season marker alone (a "(Staffel N)" parenthetical, no episode) is equally
    # a series signal: a long item (>1800s) becomes Episode, not NULL.
    r = enrich("ARD", "Vorstadtweiber", "Rückkehr (Staffel 2)", "", 3000)
    assert r["season"] == 2
    assert r["category"] == "Episode"


def test_bare_episode_number_does_not_override_movie():
    # GUARD: a running "Folge N" prefix on a metazeile-labelled film does NOT
    # demote the Movie -- the episodic lift only fills a NULL/Clip medium, so a
    # film-Reihe airing keeps category Movie (consistent with the S/E rule).
    r = enrich("ARD", "Marie Brand", "Folge 3: Der Fall",
                 "Krimi, Deutschland 2020", 5400)
    assert r["episode"] == 3
    assert r["category"] == "Movie"


def test_mehrteiler_count_overrides_movie_label():
    # "(5/6)" is a multi-part marker: a miniseries, not a standalone film. It
    # overrides the Movie label that topic 'Fernsehfilm' would otherwise give --
    # on TMDB such Mehrteiler are TV series, so they must be Episode.
    r = enrich("3Sat", "Fernsehfilm", "Eldorado KaDeWe - Jetzt ist unsere Zeit (5/6)", "", 2740)
    assert r["episode"] == 5
    assert r["episode_count"] == 6
    assert r["category"] == "Episode"


def test_mehrteiler_count_fills_null_category():
    # A multi-part marker turns a NULL medium (duration prior, >1800s) into an
    # Episode (a multi-part documentary/reihe), not an unknown.
    r = enrich("ARD", "Beiträge", "Die Geschichte des Südwestens (1/7)", "", 2684)
    assert r["episode"] == 1
    assert r["episode_count"] == 7
    assert r["category"] == "Episode"


def test_mehrteiler_count_does_not_override_clip():
    # A trailer with a "(n/m)" marker stays Clip -- the multi-part rule never
    # promotes a genuine clip/trailer to Episode.
    r = enrich("BR", "Tatort", "Trailer: Unvergänglich (1/2)", "", 29)
    assert r["episode_count"] == 2
    assert "T" in r["flags"]
    assert r["category"] == "Clip"


def test_film_reihe_with_se_stays_movie_with_series_name():
    # A feature-length film-reihe entry carries a "Krimi/Fernsehfilm" label AND
    # an explicit Sxx/Exx. TMDB is inconsistent for such Reihen (Sarah Kohr =
    # tv/202362 series, but Rosamunde Pilcher = individual movies), so enrich is
    # internally CONSISTENT: an Sxx/Exx does not override a Movie label -- the row
    # stays Movie, keeping its series_name. match bridges the TMDB split later.
    r = enrich("3Sat", "Sarah Kohr",
                 "Das verschwundene Mädchen - Krimi, Deutschland 2014 (S1/E3)", "", 5367)
    assert r["category"] == "Movie"
    assert r["series_name"] == "Sarah Kohr"
    assert r["season"] == 1
    assert r["episode"] == 3


def test_standalone_film_without_se_stays_movie():
    # The override is gated on an explicit episodic marker: a feature film with a
    # "Spielfilm"/"Krimi" metazeile but NO Sxx/Exx and NO "(n/m)" stays Movie.
    r = enrich("3Sat", "Spielfilm",
                 "Der Vorname - Komödie, Deutschland 2018", "", 5400)
    assert r["category"] == "Movie"
    assert r["season"] is None
    assert r["episode"] is None


def test_fiction_topic_lifts_null_to_movie():
    # A known fiction-Reihe topic (Tatort) with NO film metazeile on this airing
    # leaves category NULL via the duration prior (a 89-min crime film, >1800s).
    # The fiction-topic allowlist lifts it to Movie with series_name, matching the
    # labelled airings of the same Reihe (internal consistency).
    r = enrich("ARD", "Tatort", "Tatort: Seenot", "", 5339)
    assert r["category"] == "Movie"
    assert r["series_name"] == "Tatort"


def test_fiction_topic_lifts_sub_60min_film_to_movie():
    # GUARD (round 16): a fiction-Reihe film in the 30-60 min band -- a Maerchen
    # fairy-tale film (~58 min) -- must stay Movie. The raised duration-prior
    # ceiling (60 min) would otherwise call it Episode; the fiction lift reclaims
    # any film-length (>=1800s) prior guess for a known fiction topic.
    r = enrich("ARD", "Märchen in der ARD", "Die Gänseprinzessin", "", 3536)
    assert r["category"] == "Movie"
    assert r["series_name"] == "Märchen in der ARD"


def test_fiction_topic_does_not_touch_episode():
    # The lift fires ONLY on a NULL medium. A Tatort airing with explicit Sxx/Exx
    # is Episode (episodic pass) and stays Episode -- the per-airing Movie/Episode
    # scatter inside a Reihe is left for match to regroup via series_name.
    r = enrich("ARD", "Tatort", "Tatort: Seenot (S1/E5)", "", 3000)
    assert r["season"] == 1
    assert r["episode"] == 5
    assert r["category"] == "Episode"


def test_fiction_topic_trailer_stays_clip():
    # A short trailer of a fiction Reihe is Clip via the duration prior and is NOT
    # lifted (the lift only touches NULL, and a trailer carries the T flag).
    r = enrich("ARD", "Tatort", "Tatort: Seenot (Trailer)", "", 40)
    assert "T" in r["flags"]
    assert r["category"] == "Clip"


def test_non_fiction_feature_topic_stays_null():
    # A feature-length NON-fiction topic (phoenix news block) is not in the
    # allowlist, so it stays NULL -- the lift never guesses beyond known Reihen.
    r = enrich("ARD", "phoenix vor ort", "phoenix vor ort: Bundestag", "", 5000)
    assert r["category"] is None


def test_midtitle_part_marker_is_episode():
    # "Title N/M - Subtitle": a multi-part marker in the MIDDLE of the title (a
    # nature-doc miniseries) -- NPART is end-anchored and PART needs parens, so
    # both miss it. It parses to episode/episode_count and (via the Mehrteiler
    # rule) classifies as Episode; a >1800s part would otherwise fall to NULL.
    r = enrich("3Sat", "Natur", "Wunderwelt Schweiz 3/4 - Das Tessin", "", 3027)
    assert r["episode"] == 3
    assert r["episode_count"] == 4
    assert r["category"] == "Episode"
    assert r["clean_title"] == "Wunderwelt Schweiz - Das Tessin"


def test_midtitle_part_ignores_mixed_fraction():
    # A MIXED fraction in a film title ("8 1/2 - ...", Fellini) must NOT read as a
    # part marker: a whole number + space directly before the "n/m" disqualifies it
    # (else the film would be wrongly turned into an Episode).
    r = enrich("ARTE.DE", "Kino - Filme", "8 1/2 - Ein Film von Fellini", "", 6000)
    assert r["episode"] is None
    assert r["episode_count"] is None
    assert r["category"] == "Movie"


def test_short_trailer_in_film_topic_is_clip_not_movie():
    # A trailer in a film-rubric topic (Filme in der ARD -> Movie via FORMAT_TOPICS)
    # is short and carries the T flag: a trailer is always a Clip, never a Movie.
    r = enrich("ARD", "Filme in der ARD", "Trailer: Gladbeck", "", 123)
    assert "T" in r["flags"]
    assert r["category"] == "Clip"


def test_long_trailer_themed_show_stays_episode():
    # The trailer demotion is gated on a SHORT duration: a long-form show whose
    # title merely mentions "Trailer" (a 25-min magazine about film trailers) keeps
    # the T flag but is NOT demoted -- it stays its duration-prior medium (Episode).
    r = enrich("ServusTV", "Trailer.AT", "Trailer.AT: Folge 6", "", 1559)
    assert "T" in r["flags"]
    assert r["category"] == "Episode"


def test_arte_companion_interview_is_clip_not_movie():
    # Under an ARTE short-film sub-label ("Kurzfilme" -> Movie), a companion
    # interview with the director is filed in the same topic but is a clip about
    # the film, not the film. A short companion piece is demoted to Clip and
    # carries the I (interview) flag.
    r = enrich("ARTE.DE", "Kino - Kurzfilme",
                 'Interview mit Ellen Ekman - Regisseurin von "Discokugel"', "", 449)
    assert r["category"] == "Clip"
    assert "I" in r["flags"]


def test_making_of_is_clip_not_movie():
    # A making-of is a companion clip and carries the M (making-of) flag.
    r = enrich("ZDF", "Filme", "Making of - Folge 2 - Shut up & Dance", "", 185)
    assert r["category"] == "Clip"
    assert "M" in r["flags"]


def test_feature_film_titled_interview_stays_movie():
    # The companion demotion/flag is gated on a short duration: a feature-length
    # film whose title merely starts with "Interview mit" (a 2-h drama) stays Movie
    # and gets NO interview flag.
    r = enrich("ARTE.DE", "Kino - Filme", "Interview mit einem Vampir", "", 6840)
    assert r["category"] == "Movie"
    assert "I" not in r["flags"]


def test_fiction_topics_extendable_via_param():
    # The allowlist is configurable (it grows over time): a topic absent from the
    # built-in default stays NULL, but lifts to Movie when supplied via the
    # fiction_topics argument (the CLI passes the built-in default unioned with
    # config). The supplied set must be casefolded, like the built-in default.
    r0 = enrich("ARD", "Mein Regio-Krimi", "Mein Regio-Krimi: Folge X", "", 5000)
    assert r0["category"] is None
    r1 = enrich("ARD", "Mein Regio-Krimi", "Mein Regio-Krimi: Folge X", "", 5000,
                 fiction_topics=FICTION_TOPICS | {"mein regio-krimi"})
    assert r1["category"] == "Movie"


# -- cmd_enrich: DB write side ---------------------------------------------

def open_db(tmp_path):
    return db_connect(str(tmp_path / "theke.db"))


def insert_row(conn, mediathek_id, sender="ARD", topic="", title="",
               description="", duration=0, status="0"):
    conn.execute(
        "INSERT INTO mediathek (status, mediathek_id, sender, topic, title, "
        "description, duration) VALUES (?,?,?,?,?,?,?)",
        (status, mediathek_id, sender, topic, title, description, duration))


def args(force=False):
    return SimpleNamespace(enrich_cmd="run", force=force)


def insert_enriched(conn, mediathek_id, sender="ARD", **cols):
    """Insert a row with enrich columns already set (for --analyze tests)."""
    base = dict(status="1", mediathek_id=mediathek_id, sender=sender, **cols)
    keys = list(base)
    conn.execute(f"INSERT INTO mediathek ({','.join(keys)}) "
                 f"VALUES ({','.join(':' + k for k in keys)})", base)


def test_cmd_enrich_fills_columns_and_flips_status(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", sender="ARD", topic="Filmmittwoch im Ersten",
                   title="Der Fall (Audiodeskription)",
                   description="Spielfilm Deutschland 2003 Ein Fall.", duration=5400)
        result = cmd_enrich(conn, Config(), args())
        assert result == {"enriched": 1}
        row = conn.execute("SELECT * FROM mediathek WHERE mediathek_id='a'").fetchone()
        assert row["status"] == "1"
        assert row["category"] == "Movie"
        assert row["year"] == 2003
        assert row["country"] == "Deutschland"
        assert row["flags"] == "A"
        assert row["clean_title"] == "Der Fall"
        assert row["enrich_confidence"] == 0.9
    finally:
        conn.close()


def test_cmd_enrich_default_scope_skips_already_enriched(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", title="A")
        insert_row(conn, "b", title="B")
        assert cmd_enrich(conn, Config(), args())["enriched"] == 2
        # both rows are status '1' now -> a second default run does nothing
        assert cmd_enrich(conn, Config(), args())["enriched"] == 0
    finally:
        conn.close()


def test_cmd_enrich_force_reprocesses_all(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", title="A")
        insert_row(conn, "b", title="B")
        cmd_enrich(conn, Config(), args())
        assert cmd_enrich(conn, Config(), args(force=True))["enriched"] == 2
    finally:
        conn.close()


def test_cmd_enrich_preserves_phase3_ids(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", sender="ARD", title="Der Fall")
        conn.execute("UPDATE mediathek SET tmdb_id='123', imdb_id='tt9', "
                     "match_confidence=0.8 WHERE mediathek_id='a'")
        cmd_enrich(conn, Config(), args())
        row = conn.execute("SELECT * FROM mediathek WHERE mediathek_id='a'").fetchone()
        assert row["tmdb_id"] == "123"
        assert row["imdb_id"] == "tt9"
        assert row["match_confidence"] == 0.8
        assert row["clean_title"] == "Der Fall"   # enrich still ran
    finally:
        conn.close()


# -- enrich reset (status 1/2 -> 0) ----------------------------------------

def reset_args(status_only=False):
    return SimpleNamespace(enrich_cmd="reset", status_only=status_only)


def test_cmd_enrich_reset_flips_status_and_clears_columns(tmp_path):
    conn = open_db(tmp_path)
    try:
        # one enriched ('1') and one matched ('2') row, both with derived data
        insert_enriched(conn, "a", clean_title="Der Fall", year=2003,
                        category="Movie", language="de", enrich_confidence=0.9)
        insert_enriched(conn, "b", clean_title="Das Boot", year=1981,
                        category="Movie", language="de", enrich_confidence=0.9)
        conn.execute("UPDATE mediathek SET status='2', tmdb_id='123', "
                     "match_confidence=0.8 WHERE mediathek_id='b'")
        result = cmd_enrich(conn, Config(), reset_args())
        assert result == {"reset": 2}
        for mid in ("a", "b"):
            row = conn.execute("SELECT * FROM mediathek WHERE mediathek_id=?",
                               (mid,)).fetchone()
            assert row["status"] == "0"
            assert row["clean_title"] is None
            assert row["year"] is None
            assert row["category"] is None
            assert row["enrich_confidence"] is None
            assert row["language"] == ""          # back to the fetch default
            assert row["tmdb_id"] == ""
            assert row["match_confidence"] is None
    finally:
        conn.close()


def test_cmd_enrich_reset_status_only_keeps_columns(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_enriched(conn, "a", clean_title="Der Fall", year=2003,
                        category="Movie", language="de", enrich_confidence=0.9)
        result = cmd_enrich(conn, Config(), reset_args(status_only=True))
        assert result == {"reset": 1}
        row = conn.execute("SELECT * FROM mediathek WHERE mediathek_id='a'").fetchone()
        assert row["status"] == "0"
        assert row["clean_title"] == "Der Fall"   # untouched
        assert row["year"] == 2003
        assert row["enrich_confidence"] == 0.9
    finally:
        conn.close()


def test_cmd_enrich_reset_leaves_new_rows(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "n", title="New")                  # status '0'
        insert_enriched(conn, "a", clean_title="Der Fall")  # status '1'
        assert cmd_enrich(conn, Config(), reset_args())["reset"] == 1
        n = conn.execute("SELECT status FROM mediathek WHERE mediathek_id='n'").fetchone()
        assert n["status"] == "0"   # already new -> untouched
    finally:
        conn.close()


def test_cli_enrich_json_on_stdout_progress_on_stderr(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_row(conn, "a", title="A")
        insert_row(conn, "b", title="B")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "enrich", "run"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"enriched": 2}   # one parseable object
    assert captured.out.strip().count("\n") == 0           # ... and only that


# -- enrich report (stored / --live), read-only ----------------------------

def test_analyze_report_from_stored_columns(tmp_path):
    conn = open_db(tmp_path)
    try:
        # two ARD rows: one fully enriched film, one NULL-category fallback
        insert_enriched(conn, "a", year=2003, country="Deutschland",
                          category="Movie", enrich_confidence=0.9, flags="A")
        insert_enriched(conn, "b", category=None, enrich_confidence=0.2,
                          flags="")
        report = enrich_report(conn, live=False, min_rows=1)
        assert report == {"ARD": {
            "n": 2, "year_pct": 50.0, "country_pct": 50.0, "se_pct": 0.0,
            "cat_pct": 50.0, "unklar_pct": 50.0,
            "genre_pct": 0.0, "slot_pct": 0.0, "events_pct": 0.0,
            "flag_a_pct": 50.0, "flag_e_pct": 0.0, "flag_s_pct": 0.0,
            "flag_u_pct": 0.0, "flag_t_pct": 0.0,
        }}
    finally:
        conn.close()


def test_dry_run_report_is_live_and_writes_nothing(tmp_path):
    conn = open_db(tmp_path)
    try:
        # raw, unenriched rows; enrich() runs live over them
        insert_row(conn, "a", sender="ARD", topic="Filmmittwoch im Ersten",
                   title="Der Fall", description="Spielfilm Deutschland 2003 Ein Fall.",
                   duration=5400)                     # -> year+country+Spielfilm, conf 0.9
        insert_row(conn, "b", sender="ARD", topic="heute", title="heute",
                   description="", duration=900)       # -> Beitrag/Episode, conf 0.5
        report = enrich_report(conn, live=True, min_rows=1)
        assert report == {"ARD": {
            "n": 2, "year_pct": 50.0, "country_pct": 50.0, "se_pct": 0.0,
            "cat_pct": 50.0, "unklar_pct": 0.0,
            "genre_pct": 0.0, "slot_pct": 50.0, "events_pct": 0.0,  # "Filmmittwoch im Ersten" -> slot
            "flag_a_pct": 0.0, "flag_e_pct": 0.0, "flag_s_pct": 0.0,
            "flag_u_pct": 0.0, "flag_t_pct": 0.0,
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
        insert_enriched(conn, "a", genre="Documentary", category=None,
                          enrich_confidence=0.2, flags="")
        insert_enriched(conn, "b", slot="hr Retro", category=None,
                          enrich_confidence=0.2, flags="")
        insert_enriched(conn, "c", series_name="Berlinale", category="Event",
                          enrich_confidence=0.8, flags="")
        insert_enriched(conn, "d", category=None, enrich_confidence=0.2, flags="")
        st = enrich_report(conn, live=False, min_rows=1)["ARD"]
        assert st["genre_pct"] == 25.0
        assert st["slot_pct"] == 25.0
        assert st["events_pct"] == 25.0
    finally:
        conn.close()


def test_report_min_rows_filters_small_senders(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_enriched(conn, "a", category="unklar", enrich_confidence=0.2, flags="")
        assert enrich_report(conn, live=False, min_rows=2) == {}   # only 1 ARD row
    finally:
        conn.close()


def test_report_sender_filter_narrows_to_named_senders(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_enriched(conn, "a", sender="ARD", category="unklar",
                          enrich_confidence=0.2, flags="")
        insert_enriched(conn, "z", sender="ZDF", category="Spielfilm",
                          enrich_confidence=0.9, flags="")
        report = enrich_report(conn, live=False, min_rows=1, senders=["ZDF"])
        assert set(report) == {"ZDF"}
        assert report["ZDF"]["n"] == 1
    finally:
        conn.close()


def test_report_by_confidence_splits_category_into_per_level_columns(tmp_path):
    conn = open_db(tmp_path)
    try:
        # two ARD rows, one per confidence level (0.9 and 0.5)
        insert_enriched(conn, "a", category="Spielfilm", enrich_confidence=0.9, flags="")
        insert_enriched(conn, "b", category="Beitrag/Episode", enrich_confidence=0.5, flags="")
        st = enrich_report(conn, live=False, min_rows=1, by_confidence=True)["ARD"]
        assert st["c90_pct"] == 50.0   # 1 of 2 rows at conf 0.9
        assert st["c80_pct"] == 0.0
        assert st["c50_pct"] == 50.0   # 1 of 2 rows at conf 0.5
        assert st["c20_pct"] == 0.0
        # the per-level columns are absent unless requested (stable default shape)
        assert "c90_pct" not in enrich_report(conn, live=False, min_rows=1)["ARD"]
    finally:
        conn.close()


def test_cli_enrich_report_by_confidence_json(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_enriched(conn, "a", sender="ARD", category="Spielfilm",
                          enrich_confidence=0.9, flags="")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "enrich", "report",
                 "--by-confidence", "--min-rows", "0"]) == 0
    st = json.loads(capsys.readouterr().out)["senders"]["ARD"]
    assert st["c90_pct"] == 100.0


def test_report_diff_reports_per_field_churn(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", sender="ARD", topic="x", title="Der Fall",
                   description="Spielfilm Deutschland 2003.", duration=5400)
        # pretend an older enrich() run stored a different category/year; the
        # other columns already match what enrich() produces today
        conn.execute("UPDATE mediathek SET category='OldCat', year=1999, "
                     "clean_title='Der Fall', series_name='x', country='Deutschland', "
                     "enrich_confidence=0.9, language='de', flags='' "
                     "WHERE mediathek_id='a'")
        ard = enrich_report_diff(conn)["ARD"]
        assert ard["category"]["changed"] == 1
        assert ard["year"]["changed"] == 1
        assert ard["category"]["samples"][0] == {
            "id": "a", "before": "OldCat", "after": "Movie"}
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
        # stored columns already equal a live enrich() pass -> no churn
        insert_row(conn, "a", sender="ARD", topic="x", title="A", duration=60)
        conn.execute("UPDATE mediathek SET category='Clip', series_name='x', "
                     "clean_title='A', language='de', flags='', "
                     "enrich_confidence=0.5 WHERE mediathek_id='a'")  # Clip -> conf 0.5
        assert enrich_report_diff(conn) == {}
    finally:
        conn.close()


def test_cli_enrich_report_diff_json(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_row(conn, "a", sender="ARD", topic="x", title="Der Fall",
                   description="Spielfilm Deutschland 2003.", duration=5400)
        conn.execute("UPDATE mediathek SET category='OldCat', year=1999, "
                     "clean_title='Der Fall', series_name='x', country='Deutschland', "
                     "enrich_confidence=0.9, language='de', flags='' "
                     "WHERE mediathek_id='a'")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "enrich", "report", "--diff"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "diff"
    assert out["senders"]["ARD"]["category"]["changed"] == 1


def test_cli_enrich_report_json(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_row(conn, "a", title="A")
        insert_row(conn, "b", title="B")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "enrich", "report"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "stored"           # below the default min_rows -> empty
    assert out["senders"] == {}


def test_cli_enrich_report_live_json(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_row(conn, "a", title="A")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "enrich", "report", "--live"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "live"


def test_cli_enrich_report_min_rows_zero_shows_small_sender(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_enriched(conn, "a", sender="ARD", category="unklar",
                          enrich_confidence=0.2, flags="")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "enrich", "report", "--min-rows", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["senders"]) == {"ARD"}    # the single-row sender is now visible


def test_cli_enrich_bare_runs_default_action(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    db_connect(db).close()
    assert main(["--json", "--db", db, "enrich"]) == 0  # defaults to `run`
    assert "enriched" in json.loads(capsys.readouterr().out)


# -- enrich audit (read-only findings scan) --------------------------------

def test_audit_bare_topic(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", sender="ARD", topic="Spielfilm", title="X")
        insert_row(conn, "b", sender="ARD", topic="Tatort", title="Y")   # real series
        res = enrich_audit(conn, checks=["bare-topic"])
        assert res["ARD"]["bare-topic"]["count"] == 1
        assert res["ARD"]["bare-topic"]["examples"] == ["Spielfilm"]
    finally:
        conn.close()


def test_audit_topic_pipe(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", sender="HR", topic="hr Retro | Geschichte", title="X")
        res = enrich_audit(conn, checks=["topic-pipe"])
        assert res["HR"]["topic-pipe"]["count"] == 1
        assert res["HR"]["topic-pipe"]["examples"] == ["hr Retro | Geschichte"]
    finally:
        conn.close()


def test_audit_topic_marker(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", sender="ORF", topic="ZIB (mit Gebärdensprache)", title="X")
        res = enrich_audit(conn, checks=["topic-marker"])
        assert res["ORF"]["topic-marker"]["count"] == 1
        assert res["ORF"]["topic-marker"]["examples"] == ["ZIB (mit Gebärdensprache)"]
    finally:
        conn.close()


def test_audit_case_variants(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_row(conn, "a", sender="3Sat", topic="nano", title="X")
        insert_row(conn, "b", sender="3Sat", topic="NANO", title="Y")
        res = enrich_audit(conn, checks=["case-variants"])
        assert res["3Sat"]["case-variants"]["count"] == 2          # both rows
        assert res["3Sat"]["case-variants"]["examples"] == ["NANO/nano"]
    finally:
        conn.close()


def test_audit_country_shape(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_enriched(conn, "a", sender="ZDF", country="vom 3. Mai")   # date residue
        insert_enriched(conn, "b", sender="ZDF", country="Deutschland")  # real country
        res = enrich_audit(conn, checks=["country-shape"])
        assert res["ZDF"]["country-shape"]["count"] == 1
        assert res["ZDF"]["country-shape"]["examples"] == ["vom 3. Mai"]
    finally:
        conn.close()


def test_audit_title_credit(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_enriched(conn, "a", sender="3Sat",
                          clean_title="Der Wald - Film von Hans Meiser")
        insert_enriched(conn, "b", sender="3Sat", clean_title="Der Wald")
        res = enrich_audit(conn, checks=["title-credit"])
        assert res["3Sat"]["title-credit"]["count"] == 1
        assert res["3Sat"]["title-credit"]["examples"] == ["Der Wald - Film von Hans Meiser"]
    finally:
        conn.close()


def test_audit_episodic_unparsed(tmp_path):
    conn = open_db(tmp_path)
    try:
        # raw, unenriched -> season/episode are NULL but the title looks episodic
        insert_row(conn, "a", sender="HR", topic="Die Reise", title="Die Reise, Folge 3")
        res = enrich_audit(conn, checks=["episodic-unparsed"])
        assert res["HR"]["episodic-unparsed"]["count"] == 1
        assert res["HR"]["episodic-unparsed"]["examples"] == ["Die Reise, Folge 3"]
    finally:
        conn.close()


def test_audit_limit_caps_examples(tmp_path):
    conn = open_db(tmp_path)
    try:
        for i, w in enumerate(("Spielfilm", "Drama", "Krimi")):
            insert_row(conn, str(i), sender="ARD", topic=w, title="X")
        res = enrich_audit(conn, checks=["bare-topic"], limit=2)
        assert res["ARD"]["bare-topic"]["count"] == 3          # all counted
        assert len(res["ARD"]["bare-topic"]["examples"]) == 2  # examples capped
    finally:
        conn.close()


def test_audit_unknown_check_raises(tmp_path):
    conn = open_db(tmp_path)
    try:
        with pytest.raises(Exception):
            enrich_audit(conn, checks=["nope"])
    finally:
        conn.close()


def test_cli_audit_json(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_row(conn, "a", sender="ARD", topic="Spielfilm", title="X")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "enrich", "audit", "--check", "bare-topic"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["senders"]["ARD"]["bare-topic"]["count"] == 1


def test_cli_audit_unknown_check_exits_1(tmp_path):
    db = str(tmp_path / "theke.db")
    db_connect(db).close()
    assert main(["--db", db, "enrich", "audit", "--check", "nope"]) == 1


# -- enrich show (read-only row sampler, structured filters) ----------------

def show_args(sender=None, like=None, eq=None, null=None, not_null=None,
              min_conf=None, max_conf=None, limit=20):
    return SimpleNamespace(enrich_cmd="show", sender=sender, like=like, eq=eq,
                           null=null, not_null=not_null, min_conf=min_conf,
                           max_conf=max_conf, limit=limit)


def test_show_like_filter(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_enriched(conn, "a", clean_title="Der Wald - Film von Hans")
        insert_enriched(conn, "b", clean_title="Der Wald")
        where, params = _build_show_where(conn, show_args(like=[["clean_title", "%Film von %"]]))
        rows = enrich_show(conn, where, params, 20)
        assert [r["mediathek_id"] for r in rows] == ["a"]
        assert rows[0]["clean_title"] == "Der Wald - Film von Hans"
    finally:
        conn.close()


def test_show_eq_with_sender_filter(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_enriched(conn, "a", sender="ARD", category="Spielfilm")
        insert_enriched(conn, "b", sender="ZDF", category="Spielfilm")
        insert_enriched(conn, "c", sender="ARD", category="Doku")
        where, params = _build_show_where(conn, show_args(sender="ARD", eq=[["category", "Spielfilm"]]))
        rows = enrich_show(conn, where, params, 20)
        assert [r["mediathek_id"] for r in rows] == ["a"]
    finally:
        conn.close()


def test_show_null_and_not_null(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_enriched(conn, "a", year=2003)
        insert_enriched(conn, "b")            # year stays NULL
        nullrows = enrich_show(conn, *_build_show_where(conn, show_args(null=["year"])), 20)
        notnull = enrich_show(conn, *_build_show_where(conn, show_args(not_null=["year"])), 20)
        assert [r["mediathek_id"] for r in nullrows] == ["b"]
        assert [r["mediathek_id"] for r in notnull] == ["a"]
    finally:
        conn.close()


def test_show_min_conf(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_enriched(conn, "a", enrich_confidence=0.9)
        insert_enriched(conn, "b", enrich_confidence=0.2)
        rows = enrich_show(conn, *_build_show_where(conn, show_args(min_conf=0.5)), 20)
        assert [r["mediathek_id"] for r in rows] == ["a"]
    finally:
        conn.close()


def test_show_limit_caps_rows(tmp_path):
    conn = open_db(tmp_path)
    try:
        for i in range(3):
            insert_enriched(conn, str(i), category="Spielfilm")
        rows = enrich_show(conn, *_build_show_where(conn, show_args()), 2)
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
        insert_enriched(conn, "a", sender="3Sat", clean_title="Der Wald - Film von Hans")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "enrich", "show",
                 "--like", "clean_title", "%Film von %"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["mediathek_id"] for r in out["rows"]] == ["a"]


def test_cli_show_unknown_field_exits_1(tmp_path):
    db = str(tmp_path / "theke.db")
    db_connect(db).close()
    assert main(["--db", db, "enrich", "show", "--null", "nope"]) == 1


# -- enrich dist (read-only field value distribution) ----------------------

def test_dist_counts_values_descending(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_enriched(conn, "a", category="Spielfilm")
        insert_enriched(conn, "b", category="Spielfilm")
        insert_enriched(conn, "c", category="Doku")
        assert enrich_dist(conn, "category") == [("Spielfilm", 2), ("Doku", 1)]
    finally:
        conn.close()


def test_dist_sender_filter(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_enriched(conn, "a", sender="ARD", category="Spielfilm")
        insert_enriched(conn, "b", sender="ZDF", category="Spielfilm")
        assert enrich_dist(conn, "category", senders=["ARD"]) == [("Spielfilm", 1)]
    finally:
        conn.close()


def test_dist_limit_caps_entries(tmp_path):
    conn = open_db(tmp_path)
    try:
        for i, c in enumerate(("Spielfilm", "Doku", "Krimi")):
            insert_enriched(conn, str(i), category=c)
        assert len(enrich_dist(conn, "category", limit=2)) == 2
    finally:
        conn.close()


def test_dist_unknown_field_raises(tmp_path):
    conn = open_db(tmp_path)
    try:
        with pytest.raises(Exception):
            enrich_dist(conn, "nope")
    finally:
        conn.close()


def test_cli_dist_json(tmp_path, capsys):
    db = str(tmp_path / "theke.db")
    conn = db_connect(db)
    try:
        insert_enriched(conn, "a", sender="ARD", category="Spielfilm")
    finally:
        conn.close()
    assert main(["--json", "--db", db, "enrich", "dist", "--field", "category"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["field"] == "category"
    assert out["values"] == [["Spielfilm", 1]]   # tuples serialize to JSON arrays


def test_cli_dist_unknown_field_exits_1(tmp_path):
    db = str(tmp_path / "theke.db")
    db_connect(db).close()
    assert main(["--db", db, "enrich", "dist", "--field", "nope"]) == 1
