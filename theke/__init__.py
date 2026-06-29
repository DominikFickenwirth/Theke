"""Theke -- self-hosted media manager CLI.

All logic lives in this package module (split into more files later if ever
needed). Sections: config / DB / CLI.
"""

import argparse
import dataclasses
import glob
import hashlib
import io
import json
import logging
import lzma
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, time, timezone

from theke import core
from theke.core import (Config, ConfigError, CONFIG_DEFAULT_PATH, load_config,
                        DbError, DbLockedError, MIGRATIONS, db_connect,
                        db_get_meta, db_set_meta)
from theke.enrich import enrich, looks_like_country, GENRE_SET, ENRICH_COLS, CATWORD, FICTION_TOPICS
from theke.match import (tmdb_movie, find_matches, tmdb_tv, find_episode_matches,
                         arte_anchor_ids, find_arte_links, tmdb_search, pick_by_year)
from theke.queue import select_downloads, resolution_of
from theke.files import is_hls, download_file, download_hls, run_remux, check_ffmpeg, move_file
from theke import subtitle

# Progress and diagnostics go to this logger; main() routes it to stderr so the
# stdout result (the single JSON object) stays clean. Tests capture it via
# pytest's caplog, so no progress argument needs to be threaded through the code.
log = logging.getLogger("theke")


# -- fetch --------------------------------------------------------------------
# Download the MediathekView film list and import it into the mediathek table.
# The list is an XZ-compressed, flat JSON object with duplicate keys
# ("Filmliste" twice, then many "X"); each "X" is a 20-field array.

# Field order of an "X" array, exactly as in the MV format (DatenFilm). The
# rtmp_* slots are dead legacy fields and never become columns.
FIELDS = [
    "sender", "thema", "titel", "datum", "zeit", "dauer", "groesse_mb",
    "beschreibung", "url", "website", "url_untertitel", "url_rtmp",
    "url_klein", "url_rtmp_klein", "url_hd", "url_rtmp_hd", "datum_l",
    "url_history", "geo", "neu",
]


def film_id(sender, thema, url, website) -> str:
    """Film identity exactly like MediathekView's DatenFilm.getSha256():
    SHA-256 over sender + thema + url + website, each UTF-16LE-encoded."""
    digest = hashlib.sha256()
    for part in (sender, thema, url, website):
        digest.update(part.encode("utf-16-le"))
    return digest.hexdigest()


def decode_rel_url(base: str, encoded: str) -> str:
    """Decode MV's relative URL scheme "offset|suffix" = base[:offset] + suffix.
    Empty -> empty; no "|" (or non-numeric offset) -> taken verbatim."""
    if not encoded:
        return ""
    sep = encoded.find("|")
    if sep == -1:
        return encoded
    try:
        cut = int(encoded[:sep])
    except ValueError:
        return encoded
    return base[:cut] + encoded[sep + 1:]


def parse_duration(value: str):
    """"HH:MM:SS" -> seconds, or None if absent/unparseable."""
    try:
        hours, minutes, seconds = (int(p) for p in value.split(":"))
        return hours * 3600 + minutes * 60 + seconds
    except (ValueError, AttributeError):
        return None


def to_int(value: str):
    """Parse an integer field, or None if absent/unparseable."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def parse_date(datum: str, zeit: str, datum_l: str):
    """Broadcast time as ISO "YYYY-MM-DD HH:MM:SS".

    The German wall-clock strings are authoritative and timezone-free; the
    datum_l epoch is only a fallback (read as UTC) when no date string exists.
    Converting datum_l to the right wall clock would need the Europe/Berlin
    zone, unavailable in the stdlib on Windows.
    """
    if datum:
        try:
            stamp = datetime.strptime(
                f"{datum} {zeit or '00:00:00'}", "%d.%m.%Y %H:%M:%S")
            return stamp.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    if datum_l:
        try:
            return datetime.fromtimestamp(
                int(datum_l), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _build_film(values, last_sender, last_thema) -> dict:
    """Turn one "X" array into a DB-shaped row dict (column names already)."""
    raw = dict(zip(FIELDS, values))
    sender = raw.get("sender") or last_sender
    thema = raw.get("thema") or last_thema
    url = raw.get("url", "")
    return {
        "mediathek_id":    film_id(sender, thema, url, raw.get("website", "")),
        "status":          "0",
        "sender":          sender,
        "topic":           thema,
        "title":           raw.get("titel", ""),
        "description":     raw.get("beschreibung", ""),
        "date":            parse_date(raw.get("datum", ""), raw.get("zeit", ""), raw.get("datum_l", "")),
        "duration":        parse_duration(raw.get("dauer", "")),
        "size_mb":         to_int(raw.get("groesse_mb", "")),
        "url_video":       url,
        "url_video_small": decode_rel_url(url, raw.get("url_klein", "")),
        "url_video_hd":    decode_rel_url(url, raw.get("url_hd", "")),
        "url_subtitle":    decode_rel_url(url, raw.get("url_untertitel", "")),
        "url_website":     raw.get("website", ""),
        "url_history":     decode_rel_url(url, raw.get("url_history", "")),
        "geo":             raw.get("geo", ""),
    }


def parse_filmliste(stream, chunk_size=1 << 16):
    """Stream the decompressed MV film list.

    Yields the metadata dict first, then one DB-shaped dict per film. We
    raw_decode one member value at a time over a refilled buffer so memory
    stays flat for the ~500k films. sender/thema inherit from the previous
    film when their field is empty.
    """
    decoder = json.JSONDecoder()
    buf = ""
    pos = 0

    def refill():
        nonlocal buf, pos
        chunk = stream.read(chunk_size)
        if not chunk:
            return False
        buf = buf[pos:] + chunk
        pos = 0
        return True

    def seek(stops):
        # advance past whitespace and any char in `stops`; refill as needed.
        nonlocal pos
        while True:
            while pos < len(buf) and (buf[pos].isspace() or buf[pos] in stops):
                pos += 1
            if pos < len(buf):
                return True
            if not refill():
                return False

    def decode():
        # raw_decode one JSON value at pos, refilling while it looks incomplete.
        nonlocal pos
        while True:
            try:
                value, pos = decoder.raw_decode(buf, pos)
                return value
            except json.JSONDecodeError:
                if not refill():
                    raise

    if not seek(""):
        return
    pos += 1  # consume the opening '{'

    metadata = None
    seen_filmliste = 0
    last_sender = ""
    last_thema = ""
    while seek(","):
        if buf[pos] == "}":
            break
        key = decode()
        seek(":")
        value = decode()
        if key == "Filmliste":
            seen_filmliste += 1
            if seen_filmliste == 1:  # first = metadata, second = column names
                metadata = {
                    "erstellt_am": value[1] if len(value) > 1 else "",
                    "id":          value[4] if len(value) > 4 else "",
                }
                yield metadata
        elif key == "X":
            if metadata is None:  # malformed list without a header
                metadata = {"erstellt_am": "", "id": ""}
                yield metadata
            film = _build_film(value, last_sender, last_thema)
            last_sender = film["sender"]
            last_thema = film["topic"]
            yield film


# Columns enrich reads as input (== enrich() signature). A refresh that
# leaves every one of these unchanged does not invalidate an existing
# enrichment, so status is preserved on such an overwrite (see _UPSERT_SQL).
ENRICH_INPUT_COLS = ["sender", "topic", "title", "description", "duration"]

# Columns written on every import. language/tmdb_id/imdb_id/match_confidence are
# owned by phase 3 and never touched here, so an ID assignment survives every
# refresh. status is special: an overwrite resets it to '0' (re-enrich) only
# when a enrich-relevant column changed, else the existing status is kept.
MIRROR_COLS = [
    "mediathek_id", "status", "sender", "topic", "title", "description",
    "date", "duration", "size_mb", "url_video", "url_video_small",
    "url_video_hd", "url_subtitle", "url_website", "url_history", "geo",
]

# Null-safe (IS NOT) compare of the existing row against the incoming (excluded)
# values; any enrich-input change flips status to '0', otherwise it is kept.
_STATUS_SET = (
    "status=CASE WHEN "
    + " OR ".join(f"{c} IS NOT excluded.{c}" for c in ENRICH_INPUT_COLS)
    + " THEN '0' ELSE status END"
)

_UPSERT_SQL = (
    "INSERT INTO mediathek ({cols}) VALUES ({vals}) "
    "ON CONFLICT(mediathek_id) DO UPDATE SET {sets}"
).format(
    cols=", ".join(MIRROR_COLS),
    vals=", ".join(":" + c for c in MIRROR_COLS),
    sets=", ".join([_STATUS_SET]
                   + [f"{c}=excluded.{c}" for c in MIRROR_COLS
                      if c not in ("mediathek_id", "status")]),
)


def _upsert_films(conn, films, batch=5000) -> int:
    """Upsert film rows in batches; return the number of rows written. Logs a
    running count every 50k rows so a big import shows progress."""
    count = 0
    reported = 0
    rows = []

    def flush():
        nonlocal count, reported
        if not rows:
            return
        conn.executemany(_UPSERT_SQL, rows)
        count += len(rows)
        rows.clear()
        if count - reported >= 50000:
            reported = count
            log.info("imported %d films", count)

    for film in films:
        rows.append(film)
        if len(rows) >= batch:
            flush()
    flush()
    return count


def _store_meta(conn, meta):
    db_set_meta(conn, "filmliste_id",      meta.get("id", ""))
    db_set_meta(conn, "filmliste_created", meta.get("erstellt_am", ""))


def import_films(conn, films, meta) -> dict:
    """Import a list (full or diff alike): upsert the new/changed films and store
    the list metadata in one transaction, so an abort rolls back cleanly and a
    re-run is idempotent. Entries no longer in the source are kept -- the mirror
    only grows or updates, never deletes."""
    log.info("importing into the database")
    conn.execute("BEGIN")
    try:
        imported = _upsert_films(conn, films)
        _store_meta(conn, meta)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"imported": imported}


def can_use_diff(lastcreated, now=None) -> bool:
    """A diff is usable only if the local list was created after today 07:00
    UTC (MediathekView's FilmListMetaData.canUseDiffList)."""
    try:
        stamp = datetime.strptime(
            lastcreated, "%d.%m.%Y, %H:%M").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    now = now or datetime.now(timezone.utc)
    cutoff = datetime.combine(now.date(), time(7, 0), tzinfo=timezone.utc)
    return stamp > cutoff


def fetch_list_id(cfg):
    """Server's filmliste.id (hash of the current server list), or None if the
    small request fails (then we re-download to be safe)."""
    try:
        return core.http_get(cfg.filmliste_id_url, cfg.download_timeout).decode("utf-8").strip()
    except Exception:
        return None


def _load_list(url, timeout=None):
    """Download, stream-decompress and parse a list; return (meta, films)."""
    log.info("downloading %s", url.rsplit("/", 1)[-1])
    raw = core.http_get(url, timeout)
    log.info("download done (%.1f MB), unpacking", len(raw) / (1 << 20))
    stream = lzma.open(io.BytesIO(raw), "rt", encoding="utf-8")
    films = parse_filmliste(stream)
    return next(films), films  # metadata is the first yield


def _do_full(conn, cfg):
    meta, films = _load_list(cfg.filmliste_url, cfg.download_timeout)
    return {"action": "full", **import_films(conn, films, meta)}


def _do_diff(conn, cfg):
    try:
        meta, films = _load_list(cfg.filmliste_diff_url, cfg.download_timeout)
    except Exception:
        return None  # download/parse failed -> caller falls back to full
    return {"action": "diff", **import_films(conn, films, meta)}


def cmd_fetch(conn, cfg, args: argparse.Namespace) -> dict:
    """Refresh the film-list mirror (MediathekView update logic)"""
    if args.force or db_get_meta(conn, "filmliste_id") is None:
        return _do_full(conn, cfg)
    log.info("checking the server list id")
    if fetch_list_id(cfg) == db_get_meta(conn, "filmliste_id"):
        return {"action": "skip"}
    if can_use_diff(db_get_meta(conn, "filmliste_created")):
        result = _do_diff(conn, cfg)
        if result and result["imported"]:
            return result
        return _do_full(conn, cfg)  # diff failed or was empty
    return _do_full(conn, cfg)


# -- enrich -----------------------------------------------------------------
# Extract structured metadata from the free-text fields and flip status 0 -> 1.

_ENRICH_READ = (
    "SELECT mediathek_id, " + ", ".join(ENRICH_INPUT_COLS)
    + " FROM mediathek WHERE status='0'"
)

_UPDATE_SQL = (
    "UPDATE mediathek SET {sets}, status='1' WHERE mediathek_id=:mediathek_id"
).format(sets=", ".join(f"{c}=:{c}" for c in ENRICH_COLS))

# reset clears what a stage wrote, back to the freshly-fetched baseline: match
# owns tmdb_id/match_confidence (tmdb_id has a '' default), enrich additionally
# owns the enrich columns (language too carries a '' default).
_MATCH_CLEAR  = "tmdb_id='', match_confidence=NULL"
_ENRICH_CLEAR = ", ".join(f"{c}=''" if c == "language" else f"{c}=NULL"
                          for c in ENRICH_COLS) + ", " + _MATCH_CLEAR


def cmd_enrich(conn, cfg, args: argparse.Namespace) -> dict:
    """Dispatch a enrich action: `run` writes the enrich columns; the others
    (`report`/`audit`/`show`/`dist`) are read-only inspection tools."""
    match args.enrich_cmd:
        case "run":    return _enrich_run(conn, cfg, args)
        case "reset":  return _enrich_reset(conn, args)
        case "report": return _enrich_report_cmd(conn, args)
        case "audit":  return _enrich_audit_cmd(conn, args)
        case "show":   return _enrich_show_cmd(conn, args)
        case "dist":   return _enrich_dist_cmd(conn, args)
        case _: raise DbError(f"unhandled enrich action: {args.enrich_cmd}")


def _enrich_run(conn, cfg, args) -> dict:
    """Enrich mediathek rows into the enrich columns and flip status 0 -> 1.
    By default only unenriched rows (status '0'); --force reprocesses all."""
    sql = _ENRICH_READ if not args.force else _ENRICH_READ.replace(
        " WHERE status='0'", "")
    fiction = FICTION_TOPICS | {t.casefold() for t in cfg.fiction_topics}
    log.info("enriching rows")
    conn.execute("BEGIN")
    try:
        count = _enrich_rows(conn, conn.execute(sql), fiction)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"enriched": count}


