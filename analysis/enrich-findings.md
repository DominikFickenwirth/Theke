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
