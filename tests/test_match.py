"""Tests for the match stage (wish-first TMDB matching, movies)."""

import json
import logging
from types import SimpleNamespace

import pytest

import theke
from theke import Config, ConfigError, cmd_match, db_connect
from theke.match import (normalize, strip_articles, title_similarity,
                         score_match, tmdb_movie, find_matches,
                         tmdb_tv, score_episode, find_episode_matches,
                         is_arte_sender, arte_video_id, arte_anchor_ids,
                         find_arte_links)


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


def test_title_similarity_substring_high_only_with_coverage():
    # tmdb title is most of clean_title (just a trailing artifact) -> high.
    assert title_similarity(["Bibi & Tina - Der Film"],
                            "Bibi & Tina - Der Film HD") == 0.95


def test_title_similarity_low_coverage_substring_not_high():
    # a short generic title inside a long clean_title (a franchise/series name)
    # must NOT earn the substring bonus -> falls to the fuzzy ratio, below floor.
    assert title_similarity(["Tatort"], "Tatort Der rote Schatten") < 0.85


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


def test_score_year_tolerance_override_widens_gate():
    # delta 4 is rejected at the default 2, accepted at tolerance 5:
    # title 1.0 * year_factor(1-0.03*4=0.88) * runtime 1.0 = 0.88
    s = score_match(BOOT, row(year=1985), year_tolerance=5)
    assert s["rejected"] is False
    assert s["confidence"] == 0.88
    assert s["year_delta"] == 4


def test_score_missing_year_caps_confidence():
    # no year gate -> NO_YEAR_FACTOR 0.85; runtime exact -> 1.0
    s = score_match(BOOT, row(year=None))
    assert s["year_delta"] is None
    assert s["confidence"] == 0.85


def test_score_runtime_off_soft_penalty():
    # within the floor (>=50% of 149) but beyond tolerance: 120 min vs 149 min
    # -> title 1.0 * year 1.0 * runtime_factor 0.90
    s = score_match(BOOT, row(duration=7200))   # 7200 s = 120 min
    assert s["confidence"] == 0.9
    assert s["runtime_delta"] == -29   # 120 - 149


def test_score_runtime_grossly_short_is_rejected():
    # 30 min is below 50% of 149 min -> clip/trailer, rejected outright
    s = score_match(BOOT, row(duration=1800))   # 1800 s = 30 min
    assert s["rejected"] is True
    assert s["confidence"] == 0.0


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


def test_tmdb_movie_passes_configured_timeout(monkeypatch):
    seen = {}

    def fake_get(url, timeout=None):
        seen["timeout"] = timeout
        return json.dumps(TMDB_BOOT).encode("utf-8")

    monkeypatch.setattr(theke.core, "http_get", fake_get)
    tmdb_movie(Config(tmdb_api_key="KEY", download_timeout=44), 1234)
    assert seen["timeout"] == 44


def test_tmdb_movie_parses_titles_year_runtime(monkeypatch):
    seen = {}

    def fake_get(url, timeout=None):
        seen["url"] = url
        return json.dumps(TMDB_BOOT).encode("utf-8")

    monkeypatch.setattr(theke.core, "http_get", fake_get)
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


def test_find_matches_year_tolerance_override(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "m1", "Das Boot", 1985, 8940)   # delta 4 from BOOT's 1981
        assert find_matches(conn, BOOT, min_conf=0.6) == []        # default 2 rejects
        matches = find_matches(conn, BOOT, min_conf=0.6, year_tolerance=5)
        assert [m["mediathek_id"] for m in matches] == ["m1"]
        assert matches[0]["confidence"] == 0.88   # 1-0.03*4
    finally:
        conn.close()


def test_find_matches_year_prefilter_skips_far_rows(tmp_path, caplog):
    # #1: rows outside the year window are never scored in Python (SQL prunes
    # them), but jahrlose rows survive (no year gate). BOOT is 1981, tol 2 ->
    # window 1979..1983: m1 (in) and m3 (no year) are scanned, m2 (1995) is not.
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "m1", "Das Boot", 1981, 8940)
        insert_movie(conn, "m2", "Das Boot", 1995, 8940)
        insert_movie(conn, "m3", "Das Boot", None, 8940)
        with caplog.at_level(logging.DEBUG, logger="theke"):
            matches = find_matches(conn, BOOT, min_conf=0.6)
        assert [m["mediathek_id"] for m in matches] == ["m1", "m3"]
    finally:
        conn.close()
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("find_matches: scanned 2") for m in msgs)   # m2 never scored


