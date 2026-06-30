"""Tests for the wishlist / library stage (phase 9): the library table,
cmd_library (add/list/remove), download -> library recording, and the
single pipeline pass (_run_pass, the body the scheduler loops)."""

import json
import logging
from types import SimpleNamespace

import pytest

import theke
from theke import *
from theke import (cmd_library, cmd_queue, _run_pass, db_connect, main,
                   tmdb_search, pick_by_year)


# -- helpers -----------------------------------------------------------------

# original_language 'en', title "Mein Film", year 2020, runtime 100 min.
TMDB = {"title": "Mein Film", "original_title": "My Film",
        "release_date": "2020-05-01", "runtime": 100, "original_language": "en",
        "alternative_titles": {"titles": []}}

CFG = Config(tmdb_api_key="KEY", languages=["de"])

# A /search/movie response: popularity-ordered, the wanted film first.
SEARCH = {"results": [
    {"id": 9268, "title": "Die Klapperschlange", "release_date": "1981-04-22"},
    {"id": 999,  "title": "Escape Remake",       "release_date": "2013-01-01"},
]}


def open_db(tmp_path):
    return db_connect(str(tmp_path / "theke.db"))


def stub_tmdb(monkeypatch):
    monkeypatch.setattr(theke.core, "http_get",
                        lambda url, timeout=None: json.dumps(TMDB).encode("utf-8"))


def stub_search(monkeypatch, payload=SEARCH):
    monkeypatch.setattr(theke.core, "http_get",
                        lambda url, timeout=None: json.dumps(payload).encode("utf-8"))


def insert_movie(conn, mediathek_id, tmdb_id="100", language="de", duration=6000,
                 url_video="http://v", clean_title="Mein Film", year=2020,
                 status="1"):
    """A movie row (category 'Movie') so find_matches can pick it; duration is in
    seconds -- 6000 s = 100 min, matching the TMDB runtime above."""
    cols = dict(status=status, mediathek_id=mediathek_id, tmdb_id=tmdb_id,
                language=language, duration=duration, url_video=url_video,
                category="Movie", clean_title=clean_title, year=year)
    conn.execute(f"INSERT INTO mediathek ({','.join(cols)}) VALUES "
                 f"({','.join(':' + k for k in cols)})", cols)


def library_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM library ORDER BY tmdb_id")]


def libargs(library_cmd="list", tmdb=None, all=False, status=None, json=False,
            title=None, year=None, year_tolerance=None, path=None, format=None,
            mode="auto", tmdb_list=None):
    return SimpleNamespace(library_cmd=library_cmd, tmdb=tmdb, all=all,
                           status=status, json=json, title=title, year=year,
                           year_tolerance=year_tolerance, path=path,
                           format=format, mode=mode, tmdb_list=tmdb_list)


