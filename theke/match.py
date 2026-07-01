# -- match (phase 3, part 2: wish-first TMDB matching, movies) ----------------
# Drive from a canonical TMDB id: pull its title variants + year + runtime once,
# then search the enrich-normalized columns for matching mediathek rows. Pure
# helpers (normalize/score) are network-free and unit-testable; tmdb_movie is the
# only IO and goes through core.http_get (monkeypatched in tests).
import difflib
import json
import logging
import re
import time
from functools import lru_cache
from urllib.parse import urlencode

from theke import core   # http_get; via the module so theke.core.http_get patches apply

log = logging.getLogger("theke")   # scan timings at DEBUG (theke run --verbose)

# Scoring knobs. Title is a gate (below the floor is no match); year is a near-
# hard gate (production year assumed); runtime is a soft confirmer.
TITLE_FLOOR            = 0.85
SUBSTR_SIM             = 0.95   # tmdb title is a whole-token run inside clean_title
SUBSTR_COVERAGE        = 0.60   # ...and covers at least this share of the longer side
YEAR_TOLERANCE         = 2      # |year - release_year| above this -> rejected
YEAR_PENALTY           = 0.03   # per year of distance, within tolerance
NO_YEAR_FACTOR         = 0.85   # row has no year -> no gate, capped confidence
RUNTIME_TOLERANCE      = 0.15   # relative runtime distance still counted as a hit
RUNTIME_PENALTY_FACTOR = 0.90   # beyond tolerance: soft penalty, never a reject
RUNTIME_FLOOR_RATIO    = 0.50   # duration below this share of runtime -> rejected (clip)

ARTICLES = {"der", "die", "das", "ein", "eine", "the", "a", "an"}


# -- normalization (both sides identical) ------------------------------------

def normalize(title) -> str:
    """Casefold, fold ae/oe/ue/ss, punctuation -> space, collapse whitespace.
    The subtitle after a colon is kept (only the punctuation is dropped)."""
    s = (title or "").casefold()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_articles(s: str) -> str:
    """Drop a single leading article token; never empty a sole-token title
    (so "Die Hard"/"Das Boot" survive as "hard"/"boot", but "die" stays)."""
    parts = s.split()
    if len(parts) > 1 and parts[0] in ARTICLES:
        return " ".join(parts[1:])
    return s


def _forms(s: str) -> list:
    """A normalized string in both its full and article-stripped form."""
    stripped = strip_articles(s)
    return [s] if stripped == s else [s, stripped]


@lru_cache(maxsize=1 << 17)
def _match_forms(title) -> tuple:
    """A title's comparison forms as (normalized_string, token_tuple) pairs (full
    + article-stripped). Cached so a row's clean_title is normalized once per
    process, not once per row per wish (the run-pass hot path)."""
    return tuple((f, tuple(f.split())) for f in _forms(normalize(title)))


# -- title similarity --------------------------------------------------------

def _is_token_run(short, long) -> bool:
    """True if `short` is a contiguous run of tokens inside `long`."""
    if not short or len(short) > len(long):
        return False
    return any(long[i:i + len(short)] == short for i in range(len(long) - len(short) + 1))


def _pair_sim(a, ta, b, tb) -> float:
    """Similarity of two normalized strings (with their pre-split token tuples):
    exact, whole-token containment with enough coverage (so a short franchise
    name inside a long title does not over-score), else a difflib ratio."""
    if a == b:
        return 1.0
    if _is_token_run(ta, tb) or _is_token_run(tb, ta):
        if min(len(ta), len(tb)) / max(len(ta), len(tb)) >= SUBSTR_COVERAGE:
            return SUBSTR_SIM
    return difflib.SequenceMatcher(None, a, b).ratio()


def title_similarity(tmdb_titles, clean_title) -> float:
    """Best similarity of any TMDB title variant against clean_title, each
    compared in full and article-stripped form (handles missing/extra article)."""
    clean_forms = _match_forms(clean_title)
    best = 0.0
    for t in tmdb_titles:
        for tf, tt in _match_forms(t):
            for cf, ct in clean_forms:
                best = max(best, _pair_sim(tf, tt, cf, ct))
                if best == 1.0:
                    return 1.0
    return best


