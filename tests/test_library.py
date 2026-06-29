"""Tests for the wishlist / library stage (phase 9): the library table,
cmd_library (add/list/remove), download -> library recording, and the
cmd_update orchestrator."""

import json
from types import SimpleNamespace

import pytest

import theke
from theke import *
from theke import cmd_library, cmd_queue, cmd_update, db_connect, main


# -- helpers -----------------------------------------------------------------

# original_language 'en', title "Mein Film", year 2020, runtime 100 min.
TMDB = {"title": "Mein Film", "original_title": "My Film",
        "release_date": "2020-05-01", "runtime": 100, "original_language": "en",
        "alternative_titles": {"titles": []}}

CFG = Config(tmdb_api_key="KEY", languages=["de"])


def open_db(tmp_path):
    return db_connect(str(tmp_path / "theke.db"))


def stub_tmdb(monkeypatch):
    monkeypatch.setattr(theke.core, "http_get",
                        lambda url, timeout=None: json.dumps(TMDB).encode("utf-8"))


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


def libargs(library_cmd="list", tmdb=None, all=False, status=None, json=False):
    return SimpleNamespace(library_cmd=library_cmd, tmdb=tmdb, all=all,
                           status=status, json=json)


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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(library)")}
        assert cols == {"tmdb_id", "status", "title", "created_at", "updated_at"}
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


def test_library_add_without_key_leaves_title_empty(tmp_path):
    conn = open_db(tmp_path)
    try:
        cmd_library(conn, Config(), libargs("add", tmdb=["100"]))
        assert library_rows(conn)[0]["title"] == ""
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
