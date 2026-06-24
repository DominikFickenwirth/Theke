# -- match (phase 3, part 2: wish-first TMDB matching, movies) ----------------
# Drive from a canonical TMDB id: pull its title variants + year + runtime once,
# then search the enrich-normalized columns for matching mediathek rows. Pure
# helpers (normalize/score) are network-free and unit-testable; tmdb_movie is the
# only IO and goes through theke.http_get (monkeypatched in tests).
import difflib
import json
import re
from urllib.parse import urlencode

import theke   # for http_get, resolved at call time (avoids an import cycle)

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


# -- title similarity --------------------------------------------------------

def _is_token_run(short: list, long: list) -> bool:
    """True if `short` is a contiguous run of tokens inside `long`."""
    if not short or len(short) > len(long):
        return False
    return any(long[i:i + len(short)] == short for i in range(len(long) - len(short) + 1))


def _pair_sim(a: str, b: str) -> float:
    """Similarity of two normalized strings: exact, whole-token containment with
    enough coverage (so a short franchise name inside a long title does not
    over-score), else a character-level difflib ratio."""
    if a == b:
        return 1.0
    ta, tb = a.split(), b.split()
    if _is_token_run(ta, tb) or _is_token_run(tb, ta):
        if min(len(ta), len(tb)) / max(len(ta), len(tb)) >= SUBSTR_COVERAGE:
            return SUBSTR_SIM
    return difflib.SequenceMatcher(None, a, b).ratio()


def title_similarity(tmdb_titles, clean_title) -> float:
    """Best similarity of any TMDB title variant against clean_title, each
    compared in full and article-stripped form (handles missing/extra article)."""
    clean_forms = _forms(normalize(clean_title))
    best = 0.0
    for t in tmdb_titles:
        for tf in _forms(normalize(t)):
            for cf in clean_forms:
                best = max(best, _pair_sim(tf, cf))
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


def score_match(tmdb_meta, row) -> dict:
    """Score one mediathek row against TMDB metadata. Deterministic and
    explainable: title is the gate, year a near-hard gate, runtime a soft
    confirmer. Returns confidence + the breakdown that fed it."""
    title_sim = title_similarity(tmdb_meta["titles"], row["clean_title"])
    rejected = title_sim < TITLE_FLOOR

    my, ry = row["year"], tmdb_meta.get("year")
    if my is not None and ry is not None:
        year_delta = abs(my - ry)
        if year_delta > YEAR_TOLERANCE:
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
    data = json.loads(theke.http_get(url).decode("utf-8"))

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


def tmdb_tv(cfg, tmdb_id, season, episode) -> dict:
    """Fetch a TMDB series' metadata for episode matching, in two calls: the
    series (name + original + DE alternative titles, the gate) and the episode
    (name + runtime + air year, plus its translated names as soft confirmers).
    TV alternative_titles live under 'results' (movies use 'titles')."""
    sp = urlencode({"api_key": cfg.tmdb_api_key, "language": cfg.tmdb_language,
                    "append_to_response": "alternative_titles"})
    s = json.loads(theke.http_get(f"{cfg.tmdb_api_url}/tv/{tmdb_id}?{sp}").decode("utf-8"))
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
    e = json.loads(theke.http_get(url).decode("utf-8"))
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


# -- candidate search --------------------------------------------------------

def find_matches(conn, tmdb_meta, min_conf) -> list:
    """Scan the movie subset, score each row, return the matches (confidence >=
    min_conf, not rejected) sorted by confidence desc, then mediathek_id."""
    rows = conn.execute("SELECT mediathek_id, clean_title, year, duration, flags "
                        "FROM mediathek WHERE category='Movie' AND status='1'")
    out = []
    for r in rows:
        if r["flags"] and "T" in r["flags"]:   # trailers are never the wanted film
            continue
        s = score_match(tmdb_meta, r)
        if s["rejected"] or s["confidence"] < min_conf:
            continue
        out.append({"mediathek_id": r["mediathek_id"], "clean_title": r["clean_title"],
                    "confidence": s["confidence"], "title_sim": s["title_sim"],
                    "year_delta": s["year_delta"], "runtime_delta": s["runtime_delta"]})
    out.sort(key=lambda m: (-m["confidence"], m["mediathek_id"]))
    return out


def find_episode_matches(conn, tv_meta, min_conf) -> list:
    """Scan the Episode subset for the wanted (season, episode), score each row,
    return the matches (confidence >= min_conf, not rejected) sorted by
    confidence desc, then mediathek_id."""
    rows = conn.execute(
        "SELECT mediathek_id, clean_title, series_name, season, episode, duration, "
        "flags FROM mediathek WHERE category='Episode' AND status='1' "
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
    groups = {}
    for r in conn.execute("SELECT mediathek_id, clean_title, url_website FROM "
                          "mediathek WHERE sender LIKE 'ARTE.%' AND status='1'"):
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
    return out
