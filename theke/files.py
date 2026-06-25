# -- files (phases 6-8: download / remux / move) ------------------------------
# Queue-independent file primitives driven by explicit URLs/paths: download a
# media URL (plain HTTP with Range-resume, or HLS segment assembly with an ffmpeg
# fallback), remux via ffmpeg (stream copy, no transcode), move into the library.
# Network is touched through http_get/open_url (monkeypatched in tests); ffmpeg
# through run_ffmpeg. CLI wiring + result emission live in __init__.py.

import logging
import os
import re
import urllib.parse
import urllib.request

import theke   # for http_get, resolved at call time (avoids an import cycle)

log = logging.getLogger("theke")

CHUNK = 1 << 16   # 64 KiB streaming buffer

_BANDWIDTH_RX = re.compile(r"BANDWIDTH=(\d+)")
_URI_RX       = re.compile(r'URI="([^"]*)"')
_METHOD_RX    = re.compile(r"METHOD=([^,\s]+)")


# -- m3u8 parsing -------------------------------------------------------------

def is_hls(url) -> bool:
    """True if the URL points at an HLS playlist (path ends '.m3u8', query
    ignored)."""
    return urllib.parse.urlsplit(url).path.lower().endswith(".m3u8")


def is_master(text) -> bool:
    """True if the playlist lists variant streams (a master playlist)."""
    return "#EXT-X-STREAM-INF" in text


def parse_master(text, base_url) -> list:
    """Variants of a master playlist as (bandwidth, absolute_uri) in file order."""
    variants = []
    bandwidth = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            m = _BANDWIDTH_RX.search(line)
            bandwidth = int(m.group(1)) if m else 0
        elif line and not line.startswith("#") and bandwidth is not None:
            variants.append((bandwidth, urllib.parse.urljoin(base_url, line)))
            bandwidth = None
    return variants


def parse_media_playlist(text, base_url):
    """Parse a media playlist: return (init_uri | None, [segment_uris], encrypted).
    URIs are resolved against base_url; encrypted is True for a real #EXT-X-KEY
    (METHOD other than NONE)."""
    init = None
    segments = []
    encrypted = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP"):
            m = _URI_RX.search(line)
            if m:
                init = urllib.parse.urljoin(base_url, m.group(1))
        elif line.startswith("#EXT-X-KEY"):
            m = _METHOD_RX.search(line)
            if m and m.group(1).upper() != "NONE":
                encrypted = True
        elif line and not line.startswith("#"):
            segments.append(urllib.parse.urljoin(base_url, line))
    return init, segments, encrypted


# -- direct download ----------------------------------------------------------

def open_url(url, offset=0):
    """Open a URL for streaming; return (reader, resumed). With offset>0 a Range
    request is sent and resumed is True only when the server honors it (HTTP 206).
    The network seam for the direct downloader -- monkeypatched in tests."""
    headers = {"User-Agent": theke.USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    response = urllib.request.urlopen(urllib.request.Request(url, headers=headers))
    return response, response.status == 206


def download_file(url, out, retries) -> int:
    """Stream url to out, resuming a leftover '.part' via Range when possible.
    On error retry up to `retries` times (each retry resumes from the current
    '.part' size). Return the byte count. The seam is open_url."""
    log.info("downloading %s", url.rsplit("/", 1)[-1])
    for attempt in range(retries + 1):
        try:
            return _download_once(url, out)
        except Exception as exc:
            if attempt == retries:
                raise
            log.info("download error (%s); retry %d/%d", exc, attempt + 1, retries)


def _download_once(url, out) -> int:
    part = out + ".part"
    offset = os.path.getsize(part) if os.path.exists(part) else 0
    reader, resumed = open_url(url, offset)
    if offset and not resumed:
        offset = 0   # server ignored the Range -> rewrite from scratch
        log.info("range not honored; restarting")
    elif offset:
        log.info("resuming at %d bytes", offset)
    try:
        with open(part, "ab" if offset else "wb") as fh:   # wb truncates a stale part
            while True:
                buf = reader.read(CHUNK)
                if not buf:
                    break
                fh.write(buf)
    finally:
        reader.close()
    os.replace(part, out)
    return os.path.getsize(out)
