# Notes for `theke match` (phase 4) -- TMDB inconsistencies + bridging logic

Context: `theke enrich` classifies each mediathek row into a single internally
CONSISTENT medium (Movie / Episode / Clip / Event / None). TMDB itself is NOT
consistent for German TV-film *Reihen*, so enrich cannot mirror TMDB per-row;
`match` must bridge the gap. This file collects what `match` needs to handle.

## The core problem: TV-film Reihen are modelled inconsistently on TMDB

A "Reihe" is a branded run of feature-length (~90 min) TV films. TMDB models
them three different ways, with no rule:

- as a **TV series** with seasons/episodes:
  - Sarah Kohr = tv/202362
  - Inga Lindstroem = tv/61385
- as **individual movies** (one /movie/ per film, sometimes grouped in a TMDB
  *list*, not a series):
  - Rosamunde Pilcher (e.g. movie/219894 "...: September")
- mixed / partial within the same brand (some entries as movies, the umbrella
  also present as a list).

Same content, different modelling. So whatever single category enrich assigns,
it will be "wrong" for some TMDB ids.

## What enrich does (the consistent convention)

- Feature-length fiction that carries a film label ("Spielfilm/Fernsehfilm/
  Krimi/..." metazeile or a film topic) -> **Movie**, with `series_name` set
  from the topic (e.g. "Der Bozen-Krimi", "Sarah Kohr", "Praxis mit Meerblick").
- A serialized Mehrteiler/miniseries "(n/m)" -> **Episode** (Eldorado KaDeWe,
  Sisi, Unsere wunderbaren Jahre).
- A normal serialized series with Sxx/Exx and no film label -> **Episode**
  (The Rookie, Doku-Reihen, web series).
- 4-digit (year) seasons are kept: TMDB uses them for daily ZDF shows
  (heute journal Season 2024 with running episode numbers). See enrich-findings.

### Known residual inconsistency (enrich cannot fix per-row)

Within ONE film-reihe, broadcaster labelling varies row to row: only some
airings carry the "Krimi/Fernsehfilm" metazeile. So a series splits, e.g.:
- "Sarah Kohr": ~2 rows Movie (labelled) vs ~26 rows Episode (Sxx/Exx, no label)
- "Der Bozen-Krimi": 71 Movie vs 4 Episode vs 2 Clip
`series_name` IS set consistently across all of them, so match can regroup.

Worst offenders are the famous crime-film series, scattered across all four
media by per-airing labelling (live DB):
- "Tatort":         672 None / 511 Movie / 181 Episode / 166 Clip
- "Polizeiruf 110": 140 None /  42 Movie /  33 Episode /  21 Clip
- "Krimi und Thriller" slot None bucket = 192 feature-length crime films
  (Donna Leon, Usedom-Krimi, Blind ermittelt, Mordkommission Istanbul, ...).
The None rows are the real loss: feature-length crime fiction invisible to any
category-gated search. enrich cannot lift them per-row -- a 88-min None with no
label is indistinguishable from a long talk show without cross-row context.

## What `match` must do (bridging logic)

1. **Search across the Movie/Episode boundary for feature-length fiction.**
   When the resolved TMDB id's runtime is film-length (episodes > ~60 min, or a
   movie), search BOTH `category='Movie'` AND `category='Episode'` rows --
   do not gate on category alone.
   - TMDB *series* with long episodes (Sarah Kohr): the wanted rows may be filed
     Movie (labelled airings) or Episode (unlabelled). Match both.
   - TMDB *movie* of a reihe (Rosamunde Pilcher): the wanted rows may be filed
     Movie or Episode. Match both.

2. **Use `series_name` to regroup a split reihe.** All airings of one reihe share
   `series_name`; the per-row category split is noise. For a series id, candidates
   = rows with matching `series_name` (+ Sxx/Exx when present), regardless of
   category.

3. **Short episodes stay category-gated.** When the TMDB episode runtime is short
   (< ~60 min, normal serialized series like The Rookie), the existing
   `category='Episode'` + exact (season, episode) gate is correct and precise --
   keep it; do not widen to Movie there (avoids matching unrelated films).

4. **Daily/year-season shows**: 4-digit season + running episode can match TMDB
   directly (heute journal). Low priority (normal TV), but harmless.

## Open question for the user (deferred)

If per-row category splits inside a reihe turn out to hurt match precision more
than the bridging logic can absorb, an alternative is a **bulk series-level
consistency pass** in enrich: for each `series_name`, if any row carries a film
label, promote the whole series to Movie (talk/news/sports never carry a film
label, so they are untouched -- except sports Reihen like "Olympia" which use
small season numbers; those would need an explicit exclude). Not done now: the
user placed the compensating logic in `match`.