def test_find_matches_normalizes_each_title_once_across_wishes(tmp_path, monkeypatch):
    # #2: a title is normalized once per process, not once per row per wish. Two
    # scans over the same rows must not re-normalize an already-seen title.
    if hasattr(theke.match, "_match_forms"):
        theke.match._match_forms.cache_clear()
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "m1", "Das Boot", 1981, 8940)
        insert_movie(conn, "m2", "Heat", 1981, 6000)   # in-window, title misses
        calls = []
        real = theke.match.normalize
        monkeypatch.setattr(theke.match, "normalize",
                            lambda t: (calls.append(t), real(t))[1])
        find_matches(conn, BOOT, min_conf=0.6)
        find_matches(conn, BOOT, min_conf=0.6)   # second wish, same rows
    finally:
        conn.close()
    assert calls.count("Das Boot") == 1   # tmdb title + m1 row + rescan -> still 1
    assert calls.count("Heat") == 1


def test_find_matches_excludes_trailers(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "m1", "Das Boot", 1981, 8940)   # would score 1.0
        conn.execute("UPDATE mediathek SET flags='T' WHERE mediathek_id='m1'")
        assert find_matches(conn, BOOT, min_conf=0.6) == []
    finally:
        conn.close()


def test_find_matches_only_status_1(tmp_path):
    # match only touches enriched-and-unmatched rows: a status '0' (not yet
    # enriched) and a status '2' (already matched) row are both skipped, even
    # though they'd otherwise score 1.0.
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "m1", "Das Boot", 1981, 8940)   # status '1' -> match
        insert_movie(conn, "m0", "Das Boot", 1981, 8940)
        conn.execute("UPDATE mediathek SET status='0' WHERE mediathek_id='m0'")
        insert_movie(conn, "m2", "Das Boot", 1981, 8940)
        conn.execute("UPDATE mediathek SET status='2' WHERE mediathek_id='m2'")
        matches = find_matches(conn, BOOT, min_conf=0.6)
        assert [m["mediathek_id"] for m in matches] == ["m1"]
    finally:
        conn.close()


# -- cmd_match (CLI write/read side) -----------------------------------------

CFG = Config(tmdb_api_key="KEY")


def margs(match_cmd="run", tmdb="1234", type="movie", dry_run=False,
          min_conf=None, limit=20, json=False, year_tolerance=None):
    return SimpleNamespace(match_cmd=match_cmd, tmdb=tmdb, type=type,
                           dry_run=dry_run, min_conf=min_conf, limit=limit,
                           json=json, year_tolerance=year_tolerance)


def boot_db(tmp_path, monkeypatch):
    """A DB with two matching movie rows (m1 exact, m2 substring) + one miss,
    and http_get stubbed to the canned Das Boot payload."""
    monkeypatch.setattr(theke.core, "http_get",
                        lambda url, timeout=None: json.dumps(TMDB_BOOT).encode("utf-8"))
    conn = open_db(tmp_path)
    insert_movie(conn, "m1", "Das Boot", 1981, 8940)
    insert_movie(conn, "m2", "Das Boot Extended", 1981, 9000)
    insert_movie(conn, "m3", "Heat", 1995, 6000)   # rejected (title floor)
    return conn


def tmdb_of(conn, mediathek_id):
    return conn.execute("SELECT tmdb_id, match_confidence FROM mediathek "
                        "WHERE mediathek_id=?", (mediathek_id,)).fetchone()


def status_of(conn, mediathek_id):
    return conn.execute("SELECT status FROM mediathek WHERE mediathek_id=?",
                        (mediathek_id,)).fetchone()["status"]


def test_cmd_match_run_writes_id_and_confidence(tmp_path, monkeypatch):
    conn = boot_db(tmp_path, monkeypatch)
    try:
        result = cmd_match(conn, CFG, margs())
        assert result == {"tmdb_id": "1234", "title": "Das Boot",
                          "candidates": 2, "written": 2, "arte_linked": 0}
        assert tuple(tmdb_of(conn, "m1")) == ("1234", 1.0)
        assert tuple(tmdb_of(conn, "m2")) == ("1234", 0.95)
        assert tmdb_of(conn, "m3")["tmdb_id"] == ""   # rejected, untouched
        assert status_of(conn, "m1") == "2"           # written -> matched
        assert status_of(conn, "m2") == "2"
        assert status_of(conn, "m3") == "1"           # rejected, status untouched
    finally:
        conn.close()