def _enrich_reset(conn, args) -> dict:
    """Undo enrich: take enriched/matched rows (status '1'/'2') back to '0', as
    if freshly fetched. Clears the enrich + match columns unless --status-only."""
    sets = "status='0'" if args.status_only else f"status='0', {_ENRICH_CLEAR}"
    return _reset(conn, sets, "status IN ('1','2')")


def _reset(conn, sets, where) -> dict:
    """Run one status-reset UPDATE in a transaction; report the rows changed."""
    conn.execute("BEGIN")
    try:
        count = conn.execute(f"UPDATE mediathek SET {sets} WHERE {where}").rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"reset": count}


def _enrich_rows(conn, rows, fiction_topics=FICTION_TOPICS, batch=5000) -> int:
    """Stream rows through enrich(), write updates in batches; log every 50k."""
    count = 0
    reported = 0
    params = []

    def flush():
        nonlocal count, reported
        if not params:
            return
        conn.executemany(_UPDATE_SQL, params)
        count += len(params)
        params.clear()
        if count - reported >= 50000:
            reported = count
            log.info("enriched %d rows", count)

    for row in rows:
        meta = enrich(row["sender"], row["topic"], row["title"],
                        row["description"], row["duration"], fiction_topics)
        meta["mediathek_id"] = row["mediathek_id"]
        params.append(meta)
        if len(params) >= batch:
            flush()
    flush()
    return count


# -- enrich coverage report (read-only) -------------------------------------
# Per-sender coverage of the enrich fields, for iterating the algorithm. Two
# sources, one tally: --analyze reads the stored columns, --dry-run runs
# enrich() live (writing nothing). Both expose the same keys per row.

REPORT_MIN_ROWS = 1000   # senders below this are omitted (long tail of one-offs)

_REPORT_FIELDS = ["year", "country", "se", "cat", "unklar", "genre", "slot", "events",
                  "flag_a", "flag_e", "flag_s", "flag_u", "flag_t"]

# Per-confidence-level buckets for --by-confidence: deterministic levels emitted
# by enrich._confidence (0.9/0.8/0.5/0.2). Counted always, summarized only when
# requested, so the default report shape stays stable.
_CONF_LEVELS = [("c90", 0.9), ("c80", 0.8), ("c50", 0.5), ("c20", 0.2)]


def _split_csv(value):
    """Comma-separated CLI value -> list of trimmed items, or None when unset."""
    if not value:
        return None
    return [s.strip() for s in value.split(",") if s.strip()]


def _sender_clause(senders):
    """WHERE fragment + params restricting to the given senders ('' when None)."""
    if not senders:
        return "", []
    return f"WHERE sender IN ({','.join('?' * len(senders))})", list(senders)


def _new_counter() -> dict:
    return dict.fromkeys(["n"] + _REPORT_FIELDS + [k for k, _ in _CONF_LEVELS], 0)


def _tally(counter, row):
    """Increment a sender's coverage counters from a row/dict (None-safe). Works
    on both a sqlite3.Row (stored columns) and a enrich() result dict."""
    counter["n"] += 1
    if row["year"] is not None:    counter["year"] += 1
    if row["country"] is not None: counter["country"] += 1
    if row["season"] is not None or row["episode"] is not None: counter["se"] += 1
    conf = row["enrich_confidence"]
    if conf is not None and conf >= 0.8: counter["cat"] += 1   # category from a real signal
    if row["category"] is None:     counter["unklar"] += 1     # NULL = unknown medium
    if row["genre"] is not None:    counter["genre"] += 1
    if row["slot"] is not None:     counter["slot"] += 1
    if row["category"] == "Event":  counter["events"] += 1
    if conf is not None:
        for key, level in _CONF_LEVELS:
            if round(conf, 2) == level: counter[key] += 1
    flags = row["flags"] or ""
    for letter in "aesut":
        if letter.upper() in flags: counter["flag_" + letter] += 1


def _summarize(counter, by_confidence=False) -> dict:
    n = counter["n"]
    out = {"n": n}
    out.update({f + "_pct": round(100 * counter[f] / n, 1) for f in _REPORT_FIELDS})
    if by_confidence:
        out.update({k + "_pct": round(100 * counter[k] / n, 1) for k, _ in _CONF_LEVELS})
    return out


def enrich_report(conn, live: bool, min_rows=REPORT_MIN_ROWS, senders=None,
                    by_confidence=False) -> dict:
    """Per-sender enrich coverage. live=False summarizes the stored columns;
    live=True runs enrich() over the rows without writing. `senders` limits the
    scan to a list of senders; `by_confidence` adds per-confidence-level columns.
    Read-only -> no transaction."""
    acc = {}
    where, params = _sender_clause(senders)
    if live:
        rows = conn.execute("SELECT mediathek_id, sender, topic, title, "
                            "description, duration FROM mediathek " + where, params)
        for r in rows:
            meta = enrich(r["sender"], r["topic"], r["title"],
                            r["description"], r["duration"])
            _tally(acc.setdefault(r["sender"], _new_counter()), meta)
    else:
        rows = conn.execute("SELECT sender, year, country, season, episode, "
                            "category, enrich_confidence, flags, genre, slot "
                            "FROM mediathek " + where, params)
        for r in rows:
            _tally(acc.setdefault(r["sender"], _new_counter()), r)
    return {s: _summarize(c, by_confidence) for s, c in acc.items() if c["n"] >= min_rows}


def enrich_report_diff(conn, senders=None, sample_limit=5) -> dict:
    """Per sender/field churn between the stored enrich columns and a live
    enrich() pass: how many rows would change, with a few before/after samples
    ({id, before, after}). Senders with no churn are omitted. Most useful after a
    enrich run (against unenriched rows everything looks 'changed').
    Read-only -> no transaction."""
    where, params = _sender_clause(senders)
    rows = conn.execute(
        "SELECT mediathek_id, sender, topic, title, description, duration, "
        + ", ".join(ENRICH_COLS) + " FROM mediathek " + where, params)
    acc = {}
    for r in rows:
        live = enrich(r["sender"], r["topic"], r["title"],
                        r["description"], r["duration"])
        bucket = acc.setdefault(r["sender"], {})
        for f in ENRICH_COLS:
            if r[f] == live[f]:
                continue
            fb = bucket.setdefault(f, {"changed": 0, "samples": []})
            fb["changed"] += 1
            if len(fb["samples"]) < sample_limit:
                fb["samples"].append(
                    {"id": r["mediathek_id"], "before": r[f], "after": live[f]})
    return {s: b for s, b in acc.items() if b}


def _print_report_diff(diff):
    """Churn per sender/field to stdout: count + a couple of before/after samples."""
    print("enrich churn (stored vs live)")
    for sender in sorted(diff):
        print(f"{sender}:")
        for f in sorted(diff[sender]):
            fb = diff[sender][f]
            ex = "; ".join(f'{s["before"]!r}->{s["after"]!r}' for s in fb["samples"][:2])
            print(f'  {f:20}{fb["changed"]:>8}   {ex}')


_REPORT_TABLE_COLS = [("year", "year"), ("country", "cntry"), ("se", "S/E"),
                      ("cat", "cat"), ("unklar", "unkl"), ("genre", "genre"),
                      ("slot", "slot"), ("events", "evt"), ("flag_a", "A"),
                      ("flag_e", "E"), ("flag_s", "S"), ("flag_u", "U"), ("flag_t", "T")]
_CONF_TABLE_COLS = [("c90", "c.9"), ("c80", "c.8"), ("c50", "c.5"), ("c20", "c.2")]


