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