def test_cmd_match_run_dry_run_writes_nothing(tmp_path, monkeypatch):
    conn = boot_db(tmp_path, monkeypatch)
    try:
        result = cmd_match(conn, CFG, margs(dry_run=True))
        assert result["candidates"] == 2 and result["written"] == 0
        assert tmdb_of(conn, "m1")["tmdb_id"] == ""
        assert status_of(conn, "m1") == "1"   # nothing written, status untouched
    finally:
        conn.close()


def test_cmd_match_run_keeps_existing_other_id(tmp_path, monkeypatch):
    conn = boot_db(tmp_path, monkeypatch)
    try:
        conn.execute("UPDATE mediathek SET tmdb_id='999' WHERE mediathek_id='m1'")
        result = cmd_match(conn, CFG, margs())
        assert result["written"] == 1            # m2 written, m1 conflict-skipped
        assert tmdb_of(conn, "m1")["tmdb_id"] == "999"
        assert tmdb_of(conn, "m2")["tmdb_id"] == "1234"
        assert status_of(conn, "m1") == "1"      # conflict-skipped, status untouched
        assert status_of(conn, "m2") == "2"
    finally:
        conn.close()


def test_cmd_match_run_min_conf_override(tmp_path, monkeypatch):
    conn = boot_db(tmp_path, monkeypatch)
    try:
        result = cmd_match(conn, CFG, margs(min_conf=0.99))
        assert result == {"tmdb_id": "1234", "title": "Das Boot",
                          "candidates": 1, "written": 1, "arte_linked": 0}
    finally:
        conn.close()


def test_cmd_match_run_year_tolerance_override(tmp_path, monkeypatch):
    monkeypatch.setattr(theke.core, "http_get",
                        lambda url, timeout=None: json.dumps(TMDB_BOOT).encode("utf-8"))
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "m1", "Das Boot", 1985, 8940)   # delta 4, beyond default 2
        assert cmd_match(conn, CFG, margs())["candidates"] == 0      # default rejects
        result = cmd_match(conn, CFG, margs(year_tolerance=5))
        assert result["candidates"] == 1 and result["written"] == 1
        assert tmdb_of(conn, "m1")["tmdb_id"] == "1234"
    finally:
        conn.close()


def test_cmd_match_show_is_read_only(tmp_path, monkeypatch):
    conn = boot_db(tmp_path, monkeypatch)
    try:
        result = cmd_match(conn, CFG, margs(match_cmd="show", json=True))
        assert result["tmdb_id"] == "1234"
        assert [m["mediathek_id"] for m in result["matches"]] == ["m1", "m2"]
        assert tmdb_of(conn, "m1")["tmdb_id"] == ""   # nothing written
    finally:
        conn.close()


# -- match reset (status 2 -> 1) ---------------------------------------------

def reset_margs(status_only=False):
    return SimpleNamespace(match_cmd="reset", status_only=status_only)


def test_cmd_match_reset_flips_status_and_clears_id(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "m1", "Das Boot", 1981, 8940)
        conn.execute("UPDATE mediathek SET status='2', tmdb_id='1234', "
                     "match_confidence=1.0 WHERE mediathek_id='m1'")
        result = cmd_match(conn, CFG, reset_margs())
        assert result == {"reset": 1}
        row = conn.execute("SELECT * FROM mediathek WHERE mediathek_id='m1'").fetchone()
        assert row["status"] == "1"
        assert row["tmdb_id"] == ""
        assert row["match_confidence"] is None
        assert row["clean_title"] == "Das Boot"   # enrich data kept
    finally:
        conn.close()


def test_cmd_match_reset_status_only_keeps_id(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "m1", "Das Boot", 1981, 8940)
        conn.execute("UPDATE mediathek SET status='2', tmdb_id='1234', "
                     "match_confidence=1.0 WHERE mediathek_id='m1'")
        result = cmd_match(conn, CFG, reset_margs(status_only=True))
        assert result == {"reset": 1}
        row = conn.execute("SELECT * FROM mediathek WHERE mediathek_id='m1'").fetchone()
        assert row["status"] == "1"
        assert row["tmdb_id"] == "1234"          # untouched
        assert row["match_confidence"] == 1.0
    finally:
        conn.close()


def test_cmd_match_reset_leaves_non_matched_rows(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "m1", "Das Boot", 1981, 8940)   # status '1'
        result = cmd_match(conn, CFG, reset_margs())
        assert result == {"reset": 0}                       # nothing at status '2'
        assert status_of(conn, "m1") == "1"
    finally:
        conn.close()


def test_cmd_match_reset_needs_no_api_key(tmp_path):
    # reset is a pure DB op -> no TMDB key required (unlike run/show)
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "m1", "Das Boot", 1981, 8940)
        conn.execute("UPDATE mediathek SET status='2' WHERE mediathek_id='m1'")
        assert cmd_match(conn, Config(), reset_margs()) == {"reset": 1}
    finally:
        conn.close()