def _print_report_table(report, mode, by_confidence=False):
    """One aligned line per sender, sorted by row count, to stdout (the result).
    With by_confidence the single cat column is replaced by per-level columns."""
    cols = _REPORT_TABLE_COLS
    if by_confidence:
        i = next(j for j, (f, _) in enumerate(cols) if f == "cat")
        cols = cols[:i] + _CONF_TABLE_COLS + cols[i + 1:]
    print(f"enrich coverage ({mode}, % of rows)")
    print(f'{"SENDER":14}{"n":>8}' + "".join(f"{h:>7}" for _, h in cols))
    for sender, st in sorted(report.items(), key=lambda kv: -kv[1]["n"]):
        print(f'{sender:14}{st["n"]:>8}'
              + "".join(f'{st[f + "_pct"]:>7.1f}' for f, _ in cols))


def _enrich_report_cmd(conn, args) -> dict:
    senders = _split_csv(args.sender)
    if args.diff:
        log.info("running enrich() live to diff against the stored columns")
        diff = enrich_report_diff(conn, senders=senders)
        if args.json:
            return {"mode": "diff", "senders": diff}
        _print_report_diff(diff)
        return {}
    if args.live:
        log.info("running enrich() live (no writes)")
    report = enrich_report(conn, live=args.live, min_rows=args.min_rows,
                             senders=senders, by_confidence=args.by_confidence)
    mode = "live" if args.live else "stored"
    if args.json:
        return {"mode": mode, "senders": report}
    _print_report_table(report, mode, by_confidence=args.by_confidence)
    return {}


# -- enrich audit (read-only findings scan) ---------------------------------
# Promotes the analysis/_audit_sender.py battery into the CLI: per sender, the
# rows where a heuristic visibly mishandled the input (counts coverage as filled
# but not correct). Each check returns {sender: {count, examples}}; enrich_audit
# assembles {sender: {check: ...}}. Read-only.

# Topic that is itself a bare format/genre word (not a real series). CATWORD has
# no bare "Film"/"Doku"; add them and the curated genre rubrics (GENRE_SET).
_BARE_TOPIC = set(re.split(r"\|", CATWORD)) | {"Film", "Doku"} | GENRE_SET
_TOPIC_MARKER = re.compile(
    r"\((?:mit\s+)?(?:Gebärdensprache|Audiodeskription|Hörfassung|klare Sprache"
    r"|Originalversion|mit Untertitel|OmU|OmdU|ÖGS|OV)\)?", re.I)
_TITLE_CREDIT = re.compile(r"\b(?:Film|" + CATWORD + r")\s+von\s+\S", re.I)
_EPISODIC = re.compile(r"Staffel.*Folge|,\s*Folge\s+\d|\bTeil\s+\d|\b\d+\s*/\s*\d+\b", re.I)


def _audit_scan(conn, where, params, select, predicate, limit) -> dict:
    """Generic row-level check: predicate(row) -> example string (or None to skip).
    Buckets count per sender and collects up to `limit` distinct examples."""
    out = {}
    for r in conn.execute(f"SELECT sender, {select} FROM mediathek " + where, params):
        ex = predicate(r)
        if ex is None:
            continue
        b = out.setdefault(r["sender"], {"count": 0, "examples": []})
        b["count"] += 1
        if ex not in b["examples"] and len(b["examples"]) < limit:
            b["examples"].append(ex)
    return out


def _check_bare_topic(conn, where, params, limit):
    return _audit_scan(conn, where, params, "topic",
                       lambda r: r["topic"] if r["topic"] in _BARE_TOPIC else None, limit)


def _check_topic_pipe(conn, where, params, limit):
    return _audit_scan(conn, where, params, "topic",
                       lambda r: r["topic"] if "|" in (r["topic"] or "") else None, limit)


def _check_topic_marker(conn, where, params, limit):
    return _audit_scan(conn, where, params, "topic",
                       lambda r: r["topic"] if _TOPIC_MARKER.search(r["topic"] or "") else None, limit)


def _check_country_shape(conn, where, params, limit):
    return _audit_scan(conn, where, params, "country",
                       lambda r: r["country"] if r["country"] and not looks_like_country(r["country"]) else None, limit)


def _check_title_credit(conn, where, params, limit):
    return _audit_scan(conn, where, params, "clean_title",
                       lambda r: r["clean_title"] if _TITLE_CREDIT.search(r["clean_title"] or "") else None, limit)


def _check_episodic_unparsed(conn, where, params, limit):
    def pred(r):
        looks = _EPISODIC.search(r["title"] or "")
        return r["title"] if looks and r["season"] is None and r["episode"] is None else None
    return _audit_scan(conn, where, params, "title, season, episode", pred, limit)


def _check_case_variants(conn, where, params, limit):
    """Topics within a sender that collapse to one casefold but differ in raw form."""
    rows = conn.execute("SELECT sender, topic, count(*) c FROM mediathek "
                        + where + " GROUP BY sender, topic", params)
    bysender = {}
    for r in rows:
        if not r["topic"]:
            continue
        bysender.setdefault(r["sender"], {}).setdefault(
            r["topic"].casefold(), []).append((r["topic"], r["c"]))
    out = {}
    for sender, groups in bysender.items():
        multi = [v for v in groups.values() if len({t for t, _ in v}) > 1]
        if not multi:
            continue
        out[sender] = {
            "count": sum(c for v in multi for _, c in v),
            "examples": ["/".join(sorted(t for t, _ in v)) for v in multi[:limit]]}
    return out


# Check name -> implementation. Names are the public --check vocabulary.
AUDIT_CHECKS = {
    "bare-topic":        _check_bare_topic,
    "case-variants":     _check_case_variants,
    "topic-pipe":        _check_topic_pipe,
    "topic-marker":      _check_topic_marker,
    "country-shape":     _check_country_shape,
    "title-credit":      _check_title_credit,
    "episodic-unparsed": _check_episodic_unparsed,
}


def enrich_audit(conn, senders=None, checks=None, limit=5) -> dict:
    """Run the findings checks (default all) and return {sender: {check: {count,
    examples}}}. country-shape/title-credit/episodic-unparsed only fire on
    already-enriched rows. Read-only -> no transaction."""
    names = checks or list(AUDIT_CHECKS)
    unknown = [c for c in names if c not in AUDIT_CHECKS]
    if unknown:
        raise ValueError(f"unknown audit check(s): {', '.join(unknown)} "
                         f"(known: {', '.join(AUDIT_CHECKS)})")
    where, params = _sender_clause(senders)
    out = {}
    for name in names:
        for sender, res in AUDIT_CHECKS[name](conn, where, params, limit).items():
            out.setdefault(sender, {})[name] = res
    return out


def _print_audit(result):
    """Findings per sender/check to stdout: count + the collected examples."""
    print("enrich audit (findings: count + examples)")
    for sender in sorted(result):
        print(f"{sender}:")
        for check in sorted(result[sender]):
            res = result[sender][check]
            print(f'  {check:18}{res["count"]:>8}')
            for ex in res["examples"]:
                print(f"      {ex!r}")


def _enrich_audit_cmd(conn, args) -> dict:
    result = enrich_audit(conn, senders=_split_csv(args.sender),
                            checks=_split_csv(args.check), limit=args.limit)
    if args.json:
        return {"senders": result}
    _print_audit(result)
    return {}


# -- enrich show / dist (read-only inspection) ------------------------------
# show dumps the enrich columns of matching rows; dist tallies one field's
# value distribution. Both validate field names against the live mediathek
# columns so a name can be interpolated into SQL safely (values stay bound).

_SHOW_COLS = ["sender", "topic", "title", "clean_title", "series_name",
              "category", "country", "year", "season", "episode", "flags",
              "enrich_confidence"]


def _valid_fields(conn) -> set:
    """The mediathek column names (for validating --field / filter fields)."""
    return {r["name"] for r in conn.execute("PRAGMA table_info(mediathek)")}


def _check_field(field, valid):
    if field not in valid:
        raise ValueError(f"unknown field: {field}")
    return field


def _build_show_where(conn, args):
    """Turn the structured filter flags into a parameterized WHERE clause. Field
    names are validated against the table; values are bound, never interpolated."""
    valid = _valid_fields(conn)
    conds, params = [], []
    senders = _split_csv(args.sender)
    if senders:
        conds.append(f"sender IN ({','.join('?' * len(senders))})")
        params += senders
    for field, pattern in args.like or []:
        conds.append(f"{_check_field(field, valid)} LIKE ?"); params.append(pattern)
    for field, value in args.eq or []:
        conds.append(f"{_check_field(field, valid)} = ?"); params.append(value)
    for field in args.null or []:
        conds.append(f"{_check_field(field, valid)} IS NULL")
    for field in args.not_null or []:
        conds.append(f"{_check_field(field, valid)} IS NOT NULL")
    if args.min_conf is not None:
        conds.append("enrich_confidence >= ?"); params.append(args.min_conf)
    if args.max_conf is not None:
        conds.append("enrich_confidence <= ?"); params.append(args.max_conf)
    return ("WHERE " + " AND ".join(conds) if conds else ""), params


def enrich_show(conn, where_sql, params, limit) -> list:
    """Rows matching where_sql, as dicts over mediathek_id + the inspection
    columns. Read-only -> no transaction."""
    rows = conn.execute(
        "SELECT mediathek_id, " + ", ".join(_SHOW_COLS) + " FROM mediathek "
        + where_sql + " LIMIT ?", list(params) + [limit])
    return [dict(r) for r in rows]


def _print_show(rows):
    """One compact two-line block per row to stdout."""
    print(f"{len(rows)} row(s)")
    for r in rows:
        print(f'[{r["sender"]}] {r["clean_title"]!r}  cat={r["category"]!r} '
              f'country={r["country"]!r} year={r["year"]} '
              f'S/E={r["season"]}/{r["episode"]} conf={r["enrich_confidence"]}')
        print(f'      topic={r["topic"]!r} title={r["title"]!r} '
              f'series={r["series_name"]!r} flags={r["flags"]!r}')


def _enrich_show_cmd(conn, args) -> dict:
    where, params = _build_show_where(conn, args)
    rows = enrich_show(conn, where, params, args.limit)
    if args.json:
        return {"rows": rows}
    _print_show(rows)
    return {}


def enrich_dist(conn, field, senders=None, limit=30) -> list:
    """Top-N (value, count) of one field, descending by count. `field` is
    validated against the table columns. Read-only -> no transaction."""
    field = _check_field(field, _valid_fields(conn))
    where, params = _sender_clause(senders)
    rows = conn.execute(
        f"SELECT {field} AS v, count(*) AS c FROM mediathek " + where
        + f" GROUP BY {field} ORDER BY c DESC, v LIMIT ?", list(params) + [limit])
    return [(r["v"], r["c"]) for r in rows]


def _print_dist(field, dist):
    """Value distribution to stdout, one aligned line per value."""
    print(f"distribution of {field} (count: value)")
    for value, count in dist:
        print(f"   {count:8}  {value!r}")


def _enrich_dist_cmd(conn, args) -> dict:
    dist = enrich_dist(conn, args.field, senders=_split_csv(args.sender),
                         limit=args.limit)
    if args.json:
        return {"field": args.field, "values": dist}
    _print_dist(args.field, dist)
    return {}


# -- match --------------------------------------------------------------------
# Wish-first: resolve a TMDB id to its title variants/year/runtime, then tag the
# matching movie rows with tmdb_id + match_confidence. `run` writes; `show` is a
# read-only score explainer for tuning. Heavy lifting lives in theke.match.

