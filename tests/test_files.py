"""Tests for the file primitives (phases 6-8): download, remux, move."""

import errno
import io
import json
import logging
import os
import urllib.error

import pytest

import theke
import theke.files as files
from theke import Config, main
from theke.files import (is_hls, is_master, parse_master, parse_media_playlist,
                         download_file, download_hls, ffmpeg_args, run_remux,
                         run_ffmpeg, check_ffmpeg, move_file)


def install_http(monkeypatch, mapping):
    """Monkeypatch theke.http_get to serve URL-mapped bytes (or raise an
    Exception value); an unmapped URL is an error."""
    def fake_get(url, timeout=None):
        value = mapping.get(url)
        if value is None:
            raise RuntimeError(f"unexpected url: {url}")
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(theke, "http_get", fake_get)


# -- m3u8 parsing (pure) ------------------------------------------------------

def test_is_hls_by_suffix():
    assert is_hls("https://h/x/playlist.m3u8") is True
    assert is_hls("https://h/x/PLAYLIST.M3U8") is True


def test_is_hls_ignores_query_string():
    assert is_hls("https://h/x/playlist.m3u8?token=abc&t=1") is True
    assert is_hls("https://h/x/video.mp4?fmt=m3u8") is False


def test_is_hls_false_for_plain_media():
    assert is_hls("https://h/x/video.mp4") is False
    assert is_hls("https://h/x/audio.m4a") is False


def test_is_master_detects_stream_inf():
    assert is_master("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\na.m3u8\n") is True
    assert is_master("#EXTM3U\n#EXTINF:6.0,\nseg0.ts\n") is False


MASTER = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
    "low/index.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720\n"
    "high/index.m3u8\n"
)


def test_parse_master_resolves_and_keeps_order():
    out = parse_master(MASTER, "https://h/v/master.m3u8")
    assert out == [(800000, "https://h/v/low/index.m3u8"),
                   (2400000, "https://h/v/high/index.m3u8")]


MEDIA = (
    "#EXTM3U\n"
    "#EXT-X-TARGETDURATION:6\n"
    "#EXTINF:6.000,\n"
    "seg0.ts\n"
    "#EXTINF:6.000,\n"
    "seg1.ts\n"
    "#EXT-X-ENDLIST\n"
)


def test_parse_media_playlist_segments_in_order():
    init, segs, enc = parse_media_playlist(MEDIA, "https://h/v/media.m3u8")
    assert init is None
    assert segs == ["https://h/v/seg0.ts", "https://h/v/seg1.ts"]
    assert enc is False


def test_parse_media_playlist_init_segment():
    text = ('#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n'
            '#EXTINF:4.0,\nseg0.m4s\n#EXT-X-ENDLIST\n')
    init, segs, enc = parse_media_playlist(text, "https://h/v/media.m3u8")
    assert init == "https://h/v/init.mp4"
    assert segs == ["https://h/v/seg0.m4s"]
    assert enc is False


def test_parse_media_playlist_encrypted_flagged():
    text = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n'
            '#EXTINF:6.0,\nseg0.ts\n#EXT-X-ENDLIST\n')
    init, segs, enc = parse_media_playlist(text, "https://h/v/media.m3u8")
    assert enc is True


def test_parse_media_playlist_method_none_not_encrypted():
    text = ('#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n'
            '#EXTINF:6.0,\nseg0.ts\n#EXT-X-ENDLIST\n')
    init, segs, enc = parse_media_playlist(text, "https://h/v/media.m3u8")
    assert enc is False


# -- network timeout ----------------------------------------------------------

def test_open_url_passes_timeout_to_urlopen(monkeypatch):
    seen = {}

    class Resp:
        status = 200

    def fake_urlopen(request, timeout=None):
        seen["timeout"] = timeout
        return Resp()

    monkeypatch.setattr(files.urllib.request, "urlopen", fake_urlopen)
    reader, resumed = files.open_url("http://x", timeout=7)
    assert seen["timeout"] == 7
    assert resumed is False


def test_download_file_threads_timeout_to_open_url(tmp_path, monkeypatch):
    seen = {}

    def opener(url, offset=0, timeout=None):
        seen["timeout"] = timeout
        return io.BytesIO(b"data"), False

    monkeypatch.setattr(files, "open_url", opener)
    download_file(url="http://x", out=str(tmp_path / "v.mp4"), retries=0, timeout=9)
    assert seen["timeout"] == 9


def test_download_hls_threads_timeout_to_http_get(tmp_path, monkeypatch):
    seen = []

    def fake_get(url, timeout=None):
        seen.append(timeout)
        return {MEDIA_URL: MEDIA_TXT, "https://h/v/seg0.ts": b"AAA",
                "https://h/v/seg1.ts": b"BBB"}[url]

    monkeypatch.setattr(theke, "http_get", fake_get)
    download_hls(url=MEDIA_URL, out=str(tmp_path / "v.ts"), retries=0,
                 ffmpeg_path="ffmpeg", timeout=11)
    assert seen == [11, 11, 11]   # playlist + both segments


# -- direct download (resume + retry) -----------------------------------------

class Opener:
    """Fake open_url: records offsets; serves data[offset:] when the server
    honors the Range (resumable), else the whole body (HTTP 200)."""

    def __init__(self, data, resumable=True):
        self.data = data
        self.resumable = resumable
        self.offsets = []

    def __call__(self, url, offset=0, timeout=None):
        self.offsets.append(offset)
        resumed = self.resumable and offset > 0
        body = self.data[offset:] if resumed else self.data
        return io.BytesIO(body), resumed


def test_download_full_writes_bytes_and_removes_part(tmp_path, monkeypatch):
    data = b"hello world payload"
    monkeypatch.setattr(files, "open_url", Opener(data))
    out = str(tmp_path / "v.mp4")
    n = download_file(out=out, url="http://x", retries=0)
    assert n == len(data)
    assert (tmp_path / "v.mp4").read_bytes() == data
    assert not (tmp_path / "v.mp4.part").exists()


def test_download_creates_missing_parent_dirs(tmp_path, monkeypatch):
    data = b"hello world payload"
    monkeypatch.setattr(files, "open_url", Opener(data))
    out = str(tmp_path / "new" / "sub" / "v.mp4")      # parents do not exist yet
    n = download_file(out=out, url="http://x", retries=0)
    assert n == len(data)
    assert (tmp_path / "new" / "sub" / "v.mp4").read_bytes() == data


def test_download_resumes_from_part_with_206(tmp_path, monkeypatch):
    data = b"0123456789abcdef"
    (tmp_path / "v.mp4.part").write_bytes(data[:6])   # 6 bytes already on disk
    opener = Opener(data, resumable=True)
    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    download_file(out=out, url="http://x", retries=0)
    assert opener.offsets == [6]                       # asked to resume at 6
    assert (tmp_path / "v.mp4").read_bytes() == data


def test_download_restarts_when_server_ignores_range(tmp_path, monkeypatch):
    data = b"0123456789abcdef"
    (tmp_path / "v.mp4.part").write_bytes(b"STALE")    # leftover partial
    opener = Opener(data, resumable=False)             # server answers 200
    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    download_file(out=out, url="http://x", retries=0)
    assert (tmp_path / "v.mp4").read_bytes() == data   # truncated, rewritten


