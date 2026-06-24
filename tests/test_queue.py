"""Tests for the download queue (phase 5): dedup selection + cmd_queue."""

import json

import pytest

import theke
from theke import *
from theke.queue import select_downloads


# -- select_downloads (pure dedup) -------------------------------------------

def row(mid, language="de", duration=6000, size_mb=700, url_video="http://v",
        url_video_hd="", url_video_small="", url_subtitle="", url_website="",
        date="2026-01-01 20:00:00"):
    return dict(mediathek_id=mid, language=language, duration=duration,
                size_mb=size_mb, url_video=url_video, url_video_hd=url_video_hd,
                url_video_small=url_video_small, url_subtitle=url_subtitle,
                url_website=url_website, date=date)


def test_select_single_row_is_av_sd():
    out = select_downloads([row("a")], ["de"], "en")
    assert out == [{"mediathek_id": "a", "language": "de",
                    "resolution": "SD", "remux": "AV"}]


def test_select_hd_when_hd_url_present():
    out = select_downloads([row("a", url_video_hd="http://hd")], ["de"], "en")
    assert out == [{"mediathek_id": "a", "language": "de",
                    "resolution": "HD", "remux": "AV"}]


def test_select_lq_when_only_small_url():
    out = select_downloads([row("a", url_video="", url_video_small="http://s")],
                           ["de"], "en")
    assert out == [{"mediathek_id": "a", "language": "de",
                    "resolution": "LQ", "remux": "AV"}]


def test_select_drops_non_whitelisted_language():
    out = select_downloads([row("a", "de"), row("b", "fr")], ["de"], "en")
    assert out == [{"mediathek_id": "a", "language": "de",
                    "resolution": "SD", "remux": "AV"}]


def test_select_empty_when_nothing_whitelisted():
    assert select_downloads([row("a", "fr")], ["de"], "en") == []


def test_select_same_duration_languages_share_video():
    # b shares a's video (equal duration) -> audio only.
    out = select_downloads([row("a", "de", duration=6000),
                            row("b", "fr", duration=6000)], ["de", "fr"], "en")
    assert out == [{"mediathek_id": "a", "language": "de", "resolution": "SD", "remux": "AV"},
                   {"mediathek_id": "b", "language": "fr", "resolution": "SD", "remux": "A"}]


def test_select_different_duration_languages_each_own_video():
    out = select_downloads([row("a", "de", duration=6000),
                            row("b", "fr", duration=5000)], ["de", "fr"], "en")
    assert out == [{"mediathek_id": "a", "language": "de", "resolution": "SD", "remux": "AV"},
                   {"mediathek_id": "b", "language": "fr", "resolution": "SD", "remux": "AV"}]


def test_select_arte_shared_programme_id_shares_video_despite_duration():
    # Same Arte programme id 116786-000-A across languages -> shared video even
    # though the durations differ slightly.
    a = row("a", "de", duration=6000,
            url_website="https://www.arte.tv/de/videos/116786-000-A/foo/")
    b = row("b", "fr", duration=5999,
            url_website="https://www.arte.tv/fr/videos/116786-000-A/bar/")
    out = select_downloads([a, b], ["de", "fr"], "en")
    assert out == [{"mediathek_id": "a", "language": "de", "resolution": "SD", "remux": "AV"},
                   {"mediathek_id": "b", "language": "fr", "resolution": "SD", "remux": "A"}]


def test_select_anchor_is_best_resolution_across_languages():
    # fr has the HD copy -> it carries the video; de (same duration) becomes audio.
    # Output: anchor first, then the rest in whitelist-preference order.
    a = row("a", "de", duration=6000)
    b = row("b", "fr", duration=6000, url_video_hd="http://hd")
    out = select_downloads([a, b], ["de", "fr"], "en")
    assert out == [{"mediathek_id": "b", "language": "fr", "resolution": "HD", "remux": "AV"},
                   {"mediathek_id": "a", "language": "de", "resolution": "SD", "remux": "A"}]


def test_select_per_language_pick_prefers_hd_over_subtitle():
    a = row("a", "de", url_subtitle="http://sub")   # SD + subtitle
    b = row("b", "de", url_video_hd="http://hd")     # HD, no subtitle
    out = select_downloads([a, b], ["de"], "en")
    assert out == [{"mediathek_id": "b", "language": "de",
                    "resolution": "HD", "remux": "AV"}]


def test_select_per_language_pick_prefers_subtitle_at_equal_quality():
    a = row("a", "de")                               # SD, no subtitle
    b = row("b", "de", url_subtitle="http://sub")    # SD + subtitle
    out = select_downloads([a, b], ["de"], "en")
    assert out == [{"mediathek_id": "b", "language": "de",
                    "resolution": "SD", "remux": "AV"}]