def cmd_match(conn, cfg, args: argparse.Namespace) -> dict:
    """Dispatch a match action: `run` tags rows, `show` explains scores,
    `reset` undoes a match. reset is a pure DB op (no TMDB key/type needed)."""
    if args.match_cmd == "reset":
        return _match_reset(conn, args)
    if not cfg.tmdb_api_key:
        raise ConfigError("no TMDB API key configured (set tmdb_api_key)")
    if args.type == "series" and (args.season is None or args.episode is None):
        raise ValueError("--type series requires --season and --episode")
    match args.match_cmd:
        case "run":  return _match_run(conn, cfg, args)
        case "show": return _match_show(conn, cfg, args)
        case _: raise DbError(f"unhandled match action: {args.match_cmd}")


def _match_reset(conn, args) -> dict:
    """Undo match: take matched rows (status '2') back to enriched ('1').
    Clears tmdb_id + match_confidence unless --status-only."""
    sets = "status='1'" if args.status_only else f"status='1', {_MATCH_CLEAR}"
    return _reset(conn, sets, "status='2'")


def _match_resolve(conn, cfg, args, min_conf) -> tuple:
    """Resolve the TMDB id and find candidate rows, for a movie (title/year/
    runtime, year within --year-tolerance) or a series episode (series-name +
    exact season/episode). Returns (meta, result_head, matches); the head
    carries the episode title + series name for a series, the film title for a
    movie."""
    if args.type == "series":
        meta = tmdb_tv(cfg, args.tmdb, args.season, args.episode)
        matches = find_episode_matches(conn, meta, min_conf)
        head = {"tmdb_id": meta["tmdb_id"], "title": meta["episode_name"],
                "series": meta["series_title"]}
    else:
        tol = cfg.match_year_tolerance if args.year_tolerance is None else args.year_tolerance
        meta = tmdb_movie(cfg, args.tmdb)
        matches = find_matches(conn, meta, min_conf, year_tolerance=tol)
        head = {"tmdb_id": meta["tmdb_id"], "title": meta["title"]}
    return meta, head, matches


def _match_run(conn, cfg, args) -> dict:
    """Resolve the TMDB id and write tmdb_id + match_confidence onto matching
    rows. A pass-1 hit on an Arte sender triggers a second pass that links the
    film's other-language Arte variants by their shared video-id (they inherit
    the hit's confidence). An existing different tmdb_id is preserved (logged,
    not clobbered); --dry-run computes but writes nothing. `candidates` and
    `arte_linked` report what the two passes found (shown even with --dry-run);
    `written` is what was actually tagged."""
    min_conf = cfg.match_min_confidence if args.min_conf is None else args.min_conf
    meta, head, matches = _match_resolve(conn, cfg, args, min_conf)
    anchors = arte_anchor_ids(conn, matches)
    links = find_arte_links(conn, anchors, {m["mediathek_id"] for m in matches})
    written = 0
    if not args.dry_run:
        conn.execute("BEGIN")
        try:
            for m in matches + links:
                cur = conn.execute("SELECT tmdb_id FROM mediathek WHERE mediathek_id=?",
                                   (m["mediathek_id"],)).fetchone()["tmdb_id"]
                if cur and cur != meta["tmdb_id"]:
                    log.warning("skip %s: already tmdb_id %s", m["mediathek_id"], cur)
                    continue
                conn.execute("UPDATE mediathek SET tmdb_id=?, match_confidence=?, "
                             "status='2' WHERE mediathek_id=?",
                             (meta["tmdb_id"], m["confidence"], m["mediathek_id"]))
                written += 1
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return {**head, "candidates": len(matches), "written": written,
            "arte_linked": len(links)}


def _match_show(conn, cfg, args) -> dict:
    """Read-only: list candidate rows with their score breakdown (default lists
    everything not rejected, for tuning)."""
    min_conf = 0.0 if args.min_conf is None else args.min_conf
    meta, head, matches = _match_resolve(conn, cfg, args, min_conf)
    matches = matches[:args.limit]
    if args.json:
        return {**head, "matches": matches}
    _print_matches(meta, matches, args.type)
    return {}


def _print_matches(meta, matches, type_):
    """One header line + one line per candidate to stdout (the result)."""
    if type_ == "series":
        print(f'{meta["episode_name"]!r} of {meta["series_title"]!r} '
              f'(tmdb {meta["tmdb_id"]}, {len(matches)} candidate(s))')
        for m in matches:
            print(f'  {m["confidence"]:.3f}  {m["clean_title"]!r}  '
                  f'series={m["series_sim"]} ep={m["episode_title_sim"]} '
                  f'dRun={m["runtime_delta"]}')
        return
    print(f'{meta["title"]!r} (tmdb {meta["tmdb_id"]}, year {meta["year"]}, '
          f'{len(matches)} candidate(s))')
    for m in matches:
        print(f'  {m["confidence"]:.3f}  {m["clean_title"]!r}  '
              f'sim={m["title_sim"]} dY={m["year_delta"]} dRun={m["runtime_delta"]}')


# -- queue (phase 5: staging + review) ---------------------------------------
# Status chars, chosen ASCII-ascending in lifecycle order so a plain sort tracks
# progress: '0' proposed, 'A' approved, 'B' busy (downloading), 'C' cancelled,
# 'D' done, 'F' failed. DB-only stage; nothing here touches the filesystem.

QUEUE_STATUS = {"proposed": "0", "approved": "A", "busy": "B",
                "cancelled": "C", "done": "D", "failed": "F"}
QUEUE_ACTIVE = ("0", "A", "B")   # proposed/approved/busy -- not yet terminal


