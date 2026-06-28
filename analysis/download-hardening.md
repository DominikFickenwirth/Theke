# Download hardening -- residual failure sources

Follow-ups found after fixing (a) silently-truncated direct downloads and (b)
the hang on a dropped connection (network timeout). These are the *same* failure
classes in code paths the first two fixes did not cover, plus a few new ones.

Each item is one TDD step (red commit + green commit), reviewed before the next.
Recommended order is the numbering below. Items 1 and 2 are closely related and
may be done in one step if it stays small.

Key files: `theke/files.py` (download/remux/move primitives, network via
`open_url`/`theke.http_get`, ffmpeg via `run_ffmpeg`), `theke/__init__.py`
(`http_get`, `_download_entry`, `_fetch`, CLI wiring), `tests/test_files.py`.

---

## 1. HLS segments are not length-checked -- truncation passes silently

**Where:** `files.py` `_fetch_segment` / `_download_segments`.
**Symptom:** Each segment is fetched with `theke.http_get` and written as-is. The
truncation guard added for direct downloads lives only in `_download_once`.
A segment delivered over a connection-close stream *without* `Content-Length`
can arrive short, get written, concatenated, and the whole HLS output looks
"done" while being corrupt. (Segments *with* `Content-Length` are already saved
by `http.client` raising `IncompleteRead` into the retry loop -- so the gap is
specifically the no-Content-Length case.)
**Desired:** A segment whose received length is below its advertised
`Content-Length` is rejected (raises into the segment retry loop), never written
as final. Decide and document behaviour when no length is advertised at all
(see item 2 -- keep consistent).
**Test edge cases:** short segment vs advertised length -> raises/retries; exact
length -> ok; missing Content-Length -> matches the policy chosen in item 2.

## 2. Direct download with no Content-Length -- truncation undetectable

**Where:** `files.py` `_download_once` (the `total is not None` guard).
**Symptom:** The truncation check only fires when a `Content-Length` is known.
An HTTP/1.0 / `Connection: close` response with no `Content-Length` that drops
at EOF reads as a clean empty buffer -> looks complete.
**Desired:** Define and implement a policy for the unknown-length case. Options
to weigh in the prompt: treat zero-byte / suspiciously-small results as failures;
or probe the finished file (ffmpeg/duration) before accepting. At minimum, an
empty result must not be accepted as a complete download.
**Test edge cases:** no Content-Length + non-empty stream -> ok; no
Content-Length + empty stream -> failure; keep the Content-Length path unchanged.

## 3. Resume appends across a changed/corrupt .part

**Where:** `files.py` `_download_once` (Range resume from a leftover `.part`).
**Symptom:** Resume is by byte offset only -- no `ETag`/`Last-Modified`/
`If-Range` validation. If the remote file changed between attempts (re-encode,
different version), the resumed bytes splice two files together; if lengths
happen to match, the truncation check still passes -> silent corruption.
**Desired:** Send `If-Range` (using the stored ETag/Last-Modified from the first
response) so a changed resource makes the server return 200 (full body) instead
of 206; then restart from scratch instead of appending. Persist the validator
alongside the `.part` (sidecar file) so it survives process restarts.
**Test edge cases:** unchanged resource -> 206, resumes; changed resource -> 200,
restarts cleanly; no validator available -> current behaviour.

## 4. ffmpeg HLS fallback can hang on a dropped connection

**Where:** `files.py` `_hls_ffmpeg` and the encrypted-HLS handoff in
`download_hls`.
**Symptom:** These run `ffmpeg -i <url>` with no `-rw_timeout` / `-timeout`.
This is the *same* hang already fixed for direct downloads, but ffmpeg fetching
the stream itself has no timeout, so a network drop hangs the process forever.
The configured `download_timeout` never reaches ffmpeg.
**Desired:** Pass the configured timeout to ffmpeg (`-rw_timeout` in microseconds,
and/or `-timeout`) on both ffmpeg-fetches-network paths. Thread `timeout` through
`_hls_ffmpeg`.
**Test edge cases:** assert the built ffmpeg arg list contains the timeout flag
with the value derived from `download_timeout`; None timeout -> no flag (or a
documented default).

