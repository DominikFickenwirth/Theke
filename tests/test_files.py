"""Tests for the file primitives (phases 6-8): download, remux, move."""

import io
import json
import os

import pytest

import theke
import theke.files as files
from theke import Config, main
from theke.files import (is_hls, is_master, parse_master, parse_media_playlist,
                         download_file, download_hls, ffmpeg_args, run_remux,
                         run_ffmpeg)


def install_http(monkeypatch, mapping):
    """Monkeypatch theke.http_get to serve URL-mapped bytes (or raise an
    Exception value); an unmapped URL is an error."""
    def fake_get(url):
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


# -- direct download (resume + retry) -----------------------------------------

class Opener:
    """Fake open_url: records offsets; serves data[offset:] when the server
    honors the Range (resumable), else the whole body (HTTP 200)."""

    def __init__(self, data, resumable=True):
        self.data = data
        self.resumable = resumable
        self.offsets = []

    def __call__(self, url, offset=0):
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

    def opener(url, offset=0):
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

    def opener(url, offset=0):
        calls["n"] += 1
        if calls["n"] == 1:
            return Failing(data), False                # dies after one chunk
        return io.BytesIO(data[offset:]), offset > 0   # resumes the rest

    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    download_file(out=out, url="http://x", retries=2)
    assert (tmp_path / "v.mp4").read_bytes() == data


def test_download_raises_after_exhausting_retries(tmp_path, monkeypatch):
    def opener(url, offset=0):
        raise RuntimeError("always down")

    monkeypatch.setattr(files, "open_url", opener)
    out = str(tmp_path / "v.mp4")
    with pytest.raises(RuntimeError, match="always down"):
        download_file(out=out, url="http://x", retries=2)


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

    def fake_ffmpeg(args):
        seen["args"] = args
        with open(args[-1], "wb") as fh:
            fh.write(b"muxed")

    monkeypatch.setattr(files, "run_ffmpeg", fake_ffmpeg)
    run_remux("ffmpeg", "in.ts", "AV", out, language="fra")
    assert seen["args"] == ["ffmpeg", "-y", "-i", "in.ts", "-c", "copy",
                            "-metadata:s:a:0", "language=fra", out]


def test_run_ffmpeg_missing_binary_raises(tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        run_ffmpeg(["this-ffmpeg-does-not-exist", "-version"])


def test_run_ffmpeg_nonzero_exit_raises(monkeypatch):
    class Proc:
        returncode = 1
        stderr = "boom line 1\nboom line 2\n"

    monkeypatch.setattr(files.subprocess, "run", lambda *a, **k: Proc())
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        run_ffmpeg(["ffmpeg", "-i", "x"])


def test_cli_file_remux_json(tmp_path, capsys, monkeypatch):
    out = str(tmp_path / "out.mp4")
    monkeypatch.setattr(files, "run_ffmpeg",
                        lambda args: open(args[-1], "wb").write(b"ok"))
    rc = main(["--json", "file", "remux", "--in", "in.ts", "--remux", "AV",
               "--out", out])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"remux": "AV", "out": out}


def test_cli_file_remux_bad_mode_is_usage_error():
    assert main(["file", "remux", "--in", "in.ts", "--remux", "X",
                 "--out", "o.mp4"]) == 2
