"""Tests for the download queue (phase 5): dedup selection + cmd_queue."""

import json
import os

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
          ids=None, all=False, force=False, cancelled=False, done=False,
          failed=False, json=False, language=None, resolution=None,
          remux=None, url=None, path=None, url_subtitle=None):
    return SimpleNamespace(queue_cmd=queue_cmd, tmdb=tmdb, mediathek_id=mediathek_id,
                           status=status, ids=ids or [], all=all, force=force,
                           cancelled=cancelled, done=done, failed=failed, json=json,
                           language=language, resolution=resolution,
                           remux=remux, url=url, path=path, url_subtitle=url_subtitle)


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
                 r["remux"], r["tmdb_id"]) for r in rows] == [
            ("m_de", "0", "de", "SD", "AV", "100"),
            ("m_fr", "0", "fr", "SD", "A",  "100")]
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
        assert (r["mediathek_id"], r["resolution"], r["remux"], r["path"]) == \
               ("m_de", "HD", "AV", "movies/Mein Film (2020)/Mein Film (2020).mp4")
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
        assert r["path"] == "movies/Solo (2019)/Solo (2019).mp4" and r["remux"] == "AV"
    finally:
        conn.close()


def test_queue_add_missing_year_does_not_render_none(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_noyear", status="1", tmdb_id="", language="de",
                         clean_title="Doku", year=None)
        cmd_queue(conn, CFG, qargs(mediathek_id=["m_noyear"]))
        # not "Doku (None)" -- a None year renders empty in the path
        assert queue_rows(conn)[0]["path"] == "movies/Doku ()/Doku ().mp4"
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


# -- queue add: url / path / subtitle population (phase 6-8 prep) -------------
# queue download must run off the queue row alone, so add resolves the source
# url, the full library path and the subtitle url at staging time.

def test_queue_add_tmdb_fills_url_and_path(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)   # title "Mein Film", year 2020, original_language en
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_de", language="de", duration=6000, url_video="http://de")
        insert_mediathek(conn, "m_fr", language="fr", duration=6000, url_video="http://fr")  # shares video -> audio
        cmd_queue(conn, CFG, qargs(tmdb=["100"]))
        rows = {r["mediathek_id"]: r for r in queue_rows(conn)}
        # anchor de: AV video -> .mp4, no language infix; primary source url
        assert rows["m_de"]["url"] == "http://de"
        assert rows["m_de"]["path"] == "movies/Mein Film (2020)/Mein Film (2020).mp4"
        # fr shares de's video -> audio-only, language infix + audio ext
        assert rows["m_fr"]["remux"] == "A"
        assert rows["m_fr"]["url"] == "http://fr"
        assert rows["m_fr"]["path"] == "movies/Mein Film (2020)/Mein Film (2020).fr.aac"
    finally:
        conn.close()


def test_queue_add_tmdb_second_video_gets_language_infix(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_de", language="de", duration=6000, url_video="http://de")
        insert_mediathek(conn, "m_fr", language="fr", duration=5000, url_video="http://fr")  # own video (diff duration)
        cmd_queue(conn, CFG, qargs(tmdb=["100"]))
        rows = {r["mediathek_id"]: r for r in queue_rows(conn)}
        assert rows["m_de"]["remux"] == "AV"
        assert rows["m_de"]["path"] == "movies/Mein Film (2020)/Mein Film (2020).mp4"
        # non-anchor video keeps its own video but is tagged with the language
        assert rows["m_fr"]["remux"] == "AV"
        assert rows["m_fr"]["path"] == "movies/Mein Film (2020)/Mein Film (2020).fr.mp4"
    finally:
        conn.close()


def test_queue_add_tmdb_hd_url_selected(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_de", language="de", url_video="http://sd",
                         url_video_hd="http://hd")
        cmd_queue(conn, Config(tmdb_api_key="KEY", languages=["de"]), qargs(tmdb=["100"]))
        assert queue_rows(conn)[0]["url"] == "http://hd"
    finally:
        conn.close()


def test_queue_add_mediathek_fills_url_and_path(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)   # not needed (no tmdb_id)
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_solo", status="1", tmdb_id="", language="de",
                         clean_title="Solo", year=2019, url_video="http://s")
        cmd_queue(conn, CFG, qargs(mediathek_id=["m_solo"]))
        r = queue_rows(conn)[0]
        assert r["url"] == "http://s"
        assert r["path"] == "movies/Solo (2019)/Solo (2019).mp4"
    finally:
        conn.close()


