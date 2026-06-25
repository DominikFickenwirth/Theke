# -- files (phases 6-8: download / remux / move) ------------------------------
# Queue-independent file primitives driven by explicit URLs/paths: download a
# media URL (plain HTTP with Range-resume, or HLS segment assembly with an ffmpeg
# fallback), remux via ffmpeg (stream copy, no transcode), move into the library.
# Network is touched through http_get/open_url (monkeypatched in tests); ffmpeg
# through run_ffmpeg. CLI wiring + result emission live in __init__.py.

import collections
import logging
import os
import re
import shutil
import subprocess
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


# -- ffmpeg seam --------------------------------------------------------------

def run_ffmpeg(args) -> None:
    """Run ffmpeg (args[0] is the binary); raise on a missing binary or non-zero
    exit, surfacing the stderr tail. The subprocess seam -- monkeypatched in tests."""
    try:
        proc = subprocess.run(args, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        raise RuntimeError(f"ffmpeg not found: {args[0]} (set ffmpeg_path)") from None
    if proc.returncode != 0:
        tail = "; ".join(collections.deque((proc.stderr or "").splitlines(), 3))
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): {tail}")


# -- HLS download -------------------------------------------------------------

def download_hls(url, out, retries, ffmpeg_path):
    """Download an HLS stream to out: resolve the master playlist (highest
    bandwidth), assemble the media playlist's segments natively, and hand off to
    ffmpeg when the stream is encrypted or native assembly fails after retries.
    Return (action, bytes, segments) with action 'hls' or 'hls-ffmpeg'."""
    text = theke.http_get(url).decode("utf-8")
    media_url = url
    if is_master(text):
        variants = parse_master(text, url)
        if not variants:
            raise RuntimeError("empty HLS master playlist")
        media_url = max(variants, key=lambda v: v[0])[1]
        text = theke.http_get(media_url).decode("utf-8")
    init, segments, encrypted = parse_media_playlist(text, media_url)
    if encrypted:
        log.info("encrypted HLS; handing off to ffmpeg")
        return "hls-ffmpeg", _hls_ffmpeg(url, out, ffmpeg_path), len(segments)
    try:
        nbytes = _download_segments(out, init, segments, retries)
        return "hls", nbytes, len(segments)
    except Exception as exc:
        log.info("native HLS failed (%s); handing off to ffmpeg", exc)
        return "hls-ffmpeg", _hls_ffmpeg(url, out, ffmpeg_path), len(segments)


def _hls_ffmpeg(url, out, ffmpeg_path) -> int:
    run_ffmpeg([ffmpeg_path, "-y", "-i", url, "-c", "copy", out])
    return os.path.getsize(out)


def _download_segments(out, init, segments, retries) -> int:
    """Fetch each segment into out.segments/ (skipping ones already on disk), then
    concatenate init + segments into out and drop the segment dir."""
    segdir = out + ".segments"
    os.makedirs(segdir, exist_ok=True)
    parts = ([("seg_init", init)] if init else []) + \
            [(f"seg_{i:05d}.ts", u) for i, u in enumerate(segments)]
    log.info("downloading %d HLS segments", len(segments))
    for attempt in range(retries + 1):
        try:
            for name, seg_url in parts:
                _fetch_segment(os.path.join(segdir, name), seg_url)
            break
        except Exception as exc:
            if attempt == retries:
                raise
            log.info("segment error (%s); retry %d/%d", exc, attempt + 1, retries)
    with open(out, "wb") as dst:
        for name, _ in parts:
            with open(os.path.join(segdir, name), "rb") as src:
                shutil.copyfileobj(src, dst)
    shutil.rmtree(segdir)
    return os.path.getsize(out)


def _fetch_segment(path, url) -> None:
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return   # already downloaded (resume)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(theke.http_get(url))
    os.replace(tmp, path)