def write_file(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


# A v3 /list/{id} response: two movies + one tv item (skipped, library is
# movies-only). Years derive from release_date / first_air_date.
TMDB_LIST = {"items": [
    {"id": 100, "title": "Mein Film",  "release_date": "2020-05-01", "media_type": "movie"},
    {"id": 200, "title": "Zweiter",    "release_date": "1999-01-01", "media_type": "movie"},
    {"id": 300, "name":  "Eine Serie", "first_air_date": "2010-01-01", "media_type": "tv"},
]}


def stub_list(monkeypatch, payload=TMDB_LIST):
    monkeypatch.setattr(theke.core, "http_get",
        lambda url, timeout=None, headers=None: json.dumps(payload).encode("utf-8"))


def stub_import(monkeypatch):
    """http_get branching by URL for import resolution: /movie/999 is unknown
    (404 -> raises), any other /movie/<id> is the valid TMDB film (title "Mein
    Film", year 2020), and /search/movie yields SEARCH (first hit id 9268, 1981)
    unless the query mentions 'Nonexistent' (then no results)."""
    def fake(url, timeout=None):
        if "/movie/999" in url:
            raise RuntimeError("404 Not Found")
        if "/movie/" in url:
            return json.dumps(TMDB).encode("utf-8")
        if "/search/movie" in url:
            payload = {"results": []} if "Nonexistent" in url else SEARCH
            return json.dumps(payload).encode("utf-8")
        raise AssertionError(f"unexpected url {url}")
    monkeypatch.setattr(theke.core, "http_get", fake)


def qargs(queue_cmd, **kw):
    base = dict(tmdb=None, mediathek_id=None, language=None, resolution=None,
                remux=None, url=None, path=None, url_subtitle=None, status=None,
                ids=[], all=False, force=False, cancelled=False, done=False,
                failed=False, json=False)
    base.update(kw)
    return SimpleNamespace(queue_cmd=queue_cmd, **base)


def _fake_dl(url, out, retries, timeout=None, stall_timeout=0):
    with open(out, "wb") as fh:
        fh.write(b"SRC")
    return 3


def _fake_remux(ffmpeg_path, in_path, mode, out_path, language=None):
    with open(out_path, "wb") as fh:
        fh.write(b"MUX")
    return 3


def download_cfg(tmp_path, **kw):
    lib = (tmp_path / "lib").as_posix() + "/{Title} ({Year})/{Title} ({Year}).mp4"
    kw.setdefault("languages", ["de"])
    return Config(tmdb_api_key="KEY", temp_path=str(tmp_path / "scratch"),
                  library_path=lib, **kw)


def stub_files(monkeypatch):
    monkeypatch.setattr(theke, "download_file", _fake_dl)
    monkeypatch.setattr(theke, "run_remux", _fake_remux)


def stub_stages(monkeypatch):
    """No-op fetch + enrich so _run_pass exercises only the wish loop."""
    monkeypatch.setattr(theke, "cmd_fetch", lambda conn, cfg, args: {"action": "skip"})
    monkeypatch.setattr(theke, "_enrich_run", lambda conn, cfg, args: {"enriched": 0})


# -- migration ---------------------------------------------------------------

def test_library_migration_creates_table(tmp_path):
    conn = open_db(tmp_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(library)")}
        assert cols == {"tmdb_id", "status", "title", "year", "path",
                        "created_at", "updated_at"}
    finally:
        conn.close()


# -- cmd_library add / list / remove -----------------------------------------

def test_library_add_inserts_wish(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        result = cmd_library(conn, CFG, libargs("add", tmdb=["100"]))
        assert result == {"added": 1, "skipped": 0}
        rows = library_rows(conn)
        assert len(rows) == 1
        assert rows[0]["tmdb_id"] == "100"
        assert rows[0]["status"] == "W"
        assert rows[0]["title"] == "Mein Film"   # captured from TMDB at add
        assert rows[0]["year"] == 2020            # release year from TMDB
    finally:
        conn.close()


def test_library_add_idempotent(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cmd_library(conn, CFG, libargs("add", tmdb=["100"]))
        result = cmd_library(conn, CFG, libargs("add", tmdb=["100"]))
        assert result == {"added": 0, "skipped": 1}
        assert len(library_rows(conn)) == 1
    finally:
        conn.close()


def test_library_add_invalid_tmdb_id_raises(tmp_path, monkeypatch):
    # An invalid id makes TMDB answer 404 -> http_get raises; add must propagate
    # it (not silently insert a bogus wish) and leave the table untouched.
    def boom(url, timeout=None):
        raise RuntimeError("404 Not Found")
    monkeypatch.setattr(theke.core, "http_get", boom)
    conn = open_db(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            cmd_library(conn, CFG, libargs("add", tmdb=["999999"]))
        assert library_rows(conn) == []
    finally:
        conn.close()


def test_library_add_without_key_leaves_title_empty(tmp_path):
    conn = open_db(tmp_path)
    try:
        cmd_library(conn, Config(), libargs("add", tmdb=["100"]))
        assert library_rows(conn)[0]["title"] == ""
        assert library_rows(conn)[0]["year"] is None
    finally:
        conn.close()


def test_tmdb_list_parses_movie_and_tv_items(monkeypatch):
    stub_list(monkeypatch)
    items = theke.tmdb_list(CFG, "7")
    assert items == [
        {"tmdb_id": "100", "title": "Mein Film",  "year": 2020, "media_type": "movie"},
        {"tmdb_id": "200", "title": "Zweiter",    "year": 1999, "media_type": "movie"},
        {"tmdb_id": "300", "title": "Eine Serie", "year": 2010, "media_type": "tv"},
    ]


def test_tmdb_list_paginates_over_total_pages(monkeypatch):
    # a paged response (total_pages 2) is followed page by page until exhausted.
    pages = {
        1: {"results": [{"id": 1, "title": "A", "release_date": "2001-01-01",
                         "media_type": "movie"}], "total_pages": 2},
        2: {"results": [{"id": 2, "title": "B", "release_date": "2002-01-01",
                         "media_type": "movie"}], "total_pages": 2},
    }
    def fake(url, timeout=None, headers=None):
        page = 2 if "page=2" in url else 1
        return json.dumps(pages[page]).encode("utf-8")
    monkeypatch.setattr(theke.core, "http_get", fake)
    items = theke.tmdb_list(CFG, "7")
    assert [it["tmdb_id"] for it in items] == ["1", "2"]


def test_tmdb_list_uses_bearer_when_token_set(monkeypatch):
    seen = {}
    def fake(url, timeout=None, headers=None):
        seen["url"], seen["headers"] = url, headers
        return json.dumps({"items": []}).encode("utf-8")
    monkeypatch.setattr(theke.core, "http_get", fake)
    theke.tmdb_list(Config(tmdb_read_token="TOK"), "7")
    assert seen["headers"] == {"Authorization": "Bearer TOK"}
    assert "api_key" not in seen["url"]


def test_tmdb_list_uses_api_key_without_token(monkeypatch):
    seen = {}
    def fake(url, timeout=None, headers=None):
        seen["url"], seen["headers"] = url, headers
        return json.dumps({"items": []}).encode("utf-8")
    monkeypatch.setattr(theke.core, "http_get", fake)
    theke.tmdb_list(Config(tmdb_api_key="KEY"), "7")
    assert seen["headers"] is None
    assert "api_key=KEY" in seen["url"]


def test_library_add_tmdb_list_adds_movies_skips_series(tmp_path, monkeypatch, caplog):
    stub_list(monkeypatch)
    conn = open_db(tmp_path)
    try:
        with caplog.at_level("WARNING"):
            result = cmd_library(conn, CFG, libargs("add", tmdb_list=["7"]))
        assert result == {"added": 2, "skipped": 0, "series_skipped": 1}
        rows = library_rows(conn)
        assert [r["tmdb_id"] for r in rows] == ["100", "200"]
        assert all(r["status"] == "W" for r in rows)
        assert rows[0]["title"] == "Mein Film" and rows[0]["year"] == 2020
        assert any("series" in r.message for r in caplog.records)   # warned on the skip
    finally:
        conn.close()


def test_library_add_tmdb_list_idempotent(tmp_path, monkeypatch):
    stub_list(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cmd_library(conn, CFG, libargs("add", tmdb_list=["7"]))
        result = cmd_library(conn, CFG, libargs("add", tmdb_list=["7"]))
        assert result == {"added": 0, "skipped": 2, "series_skipped": 1}
        assert len(library_rows(conn)) == 2
    finally:
        conn.close()


def test_library_add_tmdb_list_without_credentials_raises(tmp_path):
    conn = open_db(tmp_path)
    try:
        with pytest.raises(ConfigError):
            cmd_library(conn, Config(), libargs("add", tmdb_list=["7"]))
        assert library_rows(conn) == []
    finally:
        conn.close()


def test_library_add_tmdb_list_and_tmdb_together_raises(tmp_path, monkeypatch):
    stub_list(monkeypatch)
    conn = open_db(tmp_path)
    try:
        with pytest.raises(ValueError):
            cmd_library(conn, CFG, libargs("add", tmdb=["100"], tmdb_list=["7"]))
    finally:
        conn.close()


def test_library_remove_by_tmdb(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cmd_library(conn, CFG, libargs("add", tmdb=["100", "200"]))
        result = cmd_library(conn, CFG, libargs("remove", tmdb=["100"]))
        assert result == {"removed": 1}
        assert [r["tmdb_id"] for r in library_rows(conn)] == ["200"]
    finally:
        conn.close()


def test_library_remove_all(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cmd_library(conn, CFG, libargs("add", tmdb=["100", "200"]))
        result = cmd_library(conn, CFG, libargs("remove", all=True))
        assert result == {"removed": 2}
        assert library_rows(conn) == []
    finally:
        conn.close()


def test_library_remove_needs_a_selector(tmp_path):
    conn = open_db(tmp_path)
    try:
        with pytest.raises(ValueError):
            cmd_library(conn, CFG, libargs("remove"))
    finally:
        conn.close()


def test_library_list_prints_year(tmp_path, monkeypatch, capsys):
    stub_tmdb(monkeypatch)   # title "Mein Film", year 2020
    conn = open_db(tmp_path)
    try:
        cmd_library(conn, CFG, libargs("add", tmdb=["100"]))
        capsys.readouterr()
        cmd_library(conn, CFG, libargs("list"))   # human-readable (not --json)
        out = capsys.readouterr().out
        # tmdb_id right-aligned to width 8, then 'Title' (Year)
        assert "  [W]      100  'Mein Film' (2020)" in out
    finally:
        conn.close()


def test_library_list_omits_missing_year(tmp_path, capsys):
    conn = open_db(tmp_path)
    try:
        cmd_library(conn, Config(), libargs("add", tmdb=["100"]))   # no key -> no year
        capsys.readouterr()
        cmd_library(conn, Config(), libargs("list"))
        out = capsys.readouterr().out
        assert "  [W]      100  ''" in out   # no trailing "(...)" for a NULL year
    finally:
        conn.close()


def test_library_list_filters_by_status(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cmd_library(conn, CFG, libargs("add", tmdb=["100", "200"]))
        conn.execute("UPDATE library SET status='L' WHERE tmdb_id='200'")
        result = cmd_library(conn, CFG, libargs("list", status="wish", json=True))
        assert result["count"] == 1
        assert result["library"][0]["tmdb_id"] == "100"
    finally:
        conn.close()


# -- tmdb_search / pick_by_year (title -> tmdb_id resolution) ----------------

def test_tmdb_search_parses_results(monkeypatch):
    captured = {}
    def fake(url, timeout=None):
        captured["url"] = url
        return json.dumps(SEARCH).encode("utf-8")
    monkeypatch.setattr(theke.core, "http_get", fake)
    res = tmdb_search(CFG, "Die Klapperschlange")
    assert res == [
        {"tmdb_id": "9268", "title": "Die Klapperschlange", "year": 1981},
        {"tmdb_id": "999",  "title": "Escape Remake",       "year": 2013},
    ]
    assert "/search/movie?" in captured["url"]
    assert "query=Die+Klapperschlange" in captured["url"]


# year, popularity order: A 2010, B 1981, C 1983 (B before C).
CANDS = [{"tmdb_id": "1", "title": "A", "year": 2010},
         {"tmdb_id": "2", "title": "B", "year": 1981},
         {"tmdb_id": "3", "title": "C", "year": 1983}]


def test_pick_by_year_closest_within_tolerance():
    assert pick_by_year(CANDS, 1981, 5)["tmdb_id"] == "2"   # delta 0


def test_pick_by_year_ties_keep_popularity_order():
    # 1982: B and C are both delta 1; B is more popular (earlier) -> "2".
    assert pick_by_year(CANDS, 1982, 2)["tmdb_id"] == "2"


def test_pick_by_year_none_when_all_out_of_tolerance():
    assert pick_by_year(CANDS, 1990, 2) is None   # deltas 20/9/7


def test_pick_by_year_no_year_takes_most_popular():
    assert pick_by_year(CANDS, None, 2)["tmdb_id"] == "1"


def test_pick_by_year_skips_candidates_without_year():
    cands = [{"tmdb_id": "1", "title": "A", "year": None},
             {"tmdb_id": "2", "title": "B", "year": 1981}]
    assert pick_by_year(cands, 1981, 2)["tmdb_id"] == "2"


def test_pick_by_year_empty():
    assert pick_by_year([], 1981, 2) is None


# -- import: format detection ------------------------------------------------

def test_detect_format_by_extension():
    assert theke._import_detect_format("wishes.csv", None) == "csv"
    assert theke._import_detect_format("wishes.txt", None) == "txt"


def test_detect_format_extension_case_insensitive():
    assert theke._import_detect_format("WISHES.CSV", None) == "csv"


def test_detect_format_override_wins():
    assert theke._import_detect_format("data.dat", "csv") == "csv"
    assert theke._import_detect_format("wishes.csv", "txt") == "txt"


def test_detect_format_unknown_extension_raises():
    with pytest.raises(ValueError):
        theke._import_detect_format("data.dat", None)


# -- import: title (year) parsing --------------------------------------------

def test_parse_title_with_year():
    assert theke._parse_title("Der Pate (1972)") == ("Der Pate", 1972)


def test_parse_title_without_year():
    assert theke._parse_title("Heat") == ("Heat", None)


def test_parse_title_non_four_digit_paren_is_not_a_year():
    assert theke._parse_title("Foo (12)") == ("Foo (12)", None)


def test_parse_title_trailing_number_without_paren_is_part_of_title():
    assert theke._parse_title("Blade Runner 2049") == ("Blade Runner 2049", None)


# -- import: txt parsing -----------------------------------------------------

def test_parse_txt_auto_classifies_and_skips_blanks():
    text = "12345\nDer Pate (1972)\n\n  \nHeat\n"
    assert theke._parse_txt(text, "auto") == [
        (1, "12345", {"kind": "id", "id": "12345"}),
        (2, "Der Pate (1972)", {"kind": "title", "title": "Der Pate", "year": 1972}),
        (5, "Heat", {"kind": "title", "title": "Heat", "year": None}),
    ]


def test_parse_txt_id_mode_takes_whole_line_as_id():
    text = "12345\nDer Pate (1972)"
    assert theke._parse_txt(text, "id") == [
        (1, "12345", {"kind": "id", "id": "12345"}),
        (2, "Der Pate (1972)", {"kind": "id", "id": "Der Pate (1972)"}),
    ]


def test_parse_txt_title_mode_takes_digits_as_a_title():
    assert theke._parse_txt("12345", "title") == [
        (1, "12345", {"kind": "title", "title": "12345", "year": None}),
    ]


# -- import: csv parsing -----------------------------------------------------

def test_parse_csv_tmdb_id_only():
    assert theke._parse_csv("tmdb_id\n100\n200") == [
        (2, "100", {"kind": "id", "id": "100"}),
        (3, "200", {"kind": "id", "id": "200"}),
    ]


def test_parse_csv_title_and_year():
    assert theke._parse_csv("title,year\nDer Pate,1972\nHeat,") == [
        (2, "Der Pate,1972", {"kind": "title", "title": "Der Pate", "year": 1972}),
        (3, "Heat,", {"kind": "title", "title": "Heat", "year": None}),
    ]


def test_parse_csv_all_columns_prefers_id_then_title_and_ignores_dummy():
    text = "tmdb_id,title,year,dummy\n100,,,x\n,Heat,1995,y"
    assert theke._parse_csv(text) == [
        (2, "100,,,x", {"kind": "id", "id": "100"}),
        (3, ",Heat,1995,y", {"kind": "title", "title": "Heat", "year": 1995}),
    ]


def test_parse_csv_unknown_column_raises():
    with pytest.raises(ValueError):
        theke._parse_csv("foo\n1")


def test_parse_csv_title_without_year_raises():
    with pytest.raises(ValueError):
        theke._parse_csv("title\nHeat")


def test_parse_csv_year_without_title_raises():
    with pytest.raises(ValueError):
        theke._parse_csv("year\n1995")


def test_parse_csv_no_id_or_title_column_raises():
    with pytest.raises(ValueError):
        theke._parse_csv("dummy\nx")


def test_parse_csv_bad_year_is_a_row_error():
    rows = theke._parse_csv("title,year\nHeat,abc")
    assert rows[0][0] == 2 and rows[0][2]["kind"] == "error"


def test_parse_csv_empty_row_is_a_row_error():
    rows = theke._parse_csv("tmdb_id,title,year\n,,")
    assert rows[0][0] == 2 and rows[0][2]["kind"] == "error"


# -- import: delimiter sniffing ----------------------------------------------

def test_sniff_delimiter_semicolon():
    assert theke._sniff_delimiter("title;year") == ";"


def test_sniff_delimiter_comma():
    assert theke._sniff_delimiter("tmdb_id,title,year") == ","


def test_sniff_delimiter_tab():
    assert theke._sniff_delimiter("title\tyear") == "\t"


def test_sniff_delimiter_defaults_to_comma_when_none():
    assert theke._sniff_delimiter("title") == ","


def test_sniff_delimiter_picks_the_more_frequent():
    # one comma inside a title, two semicolons as real separators -> ";".
    assert theke._sniff_delimiter("tmdb_id;title;year") == ";"


def test_parse_csv_semicolon_delimited():
    assert theke._parse_csv("title;year\nDer Pate;1972\nHeat;") == [
        (2, "Der Pate;1972", {"kind": "title", "title": "Der Pate", "year": 1972}),
        (3, "Heat;", {"kind": "title", "title": "Heat", "year": None}),
    ]


# -- import: file reading / encoding -----------------------------------------

def test_read_import_file_utf8(tmp_path):
    p = tmp_path / "u.csv"
    p.write_bytes("Gruesse Grüße".encode("utf-8"))  # umlauts as utf-8
    assert theke._read_import_file(str(p)) == "Gruesse Grüße"


def test_read_import_file_cp1252_fallback(tmp_path):
    p = tmp_path / "c.csv"
    p.write_bytes(b"Gruesse Gr\xfc\xdfe")  # "Gruesse Gruesse" with cp1252 ue/sz
    assert theke._read_import_file(str(p)) == "Gruesse Grüße"


def test_import_csv_cp1252_file_resolves_title(tmp_path, monkeypatch):
    stub_import(monkeypatch)
    p = tmp_path / "wishes.csv"
    p.write_bytes("title,year\nGrüße,1981\n".encode("cp1252"))  # SEARCH hit is 1981
    conn = open_db(tmp_path)
    try:
        result = cmd_library(conn, CFG, libargs("import", path=str(p), json=True))
        assert result["added"] == 1 and result["failed"] == 0
    finally:
        conn.close()


def test_import_csv_semicolon_cp1252_file(tmp_path, monkeypatch):
    # The real lfi2.csv case: cp1252 bytes AND a ';' separator.
    stub_import(monkeypatch)
    p = tmp_path / "wishes.csv"
    p.write_bytes("title;year\nGrüße;1981\n".encode("cp1252"))
    conn = open_db(tmp_path)
    try:
        result = cmd_library(conn, CFG, libargs("import", path=str(p), json=True))
        assert result["added"] == 1 and result["failed"] == 0
    finally:
        conn.close()


# -- cmd_library add by title ------------------------------------------------

def test_library_add_by_title_resolves_tmdb_id(tmp_path, monkeypatch):
    stub_search(monkeypatch)
    conn = open_db(tmp_path)
    try:
        result = cmd_library(conn, CFG,
                             libargs("add", title="Die Klapperschlange", year=1981))
        assert result == {"added": 1, "skipped": 0}
        rows = library_rows(conn)
        assert (rows[0]["tmdb_id"], rows[0]["status"], rows[0]["title"]) == \
               ("9268", "W", "Die Klapperschlange")
        assert rows[0]["year"] == 1981   # release year of the resolved candidate
    finally:
        conn.close()


def test_library_add_by_title_year_within_tolerance(tmp_path, monkeypatch):
    # film is 1981, the wish says 1979 -- inside the default tolerance of 2.
    stub_search(monkeypatch)
    conn = open_db(tmp_path)
    try:
        result = cmd_library(conn, CFG,
                             libargs("add", title="Die Klapperschlange", year=1979))
        assert result == {"added": 1, "skipped": 0}
        assert library_rows(conn)[0]["tmdb_id"] == "9268"
    finally:
        conn.close()


def test_library_add_by_title_year_outside_tolerance_raises(tmp_path, monkeypatch):
    # 1981 vs 1975 is 6 years -- beyond the default tolerance of 2.
    stub_search(monkeypatch)
    conn = open_db(tmp_path)
    try:
        with pytest.raises(ValueError):
            cmd_library(conn, CFG,
                        libargs("add", title="Die Klapperschlange", year=1975))
    finally:
        conn.close()


def test_library_add_by_title_year_tolerance_override(tmp_path, monkeypatch):
    stub_search(monkeypatch)
    conn = open_db(tmp_path)
    try:
        result = cmd_library(conn, CFG, libargs(
            "add", title="Die Klapperschlange", year=1975, year_tolerance=10))
        assert result == {"added": 1, "skipped": 0}
        assert library_rows(conn)[0]["tmdb_id"] == "9268"
    finally:
        conn.close()


def test_library_add_by_title_no_results_raises(tmp_path, monkeypatch):
    stub_search(monkeypatch, payload={"results": []})
    conn = open_db(tmp_path)
    try:
        with pytest.raises(ValueError):
            cmd_library(conn, CFG, libargs("add", title="Nonexistent", year=2000))
    finally:
        conn.close()


def test_library_add_by_title_without_key_raises(tmp_path):
    conn = open_db(tmp_path)
    try:
        with pytest.raises(ConfigError):
            cmd_library(conn, Config(), libargs("add", title="Anything"))
    finally:
        conn.close()


def test_library_add_title_and_tmdb_together_raises(tmp_path, monkeypatch):
    stub_search(monkeypatch)
    conn = open_db(tmp_path)
    try:
        with pytest.raises(ValueError):
            cmd_library(conn, CFG, libargs("add", tmdb=["100"], title="X"))
    finally:
        conn.close()


# -- cmd_library import ------------------------------------------------------

def test_import_txt_auto_adds_wishes(tmp_path, monkeypatch):
    stub_import(monkeypatch)
    path = write_file(tmp_path, "wishes.txt", "100\nDie Klapperschlange (1981)\n")
    conn = open_db(tmp_path)
    try:
        result = cmd_library(conn, CFG, libargs("import", path=path, json=True))
        assert result == {"added": 2, "skipped": 0, "failed": 0, "errors": []}
        assert {r["tmdb_id"] for r in library_rows(conn)} == {"100", "9268"}
    finally:
        conn.close()


def test_import_unresolved_title_goes_to_error_log(tmp_path, monkeypatch):
    stub_import(monkeypatch)
    path = write_file(tmp_path, "wishes.txt", "Nonexistent Film (2000)\n100")
    conn = open_db(tmp_path)
    try:
        result = cmd_library(conn, CFG, libargs("import", path=path, json=True))
        assert result["added"] == 1
        assert result["failed"] == 1
        assert result["errors"][0]["line"] == 1
        assert result["errors"][0]["input"] == "Nonexistent Film (2000)"
        assert [r["tmdb_id"] for r in library_rows(conn)] == ["100"]
    finally:
        conn.close()


def test_import_invalid_tmdb_id_goes_to_error_log(tmp_path, monkeypatch):
    stub_import(monkeypatch)
    path = write_file(tmp_path, "wishes.txt", "999")
    conn = open_db(tmp_path)
    try:
        result = cmd_library(conn, CFG, libargs("import", path=path, json=True))
        assert result["added"] == 0 and result["failed"] == 1
        assert result["errors"][0]["input"] == "999"
        assert library_rows(conn) == []
    finally:
        conn.close()


def test_import_idempotent_skips_existing(tmp_path, monkeypatch):
    stub_import(monkeypatch)
    path = write_file(tmp_path, "wishes.txt", "100")
    conn = open_db(tmp_path)
    try:
        cmd_library(conn, CFG, libargs("import", path=path, json=True))
        result = cmd_library(conn, CFG, libargs("import", path=path, json=True))
        assert result == {"added": 0, "skipped": 1, "failed": 0, "errors": []}
    finally:
        conn.close()


def test_import_csv_resolves_id_and_title(tmp_path, monkeypatch):
    stub_import(monkeypatch)
    path = write_file(tmp_path, "wishes.csv",
                      "tmdb_id,title,year\n100,,\n,Die Klapperschlange,1981\n")
    conn = open_db(tmp_path)
    try:
        result = cmd_library(conn, CFG, libargs("import", path=path, json=True))
        assert result["added"] == 2 and result["failed"] == 0
        assert {r["tmdb_id"] for r in library_rows(conn)} == {"100", "9268"}
    finally:
        conn.close()


def test_import_csv_bad_year_row_in_error_log(tmp_path, monkeypatch):
    stub_import(monkeypatch)
    path = write_file(tmp_path, "wishes.csv",
                      "title,year\nDie Klapperschlange,1981\nHeat,abc")
    conn = open_db(tmp_path)
    try:
        result = cmd_library(conn, CFG, libargs("import", path=path, json=True))
        assert result["added"] == 1 and result["failed"] == 1
        assert result["errors"][0]["line"] == 3
    finally:
        conn.close()


def test_import_format_override(tmp_path, monkeypatch):
    stub_import(monkeypatch)
    path = write_file(tmp_path, "wishes.dat", "100")
    conn = open_db(tmp_path)
    try:
        result = cmd_library(conn, CFG,
                             libargs("import", path=path, format="txt", json=True))
        assert result["added"] == 1
    finally:
        conn.close()


def test_import_without_key_raises(tmp_path):
    path = write_file(tmp_path, "wishes.txt", "100")
    conn = open_db(tmp_path)
    try:
        with pytest.raises(ConfigError):
            cmd_library(conn, Config(), libargs("import", path=path))
    finally:
        conn.close()


def test_import_human_output_lists_errors(tmp_path, monkeypatch, capsys):
    stub_import(monkeypatch)
    path = write_file(tmp_path, "wishes.txt", "Nonexistent (2000)")
    conn = open_db(tmp_path)
    try:
        capsys.readouterr()
        cmd_library(conn, CFG, libargs("import", path=path))   # not --json
        out = capsys.readouterr().out
        assert "0 added, 0 skipped, 1 failed" in out
        assert "line 1:" in out
    finally:
        conn.close()


# -- import: informative resolution failures ---------------------------------

def test_search_title_no_results_says_no_match(monkeypatch):
    # 0 candidates -> the message must name the title and "no ... match".
    stub_search(monkeypatch, payload={"results": []})
    with pytest.raises(ValueError) as exc:
        theke._search_title(CFG, "Ghostfilm", 2000, 2)
    msg = str(exc.value)
    assert "no" in msg.lower() and "match" in msg.lower() and "Ghostfilm" in msg


def test_search_title_wrong_year_lists_found_years(monkeypatch):
    # SEARCH has two candidates (1981, 2013); wanting 2000 +-2 excludes both.
    stub_search(monkeypatch)
    with pytest.raises(ValueError) as exc:
        theke._search_title(CFG, "Die Klapperschlange", 2000, 2)
    msg = str(exc.value)
    assert msg.startswith("2 ")          # the candidate count is reported
    assert "1981" in msg and "2013" in msg   # the years that were found
    assert "2000" in msg                 # the wanted year


def test_search_title_retries_without_leading_article(monkeypatch):
    # "Der Pate" yields nothing; the retry drops the article ("Pate") and hits.
    calls = []
    def fake(url, timeout=None):
        calls.append(url)
        payload = {"results": []} if "query=Der" in url else SEARCH
        return json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(theke.core, "http_get", fake)
    tid, title, year = theke._search_title(CFG, "Der Pate", 1981, 2)
    assert tid == "9268"                     # SEARCH's first hit (id 9268, 1981)
    assert len(calls) == 2                    # full title, then stripped retry
    assert "query=Der+Pate" in calls[0]
    assert "query=Pate" in calls[1]


def test_search_title_no_article_does_not_retry(monkeypatch):
    # No leading article -> no second query; 0 results stays a no-match error.
    calls = []
    def fake(url, timeout=None):
        calls.append(url)
        return json.dumps({"results": []}).encode("utf-8")
    monkeypatch.setattr(theke.core, "http_get", fake)
    with pytest.raises(ValueError):
        theke._search_title(CFG, "Ghostfilm", 2000, 2)
    assert len(calls) == 1


def test_import_title_without_year_is_error(tmp_path, monkeypatch):
    # A txt title line with no "(YYYY)" must fail (no guessing without a year).
    stub_import(monkeypatch)
    path = write_file(tmp_path, "wishes.txt", "Heat\n100")
    conn = open_db(tmp_path)
    try:
        result = cmd_library(conn, CFG, libargs("import", path=path, json=True))
        assert result["added"] == 1 and result["failed"] == 1
        assert result["errors"][0]["line"] == 1
        assert result["errors"][0]["input"] == "Heat"
        assert "year" in result["errors"][0]["reason"].lower()
        assert [r["tmdb_id"] for r in library_rows(conn)] == ["100"]
    finally:
        conn.close()


def test_import_csv_title_without_year_is_error(tmp_path, monkeypatch):
    stub_import(monkeypatch)
    path = write_file(tmp_path, "wishes.csv",
                      "title,year\nDie Klapperschlange,1981\nHeat,")
    conn = open_db(tmp_path)
    try:
        result = cmd_library(conn, CFG, libargs("import", path=path, json=True))
        assert result["added"] == 1 and result["failed"] == 1
        assert result["errors"][0]["line"] == 3
        assert "year" in result["errors"][0]["reason"].lower()
    finally:
        conn.close()


def test_import_logs_progress_per_entry(tmp_path, monkeypatch, caplog):
    # Each entry emits a live "[n/total]" progress line (to stderr via logging).
    stub_import(monkeypatch)
    path = write_file(tmp_path, "wishes.txt", "100\nDie Klapperschlange (1981)")
    conn = open_db(tmp_path)
    try:
        with caplog.at_level(logging.INFO, logger="theke"):
            cmd_library(conn, CFG, libargs("import", path=path, json=True))
        msgs = [r.getMessage() for r in caplog.records]
        assert any("1/2" in m for m in msgs)
        assert any("2/2" in m for m in msgs)
    finally:
        conn.close()


# -- download records the library --------------------------------------------

def test_download_records_library_as_L(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    stub_files(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path)
        insert_movie(conn, "m_de", tmdb_id="100", status="2")
        cmd_queue(conn, cfg, qargs("add", tmdb=["100"]))
        conn.execute("UPDATE queue SET status='A'")
        cmd_queue(conn, cfg, qargs("download", all=True))
        rows = library_rows(conn)
        assert len(rows) == 1
        assert (rows[0]["tmdb_id"], rows[0]["status"]) == ("100", "L")
        # path is the folder the video landed in (template renders
        # "<lib>/Mein Film (2020)/Mein Film (2020).mp4").
        assert rows[0]["path"] == (tmp_path / "lib" / "Mein Film (2020)").as_posix()
        # year carried through the queue, even without a prior wish.
        assert rows[0]["year"] == 2020
    finally:
        conn.close()


def test_download_flips_existing_wish_to_L(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    stub_files(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path)
        cmd_library(conn, cfg, libargs("add", tmdb=["100"]))   # a 'W' wish
        insert_movie(conn, "m_de", tmdb_id="100", status="2")
        cmd_queue(conn, cfg, qargs("add", tmdb=["100"]))
        conn.execute("UPDATE queue SET status='A'")
        cmd_queue(conn, cfg, qargs("download", all=True))
        rows = library_rows(conn)
        assert len(rows) == 1                       # flipped, not duplicated
        assert rows[0]["status"] == "L"
    finally:
        conn.close()


def test_download_without_tmdb_records_nothing(tmp_path, monkeypatch):
    stub_files(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path)
        insert_movie(conn, "m_de", tmdb_id="", status="1", clean_title="Solo")
        cmd_queue(conn, cfg, qargs("add", mediathek_id=["m_de"]))
        conn.execute("UPDATE queue SET status='A'")
        cmd_queue(conn, cfg, qargs("download", all=True))
        assert library_rows(conn) == []
    finally:
        conn.close()


# -- _run_pass (one pipeline pass) -------------------------------------------

def test_update_auto_approve_downloads_and_marks_library(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    stub_files(monkeypatch)
    stub_stages(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path, queue_auto_approve=True)
        insert_movie(conn, "m_de", tmdb_id="", status="1")   # not matched yet
        cmd_library(conn, cfg, libargs("add", tmdb=["100"]))
        result = _run_pass(conn, cfg)
        assert result["queued"] == 1
        assert result["downloaded"] == 1
        assert library_rows(conn)[0]["status"] == "L"
    finally:
        conn.close()


def test_update_without_auto_approve_stops_at_proposed(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    stub_files(monkeypatch)
    stub_stages(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path, queue_auto_approve=False)
        insert_movie(conn, "m_de", tmdb_id="", status="1")
        cmd_library(conn, cfg, libargs("add", tmdb=["100"]))
        result = _run_pass(conn, cfg)
        assert result["queued"] == 1
        assert result["downloaded"] == 0
        q = conn.execute("SELECT status FROM queue").fetchone()
        assert q["status"] == "0"                    # proposed -- the gate
        assert library_rows(conn)[0]["status"] == "W"   # still a wish
    finally:
        conn.close()


def test_update_skips_already_satisfied_wish(tmp_path, monkeypatch):
    # a wish already in the library ('L') is not re-matched / re-queued.
    stub_tmdb(monkeypatch)
    stub_files(monkeypatch)
    stub_stages(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path, queue_auto_approve=True)
        insert_movie(conn, "m_de", tmdb_id="100", status="2")
        conn.execute("INSERT INTO library (tmdb_id, status, title, created_at, "
                     "updated_at) VALUES ('100','L','',?,?)",
                     (theke._now(), theke._now()))
        result = _run_pass(conn, cfg)
        assert result["wishes"] == 0
        assert result["queued"] == 0
        assert conn.execute("SELECT count(*) c FROM queue").fetchone()["c"] == 0
    finally:
        conn.close()


def test_update_wish_failure_does_not_abort(tmp_path, monkeypatch):
    def boom(url, timeout=None):
        raise RuntimeError("tmdb down")
    monkeypatch.setattr(theke.core, "http_get", boom)
    stub_stages(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path, queue_auto_approve=False)
        conn.execute("INSERT INTO library (tmdb_id, status, title, created_at, "
                     "updated_at) VALUES ('100','W','',?,?)",
                     (theke._now(), theke._now()))
        result = _run_pass(conn, cfg)
        assert result["failed"] == 1
        assert result["queued"] == 0
        assert library_rows(conn)[0]["status"] == "W"
    finally:
        conn.close()


# A configured list with a single movie that matches the inserted mediathek row.
LIST_ONE = {"items": [{"id": 100, "title": "Mein Film",
                       "release_date": "2020-05-01", "media_type": "movie"}]}


def stub_update_with_list(monkeypatch, payload):
    """http_get branching for update: /list/ yields the TMDB list payload, any
    /movie/<id> the valid TMDB film used for matching."""
    def fake(url, timeout=None, headers=None):
        if "/list/" in url:
            return json.dumps(payload).encode("utf-8")
        if "/movie/" in url:
            return json.dumps(TMDB).encode("utf-8")
        raise AssertionError(f"unexpected url {url}")
    monkeypatch.setattr(theke.core, "http_get", fake)


def test_update_imports_configured_list(tmp_path, monkeypatch):
    # a configured tmdb list is imported into the library before the wish loop,
    # so its movies are matched + queued + downloaded in the same pass.
    stub_update_with_list(monkeypatch, LIST_ONE)
    stub_files(monkeypatch)
    stub_stages(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path, queue_auto_approve=True, tmdb_lists=["7"])
        insert_movie(conn, "m_de", tmdb_id="", status="1")
        result = _run_pass(conn, cfg)
        assert result["list_added"] == 1
        assert result["wishes"] == 1            # imported wish entered the loop
        assert result["downloaded"] == 1
        assert library_rows(conn)[0]["tmdb_id"] == "100"
        assert library_rows(conn)[0]["status"] == "L"
    finally:
        conn.close()


def test_update_list_failure_does_not_abort(tmp_path, monkeypatch):
    def boom(url, timeout=None, headers=None):
        raise RuntimeError("list down")
    monkeypatch.setattr(theke.core, "http_get", boom)
    stub_stages(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path, queue_auto_approve=False, tmdb_lists=["7"])
        result = _run_pass(conn, cfg)
        assert result["list_added"] == 0
        assert result["wishes"] == 0
    finally:
        conn.close()


# -- CLI wiring --------------------------------------------------------------

def test_library_cli_add_and_default_list(tmp_path, monkeypatch, capsys):
    stub_tmdb(monkeypatch)
    db = str(tmp_path / "theke.db")
    cfgpath = tmp_path / "theke.json"
    cfgpath.write_text(json.dumps({"db_path": db, "tmdb_api_key": "KEY"}),
                       encoding="utf-8")
    assert main(["--config", str(cfgpath), "library", "add", "--tmdb", "100"]) == 0
    capsys.readouterr()
    assert main(["--json", "--config", str(cfgpath), "library"]) == 0   # default list
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1 and out["library"][0]["tmdb_id"] == "100"


def test_library_cli_add_by_title(tmp_path, monkeypatch, capsys):
    stub_search(monkeypatch)
    db = str(tmp_path / "theke.db")
    cfgpath = tmp_path / "theke.json"
    cfgpath.write_text(json.dumps({"db_path": db, "tmdb_api_key": "KEY"}),
                       encoding="utf-8")
    assert main(["--json", "--config", str(cfgpath), "library", "add",
                 "--title", "Die Klapperschlange", "--year", "1981"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"added": 1, "skipped": 0}
    conn = db_connect(db)
    try:
        assert library_rows(conn)[0]["tmdb_id"] == "9268"
    finally:
        conn.close()


def test_library_cli_add_tmdb_list(tmp_path, monkeypatch, capsys):
    stub_list(monkeypatch)
    db = str(tmp_path / "theke.db")
    cfgpath = tmp_path / "theke.json"
    cfgpath.write_text(json.dumps({"db_path": db, "tmdb_api_key": "KEY"}),
                       encoding="utf-8")
    assert main(["--json", "--config", str(cfgpath), "library", "add",
                 "--tmdb-list", "7"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"added": 2, "skipped": 0, "series_skipped": 1}
    conn = db_connect(db)
    try:
        assert [r["tmdb_id"] for r in library_rows(conn)] == ["100", "200"]
    finally:
        conn.close()


def test_import_cli_txt(tmp_path, monkeypatch, capsys):
    stub_import(monkeypatch)
    path = write_file(tmp_path, "wishes.txt", "100")
    db = str(tmp_path / "theke.db")
    cfgpath = tmp_path / "theke.json"
    cfgpath.write_text(json.dumps({"db_path": db, "tmdb_api_key": "KEY"}),
                       encoding="utf-8")
    assert main(["--json", "--config", str(cfgpath),
                 "library", "import", path]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"added": 1, "skipped": 0, "failed": 0, "errors": []}


def test_import_cli_format_override(tmp_path, monkeypatch, capsys):
    stub_import(monkeypatch)
    path = write_file(tmp_path, "wishes.dat", "tmdb_id\n100")
    db = str(tmp_path / "theke.db")
    cfgpath = tmp_path / "theke.json"
    cfgpath.write_text(json.dumps({"db_path": db, "tmdb_api_key": "KEY"}),
                       encoding="utf-8")
    assert main(["--json", "--config", str(cfgpath), "library", "import",
                 path, "--format", "csv"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["added"] == 1


def test_run_once_cli_runs(tmp_path, monkeypatch, capsys):
    stub_tmdb(monkeypatch)
    stub_stages(monkeypatch)
    db = str(tmp_path / "theke.db")
    cfgpath = tmp_path / "theke.json"
    cfgpath.write_text(json.dumps({"db_path": db, "tmdb_api_key": "KEY"}),
                       encoding="utf-8")
    assert main(["--json", "--config", str(cfgpath), "run", "--once"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["queued"] == 0   # no wishes
