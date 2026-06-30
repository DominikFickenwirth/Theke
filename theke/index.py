# -- library indexer (phase 12: theke library scan) --------------------------
# Walk the on-disk movie library and reconcile it with the `library` table:
# identify each film (known DB path / Kodi nfo uniqueid / folder-name + TMDB
# search), probe its physical attributes via ffprobe, detect deletions by a
# mark-and-sweep over indexed_at, and report unidentified folders. The DB is the
# authority; the media server (Kodi/Jellyfin/...) is never read as one. Pure
# parsing/walking helpers live here; CLI wiring + TMDB/DB access stay in
# __init__.py. ffprobe is a seam (run_ffprobe), monkeypatched in tests.

import json
import os
import re
import subprocess

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".flv",
              ".webm", ".mpg", ".mpeg"}

# Kodi/Jellyfin extras subfolders: never their own film, skipped while walking.
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
    malformed nfo still yields its id. The format is shared by Kodi/Emby/Jellyfin
    -- reading it is plattform-neutral, not a media-server binding."""
    m = _UNIQUEID_RX.search(text) or _TMDBID_RX.search(text)
    return m.group(1) if m else None


def is_lang_variant(filename) -> bool:
    """True when a video filename carries a '.<xx>' language infix (a 2-letter code
    before the extension, e.g. 'Film (2020).en.mp4') -- a non-anchor language copy,
    matching the infix _library_path writes for secondary picks."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    head, dot, tail = stem.rpartition(".")
    return bool(dot) and len(tail) == 2 and tail.isalpha()
