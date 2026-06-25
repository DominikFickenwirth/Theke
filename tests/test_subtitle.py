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
