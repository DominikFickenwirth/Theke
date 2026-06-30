# -- library indexer (phase 12: theke library scan) --------------------------
# Walk the on-disk movie library and reconcile it with the `library` table:
# identify each film (known DB path / Kodi nfo uniqueid / folder-name + TMDB
# search), probe its physical attributes via ffprobe, detect deletions by a
# mark-and-sweep over indexed_at, and report unidentified folders. The DB is the
# authority; the media server (Kodi/Emby/Jellyfin/Plex) is never read as one. Pure
# parsing/walking helpers live here; CLI wiring + TMDB/DB access stay in
# __init__.py. ffprobe is a seam (run_ffprobe), monkeypatched in tests.

import json
import os
import re
import subprocess

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".flv",
              ".webm", ".mpg", ".mpeg"}

# Kodi/Emby/Jellyfin extras subfolders: never their own film, skipped while walking.
EXTRAS_DIRS = {"behind the scenes", "extras", "featurettes", "trailers",
               "deleted scenes", "making of", "interviews", "scenes", "shorts",
               "other", "sample", "samples"}

IGNORE_MARKER = ".thekeignore"   # a folder holding this is skipped (Theke-specific)


# -- name / nfo parsing ------------------------------------------------------

_FOLDER_RX = re.compile(r"^(.+?)\s*\((\d{4})\)")
_UNIQUEID_RX = re.compile(r"""<uniqueid[^>]*\btype=["']tmdb["'][^>]*>\s*(\d+)""", re.I)
_TMDBID_RX = re.compile(r"<tmdbid>\s*(\d+)", re.I)


def parse_folder_title(name) -> tuple | None:
    """Pull (title, year) from a 'Title (Year)' folder name, tolerating trailing
    quality/edition junk after the year. None when there is no parenthesized year
    (the folder is then reported unresolved rather than guessed)."""
    m = _FOLDER_RX.match(name)
    return (m.group(1).strip(), int(m.group(2))) if m else None


def nfo_tmdb_id(text) -> str | None:
    """The TMDB id from a Kodi-style nfo: <uniqueid type="tmdb">, else the legacy
    <tmdbid>; None when neither is present. Regex (not an XML parse) so a slightly
    malformed nfo still yields its id. The format is shared across media servers
    (Kodi/Emby/Jellyfin/Plex) -- reading it is platform-neutral, not a media-server binding."""
    m = _UNIQUEID_RX.search(text) or _TMDBID_RX.search(text)
    return m.group(1) if m else None


def is_lang_variant(filename) -> bool:
    """True when a video filename carries a '.<xx>' language infix (a 2-letter code
    before the extension, e.g. 'Film (2020).en.mp4') -- a non-anchor language copy,
    matching the infix _library_path writes for secondary picks."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    head, dot, tail = stem.rpartition(".")
    return bool(dot) and len(tail) == 2 and tail.isalpha()


# -- ffprobe ------------------------------------------------------------------

# ISO 639-2 (and a few /B variants) -> the 2-letter codes mediathek/queue use, so
# library languages read the same way; an unknown tag passes through unchanged.
_ISO2 = {"deu": "de", "ger": "de", "eng": "en", "fra": "fr", "fre": "fr",
         "spa": "es", "ita": "it", "nld": "nl", "dut": "nl", "pol": "pl",
         "rus": "ru", "tur": "tr", "ara": "ar", "por": "pt"}


def run_ffprobe(ffprobe_path, path) -> dict:
    """Probe `path` with ffprobe and return the parsed -print_format json dict
    (streams + format). Raises on a missing binary or non-zero exit. The subprocess
    seam -- monkeypatched in tests."""
    args = [ffprobe_path, "-v", "quiet", "-show_streams", "-show_format",
            "-print_format", "json", path]
    try:
        proc = subprocess.run(args, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(f"ffprobe not found: {ffprobe_path} (set ffprobe_path)") from None
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed (exit {proc.returncode}): {path}")
    return json.loads(proc.stdout or "{}")


def probe_attrs(data) -> dict:
    """Physical attributes from a run_ffprobe dict: resolution 'WxH' (first video
    stream), duration in whole seconds (format.duration), and the comma-joined audio
    languages (normalized to 2-letter, deduped, in stream order). Each is None when
    the source carries no such information."""
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    resolution = f"{video['width']}x{video['height']}" if video and video.get("width") else None
    duration = data.get("format", {}).get("duration")
    try:
        duration = int(float(duration))
    except (TypeError, ValueError):
        duration = None
    langs = []
    for s in streams:
        if s.get("codec_type") != "audio":
            continue
        code = (s.get("tags") or {}).get("language", "").lower()
        code = _ISO2.get(code, code)
        if code and code != "und" and code not in langs:
            langs.append(code)
    return {"resolution": resolution, "duration": duration,
            "languages": ",".join(langs) or None}


# -- walking ------------------------------------------------------------------

def pick_anchor(paths):
    """The anchor video among a folder's files: the largest non-language-variant
    (the primary copy _library_path wrote without a '.<lang>' infix), or -- when
    every file is a language variant -- the largest of those. Reads file sizes."""
    primaries = [p for p in paths if not is_lang_variant(p)]
    return max(primaries or list(paths), key=os.path.getsize)


def walk_library(root):
    """Walk `root` and yield (kind, dirpath, videos) per relevant folder: 'movie'
    for a folder that directly holds video files (its subfolders are treated as
    extras and not descended into), 'ignored' for a folder carrying IGNORE_MARKER
    (its whole subtree is skipped). Extras-named subfolders of a video-less folder
    are pruned so they never surface as their own film."""
    for dirpath, dirnames, filenames in os.walk(root):
        if IGNORE_MARKER in filenames:
            dirnames[:] = []
            yield ("ignored", dirpath, [])
            continue
        videos = sorted(os.path.join(dirpath, f) for f in filenames
                        if os.path.splitext(f)[1].lower() in VIDEO_EXTS)
        if videos:
            dirnames[:] = []   # a movie folder; deeper folders are extras
            yield ("movie", dirpath, videos)
        else:
            dirnames[:] = [d for d in dirnames if d.lower() not in EXTRAS_DIRS]
