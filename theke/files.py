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
import urllib.error
import urllib.parse
import urllib.request

import theke   # for http_get, resolved at call time (avoids an import cycle)

log = logging.getLogger("theke")

CHUNK = 1 << 16   # 64 KiB streaming buffer
PROGRESS_BYTES = 100 << 20   # emit a transfer-progress line every 100 MiB


def _ensure_parent(path) -> None:
    """Create the parent directory of an output path if it does not exist yet."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

# -- transfer progress --------------------------------------------------------

def _content_length(reader):
    """Total byte length advertised by a urllib response, or None when absent
    (chunked stream, or a test BytesIO with no getheader)."""
    try:
        return int(reader.getheader("Content-Length"))
    except (AttributeError, TypeError, ValueError):
        return None


class _Progress:
    """Throttled byte-transfer reporter: logs a '-> label: ...' line each time the
    running byte count crosses a PROGRESS_BYTES milestone (with a percent when the
    total is known). Read PROGRESS_BYTES off the module so tests can shrink it."""

    def __init__(self, label, total):
        self.label = label
        self.total = total                  # bytes, or None when unknown
        self.next = PROGRESS_BYTES          # next milestone to announce

    def update(self, done):
        if done < self.next:
            return
        mib = done / (1 << 20)
        if self.total:
            log.info("%s: %.0f MiB / %.0f MiB (%d%%)", self.label, mib,
                     self.total / (1 << 20), 100 * done // self.total)
        else:
            log.info("%s: %.0f MiB", self.label, mib)
        self.next = (done // PROGRESS_BYTES + 1) * PROGRESS_BYTES


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

def open_url(url, offset=0, timeout=None, validator=None):
    """Open a URL for streaming; return (reader, resumed). With offset>0 a Range
    request is sent and resumed is True only when the server honors it (HTTP 206).
    With `validator` set (a stored ETag/Last-Modified), an If-Range header makes the
    server answer 200 (full body) instead of 206 when the resource has changed, so
    a resume never splices two versions. `timeout` (seconds) bounds each socket
    operation (None = no timeout). The network seam -- monkeypatched in tests."""
    headers = {"User-Agent": theke.USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
        if validator:
            headers["If-Range"] = validator
    response = urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=timeout)
    return response, response.status == 206


def _validator(reader):
    """A cache validator for an If-Range resume: the response ETag, else its
    Last-Modified, else None (a test BytesIO without getheader, or neither header)."""
    try:
        return reader.getheader("ETag") or reader.getheader("Last-Modified")
    except (AttributeError, TypeError):
        return None


def _read_validator(meta):
    """The validator stored beside a leftover '.part', or None when absent."""
    try:
        with open(meta, encoding="utf-8") as fh:
            return fh.read() or None
    except OSError:
        return None


_TRANSIENT_HTTP = {408, 429}   # client errors still worth retrying


def _is_fatal_http(exc) -> bool:
    """True for an HTTP status a retry cannot fix: a 4xx other than the transient
    ones and 416 (which is handled by resetting the '.part')."""
    return (isinstance(exc, urllib.error.HTTPError)
            and 400 <= exc.code < 500
            and exc.code not in _TRANSIENT_HTTP and exc.code != 416)


def download_file(url, out, retries, timeout=None) -> int:
    """Stream url to out, resuming a leftover '.part' via Range when possible.
    On a transient error retry up to `retries` times (each retry resumes from the
    current '.part' size); a fatal HTTP status (404/403/410/...) fails fast and a
    416 discards the stale '.part' and restarts. `timeout` (seconds) bounds each
    socket read. Return the byte count. Seam: open_url."""
    log.info("downloading %s", url.rsplit("/", 1)[-1])
    for attempt in range(retries + 1):
        try:
            return _download_once(url, out, timeout)
        except Exception as exc:
            _classify_download_error(exc, out)   # fatal -> reraise; 416 -> reset .part
            if attempt == retries:
                raise
            log.info("download error (%s); retry %d/%d", exc, attempt + 1, retries)


def _classify_download_error(exc, out) -> None:
    """Triage a download failure: re-raise a fatal HTTP status so the retry loop
    does not spin, or discard a stale oversized '.part' on 416 so the next attempt
    restarts from scratch. Anything else is left to retry."""
    if not isinstance(exc, urllib.error.HTTPError):
        return
    if exc.code == 416:
        _discard(out + ".part")
        _discard(out + ".part.meta")
        log.info("range unsatisfiable; discarded stale .part, restarting")
    elif _is_fatal_http(exc):
        raise exc


def _download_once(url, out, timeout=None) -> int:
    part = out + ".part"
    meta = part + ".meta"
    _ensure_parent(out)
    offset = os.path.getsize(part) if os.path.exists(part) else 0
    validator = _read_validator(meta) if offset else None
    extra = {"validator": validator} if validator else {}   # keep old seam intact
    reader, resumed = open_url(url, offset, timeout, **extra)
    if offset and not resumed:
        offset = 0   # range ignored or resource changed (If-Range) -> rewrite
        log.info("range not honored or resource changed; restarting")
    elif offset:
        log.info("resuming at %d bytes", offset)
    _store_validator(meta, _validator(reader))   # remember it for a later resume
    total = _content_length(reader)
    if total is not None and resumed:
        total += offset                  # 206 Content-Length covers only the remainder
    progress = _Progress(os.path.basename(out), total)
    done = offset
    try:
        with open(part, "ab" if offset else "wb") as fh:   # wb truncates a stale part
            while True:
                buf = reader.read(CHUNK)
                if not buf:
                    break
                fh.write(buf)
                done += len(buf)
                progress.update(done)
    finally:
        reader.close()
    if total is not None:
        if done < total:                     # EOF below Content-Length == dropped
            raise RuntimeError(              # connection; keep .part so a retry resumes
                f"incomplete download: {done}/{total} bytes ({os.path.basename(out)})")
    elif done == offset:                      # no Content-Length: nothing received this
        raise RuntimeError(                  # attempt (incl. a no-progress resume of an
            f"empty download: no bytes received ({os.path.basename(out)})")  # always-
    os.replace(part, out)                    # incomplete leftover .part) is never done
    _discard(meta)                           # validator only matters while a .part lives
    return os.path.getsize(out)


def _store_validator(meta, validator) -> None:
    """Persist a resume validator beside the '.part' (skipped when none is known)."""
    if validator:
        with open(meta, "w", encoding="utf-8") as fh:
            fh.write(validator)


def _discard(path) -> None:
    """Remove a path if present (best-effort sidecar cleanup)."""
    try:
        os.remove(path)
    except OSError:
        pass


# -- ffmpeg seam --------------------------------------------------------------

_DURATION_RX = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_TIME_RX     = re.compile(r"\btime=\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def _hms(h, m, s) -> float:
    """Seconds from an ffmpeg HH:MM:SS(.ss) timestamp split into its parts."""
    return int(h) * 3600 + int(m) * 60 + float(s)


def _fmt_hms(seconds) -> str:
    """Whole-second HH:MM:SS rendering of a duration in seconds."""
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


class _FfmpegProgress:
    """Throttled time-based reporter for an ffmpeg run of known duration: logs a
    line each time the elapsed media time crosses the next 10% milestone."""

    def __init__(self, label, duration):
        self.label = label
        self.duration = duration            # seconds, > 0
        self.step = duration / 10
        self.next = self.step

    def update(self, t):
        if t < self.next:
            return
        pct = min(100, int(100 * t / self.duration))
        log.info("%s: %s / %s (%d%%)", self.label, _fmt_hms(t),
                 _fmt_hms(self.duration), pct)
        while self.next <= t:
            self.next += self.step


def _iter_ffmpeg_lines(stream):
    """Yield ffmpeg's stderr as logical lines, splitting on both newline and the
    carriage returns it uses for its live stat updates."""
    buf = ""
    while True:
        chunk = stream.read(256)
        if not chunk:
            break
        buf = (buf + chunk).replace("\r", "\n")
        *lines, buf = buf.split("\n")
        for line in lines:
            if line:
                yield line
    if buf:
        yield buf


def run_ffmpeg(args) -> None:
    """Run ffmpeg (args[0] is the binary), streaming its progress to the log;
    raise on a missing binary or non-zero exit, surfacing the stderr tail. The
    subprocess seam -- monkeypatched in tests."""
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        raise RuntimeError(f"ffmpeg not found: {args[0]} (set ffmpeg_path)") from None
    label = os.path.basename(args[-1])
    tail = collections.deque(maxlen=3)
    progress = None
    for line in _iter_ffmpeg_lines(proc.stderr):
        tail.append(line)
        if progress is None:
            m = _DURATION_RX.search(line)
            if m:
                progress = _FfmpegProgress(label, _hms(*m.groups()))
        m = _TIME_RX.search(line)
        if m and progress:
            progress.update(_hms(*m.groups()))
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"ffmpeg failed (exit {code}): {'; '.join(tail)}")


def expand_ffmpeg_path(ffmpeg_path) -> str:
    """The configured ffmpeg path resolved for diagnostics: ~ and env vars
    expanded, a bare command name looked up on PATH, the result made absolute."""
    expanded = os.path.expanduser(os.path.expandvars(ffmpeg_path))
    return shutil.which(expanded) or os.path.abspath(expanded)


def check_ffmpeg(ffmpeg_path) -> str:
    """Probe the configured ffmpeg binary by running '-version'; return its first
    output line (the version string), or raise on a missing/failing binary. The
    error names the expanded, absolute path that was tried."""
    expected = expand_ffmpeg_path(ffmpeg_path)
    try:
        proc = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(f"ffmpeg not found: {expected} (set ffmpeg_path)") from None
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): {expected} -version")
    return proc.stdout.splitlines()[0].strip() if proc.stdout else ""


# -- move ---------------------------------------------------------------------

def move_file(src, dst, force=False) -> str:
    """Move src to dst, creating parent dirs. An existing dst is an error unless
    force. The payload first lands on a temp name on the destination filesystem and
    is swapped in with a single atomic os.replace, so an interrupted cross-device
    move never leaves a partial file under the final name and the prior file (with
    force) survives until the swap succeeds. Return dst."""
    if os.path.exists(dst) and not force:
        raise RuntimeError(f"destination exists: {dst}")
    _ensure_parent(dst)
    tmp = dst + ".part"
    _discard(tmp)                # clear a leftover from an earlier aborted move
    shutil.move(src, tmp)        # copy/rename onto the destination filesystem
    os.replace(tmp, dst)         # atomic swap into the final name
    log.info("moved %s -> %s", os.path.basename(src), dst)
    return dst


# -- remux --------------------------------------------------------------------

_REMUX_CODEC = {"AV": ["-c", "copy"],
                "A":  ["-vn", "-c:a", "copy"],
                "V":  ["-an", "-c:v", "copy"]}


def ffmpeg_args(ffmpeg_path, in_path, mode, out_path, language=None) -> list:
    """Build the ffmpeg stream-copy command for a remux mode ('AV'/'A'/'V').
    With language set, tag the first audio track (-metadata:s:a:0 language=...)."""
    if mode not in _REMUX_CODEC:
        raise ValueError(f"unknown remux mode: {mode}")
    meta = ["-metadata:s:a:0", f"language={language}"] if language else []
    return [ffmpeg_path, "-y", "-i", in_path] + _REMUX_CODEC[mode] + meta + [out_path]


def run_remux(ffmpeg_path, in_path, mode, out_path, language=None) -> int:
    """Remux in_path into out_path (stream copy, no transcode); return the output
    size in bytes."""
    log.info("remuxing %s -> %s (%s)", os.path.basename(in_path),
             os.path.basename(out_path), mode)
    _ensure_parent(out_path)
    try:
        run_ffmpeg(ffmpeg_args(ffmpeg_path, in_path, mode, out_path, language))
    except Exception:
        if os.path.exists(out_path):   # drop the partial/faulty target ffmpeg left
            os.remove(out_path)
        raise
    return os.path.getsize(out_path)


# -- HLS download -------------------------------------------------------------

def download_hls(url, out, retries, ffmpeg_path, timeout=None):
    """Download an HLS stream to out: resolve the master playlist (highest
    bandwidth), assemble the media playlist's segments natively, and hand off to
    ffmpeg when the stream is encrypted or native assembly fails after retries.
    `timeout` (seconds) bounds each playlist/segment fetch (None = no timeout).
    Return (action, bytes, segments) with action 'hls' or 'hls-ffmpeg'."""
    _ensure_parent(out)
    text = theke.http_get(url, timeout).decode("utf-8")
    media_url = url
    if is_master(text):
        variants = parse_master(text, url)
        if not variants:
            raise RuntimeError("empty HLS master playlist")
        media_url = max(variants, key=lambda v: v[0])[1]
        text = theke.http_get(media_url, timeout).decode("utf-8")
    init, segments, encrypted = parse_media_playlist(text, media_url)
    if encrypted:
        log.info("encrypted HLS; handing off to ffmpeg")
        return "hls-ffmpeg", _hls_ffmpeg(url, out, ffmpeg_path, timeout), len(segments)
    try:
        nbytes = _download_segments(out, init, segments, retries, timeout)
        return "hls", nbytes, len(segments)
    except Exception as exc:
        log.info("native HLS failed (%s); handing off to ffmpeg", exc)
        return "hls-ffmpeg", _hls_ffmpeg(url, out, ffmpeg_path, timeout), len(segments)


def _hls_ffmpeg_args(url, out, ffmpeg_path, timeout=None) -> list:
    """ffmpeg command to fetch an HLS stream straight to out (stream copy). With
    timeout set, -rw_timeout (microseconds) bounds each network read/write so a
    dropped connection fails instead of hanging forever."""
    rw = ["-rw_timeout", str(int(timeout * 1_000_000))] if timeout else []
    return [ffmpeg_path, "-y"] + rw + ["-i", url, "-c", "copy", out]


def _hls_ffmpeg(url, out, ffmpeg_path, timeout=None) -> int:
    try:
        run_ffmpeg(_hls_ffmpeg_args(url, out, ffmpeg_path, timeout))
    except Exception:
        if os.path.exists(out):   # drop the partial/faulty target ffmpeg left
            os.remove(out)
        raise
    return os.path.getsize(out)


def _download_segments(out, init, segments, retries, timeout=None) -> int:
    """Fetch each segment into out.segments/ (skipping ones already on disk), then
    concatenate init + segments into out and drop the segment dir."""
    segdir = out + ".segments"
    os.makedirs(segdir, exist_ok=True)
    parts = ([("seg_init", init)] if init else []) + \
            [(f"seg_{i:05d}.ts", u) for i, u in enumerate(segments)]
    log.info("downloading %d HLS segments", len(segments))
    progress = _Progress(os.path.basename(out), None)
    for attempt in range(retries + 1):
        try:
            done = 0
            for name, seg_url in parts:
                path = os.path.join(segdir, name)
                _fetch_segment(path, seg_url, timeout)
                done += os.path.getsize(path)
                progress.update(done)
            break
        except Exception as exc:
            if _is_fatal_http(exc) or attempt == retries:   # permanent -> stop spinning
                raise
            log.info("segment error (%s); retry %d/%d", exc, attempt + 1, retries)
    with open(out, "wb") as dst:
        for name, _ in parts:
            with open(os.path.join(segdir, name), "rb") as src:
                shutil.copyfileobj(src, dst)
    shutil.rmtree(segdir)
    return os.path.getsize(out)


def _fetch_segment(path, url, timeout=None) -> None:
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return   # already downloaded (resume)
    body = theke.http_get(url, timeout)
    if not body:   # no Content-Length + early EOF reads empty: never accept as final
        raise RuntimeError(f"empty segment: {os.path.basename(path)}")
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(body)
    os.replace(tmp, path)
