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


# -- SRT export ---------------------------------------------------------------
# index, "HH:MM:SS,mmm --> ...", then runs wrapped in <b>/<i>/<u>/<font color>.
# Tags open/close only at style boundaries between runs.
def _srt_ts(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _open(s):
    t = ""
    if s.bold:      t += "<b>"
    if s.italic:    t += "<i>"
    if s.underline: t += "<u>"
    if s.color:     t += f'<font color="{s.color}">'
    return t


def _close(s):
    t = ""
    if s.color:     t += "</font>"
    if s.underline: t += "</u>"
    if s.italic:    t += "</i>"
    if s.bold:      t += "</b>"
    return t


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def export_srt(doc):
    """Serialise a Document to SubRip (.srt) text."""
    out = []
    for i, cue in enumerate(doc.cues, 1):
        out.append(str(i))
        out.append(f"{_srt_ts(cue.start)} --> {_srt_ts(cue.end)}")
        line, prev = "", None
        for run in cue.runs:
            if prev is not None and prev != run.style:
                line += _close(prev)
            if prev is None or prev != run.style:
                line += _open(run.style)
            line += _esc(run.text)
            prev = run.style
        if prev is not None:
            line += _close(prev)
        out.append(line)
        out.append("")
    return "\n".join(out)


# -- WebVTT -> TTML2 ----------------------------------------------------------
# WebVTT does NOT export to SRT directly; it is rewritten into a TTML string
# (with a fixed <head> style table for the broadcaster colour classes) and
# re-enters the same parser/exporters path. Broadcaster VTT is well-formed, so
# the block-splitter and plain span stack below are enough in practice.
VTT_CLASS_STYLE = {  # WebVTT <c.class> -> <style xml:id> emitted in the head
    "textWhite":  "cTextWhite",
    "textYellow": "cTextYellow",
    "textCyan":   "cTextCyan",
}
VTT_ALIGN = {  # cue setting align:* -> tts:textAlign
    "start": "start", "left": "start",
    "middle": "center", "center": "center",
    "end": "end", "right": "end",
}
_TAG = re.compile(r"<[^>]*>")


def _vtt_ts(ts):
    """MM:SS.mmm or HH:MM:SS.mmm -> seconds."""
    hms, dot, ms = ts.strip().partition(".")
    if not dot or len(ms) != 3 or not ms.isdigit():
        raise ValueError(f"Invalid VTT timestamp: {ts!r}")
    parts = hms.split(":")
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = "0", parts[0], parts[1]
    else:
        raise ValueError(f"Invalid VTT timestamp: {ts!r}")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _vtt_fmt(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def parse_vtt(text):
    """Split into cues: (begin, end, settings, payload). Mini block-splitter."""
    cues = []
    for bi, block in enumerate(re.split(r"\r?\n\r?\n", text.strip())):
        lines = block.splitlines()
        if not lines:
            continue
        if bi == 0 and lines[0].startswith("WEBVTT"):
            continue
        if lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        if "-->" not in lines[0]:
            lines = lines[1:]   # drop the optional cue-identifier line
        left, _, rest = lines[0].partition("-->")
        rest = rest.split(None, 1)
        begin, end = _vtt_ts(left), _vtt_ts(rest[0])
        settings = {}
        for tok in (rest[1].split() if len(rest) > 1 else []):
            k, _, v = tok.partition(":")
            if k and v:
                settings[k] = v
        cues.append((begin, end, settings, "\n".join(lines[1:])))
    return cues


def _is_vtt_ts_tag(raw):
    """Cue-internal timestamp tag like <00:00:01.000> -- stripped on convert."""
    t = raw.strip()
    if not t or " " in t or t.startswith("/") or "." not in t:
        return False
    left, _, right = t.partition(".")
    return (right.isdigit() and len(right) == 3
            and all(p.isdigit() for p in left.split(":")) and len(left.split(":")) in (2, 3))


def _vtt_text(s):
    """Escape text and map newlines to <br/>."""
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;").replace("\n", "<br/>"))


def convert_inline(text):
    """VTT inline markup -> TTML spans (simplified stack)."""
    out, stack, pos = [], [], 0
    for m in _TAG.finditer(text):
        out.append(_vtt_text(text[pos:m.start()]))
        pos = m.end()
        raw = m.group()[1:-1].strip()
        if _is_vtt_ts_tag(raw):
            continue
        closing = raw.startswith("/")
        inner = raw[1:].strip() if closing else raw
        name = re.split(r"[ .]", inner, maxsplit=1)[0]
        if name == "br":
            out.append("<br/>")
        elif name in ("b", "i", "u"):
            attr = {"b": 'tts:fontWeight="bold"', "i": 'tts:fontStyle="italic"',
                    "u": 'tts:textDecoration="underline"'}[name]
            if closing:
                if stack: out.append(stack.pop())
            else:
                out.append(f"<span {attr}>"); stack.append("</span>")
        elif name == "c":
            if closing:
                if stack: out.append(stack.pop())
            else:
                sid = next((VTT_CLASS_STYLE[c] for c in inner.split(".")[1:]
                            if c in VTT_CLASS_STYLE), None)
                if sid:
                    out.append(f'<span style="{sid}">'); stack.append("</span>")
                else:
                    stack.append("")   # unknown class: keep <c> balance, emit no span
        # unknown tags (e.g. <v Fred>, <lang en>) stripped, content kept
    out.append(_vtt_text(text[pos:]))
    while stack:
        out.append(stack.pop())
    return "".join(out)


def vtt_to_ttml2(text):
    """Rewrite a WebVTT string into an equivalent TTML2 string."""
    if not text.lstrip("﻿").startswith("WEBVTT"):
        raise ValueError("Not a WebVTT file (missing WEBVTT header)")
    head = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<tt xmlns="http://www.w3.org/ns/ttml"'
        ' xmlns:tts="http://www.w3.org/ns/ttml#styling"'
        ' xmlns:ttp="http://www.w3.org/ns/ttml#parameter"'
        ' ttp:timeBase="media" ttp:frameRate="30">\n'
        '  <head><styling>\n'
        '    <style xml:id="cTextWhite" tts:color="#FFFFFF"/>\n'
        '    <style xml:id="cTextYellow" tts:color="#FFFF00"/>\n'
        '    <style xml:id="cTextCyan" tts:color="#00FFFF"/>\n'
        '  </styling></head>\n'
        '  <body><div>\n'
    )
    body = []
    for begin, end, settings, payload in parse_vtt(text):
        align = VTT_ALIGN.get(settings.get("align"))
        a = f' tts:textAlign="{align}"' if align else ""
        body.append(f'    <p begin="{_vtt_fmt(begin)}" end="{_vtt_fmt(end)}"{a}>'
                    f'{convert_inline(payload)}</p>\n')
    return head + "".join(body) + "  </div></body>\n</tt>\n"


# -- ASS export ---------------------------------------------------------------
# A Dialogue line per cue, placed on screen via {\pos(x,y)\an<n>} derived from
# the cue's region rect + text/displayAlign. Style transitions are inline
# override tags (\b1 \i1 \u1, \1c colour in BGR).
_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
Collisions: Normal
PlayResX: {x}
PlayResY: {y}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,24,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2,1,2,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_ts(sec):
    ms = max(0, int(round(sec * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:d}:{m:02d}:{s:02d}.{ms // 10:02d}"   # centiseconds


def _ass_escape(s):
    # Neutralise ASS override braces with fullwidth look-alikes (U+FF5B/U+FF5D);
    # written as chr() so this source stays CP-1252-safe.
    return s.replace("{", chr(0xFF5B)).replace("}", chr(0xFF5D))


def _resolve_len(length, ref):
    value, unit = length
    return value if unit == "px" else ref * value / 100.0


def _resolve_rect(region, play_x, play_y):
    x, y, w, h = 0, 0, play_x, play_y
    if region and region.origin:
        x = round(_resolve_len(region.origin[0], play_x))
        y = round(_resolve_len(region.origin[1], play_y))
    if region and region.extent:
        w = round(_resolve_len(region.extent[0], play_x))
        h = round(_resolve_len(region.extent[1], play_y))
    return x, y, w, h


def _anchor(rect, text_align, display_align):
    """Map rect + align hints to an ASS \\pos point + \\an numpad anchor."""
    rx, ry, w, h = rect
    ta = (text_align or "center").lower()
    if "end" in ta or "right" in ta:
        x, col = rx + w, 3
    elif "start" in ta or "left" in ta:
        x, col = rx, 1
    else:
        x, col = rx + w // 2, 2
    da = (display_align or "after").lower()
    if "before" in da or "top" in da:
        y, rowbase = ry, 6
    elif "center" in da or "middle" in da:
        y, rowbase = ry + h // 2, 3
    else:
        y, rowbase = ry + h, 0
    return x, y, rowbase + col


def _ass_delta(prev, cur):
    """Override tags for the change from one run's style to the next."""
    s = ""
    if prev.bold != cur.bold:
        s += "\\b1" if cur.bold else "\\b0"
    if prev.italic != cur.italic:
        s += "\\i1" if cur.italic else "\\i0"
    if prev.underline != cur.underline:
        s += "\\u1" if cur.underline else "\\u0"
    if prev.color != cur.color:
        if cur.color:
            h = cur.color.lstrip("#")
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            s += f"\\1c&H{b:02X}{g:02X}{r:02X}&\\1a&H00&"   # ASS colour is BGR, opaque
        else:
            s += "\\1a&H00&"
    return s


def export_ass(doc, play_x=384, play_y=288):
    """Serialise a Document to Advanced SubStation Alpha (.ass) text."""
    out = [_ASS_HEADER.format(x=play_x, y=play_y)]
    for cue in doc.cues:
        region = doc.regions.get(cue.region_id)
        rect = _resolve_rect(region, play_x, play_y)
        text_align = cue.cue_style.text_align or (region.text_align if region else None)
        display_align = cue.cue_style.display_align or (region.display_align if region else None)
        x, y, an = _anchor(rect, text_align, display_align)
        parts = [f"{{\\pos({x},{y})\\an{an}}}"]
        prev = Style()
        for run in cue.runs:
            if run.style != prev:
                parts.append("{" + _ass_delta(prev, run.style) + "}")
                prev = run.style
            parts.append(_ass_escape(run.text).replace("\n", "\\N"))
        out.append(f"Dialogue: 0,{_ass_ts(cue.start)},{_ass_ts(cue.end)},"
                   f"Default,,0,0,0,,{''.join(parts)}")
    return "\n".join(out) + "\n"


# -- TTML export --------------------------------------------------------------
# Re-serialise the model to inline-styled TTML (media-time clock); styled runs
# become tts:* spans, "\n" runs become <br/>. Positioning/regions are dropped.
def _ttml_style_attrs(style):
    a = []
    if style.bold:      a.append('tts:fontWeight="bold"')
    if style.italic:    a.append('tts:fontStyle="italic"')
    if style.underline: a.append('tts:textDecoration="underline"')
    if style.color:     a.append(f'tts:color="{style.color}"')
    return " ".join(a)


def _ttml_run(run):
    if run.text == "\n":
        return "<br/>"
    text = _esc(run.text)
    attrs = _ttml_style_attrs(run.style)
    return f"<span {attrs}>{text}</span>" if attrs else text


def export_ttml(doc):
    """Serialise a Document to inline-styled TTML text."""
    head = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<tt xmlns="http://www.w3.org/ns/ttml"'
        ' xmlns:tts="http://www.w3.org/ns/ttml#styling"'
        ' xmlns:ttp="http://www.w3.org/ns/ttml#parameter"'
        ' ttp:timeBase="media">\n'
        '  <body><div>\n'
    )
    body = []
    for cue in doc.cues:
        content = "".join(_ttml_run(r) for r in cue.runs)
        body.append(f'    <p begin="{_vtt_fmt(cue.start)}" end="{_vtt_fmt(cue.end)}">'
                    f'{content}</p>\n')
    return head + "".join(body) + "  </div></body>\n</tt>\n"