def test_select_ov_resolves_to_original_language_in_whitelist():
    out = select_downloads([row("a", language="ov")], ["en"], "en")
    assert out == [{"mediathek_id": "a", "language": "en",
                    "resolution": "SD", "remux": "AV"}]


def test_select_ov_dropped_when_original_language_not_whitelisted():
    assert select_downloads([row("a", language="ov")], ["de"], "en") == []


# -- cmd_queue add (CLI write side) ------------------------------------------

from types import SimpleNamespace
from theke import cmd_queue, db_connect, main

# original_language 'en', title "Mein Film", year 2020.
TMDB = {"title": "Mein Film", "original_title": "My Film",
        "release_date": "2020-05-01", "runtime": 100, "original_language": "en",
        "alternative_titles": {"titles": []}}

CFG = Config(tmdb_api_key="KEY", languages=["de", "fr"])


def open_db(tmp_path):
    return db_connect(str(tmp_path / "theke.db"))


def insert_mediathek(conn, mediathek_id, status="2", tmdb_id="100", language="de",
                     duration=6000, size_mb=700, url_video="http://v",
                     url_video_hd="", url_video_small="", url_subtitle="",
                     url_website="", date="2026-01-01 20:00:00",
                     clean_title="Film", year=2020):
    cols = dict(status=status, mediathek_id=mediathek_id, tmdb_id=tmdb_id,
                language=language, duration=duration, size_mb=size_mb,
                url_video=url_video, url_video_hd=url_video_hd,
                url_video_small=url_video_small, url_subtitle=url_subtitle,
                url_website=url_website, date=date, clean_title=clean_title, year=year)
    conn.execute(f"INSERT INTO mediathek ({','.join(cols)}) VALUES "
                 f"({','.join(':' + k for k in cols)})", cols)


def qargs(queue_cmd="add", tmdb=None, mediathek_id=None, status=None,
          ids=None, all=False, json=False):
    return SimpleNamespace(queue_cmd=queue_cmd, tmdb=tmdb, mediathek_id=mediathek_id,
                           status=status, ids=ids or [], all=all, json=json)


def stub_tmdb(monkeypatch):
    monkeypatch.setattr(theke, "http_get",
                        lambda url: json.dumps(TMDB).encode("utf-8"))


def queue_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM queue ORDER BY id")]


