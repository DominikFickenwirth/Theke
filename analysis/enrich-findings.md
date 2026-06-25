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

## Round 5 -- fiction-topic allowlist lifts NULL -> Movie  [DONE]

The documented "feature-length crime fiction stuck in NULL" loss (Tatort etc.)
is recovered IN enrich (not deferred to match): per the user's convention
(film-reihen = Movie with series_name), a Reihe should be one consistent medium.

Many airings of a crime-/TV-film Reihe carry NO film metazeile, so they fell to
the duration prior and, being feature-length (>1800s), landed in NULL. Sampled
the Tatort NULL bucket: 579 rows 60-90 min, 89 rows >90 min, 4 rows 30-60 min --
all genuine ~90 min crime films, no making-of/interview noise.

Fix: a FICTION_TOPICS allowlist; when category is still NULL after all other
passes and the topic is a known fiction Reihe (and not a trailer), -> Movie,
kat_src 'topic-fiction' (confidence 0.5). Fires ONLY on NULL, so it never
overrides Episode (Sxx/Exx) or Clip (trailer) -- the per-airing scatter inside a
Reihe is left for match to regroup via series_name (unchanged from round 4).

Allowlist derivation (data-driven, the safety net): every topic in the set
already produces >=8 metazeile-labelled Movie rows in the live DB. A film label
("Spielfilm/Fernsehfilm/Krimi/...") never appears on talk/news/sports, so this
threshold cannot admit non-fiction. 44 topics qualify (Tatort 511 Movie,
Polizeiruf 110 42, Der Usedom-Krimi 85, Donna Leon 60, Praxis mit Meerblick 149,
the named Krimi-/Herzkino-Reihen, plus a few generic film SLOTS like "Filme im
Ersten"). Configurable: config['fiction_topics'] (casefolded) is unioned with the
built-in default, since the brand list changes over time.

Live-DB effect: exactly 1328 transitions, ALL NULL->Movie, no other transition
(verified by joining the pre/post re-enrich snapshots on mediathek_id). Movie
8723 -> 10051, NULL 112254 -> 110926. Zero regressions.

Known remaining (minor): the generic film SLOTS in the set (Filme im Ersten,
FilmMittwoch im Ersten, Spielfilm in 3sat, ...) keep series_name = slot name; a
later cleanup could route them via FORMAT_TOPICS for a NULL series_name. Lifted
"Dokumentarfilmzeit" rows get no Documentary genre (the metazeile ones do).

## Round 6 -- companion clips: trailer / interview / making-of -> Clip  [DONE]

Probing the Movie bucket for false positives found short companion pieces filed
as Movie/Episode (the work's medium leaked onto a clip ABOUT the work):
- TRAILERS in a film-rubric topic: "Trailer: Gladbeck" (Filme in der ARD ->
  Movie via FORMAT_TOPICS) stayed Movie. The T flag did not demote them.
- ARTE short-film INTERVIEWS: under "Kino - Kurzfilme"/"Cinéma - Courts metrages"
  (ARTE_SUB -> Movie) the companion "Interview mit X - Regisseurin"/"Rencontre
  avec ..." rows became Movie (149 rows).
- MAKING-OF segments ("Making of - Folge 2", ZDF Filme) -> Movie/Episode.

The T flag is NOISY on its own: it also matches long-form shows that merely
mention a trailer in the title (Trailer.AT 25-min magazine, Cinema Strikes Back
podcast 90 min+, ESC "Vorschau" 3.4 h). So a blanket "T -> Clip" is wrong.

Fix (duration-gated, so feature films are never touched):
- T flag AND <300s -> Clip (a short trailer).
- title is a making-of (M flag) or interview/Rencontre/Entretien/Gespräch (I
  flag) AND <900s -> set the flag and, if Movie/Episode, demote to Clip.
The gate is the safety: a 2-h drama titled "Interview mit einem Vampir" stays
Movie (verified DB-wide: every Movie<900s companion hit was a real companion,
no feature film). New flags I (interview) and M (making-of); trailer stays the
existing T flag (a flag, not a category -- user decision).

Live-DB effect: trailer demotion 166 Episode + 23 Movie -> Clip; companion
demotion 1117 Episode + 151 Movie -> Clip; flags M=259, I=1160. No other
transition. Movie 10051 -> 9877, Clip grows accordingly.

## Round 7 -- mid-title "n/m - Subtitle" multi-part marker -> Episode  [DONE]

`enrich audit` flagged episodic-unparsed rows: nature-doc miniseries written
"Titel n/m - Untertitel" (Wunderwelt Schweiz 3/4 - Das Tessin, Die Sprache der
Tiere 1/5, Straende Europas 1/6). The "n/m" sits MID-title before a " - "
subtitle; NPART is end-anchored and PART needs parens, so both missed it (42
rows, 30 fell to NULL via the duration prior).

Fix: a MIDPART regex `(?<![\d./])(?<!\d\s)(\d{1,2})/(\d{1,2})\s+(?=[-–]\s)` with
an n<=m<=20 guard. The "n/m " is removed from the title (keeping "Titel -
Untertitel") and feeds episode/episode_count -> Episode via the Mehrteiler rule.

GUARD (user-raised, now tested): the `(?<!\d\s)` rejects a MIXED fraction
"8 1/2 - ..." (Fellini) -- a whole number + space before the n/m is a runtime,
not a part. Without it the film became an Episode. Verified DB-wide: 0 remaining
mixed-fraction matches; exactly 30 NULL->Episode, 42 rows gain an episode number.

Process note (user feedback): always add a false-positive guard test for a new
regex/rule, proactively -- do not wait for the counter-example. Re-checked all
three of this session's rules DB-wide afterwards: fiction lift had 0 non-film
contamination (the 3 "spezial" hits were real Tatort episode titles), trailer
and companion demotions had 0 false matches.

# Part 2 -- clean_title / series_name cleanup

New goal (user): clean up clean_title and series_name. Common noise: the words
"Folge"/"Episode"/"Staffel"/"Teil" leaking into the title; series_name carrying
slot content; plus actively hunting further ugliness. Same method (verify against
`build/theke.db` via `analysis/_reenrich.py`), TDD, a guard test per rule.

## Round 8 -- ", Teil N" / Staffel-Folge comma residue in clean_title  [DONE]

Probing clean_title for residue found the TEIL/STAFFOLGE strips leaving dangling
commas:
- "<Title>, Teil N: <Subtitle>" -> "<Title>,: <Subtitle>" (the ", " before
  "Teil" and the ":" after were not consumed): "Tauchwandern am Bodensee,: Quer
  durch den Untersee", "Making-of,: Die Stunts", the Zirkus-Charles-Knie /
  Entrümpler series (19 rows with a ",:" interior).
- "<Title>, Teil N" at the end -> trailing comma ("Jahresrückblick vom
  28.12.2023,", "... voller Länge,").
- "<Title>, Staffel N, Folge M" -> STAFFOLGE strip leaves a trailing comma
  ("Der Haustier-Check,").

Fix: TEIL's leading char class gains a comma (`[-–(,]?`) so ", Teil N" is removed
whole (keeping a ": Subtitle"); the final clean_title strip set gains `,` and `·`
so any remaining trailing comma/middot is trimmed.

GUARD: an ordinary comma with no Teil/Staffel marker stays ("Stadt, Land, Fluss"
untouched) -- the comma is only consumed when adjacent to a "Teil N" marker, and
the final strip only trims at the ends.

Live-DB effect: ",:" interiors 19 -> 0, trailing commas -> 0, trailing middots
-> 0. category distribution unchanged (clean_title-only round).
