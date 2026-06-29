"""Tests for the wishlist / library stage (phase 9): the library table,
cmd_library (add/list/remove), download -> library recording, and the
cmd_update orchestrator."""

import json
from types import SimpleNamespace

import pytest

import theke
from theke import *
from theke import (cmd_library, cmd_queue, cmd_update, db_connect, main,
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
            title=None, year=None, year_tolerance=None):
    return SimpleNamespace(library_cmd=library_cmd, tmdb=tmdb, all=all,
                           status=status, json=json, title=title, year=year,
                           year_tolerance=year_tolerance)


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
    """No-op fetch + enrich so cmd_update exercises only the wish loop."""
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


# -- cmd_update orchestrator -------------------------------------------------

def test_update_auto_approve_downloads_and_marks_library(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    stub_files(monkeypatch)
    stub_stages(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path, queue_auto_approve=True)
        insert_movie(conn, "m_de", tmdb_id="", status="1")   # not matched yet
        cmd_library(conn, cfg, libargs("add", tmdb=["100"]))
        result = cmd_update(conn, cfg, SimpleNamespace())
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
        result = cmd_update(conn, cfg, SimpleNamespace())
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
        result = cmd_update(conn, cfg, SimpleNamespace())
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
        result = cmd_update(conn, cfg, SimpleNamespace())
        assert result["failed"] == 1
        assert result["queued"] == 0
        assert library_rows(conn)[0]["status"] == "W"
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


def test_update_cli_runs(tmp_path, monkeypatch, capsys):
    stub_tmdb(monkeypatch)
    stub_stages(monkeypatch)
    db = str(tmp_path / "theke.db")
    cfgpath = tmp_path / "theke.json"
    cfgpath.write_text(json.dumps({"db_path": db, "tmdb_api_key": "KEY"}),
                       encoding="utf-8")
    assert main(["--json", "--config", str(cfgpath), "update"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["queued"] == 0   # no wishes
