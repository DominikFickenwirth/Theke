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


# -- export_srt ---------------------------------------------------------------
# Derived by hand from TTML_DOC: 1-based index, "HH:MM:SS,mmm --> ...", then the
# run text with <i>/<font> tags opened/closed at style boundaries, blank line
# between blocks. 8.5s -> 00:00:08,500.
EXPECTED_SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:03,000\n"
    "Erste Zeile\n"
    "Zweite Zeile\n"
    "\n"
    "2\n"
    "00:00:04,000 --> 00:00:06,000\n"
    "Normal <i>kursiv</i> Ende\n"
    "\n"
    "3\n"
    "00:00:05,000 --> 00:00:08,500\n"
    '<font color="#FFFF00">Gelb</font>\n'
)


def test_export_srt_matches_expected():
    doc = subtitle.parse_ttml(TTML_DOC)
    assert subtitle.export_srt(doc) == EXPECTED_SRT


# -- vtt_to_ttml2 -------------------------------------------------------------
# WebVTT is normalised "up" to TTML so it flows through the same parser. A
# <c.textYellow> class maps to the cTextYellow head style (tts:color #FFFF00);
# <i> to italic; cue newlines to <br/>.
VTT_SAMPLE = (
    "WEBVTT\n\n"
    "1\n"
    "00:00:01.000 --> 00:00:04.000 align:center\n"
    "Guten <c.textYellow>Abend</c>.\n"
    "Willkommen.\n\n"
    "2\n"
    "00:00:05.000 --> 00:00:08.500\n"
    "Eine <i>kursive</i> Stelle.\n"
)


def test_vtt_to_ttml2_is_detected_as_ttml():
    assert subtitle.detect_format(subtitle.vtt_to_ttml2(VTT_SAMPLE)) == "ttml"


def test_vtt_flows_through_ttml_parser():
    doc = subtitle.parse_ttml(subtitle.vtt_to_ttml2(VTT_SAMPLE))
    assert len(doc.cues) == 2
    assert (doc.cues[0].start, doc.cues[0].end) == (1.0, 4.0)
    assert doc.cues[0].runs == [
        subtitle.Run("Guten ", subtitle.Style()),
        subtitle.Run("Abend", subtitle.Style(color="#FFFF00")),
        subtitle.Run(".", subtitle.Style()),
        subtitle.Run("\n", subtitle.Style()),
        subtitle.Run("Willkommen.", subtitle.Style()),
    ]
    assert doc.cues[1].runs == [
        subtitle.Run("Eine ", subtitle.Style()),
        subtitle.Run("kursive", subtitle.Style(italic=True)),
        subtitle.Run(" Stelle.", subtitle.Style()),
    ]


# -- export_ass ---------------------------------------------------------------
# Placement derived by hand at PlayRes 384x288:
#   region "top": origin 0%/0% -> (0,0); extent 100%/20% -> (384, round(57.6)=58);
#     textAlign center + displayAlign before -> \pos(0+384/2, 0) = \pos(192,0), \an8.
#   no region: rect (0,0,384,288); default center/after -> \pos(192,288), \an2.
#   colour #FF0000 -> ASS BGR opaque -> \1c&H0000FF&\1a&H00&. Times in centiseconds.
TTML_ASS = (
    '<tt xmlns="http://www.w3.org/ns/ttml" xmlns:tts="http://www.w3.org/ns/ttml#styling">'
    '<head><layout>'
    '<region xml:id="top" tts:origin="0% 0%" tts:extent="100% 20%"'
    ' tts:displayAlign="before" tts:textAlign="center"/>'
    '</layout></head>'
    '<body><div>'
    '<p begin="00:00:01.000" end="00:00:02.000" region="top">Oben</p>'
    '<p begin="00:00:03.000" end="00:00:04.000"><span tts:color="#FF0000">Rot</span></p>'
    '</div></body></tt>'
)


def test_export_ass_dialogue_lines():
    doc = subtitle.parse_ttml(TTML_ASS)
    dialogues = [l for l in subtitle.export_ass(doc).splitlines()
                 if l.startswith("Dialogue")]
    assert dialogues == [
        r"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\pos(192,0)\an8}Oben",
        r"Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,{\pos(192,288)\an2}{\1c&H0000FF&\1a&H00&}Rot",
    ]


def test_export_ass_header_carries_playres():
    doc = subtitle.parse_ttml(TTML_ASS)
    head = subtitle.export_ass(doc)
    assert "PlayResX: 384" in head and "PlayResY: 288" in head


# -- export_ttml --------------------------------------------------------------
# Inline-styled TTML, media-time clock HH:MM:SS.mmm. Default-style runs are bare
# text; styled runs become tts:* spans; "\n" runs become <br/>.
TINY_TTML = (
    '<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
    '<p begin="00:00:01.000" end="00:00:02.000">Hallo</p>'
    '</div></body></tt>'
)

EXPECTED_TTML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<tt xmlns="http://www.w3.org/ns/ttml"'
    ' xmlns:tts="http://www.w3.org/ns/ttml#styling"'
    ' xmlns:ttp="http://www.w3.org/ns/ttml#parameter"'
    ' ttp:timeBase="media">\n'
    '  <body><div>\n'
    '    <p begin="00:00:01.000" end="00:00:02.000">Hallo</p>\n'
    '  </div></body>\n'
    '</tt>\n'
)


def test_export_ttml_matches_expected():
    doc = subtitle.parse_ttml(TINY_TTML)
    assert subtitle.export_ttml(doc) == EXPECTED_TTML


def test_export_ttml_round_trips_cues():
    doc = subtitle.parse_ttml(TTML_DOC)
    reparsed = subtitle.parse_ttml(subtitle.export_ttml(doc))
    original = [(c.start, c.end, c.runs) for c in doc.cues]
    again = [(c.start, c.end, c.runs) for c in reparsed.cues]
    assert again == original


# -- convert (orchestrator) ---------------------------------------------------

def test_convert_ttml_to_all_formats():
    result = subtitle.convert(TTML_DOC, ["srt", "ass", "ttml"])
    assert set(result) == {"srt", "ass", "ttml"}
    assert result["srt"] == EXPECTED_SRT
    assert "Dialogue" in result["ass"]
    assert result["ttml"].startswith('<?xml')


def test_convert_accepts_webvtt_input():
    result = subtitle.convert(VTT_SAMPLE, ["srt"])
    assert "srt" in result
    assert "Guten" in result["srt"]


def test_convert_unknown_input_is_empty():
    assert subtitle.convert(HTML_PAGE, ["srt", "ass"]) == {}


def test_convert_skips_unknown_format():
    result = subtitle.convert(TTML_DOC, ["srt", "bogus"])
    assert set(result) == {"srt"}