# -- scoring -----------------------------------------------------------------

def _runtime_factor(runtime, duration):
    """Soft runtime confirmer shared by movies and episodes: 1.0 within
    tolerance, a soft penalty beyond it, a hard reject below the clip floor.
    Returns (factor, delta_minutes, rejected); no runtime/duration -> neutral."""
    if not (runtime and duration):
        return 1.0, None, False
    dur_min = duration / 60
    delta = int(round(dur_min - runtime))
    if dur_min < runtime * RUNTIME_FLOOR_RATIO:   # clip/trailer/excerpt, not the film
        return 0.0, delta, True
    rel = abs(dur_min - runtime) / runtime
    factor = 1.0 if rel <= RUNTIME_TOLERANCE else RUNTIME_PENALTY_FACTOR
    return factor, delta, False


def score_match(tmdb_meta, row, year_tolerance=YEAR_TOLERANCE) -> dict:
    """Score one mediathek row against TMDB metadata. Deterministic and
    explainable: title is the gate, year a near-hard gate (within
    year_tolerance years), runtime a soft confirmer. Returns confidence + the
    breakdown that fed it."""
    title_sim = title_similarity(tmdb_meta["titles"], row["clean_title"])
    rejected = title_sim < TITLE_FLOOR

    my, ry = row["year"], tmdb_meta.get("year")
    if my is not None and ry is not None:
        year_delta = abs(my - ry)
        if year_delta > year_tolerance:
            rejected = True
            year_factor = 0.0
        else:
            year_factor = 1.0 - YEAR_PENALTY * year_delta
    else:
        year_delta = None
        year_factor = NO_YEAR_FACTOR

    runtime_factor, runtime_delta, rt_reject = _runtime_factor(
        tmdb_meta.get("runtime"), row["duration"])
    rejected = rejected or rt_reject

    confidence = 0.0 if rejected else round(title_sim * year_factor * runtime_factor, 3)
    return {"confidence": confidence, "title_sim": round(title_sim, 3),
            "year_delta": year_delta, "runtime_delta": runtime_delta,
            "rejected": rejected}


def score_episode(tv_meta, row) -> dict:
    """Score one Episode row against a TMDB series+episode. The series-name
    similarity and the exact (season, episode) are gates; the episode-title
    similarity and runtime are soft confirmers. Deterministic and explainable."""
    series_sim = title_similarity(tv_meta["series_titles"], row["series_name"])
    episode_title_sim = title_similarity(tv_meta["episode_titles"], row["clean_title"])
    rejected = series_sim < TITLE_FLOOR
    if row["season"] != tv_meta["season"] or row["episode"] != tv_meta["episode"]:
        rejected = True

    runtime_factor, runtime_delta, rt_reject = _runtime_factor(
        tv_meta.get("runtime"), row["duration"])
    rejected = rejected or rt_reject

    confidence = 0.0 if rejected else round(series_sim * runtime_factor, 3)
    return {"confidence": confidence, "series_sim": round(series_sim, 3),
            "episode_title_sim": round(episode_title_sim, 3),
            "runtime_delta": runtime_delta, "rejected": rejected}


# -- TMDB lookup (the only IO) -----------------------------------------------

def tmdb_movie(cfg, tmdb_id) -> dict:
    """Fetch a TMDB movie's canonical metadata for matching: German + original
    title + the DE alternative titles, plus release year and runtime."""
    params = urlencode({"api_key": cfg.tmdb_api_key, "language": cfg.tmdb_language,
                        "append_to_response": "alternative_titles"})
    url = f"{cfg.tmdb_api_url}/movie/{tmdb_id}?{params}"
    data = json.loads(core.http_get(url, cfg.download_timeout).decode("utf-8"))

    titles = []
    for t in (data.get("title"), data.get("original_title")):
        if t and t not in titles:
            titles.append(t)
    for alt in data.get("alternative_titles", {}).get("titles", []):
        if alt.get("iso_3166_1") == "DE" and alt.get("title") not in titles:
            titles.append(alt["title"])

    release = data.get("release_date") or ""
    year = int(release[:4]) if release[:4].isdigit() else None
    return {"tmdb_id": str(tmdb_id), "title": titles[0] if titles else None,
            "titles": titles, "year": year, "runtime": data.get("runtime") or None,
            "original_language": data.get("original_language")}