def test_cmd_match_requires_api_key(tmp_path, monkeypatch):
    conn = boot_db(tmp_path, monkeypatch)
    try:
        with pytest.raises(ConfigError, match="API key"):
            cmd_match(conn, Config(), margs())
    finally:
        conn.close()


# -- cmd_match (series episodes) ---------------------------------------------

def tv_margs(match_cmd="run", tmdb="55", season=2, episode=6, dry_run=False,
             min_conf=None, limit=20, json=False, year_tolerance=None):
    return SimpleNamespace(match_cmd=match_cmd, tmdb=tmdb, type="series",
                           season=season, episode=episode, dry_run=dry_run,
                           min_conf=min_conf, limit=limit, json=json,
                           year_tolerance=year_tolerance)


def tv_db(tmp_path, monkeypatch):
    """A DB with two matching Tatort episodes (e1 exact, e5 runtime-penalized) +
    one same-S/E miss (wrong series), and http_get stubbed to the canned payloads."""
    monkeypatch.setattr(theke.core, "http_get", fake_tv_get([]))
    conn = open_db(tmp_path)
    insert_episode(conn, "e1", "Der rote Schatten", "Tatort", 2, 6)         # 1.0
    insert_episode(conn, "e5", "Der rote Schatten", "Tatort", 2, 6, 4200)   # 0.9 (runtime)
    insert_episode(conn, "e2", "Der rote Schatten", "Lindenstrasse", 2, 6)  # series floor -> out
    return conn


def test_cmd_match_run_series_writes_id_and_confidence(tmp_path, monkeypatch):
    conn = tv_db(tmp_path, monkeypatch)
    try:
        result = cmd_match(conn, CFG, tv_margs())
        assert result == {"tmdb_id": "55", "title": "Der rote Schatten",
                          "series": "Tatort", "candidates": 2, "written": 2,
                          "arte_linked": 0}
        assert tuple(tmdb_of(conn, "e1")) == ("55", 1.0)
        assert tuple(tmdb_of(conn, "e5")) == ("55", 0.9)
        assert tmdb_of(conn, "e2")["tmdb_id"] == ""   # wrong series, untouched
        assert status_of(conn, "e1") == "2"
        assert status_of(conn, "e5") == "2"
        assert status_of(conn, "e2") == "1"
    finally:
        conn.close()


def test_cmd_match_run_series_dry_run_writes_nothing(tmp_path, monkeypatch):
    conn = tv_db(tmp_path, monkeypatch)
    try:
        result = cmd_match(conn, CFG, tv_margs(dry_run=True))
        assert result["candidates"] == 2 and result["written"] == 0
        assert tmdb_of(conn, "e1")["tmdb_id"] == ""
        assert status_of(conn, "e1") == "1"
    finally:
        conn.close()


def test_cmd_match_series_requires_season_and_episode(tmp_path, monkeypatch):
    conn = tv_db(tmp_path, monkeypatch)
    try:
        with pytest.raises(ValueError, match="season"):
            cmd_match(conn, CFG, tv_margs(season=None))
        with pytest.raises(ValueError, match="episode"):
            cmd_match(conn, CFG, tv_margs(episode=None))
    finally:
        conn.close()


def test_cmd_match_show_series_is_read_only(tmp_path, monkeypatch):
    conn = tv_db(tmp_path, monkeypatch)
    try:
        result = cmd_match(conn, CFG, tv_margs(match_cmd="show", json=True))
        assert result["tmdb_id"] == "55"
        assert result["title"] == "Der rote Schatten"
        assert result["series"] == "Tatort"
        assert [m["mediathek_id"] for m in result["matches"]] == ["e1", "e5"]
        assert tmdb_of(conn, "e1")["tmdb_id"] == ""   # nothing written
    finally:
        conn.close()


# -- series episodes: tmdb_tv (two-call IO via monkeypatched http_get) --------

TMDB_TATORT = {
    "name":          "Tatort",
    "original_name": "Tatort",
    "alternative_titles": {"results": [
        {"iso_3166_1": "US", "title": "Scene of the Crime"},
        {"iso_3166_1": "DE", "title": "Tatort (Krimireihe)"},
    ]},
}

TMDB_TATORT_EP = {
    "name":      "Der rote Schatten",
    "runtime":   89,
    "air_date":  "2017-03-19",
    "translations": {"translations": [
        {"iso_3166_1": "US", "iso_639_1": "en", "data": {"name": "The Red Shadow"}},
        {"iso_3166_1": "DE", "iso_639_1": "de", "data": {"name": "Der rote Schatten"}},
    ]},
}