def _now() -> str:
    """Current UTC time as an ISO-8601 'Z' string (queue timestamps)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_queue(conn, cfg, args: argparse.Namespace) -> dict:
    """Dispatch a queue action. `add` stages downloads (the only writer of new
    rows); list/approve/cancel manage the review queue."""
    match args.queue_cmd:
        case "add":     return _queue_add(conn, cfg, args)
        case "list":    return _queue_list(conn, args)
        case "approve": return _queue_set_status(conn, args,
                            tuple(QUEUE_STATUS.values()) if args.force else ("0",),
                            "A", "approved")
        case "cancel":  return _queue_set_status(conn, args, QUEUE_ACTIVE, "C", "cancelled")
        case "delete":  return _queue_delete(conn, args)
        case "download": return _queue_download(conn, cfg, args)
        case _: raise DbError(f"unhandled queue action: {args.queue_cmd}")


def _queue_add(conn, cfg, args) -> dict:
    """Stage downloads into the queue. `--tmdb` resolves a matched film, dedups
    its many mediathek rows (theke.queue.select_downloads) and queues the minimal
    set; `--mediathek-id` queues one row directly ('AV'). New entries are
    'proposed' unless queue_auto_approve is set. A mediathek_id already queued in
    an active state (proposed/approved/busy) is skipped; a terminal one does not
    block a re-queue. `deduplicated` counts the source rows collapsed or filtered.
    Per-row columns (url/path/...) are CLI-overridable via _queue_overrides."""
    if not args.tmdb and not args.mediathek_id:
        raise ValueError("queue add needs --tmdb or --mediathek-id")
    status = QUEUE_STATUS["approved"] if cfg.queue_auto_approve else QUEUE_STATUS["proposed"]
    overrides = _queue_overrides(args)
    totals = {"queued": 0, "skipped": 0, "deduplicated": 0}
    conn.execute("BEGIN")
    try:
        for tid in args.tmdb or []:
            _queue_add_tmdb(conn, cfg, str(tid), status, overrides, totals)
        for mid in args.mediathek_id or []:
            _queue_add_mediathek(conn, cfg, mid, status, overrides, totals)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return totals


_QUEUE_OVERRIDE_COLS = ("language", "resolution", "remux", "url",
                        "path", "url_subtitle")


def _queue_overrides(args) -> dict:
    """The queue columns explicitly overridden on the CLI (None = not given);
    applied verbatim over the computed values, so a manual escape hatch."""
    return {c: getattr(args, c) for c in _QUEUE_OVERRIDE_COLS
            if getattr(args, c, None) is not None}


def _queue_add_tmdb(conn, cfg, tmdb_id, status, overrides, totals):
    """Queue the deduplicated download set of one matched film. The first pick is
    the anchor (best resolution, primary path); the rest carry a language infix in
    their path."""
    if not cfg.tmdb_api_key:
        raise ConfigError("no TMDB API key configured (set tmdb_api_key)")
    meta = tmdb_movie(cfg, tmdb_id)
    fields = _path_fields(meta["title"], meta["year"])
    rows = {r["mediathek_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM mediathek WHERE status='2' AND tmdb_id=?", (tmdb_id,))}
    picks = select_downloads(list(rows.values()), cfg.languages, meta["original_language"])
    totals["deduplicated"] += len(rows) - len(picks)
    for i, p in enumerate(picks):
        src = rows[p["mediathek_id"]]
        row = _queue_row(status, p["mediathek_id"], tmdb_id, p["language"],
                         p["resolution"], p["remux"], src,
                         _library_path(cfg, fields, p["language"], p["remux"], i == 0),
                         meta["year"])
        _queue_insert(conn, row, overrides, totals)


def _queue_add_mediathek(conn, cfg, mediathek_id, status, overrides, totals):
    """Queue one mediathek row directly (manual pick, no dedup, full 'AV', primary
    path). Uses the TMDB title/year when the row is matched and a key is set, else
    its enriched clean_title/series_name/season/episode."""
    r = conn.execute("SELECT * FROM mediathek WHERE mediathek_id=?",
                     (mediathek_id,)).fetchone()
    if r is None:
        raise ValueError(f"no mediathek row {mediathek_id!r}")
    r = dict(r)
    tmdb_id, language = r["tmdb_id"] or "", r["language"]
    if tmdb_id and cfg.tmdb_api_key:
        meta = tmdb_movie(cfg, tmdb_id)
        title, year = meta["title"], meta["year"]
        if language == "ov":
            language = meta["original_language"]
    else:
        title, year = r["clean_title"], r["year"]
    fields = _path_fields(title, year, r["series_name"], r["season"], r["episode"])
    resolution = resolution_of(r)
    row = _queue_row(status, mediathek_id, tmdb_id, language, resolution,
                     "AV", r, _library_path(cfg, fields, language, "AV", True), year)
    _queue_insert(conn, row, overrides, totals)


_RES_URL = {"HD": "url_video_hd", "SD": "url_video", "LQ": "url_video_small"}


def _media_url(row, resolution) -> str:
    """Source media url for the chosen resolution, falling back to any present."""
    return (row.get(_RES_URL[resolution]) or row.get("url_video")
            or row.get("url_video_small") or "")


def _queue_row(status, mediathek_id, tmdb_id, language, resolution,
               remux, src, path, year) -> dict:
    """Assemble one queue column dict from a pick + its mediathek source row."""
    return {"status": status, "mediathek_id": mediathek_id, "tmdb_id": tmdb_id,
            "language": language, "resolution": resolution,
            "remux": remux, "url": _media_url(src, resolution),
            "url_subtitle": src.get("url_subtitle") or "", "path": path,
            "year": year}


def _path_fields(title, year, series=None, season=None, episode=None) -> dict:
    """The placeholder values for the library_path template. Series/Season/Episode
    are reserved for the (later) series layout; for movies they render empty."""
    return {"Title": title, "Year": year, "Series": series,
            "Season": season, "Episode": episode}


_RESERVED_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_component(value) -> str:
    """Make a rendered template value safe as a path component on the target FS:
    replace reserved characters (Windows + POSIX) with '-' and strip trailing dots
    and spaces (illegal/awkward on Windows)."""
    return _RESERVED_PATH_CHARS.sub("-", value).rstrip(". ")


def _render_template(template, fields, sanitize=False) -> str:
    """Substitute {Placeholder} (case-insensitive) from `fields`. An optional
    ':N' zero-pads an integer to N digits ({Season:2} -> '03'). None/empty values
    render empty (never 'None'); an unknown placeholder is an error (typo guard).
    With sanitize, each substituted value is made path-safe (the template's own
    separators are untouched)."""
    lookup = {k.casefold(): v for k, v in fields.items()}

    def repl(match):
        key, width = match.group(1).casefold(), match.group(2)
        if key not in lookup:
            raise KeyError(f"unknown template placeholder: {match.group(1)}")
        value = lookup[key]
        if value is None or value == "":
            return ""
        rendered = f"{int(value):0{int(width)}d}" if width else str(value)
        return _sanitize_component(rendered) if sanitize else rendered

    return re.sub(r"\{([A-Za-z_]+)(?::(\d+))?\}", repl, template)


def _library_path(cfg, fields, language, remux, primary) -> str:
    """Resolve the full library destination for one queue row: render the
    configured template, drop its extension and re-apply video_ext (or audio_ext
    for audio-only rows). Non-anchor picks get a '.<language>' infix so several
    language variants of one film coexist in the same folder."""
    stem = os.path.splitext(_render_template(cfg.library_path, fields, sanitize=True))[0]
    ext = cfg.audio_ext if remux == "A" else cfg.video_ext
    infix = "" if primary else f".{language}"
    return f"{stem}{infix}.{ext}"


def _queue_insert(conn, row, overrides, totals):
    """Insert one queue column dict unless its mediathek_id is already queued
    active. CLI overrides replace computed columns last; the timestamps are set
    here so an override can never forge them."""
    row = {**row, **overrides}
    actives = "(" + ",".join("?" * len(QUEUE_ACTIVE)) + ")"
    if conn.execute(f"SELECT 1 FROM queue WHERE mediathek_id=? AND status IN "
                    f"{actives}", (row["mediathek_id"], *QUEUE_ACTIVE)).fetchone():
        totals["skipped"] += 1
        return
    ts = _now()
    cols = ["status", "mediathek_id", "tmdb_id", "language", "resolution",
            "remux", "url", "url_subtitle", "path", "year", "created_at", "updated_at"]
    row = {**row, "created_at": ts, "updated_at": ts}
    conn.execute(f"INSERT INTO queue ({', '.join(cols)}) VALUES "
                 f"({', '.join(':' + c for c in cols)})", row)
    totals["queued"] += 1


def _queue_list(conn, args) -> dict:
    """Read-only listing, optionally filtered by lifecycle state (--status name),
    ordered by creation. --json returns the rows; otherwise prints a table."""
    sql = "SELECT * FROM queue"
    params = ()
    if args.status:
        sql += " WHERE status=?"
        params = (QUEUE_STATUS[args.status],)
    sql += " ORDER BY created_at, id"
    rows = [dict(r) for r in conn.execute(sql, params)]
    if args.json:
        return {"queue": rows, "count": len(rows)}
    _print_queue(rows)
    return {}


def _queue_set_status(conn, args, from_states, to, key) -> dict:
    """Move queue rows to a new lifecycle state: the given ids or, with --all,
    every row currently in `from_states`. Only rows in `from_states` are touched.
    Returns {key: count}."""
    if args.all and args.ids:
        raise ValueError("give queue ids or --all, not both")
    if not args.all and not args.ids:
        raise ValueError("give queue ids or --all")
    froms = "(" + ",".join("?" * len(from_states)) + ")"
    sql = f"UPDATE queue SET status=?, updated_at=? WHERE status IN {froms}"
    params = [to, _now(), *from_states]
    if not args.all:
        sql += " AND id IN (" + ",".join("?" * len(args.ids)) + ")"
        params += args.ids
    conn.execute("BEGIN")
    try:
        n = conn.execute(sql, params).rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {key: n}


def _queue_delete(conn, args) -> dict:
    """Hard-delete queue entries by exactly one selector: given ids, every entry
    (--all), or terminal states (--cancelled/--done/--failed, combinable).
    Returns {deleted: count}."""
    states = [QUEUE_STATUS[name] for name, on in
              (("cancelled", args.cancelled), ("done", args.done),
               ("failed", args.failed)) if on]
    if sum((bool(args.ids), args.all, bool(states))) != 1:
        raise ValueError("give queue ids, status flags "
                         "(--cancelled/--done/--failed), or --all")
    if args.all:
        sql, params = "DELETE FROM queue", ()
    elif states:
        sql = "DELETE FROM queue WHERE status IN (" + ",".join("?" * len(states)) + ")"
        params = states
    else:
        sql = "DELETE FROM queue WHERE id IN (" + ",".join("?" * len(args.ids)) + ")"
        params = args.ids
    conn.execute("BEGIN")
    try:
        n = conn.execute(sql, params).rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"deleted": n}


# -- queue download (phases 6-8: chained off the self-contained queue row) ----
# The gated stage: for approved rows, run download -> remux -> move using only
# the queue row (url/remux/language/url_subtitle/path). Each row downloads and
# remuxes under a unique temp prefix (parallel-safe), moves to its stored path,
# then drops its temp files. A failing row is marked 'failed' (with the error)
# and never aborts the batch. The file primitives are the theke.files seams.

# ffmpeg wants an ISO 639-2/B audio tag; map the common 2-letter codes, pass the
# rest through (an unknown code is harmless metadata, not a failure).
_LANG3 = {"de": "deu", "en": "eng", "fr": "fra", "es": "spa", "it": "ita",
          "nl": "nld", "pl": "pol", "ru": "rus", "tr": "tur", "ar": "ara",
          "pt": "por"}


def _lang_tag(language):
    """ISO 639-2 audio tag from the queue's language code (None when unset)."""
    return _LANG3.get(language, language) if language else None


def _url_ext(url) -> str:
    """File extension of a URL's path ('' when it carries none)."""
    return os.path.splitext(urllib.parse.urlsplit(url).path)[1]


def _subtitle_lang(row) -> str:
    """Sidecar language tag (Jellyfin convention): the queue language when it is a
    2-letter code, else 'de' (covers '', 'ov' and original-language fallbacks)."""
    lang = (row["language"] or "").strip()
    return lang if len(lang) == 2 and lang.isalpha() else "de"


def _temp_base(tmpdir, row) -> str:
    """Speaking, per-row-unique temp prefix 'theke_{id}_{path stem}' under tmpdir;
    the row id keeps concurrent rows from colliding."""
    stem = os.path.splitext(os.path.basename(row["path"]))[0]
    return os.path.join(tmpdir, f"theke_{row['id']}_{stem}")


def _queue_download(conn, cfg, args) -> dict:
    """Run download -> remux -> move for the given approved ids, or every
    approved row with --all. Only rows in 'approved' are eligible (the gate);
    re-approve a failed row to retry it."""
    if args.all and args.ids:
        raise ValueError("give queue ids or --all, not both")
    if not args.all and not args.ids:
        raise ValueError("give queue ids or --all")
    sql = "SELECT * FROM queue WHERE status='A'"
    params = []
    if not args.all:
        sql += " AND id IN (" + ",".join("?" * len(args.ids)) + ")"
        params = list(args.ids)
    sql += " ORDER BY created_at, id"
    rows = [dict(r) for r in conn.execute(sql, params)]
    totals = {"downloaded": 0, "failed": 0}
    for row in rows:
        _download_entry(conn, cfg, row, args.force, totals)
    return totals


def _download_entry(conn, cfg, row, force, totals):
    """Process one approved row end to end under a unique temp prefix. Subtitles
    are best-effort (a missing sidecar never fails the film)."""
    _queue_status(conn, row["id"], QUEUE_STATUS["busy"], None)
    tmpdir = cfg.temp_path or tempfile.gettempdir()
    base = _temp_base(tmpdir, row)
    try:
        os.makedirs(tmpdir, exist_ok=True)
        src = base + ".src" + _url_ext(row["url"])
        _fetch(cfg, row["url"], src)
        muxed = base + ".mux" + (os.path.splitext(row["path"])[1] or "." + cfg.video_ext)
        run_remux(cfg.ffmpeg_path, src, row["remux"], muxed, _lang_tag(row["language"]))
        move_file(muxed, row["path"], force)
        if row["url_subtitle"]:
            try:
                _download_subtitle(cfg, row, base, force)
            except Exception as exc:
                log.warning("queue %s subtitle skipped: %s", row["id"], exc)
        _cleanup(base)
        _queue_status(conn, row["id"], QUEUE_STATUS["done"], None)
        _library_record(conn, row["tmdb_id"], os.path.dirname(row["path"]), row["year"])
        totals["downloaded"] += 1
        log.info("queue %s done -> %s", row["id"], row["path"])
    except Exception as exc:
        _cleanup(base)
        _queue_status(conn, row["id"], QUEUE_STATUS["failed"], str(exc))
        totals["failed"] += 1
        log.warning("queue %s failed: %s", row["id"], exc)


def _fetch(cfg, url, out):
    """Download a media URL to `out`, routing HLS playlists to the segment path."""
    if is_hls(url):
        download_hls(url, out, cfg.download_retries, cfg.ffmpeg_path, cfg.download_timeout)
    else:
        download_file(url, out, cfg.download_retries, cfg.download_timeout,
                      cfg.download_stall_timeout)