def test_download_retries_then_succeeds(tmp_path, monkeypatch):
    data = b"retry me please"
    calls = {"n": 0}

    def opener(url, offset=0, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection reset")
        return io.BytesIO(data[offset:] if offset else data), offset > 0

    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    download_file(out=out, url="http://x", retries=2)
    assert calls["n"] == 2
    assert (tmp_path / "v.mp4").read_bytes() == data


def test_download_resumes_across_a_midstream_failure(tmp_path, monkeypatch):
    data = os.urandom(files.CHUNK + 5000)              # > one chunk
    calls = {"n": 0}

    class Failing:
        def __init__(self, body):
            self.buf = io.BytesIO(body)
            self.reads = 0
        def read(self, n):
            if self.reads >= 1:
                raise RuntimeError("midstream drop")
            self.reads += 1
            return self.buf.read(n)
        def close(self):
            pass

    def opener(url, offset=0, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return Failing(data), False                # dies after one chunk
        return io.BytesIO(data[offset:]), offset > 0   # resumes the rest

    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    download_file(out=out, url="http://x", retries=2)
    assert (tmp_path / "v.mp4").read_bytes() == data


def test_download_raises_after_exhausting_retries(tmp_path, monkeypatch):
    def opener(url, offset=0, timeout=None):
        raise RuntimeError("always down")

    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    with pytest.raises(RuntimeError, match="always down"):
        download_file(out=out, url="http://x", retries=2)


class Sized:
    """Fake response advertising a Content-Length header, serving `body` then a
    clean EOF. A body shorter than `length` models a dropped connection that ends
    in EOF instead of raising (the silent-truncation case)."""

    def __init__(self, body, length):
        self.buf = io.BytesIO(body)
        self.length = length

    def getheader(self, name, default=None):
        return str(self.length) if name == "Content-Length" else default

    def read(self, n=-1):
        return self.buf.read(n)

    def close(self):
        pass


def test_download_truncated_stream_raises_not_silently_completes(tmp_path, monkeypatch):
    # server promises 16 bytes but delivers 6, then a clean EOF: must NOT be taken
    # as a finished download (no final file, the half stays a .part).
    def opener(url, offset=0, timeout=None):
        return Sized(b"012345", length=16), False
    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    with pytest.raises(RuntimeError, match="incomplete"):
        download_file(out=out, url="http://x", retries=0)
    assert not (tmp_path / "v.mp4").exists()           # half file never the result
    assert (tmp_path / "v.mp4.part").read_bytes() == b"012345"   # kept for resume


def test_download_truncated_then_resumes_to_completion(tmp_path, monkeypatch):
    data = b"0123456789abcdef"                          # 16 bytes
    calls = {"n": 0}

    def opener(url, offset=0, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return Sized(data[:6], length=16), False    # drops at 6 bytes (EOF)
        return Sized(data[offset:], length=len(data) - offset), offset > 0

    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    n = download_file(out=out, url="http://x", retries=2)
    assert n == len(data)
    assert (tmp_path / "v.mp4").read_bytes() == data
    assert not (tmp_path / "v.mp4.part").exists()


# -- direct download length guard (item 2) ------------------------------------
# Without a Content-Length the truncation check cannot fire, so a connection that
# drops at EOF reads back as a clean empty buffer and looks complete. Consistent
# with item 1 ("empty = reject"), the minimum guard is: a no-Content-Length
# stream that delivers zero bytes (nothing received this attempt) is never
# accepted as a finished download. The Content-Length path stays unchanged.

def test_download_no_content_length_nonempty_completes(tmp_path, monkeypatch):
    # no Content-Length (BytesIO has no getheader) + non-empty stream -> ok.
    data = b"streamed body without length"
    def opener(url, offset=0, timeout=None):
        return io.BytesIO(data), False
    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    n = download_file(out=out, url="http://x", retries=0)
    assert n == len(data)
    assert (tmp_path / "v.mp4").read_bytes() == data
    assert not (tmp_path / "v.mp4.part").exists()


def test_download_no_content_length_empty_stream_raises(tmp_path, monkeypatch):
    # no Content-Length + empty stream (dropped at EOF) -> failure, never the result.
    def opener(url, offset=0, timeout=None):
        return io.BytesIO(b""), False
    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    with pytest.raises(RuntimeError, match="empty download"):
        download_file(out=out, url="http://x", retries=0)
    assert not (tmp_path / "v.mp4").exists()           # empty buffer never finalized


def test_download_content_length_zero_path_unchanged(tmp_path, monkeypatch):
    # The empty-result guard is scoped to the no-Content-Length case: a server that
    # explicitly advertises Content-Length: 0 stays authoritative and is accepted,
    # so the Content-Length path is unchanged.
    def opener(url, offset=0, timeout=None):
        return Sized(b"", length=0), False
    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    n = download_file(out=out, url="http://x", retries=0)
    assert n == 0
    assert (tmp_path / "v.mp4").read_bytes() == b""


# -- resume validation via If-Range (item 3) ----------------------------------
# A byte-offset resume that does not validate the resource can splice two
# different remote versions into one corrupt file (and the length check still
# passes when the sizes line up). The fix sends If-Range with the stored ETag/
# Last-Modified, so a changed resource yields a full 200 (restart) instead of a
# 206 (append); the validator is persisted in a '<part>.meta' sidecar so it
# survives process restarts, and is dropped once the download completes.

class SizedTagged(Sized):
    """A Sized response that also advertises an ETag (for validator storage)."""

    def __init__(self, body, length, etag):
        super().__init__(body, length)
        self.etag = etag

    def getheader(self, name, default=None):
        if name == "ETag":
            return self.etag
        return super().getheader(name, default)


def test_open_url_sends_if_range_with_validator(monkeypatch):
    seen = {}

    class Resp:
        status = 206

    def fake_urlopen(request, timeout=None):
        seen["range"] = request.get_header("Range")
        seen["if_range"] = request.get_header("If-range")   # urllib capitalizes keys
        return Resp()

    monkeypatch.setattr(files.urllib.request, "urlopen", fake_urlopen)
    reader, resumed = files.open_url("http://x", offset=6, validator='"etag-1"')
    assert seen["range"] == "bytes=6-"
    assert seen["if_range"] == '"etag-1"'
    assert resumed is True


def test_open_url_no_if_range_without_validator(monkeypatch):
    seen = {}

    class Resp:
        status = 206

    def fake_urlopen(request, timeout=None):
        seen["if_range"] = request.get_header("If-range")
        return Resp()

    monkeypatch.setattr(files.urllib.request, "urlopen", fake_urlopen)
    files.open_url("http://x", offset=6)
    assert seen["if_range"] is None                          # current behaviour


def test_download_resume_sends_stored_validator(tmp_path, monkeypatch):
    data = b"0123456789abcdef"
    (tmp_path / "v.mp4.part").write_bytes(data[:6])
    (tmp_path / "v.mp4.part.meta").write_text('"etag-1"', encoding="utf-8")
    seen = {}

    def opener(url, offset=0, timeout=None, validator=None):
        seen["offset"], seen["validator"] = offset, validator
        return io.BytesIO(data[offset:]), True
    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    download_file(out=out, url="http://x", retries=0)
    assert seen["offset"] == 6
    assert seen["validator"] == '"etag-1"'                   # sidecar -> If-Range
    assert (tmp_path / "v.mp4").read_bytes() == data
    assert not (tmp_path / "v.mp4.part.meta").exists()       # cleared on success


def test_download_changed_resource_restarts_without_splicing(tmp_path, monkeypatch):
    new = b"BRANDNEWDATA1234"                                # 16 bytes
    (tmp_path / "v.mp4.part").write_bytes(b"OLDOLD")         # stale 6-byte partial
    (tmp_path / "v.mp4.part.meta").write_text('"etag-old"', encoding="utf-8")

    def opener(url, offset=0, timeout=None, validator=None):
        return io.BytesIO(new), False                        # changed -> full 200
    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    download_file(out=out, url="http://x", retries=0)
    assert (tmp_path / "v.mp4").read_bytes() == new          # not b"OLDOLD" + new


def test_download_truncated_stores_response_validator_for_resume(tmp_path, monkeypatch):
    data = b"0123456789abcdef"                               # 16 bytes
    calls = {"n": 0}

    def opener(url, offset=0, timeout=None, validator=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return SizedTagged(data[:6], length=16, etag='"etag-9"'), False
        assert validator == '"etag-9"'                       # response ETag was stored
        return SizedTagged(data[offset:], length=16 - offset, etag='"etag-9"'), True
    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    n = download_file(out=out, url="http://x", retries=1)
    assert calls["n"] == 2
    assert n == len(data)
    assert (tmp_path / "v.mp4").read_bytes() == data
    assert not (tmp_path / "v.mp4.part.meta").exists()


# -- download error classification (item 7) -----------------------------------
# Not every failure is transient: a 4xx the request can't fix by repeating (404,
# 403, 410) must fail fast instead of burning all retries; a 416 (range not
# satisfiable, caused by a stale oversized .part) must discard the leftover and
# restart once, which it could never self-heal before. 5xx and network errors
# still retry.

def test_download_fatal_http_status_fails_without_retrying(tmp_path, monkeypatch):
    calls = {"n": 0}

    def opener(url, offset=0, timeout=None, **kw):
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr(files, "open_url", opener)
    with pytest.raises(urllib.error.HTTPError):
        download_file(out=str(tmp_path / "v.mp4"), url="http://x", retries=3)
    assert calls["n"] == 1                       # no retry spin on a permanent 404


def test_download_416_discards_stale_part_and_restarts(tmp_path, monkeypatch):
    data = b"fresh complete data"
    (tmp_path / "v.mp4.part").write_bytes(b"STALE OVERSIZED PART, LONGER THAN REMOTE")
    (tmp_path / "v.mp4.part.meta").write_text('"etag"', encoding="utf-8")
    calls = {"n": 0}

    def opener(url, offset=0, timeout=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            assert offset > 0                    # first tried to resume the stale part
            raise urllib.error.HTTPError(url, 416, "Range Not Satisfiable", {}, None)
        assert offset == 0                       # reset -> restarted from scratch
        return io.BytesIO(data), False
    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    n = download_file(out=out, url="http://x", retries=2)
    assert n == len(data)
    assert (tmp_path / "v.mp4").read_bytes() == data
    assert not (tmp_path / "v.mp4.part.meta").exists()


def test_download_server_error_still_retries(tmp_path, monkeypatch):
    data = b"ok now"
    calls = {"n": 0}

    def opener(url, offset=0, timeout=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)
        return io.BytesIO(data), False
    monkeypatch.setattr(files, "open_url", opener)
    n = download_file(out=str(tmp_path / "v.mp4"), url="http://x", retries=2)
    assert calls["n"] == 2                        # 5xx is transient -> retried
    assert n == len(data)


# -- disk-full is non-retryable (item 9) --------------------------------------
# A write that fails with ENOSPC will never succeed by retrying; spinning the
# retry loop just wastes time and leaves a .part. It must fail fast.

def test_download_disk_full_fails_without_retrying(tmp_path, monkeypatch):
    calls = {"n": 0}

    def opener(url, offset=0, timeout=None, **kw):
        calls["n"] += 1
        return io.BytesIO(b"payload"), False
    monkeypatch.setattr(files, "open_url", opener)
    real_open = open

    class FullFile:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, b):
            err = OSError("no space left on device")
            err.errno = errno.ENOSPC
            raise err

    def fake_open(path, mode="r", *a, **k):
        if str(path).endswith(".part"):
            return FullFile()
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr("builtins.open", fake_open)
    with pytest.raises(OSError):
        download_file(out=str(tmp_path / "v.mp4"), url="http://x", retries=3)
    assert calls["n"] == 1                        # disk full -> no retry spin


# -- download stall guard (item 8) --------------------------------------------
# The socket timeout bounds a single read, not the whole transfer, so a server
# that trickles a little data per interval never trips it and the download hangs
# effectively forever. download_stall_timeout (config, default 120 s; 0 = off)
# adds a throughput floor: a transfer delivering less than one CHUNK per window is
# aborted into the retry loop. The clock is injected via time.monotonic.

def test_download_stall_aborts_on_low_throughput(tmp_path, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(files.time, "monotonic", lambda: clock["t"])

    class Trickle:
        def __init__(self, n):
            self.left = n

        def read(self, size):
            clock["t"] += 1.0                 # each read takes ~1 s
            if self.left <= 0:
                return b""
            self.left -= 1
            return b"x"                        # one byte per read -> far below floor

        def close(self):
            pass

    monkeypatch.setattr(files, "open_url", lambda *a, **k: (Trickle(1000), False))
    with pytest.raises(RuntimeError, match="stall"):
        download_file(out=str(tmp_path / "v.mp4"), url="http://x", retries=0,
                      stall_timeout=5)


def test_download_stall_disabled_with_zero(tmp_path, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(files.time, "monotonic", lambda: clock["t"])

    class Trickle:
        def __init__(self, body):
            self.buf = io.BytesIO(body)

        def read(self, size):
            clock["t"] += 100.0               # huge gaps, but the guard is off
            return self.buf.read(1)

        def close(self):
            pass

    monkeypatch.setattr(files, "open_url", lambda *a, **k: (Trickle(b"abc"), False))
    n = download_file(out=str(tmp_path / "v.mp4"), url="http://x", retries=0,
                      stall_timeout=0)
    assert n == 3                              # 0 disables the floor -> completes


def test_download_stall_allows_steady_throughput(tmp_path, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(files.time, "monotonic", lambda: clock["t"])
    data = b"y" * (files.CHUNK + 100)

    class Clocked:
        def __init__(self, body):
            self.buf = io.BytesIO(body)

        def read(self, size):
            clock["t"] += 1.0                 # 1 s per read, window is 5 -> never low
            return self.buf.read(size)

        def close(self):
            pass

    monkeypatch.setattr(files, "open_url", lambda *a, **k: (Clocked(data), False))
    n = download_file(out=str(tmp_path / "v.mp4"), url="http://x", retries=0,
                      stall_timeout=5)
    assert n == len(data)


# -- byte progress (downloads) ------------------------------------------------

def test_content_length_from_header_and_missing():
    class R:
        def getheader(self, name): return "1234"
    assert files._content_length(R()) == 1234
    assert files._content_length(io.BytesIO(b"x")) is None     # no getheader -> None


def test_progress_known_total_formats_mib_and_percent(caplog):
    caplog.set_level(logging.INFO, logger="theke")
    p = files._Progress("v.mp4", total=300 << 20)              # default step is 100 MiB
    for done in (100 << 20, 200 << 20, 300 << 20):
        p.update(done)
    # percents: 100*100//300=33, 100*200//300=66, 100*300//300=100 (integer division)
    assert [r.getMessage() for r in caplog.records] == [
        "v.mp4: 100 MiB / 300 MiB (33%)",
        "v.mp4: 200 MiB / 300 MiB (66%)",
        "v.mp4: 300 MiB / 300 MiB (100%)"]


def test_progress_unknown_total_formats_mib_only(caplog):
    caplog.set_level(logging.INFO, logger="theke")
    p = files._Progress("v.mp4", total=None)
    p.update(100 << 20)
    p.update(200 << 20)
    assert [r.getMessage() for r in caplog.records] == [
        "v.mp4: 100 MiB", "v.mp4: 200 MiB"]


def test_progress_skips_steps_below_threshold(caplog):
    caplog.set_level(logging.INFO, logger="theke")
    p = files._Progress("v.mp4", total=None)
    p.update(50 << 20)                                          # below 100 MiB -> silent
    assert caplog.records == []


def test_download_logs_a_progress_line_per_threshold(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(files, "CHUNK", 4)                      # 4-byte reads
    monkeypatch.setattr(files, "PROGRESS_BYTES", 4)            # milestone every 4 bytes
    data = b"0123456789AB"                                      # 12 bytes -> 3 chunks
    monkeypatch.setattr(files, "open_url", Opener(data))
    caplog.set_level(logging.INFO, logger="theke")
    download_file(out=str(tmp_path / "v.mp4"), url="http://x/v.mp4", retries=0)
    progress = [r.getMessage() for r in caplog.records if "MiB" in r.getMessage()]
    assert len(progress) == 3                                   # at 4, 8, 12 bytes


# -- HLS download + ffmpeg fallback -------------------------------------------

MASTER_URL = "https://h/v/master.m3u8"
MEDIA_URL  = "https://h/v/media.m3u8"
MASTER_TXT = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nmedia.m3u8\n"
MEDIA_TXT  = b"#EXTM3U\n#EXTINF:6,\nseg0.ts\n#EXTINF:6,\nseg1.ts\n#EXT-X-ENDLIST\n"


def test_hls_native_concatenates_segments_in_order(tmp_path, monkeypatch):
    install_http(monkeypatch, {MASTER_URL: MASTER_TXT, MEDIA_URL: MEDIA_TXT,
                               "https://h/v/seg0.ts": b"AAA",
                               "https://h/v/seg1.ts": b"BBB"})
    out = str(tmp_path / "v.ts")
    action, nbytes, nsegs = download_hls(url=MASTER_URL, out=out, retries=0,
                                         ffmpeg_path="ffmpeg")
    assert action == "hls"
    assert nsegs == 2
    assert (tmp_path / "v.ts").read_bytes() == b"AAABBB"
    assert nbytes == 6
    assert not (tmp_path / "v.ts.segments").exists()   # segdir cleaned up


def test_hls_creates_missing_parent_dirs(tmp_path, monkeypatch):
    media = (b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n'
             b'#EXTINF:6,\nseg0.ts\n#EXT-X-ENDLIST\n')
    install_http(monkeypatch, {MEDIA_URL: media})

    def fake_ffmpeg(args):
        with open(args[-1], "wb") as fh:
            fh.write(b"FROM-FFMPEG")

    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    out = str(tmp_path / "new" / "sub" / "v.ts")       # parents do not exist yet
    action, nbytes, nsegs = download_hls(url=MEDIA_URL, out=out, retries=0,
                                         ffmpeg_path="ffmpeg")
    assert action == "hls-ffmpeg"
    assert (tmp_path / "new" / "sub" / "v.ts").read_bytes() == b"FROM-FFMPEG"


def test_hls_native_prepends_init_segment(tmp_path, monkeypatch):
    media = (b'#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n'
             b'#EXTINF:4,\nseg0.m4s\n#EXT-X-ENDLIST\n')
    install_http(monkeypatch, {MEDIA_URL: media,
                               "https://h/v/init.mp4": b"INIT",
                               "https://h/v/seg0.m4s": b"DATA"})
    out = str(tmp_path / "v.ts")
    action, nbytes, nsegs = download_hls(url=MEDIA_URL, out=out, retries=0,
                                         ffmpeg_path="ffmpeg")
    assert (tmp_path / "v.ts").read_bytes() == b"INITDATA"


def test_hls_resume_skips_existing_segment_files(tmp_path, monkeypatch):
    # seg1 already on disk; its URL is omitted from the map -> fetching it errors
    monkeypatch.setattr(files, "run_ffmpeg",
                        lambda args: pytest.fail("should not fall back"))
    install_http(monkeypatch, {MEDIA_URL: MEDIA_TXT, "https://h/v/seg0.ts": b"AAA"})
    out = str(tmp_path / "v.ts")
    segdir = tmp_path / "v.ts.segments"
    segdir.mkdir()
    (segdir / "seg_00001.ts").write_bytes(b"BBB")
    action, nbytes, nsegs = download_hls(url=MEDIA_URL, out=out, retries=0,
                                         ffmpeg_path="ffmpeg")
    assert action == "hls"
    assert (tmp_path / "v.ts").read_bytes() == b"AAABBB"


def test_hls_encrypted_falls_back_to_ffmpeg(tmp_path, monkeypatch):
    media = (b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n'
             b'#EXTINF:6,\nseg0.ts\n#EXT-X-ENDLIST\n')
    install_http(monkeypatch, {MEDIA_URL: media})
    seen = {}

    def fake_ffmpeg(args):
        seen["args"] = args
        with open(args[-1], "wb") as fh:
            fh.write(b"FROM-FFMPEG")

    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    out = str(tmp_path / "v.ts")
    action, nbytes, nsegs = download_hls(url=MEDIA_URL, out=out, retries=0,
                                         ffmpeg_path="ffmpeg")
    assert action == "hls-ffmpeg"
    assert (tmp_path / "v.ts").read_bytes() == b"FROM-FFMPEG"
    assert MEDIA_URL in seen["args"] and "ffmpeg" in seen["args"]


def test_hls_ffmpeg_removes_faulty_output_on_failure(tmp_path, monkeypatch):
    media = (b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n'
             b'#EXTINF:6,\nseg0.ts\n#EXT-X-ENDLIST\n')
    install_http(monkeypatch, {MEDIA_URL: media})

    def fake_ffmpeg(args):
        with open(args[-1], "wb") as fh:        # ffmpeg writes a partial file...
            fh.write(b"partial garbage")
        raise RuntimeError("ffmpeg failed (exit 1): boom")   # ...then dies

    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    out = tmp_path / "v.ts"
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        download_hls(url=MEDIA_URL, out=str(out), retries=0, ffmpeg_path="ffmpeg")
    assert not out.exists()                     # faulty target cleaned up


def test_hls_native_failure_falls_back_to_ffmpeg(tmp_path, monkeypatch):
    install_http(monkeypatch, {MEDIA_URL: MEDIA_TXT, "https://h/v/seg0.ts": b"AAA",
                               "https://h/v/seg1.ts": RuntimeError("seg gone")})

    def fake_ffmpeg(args):
        with open(args[-1], "wb") as fh:
            fh.write(b"FALLBACK")

    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    out = str(tmp_path / "v.ts")
    action, nbytes, nsegs = download_hls(url=MEDIA_URL, out=out, retries=0,
                                         ffmpeg_path="ffmpeg")
    assert action == "hls-ffmpeg"
    assert (tmp_path / "v.ts").read_bytes() == b"FALLBACK"


# -- ffmpeg HLS-fallback timeout (item 4) -------------------------------------
# When ffmpeg fetches the HLS stream itself (encrypted stream, or native assembly
# failed) it must get the configured network timeout, else a dropped connection
# hangs the process forever -- the same failure the direct downloader already
# guards. -rw_timeout bounds each network read/write and is in microseconds.

def test_hls_ffmpeg_args_includes_rw_timeout():
    args = files._hls_ffmpeg_args("http://x/v.m3u8", "out.ts", "ffmpeg", timeout=60)
    assert "-rw_timeout" in args
    assert args[args.index("-rw_timeout") + 1] == "60000000"   # 60 s in microseconds
    assert args.index("-rw_timeout") < args.index("-i")        # input option


def test_hls_ffmpeg_args_omits_timeout_when_none():
    args = files._hls_ffmpeg_args("http://x/v.m3u8", "out.ts", "ffmpeg", timeout=None)
    assert "-rw_timeout" not in args


def test_hls_encrypted_handoff_passes_timeout_to_ffmpeg(tmp_path, monkeypatch):
    media = (b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n'
             b'#EXTINF:6,\nseg0.ts\n#EXT-X-ENDLIST\n')
    install_http(monkeypatch, {MEDIA_URL: media})
    seen = {}

    def fake_ffmpeg(args):
        seen["args"] = args
        with open(args[-1], "wb") as fh:
            fh.write(b"X")
    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    download_hls(url=MEDIA_URL, out=str(tmp_path / "v.ts"), retries=0,
                 ffmpeg_path="ffmpeg", timeout=30)
    assert seen["args"][seen["args"].index("-rw_timeout") + 1] == "30000000"


def test_hls_native_failure_handoff_passes_timeout_to_ffmpeg(tmp_path, monkeypatch):
    install_http(monkeypatch, {MEDIA_URL: MEDIA_TXT, "https://h/v/seg0.ts": b"AAA",
                               "https://h/v/seg1.ts": RuntimeError("seg gone")})
    seen = {}

    def fake_ffmpeg(args):
        seen["args"] = args
        with open(args[-1], "wb") as fh:
            fh.write(b"X")
    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    download_hls(url=MEDIA_URL, out=str(tmp_path / "v.ts"), retries=0,
                 ffmpeg_path="ffmpeg", timeout=45)
    assert seen["args"][seen["args"].index("-rw_timeout") + 1] == "45000000"


def test_hls_segment_fatal_http_skips_retries_to_ffmpeg(tmp_path, monkeypatch):
    # a 404 segment is permanent: don't spin segment retries, hand off to ffmpeg.
    seg_calls = {"n": 0}

    def fake_get(url, timeout=None):
        if url == MEDIA_URL:
            return MEDIA_TXT
        seg_calls["n"] += 1
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr(theke, "http_get", fake_get)

    def fake_ffmpeg(args):
        with open(args[-1], "wb") as fh:
            fh.write(b"FF")
    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    action, nbytes, nsegs = download_hls(url=MEDIA_URL, out=str(tmp_path / "v.ts"),
                                         retries=3, ffmpeg_path="ffmpeg")
    assert action == "hls-ffmpeg"
    assert seg_calls["n"] == 1                    # first 404 -> ffmpeg, no retry spin


def test_hls_logs_byte_progress_lines(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(files, "PROGRESS_BYTES", 2)            # milestone every 2 bytes
    install_http(monkeypatch, {MEDIA_URL: MEDIA_TXT,
                               "https://h/v/seg0.ts": b"AAA",  # 3 bytes -> done 3
                               "https://h/v/seg1.ts": b"BBB"}) # 3 bytes -> done 6
    caplog.set_level(logging.INFO, logger="theke")
    download_hls(url=MEDIA_URL, out=str(tmp_path / "v.ts"), retries=0,
                 ffmpeg_path="ffmpeg")
    progress = [r.getMessage() for r in caplog.records if "MiB" in r.getMessage()]
    assert len(progress) == 2                                  # after 3 bytes and 6 bytes


# -- HLS segment length guard (item 1) ----------------------------------------
# theke.http_get exposes no Content-Length to the caller: a short read *with* a
# Content-Length already raises IncompleteRead inside http_get (-> retry loop), so
# the only residual, otherwise-undetectable case is a no-Content-Length stream
# that ends early, which reads back as an empty body. Consistent with item 2 ("an
# empty result must not be accepted as a complete download"), an empty segment is
# rejected and never finalized.

def test_fetch_segment_rejects_empty_body_not_written(tmp_path, monkeypatch):
    install_http(monkeypatch, {"https://h/v/seg0.ts": b""})
    path = str(tmp_path / "seg_00000.ts")
    with pytest.raises(RuntimeError, match="empty segment"):
        files._fetch_segment(path, "https://h/v/seg0.ts")
    assert not os.path.exists(path)              # never finalized
    assert not os.path.exists(path + ".part")    # no stale temp left behind


def test_fetch_segment_writes_nonempty_body(tmp_path, monkeypatch):
    install_http(monkeypatch, {"https://h/v/seg0.ts": b"DATA"})
    path = str(tmp_path / "seg_00000.ts")
    files._fetch_segment(path, "https://h/v/seg0.ts")
    assert (tmp_path / "seg_00000.ts").read_bytes() == b"DATA"   # exact length -> ok


def test_hls_empty_segment_retries_then_succeeds(tmp_path, monkeypatch):
    # seg1 arrives empty (silent truncation) on the first try, full on the retry;
    # the empty body is never kept, so the assembled output is whole.
    calls = {"n": 0}

    def fake_get(url, timeout=None):
        if url == "https://h/v/seg1.ts":
            calls["n"] += 1
            return b"" if calls["n"] == 1 else b"BBB"
        return {MEDIA_URL: MEDIA_TXT, "https://h/v/seg0.ts": b"AAA"}[url]

    monkeypatch.setattr(theke, "http_get", fake_get)
    monkeypatch.setattr(files, "run_ffmpeg",
                        lambda args: pytest.fail("should not fall back"))
    out = str(tmp_path / "v.ts")
    action, nbytes, nsegs = download_hls(url=MEDIA_URL, out=out, retries=1,
                                         ffmpeg_path="ffmpeg")
    assert action == "hls"
    assert (tmp_path / "v.ts").read_bytes() == b"AAABBB"


def test_hls_empty_segment_without_retries_falls_back_to_ffmpeg(tmp_path, monkeypatch):
    install_http(monkeypatch, {MEDIA_URL: MEDIA_TXT, "https://h/v/seg0.ts": b"AAA",
                               "https://h/v/seg1.ts": b""})   # silent truncation

    def fake_ffmpeg(args):
        with open(args[-1], "wb") as fh:
            fh.write(b"FALLBACK")

    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    out = str(tmp_path / "v.ts")
    action, nbytes, nsegs = download_hls(url=MEDIA_URL, out=out, retries=0,
                                         ffmpeg_path="ffmpeg")
    assert action == "hls-ffmpeg"
    assert (tmp_path / "v.ts").read_bytes() == b"FALLBACK"


# -- file download CLI --------------------------------------------------------

def test_cli_file_download_direct_json(tmp_path, capsys, monkeypatch):
    out = str(tmp_path / "v.mp4")
    monkeypatch.setattr(files, "open_url", Opener(b"data"))
    rc = main(["--json", "file", "download", "--url", "http://x/v.mp4", "--out", out])
    assert rc == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"action": "download", "out": out, "bytes": 4}
    assert captured.out.strip().count("\n") == 0      # one JSON line on stdout
    assert "-> downloading" in captured.err            # progress on stderr


def test_cli_file_download_hls_routes_to_segments(tmp_path, capsys, monkeypatch):
    install_http(monkeypatch, {MEDIA_URL: MEDIA_TXT, "https://h/v/seg0.ts": b"AAA",
                               "https://h/v/seg1.ts": b"BBB"})
    out = str(tmp_path / "v.ts")
    rc = main(["--json", "file", "download", "--url", MEDIA_URL, "--out", out])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "action": "hls", "out": out, "bytes": 6, "segments": 2}
    assert (tmp_path / "v.ts").read_bytes() == b"AAABBB"


def test_cli_file_download_missing_args_is_usage_error(capsys):
    assert main(["file", "download"]) == 2


def test_cli_file_download_timeout_overrides_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = {}

    def opener(url, offset=0, timeout=None):
        seen["timeout"] = timeout
        return io.BytesIO(b"data"), False

    monkeypatch.setattr(files, "open_url", opener)
    rc = main(["file", "download", "--url", "http://x/v.mp4",
               "--out", str(tmp_path / "v.mp4"), "--timeout", "5"])
    assert rc == 0
    assert seen["timeout"] == 5


def test_cli_file_download_timeout_defaults_to_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)          # no theke.json -> config defaults
    seen = {}

    def opener(url, offset=0, timeout=None):
        seen["timeout"] = timeout
        return io.BytesIO(b"data"), False

    monkeypatch.setattr(files, "open_url", opener)
    rc = main(["file", "download", "--url", "http://x/v.mp4",
               "--out", str(tmp_path / "v.mp4")])
    assert rc == 0
    assert seen["timeout"] == 60         # Config().download_timeout


def test_cli_file_download_passes_stall_timeout_from_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)          # no theke.json -> config defaults
    seen = {}

    def fake_download(url, out, retries, timeout=None, stall_timeout=0):
        seen["stall"] = stall_timeout
        with open(out, "wb") as fh:
            fh.write(b"x")
        return 1
    monkeypatch.setattr(theke, "download_file", fake_download)
    rc = main(["file", "download", "--url", "http://x/v.mp4",
               "--out", str(tmp_path / "v.mp4")])
    assert rc == 0
    assert seen["stall"] == 120          # Config().download_stall_timeout


# -- remux (ffmpeg stream copy) -----------------------------------------------

def test_ffmpeg_args_av_copies_all():
    assert ffmpeg_args("ffmpeg", "in.ts", "AV", "out.mp4") == [
        "ffmpeg", "-y", "-i", "in.ts", "-c", "copy", "out.mp4"]


def test_ffmpeg_args_audio_only():
    assert ffmpeg_args("ffmpeg", "in.ts", "A", "out.aac") == [
        "ffmpeg", "-y", "-i", "in.ts", "-vn", "-c:a", "copy", "out.aac"]


def test_ffmpeg_args_video_only():
    assert ffmpeg_args("ffmpeg", "in.ts", "V", "out.mp4") == [
        "ffmpeg", "-y", "-i", "in.ts", "-an", "-c:v", "copy", "out.mp4"]


def test_ffmpeg_args_sets_audio_language():
    assert ffmpeg_args("ffmpeg", "in.ts", "AV", "out.mp4", language="deu") == [
        "ffmpeg", "-y", "-i", "in.ts", "-c", "copy",
        "-metadata:s:a:0", "language=deu", "out.mp4"]


def test_ffmpeg_args_unknown_mode_raises():
    with pytest.raises(ValueError, match="mode"):
        ffmpeg_args("ffmpeg", "in.ts", "X", "out.mp4")


def test_run_remux_invokes_ffmpeg_with_built_args(tmp_path, monkeypatch):
    out = str(tmp_path / "out.mp4")
    seen = {}

    def fake_ffmpeg(args, duration=None):
        seen["args"] = args
        with open(args[-1], "wb") as fh:
            fh.write(b"muxed")

    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    run_remux("ffmpeg", "in.ts", "AV", out, language="fra")
    assert seen["args"] == ["ffmpeg", "-y", "-i", "in.ts", "-c", "copy",
                            "-metadata:s:a:0", "language=fra", out]


def test_run_remux_creates_missing_parent_dirs(tmp_path, monkeypatch):
    out = str(tmp_path / "new" / "sub" / "out.mp4")    # parents do not exist yet

    def fake_ffmpeg(args, duration=None):
        with open(args[-1], "wb") as fh:
            fh.write(b"muxed")

    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    n = run_remux("ffmpeg", "in.ts", "AV", out)
    assert (tmp_path / "new" / "sub" / "out.mp4").read_bytes() == b"muxed"
    assert n == len(b"muxed")


def test_run_remux_removes_faulty_output_on_failure(tmp_path, monkeypatch):
    out = tmp_path / "out.mp4"

    def fake_ffmpeg(args, duration=None):
        with open(args[-1], "wb") as fh:        # ffmpeg writes a partial file...
            fh.write(b"partial garbage")
        raise RuntimeError("ffmpeg failed (exit 1): boom")   # ...then dies

    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        run_remux("ffmpeg", "in.ts", "AV", str(out))
    assert not out.exists()                     # faulty target cleaned up


def test_run_remux_failure_without_output_is_fine(tmp_path, monkeypatch):
    out = tmp_path / "out.mp4"

    def fake_ffmpeg(args, duration=None):
        raise RuntimeError("ffmpeg failed (exit 1): no output written")

    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        run_remux("ffmpeg", "in.ts", "AV", str(out))
    assert not out.exists()


def test_run_ffmpeg_missing_binary_raises(tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        run_ffmpeg(["this-ffmpeg-does-not-exist", "-version"])


# -- remux truncation guard (item 6) ------------------------------------------
# A silently-truncated source (one that slipped past the download guards because
# no Content-Length/ETag was available) is often copied by ffmpeg without error,
# yielding a short output. run_remux compares source vs output duration and fails
# so a truncated film never reaches the library. The check is skipped when either
# duration is unknown, so it never breaks a healthy remux whose probe can't read a
# test stub (or when ffmpeg is absent).

def test_remux_rejects_truncated_source(tmp_path, monkeypatch):
    out = str(tmp_path / "out.mp4")

    def fake_ffmpeg(args, duration=None):
        with open(args[-1], "wb") as fh:
            fh.write(b"short")
        return 12.0                                # -progress: only 12 s written
    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(files, "probe_duration", lambda ff, path: 3600.0)  # source 1 h
    with pytest.raises(RuntimeError, match="truncated"):
        run_remux("ffmpeg", "in.ts", "AV", out)
    assert not os.path.exists(out)                 # short output dropped, not kept


def test_remux_accepts_matching_duration(tmp_path, monkeypatch):
    out = str(tmp_path / "out.mp4")

    def fake_ffmpeg(args, duration=None):
        with open(args[-1], "wb") as fh:
            fh.write(b"full")
        return 3600.0                              # -progress: full length written
    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(files, "probe_duration", lambda ff, path: 3600.0)
    n = run_remux("ffmpeg", "in.ts", "AV", out)
    assert n == 4                                  # accepted (len b"full")


def test_remux_uses_progress_duration_not_output_container(tmp_path, monkeypatch):
    # The .aac regression: a raw-stream output reports no container duration, so
    # ffmpeg estimates it from bitrate -- materially short of the truth. The check
    # must trust ffmpeg's -progress out_time (run_ffmpeg's return), never re-probe
    # the output container, or every audio-only remux would be falsely rejected.
    out = str(tmp_path / "out.aac")

    def fake_ffmpeg(args, duration=None):
        with open(args[-1], "wb") as fh:
            fh.write(b"full audio")
        return 5412.0                              # -progress: true written length
    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    # probe_duration reads the SOURCE header; a re-probe of the output would yield
    # the short bitrate estimate (5183) and wrongly fail the remux.
    monkeypatch.setattr(files, "probe_duration",
                        lambda ff, path: 5413.0 if path == "in.ts" else 5183.0)
    n = run_remux("ffmpeg", "in.ts", "A", out)
    assert n == len(b"full audio")                 # accepted, not mistaken for truncation


def test_remux_skips_check_when_source_duration_unknown(tmp_path, monkeypatch):
    out = str(tmp_path / "out.mp4")

    def fake_ffmpeg(args, duration=None):
        with open(args[-1], "wb") as fh:
            fh.write(b"x")
        return 1.0
    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(files, "probe_duration", lambda ff, path: None)  # source unknown
    n = run_remux("ffmpeg", "in.ts", "AV", out)    # no reference -> no guard
    assert n == 1


def test_probe_duration_parses_ffmpeg_stderr(monkeypatch):
    class Proc:
        stderr = "  Duration: 01:02:03.50, start: 0.0\n"
    monkeypatch.setattr(files.subprocess, "run", lambda *a, **k: Proc())
    assert files.probe_duration("ffmpeg", "x.mp4") == 3723.5   # 3600 + 120 + 3.5


class FakePopen:
    """Fake subprocess.Popen for run_ffmpeg: serves stdout_text as the -progress
    key=value stream and stderr_text as the diagnostics stream, returning
    returncode from wait()."""

    def __init__(self, stdout_text="", stderr_text="", returncode=0):
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self._rc = returncode

    def wait(self):
        return self._rc


def test_run_ffmpeg_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(files.subprocess, "Popen",
                        lambda *a, **k: FakePopen(stderr_text="boom line 1\nboom line 2\n",
                                                  returncode=1))
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        run_ffmpeg(["ffmpeg", "-i", "x"])


def test_run_ffmpeg_injects_progress_flags(monkeypatch):
    seen = {}
    def fake_popen(args, **k):
        seen["args"] = list(args)
        return FakePopen()
    monkeypatch.setattr(files.subprocess, "Popen", fake_popen)
    run_ffmpeg(["ffmpeg", "-i", "in.ts", "out.mp4"])
    assert "-progress" in seen["args"] and "pipe:1" in seen["args"]
    assert "-nostats" in seen["args"]
    assert seen["args"][0] == "ffmpeg" and seen["args"][-1] == "out.mp4"


# ffmpeg's -progress stream is newline-terminated key=value blocks, each ending in
# progress=continue (progress=end on the last). 40 s total, a block every 4 s of
# media time; out_time_us is the elapsed position in microseconds.
FFMPEG_PROGRESS = (
    "frame=100\nout_time_us=4000000\nprogress=continue\n"
    "frame=200\nout_time_us=8000000\nprogress=continue\n"
    "frame=300\nout_time_us=12000000\nprogress=continue\n"
    "frame=1000\nout_time_us=40000000\nprogress=end\n")


def test_run_ffmpeg_logs_progress_from_progress_stream(monkeypatch, caplog):
    monkeypatch.setattr(files.subprocess, "Popen",
                        lambda *a, **k: FakePopen(stdout_text=FFMPEG_PROGRESS))
    caplog.set_level(logging.INFO, logger="theke")
    run_ffmpeg(["ffmpeg", "-i", "in.ts", "out.mp4"], duration=40.0)   # label = out.mp4
    # 40 s total -> 10% step is 4 s; times 4/8/12/40 s -> 10/20/30/100 percent
    msgs = [r.getMessage() for r in caplog.records if "%" in r.getMessage()]
    assert msgs == [
        "out.mp4: 00:00:04 / 00:00:40 (10%)",
        "out.mp4: 00:00:08 / 00:00:40 (20%)",
        "out.mp4: 00:00:12 / 00:00:40 (30%)",
        "out.mp4: 00:00:40 / 00:00:40 (100%)"]


def test_run_ffmpeg_without_duration_logs_no_progress(monkeypatch, caplog):
    monkeypatch.setattr(files.subprocess, "Popen",
                        lambda *a, **k: FakePopen(stdout_text=FFMPEG_PROGRESS))
    caplog.set_level(logging.INFO, logger="theke")
    run_ffmpeg(["ffmpeg", "-i", "in.ts", "out.mp4"])              # no duration -> silent
    assert [r.getMessage() for r in caplog.records if "%" in r.getMessage()] == []


def test_run_ffmpeg_returns_final_output_duration(monkeypatch):
    monkeypatch.setattr(files.subprocess, "Popen",
                        lambda *a, **k: FakePopen(stdout_text=FFMPEG_PROGRESS))
    # final out_time_us is 40000000 us -> 40.0 s, the true written length
    assert run_ffmpeg(["ffmpeg", "-i", "in.ts", "out.mp4"]) == 40.0


def test_run_ffmpeg_returns_none_without_progress(monkeypatch):
    monkeypatch.setattr(files.subprocess, "Popen",
                        lambda *a, **k: FakePopen(stdout_text=""))   # no progress emitted
    assert run_ffmpeg(["ffmpeg", "-i", "in.ts", "out.mp4"]) is None


# -- check-ffmpeg (probe the configured binary via -version) -------------------

class FakeRun:
    """Fake subprocess.run result for check_ffmpeg: serves stdout and returncode."""

    def __init__(self, stdout, returncode):
        self.stdout = stdout
        self.returncode = returncode


def test_check_ffmpeg_returns_version_line(monkeypatch):
    version = ("ffmpeg version 6.0 Copyright (c) 2000-2023 the FFmpeg developers\n"
               "built with gcc 13.1.1\n")
    monkeypatch.setattr(files.subprocess, "run",
                        lambda *a, **k: FakeRun(version, 0))
    assert check_ffmpeg("ffmpeg") == (
        "ffmpeg version 6.0 Copyright (c) 2000-2023 the FFmpeg developers")


def test_check_ffmpeg_missing_binary_raises():
    with pytest.raises(RuntimeError, match="not found"):
        check_ffmpeg("this-ffmpeg-does-not-exist")


def test_check_ffmpeg_error_shows_expanded_path(monkeypatch):
    monkeypatch.setenv("THEKE_FF_DIR", "nope-ffmpeg-dir")
    with pytest.raises(RuntimeError) as exc:
        check_ffmpeg("$THEKE_FF_DIR/ffmpeg")   # not found -> expanded path in message
    assert os.path.abspath("nope-ffmpeg-dir/ffmpeg") in str(exc.value)


def test_check_ffmpeg_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(files.subprocess, "run",
                        lambda *a, **k: FakeRun("", 1))
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        check_ffmpeg("ffmpeg")


def test_cli_file_remux_check_ffmpeg_json(capsys, monkeypatch):
    monkeypatch.setattr(files.subprocess, "run",
                        lambda *a, **k: FakeRun("ffmpeg version 6.0\n", 0))
    rc = main(["--json", "file", "remux", "--check-ffmpeg"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "check_ffmpeg": True, "ffmpeg_path": "ffmpeg", "version": "ffmpeg version 6.0"}


def test_cli_file_remux_check_ffmpeg_missing_binary_errors(capsys, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(files.subprocess, "run", boom)
    rc = main(["--json", "file", "remux", "--check-ffmpeg"])
    assert rc == 1
    assert "not found" in json.loads(capsys.readouterr().out)["error"]


def test_cli_file_remux_json(tmp_path, capsys, monkeypatch):
    out = str(tmp_path / "out.mp4")
    monkeypatch.setattr(files, "run_ffmpeg",
                        lambda args, duration=None: open(args[-1], "wb").write(b"ok"))
    rc = main(["--json", "file", "remux", "--in", "in.ts", "--mode", "AV",
               "--out", out])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"remux": "AV", "out": out}


def test_cli_file_remux_bad_mode_is_usage_error():
    assert main(["file", "remux", "--in", "in.ts", "--mode", "X",
                 "--out", "o.mp4"]) == 2


# -- remux-subtitle (ffmpeg-free TTML/VTT -> SRT/ASS/TTML) ---------------------

_TTML_IN = ('<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
            '<p begin="00:00:01.000" end="00:00:02.000">Hallo</p>'
            '</div></body></tt>')


def test_cli_file_remux_subtitle_single_format_json(tmp_path, capsys):
    src = tmp_path / "Mobbing.xml"
    src.write_text(_TTML_IN, encoding="utf-8")
    dest = str(tmp_path / "Mobbing.de.srt")
    rc = main(["--json", "file", "remux-subtitle", "--in", str(src),
               "--language", "de", "--format", "srt"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"subtitle": [dest]}
    with open(dest, encoding="utf-8") as fh:
        assert "Hallo" in fh.read()


def test_cli_file_remux_subtitle_defaults_to_configured_formats(tmp_path):
    src = tmp_path / "Mobbing.xml"
    src.write_text(_TTML_IN, encoding="utf-8")
    rc = main(["file", "remux-subtitle", "--in", str(src)])   # default: srt, ass, ttml
    assert rc == 0
    for ext in (".de.srt", ".de.ass", ".de.ttml"):
        assert os.path.exists(str(tmp_path / ("Mobbing" + ext))), ext


# -- move ---------------------------------------------------------------------

def test_move_creates_parent_dirs_and_moves(tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"film")
    dst = tmp_path / "lib" / "Movie (2020)" / "Movie (2020).mp4"
    out = move_file(str(src), str(dst), force=False)
    assert out == str(dst)
    assert dst.read_bytes() == b"film"
    assert not src.exists()


def test_move_existing_dst_errors(tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"new")
    dst = tmp_path / "dst.mp4"
    dst.write_bytes(b"old")
    with pytest.raises(RuntimeError, match="exists"):
        move_file(str(src), str(dst), force=False)
    assert dst.read_bytes() == b"old"          # untouched
    assert src.exists()


def test_move_force_overwrites(tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"new")
    dst = tmp_path / "dst.mp4"
    dst.write_bytes(b"old")
    move_file(str(src), str(dst), force=True)
    assert dst.read_bytes() == b"new"


# -- atomic move into the library (item 5) ------------------------------------
# A cross-device move is copy-then-delete; interrupted mid-copy it can leave a
# partial file under the final library name -- and with force the prior good file
# was already deleted. The fix lands the payload on a temp name on the destination
# filesystem and swaps it in with one atomic os.replace, so a failed copy never
# touches the final name and the prior file survives until the swap.

def test_move_failure_keeps_prior_file_and_no_partial(tmp_path, monkeypatch):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"NEWDATA")
    dst = tmp_path / "lib" / "movie.mp4"
    dst.parent.mkdir()
    dst.write_bytes(b"OLD-GOOD")                 # prior library file

    def boom(s, d):
        raise OSError("disk full mid-copy")
    monkeypatch.setattr(files.shutil, "move", boom)
    with pytest.raises(OSError):
        move_file(str(src), str(dst), force=True)
    assert dst.read_bytes() == b"OLD-GOOD"       # prior file intact, not pre-deleted


def test_move_success_leaves_no_temp(tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"film")
    dst = tmp_path / "lib" / "movie.mp4"
    move_file(str(src), str(dst), force=False)
    assert dst.read_bytes() == b"film"
    assert not (tmp_path / "lib" / "movie.mp4.part").exists()   # temp swapped away


def test_cli_file_move_json(tmp_path, capsys):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    dst = str(tmp_path / "lib" / "out.mp4")
    rc = main(["--json", "file", "move", "--in", str(src), "--out", dst])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"moved": dst}