def fake_tv_get(seen):
    """http_get stub: the episode payload for /season/.../episode/ URLs, the
    series payload otherwise. Appends every URL seen to `seen`."""
    def get(url, timeout=None):
        seen.append(url)
        body = TMDB_TATORT_EP if "/season/" in url else TMDB_TATORT
        return json.dumps(body).encode("utf-8")
    return get


def test_tmdb_tv_parses_series_episode(monkeypatch):
    seen = []
    monkeypatch.setattr(theke.core, "http_get", fake_tv_get(seen))
    meta = tmdb_tv(Config(tmdb_api_key="KEY"), 55, 2, 6)

    # series + episode endpoints both hit, with id/key/season/episode in the URLs.
    assert any("/tv/55?" in u and "KEY" in u for u in seen)
    assert any("/tv/55/season/2/episode/6?" in u for u in seen)
    # series: original_name deduped, US dropped, DE alternative kept.
    assert meta["series_titles"] == ["Tatort", "Tatort (Krimireihe)"]
    assert meta["series_title"] == "Tatort"
    # episode: name + the translated (US) name, the DE dup folded out.
    assert meta["episode_name"] == "Der rote Schatten"
    assert meta["episode_titles"] == ["Der rote Schatten", "The Red Shadow"]
    assert meta["runtime"] == 89
    assert meta["year"] == 2017          # from air_date
    assert meta["season"] == 2 and meta["episode"] == 6
    assert meta["tmdb_id"] == "55"


# -- series episodes: score_episode ------------------------------------------

# series-name + (season, episode) are gates; episode-title + runtime confirm.
TATORT = {"series_titles": ["Tatort"], "episode_titles": ["Der rote Schatten"],
          "episode_name": "Der rote Schatten", "series_title": "Tatort",
          "runtime": 89, "year": 2017, "season": 2, "episode": 6, "tmdb_id": "55"}


def erow(series_name="Tatort", clean_title="Der rote Schatten", season=2,
         episode=6, duration=5340):       # 5340 s = 89 min
    return {"series_name": series_name, "clean_title": clean_title,
            "season": season, "episode": episode, "duration": duration}


def test_score_episode_exact_match_is_one():
    s = score_episode(TATORT, erow())
    assert s["rejected"] is False
    assert s["confidence"] == 1.0
    assert s["series_sim"] == 1.0
    assert s["episode_title_sim"] == 1.0


def test_score_episode_wrong_season_is_rejected():
    s = score_episode(TATORT, erow(season=3))
    assert s["rejected"] is True
    assert s["confidence"] == 0.0


def test_score_episode_wrong_episode_is_rejected():
    s = score_episode(TATORT, erow(episode=7))
    assert s["rejected"] is True
    assert s["confidence"] == 0.0


def test_score_episode_series_below_floor_is_rejected():
    # right S/E, but the series name does not match the wanted series.
    s = score_episode(TATORT, erow(series_name="Lindenstrasse"))
    assert s["rejected"] is True
    assert s["confidence"] == 0.0


def test_score_episode_runtime_off_soft_penalty():
    # 70 min vs 89 min: within the floor but beyond tolerance -> factor 0.90.
    # series 1.0 * runtime 0.90 = 0.9; delta = 70 - 89 = -19.
    s = score_episode(TATORT, erow(duration=4200))   # 4200 s = 70 min
    assert s["confidence"] == 0.9
    assert s["runtime_delta"] == -19


def test_score_episode_grossly_short_is_rejected():
    # 20 min is below 50% of 89 min -> clip, rejected outright.
    s = score_episode(TATORT, erow(duration=1200))   # 1200 s = 20 min
    assert s["rejected"] is True
    assert s["confidence"] == 0.0


# -- series episodes: find_episode_matches -----------------------------------

def insert_episode(conn, mediathek_id, clean_title, series_name, season, episode,
                   duration=5340, category="Episode"):
    conn.execute(
        "INSERT INTO mediathek (status, mediathek_id, category, clean_title, "
        "series_name, season, episode, duration) VALUES ('1',?,?,?,?,?,?,?)",
        (mediathek_id, category, clean_title, series_name, season, episode, duration))


