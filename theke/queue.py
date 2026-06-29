# -- queue (phase 5: download-queue dedup) ------------------------------------
# Pure, network-free selection: collapse the many mediathek rows of one tmdb_id
# (senders, SD/HD, languages, repeats) to the minimal download set. One video is
# downloaded per shared source; same-source other-language picks become audio
# only. Remux flags: 'AV' = need audio+video, 'A' = audio only, 'V' = video only
# (reserved -- not produced by the default policy). Accessibility flags on the
# source row steer the choice: 'A'/'E' audio (audio-description / simplified
# speech) is never used as audio; 'U'/'S' video (burned-in subtitles / sign-
# language inset) is avoided as the video source. DB I/O lives in __init__.py.

from theke.match import arte_video_id

_RES_RANK = {"HD": 2, "SD": 1, "LQ": 0}


def resolution_of(row) -> str:
    """The best video tier a row offers: 'HD' (hd url), else 'SD' (main url),
    else 'LQ' (only the small url)."""
    if row.get("url_video_hd"):    return "HD"
    if row.get("url_video"):       return "SD"
    if row.get("url_video_small"): return "LQ"
    return "SD"


def _effective_language(row, original_language):
    """A row's spoken language, resolving the 'ov' (Originalversion) marker to the
    film's actual original language (from TMDB)."""
    return original_language if row.get("language") == "ov" else row.get("language")


def _bad_audio(row) -> bool:
    """Audio unusable as a clean source: an audio-description ('A') or simplified-
    speech ('E') variant. Such rows are dropped before grouping -- every pick
    contributes audio, so a flagged one must never be selected."""
    f = row.get("flags") or ""
    return "A" in f or "E" in f


def _clean_video(row) -> bool:
    """Video free of burned-in overlays: no burned-in subtitles ('U') and no
    sign-language interpreter inset ('S'). The anchor (video source) prefers a
    clean row even at the cost of resolution."""
    f = row.get("flags") or ""
    return "U" not in f and "S" not in f


def _pick_key(row):
    """Rank one row within its language group: clean video first (no burned-in
    'U'/'S'), then HD, then a real subtitle track (url_subtitle filled -- NOT the
    burned-in 'U' flag), then larger size, later date, then mediathek_id.
    Greatest wins."""
    return (_clean_video(row), resolution_of(row) == "HD",
            bool(row.get("url_subtitle")),
            row.get("size_mb") or 0, row.get("date") or "", row["mediathek_id"])


def _shares_video(a, b) -> bool:
    """True when two rows carry the same video stream: same Arte programme id, or
    -- lacking that -- an identical duration (a plain repeat in another language)."""
    va, vb = arte_video_id(a.get("url_website")), arte_video_id(b.get("url_website"))
    if va is not None and va == vb:
        return True
    return a.get("duration") is not None and a.get("duration") == b.get("duration")


def select_downloads(rows, languages, original_language) -> list:
    """Deduplicate the rows of one tmdb_id into the download set. Keep only
    whitelisted languages (`languages`, also the preference order); drop audio-
    description / simplified-speech rows ('A'/'E' flags) so they never supply
    audio (a language with no clean audio is left out); pick the best row per
    language; the cleanest-then-best-resolution pick anchors the video (a clean
    video without burned-in 'U'/'S' overlays wins even over higher resolution);
    same-source other-language picks become audio-only. Returns the anchor first,
    then the rest in preference order, each as
    {mediathek_id, language, resolution, remux}."""
    groups = {}
    for r in rows:
        lang = _effective_language(r, original_language)
        if lang not in languages or _bad_audio(r):
            continue
        groups.setdefault(lang, []).append(dict(r, _lang=lang))
    if not groups:
        return []

    picks = [max(g, key=_pick_key) for g in groups.values()]
    anchor = sorted(picks, key=lambda p: (not _clean_video(p),
                                          -_RES_RANK[resolution_of(p)],
                                          languages.index(p["_lang"]),
                                          p["mediathek_id"]))[0]
    rest = sorted((p for p in picks if p is not anchor),
                  key=lambda p: (languages.index(p["_lang"]), p["mediathek_id"]))

    out = []
    for p in [anchor] + rest:
        remux = "AV" if p is anchor or not _shares_video(p, anchor) else "A"
        out.append({"mediathek_id": p["mediathek_id"], "language": p["_lang"],
                    "resolution": resolution_of(p), "remux": remux})
    return out