def _tmdb_search_page(cfg, title):
    """One page of TMDB movie search as (candidates, total_results, total_pages):
    candidates are {tmdb_id, title, year} (year from the release_date), in TMDB's
    popularity order. The wanted year is NOT sent (tolerant local match)."""
    params = urlencode({"api_key": cfg.tmdb_api_key, "language": cfg.tmdb_language,
                        "query": title})
    url = f"{cfg.tmdb_api_url}/search/movie?{params}"
    data = json.loads(core.http_get(url, cfg.download_timeout).decode("utf-8"))
    cands = []
    for r in data.get("results", []):
        rel = r.get("release_date") or ""
        year = int(rel[:4]) if rel[:4].isdigit() else None
        cands.append({"tmdb_id": str(r["id"]), "title": r.get("title") or "",
                      "year": year})
    return cands, data.get("total_results") or len(cands), data.get("total_pages") or 1


def tmdb_search(cfg, title) -> list:
    """Page-1 TMDB movie candidates ({tmdb_id, title, year}) for a title, thin
    wrapper over _tmdb_search_page dropping the pagination totals."""
    return _tmdb_search_page(cfg, title)[0]


def tmdb_list(cfg, list_id) -> list:
    """Fetch a TMDB list's entries as {tmdb_id, title, year, media_type}, reading
    the v3 /list/{id} endpoint. A configured read access token authenticates via a
    Bearer header (covers private lists); otherwise the api_key query param is used
    (public lists only). Paginates defensively when the response is paged."""
    headers = {"Authorization": f"Bearer {cfg.tmdb_read_token}"} if cfg.tmdb_read_token else None
    items, page, pages = [], 1, 1
    while page <= pages:
        params = {"language": cfg.tmdb_language, "page": page}
        if not cfg.tmdb_read_token:
            params["api_key"] = cfg.tmdb_api_key
        url = f"{cfg.tmdb_api_url}/list/{list_id}?{urlencode(params)}"
        data = json.loads(core.http_get(url, cfg.download_timeout, headers=headers).decode("utf-8"))
        for r in data.get("items") or data.get("results") or []:
            rel = r.get("release_date") or r.get("first_air_date") or ""
            year = int(rel[:4]) if rel[:4].isdigit() else None
            items.append({"tmdb_id": str(r["id"]),
                          "title": r.get("title") or r.get("name") or "",
                          "year": year, "media_type": r.get("media_type") or "movie"})
        pages = data.get("total_pages") or 1
        page += 1
    return items


def pick_by_year(candidates, year, tolerance):
    """Pick the one TMDB candidate to confirm for a wanted year: within
    tolerance, the smallest year distance, ties keeping TMDB's popularity order
    (candidates without a year are skipped once a year is wanted). Without a
    wanted year only an unambiguous result counts: exactly one candidate, else
    None (avoids guessing). None when nothing qualifies."""
    if not candidates:
        return None
    if year is None:
        return candidates[0] if len(candidates) == 1 else None
    best, best_delta = None, None
    for c in candidates:
        if c["year"] is None:
            continue
        delta = abs(c["year"] - year)
        if delta > tolerance or (best_delta is not None and delta >= best_delta):
            continue
        best, best_delta = c, delta
    return best


# -- unified movie search (the one title->TMDB choke point) ------------------
# search_movies does the whole job for every caller (tmdb search, library add/
# import, match bulk): search + article retry, cheap year/broadcast prefilter,
# per-survivor detail fetch, and full scoring (title floor + year + a hard
# runtime gate). Callers only pass the signals they have and read the result.


