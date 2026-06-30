"""Tests for the library indexer (phase 12): the pure parsing/walking helpers
in theke.index plus the scan orchestration (resolve/upsert/sweep + safety)."""

import json
import os

import pytest

from theke.index import (parse_folder_title, nfo_tmdb_id, is_lang_variant,
                         probe_attrs, run_ffprobe)


# -- folder-name parser ------------------------------------------------------

def test_parse_folder_title_plain():
    assert parse_folder_title("Die Klapperschlange (1981)") == ("Die Klapperschlange", 1981)


def test_parse_folder_title_trailing_junk():
    # quality/edition suffixes after the year are tolerated; year still wins.
    assert parse_folder_title("Mein Film (2020) [1080p]") == ("Mein Film", 2020)


def test_parse_folder_title_no_year_is_none():
    assert parse_folder_title("No Year Here") is None


# -- nfo uniqueid parser -----------------------------------------------------

def test_nfo_tmdb_id_uniqueid():
    assert nfo_tmdb_id('<movie><uniqueid type="tmdb">603</uniqueid></movie>') == "603"


def test_nfo_tmdb_id_attr_order_and_default():
    # type may sit after other attributes; single quotes accepted.
    assert nfo_tmdb_id("<uniqueid default='true' type='tmdb'>77</uniqueid>") == "77"


def test_nfo_tmdb_id_legacy_tmdbid():
    assert nfo_tmdb_id("<movie><tmdbid>551</tmdbid></movie>") == "551"


def test_nfo_tmdb_id_none_when_only_imdb():
    assert nfo_tmdb_id('<movie><uniqueid type="imdb">tt001</uniqueid></movie>') is None


# -- language-variant filename -----------------------------------------------

def test_is_lang_variant_true():
    assert is_lang_variant("Mein Film (2020).en.mp4") is True


def test_is_lang_variant_false_for_primary():
    assert is_lang_variant("Mein Film (2020).mp4") is False


# -- ffprobe attribute parser ------------------------------------------------

PROBE = {
    "streams": [
        {"codec_type": "video", "width": 1920, "height": 1080},
        {"codec_type": "audio", "tags": {"language": "deu"}},
        {"codec_type": "audio", "tags": {"language": "eng"}},
    ],
    "format": {"duration": "5400.000000"},
}


def test_probe_attrs_full():
    # 5400.0 s -> 5400; deu/eng normalized to de/en.
    assert probe_attrs(PROBE) == {"resolution": "1920x1080",
                                  "duration": 5400, "languages": "de,en"}


def test_probe_attrs_dedup_languages():
    data = {"streams": [{"codec_type": "audio", "tags": {"language": "deu"}},
                        {"codec_type": "audio", "tags": {"language": "ger"}}],
            "format": {}}
    assert probe_attrs(data)["languages"] == "de"   # deu and ger both -> de, deduped


def test_probe_attrs_missing_fields_are_none():
    assert probe_attrs({"streams": [], "format": {}}) == {
        "resolution": None, "duration": None, "languages": None}


def test_run_ffprobe_parses_json(monkeypatch):
    import subprocess
    out = json.dumps(PROBE)
    monkeypatch.setattr(subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""))
    assert run_ffprobe("ffprobe", "x.mp4") == PROBE


def test_run_ffprobe_missing_binary(monkeypatch):
    import subprocess
    def boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(RuntimeError):
        run_ffprobe("ffprobe", "x.mp4")
