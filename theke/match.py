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

    rt, dur = tmdb_meta.get("runtime"), row["duration"]
    if rt and dur:
        dur_min = dur / 60
        runtime_delta = int(round(dur_min - rt))
        if dur_min < rt * RUNTIME_FLOOR_RATIO:   # clip/trailer/excerpt, not the film
            rejected = True
            runtime_factor = 0.0
        else:
            rel = abs(dur_min - rt) / rt
            runtime_factor = 1.0 if rel <= RUNTIME_TOLERANCE else RUNTIME_PENALTY_FACTOR
    else:
        runtime_delta = None
        runtime_factor = 1.0

    confidence = 0.0 if rejected else round(title_sim * year_factor * runtime_factor, 3)
    return {"confidence": confidence, "title_sim": round(title_sim, 3),
            "year_delta": year_delta, "runtime_delta": runtime_delta,
            "rejected": rejected}


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


# -- candidate search --------------------------------------------------------

def find_matches(conn, tmdb_meta, min_conf) -> list:
    """Scan the movie subset, score each row, return the matches (confidence >=
    min_conf, not rejected) sorted by confidence desc, then mediathek_id."""
    rows = conn.execute("SELECT mediathek_id, clean_title, year, duration, flags "
                        "FROM mediathek WHERE category='Movie'")
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