def _in_windows(cand_year, year, broadcast_year, tol) -> bool:
    """Cheap pre-detail filter on a candidate's release year: inside the wanted
    year window (undated candidates only survive when no year is wanted) and not
    released past the broadcast year within tolerance (undated candidates
    survive)."""
    if year is not None and (cand_year is None or abs(cand_year - year) > tol):
        return False
    if broadcast_year is not None and cand_year is not None \
            and cand_year > broadcast_year + tol:
        return False
    return True


def _score_candidate(meta, title, year, runtime, tol):
    """Confidence for one fetched candidate against the wanted title/year/runtime,
    or None when a hard gate rejects it: score_match's title floor + year gate, a
    wanted year needs a TMDB year to confirm, and a wanted runtime is mandatory
    and within RUNTIME_TOLERANCE. `runtime` is in minutes."""
    row = {"clean_title": title, "year": year,
           "duration": runtime * 60 if runtime is not None else None}
    s = score_match(meta, row, year_tolerance=tol)
    if s["rejected"] or (year is not None and meta.get("year") is None):
        return None
    if runtime is not None:
        mr = meta.get("runtime")
        if not mr or abs(runtime - mr) / mr > RUNTIME_TOLERANCE:
            return None
    return s["confidence"]


def search_movies(cfg, title, *, year=None, broadcast_year=None, runtime=None,
                  tolerance=None) -> dict:
    """Resolve a movie title against TMDB, doing the whole job: search (retrying
    without a leading article), drop candidates outside the year/broadcast
    windows cheaply, then fetch each survivor's detail and fully score it (title
    floor, year, runtime as a hard gate when a wanted runtime is given). Returns
    {matches, total, truncated}: matches ({tmdb_id,title,year,runtime,confidence})
    sorted by confidence desc (ties keep TMDB popularity), the raw total_results,
    and whether results spilled past page 1 (truncated -> never a safe auto-match)."""
    tol = cfg.match_year_tolerance if tolerance is None else tolerance
    cands, total, pages = _tmdb_search_page(cfg, title)
    if not cands:
        stripped = _drop_article(title)
        if stripped != title:
            cands, total, pages = _tmdb_search_page(cfg, stripped)
    scored = []
    for i, c in enumerate(cands):
        if not _in_windows(c["year"], year, broadcast_year, tol):
            continue
        meta = tmdb_movie(cfg, c["tmdb_id"])
        conf = _score_candidate(meta, title, year, runtime, tol)
        if conf is not None:
            scored.append((-conf, i, {"tmdb_id": meta["tmdb_id"], "title": meta["title"],
                                      "year": meta["year"], "runtime": meta["runtime"],
                                      "confidence": conf}))
    scored.sort(key=lambda s: (s[0], s[1]))
    return {"matches": [s[2] for s in scored], "total": total, "truncated": pages > 1}


def resolve_one(cfg, title, *, year=None, broadcast_year=None, runtime=None,
                tolerance=None) -> dict:
    """Auto-match adapter over search_movies: a single, non-truncated match ->
    {tmdb_id,title,year,confidence}; otherwise a typed miss {error: 'none' |
    'ambiguous'(+count) | 'truncated'(+total)}. Never raises (the tmdb search
    command needs the structured result)."""
    res = search_movies(cfg, title, year=year, broadcast_year=broadcast_year,
                        runtime=runtime, tolerance=tolerance)
    if res["truncated"]:
        return {"error": "truncated", "total": res["total"]}
    ms = res["matches"]
    if not ms:
        return {"error": "none"}
    if len(ms) > 1:
        return {"error": "ambiguous", "count": len(ms)}
    m = ms[0]
    return {"tmdb_id": m["tmdb_id"], "title": m["title"], "year": m["year"],
            "confidence": m["confidence"]}


