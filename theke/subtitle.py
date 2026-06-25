# -- subtitle (phase 7: subtitle remux) ---------------------------------------
# Convert broadcaster subtitles (TTML/EBU-TT(-D) XML or WebVTT) into player-ready
# sidecars (SRT/ASS/TTML), ffmpeg-free: ffmpeg has no TTML decoder and drops
# colour/position from WebVTT. Mirrors MediathekView's pipeline (detect -> VTT
# normalised up to TTML -> one TTML parse -> per-format export); the load-bearing
# subset is prototyped in analysis/subtitle_pipeline.py. Pure text in, text out.

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

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


# -- intermediate model -------------------------------------------------------
# A cue is a time span plus a list of styled runs (mirrors MV's SubtitleDocument).
# Styles OR-merge for bold/italic/underline; colour is last-specified-wins.

@dataclass(frozen=True)
class Style:
    bold:      bool = False
    italic:    bool = False
    underline: bool = False
    color:     str | None = None   # "#RRGGBB"

    def merge(self, child):
        if child is None:
            return self
        return Style(self.bold or child.bold,
                     self.italic or child.italic,
                     self.underline or child.underline,
                     child.color if child.color else self.color)


@dataclass(frozen=True)
class CueStyle:
    """Block-level layout; child overrides parent."""
    display_align: str | None = None
    text_align:    str | None = None

    def merge(self, child):
        if child is None:
            return self
        return CueStyle(child.display_align or self.display_align,
                        child.text_align or self.text_align)


@dataclass(frozen=True)
class Region:
    """Region rect + align hints. origin/extent are (value, unit) length pairs
    with unit 'px' or '%', or None for defaults."""
    origin:        tuple | None
    extent:        tuple | None
    display_align: str | None
    text_align:    str | None


@dataclass
class Run:
    text:  str
    style: Style = Style()


@dataclass
class Cue:
    start:      float
    end:        float
    runs:       list = field(default_factory=list)
    region_id:  str | None = None         # ASS placement needs it, SRT does not
    cue_style:  CueStyle = CueStyle()


@dataclass
class Document:
    """Parsed subtitle: ordered cues plus the region table they reference."""
    cues:    list = field(default_factory=list)
    regions: dict = field(default_factory=dict)


# -- TTML parse ---------------------------------------------------------------
# One recursive walk: resolve the <style id=...> table, then descend
# body/div/p accumulating begin/end and a style stack across <span> nesting,
# emitting a Run per text node and "\n" per <br/>.
_NAMED = {  # subset of Ttml2Color's table
    "white": "#FFFFFF", "yellow": "#FFFF00", "cyan": "#00FFFF",
    "black": "#000000", "red": "#FF0000",
}


def _color(raw):
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("#"):
        return "#" + raw[1:7].upper()   # drop alpha; SRT has no alpha
    return _NAMED.get(raw.lower())


def _style_of(el):
    """Inline tts:* styling on one element (no ref resolution)."""
    g = lambda n: el.get(f"{{{TTS}}}{n}")
    return Style(bold=(g("fontWeight") == "bold"),
                 italic=(g("fontStyle") == "italic"),
                 underline=("underline" in (g("textDecoration") or "")),
                 color=_color(g("color")))


def _cuestyle_of(el):
    """Inline tts:displayAlign / tts:textAlign on one element."""
    return CueStyle(el.get(f"{{{TTS}}}displayAlign"), el.get(f"{{{TTS}}}textAlign"))


def _resolve(el, base, styles):
    """style="ref ..." then inline (StyleIndex.resolveTextStyle)."""
    s = base
    for sid in (el.get("style") or "").split():
        if sid in styles:
            s = s.merge(styles[sid])
    return s.merge(_style_of(el))


def _resolve_cue(el, cue_styles):
    """Style refs then inline, for block layout (resolveCueStyle)."""
    s = CueStyle()
    for sid in (el.get("style") or "").split():
        if sid in cue_styles:
            s = s.merge(cue_styles[sid])
    return s.merge(_cuestyle_of(el))


def _parse_len(tok):
    tok = tok.strip()
    if tok.endswith("px"):
        return (float(tok[:-2]), "px")
    if tok.endswith("%"):
        return (float(tok[:-1]), "%")
    raise ValueError(f"Unsupported length (only px/%): {tok!r}")


def _parse_len2(v):
    parts = (v or "").split()
    return (_parse_len(parts[0]), _parse_len(parts[1])) if len(parts) == 2 else None


def _collect(el, style, styles, out):
    """Flatten text + <span>/<br> into Runs (Ttml2Parser.collectRuns)."""
    if el.text:
        out.append(Run(el.text, style))
    for child in el:
        tag = child.tag.split("}")[-1]
        if tag == "br":
            out.append(Run("\n", style))
        elif tag == "span":
            _collect(child, _resolve(child, style, styles), styles, out)
        else:
            _collect(child, style, styles, out)
        if child.tail:
            out.append(Run(child.tail, style))


def parse_ttml(text):
    """Parse TTML/EBU-TT(-D) into a Document (cues + region table)."""
    root = ET.fromstring(text)
    frame_rate = float(root.get(f"{{{TTP}}}frameRate") or 30)
    tick_rate = float(root.get(f"{{{TTP}}}tickRate") or 1)

    styles, cue_styles = {}, {}
    for st in root.iter(f"{{{TT}}}style"):
        sid = st.get(f"{{{XML}}}id")
        if sid:
            styles[sid] = _style_of(st)
            cue_styles[sid] = _cuestyle_of(st)

    regions = {}
    layout = root.find(f"{{{TT}}}head/{{{TT}}}layout")
    for r in (layout.findall(f"{{{TT}}}region") if layout is not None else []):
        rid = r.get(f"{{{XML}}}id")
        if rid:
            cs = _resolve_cue(r, cue_styles)
            regions[rid] = Region(_parse_len2(r.get(f"{{{TTS}}}origin")),
                                  _parse_len2(r.get(f"{{{TTS}}}extent")),
                                  cs.display_align, cs.text_align)

    cues = []

    def walk(el, begin, end, style, region_id, cue_style):
        if el.get("begin"):
            begin = parse_time(el.get("begin"), frame_rate, tick_rate)
        if el.get("end"):
            end = parse_time(el.get("end"), frame_rate, tick_rate)
        style = _resolve(el, style, styles)
        region_id = el.get("region") or region_id
        cue_style = cue_style.merge(_resolve_cue(el, cue_styles))
        if el.tag.split("}")[-1] == "p":
            cue = Cue(begin or 0.0, end or 0.0, region_id=region_id, cue_style=cue_style)
            _collect(el, style, styles, cue.runs)
            if any(r.text.strip() and r.text != "\n" for r in cue.runs):
                cues.append(cue)
            return
        for child in el:
            walk(child, begin, end, style, region_id, cue_style)

    body = root.find(f"{{{TT}}}body")
    if body is not None:
        walk(body, None, None, Style(), None, CueStyle())
    cues.sort(key=lambda c: (c.start, c.end))
    return Document(cues, regions)
