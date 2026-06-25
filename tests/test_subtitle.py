"""Tests for the subtitle converter (TTML/EBU-TT + WebVTT -> SRT/ASS/TTML)."""

import pytest

from theke import subtitle


# -- parse_time ---------------------------------------------------------------
# Expected values pre-calculated by hand from the TTML time grammar:
#   clock  HH:MM:SS(.fff | :frames);  offset  <n>{h|m|s|ms|f|t}.

def test_parse_time_clock_fractional():
    # 0*3600 + 0*60 + 1 + 0.600
    assert subtitle.parse_time("00:00:01.600") == pytest.approx(1.6)


def test_parse_time_clock_whole_minutes_hours():
    assert subtitle.parse_time("00:01:02") == pytest.approx(62.0)        # 60 + 2
    assert subtitle.parse_time("01:00:00.000") == pytest.approx(3600.0)  # 1h


def test_parse_time_clock_frames_uses_frame_rate():
    # 10s + 12 frames / 25 fps = 10 + 0.48
    assert subtitle.parse_time("00:00:10:12", frame_rate=25) == pytest.approx(10.48)


def test_parse_time_offset_seconds_and_millis():
    assert subtitle.parse_time("5s") == pytest.approx(5.0)
    assert subtitle.parse_time("100ms") == pytest.approx(0.1)


def test_parse_time_offset_frames_and_ticks():
    assert subtitle.parse_time("270f", frame_rate=25) == pytest.approx(10.8)   # 270/25
    assert subtitle.parse_time("250t", tick_rate=1000) == pytest.approx(0.25)  # 250/1000


def test_parse_time_empty_and_indefinite_are_none():
    assert subtitle.parse_time("") is None
    assert subtitle.parse_time(None) is None
    assert subtitle.parse_time("indefinite") is None


def test_parse_time_rejects_garbage():
    with pytest.raises(ValueError):
        subtitle.parse_time("nonsense")


# -- detect_format ------------------------------------------------------------

TTML_MIN = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
    '<p begin="00:00:01.000" end="00:00:02.000">Hallo</p>'
    '</div></body></tt>'
)

VTT_MIN = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHallo\n"

# Real NDR subtitle URLs serve an HTML page, not a subtitle file.
HTML_PAGE = '<!DOCTYPE html>\n<html><head><title>UT</title></head><body></body></html>'


def test_detect_format_ttml():
    assert subtitle.detect_format(TTML_MIN) == "ttml"


def test_detect_format_webvtt():
    assert subtitle.detect_format(VTT_MIN) == "webvtt"


def test_detect_format_webvtt_with_bom():
    assert subtitle.detect_format("﻿" + VTT_MIN) == "webvtt"


def test_detect_format_html_is_unknown():
    assert subtitle.detect_format(HTML_PAGE) == "unknown"


def test_detect_format_plaintext_is_unknown():
    assert subtitle.detect_format("just some text, not a subtitle") == "unknown"


def test_detect_format_malformed_xml_is_unknown():
    assert subtitle.detect_format("<tt><body><p>unclosed") == "unknown"


# -- parse_ttml ---------------------------------------------------------------

TTML_DOC = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<tt xmlns="http://www.w3.org/ns/ttml"'
    ' xmlns:tts="http://www.w3.org/ns/ttml#styling"'
    ' xmlns:ttp="http://www.w3.org/ns/ttml#parameter" ttp:frameRate="25">'
    '<head><styling>'
    '<style xml:id="s1" tts:fontStyle="italic"/>'
    '<style xml:id="s2" tts:color="yellow"/>'
    '</styling></head>'
    '<body><div>'
    '<p begin="00:00:01.000" end="00:00:03.000">Erste Zeile<br/>Zweite Zeile</p>'
    '<p begin="00:00:04.000" end="00:00:06.000">Normal <span style="s1">kursiv</span> Ende</p>'
    '<p begin="5s" end="8.5s"><span style="s2">Gelb</span></p>'
    '</div></body></tt>'
)


def test_parse_ttml_cue_count_and_times():
    doc = subtitle.parse_ttml(TTML_DOC)
    assert len(doc.cues) == 3
    assert (doc.cues[0].start, doc.cues[0].end) == (1.0, 3.0)
    assert (doc.cues[2].start, doc.cues[2].end) == (5.0, 8.5)


def test_parse_ttml_br_becomes_newline_run():
    doc = subtitle.parse_ttml(TTML_DOC)
    assert doc.cues[0].runs == [
        subtitle.Run("Erste Zeile", subtitle.Style()),
        subtitle.Run("\n", subtitle.Style()),
        subtitle.Run("Zweite Zeile", subtitle.Style()),
    ]


def test_parse_ttml_inline_italic_span():
    doc = subtitle.parse_ttml(TTML_DOC)
    assert doc.cues[1].runs == [
        subtitle.Run("Normal ", subtitle.Style()),
        subtitle.Run("kursiv", subtitle.Style(italic=True)),
        subtitle.Run(" Ende", subtitle.Style()),
    ]


def test_parse_ttml_referenced_style_colour():
    doc = subtitle.parse_ttml(TTML_DOC)
    # named "yellow" resolves to #FFFF00 (Ttml2Color table)
    assert doc.cues[2].runs == [
        subtitle.Run("Gelb", subtitle.Style(color="#FFFF00")),
    ]