def tmdb_tv(cfg, tmdb_id, season, episode) -> dict:
    """Fetch a TMDB series' metadata for episode matching, in two calls: the
    series (name + original + DE alternative titles, the gate) and the episode
    (name + runtime + air year, plus its translated names as soft confirmers).
    TV alternative_titles live under 'results' (movies use 'titles')."""
    sp = urlencode({"api_key": cfg.tmdb_api_key, "language": cfg.tmdb_language,
                    "append_to_response": "alternative_titles"})
    s = json.loads(core.http_get(
        f"{cfg.tmdb_api_url}/tv/{tmdb_id}?{sp}", cfg.download_timeout).decode("utf-8"))
    series_titles = []
    for t in (s.get("name"), s.get("original_name")):
        if t and t not in series_titles:
            series_titles.append(t)
    for alt in s.get("alternative_titles", {}).get("results", []):
        if alt.get("iso_3166_1") == "DE" and alt.get("title") not in series_titles:
            series_titles.append(alt["title"])

    ep = urlencode({"api_key": cfg.tmdb_api_key, "language": cfg.tmdb_language,
                    "append_to_response": "translations"})
    url = f"{cfg.tmdb_api_url}/tv/{tmdb_id}/season/{season}/episode/{episode}?{ep}"
    e = json.loads(core.http_get(url, cfg.download_timeout).decode("utf-8"))
    episode_name = e.get("name") or None
    episode_titles = []
    for t in [episode_name] + [tr.get("data", {}).get("name") for tr in
                               e.get("translations", {}).get("translations", [])]:
        if t and t not in episode_titles:
            episode_titles.append(t)
    air = e.get("air_date") or ""
    year = int(air[:4]) if air[:4].isdigit() else None
    return {"tmdb_id": str(tmdb_id),
            "series_title": series_titles[0] if series_titles else None,
            "series_titles": series_titles, "episode_name": episode_name,
            "episode_titles": episode_titles, "runtime": e.get("runtime") or None,
            "year": year, "season": season, "episode": episode}


# -- bulk match (phase 15: row-driven, eager, hard-gated) --------------------
# Reverse of the lazy id-driven match: search TMDB by a row's own title and
# accept only on hard gates (title + mandatory runtime + confirmed year +
# release-not-after-broadcast), so the eager catalog stays free of false hits.


def _drop_article(title) -> str:
    """Drop a single leading article word from a raw title (case-insensitive),
    for a search retry; unchanged when there is no leading article."""
    head, _, rest = (title or "").partition(" ")
    return rest if rest and head.casefold() in ARTICLES else title


def _search_candidates(cfg, title) -> list:
    """TMDB search for a title, retrying without a leading article when the first
    query finds nothing (mirrors library's _search_title)."""
    cands = tmdb_search(cfg, title)
    if not cands:
        stripped = _drop_article(title)
        if stripped != title:
            cands = tmdb_search(cfg, stripped)
    return cands


def _broadcast_year(date):
    """The airing year from a mediathek `date` (leading 'YYYY...'); None when
    absent or unparseable."""
    s = str(date or "")[:4]
    return int(s) if s.isdigit() else None


def bulk_accept(meta, row, tol) -> dict:
    """Decide whether to eagerly tag `row` with TMDB `meta`, on hard gates (all
    must hold): the lazy score must not reject (title floor + year within tol);
    runtime is mandatory and within RUNTIME_TOLERANCE (not the soft lazy
    confirmer); a row year needs a TMDB year to confirm against; and the release
    year must not sit past the broadcast year (within tol). Returns
    {"accepted", "confidence"} with the lazy confidence when accepted."""
    s = score_match(meta, row, year_tolerance=tol)
    runtime, dur = meta.get("runtime"), row["duration"]
    my, ry = row["year"], meta.get("year")
    by = _broadcast_year(row.get("date"))
    ok = (not s["rejected"]
          and runtime and dur and abs(dur / 60 - runtime) / runtime <= RUNTIME_TOLERANCE
          and (my is None or ry is not None)
          and (by is None or ry is None or ry <= by + tol))
    return {"accepted": bool(ok), "confidence": s["confidence"] if ok else 0.0}


