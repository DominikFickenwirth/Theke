# -- subtitle (phase 7: subtitle remux) ---------------------------------------
# Convert broadcaster subtitles (TTML/EBU-TT(-D) XML or WebVTT) into player-ready
# sidecars (SRT/ASS/TTML), ffmpeg-free: ffmpeg has no TTML decoder and drops
# colour/position from WebVTT. Mirrors MediathekView's pipeline (detect -> VTT
# normalised up to TTML -> one TTML parse -> per-format export); the load-bearing
# subset is prototyped in analysis/subtitle_pipeline.py. Pure text in, text out.

import re
import xml.etree.ElementTree as ET

# -- TTML namespaces ----------------------------------------------------------
TT = "http://www.w3.org/ns/ttml"
TTS = "http://www.w3.org/ns/ttml#styling"
TTP = "http://www.w3.org/ns/ttml#parameter"
XML = "http://www.w3.org/XML/1998/namespace"


# -- format detection ---------------------------------------------------------
# WebVTT is header-sniffed ("WEBVTT" first line, after an optional BOM);
# otherwise the XML root must be <tt> in the TTML namespace. Anything else
# (HTML page, plain text, malformed XML) is unknown.
def detect_format(text):
    """Classify subtitle text as 'webvtt', 'ttml' or 'unknown'."""
    if text.lstrip("﻿").startswith("WEBVTT"):
        return "webvtt"
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return "unknown"
    return "ttml" if root.tag == f"{{{TT}}}tt" else "unknown"


# -- time parsing -------------------------------------------------------------
# TTML time: clock-time "HH:MM:SS(.fff | :frames)" and offset-time "<n><unit>"
# (h|m|s|ms|f|t). Returns seconds as float, or None for empty/"indefinite".
_CLOCK = re.compile(r"^(\d{2,}):(\d{2}):(\d{2})(?:\.(\d+)|:(\d+))?$")
_OFFSET = re.compile(r"^(\d+(?:\.\d+)?)(h|m|s|ms|f|t)$")


def parse_time(raw, frame_rate=30, tick_rate=1):
    """One TTML time expression to seconds (None for empty/indefinite)."""
    s = (raw or "").strip()
    if not s or s == "indefinite":
        return None
    m = _CLOCK.match(s)
    if m:
        h, mi, se, frac, frames = m.groups()
        total = int(h) * 3600 + int(mi) * 60 + int(se)
        if frac:
            total += float("0." + frac)
        elif frames:
            total += int(frames) / frame_rate
        return float(total)
    m = _OFFSET.match(s)
    if m:
        count, metric = float(m.group(1)), m.group(2)
        return {
            "h": count * 3600, "m": count * 60, "s": count,
            "ms": count / 1000, "f": count / frame_rate, "t": count / tick_rate,
        }[metric]
    raise ValueError(f"Unsupported TTML time expression: {raw!r}")
