"""Tests for the match stage (wish-first TMDB matching, movies)."""

import json
from types import SimpleNamespace

import pytest

import theke
from theke import Config, db_connect
from theke.match import (normalize, strip_articles, title_similarity,
                         score_match, tmdb_movie, find_matches)


# -- normalize / strip_articles ----------------------------------------------

def test_normalize_lowercases_and_drops_punctuation():
    # casefold; ':' '(' ')' -> space; whitespace collapsed.
    assert normalize("Der Fall: Die Wahrheit (2003)") == "der fall die wahrheit 2003"


def test_normalize_folds_umlauts_and_eszett():
    # ue/oe/ae and ss (the German convention), both sides identical later.
    assert normalize("Über Größe") == "ueber groesse"
    assert normalize("Mörderischer Süden") == "moerderischer sueden"


def test_normalize_collapses_whitespace_and_endash():
    assert normalize("Tatort – Der rote Schatten") == "tatort der rote schatten"
    assert normalize("  Multiple   Spaces ") == "multiple spaces"


def test_normalize_none_is_empty():
    assert normalize(None) == ""


def test_strip_articles_removes_one_leading_article():
    assert strip_articles("die hard") == "hard"
    assert strip_articles("das boot") == "boot"
    assert strip_articles("the matrix") == "matrix"
    assert strip_articles("ein mann") == "mann"
    assert strip_articles("eine frau") == "frau"


def test_strip_articles_leaves_non_article_and_sole_token():
    assert strip_articles("hard") == "hard"
    assert strip_articles("die") == "die"   # only token -> keep, never empty


# -- title_similarity --------------------------------------------------------

def test_title_similarity_exact_is_one():
    assert title_similarity(["Das Boot"], "Das Boot") == 1.0


def test_title_similarity_article_cross_match():
    # tmdb has no article, clean has one -> article-stripped form matches exactly.
    assert title_similarity(["Hard"], "Die Hard") == 1.0


def test_title_similarity_whole_token_substring_is_high():
    # tmdb title is a contiguous token-run inside clean_title (trailing artifact).
    assert title_similarity(["Tatort"], "Tatort Der rote Schatten") == 0.95


def test_title_similarity_fuzzy_uses_ratio():
    # not exact, not a token subset -> SequenceMatcher ratio.
    # "das boot" (8) is a prefix of "das boott" (9): 2*8/(8+9) = 16/17.
    assert title_similarity(["Das Boot"], "Das Boott") == pytest.approx(16 / 17, abs=5e-4)


def test_title_similarity_below_floor_for_unrelated():
    assert title_similarity(["Heat"], "Das Boot") < 0.85


# -- score_match -------------------------------------------------------------

BOOT = {"titles": ["Das Boot"], "year": 1981, "runtime": 149}   # 149 min


def row(clean_title="Das Boot", year=1981, duration=8940):       # 8940 s = 149 min
    return {"clean_title": clean_title, "year": year, "duration": duration}


def test_score_exact_match_is_one():
    s = score_match(BOOT, row())
    assert s["rejected"] is False
    assert s["confidence"] == 1.0
    assert s["title_sim"] == 1.0
    assert s["year_delta"] == 0


def test_score_year_one_off_small_penalty():
    # title 1.0 * year_factor(1-0.03*1=0.97) * runtime 1.0 = 0.97
    assert score_match(BOOT, row(year=1982))["confidence"] == 0.97


def test_score_year_two_off_larger_penalty():
    # 1.0 * (1-0.03*2=0.94) * 1.0 = 0.94
    assert score_match(BOOT, row(year=1983))["confidence"] == 0.94


def test_score_year_more_than_two_off_is_rejected():
    s = score_match(BOOT, row(year=1985))   # delta 4 > 2
    assert s["rejected"] is True
    assert s["confidence"] == 0.0


def test_score_missing_year_caps_confidence():
    # no year gate -> NO_YEAR_FACTOR 0.85; runtime exact -> 1.0
    s = score_match(BOOT, row(year=None))
    assert s["year_delta"] is None
    assert s["confidence"] == 0.85


def test_score_runtime_off_soft_penalty():
    # title 1.0 * year 1.0 * runtime_factor 0.90; 3600 s = 60 min vs 149 min
    s = score_match(BOOT, row(duration=3600))
    assert s["confidence"] == 0.9
    assert s["runtime_delta"] == -89   # 60 - 149


def test_score_below_title_floor_is_rejected():
    s = score_match(BOOT, row(clean_title="Heat"))
    assert s["rejected"] is True
    assert s["confidence"] == 0.0


# -- tmdb_movie (IO via monkeypatched http_get) ------------------------------

TMDB_BOOT = {
    "title":             "Das Boot",
    "original_title":    "Das Boot",
    "release_date":      "1981-09-17",
    "runtime":           149,
    "original_language": "de",
    "alternative_titles": {"titles": [
        {"iso_3166_1": "US", "title": "The Boat"},
        {"iso_3166_1": "DE", "title": "Das Boot - Director's Cut"},
    ]},
}


def test_tmdb_movie_parses_titles_year_runtime(monkeypatch):
    seen = {}

    def fake_get(url):
        seen["url"] = url
        return json.dumps(TMDB_BOOT).encode("utf-8")

    monkeypatch.setattr(theke, "http_get", fake_get)
    cfg = Config(tmdb_api_key="KEY")
    meta = tmdb_movie(cfg, 1234)

    assert "1234" in seen["url"] and "KEY" in seen["url"]
    assert meta["year"] == 1981
    assert meta["runtime"] == 149
    assert meta["original_language"] == "de"
    # title == original_title -> deduped; only the DE alternative is added, US dropped.
    assert meta["titles"] == ["Das Boot", "Das Boot - Director's Cut"]


# -- find_matches (DB scan over category='Movie') ----------------------------

def open_db(tmp_path):
    return db_connect(str(tmp_path / "theke.db"))


def insert_movie(conn, mediathek_id, clean_title, year, duration, category="Movie"):
    conn.execute(
        "INSERT INTO mediathek (status, mediathek_id, category, clean_title, "
        "year, duration) VALUES ('1',?,?,?,?,?)",
        (mediathek_id, category, clean_title, year, duration))


def test_find_matches_selects_movies_scores_and_sorts(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "m1", "Das Boot", 1981, 8940)            # exact -> 1.0
        insert_movie(conn, "m2", "Das Boot Extended", 1981, 9000)   # substring -> 0.95
        insert_movie(conn, "m3", "Heat", 1995, 6000)                # title floor -> out
        insert_movie(conn, "e1", "Das Boot", 1981, 8940, category="Episode")  # not a movie
        matches = find_matches(conn, BOOT, min_conf=0.6)
        assert [m["mediathek_id"] for m in matches] == ["m1", "m2"]
        assert matches[0]["confidence"] == 1.0
        assert matches[1]["confidence"] == 0.95
    finally:
        conn.close()


def test_find_matches_respects_min_conf(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "m1", "Das Boot", 1981, 8940)
        insert_movie(conn, "m2", "Das Boot Extended", 1981, 9000)
        matches = find_matches(conn, BOOT, min_conf=0.99)
        assert [m["mediathek_id"] for m in matches] == ["m1"]
    finally:
        conn.close()