def _download_subtitle(cfg, row, base, force):
    """Fetch the subtitle and write converted sidecars next to the film: one
    '<stem>.<lang>.<ext>' per configured format. ffmpeg-free (TTML/EBU-TT and
    WebVTT in, SRT/ASS/TTML out). Unrecognised input (e.g. an HTML page) is
    skipped without writing a sidecar."""
    tmp = base + ".sub" + _url_ext(row["url_subtitle"])
    download_file(row["url_subtitle"], tmp, cfg.download_retries, cfg.download_timeout)
    with open(tmp, encoding="utf-8-sig") as fh:
        outputs = subtitle.convert(fh.read(), cfg.subtitle_formats)
    if not outputs:
        log.warning("queue %s subtitle: unrecognised format, no sidecar", row["id"])
        return
    stem = os.path.splitext(row["path"])[0]
    lang = _subtitle_lang(row)
    for fmt, data in outputs.items():
        out_tmp = base + ".out" + subtitle.SUBTITLE_EXT[fmt]
        with open(out_tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
        move_file(out_tmp, f"{stem}.{lang}{subtitle.SUBTITLE_EXT[fmt]}", force)


def _cleanup(base):
    """Remove every temp artefact under the unique prefix (the '.src', '.mux' and
    subtitle temps plus any leftover '.part'/'.segments'). Best-effort."""
    for path in glob.glob(glob.escape(base) + "*"):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except OSError:
            pass


def _queue_status(conn, queue_id, status, error):
    """Set one queue row's status (+error) in its own transaction, so the long
    filesystem work runs outside any open transaction."""
    conn.execute("BEGIN")
    try:
        conn.execute("UPDATE queue SET status=?, error=?, updated_at=? WHERE id=?",
                     (status, error, _now(), queue_id))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _remux_cols(remux) -> str:
    """The remux flags as two fixed columns ('A' left, 'V' right) so entries align
    under each other: 'A' -> 'A ', 'V' -> ' V', 'AV' -> 'AV'."""
    return ("A" if "A" in remux else " ") + ("V" if "V" in remux else " ")


def _print_queue(rows):
    """One header line + one line per entry to stdout (the result)."""
    print(f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}")
    for r in rows:
        print(f'  [{r["id"]}] {r["status"]} {r["resolution"]} {_remux_cols(r["remux"])} '
              f'{r["language"]} {r["path"]!r}')


# -- library / wishlist (phase 9) --------------------------------------------
# The library table is wishlist + library record in one, keyed by tmdb_id.
# Status chars mirror the other tables: 'W' wish, 'M' missing episode, 'L' in
# library. cmd_library manages wishes; a finished queue download records its
# tmdb_id as 'L' via _library_record (called from _download_entry).

LIBRARY_STATUS = {"wish": "W", "missing": "M", "library": "L"}


def cmd_library(conn, cfg, args: argparse.Namespace) -> dict:
    """Dispatch a library action: `add` stages wishes, list/remove manage them."""
    match args.library_cmd:
        case "add":    return _library_add(conn, cfg, args)
        case "list":   return _library_list(conn, args)
        case "remove": return _library_remove(conn, args)
        case _: raise DbError(f"unhandled library action: {args.library_cmd}")


def _wish_meta(cfg, tmdb_id) -> tuple:
    """Best-effort (title, year) for the wish label; ('', None) with no key or on
    failure (the label/year are convenience only, never load-bearing)."""
    if not cfg.tmdb_api_key:
        return "", None
    try:
        meta = tmdb_movie(cfg, tmdb_id)
        return meta["title"] or "", meta["year"]
    except Exception as exc:
        log.warning("library add %s: title lookup failed: %s", tmdb_id, exc)
        return "", None


def _resolve_title(cfg, args) -> tuple:
    """Resolve --title (+ tolerant --year) to one (tmdb_id, title, year) via TMDB
    search. The year may be off by --year-tolerance years (default: config
    match_year_tolerance, as in match). Raises when unresolvable."""
    if not cfg.tmdb_api_key:
        raise ConfigError("--title lookup needs a TMDB API key (set tmdb_api_key)")
    tol = cfg.match_year_tolerance if args.year_tolerance is None else args.year_tolerance
    cand = pick_by_year(tmdb_search(cfg, args.title), args.year, tol)
    if cand is None:
        raise ValueError(f"no TMDB movie for {args.title!r} "
                         f"(year {args.year}, +-{tol})")
    return cand["tmdb_id"], cand["title"], cand["year"]


def _library_add(conn, cfg, args) -> dict:
    """Add wishes as status 'W'. Each wish is resolved to a tmdb_id: given
    directly via --tmdb, or by a title/year lookup via --title. Idempotent: an
    existing tmdb_id is left untouched (counted as skipped, never reset from 'L'
    back to 'W')."""
    if args.title and args.tmdb:
        raise ValueError("give --tmdb or --title, not both")
    if args.title:
        wishes = [_resolve_title(cfg, args)]
    elif args.tmdb:
        wishes = [(str(t), *_wish_meta(cfg, t)) for t in args.tmdb]
    else:
        raise ValueError("library add needs --tmdb or --title")
    totals = {"added": 0, "skipped": 0}
    conn.execute("BEGIN")
    try:
        for tid, title, year in wishes:
            ts = _now()
            cur = conn.execute(
                "INSERT INTO library (tmdb_id, status, title, year, created_at, updated_at) "
                "VALUES (?, 'W', ?, ?, ?, ?) ON CONFLICT(tmdb_id) DO NOTHING",
                (tid, title, year, ts, ts))
            totals["added" if cur.rowcount else "skipped"] += 1
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return totals


def _library_list(conn, args) -> dict:
    """Read-only listing, optionally filtered by lifecycle state (--status name),
    ordered by creation. --json returns the rows; otherwise prints a table."""
    sql = "SELECT * FROM library"
    params = ()
    if args.status:
        sql += " WHERE status=?"
        params = (LIBRARY_STATUS[args.status],)
    sql += " ORDER BY created_at, tmdb_id"
    rows = [dict(r) for r in conn.execute(sql, params)]
    if args.json:
        return {"library": rows, "count": len(rows)}
    _print_library(rows)
    return {}


def _library_remove(conn, args) -> dict:
    """Delete library entries by exactly one selector: given tmdb_ids or --all."""
    if args.all and args.tmdb:
        raise ValueError("give --tmdb or --all, not both")
    if not args.all and not args.tmdb:
        raise ValueError("give --tmdb or --all")
    if args.all:
        sql, params = "DELETE FROM library", ()
    else:
        ids = [str(t) for t in args.tmdb]
        sql = "DELETE FROM library WHERE tmdb_id IN (" + ",".join("?" * len(ids)) + ")"
        params = ids
    conn.execute("BEGIN")
    try:
        n = conn.execute(sql, params).rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"removed": n}


def _print_library(rows):
    """One header line + one line per entry to stdout (the result)."""
    print(f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}")
    for r in rows:
        print(f'  [{r["status"]}] {r["tmdb_id"]:>8}  {r["title"]!r}')


def _library_record(conn, tmdb_id, path, year):
    """Record a downloaded film in the library as 'L': flip an existing wish or
    insert a fresh row, recording the library folder it landed in and its release
    year (carried on the queue row). On a flip a year already captured at wish time
    is kept (COALESCE). Own transaction (like _queue_status), so it stays outside
    the long filesystem work. No-op for a queue row without a tmdb_id."""
    if not tmdb_id:
        return
    ts = _now()
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO library (tmdb_id, status, title, path, year, created_at, updated_at) "
            "VALUES (?, 'L', '', ?, ?, ?, ?) ON CONFLICT(tmdb_id) DO UPDATE SET "
            "status='L', path=excluded.path, "
            "year=COALESCE(library.year, excluded.year), updated_at=excluded.updated_at",
            (tmdb_id, path, year, ts, ts))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# -- update (phase 9: the wishlist automation orchestrator) -------------------
# One unattended pass over the whole pipeline: refresh + enrich the mirror, then
# for every open wish ('W') resolve its TMDB id, tag the matching rows and queue
# the deduplicated download set. With queue_auto_approve the approved entries are
# downloaded straight away (each finished wish is recorded as 'L' by the download
# stage); otherwise the run stops at the approval gate. A single failing wish
# never aborts the pass. Reuses the existing stage handlers verbatim.

def _ns(**kw) -> argparse.Namespace:
    """A throwaway Namespace to drive a reused stage handler from update (each
    reads only a few attributes)."""
    return argparse.Namespace(**kw)


def cmd_update(conn, cfg, args: argparse.Namespace) -> dict:
    """Run fetch -> enrich -> (match + queue) per wish -> download (when
    auto-approved). Returns one aggregated summary."""
    fetch = cmd_fetch(conn, cfg, _ns(force=False))
    enriched = _enrich_run(conn, cfg, _ns(force=False))
    status = QUEUE_STATUS["approved"] if cfg.queue_auto_approve else QUEUE_STATUS["proposed"]
    totals = {"queued": 0, "skipped": 0, "deduplicated": 0}
    wishes = [r["tmdb_id"] for r in
              conn.execute("SELECT tmdb_id FROM library WHERE status='W'")]
    failed = 0
    for tid in wishes:
        try:
            _match_run(conn, cfg, _ns(tmdb=tid, type="movie", season=None,
                                      episode=None, dry_run=False, min_conf=None,
                                      year_tolerance=None))
            _queue_add_tmdb(conn, cfg, str(tid), status, {}, totals)
        except Exception as exc:
            failed += 1
            log.warning("update wish %s failed: %s", tid, exc)
    downloaded = 0
    if cfg.queue_auto_approve:
        downloaded = _queue_download(conn, cfg, _ns(all=True, ids=[], force=False))["downloaded"]
    return {"fetch": fetch.get("action"), "enriched": enriched["enriched"],
            "wishes": len(wishes), "failed": failed, "downloaded": downloaded,
            **totals}


# -- cli ----------------------------------------------------------------------
# Stable grammar and exit codes: the GUI drives the CLI and parses the --json
# output (exactly one JSON object on stdout per call).

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3


def cmd_config(cfg) -> dict:
    """Show the effective configuration (after precedence resolution)."""
    return dataclasses.asdict(cfg)


# -- file (phases 6-8: download / remux / move) -------------------------------
# Thin handlers over theke.files; queue-independent, no DB. Filesystem work and
# the network/ffmpeg seams live in files.py.

def cmd_file(cfg, args: argparse.Namespace) -> dict:
    """Dispatch a file action: download / remux / move (each on explicit paths)."""
    match args.file_cmd:
        case "download":       return _file_download(cfg, args)
        case "remux":          return _file_remux(cfg, args)
        case "remux-subtitle": return _file_remux_subtitle(cfg, args)
        case "move":           return _file_move(cfg, args)
        case _: raise DbError(f"unhandled file action: {args.file_cmd}")


def _file_download(cfg, args) -> dict:
    retries = cfg.download_retries if args.retries is None else args.retries
    timeout = cfg.download_timeout if args.timeout is None else args.timeout
    if is_hls(args.url):
        action, nbytes, nsegs = download_hls(args.url, args.out, retries, cfg.ffmpeg_path, timeout)
        result = {"action": action, "out": args.out, "bytes": nbytes}
        if action == "hls":
            result["segments"] = nsegs
        return result
    nbytes = download_file(args.url, args.out, retries, timeout,
                           cfg.download_stall_timeout)
    return {"action": "download", "out": args.out, "bytes": nbytes}


def _file_remux(cfg, args) -> dict:
    if args.check_ffmpeg:
        return {"check_ffmpeg": True, "ffmpeg_path": cfg.ffmpeg_path,
                "version": check_ffmpeg(cfg.ffmpeg_path)}
    if not (args.in_path and args.mode and args.out):
        raise DbError("file remux requires --in, --mode and --out (or --check-ffmpeg)")
    run_remux(cfg.ffmpeg_path, args.in_path, args.mode, args.out, args.language)
    return {"remux": args.mode, "out": args.out}