def test_queue_add_by_tmdb_dedups_and_inserts(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_de", language="de", duration=6000)
        insert_mediathek(conn, "m_fr", language="fr", duration=6000)  # shares video
        insert_mediathek(conn, "m_es", language="es", duration=6000)  # not whitelisted
        result = cmd_queue(conn, CFG, qargs(tmdb=["100"]))
        assert result == {"queued": 2, "skipped": 0, "deduplicated": 1}
        rows = queue_rows(conn)
        assert [(r["mediathek_id"], r["status"], r["language"], r["resolution"],
                 r["remux"], r["name"], r["tmdb_id"]) for r in rows] == [
            ("m_de", "P", "de", "SD", "AV", "Mein Film (2020)", "100"),
            ("m_fr", "P", "fr", "SD", "A",  "Mein Film (2020)", "100")]
    finally:
        conn.close()


def test_queue_add_auto_approve_writes_approved(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_de", language="de")
        cmd_queue(conn, Config(tmdb_api_key="KEY", languages=["de"],
                               queue_auto_approve=True), qargs(tmdb=["100"]))
        assert queue_rows(conn)[0]["status"] == "A"
    finally:
        conn.close()


def test_queue_add_is_idempotent_for_active_entries(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_de", language="de", duration=6000)
        insert_mediathek(conn, "m_fr", language="fr", duration=6000)
        cmd_queue(conn, CFG, qargs(tmdb=["100"]))
        again = cmd_queue(conn, CFG, qargs(tmdb=["100"]))
        assert again == {"queued": 0, "skipped": 2, "deduplicated": 0}
        assert len(queue_rows(conn)) == 2   # no duplicates added
    finally:
        conn.close()


def test_queue_add_requeues_after_cancelled(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_de", language="de")
        cmd_queue(conn, Config(tmdb_api_key="KEY", languages=["de"]), qargs(tmdb=["100"]))
        conn.execute("UPDATE queue SET status='C'")   # cancelled, no longer active
        again = cmd_queue(conn, Config(tmdb_api_key="KEY", languages=["de"]),
                          qargs(tmdb=["100"]))
        assert again["queued"] == 1
        assert len(queue_rows(conn)) == 2   # a fresh row alongside the cancelled one
    finally:
        conn.close()


def test_queue_add_by_mediathek_id_single_av(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_de", language="de", url_video_hd="http://hd")
        result = cmd_queue(conn, CFG, qargs(mediathek_id=["m_de"]))
        assert result == {"queued": 1, "skipped": 0, "deduplicated": 0}
        r = queue_rows(conn)[0]
        assert (r["mediathek_id"], r["resolution"], r["remux"], r["name"]) == \
               ("m_de", "HD", "AV", "Mein Film (2020)")
    finally:
        conn.close()


def test_queue_add_by_mediathek_id_unmatched_uses_clean_title(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)   # must NOT be needed (no tmdb_id on the row)
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_solo", status="1", tmdb_id="", language="de",
                         clean_title="Solo", year=2019)
        cmd_queue(conn, CFG, qargs(mediathek_id=["m_solo"]))
        r = queue_rows(conn)[0]
        assert r["name"] == "Solo (2019)" and r["remux"] == "AV"
    finally:
        conn.close()


def test_queue_add_resolves_ov_via_tmdb_original_language(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)   # original_language 'en'
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_ov", language="ov")
        cmd_queue(conn, Config(tmdb_api_key="KEY", languages=["de", "en"]),
                  qargs(tmdb=["100"]))
        assert queue_rows(conn)[0]["language"] == "en"
    finally:
        conn.close()


def test_queue_add_without_selector_is_error(tmp_path):
    conn = open_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="--tmdb or --mediathek-id"):
            cmd_queue(conn, CFG, qargs())
    finally:
        conn.close()


def test_queue_add_by_tmdb_requires_api_key(tmp_path):
    conn = open_db(tmp_path)
    try:
        with pytest.raises(ConfigError, match="TMDB API key"):
            cmd_queue(conn, Config(languages=["de"]), qargs(tmdb=["100"]))
    finally:
        conn.close()


def test_queue_add_cli_json(tmp_path, monkeypatch, capsys):
    stub_tmdb(monkeypatch)
    db = str(tmp_path / "theke.db")
    cfgpath = tmp_path / "theke.json"
    cfgpath.write_text(json.dumps({"db_path": db, "tmdb_api_key": "KEY",
                                   "languages": ["de"]}), encoding="utf-8")
    conn = db_connect(db)
    insert_mediathek(conn, "m_de", language="de")
    conn.close()
    rc = main(["--json", "--config", str(cfgpath), "queue", "add", "--tmdb", "100"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"queued": 1, "skipped": 0,
                                                   "deduplicated": 0}


# -- cmd_queue list ----------------------------------------------------------

def test_queue_list_returns_entries_ordered(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_de", language="de", duration=6000)
        insert_mediathek(conn, "m_fr", language="fr", duration=6000)
        cmd_queue(conn, CFG, qargs(tmdb=["100"]))
        result = cmd_queue(conn, CFG, qargs(queue_cmd="list", json=True))
        assert result["count"] == 2
        assert [(r["mediathek_id"], r["status"], r["remux"]) for r in result["queue"]] == \
               [("m_de", "P", "AV"), ("m_fr", "P", "A")]
    finally:
        conn.close()


def test_queue_list_filters_by_status(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_de", language="de", duration=6000)
        insert_mediathek(conn, "m_fr", language="fr", duration=6000)
        cmd_queue(conn, CFG, qargs(tmdb=["100"]))
        conn.execute("UPDATE queue SET status='A' WHERE mediathek_id='m_de'")
        approved = cmd_queue(conn, CFG, qargs(queue_cmd="list", status="approved", json=True))
        proposed = cmd_queue(conn, CFG, qargs(queue_cmd="list", status="proposed", json=True))
        assert [r["mediathek_id"] for r in approved["queue"]] == ["m_de"]
        assert [r["mediathek_id"] for r in proposed["queue"]] == ["m_fr"]
    finally:
        conn.close()


def test_queue_list_cli_default_action(tmp_path, monkeypatch, capsys):
    stub_tmdb(monkeypatch)
    db = str(tmp_path / "theke.db")
    cfgpath = tmp_path / "theke.json"
    cfgpath.write_text(json.dumps({"db_path": db, "tmdb_api_key": "KEY",
                                   "languages": ["de"]}), encoding="utf-8")
    conn = db_connect(db)
    insert_mediathek(conn, "m_de", language="de")
    conn.close()
    assert main(["--config", str(cfgpath), "queue", "add", "--tmdb", "100"]) == 0
    capsys.readouterr()
    assert main(["--json", "--config", str(cfgpath), "queue"]) == 0   # bare -> list
    assert json.loads(capsys.readouterr().out)["count"] == 1