## 5. Final move into the library is non-atomic across filesystems

**Where:** `files.py` `move_file`.
**Symptom:** Uses `shutil.move`. With `temp_path` local and `library_path` on a
NAS / different mount (the stated deployment), `shutil.move` becomes copy-then-
delete. With `force` it first `os.remove(dst)` then copies, so an interrupted
copy leaves a partial file in the library under the real filename, having already
deleted the previous good copy.
**Desired:** Copy to a temp name on the destination filesystem, then atomic
`os.replace` into the final name; only remove the prior file once the new one is
in place. Never leave a partial file under the final name.
**Test edge cases:** simulate a copy failure mid-way (monkeypatch) -> final path
untouched / prior file intact, no partial under final name; success -> file at
final path, temp gone; cross-device path still works.

## 6. Corrupt source is silently remuxed

**Where:** `__init__.py` `_download_entry` -> `run_remux`.
**Symptom:** The downloaded file is handed straight to ffmpeg. ffmpeg often exits
0 on a slightly-truncated input (it remuxes what it has), so items 1-3 can
escape detection at the remux stage too -- output looks "done" but is short.
**Desired:** A post-download or post-remux sanity check (e.g. probe source
duration vs remux output duration, or a minimum-size / ffmpeg `-v error`
validation) that fails the entry instead of moving a short file into the library.
Coordinate with items 1-3 to avoid double-guarding.
**Test edge cases:** truncated source -> entry fails, nothing moved; healthy
source -> passes; keep the happy path green.

## 7. All exceptions treated as transient/retryable

**Where:** `files.py` `download_file` and `_download_segments` retry loops (bare
`except Exception`).
**Symptom:** A permanent `404`/`403`/`410` burns all retries pointlessly. Worse,
a stale *oversized* `.part` (>= remote size) makes the server return `416 Range
Not Satisfiable`, which is retried and then fails permanently -- the resume logic
never deletes the bad `.part` to self-heal, and re-approving the row will not
help either.
**Desired:** Classify errors: non-retryable HTTP statuses fail fast (no retry
spin); a `416` (or any "range unsatisfiable") drops the `.part` and restarts from
scratch once. Keep transient/network errors retrying as today.
**Test edge cases:** 404 -> fails without consuming all retries; 416 with a stale
oversized .part -> .part removed, full re-download, success; transient error ->
still retries.

## 8. Socket timeout is not a total/stall timeout

**Where:** `files.py` download loops + `open_url` (the `timeout` is per socket
operation).
**Symptom:** `download_timeout` bounds each `read()`, not the whole transfer. A
server trickling one buffer per interval (slowloris-style, or a degraded link)
keeps the download alive forever without ever tripping the timeout. No wall-clock
or throughput floor.
**Desired:** Add a stall / minimum-throughput guard: e.g. fail if no measurable
progress over a configurable window, or cap total wall-clock per transfer. New
config knob (document default). Keep it cheap and test-friendly (inject a clock).
**Test edge cases:** steady slow-but-progressing transfer -> ok; stalled/trickle
transfer -> fails after the window; inject the clock so the test is fast.

## 9. No disk-space handling / no library-filename sanitization

**Where:** `files.py` write loops (`ENOSPC`) and `library_path` templating in
`__init__.py` (path built from enriched titles).
**Symptom:** `ENOSPC` mid-write is caught as a transient error and retried
uselessly, then leaves a `.part`. Separately, a title with FS-reserved chars
(`:` `?` trailing space, Windows/NAS) makes the final open/move fail only at the
very end, after the whole download+remux completed.
**Desired:** (a) Treat `ENOSPC`/disk-full as non-retryable, fail fast with a clear
message. (b) Sanitize the templated library filename for the target FS (reserved
chars, trailing dots/spaces) at the point the path is built, before any work
starts. Consider splitting into two TDD steps if it grows.
**Test edge cases:** disk-full -> immediate clear failure, no retry spin; title
with reserved chars -> sanitized path, move succeeds; already-clean title ->
unchanged.