def test_find_episode_matches_selects_triple_scores_and_sorts(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_episode(conn, "e1", "Der rote Schatten", "Tatort", 2, 6)            # 1.0
        insert_episode(conn, "e5", "Der rote Schatten", "Tatort", 2, 6, 4200)      # 0.9 (rt)
        insert_episode(conn, "e2", "Der rote Schatten", "Lindenstrasse", 2, 6)     # series out
        insert_episode(conn, "e3", "Der rote Schatten", "Tatort", 2, 7)            # wrong episode
        insert_episode(conn, "e4", "Der rote Schatten", "Tatort", 3, 6)            # wrong season
        insert_movie(conn, "m1", "Der rote Schatten", 2017, 5340)                  # not an episode
        matches = find_episode_matches(conn, TATORT, min_conf=0.6)
        assert [m["mediathek_id"] for m in matches] == ["e1", "e5"]
        assert matches[0]["confidence"] == 1.0
        assert matches[1]["confidence"] == 0.9
    finally:
        conn.close()


def test_find_episode_matches_respects_min_conf(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_episode(conn, "e1", "Der rote Schatten", "Tatort", 2, 6)
        insert_episode(conn, "e5", "Der rote Schatten", "Tatort", 2, 6, 4200)
        matches = find_episode_matches(conn, TATORT, min_conf=0.95)
        assert [m["mediathek_id"] for m in matches] == ["e1"]
    finally:
        conn.close()


def test_find_episode_matches_excludes_trailers(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_episode(conn, "e1", "Der rote Schatten", "Tatort", 2, 6)
        conn.execute("UPDATE mediathek SET flags='T' WHERE mediathek_id='e1'")
        assert find_episode_matches(conn, TATORT, min_conf=0.6) == []
    finally:
        conn.close()


def test_find_episode_matches_only_status_1(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_episode(conn, "e1", "Der rote Schatten", "Tatort", 2, 6)   # match
        insert_episode(conn, "e0", "Der rote Schatten", "Tatort", 2, 6)
        conn.execute("UPDATE mediathek SET status='0' WHERE mediathek_id='e0'")
        insert_episode(conn, "e2", "Der rote Schatten", "Tatort", 2, 6)
        conn.execute("UPDATE mediathek SET status='2' WHERE mediathek_id='e2'")
        matches = find_episode_matches(conn, TATORT, min_conf=0.6)
        assert [m["mediathek_id"] for m in matches] == ["e1"]
    finally:
        conn.close()


# -- arte second pass (language-variant linking by video-id) -----------------

def test_is_arte_sender_matches_language_variants():
    assert is_arte_sender("ARTE.DE")
    assert is_arte_sender("ARTE.FR")
    assert is_arte_sender("arte.es")        # case-insensitive
    assert not is_arte_sender("ARTE")       # no language code
    assert not is_arte_sender("ARTE.DE.X")  # not a plain language variant
    assert not is_arte_sender("ZDF")
    assert not is_arte_sender(None)


def test_arte_video_id_extracts_shared_id():
    # the new /videos/ form and the older /guide/xx/ form carry the same token
    assert arte_video_id(
        "https://www.arte.tv/de/videos/116786-000-A/ein-balkon/") == "116786-000-A"
    assert arte_video_id(
        "http://www.arte.tv/guide/fr/067846-009-A/offene-karten") == "067846-009-A"


def test_arte_video_id_absent_is_none():
    assert arte_video_id("https://www.arte.tv/de/") is None
    assert arte_video_id("") is None
    assert arte_video_id(None) is None


def insert_arte(conn, mediathek_id, clean_title, sender, url_website,
                year=1981, duration=8940, category="Movie"):
    conn.execute(
        "INSERT INTO mediathek (status, mediathek_id, sender, category, "
        "clean_title, year, duration, url_website) VALUES ('1',?,?,?,?,?,?,?)",
        (mediathek_id, sender, category, clean_title, year, duration, url_website))


def test_arte_anchor_ids_seeds_from_arte_matches_only(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_arte(conn, "a1", "Das Boot", "ARTE.DE",
                    "https://www.arte.tv/de/videos/100000-000-A/das-boot/")
        insert_movie(conn, "z1", "Das Boot", 1981, 8940)   # non-arte (sender NULL)
        matches = [{"mediathek_id": "a1", "confidence": 1.0},
                   {"mediathek_id": "z1", "confidence": 0.97}]
        assert arte_anchor_ids(conn, matches) == {"100000-000-A": 1.0}
    finally:
        conn.close()


def test_arte_anchor_ids_empty_without_arte(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "z1", "Das Boot", 1981, 8940)
        assert arte_anchor_ids(conn, [{"mediathek_id": "z1", "confidence": 1.0}]) == {}
    finally:
        conn.close()


def test_find_arte_links_fans_out_to_variants(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_arte(conn, "a1", "Das Boot", "ARTE.DE",
                    "https://www.arte.tv/de/videos/100000-000-A/das-boot/")
        insert_arte(conn, "a2", "Le Bateau", "ARTE.FR",
                    "https://www.arte.tv/fr/videos/100000-000-A/le-bateau/")
        insert_arte(conn, "a3", "El Submarino", "ARTE.ES",
                    "https://www.arte.tv/es/videos/100000-000-A/el-submarino/")
        insert_arte(conn, "x1", "Andere", "ARTE.FR",          # different id -> skip
                    "https://www.arte.tv/fr/videos/999999-000-A/andere/")
        links = find_arte_links(conn, {"100000-000-A": 1.0}, exclude_ids={"a1"})
        assert [l["mediathek_id"] for l in links] == ["a2", "a3"]
        assert all(l["confidence"] == 1.0 for l in links)
        assert all(l["arte_video_id"] == "100000-000-A" for l in links)
    finally:
        conn.close()


def test_find_arte_links_only_status_1(tmp_path):
    # the second pass also touches only status '1' rows: a variant already
    # matched (status '2') is left alone, even when it shares the anchor's id.
    conn = open_db(tmp_path)
    try:
        insert_arte(conn, "a1", "Das Boot", "ARTE.DE",
                    "https://www.arte.tv/de/videos/100000-000-A/das-boot/")
        insert_arte(conn, "a2", "Le Bateau", "ARTE.FR",
                    "https://www.arte.tv/fr/videos/100000-000-A/le-bateau/")
        insert_arte(conn, "a3", "El Submarino", "ARTE.ES",
                    "https://www.arte.tv/es/videos/100000-000-A/el-submarino/")
        conn.execute("UPDATE mediathek SET status='2' WHERE mediathek_id='a2'")
        links = find_arte_links(conn, {"100000-000-A": 1.0}, exclude_ids={"a1"})
        assert [l["mediathek_id"] for l in links] == ["a3"]
    finally:
        conn.close()


def test_find_arte_links_empty_without_anchors(tmp_path):
    conn = open_db(tmp_path)
    try:
        insert_arte(conn, "a1", "Das Boot", "ARTE.DE",
                    "https://www.arte.tv/de/videos/100000-000-A/das-boot/")
        assert find_arte_links(conn, {}, exclude_ids=set()) == []
    finally:
        conn.close()


def arte_boot_db(tmp_path, monkeypatch):
    """A German Arte hit (a1, matches by title) plus two foreign-language variants
    (a2/a3) the title pass cannot reach, all sharing one video-id."""
    monkeypatch.setattr(theke.core, "http_get",
                        lambda url, timeout=None: json.dumps(TMDB_BOOT).encode("utf-8"))
    conn = open_db(tmp_path)
    insert_arte(conn, "a1", "Das Boot", "ARTE.DE",
                "https://www.arte.tv/de/videos/100000-000-A/das-boot/")
    insert_arte(conn, "a2", "Le Bateau", "ARTE.FR",
                "https://www.arte.tv/fr/videos/100000-000-A/le-bateau/")
    insert_arte(conn, "a3", "El Submarino", "ARTE.ES",
                "https://www.arte.tv/es/videos/100000-000-A/el-submarino/")
    return conn


def test_cmd_match_run_links_arte_language_variants(tmp_path, monkeypatch):
    conn = arte_boot_db(tmp_path, monkeypatch)
    try:
        result = cmd_match(conn, CFG, margs())
        assert result == {"tmdb_id": "1234", "title": "Das Boot",
                          "candidates": 1, "written": 3, "arte_linked": 2}
        assert tuple(tmdb_of(conn, "a1")) == ("1234", 1.0)   # pass-1 German hit
        assert tuple(tmdb_of(conn, "a2")) == ("1234", 1.0)   # variants inherit conf
        assert tuple(tmdb_of(conn, "a3")) == ("1234", 1.0)
        assert status_of(conn, "a2") == "2" and status_of(conn, "a3") == "2"
    finally:
        conn.close()


def test_cmd_match_run_arte_dry_run_reports_links_writes_nothing(tmp_path, monkeypatch):
    # arte_linked previews the second pass even in --dry-run (like candidates);
    # written stays 0 and the DB is untouched.
    conn = arte_boot_db(tmp_path, monkeypatch)
    try:
        result = cmd_match(conn, CFG, margs(dry_run=True))
        assert result["candidates"] == 1
        assert result["arte_linked"] == 2   # a2 + a3 would be linked
        assert result["written"] == 0
        assert tmdb_of(conn, "a2")["tmdb_id"] == ""
    finally:
        conn.close()


# Real Arte case: "Mysteries of Lisbon" (Raoul Ruiz, 2010) airs under six
# language senders sharing id 131183-000-A, with untranslatable titles AND
# slightly different durations (DE 14956 s vs 15340 s elsewhere) -- neither
# title nor runtime bridges them; only the shared video-id does. The payload is
# German-only (no Portuguese original_title) so every foreign variant is reached
# strictly by the id-link, not by an incidental title hit.
TMDB_LISBON = {
    "title":             "Die Geheimnisse von Lissabon",
    "original_title":    "Die Geheimnisse von Lissabon",
    "release_date":      "2010-08-26",
    "runtime":           256,                  # 15360 s, matches the DE duration
    "original_language": "de",
    "alternative_titles": {"titles": []},
}

LISBON_VARIANTS = [   # (mediathek_id, clean_title, sender, lang_slug)
    ("le", "Mysteries of Lisbon",                 "ARTE.EN", "en"),
    ("es", "Misterios de Lisboa",                 "ARTE.ES", "es"),
    ("fr", "Mysteres de Lisbonne",                "ARTE.FR", "fr"),
    ("it", "I misteri di Lisbona",                "ARTE.IT", "it"),
    ("pl", "Tajemnice Lizbony - (Misterios de Lisboa)", "ARTE.PL", "pl"),
]


def test_cmd_match_run_links_mysteries_of_lisbon(tmp_path, monkeypatch):
    monkeypatch.setattr(theke.core, "http_get",
                        lambda url, timeout=None: json.dumps(TMDB_LISBON).encode("utf-8"))
    conn = open_db(tmp_path)
    try:
        vid = "131183-000-A"
        insert_arte(conn, "de", "Die Geheimnisse von Lissabon", "ARTE.DE",
                    f"https://www.arte.tv/de/videos/{vid}/lissabon/",
                    year=2010, duration=15360)            # exact title+runtime -> 1.0
        for mid, title, sender, slug in LISBON_VARIANTS:
            insert_arte(conn, mid, title, sender,
                        f"https://www.arte.tv/{slug}/videos/{vid}/lisbon/",
                        year=None, duration=15340)        # foreign title, off runtime

        result = cmd_match(conn, CFG, margs(tmdb="49348"))
        assert result == {"tmdb_id": "49348",
                          "title": "Die Geheimnisse von Lissabon",
                          "candidates": 1, "written": 6, "arte_linked": 5}
        # the German row is the only title/runtime match
        assert tuple(tmdb_of(conn, "de")) == ("49348", 1.0)
        # all five language variants linked by id, inheriting the anchor's 1.0
        for mid, *_ in LISBON_VARIANTS:
            assert tuple(tmdb_of(conn, mid)) == ("49348", 1.0)
            assert status_of(conn, mid) == "2"
    finally:
        conn.close()


# -- scan timing (DEBUG, the per-wish full-scan cost) ------------------------

def test_find_matches_logs_scan_timing_at_debug(tmp_path, caplog):
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "m1", "Das Boot", 1981, 8940)
        insert_movie(conn, "m2", "Heat", 1981, 6000)   # in-window so it is scanned (title misses)
        with caplog.at_level(logging.DEBUG, logger="theke"):
            find_matches(conn, BOOT, min_conf=0.6)
    finally:
        conn.close()
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("find_matches: scanned 2") for m in msgs)   # rows scanned reported


def test_find_matches_scan_timing_silent_above_debug(tmp_path, caplog):
    conn = open_db(tmp_path)
    try:
        insert_movie(conn, "m1", "Das Boot", 1981, 8940)
        with caplog.at_level(logging.INFO, logger="theke"):
            find_matches(conn, BOOT, min_conf=0.6)
    finally:
        conn.close()
    assert not any("find_matches" in r.getMessage() for r in caplog.records)


def test_find_arte_links_logs_scan_timing_at_debug(tmp_path, caplog):
    conn = open_db(tmp_path)
    try:
        insert_arte(conn, "a1", "Das Boot", "ARTE.DE",
                    "https://www.arte.tv/de/videos/100000-000-A/das-boot/")
        insert_arte(conn, "a2", "Le Bateau", "ARTE.FR",
                    "https://www.arte.tv/fr/videos/100000-000-A/le-bateau/")
        with caplog.at_level(logging.DEBUG, logger="theke"):
            find_arte_links(conn, {"100000-000-A": 1.0}, exclude_ids={"a1"})
    finally:
        conn.close()
    assert any(r.getMessage().startswith("find_arte_links: scanned 2")
               for r in caplog.records)