def bulk_match(conn, cfg, limit=None, year_tolerance=None) -> dict:
    """Eager, row-driven movie match (phase 15): for each enriched-and-untried
    movie row ('1', non-trailer) search TMDB by its own clean_title, confirm on
    the hard gates (pick_by_year + bulk_accept) and tag a confident hit '3' (with
    tmdb_id) or mark the row '2' (bulk-attempted, no match -- skipped by later
    bulk passes, still lazy-matchable). Deduplicates the search per distinct
    title and caches TMDB movie lookups by id; `limit` caps rows per call so a
    backlog drains over several passes. Returns {scanned, matched, attempted}."""
    tol = cfg.match_year_tolerance if year_tolerance is None else year_tolerance
    start = time.perf_counter()
    sql = ("SELECT mediathek_id, clean_title, year, duration, date, tmdb_id "
           "FROM mediathek WHERE category='Movie' AND status='1' "
           "AND (flags IS NULL OR flags NOT LIKE '%T%') ORDER BY mediathek_id")
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(sql)]
    log.info("bulk matching %d movie rows", len(rows))
    searches, movies, matched = {}, {}, 0
    for r in rows:
        key = normalize(r["clean_title"])
        if key not in searches:
            searches[key] = _search_candidates(cfg, r["clean_title"])
        cand = pick_by_year(searches[key], r["year"], tol)
        tid, conf = None, None
        if cand is not None and not (r["tmdb_id"] and r["tmdb_id"] != cand["tmdb_id"]):
            cid = cand["tmdb_id"]
            if cid not in movies:
                movies[cid] = tmdb_movie(cfg, cid)
            a = bulk_accept(movies[cid], r, tol)
            if a["accepted"]:
                tid, conf = cid, a["confidence"]
        # persist per row (autocommit): a Ctrl+C mid-loop keeps the done rows.
        if tid is not None:
            conn.execute("UPDATE mediathek SET tmdb_id=?, match_confidence=?, "
                         "status='3' WHERE mediathek_id=?", (tid, conf, r["mediathek_id"]))
            matched += 1
        else:
            conn.execute("UPDATE mediathek SET status='2' WHERE mediathek_id=?",
                         (r["mediathek_id"],))
        log.info("bulk %s '%s' (%s): %s", r["mediathek_id"][:7], r["clean_title"],
                 r["year"], "matched" if tid is not None else "no match")
    log.debug("bulk_match: %d rows -> %d matched, %.2fs", len(rows), matched,
              time.perf_counter() - start)
    return {"scanned": len(rows), "matched": matched, "attempted": len(rows) - matched}


# -- candidate search --------------------------------------------------------

def find_matches(conn, tmdb_meta, min_conf, year_tolerance=YEAR_TOLERANCE) -> list:
    """Scan the movie subset, score each row (accepting years within
    year_tolerance), return the matches (confidence >= min_conf, not rejected)
    sorted by confidence desc, then mediathek_id."""
    start = time.perf_counter()
    sql = ("SELECT mediathek_id, clean_title, year, duration, flags "
           "FROM mediathek WHERE category='Movie' AND status IN ('1','2')")
    params = ()
    ry = tmdb_meta.get("year")   # year is a near-hard gate -> prune out-of-window
    if ry is not None:           # rows in SQL; jahrlose rows survive (no gate)
        sql += " AND (year IS NULL OR year BETWEEN ? AND ?)"
        params = (ry - year_tolerance, ry + year_tolerance)
    rows = conn.execute(sql, params)
    out, scanned = [], 0
    for r in rows:
        scanned += 1
        if r["flags"] and "T" in r["flags"]:   # trailers are never the wanted film
            continue
        s = score_match(tmdb_meta, r, year_tolerance)
        if s["rejected"] or s["confidence"] < min_conf:
            continue
        out.append({"mediathek_id": r["mediathek_id"], "clean_title": r["clean_title"],
                    "confidence": s["confidence"], "title_sim": s["title_sim"],
                    "year_delta": s["year_delta"], "runtime_delta": s["runtime_delta"]})
    out.sort(key=lambda m: (-m["confidence"], m["mediathek_id"]))
    log.debug("find_matches: scanned %d Movie rows -> %d candidates, %.2fs",
              scanned, len(out), time.perf_counter() - start)
    return out