def test_queue_add_fills_subtitle_url(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_de", language="de", url_subtitle="http://sub.vtt")
        cmd_queue(conn, CFG, qargs(mediathek_id=["m_de"]))
        assert queue_rows(conn)[0]["url_subtitle"] == "http://sub.vtt"
    finally:
        conn.close()


def test_queue_add_library_path_placeholders_case_insensitive(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cfg = Config(tmdb_api_key="KEY", languages=["de"],
                     library_path="x/{TITLE}/{Title} ({YEAR}).mp4")
        insert_mediathek(conn, "m_de", language="de")
        cmd_queue(conn, cfg, qargs(tmdb=["100"]))
        # {TITLE}/{Title}=Mein Film, {YEAR}=2020; placeholders are case-insensitive
        assert queue_rows(conn)[0]["path"] == "x/Mein Film/Mein Film (2020).mp4"
    finally:
        conn.close()


def test_render_template_movie_and_series_placeholders():
    from theke import _render_template
    # movie fields; Series/Season/Episode render empty (None)
    movie = {"Title": "Mein Film", "Year": 2020, "Series": None,
             "Season": None, "Episode": None}
    assert _render_template("{title} ({year})", movie) == "Mein Film (2020)"
    # series fields with ':N' zero-padding (2-digit), case-insensitive keys
    series = {"Title": "Pilot", "Year": 2021, "Series": "My Show",
              "Season": 3, "Episode": 7}
    out = _render_template(
        "series/{Series} ({Year})/Season {Season:2}/"
        "{series} S{SEASON:2}E{episode:2} {Title}.mp4", series)
    assert out == "series/My Show (2021)/Season 03/My Show S03E07 Pilot.mp4"


def test_render_template_unknown_placeholder_raises():
    from theke import _render_template
    with pytest.raises(KeyError, match="bogus"):
        _render_template("{bogus}", {"Title": "X"})


def test_queue_add_custom_extensions(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        cfg = Config(tmdb_api_key="KEY", languages=["de", "fr"],
                     video_ext="mkv", audio_ext="m4a")
        insert_mediathek(conn, "m_de", language="de", duration=6000)
        insert_mediathek(conn, "m_fr", language="fr", duration=6000)   # shares video -> audio
        cmd_queue(conn, cfg, qargs(tmdb=["100"]))
        rows = {r["mediathek_id"]: r for r in queue_rows(conn)}
        assert rows["m_de"]["path"] == "movies/Mein Film (2020)/Mein Film (2020).mkv"
        assert rows["m_fr"]["path"] == "movies/Mein Film (2020)/Mein Film (2020).fr.m4a"
    finally:
        conn.close()


def test_queue_add_cli_overrides_columns(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        insert_mediathek(conn, "m_de", language="de")
        cmd_queue(conn, CFG, qargs(mediathek_id=["m_de"], url="http://o",
                                   path="P/x.mp4", language="xx", resolution="HD",
                                   remux="V", url_subtitle="http://o.srt"))
        r = queue_rows(conn)[0]
        assert (r["url"], r["path"], r["language"], r["resolution"], r["remux"],
                r["url_subtitle"]) == (
            "http://o", "P/x.mp4", "xx", "HD", "V", "http://o.srt")
    finally:
        conn.close()


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
               [("m_de", "0", "AV"), ("m_fr", "0", "A")]
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


# -- cmd_queue approve -------------------------------------------------------

def qid(conn, mediathek_id):
    return conn.execute("SELECT id FROM queue WHERE mediathek_id=?",
                        (mediathek_id,)).fetchone()["id"]


def status_of(conn, mediathek_id):
    return conn.execute("SELECT status FROM queue WHERE mediathek_id=?",
                        (mediathek_id,)).fetchone()["status"]


def two_proposed(conn):
    insert_mediathek(conn, "m_de", language="de", duration=6000)
    insert_mediathek(conn, "m_fr", language="fr", duration=6000)
    cmd_queue(conn, CFG, qargs(tmdb=["100"]))


def test_queue_approve_by_id(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        two_proposed(conn)
        result = cmd_queue(conn, CFG, qargs(queue_cmd="approve", ids=[qid(conn, "m_de")]))
        assert result == {"approved": 1}
        assert status_of(conn, "m_de") == "A"
        assert status_of(conn, "m_fr") == "0"
    finally:
        conn.close()


def test_queue_approve_all(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        two_proposed(conn)
        result = cmd_queue(conn, CFG, qargs(queue_cmd="approve", all=True))
        assert result == {"approved": 2}
        assert status_of(conn, "m_de") == "A" and status_of(conn, "m_fr") == "A"
    finally:
        conn.close()


def test_queue_approve_only_touches_proposed(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        two_proposed(conn)
        conn.execute("UPDATE queue SET status='C' WHERE mediathek_id='m_fr'")
        result = cmd_queue(conn, CFG, qargs(queue_cmd="approve", all=True))
        assert result == {"approved": 1}
        assert status_of(conn, "m_fr") == "C"   # cancelled, untouched
    finally:
        conn.close()


def test_queue_approve_force_reapproves_any_status(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        two_proposed(conn)
        conn.execute("UPDATE queue SET status='C' WHERE mediathek_id='m_de'")  # cancelled
        conn.execute("UPDATE queue SET status='D' WHERE mediathek_id='m_fr'")  # done
        result = cmd_queue(conn, CFG, qargs(queue_cmd="approve", all=True, force=True))
        assert result == {"approved": 2}
        assert status_of(conn, "m_de") == "A" and status_of(conn, "m_fr") == "A"
    finally:
        conn.close()


def test_queue_approve_force_by_id(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        two_proposed(conn)
        conn.execute("UPDATE queue SET status='C' WHERE mediathek_id='m_fr'")
        result = cmd_queue(conn, CFG, qargs(queue_cmd="approve",
                                            ids=[qid(conn, "m_fr")], force=True))
        assert result == {"approved": 1}
        assert status_of(conn, "m_fr") == "A"
    finally:
        conn.close()


def test_queue_approve_without_force_ignores_cancelled(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        two_proposed(conn)
        conn.execute("UPDATE queue SET status='C' WHERE mediathek_id='m_fr'")
        result = cmd_queue(conn, CFG, qargs(queue_cmd="approve", all=True))
        assert result == {"approved": 1}            # only the proposed m_de
        assert status_of(conn, "m_fr") == "C"
    finally:
        conn.close()


def test_queue_approve_needs_ids_or_all(tmp_path):
    conn = open_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="ids or --all"):
            cmd_queue(conn, CFG, qargs(queue_cmd="approve"))
    finally:
        conn.close()


def test_queue_approve_cli(tmp_path, monkeypatch, capsys):
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
    assert main(["--json", "--config", str(cfgpath), "queue", "approve", "1"]) == 0
    assert json.loads(capsys.readouterr().out) == {"approved": 1}


# -- cmd_queue cancel --------------------------------------------------------

def test_queue_cancel_by_id(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        two_proposed(conn)
        result = cmd_queue(conn, CFG, qargs(queue_cmd="cancel", ids=[qid(conn, "m_de")]))
        assert result == {"cancelled": 1}
        assert status_of(conn, "m_de") == "C"
        assert status_of(conn, "m_fr") == "0"
    finally:
        conn.close()


def test_queue_cancel_all_active(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        two_proposed(conn)
        conn.execute("UPDATE queue SET status='A' WHERE mediathek_id='m_fr'")  # approved is active
        result = cmd_queue(conn, CFG, qargs(queue_cmd="cancel", all=True))
        assert result == {"cancelled": 2}
        assert status_of(conn, "m_de") == "C" and status_of(conn, "m_fr") == "C"
    finally:
        conn.close()


def test_queue_cancel_skips_finished(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        two_proposed(conn)
        conn.execute("UPDATE queue SET status='D' WHERE mediathek_id='m_fr'")  # done
        result = cmd_queue(conn, CFG, qargs(queue_cmd="cancel", all=True))
        assert result == {"cancelled": 1}
        assert status_of(conn, "m_fr") == "D"   # finished, untouched
    finally:
        conn.close()


def test_queue_cancel_needs_ids_or_all(tmp_path):
    conn = open_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="ids or --all"):
            cmd_queue(conn, CFG, qargs(queue_cmd="cancel"))
    finally:
        conn.close()


def test_queue_cancel_cli(tmp_path, monkeypatch, capsys):
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
    assert main(["--json", "--config", str(cfgpath), "queue", "cancel", "1"]) == 0
    assert json.loads(capsys.readouterr().out) == {"cancelled": 1}


# -- cmd_queue delete --------------------------------------------------------

def queue_count(conn):
    return conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]


def test_queue_delete_by_id(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        two_proposed(conn)
        result = cmd_queue(conn, CFG, qargs(queue_cmd="delete", ids=[qid(conn, "m_de")]))
        assert result == {"deleted": 1}
        assert [r["mediathek_id"] for r in queue_rows(conn)] == ["m_fr"]
    finally:
        conn.close()


def test_queue_delete_all(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        two_proposed(conn)
        assert cmd_queue(conn, CFG, qargs(queue_cmd="delete", all=True)) == {"deleted": 2}
        assert queue_count(conn) == 0
    finally:
        conn.close()


def test_queue_delete_by_state_combinable(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        two_proposed(conn)
        insert_mediathek(conn, "m_keep", language="de")
        cmd_queue(conn, CFG, qargs(mediathek_id=["m_keep"]))      # stays proposed
        conn.execute("UPDATE queue SET status='C' WHERE mediathek_id='m_de'")
        conn.execute("UPDATE queue SET status='D' WHERE mediathek_id='m_fr'")
        result = cmd_queue(conn, CFG, qargs(queue_cmd="delete", cancelled=True, done=True))
        assert result == {"deleted": 2}
        assert [r["mediathek_id"] for r in queue_rows(conn)] == ["m_keep"]
    finally:
        conn.close()


def test_queue_delete_failed(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        two_proposed(conn)
        conn.execute("UPDATE queue SET status='F' WHERE mediathek_id='m_fr'")
        assert cmd_queue(conn, CFG, qargs(queue_cmd="delete", failed=True)) == {"deleted": 1}
        assert [r["mediathek_id"] for r in queue_rows(conn)] == ["m_de"]
    finally:
        conn.close()


def test_queue_delete_needs_a_selector(tmp_path):
    conn = open_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="ids, status flags"):
            cmd_queue(conn, CFG, qargs(queue_cmd="delete"))
    finally:
        conn.close()


def test_queue_delete_rejects_mixed_modes(tmp_path, monkeypatch):
    stub_tmdb(monkeypatch)
    conn = open_db(tmp_path)
    try:
        two_proposed(conn)
        with pytest.raises(ValueError, match="ids, status flags"):
            cmd_queue(conn, CFG, qargs(queue_cmd="delete",
                                       ids=[qid(conn, "m_de")], cancelled=True))
    finally:
        conn.close()


def test_queue_delete_cli(tmp_path, monkeypatch, capsys):
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
    assert main(["--json", "--config", str(cfgpath), "queue", "delete", "--all"]) == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": 1}


# -- cmd_queue download (phases 6-8 chained off the queue row) ----------------
# The file primitives (download/remux/move) are stubbed; we test the chaining,
# status transitions, cleanup and error handling -- not ffmpeg/HTTP themselves.

def _fake_dl(url, out, retries):
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


def insert_local(conn, mediathek_id, clean_title, **kw):
    """A self-named, unmatched row (no TMDB needed; path from clean_title)."""
    insert_mediathek(conn, mediathek_id, status="1", tmdb_id="", language="de",
                     clean_title=clean_title, year=2020, **kw)


def test_queue_download_runs_pipeline_to_target(tmp_path, monkeypatch):
    monkeypatch.setattr(theke, "download_file", _fake_dl)
    monkeypatch.setattr(theke, "run_remux", _fake_remux)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path)
        insert_local(conn, "m_de", "Solo", url_video="http://de")
        cmd_queue(conn, cfg, qargs(mediathek_id=["m_de"]))
        conn.execute("UPDATE queue SET status='A'")
        result = cmd_queue(conn, cfg, qargs(queue_cmd="download", all=True))
        assert result == {"downloaded": 1, "failed": 0}
        target = queue_rows(conn)[0]["path"]
        assert target == (tmp_path / "lib").as_posix() + "/Solo (2020)/Solo (2020).mp4"
        with open(target, "rb") as fh:
            assert fh.read() == b"MUX"
        assert status_of(conn, "m_de") == "D"
        assert os.listdir(str(tmp_path / "scratch")) == []   # temp files cleaned up
    finally:
        conn.close()


def test_queue_download_audio_only_remux_mode(tmp_path, monkeypatch):
    # the audio pick is remuxed in mode 'A' to its '.fr.aac' destination.
    stub_tmdb(monkeypatch)
    seen = {}

    def remux(ffmpeg_path, in_path, mode, out_path, language=None):
        seen[mode] = (language, os.path.splitext(out_path)[1])   # tag + temp ext
        with open(out_path, "wb") as fh:
            fh.write(b"MUX")
        return 3

    monkeypatch.setattr(theke, "download_file", _fake_dl)
    monkeypatch.setattr(theke, "run_remux", remux)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path, languages=["de", "fr"])
        insert_mediathek(conn, "m_de", language="de", duration=6000, url_video="http://de")
        insert_mediathek(conn, "m_fr", language="fr", duration=6000, url_video="http://fr")
        cmd_queue(conn, cfg, qargs(tmdb=["100"]))
        conn.execute("UPDATE queue SET status='A'")
        result = cmd_queue(conn, cfg, qargs(queue_cmd="download", all=True))
        assert result == {"downloaded": 2, "failed": 0}
        # de -> AV remux tagged deu into a .mp4; fr -> A remux tagged fra into a .aac
        assert seen["AV"] == ("deu", ".mp4")
        assert seen["A"] == ("fra", ".aac")
        lib = (tmp_path / "lib") / "Mein Film (2020)"
        assert (lib / "Mein Film (2020).mp4").is_file()      # anchor video
        assert (lib / "Mein Film (2020).fr.aac").is_file()   # fr audio sidecar
    finally:
        conn.close()


def test_queue_download_only_processes_approved(tmp_path, monkeypatch):
    monkeypatch.setattr(theke, "download_file",
                        lambda *a, **k: pytest.fail("must not download a non-approved row"))
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path)
        insert_local(conn, "m_de", "Solo", url_video="http://de")
        cmd_queue(conn, cfg, qargs(mediathek_id=["m_de"]))   # stays proposed ('0')
        result = cmd_queue(conn, cfg, qargs(queue_cmd="download", all=True))
        assert result == {"downloaded": 0, "failed": 0}
        assert status_of(conn, "m_de") == "0"
    finally:
        conn.close()


def test_queue_download_by_id(tmp_path, monkeypatch):
    monkeypatch.setattr(theke, "download_file", _fake_dl)
    monkeypatch.setattr(theke, "run_remux", _fake_remux)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path)
        insert_local(conn, "m_a", "Aaa", url_video="http://a")
        insert_local(conn, "m_b", "Bbb", url_video="http://b")
        cmd_queue(conn, cfg, qargs(mediathek_id=["m_a"]))
        cmd_queue(conn, cfg, qargs(mediathek_id=["m_b"]))
        conn.execute("UPDATE queue SET status='A'")
        result = cmd_queue(conn, cfg, qargs(queue_cmd="download", ids=[qid(conn, "m_a")]))
        assert result == {"downloaded": 1, "failed": 0}
        assert status_of(conn, "m_a") == "D"
        assert status_of(conn, "m_b") == "A"   # untouched
    finally:
        conn.close()


def test_queue_download_failure_marks_failed_and_continues(tmp_path, monkeypatch):
    def dl(url, out, retries):
        if "bad" in url:
            raise RuntimeError("net down")
        with open(out, "wb") as fh:
            fh.write(b"SRC")
        return 3

    monkeypatch.setattr(theke, "download_file", dl)
    monkeypatch.setattr(theke, "run_remux", _fake_remux)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path)
        insert_local(conn, "m_ok", "Okay", url_video="http://ok")
        insert_local(conn, "m_bad", "Bad", url_video="http://bad")
        cmd_queue(conn, cfg, qargs(mediathek_id=["m_ok"]))
        cmd_queue(conn, cfg, qargs(mediathek_id=["m_bad"]))
        conn.execute("UPDATE queue SET status='A'")
        result = cmd_queue(conn, cfg, qargs(queue_cmd="download", all=True))
        assert result == {"downloaded": 1, "failed": 1}   # one bad row does not abort
        assert status_of(conn, "m_ok") == "D"
        assert status_of(conn, "m_bad") == "F"
        bad = next(r for r in queue_rows(conn) if r["mediathek_id"] == "m_bad")
        assert "net down" in bad["error"]
        assert os.listdir(str(tmp_path / "scratch")) == []   # temp cleaned even on failure
    finally:
        conn.close()


def test_queue_download_routes_hls(tmp_path, monkeypatch):
    def hls(url, out, retries, ffmpeg_path):
        with open(out, "wb") as fh:
            fh.write(b"SRC")
        return "hls", 3, 1

    monkeypatch.setattr(theke, "download_hls", hls)
    monkeypatch.setattr(theke, "download_file",
                        lambda *a, **k: pytest.fail("HLS url must route to download_hls"))
    monkeypatch.setattr(theke, "run_remux", _fake_remux)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path)
        insert_local(conn, "m_de", "Solo", url_video="http://h/v.m3u8")
        cmd_queue(conn, cfg, qargs(mediathek_id=["m_de"]))
        conn.execute("UPDATE queue SET status='A'")
        result = cmd_queue(conn, cfg, qargs(queue_cmd="download", all=True))
        assert result == {"downloaded": 1, "failed": 0}
        assert status_of(conn, "m_de") == "D"
    finally:
        conn.close()


def test_queue_download_writes_subtitle_sidecar(tmp_path, monkeypatch):
    def dl(url, out, retries):
        data = b"SUB" if url.endswith(".vtt") else b"SRC"
        with open(out, "wb") as fh:
            fh.write(data)
        return len(data)

    monkeypatch.setattr(theke, "download_file", dl)
    monkeypatch.setattr(theke, "run_remux", _fake_remux)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path)
        insert_local(conn, "m_de", "Solo", url_video="http://de",
                     url_subtitle="http://h/sub.vtt")
        cmd_queue(conn, cfg, qargs(mediathek_id=["m_de"]))
        conn.execute("UPDATE queue SET status='A'")
        cmd_queue(conn, cfg, qargs(queue_cmd="download", all=True))
        target = queue_rows(conn)[0]["path"]
        sidecar = os.path.splitext(target)[0] + ".vtt"
        with open(sidecar, "rb") as fh:
            assert fh.read() == b"SUB"
        assert os.listdir(str(tmp_path / "scratch")) == []
    finally:
        conn.close()


def test_queue_download_existing_target_needs_force(tmp_path, monkeypatch):
    monkeypatch.setattr(theke, "download_file", _fake_dl)
    monkeypatch.setattr(theke, "run_remux", _fake_remux)
    conn = open_db(tmp_path)
    try:
        cfg = download_cfg(tmp_path)
        insert_local(conn, "m_de", "Solo", url_video="http://de")
        cmd_queue(conn, cfg, qargs(mediathek_id=["m_de"]))
        target = queue_rows(conn)[0]["path"]
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(b"OLD")
        conn.execute("UPDATE queue SET status='A'")
        # without force: fails, original kept
        assert cmd_queue(conn, cfg, qargs(queue_cmd="download", all=True)) == \
               {"downloaded": 0, "failed": 1}
        with open(target, "rb") as fh:
            assert fh.read() == b"OLD"
        # with force: overwrites
        conn.execute("UPDATE queue SET status='A'")
        assert cmd_queue(conn, cfg, qargs(queue_cmd="download", all=True, force=True)) == \
               {"downloaded": 1, "failed": 0}
        with open(target, "rb") as fh:
            assert fh.read() == b"MUX"
    finally:
        conn.close()


def test_queue_download_needs_ids_or_all(tmp_path):
    conn = open_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="ids or --all"):
            cmd_queue(conn, CFG, qargs(queue_cmd="download"))
    finally:
        conn.close()


def test_queue_download_cli_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(theke, "download_file", _fake_dl)
    monkeypatch.setattr(theke, "run_remux", _fake_remux)
    db = str(tmp_path / "theke.db")
    lib = (tmp_path / "lib").as_posix() + "/{Title} ({Year})/{Title} ({Year}).mp4"
    cfgpath = tmp_path / "theke.json"
    cfgpath.write_text(json.dumps({"db_path": db, "languages": ["de"],
                                   "temp_path": (tmp_path / "scratch").as_posix(),
                                   "library_path": lib}), encoding="utf-8")
    conn = db_connect(db)
    insert_local(conn, "m_de", "Solo", url_video="http://de")
    conn.close()
    assert main(["--config", str(cfgpath), "queue", "add", "--mediathek-id", "m_de"]) == 0
    assert main(["--config", str(cfgpath), "queue", "approve", "--all"]) == 0
    capsys.readouterr()
    assert main(["--json", "--config", str(cfgpath), "queue", "download", "--all"]) == 0
    assert json.loads(capsys.readouterr().out) == {"downloaded": 1, "failed": 0}
