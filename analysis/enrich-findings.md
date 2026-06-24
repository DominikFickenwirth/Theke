# enrich iterative improvement -- findings log

Goal: make `theke enrich` classify in a TMDB-aligned way (Movie vs TV/Episode)
so `theke match` works. Focus: films, series, miniseries. Normal TV (news,
talk, magazines) is lower priority. Verified against the whole live DB
(`build/theke.db`, 709433 rows) via `analysis/_reenrich.py` -> `analysis/_enr.db`.

## Round 1 -- Mehrteiler "(n/m)" overrides Movie/None -> Episode  [DONE]

Problem: rows with a parsed `episode_count` (from a "(n/m)" multi-part marker)
were labelled Movie (topic 'Fernsehfilm' on 3Sat: "Eldorado KaDeWe (5/6)",
"Sisi (1/2)", "Unsere wunderbaren Jahre (3/3)") or left NULL (multi-part
documentaries). On TMDB these are TV miniseries -> Episode.

Fix: `episode_count is not None and category in (None, 'Movie')` -> Episode.
Clip/Event preserved (a trailer "Trailer: ... (1/2)" stays Clip). A standalone
film carries no "(n/m)", so real films are untouched.

Live-DB effect: None->Episode 4877, Movie->Episode 229, nothing else changed.

## Round 2 -- 4-digit (year) seasons: KEEP (verified against TMDB)  [NO CHANGE]

Question (from user): are 4-digit seasons (e.g. ZDF "S2025/E221") maintained in
TMDB? If not, drop them.

18340 rows carry a 4-digit season, all from daily ZDF news/magazines
(`heute journal`, `heute 19:00 Uhr`, `logo!`, `ZDF-Morgenmagazin`, ...).

Verified on TMDB (no API key -> checked the website):
- `heute journal` (tv/88969) uses **year-numbered seasons**: "Season 2024"
  exists, with running episode numbers (ep 70, 90, 319 by air date) -- exactly
  the ZDF "S2024/E70" scheme.
- (Tagesschau tv/94722 instead uses sequential seasons 1..75; not the 4-digit
  source here.)

Conclusion: 4-digit seasons are TMDB-aligned for these shows; the season AND the
running episode number can match. Dropping them would REDUCE matchability.
-> No change. The SE_A "4-digit season kept as-is" behaviour stays.

## Round 3 -- explicit Sxx/Exx overrides Movie -> Episode  [DONE]

Revises an earlier (pre-TMDB-goal) decision that "Reihe of TV films stays
Movie". Verified on TMDB: the German Krimi-/TV-film Reihen carrying an explicit
"(S01/E03)" are TV series, not movie collections -- Sarah Kohr (tv/202362),
Der Bozen-Krimi, Nord bei Nordwest, Praxis mit Meerblick, Der Zuerich-Krimi,
Maria Wern, Kommissar Van der Valk, ... So the explicit S/E (broadcaster series
notation) must win over a "Krimi/Fernsehfilm" metazeile.

Fix (unifies the episodic overrides): after the duration prior, an explicit
(season AND episode) or a Mehrteiler count -> Episode, overriding None/Movie/
Clip, but never a trailer (T, stays a clip) or a live Event. A standalone film
carries no marker and stays Movie.

Live-DB effect vs round-2 baseline: Movie->Episode 722 (all genuine TV series),
Episode->Clip 37 (all trailers that the old S/E-decisive rule had mislabelled
Episode -- now matching-safe Clip), Clip->Episode 25 (24 genuine multi-part
concert/web series, 1 borderline "24/25" season-designation misparse). No real
regressions.

Known remaining (minor): NPART "n/m" at end of title can misread a season
designation ("... 24/25") as episode 24 of 25. Single-digit-ish, low volume.

## Round 4 -- consistency: Sxx/Exx keeps Movie, only count -> Episode  [DONE]

New user goal: where TMDB is inconsistent, enrich must be INTERNALLY consistent
(film-reihen = Movie with series_name) and `theke match` compensates later.

Verified the inconsistency on TMDB: Sarah Kohr = tv/202362 (series), Inga
Lindstroem = tv/61385 (series), but Rosamunde Pilcher = individual /movie/
entries. Same kind of content (ZDF Herzkino / Krimi-Reihen of ~90 min standalone
TV films), modelled three ways. No per-row category can match all of them.

So round 3's "Sxx/Exx overrides Movie -> Episode" was wrong here and is revised:
- A Mehrteiler "(n/m)" count still -> Episode (serialized miniseries: Eldorado
  KaDeWe, Sisi). Overrides a Fernsehfilm label. (round 1 kept.)
- An explicit Sxx/Exx now fills only a None/Clip medium with Episode; it does
  NOT override a Movie label. Feature-length film-reihen (Der Bozen-Krimi, Nord
  bei Nordwest, Praxis mit Meerblick, Herzkino) stay Movie with series_name.

Why not a runtime gate (">60 min -> Movie") in enrich: tested against the live
DB, it pulls in long talk/news/sports with Sxx/Exx (Markus Lanz, maybrit illner,
Volle Kanne, Olympia, ...) -- 5781 rows, heavily contaminated. The robust
fiction discriminator is the explicit film label, which talk/news/sports never
carry. Talk/news use 4-digit (year) seasons; fiction uses small seasons -- but
sports Reihen (Olympia 1..19, Paralympics 4..12) also use small seasons, so a
season-size gate is not clean either. Hence: keep the film-label discriminator
in enrich, do the runtime-based Movie/Episode bridging in match.

Live-DB effect vs round-2 baseline: no Movie touched; only trailer cleanup
(Episode->Clip 37 trailers, Clip->Episode 25 multi-part concert/web series).

Residual inconsistency (deferred to match, see analysis/match-notes.md):
a film-reihe splits across Movie/Episode by whether each airing carried the
"Krimi/Fernsehfilm" metazeile (e.g. Sarah Kohr: 2 Movie vs 26 Episode).
series_name is consistent across all of them, so match can regroup.