def _file_remux_subtitle(cfg, args) -> dict:
    """Convert a subtitle file (TTML/EBU-TT or WebVTT) into '<base>.<lang>.<ext>'
    sidecars, one per requested format (--format, else config subtitle_formats).
    ffmpeg-free; an unrecognised input writes nothing."""
    formats = args.format.split(",") if args.format else cfg.subtitle_formats
    with open(args.in_path, encoding="utf-8-sig") as fh:
        outputs = subtitle.convert(fh.read(), formats)
    base = args.out or os.path.splitext(args.in_path)[0]
    written = []
    for fmt, data in outputs.items():
        dest = f"{base}.{args.language}{subtitle.SUBTITLE_EXT[fmt]}"
        if os.path.exists(dest) and not args.force:
            raise FileExistsError(f"destination exists (use --force): {dest}")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(data)
        written.append(dest)
    return {"subtitle": written}


def _file_move(cfg, args) -> dict:
    return {"moved": move_file(args.in_path, args.out, args.force)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="theke",       description="Self-hosted media manager for German public broadcasters")
    parser.add_argument("-c", "--config", metavar="PATH",      help=f"config file (default: {CONFIG_DEFAULT_PATH})")
    parser.add_argument("-d", "--db",     metavar="PATH",      help="database file (overrides db_path from config)")
    parser.add_argument("-j", "--json",   action="store_true", help="machine-readable output: one JSON object on stdout")
    parser.default_actions = {}  # command -> (default action, its subparsers action)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    sub.add_parser("config", help="show the effective configuration")

    fetch = sub.add_parser("fetch", help="refresh the film-list mirror (~30 s)", description="Refresh the film-list mirror; a full download and import takes about 30 seconds. Progress is printed to stderr.")
    fetch.add_argument("-f", "--force", action="store_true", help="always download the full list")

    enrich = sub.add_parser("enrich", help="extract metadata and inspect the result", description="Extract structured metadata from the free-text fields (run) and inspect the result with read-only reports. Progress is printed to stderr.")
    csub = enrich.add_subparsers(dest="enrich_cmd", required=True, metavar="action")
    crun = csub.add_parser("run",    help="enrich mirrored rows (default)",                      description="Extract structured metadata (title, series/season/episode, category, year, country, language, flags) into the enrich columns and flip status 0 -> 1.")
    crst = csub.add_parser("reset",  help="undo enrich: status 1/2 -> 0",                         description="Take enriched/matched rows (status '1'/'2') back to '0', as if freshly fetched. Clears the enrich + match columns unless --status-only.")
    crep = csub.add_parser("report", help="read-only per-sender coverage report",                description="Per-sender coverage of the enrich fields. Reads the stored columns by default; --live re-runs enrich() without writing.")
    caud = csub.add_parser("audit",  help="read-only findings scan for wrong/suspicious values", description="Scan for rows a heuristic visibly mishandled (coverage counts filled, not correct). country-shape/title-credit/episodic-unparsed only fire on already-enriched rows. Checks: "+ ", ".join(AUDIT_CHECKS) +".")
    csho = csub.add_parser("show",   help="read-only sample of rows with their enrich columns",  description="Dump the enrich columns of matching rows. Filters are ANDed; FIELD must be a mediathek column.")
    cdis = csub.add_parser("dist",   help="read-only value distribution of one field",           description="Top-N value frequencies of a single enrich field (or any mediathek column).")
    crun.add_argument("-f", "--force",         action="store_true",                                    help="re-enrich all rows, not just unenriched")
    crst.add_argument("-s", "--status-only",   action="store_true",                                    help="only flip status, keep the enrich + match columns")
    crep.add_argument("-s", "--sender",        metavar="X[,Y]",                                        help="restrict to these senders (comma-separated)")
    crep.add_argument("-m", "--min-rows",      metavar="N", type=int, default=REPORT_MIN_ROWS,         help=f"omit senders with fewer rows (default {REPORT_MIN_ROWS}; 0 shows all)")
    crep.add_argument("-l", "--live",          action="store_true",                                    help="run enrich() live instead of reading the stored columns")
    crep.add_argument("-d", "--diff",          action="store_true",                                    help="report per-field churn: stored columns vs a live enrich() pass")
    crep.add_argument("-b", "--by-confidence", action="store_true",                                    help="split the category column into per-confidence-level columns")
    caud.add_argument("-s", "--sender",        metavar="X[,Y]",                                        help="restrict to these senders (comma-separated)")
    caud.add_argument("-c", "--check",         metavar="NAME[,NAME]",                                  help="run only these checks (default all)")
    caud.add_argument("-l", "--limit",         metavar="N", type=int, default=5,                       help="examples per finding (default 5)")
    csho.add_argument("-s", "--sender",        metavar="X[,Y]",                                        help="restrict to these senders (comma-separated)")
    csho.add_argument(      "--like",          metavar=("FIELD", "PATTERN"), nargs=2, action="append", help="FIELD LIKE PATTERN (repeatable)")
    csho.add_argument(      "--eq",            metavar=("FIELD", "VALUE"),   nargs=2, action="append", help="FIELD = VALUE (repeatable)")
    csho.add_argument(      "--null",          action="append", metavar="FIELD",                       help="FIELD IS NULL (repeatable)")
    csho.add_argument(      "--not-null",      action="append", metavar="FIELD",                       help="FIELD IS NOT NULL (repeatable)")
    csho.add_argument("-m", "--min-conf",      type=float, metavar="X",                                help="enrich_confidence >= X")
    csho.add_argument("-M", "--max-conf",      type=float, metavar="X",                                help="enrich_confidence <= X")
    csho.add_argument("-l", "--limit",         type=int, default=20, metavar="N",                      help="max rows to dump (default 20)")
    cdis.add_argument("-s", "--sender",        metavar="X[,Y]",                                        help="restrict to these senders (comma-separated)")
    cdis.add_argument("-f", "--field",         required=True, metavar="NAME",                          help="the column to tally")
    cdis.add_argument("-l", "--limit",         type=int, default=30, metavar="N",                      help="top-N values (default 30)")

    matchp = sub.add_parser("match", help="resolve a TMDB id to mediathek rows (movies/series)", description="Wish-first matching: pull a TMDB movie's title variants/year/runtime (or a series episode's series title + season/episode) and tag the matching mediathek rows with tmdb_id + match_confidence (run), or explain the candidate scores read-only (show).")
    msub = matchp.add_subparsers(dest="match_cmd", required=True, metavar="action")
    mrun = msub.add_parser("run",  help="tag matching rows with tmdb_id + confidence (default)",  description="Resolve the TMDB id and write tmdb_id + match_confidence onto matching rows. For --type series, pass the (--tmdb, --season, --episode) triple. An existing different tmdb_id is preserved, not overwritten.")
    msho = msub.add_parser("show", help="read-only: explain candidate scores",                    description="List candidate rows with their score breakdown without writing. Defaults to listing everything not rejected.")
    mrst = msub.add_parser("reset", help="undo match: status 2 -> 1",                              description="Take matched rows (status '2') back to enriched ('1'). Clears tmdb_id + match_confidence unless --status-only. Pure DB op: no TMDB key needed.")
    mrst.add_argument("-s", "--status-only", action="store_true",                                        help="only flip status, keep tmdb_id + match_confidence")
    mrun.add_argument("-t", "--tmdb",     required=True, metavar="ID",                                  help="TMDB id to match (movie id, or series id for --type series)")
    mrun.add_argument("-T", "--type",     default="movie", choices=["movie", "series"],                 help="media type (default movie)")
    mrun.add_argument("-s", "--season",   type=int, metavar="N",                                        help="season number (required for --type series)")
    mrun.add_argument("-e", "--episode",  type=int, metavar="N",                                        help="episode number (required for --type series)")
    mrun.add_argument("-d", "--dry-run",  action="store_true",                                          help="compute matches but write nothing")
    mrun.add_argument("-m", "--min-conf", type=float, metavar="X",                                      help="min confidence to tag (default: config match_min_confidence)")
    mrun.add_argument("-y", "--year-tolerance", type=int, metavar="N", dest="year_tolerance",           help="accepted production-year difference for movies (default: config match_year_tolerance)")
    msho.add_argument("-t", "--tmdb",     required=True, metavar="ID",                                  help="TMDB id to inspect (movie id, or series id for --type series)")
    msho.add_argument("-T", "--type",     default="movie", choices=["movie", "series"],                 help="media type (default movie)")
    msho.add_argument("-s", "--season",   type=int, metavar="N",                                        help="season number (required for --type series)")
    msho.add_argument("-e", "--episode",  type=int, metavar="N",                                        help="episode number (required for --type series)")
    msho.add_argument("-m", "--min-conf", type=float, metavar="X",                                      help="min confidence to list (default 0.0)")
    msho.add_argument("-y", "--year-tolerance", type=int, metavar="N", dest="year_tolerance",           help="accepted production-year difference for movies (default: config match_year_tolerance)")
    msho.add_argument("-l", "--limit",    type=int, default=20, metavar="N",                            help="max candidates to list (default 20)")

    queuep = sub.add_parser("queue", help="stage and review the download queue", description="Stage downloads into the review queue by tmdb_id (deduplicated) or mediathek_id (direct), and manage them. DB-only: nothing here touches the filesystem.")
    qsub = queuep.add_subparsers(dest="queue_cmd", required=True, metavar="action")
    qadd = qsub.add_parser("add", help="stage downloads by tmdb_id or mediathek_id", description="Stage downloads. --tmdb dedups a matched film's many rows to the minimal download set (best quality per whitelisted language, shared video flagged for remux); --mediathek-id queues one row directly. New entries are 'proposed' unless queue_auto_approve is set.")
    qadd.add_argument("-t", "--tmdb",         action="append", metavar="ID", help="TMDB id to stage, deduplicated (repeatable)")
    qadd.add_argument("-m", "--mediathek-id", action="append", metavar="ID", help="mediathek_id to stage directly (repeatable)")
    qadd.add_argument(      "--language",     metavar="CODE",                help="override the audio language code")
    qadd.add_argument(      "--resolution",  choices=["HD", "SD", "LQ"],     help="override the video resolution tier")
    qadd.add_argument(      "--remux",       choices=["AV", "A", "V"],       help="override the remux mode")
    qadd.add_argument(      "--url",          metavar="URL",                 help="override the source media URL")
    qadd.add_argument(      "--url-subtitle", metavar="URL",                 help="override the subtitle URL")
    qadd.add_argument(      "--path",         metavar="PATH",                help="override the target library path")
    qlst = qsub.add_parser("list", help="list queue entries (default)", description="List queue entries, newest creation last. Filter by lifecycle state with --status.")
    qlst.add_argument("-s", "--status",       choices=list(QUEUE_STATUS), metavar="STATE", help="filter by state: " + ", ".join(QUEUE_STATUS))
    qapp = qsub.add_parser("approve", help="approve proposed entries for download", description="Move proposed entries to approved (the gate to download). Give queue ids or --all. With --force, re-approve entries in any state (e.g. cancelled or done).")
    qapp.add_argument("ids",            nargs="*", type=int, metavar="ID", help="queue entry ids to approve")
    qapp.add_argument("-a", "--all",          action="store_true", help="approve every proposed entry")
    qapp.add_argument("-f", "--force",        action="store_true", help="re-approve regardless of current state")
    qcan = qsub.add_parser("cancel", help="cancel active entries", description="Cancel active entries (proposed/approved/busy) -- a soft state change that keeps the record. Give queue ids or --all.")
    qcan.add_argument("ids",            nargs="*", type=int, metavar="ID", help="queue entry ids to cancel")
    qcan.add_argument("-a", "--all",          action="store_true", help="cancel every active entry")
    qdel = qsub.add_parser("delete", help="permanently remove queue entries", description="Hard-delete queue entries by exactly one selector: given ids, --all, or terminal state (--cancelled/--done/--failed, combinable).")
    qdel.add_argument("ids",            nargs="*", type=int, metavar="ID", help="queue entry ids to delete")
    qdel.add_argument("-a", "--all",          action="store_true", help="delete every entry")
    qdel.add_argument("-c", "--cancelled",    action="store_true", help="delete all cancelled entries")
    qdel.add_argument("-d", "--done",         action="store_true", help="delete all done entries")
    qdel.add_argument("-f", "--failed",       action="store_true", help="delete all failed entries")
    qdl = qsub.add_parser("download", help="download/remux/move approved entries", description="Run the download -> remux -> move pipeline for approved entries, driven entirely by the queue row (url/remux/language/url_subtitle/path). Each row works under a unique temp prefix and lands at its stored path; temp files are removed afterwards. A failing row is marked 'failed' and does not abort the batch. Give queue ids or --all. Progress is printed to stderr.")
    qdl.add_argument("ids",             nargs="*", type=int, metavar="ID", help="approved queue entry ids to download")
    qdl.add_argument("-a", "--all",           action="store_true", help="download every approved entry")
    qdl.add_argument("-f", "--force",         action="store_true", help="overwrite an existing destination file")

    filep = sub.add_parser("file", help="download / remux / move a single file", description="Queue-independent file primitives driven by explicit URLs/paths: download a media URL (HTTP with Range-resume, or HLS segment assembly with an ffmpeg fallback), remux via ffmpeg (stream copy), or move a file. Progress is printed to stderr.")
    fsub = filep.add_subparsers(dest="file_cmd", required=True, metavar="action")
    fdl = fsub.add_parser("download", help="download a media URL to a local file", description="Download --url to --out. A '.m3u8' URL is assembled from its HLS segments (ffmpeg fallback when encrypted or assembly fails); anything else is a plain HTTP download that resumes a leftover '.part' via Range. Failed downloads retry.")
    fdl.add_argument("-u", "--url",     required=True, metavar="URL",  help="media URL to download")
    fdl.add_argument("-o", "--out",     required=True, metavar="PATH", help="output file path")
    fdl.add_argument("-r", "--retries", type=int, metavar="N",         help="retry attempts on error (default: config download_retries)")
    fdl.add_argument("-t", "--timeout", type=int, metavar="SEC",       help="network timeout in seconds (default: config download_timeout)")
    frx = fsub.add_parser("remux", help="remux a file via ffmpeg (stream copy)", description="Stream-copy --in into --out with ffmpeg (no transcoding). --mode picks what to keep: AV (audio+video), A (audio only), V (video only). --language tags the first audio track. --check-ffmpeg only probes the configured ffmpeg binary and exits.")
    frx.add_argument("-i", "--in",          dest="in_path", metavar="PATH",                  help="input file path")
    frx.add_argument("-m", "--mode",        choices=["AV", "A", "V"],                         help="what to keep: AV (audio+video), A (audio), V (video)")
    frx.add_argument("-o", "--out",         metavar="PATH",                                  help="output file path")
    frx.add_argument("-l", "--language",    metavar="CODE",                                  help="set the audio track language tag (e.g. deu)")
    frx.add_argument(      "--check-ffmpeg", dest="check_ffmpeg", action="store_true",        help="probe the configured ffmpeg binary (-version) and exit")
    frxs = fsub.add_parser("remux-subtitle", help="convert a subtitle file to player-ready sidecars", description="Convert --in (TTML/EBU-TT(-D) XML or WebVTT) into one '<base>.<lang>.<ext>' sidecar per format. ffmpeg-free (ffmpeg cannot decode TTML and drops colour/position). --format overrides the configured subtitle_formats; --out sets the base path (default: --in without its extension).")
    frxs.add_argument("-i", "--in",       dest="in_path", required=True, metavar="PATH",     help="input subtitle file (.ttml/.xml/.vtt)")
    frxs.add_argument("-o", "--out",      metavar="BASE",                                     help="output base path (default: input path without extension)")
    frxs.add_argument("-l", "--language", default="de", metavar="CODE",                       help="sidecar language tag (default: de)")
    frxs.add_argument(      "--format",   metavar="LIST",                                     help="comma-separated formats to write (default: config subtitle_formats)")
    frxs.add_argument("-f", "--force",    action="store_true",                                help="overwrite existing sidecars")
    fmv = fsub.add_parser("move", help="move a file into the library", description="Move --in to --out, creating parent directories. An existing destination is an error unless --force.")
    fmv.add_argument("-i", "--in",    dest="in_path", required=True, metavar="PATH", help="source file path")
    fmv.add_argument("-o", "--out",   required=True, metavar="PATH",                 help="destination file path")
    fmv.add_argument("-f", "--force", action="store_true",                           help="overwrite an existing destination")

    libp = sub.add_parser("library", help="manage the wishlist (TMDB ids to acquire)", description="Manage the wishlist: TMDB movie ids to acquire automatically. Entries are status 'W' (wish) until a finished download records them as 'L' (in library). DB-only: nothing here touches the filesystem.")
    lsub = libp.add_subparsers(dest="library_cmd", required=True, metavar="action")
    ladd = lsub.add_parser("add",    help="add wishes by tmdb_id or title",  description="Add movie wishes (status 'W'): give TMDB ids directly via --tmdb, or resolve a title via --title (with a tolerant --year). Idempotent: an existing id is left untouched. The film title is captured as a label when a TMDB key is configured.")
    llst = lsub.add_parser("list",   help="list library entries (default)", description="List library entries, oldest creation first. Filter by lifecycle state with --status.")
    lrem = lsub.add_parser("remove", help="remove library entries",         description="Delete library entries by exactly one selector: given tmdb_ids or --all.")
    ladd.add_argument("-t", "--tmdb",            action="append", metavar="ID",    help="TMDB movie id to wish for (repeatable)")
    ladd.add_argument(      "--title",           metavar="TITLE",                  help="movie title to resolve to a TMDB id via search (instead of --tmdb)")
    ladd.add_argument("-y", "--year",            type=int, metavar="YEAR",         help="release year to disambiguate --title (may be off by --year-tolerance)")
    ladd.add_argument(      "--year-tolerance",  type=int, metavar="N", dest="year_tolerance", help="accepted year difference for --title (default: config match_year_tolerance)")
    llst.add_argument("-s", "--status", choices=list(LIBRARY_STATUS), metavar="STATE", help="filter by state: " + ", ".join(LIBRARY_STATUS))
    lrem.add_argument("-t", "--tmdb",   action="append", metavar="ID",                 help="tmdb_id to remove (repeatable)")
    lrem.add_argument("-a", "--all",    action="store_true",                           help="remove every library entry")

    sub.add_parser("update", help="run the wishlist pipeline end to end", description="One unattended pass: fetch + enrich the mirror, then for every open wish ('W') match the TMDB id and stage the deduplicated download set. With queue_auto_approve set, the approved entries are downloaded straight away (recording each finished wish as 'L'); otherwise the run stops at the approval gate. A failing wish does not abort the pass. Progress is printed to stderr.")

    _set_default_action(parser, "enrich",  csub, "run")
    _set_default_action(parser, "match",   msub, "run")
    _set_default_action(parser, "queue",   qsub, "list")
    _set_default_action(parser, "library", lsub, "list")
    return parser


