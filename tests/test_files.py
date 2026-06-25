"""Tests for the file primitives (phases 6-8): download, remux, move."""

import json

import pytest

import theke
import theke.files as files
from theke import Config, main
from theke.files import (is_hls, is_master, parse_master, parse_media_playlist)


# -- m3u8 parsing (pure) ------------------------------------------------------

def test_is_hls_by_suffix():
    assert is_hls("https://h/x/playlist.m3u8") is True
    assert is_hls("https://h/x/PLAYLIST.M3U8") is True


def test_is_hls_ignores_query_string():
    assert is_hls("https://h/x/playlist.m3u8?token=abc&t=1") is True
    assert is_hls("https://h/x/video.mp4?fmt=m3u8") is False


def test_is_hls_false_for_plain_media():
    assert is_hls("https://h/x/video.mp4") is False
    assert is_hls("https://h/x/audio.m4a") is False


def test_is_master_detects_stream_inf():
    assert is_master("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\na.m3u8\n") is True
    assert is_master("#EXTM3U\n#EXTINF:6.0,\nseg0.ts\n") is False


MASTER = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
    "low/index.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720\n"
    "high/index.m3u8\n"
)


def test_parse_master_resolves_and_keeps_order():
    out = parse_master(MASTER, "https://h/v/master.m3u8")
    assert out == [(800000, "https://h/v/low/index.m3u8"),
                   (2400000, "https://h/v/high/index.m3u8")]


MEDIA = (
    "#EXTM3U\n"
    "#EXT-X-TARGETDURATION:6\n"
    "#EXTINF:6.000,\n"
    "seg0.ts\n"
    "#EXTINF:6.000,\n"
    "seg1.ts\n"
    "#EXT-X-ENDLIST\n"
)


def test_parse_media_playlist_segments_in_order():
    init, segs, enc = parse_media_playlist(MEDIA, "https://h/v/media.m3u8")
    assert init is None
    assert segs == ["https://h/v/seg0.ts", "https://h/v/seg1.ts"]
    assert enc is False


def test_parse_media_playlist_init_segment():
    text = ('#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n'
            '#EXTINF:4.0,\nseg0.m4s\n#EXT-X-ENDLIST\n')
    init, segs, enc = parse_media_playlist(text, "https://h/v/media.m3u8")
    assert init == "https://h/v/init.mp4"
    assert segs == ["https://h/v/seg0.m4s"]
    assert enc is False


def test_parse_media_playlist_encrypted_flagged():
    text = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n'
            '#EXTINF:6.0,\nseg0.ts\n#EXT-X-ENDLIST\n')
    init, segs, enc = parse_media_playlist(text, "https://h/v/media.m3u8")
    assert enc is True


def test_parse_media_playlist_method_none_not_encrypted():
    text = ('#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n'
            '#EXTINF:6.0,\nseg0.ts\n#EXT-X-ENDLIST\n')
    init, segs, enc = parse_media_playlist(text, "https://h/v/media.m3u8")
    assert enc is False