def find_episode_matches(conn, tv_meta, min_conf) -> list:
    """Scan the Episode subset for the wanted (season, episode), score each row,
    return the matches (confidence >= min_conf, not rejected) sorted by
    confidence desc, then mediathek_id."""
    rows = conn.execute(
        "SELECT mediathek_id, clean_title, series_name, season, episode, duration, "
        "flags FROM mediathek WHERE category='Episode' AND status='1' "   # episodes are never bulk-attempted (movies-only)
        "AND season=? AND episode=?", (tv_meta["season"], tv_meta["episode"]))
    out = []
    for r in rows:
        if r["flags"] and "T" in r["flags"]:   # trailers are never the wanted episode
            continue
        s = score_episode(tv_meta, r)
        if s["rejected"] or s["confidence"] < min_conf:
            continue
        out.append({"mediathek_id": r["mediathek_id"], "clean_title": r["clean_title"],
                    "confidence": s["confidence"], "series_sim": s["series_sim"],
                    "episode_title_sim": s["episode_title_sim"],
                    "runtime_delta": s["runtime_delta"]})
    out.sort(key=lambda m: (-m["confidence"], m["mediathek_id"]))
    return out


# -- arte language variants (second match pass) ------------------------------
# Arte airs one film under several language senders (ARTE.DE/FR/ES/EN/IT/PL),
# each with a localized title -- and even slightly different durations -- that
# the title/runtime pass cannot cross-match. All variants share one programme id
# in url_website, so a pass-1 Arte hit fans out to its variants by that exact id;
# each linked row inherits the anchoring hit's confidence.

ARTE_SENDER_RX = re.compile(r"arte\.[a-z]{2,}\Z", re.IGNORECASE)
ARTE_ID_RX     = re.compile(r"/(\d{4,}-\d{3}-[A-Z])(?:[/?#]|\Z)")


def is_arte_sender(sender) -> bool:
    """True for an Arte language-variant sender ('ARTE.DE', 'ARTE.FR', ...),
    case-insensitive; False for 'ARTE' alone or any non-Arte sender."""
    return bool(ARTE_SENDER_RX.match((sender or "").strip()))


def arte_video_id(url_website):
    """The Arte programme id shared across language variants (e.g.
    '116786-000-A'), taken from a url_website ('/videos/ID/...' or the older
    '/guide/xx/ID/...'); None when absent."""
    m = ARTE_ID_RX.search(url_website or "")
    return m.group(1) if m else None


def arte_anchor_ids(conn, matches) -> dict:
    """Of the pass-1 matches, those landing on an Arte sender, as video-id ->
    best confidence. The seed for the second pass; empty when no match is an
    Arte row."""
    anchors = {}
    for m in matches:
        r = conn.execute("SELECT sender, url_website FROM mediathek "
                         "WHERE mediathek_id=?", (m["mediathek_id"],)).fetchone()
        if not is_arte_sender(r["sender"]):
            continue
        vid = arte_video_id(r["url_website"])
        if vid is not None:
            anchors[vid] = max(anchors.get(vid, 0.0), m["confidence"])
    return anchors


def find_arte_links(conn, anchors, exclude_ids) -> list:
    """Fan each anchored video-id out to the Arte rows sharing it (the language
    variants the title pass missed), skipping already-matched ids. Each linked
    row inherits its anchor's confidence. Sorted by video-id, then mediathek_id."""
    if not anchors:
        return []
    start = time.perf_counter()
    groups, scanned = {}, 0
    for r in conn.execute("SELECT mediathek_id, clean_title, url_website FROM "
                          "mediathek WHERE sender LIKE 'ARTE.%' AND status IN ('1','2')"):
        scanned += 1
        vid = arte_video_id(r["url_website"])
        if vid in anchors:
            groups.setdefault(vid, []).append(r)
    seen = set(exclude_ids)
    out = []
    for vid in sorted(anchors):
        for r in sorted(groups.get(vid, []), key=lambda x: x["mediathek_id"]):
            if r["mediathek_id"] in seen:
                continue
            seen.add(r["mediathek_id"])
            out.append({"mediathek_id": r["mediathek_id"],
                        "clean_title": r["clean_title"],
                        "confidence": anchors[vid], "arte_video_id": vid})
    log.debug("find_arte_links: scanned %d ARTE rows -> %d links, %.2fs",
              scanned, len(out), time.perf_counter() - start)
    return out