def _set_default_action(parser, command, subparsers, default):
    """Register `default` as the sub-action used when `command` is invoked with
    no action. Any parent can carry its own default (not just `run`)."""
    parser.default_actions[command] = (default, subparsers)


def _inject_default_action(parser, argv):
    """Insert a parent command's default sub-action when it is invoked bare, so
    `theke enrich` behaves like `theke enrich run` and `theke match --tmdb X`
    like `theke match run --tmdb X`. Injecting before parsing lets argparse do
    the rest -- sub-action flags, required checks and defaults all stay intact.
    An explicit action or a parent-level `-h`/`--help` is left untouched."""
    argv = list(sys.argv[1:] if argv is None else argv)
    idx = _command_index(parser, argv)
    if idx is None:
        return argv
    spec = parser.default_actions.get(argv[idx])
    if spec is None:
        return argv
    default, subparsers = spec
    nxt = argv[idx + 1] if idx + 1 < len(argv) else None
    if nxt in subparsers.choices or nxt in ("-h", "--help"):
        return argv
    return argv[:idx + 1] + [default] + argv[idx + 1:]


def _command_index(parser, argv):
    """Index of the command token in argv: the first positional, skipping the
    top-level options and the values they consume. None if argv names none."""
    value_opts = {opt for a in parser._actions if a.option_strings and a.nargs != 0
                      for opt in a.option_strings}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if not tok.startswith("-") or tok == "-":
            return i
        i += 2 if tok in value_opts and "=" not in tok else 1
    return None


def _setup_logging():
    """Route the theke logger to stderr, one prefixed line per record flushed
    live, so a long stage stays visible without polluting the stdout result (the
    single JSON object in --json mode). Rebuilt on every call so the handler
    always binds the current sys.stderr (which pytest's capsys swaps per test)."""
    log.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("-> %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def main(argv=None) -> int:
    """CLI entry point; returns the process exit code."""
    _setup_logging()

    try:
        parser = build_parser()
        args = parser.parse_args(_inject_default_action(parser, argv))
    except SystemExit as exc:  # argparse handles usage errors and --help
        return EXIT_USAGE if exc.code else EXIT_OK

    try:
        cfg = load_config(args.config, overrides={"db_path": args.db})
        match args.command:
            case "config":
                result = cmd_config(cfg)
            case "file":
                result = cmd_file(cfg, args)
            case "fetch":
                conn = db_connect(cfg.db_path)
                try:     result = cmd_fetch(conn, cfg, args)
                finally: conn.close()
            case "enrich":
                conn = db_connect(cfg.db_path)
                try:     result = cmd_enrich(conn, cfg, args)
                finally: conn.close()
            case "match":
                conn = db_connect(cfg.db_path)
                try:     result = cmd_match(conn, cfg, args)
                finally: conn.close()
            case "queue":
                conn = db_connect(cfg.db_path)
                try:     result = cmd_queue(conn, cfg, args)
                finally: conn.close()
            case "library":
                conn = db_connect(cfg.db_path)
                try:     result = cmd_library(conn, cfg, args)
                finally: conn.close()
            case "update":
                conn = db_connect(cfg.db_path)
                try:     result = cmd_update(conn, cfg, args)
                finally: conn.close()
            case _: raise DbError(f"unhandled command: {args.command}")

    except Exception as exc:  # any failure becomes one clean error, never a traceback
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCKED if isinstance(exc, DbLockedError) else EXIT_ERROR

    if args.json:
        print(json.dumps(result))
    else:
        for key, value in result.items():
            print(f"{key} = {value}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
