# CLAUDE.md -- Theke

> Context file for Claude Code: architecture, domain, conventions. Keep in sync
> when a core assumption changes.
>
> ENCODING: every repo text file (this one too) is UTF-8 but CP-1252-only in
> content. No emojis/Unicode arrows/box-drawing; draw arrows as `-->`, `<->`,
> `^`, `V`. Repo text only (source, config, docs) -- NOT runtime data: film-list
> metadata is UTF-8 and may exceed CP-1252, stored as-is. Set
> `PYTHONIOENCODING=utf-8` when printing it to the Windows console (else
> UnicodeEncodeError).

## Overview

**Theke** (from Media-*thek*) is a self-hosted media manager: it acquires German
public-broadcaster content and files it into a **Jellyfin** library. The only
source is the public Mediatheken via the **MediathekView film list** (a plain
download from liste.mediathekview.de).

**One home for the logic:** the **Python CLI** holds *all* logic and is the only
thing on the NAS; the only UI is a **web UI** (last phase) that holds no logic and
drives the CLI through a **REST API**. No desktop GUI.

## Phases (implementation order)

Each phase is usable on its own. Phases 1-2 are planned; **3+ is tentative** --
don't over-engineer ahead. Middle-phase ordering: build the full **manual
acquisition path** as one vertical slice (pick by id --> queue --> approve -->
download --> remux), then add the **wishlist** as automation on the same
machinery.

1. **Scaffolding** -- config, DB layer, CLI skeleton.
2. **Film list mirror** -- download and import into SQLite table `mediathek`,
   keyed by `mediathek_id` (MediathekView's identity: SHA-256 over
   sender+thema+url+website, each UTF-16LE). Follows MV's update logic: check the
   server list id, skip if unchanged; else apply the diff list
   (`Filmliste-diff.xz`) when usable, else full download (`--force` forces full).
3. **Classify + enrich + match** (partly done). **classify** (done): extract
   structured metadata (clean title, series/season/episode, category, year,
   country, language, flags) from free text, flip `status` '0' -> '1'. classify
   is local and cheap (no API) and is the search base for everything below.
   **enrich + match** (to come) are **demand-driven, not bulk**: TMDB IDs are
   resolved only for what is actually wanted (wishlist entries, manual picks), and
   a resolved ID is cached on the row so the enriched part of the catalog grows
   with use instead of hammering TMDB for hundreds of thousands of unwanted rows.
   match runs **wish-first** (a canonical TMDB ID --> matching mediathek rows),
   not catalog-first. A refresh must preserve existing ID assignments; vanished
   entries are kept (the mirror only grows/updates, never deletes).
4. **Review queue + gate** -- staging of proposals/picks + approval gate. Still
   no download. A manual pick is a `mediathek_id` (found via DBBrowser for now)
   fed to a CLI command that creates a queue entry; browsable search is a web-UI
   concern (last phase).
5. **Downloader** -- plain HTTP download of approved items (DE/EN separate,
   subtitles), manual via CLI.
6. **Remuxer** -- FFmpeg pipeline into the Jellyfin folders. **Manual path
   complete here.**
7. **Wishlist** -- entries by TMDB ID, auto availability checks feeding the same
   queue. Pure automation on phases 4-6.
8. **Docker + NAS deployment** -- containerize, deploy (smoke test, manual).
9. **Scheduler** -- in-app scheduler; runs unattended.
10. **Jellyfin indexer** -- parse NFO files, cache the library. Needed for 11.
11. **Quality upgrades + series completion** -- detect higher resolutions /
    missing episodes (needs the indexer).
12. **Web UI** -- the only UI: review dashboard, settings, history, and read-only
    browse/search over the catalog (REST API to the CLI).

## The gate (optional human-in-the-loop)

Review is a **configurable gate**, not a hard rule:

1. **Proposal** -- Theke generates match candidates + confidence (or the user
   picks via search) into the review queue.
2. **Approval** -- user inspects/corrects, approves or deletes; only then does
   download start. Set in config (optional auto-approve confidence threshold).

**Side-effect boundary:** DB-only operations run automatically; anything that
writes/deletes files in the library sits behind the gate.

- Automatic (DB-only): mirror, classify, enrich, match, wishlist checks,
  proposal generation. Read-only browse is external for now (DBBrowser).
- Gated (touches the library): starting downloads, replacing files.
- After approval, automatic: download, remux, placement.
- Quality upgrades are most sensitive (they delete files): proposal automatic,
  replacement only after approval -- even when the gate is otherwise relaxed.

## Architecture / pipeline

```
liste.mediathekview.de --> [Mirror] --> mediathek table <-- [ID Enricher (TMDB), on demand]
                                              |
Jellyfin (NFO files) --> [Indexer] -----------+  (phase 10)
                                              |
                                              V
                                         [Matcher]
                                              |
   [Manual pick] (CLI, by id) ------------->  +  <------------ [Wishlist] (auto)
                                              |
                                              V
                                         queue table
                                              | (after approval, if required)
                                              V
                              [Downloader (HTTP)] --> [Remuxer (FFmpeg)]
                                              |
                                              V
                                   NAS / Jellyfin media folders
```

Two producers feed the queue (a manual pick by id, the wishlist). Stages are
decoupled and idempotent -- each persists its state to the DB, so aborts/re-runs
are safe and every stage is callable on its own.

## Data model

Single SQLite file `theke.db`. Field lists indicative.

- **mediathek** -- film-list mirror, refreshed periodically. `status` is one char
  ('0' new, '1' classified). classify (phase 3) fills `language` +
  clean_title/series_name/season/episode/episode_count/category/year/country/
  flags/classify_confidence and flips status to '1'; tmdb_id/match_confidence
  are filled later by enrich+match **on demand** (only for rows a wishlist entry
  or manual pick resolves) and cached here. All phase-3 columns survive refreshes.
  `status, mediathek_id, sender, topic, title, description, date, duration,
  size_mb, url_video, url_video_small, url_video_hd, url_subtitle, url_website,
  url_history, geo, language, tmdb_id, match_confidence, clean_title,
  series_name, season, episode, episode_count, category, year, country, flags,
  classify_confidence`
  Plus a `meta` key/value table (filmliste_id, filmliste_created).
- **queue** -- review queue + download record in one. Lifecycle
  `proposed -> approved -> downloading -> done / failed / cancelled`.
  `status, id, mediathek_id, source (match/pick/wishlist), language,
  confidence, target_path, resolution, error, created_at, updated_at`
- **wishlist** (phase 7) -- `tmdb_id, type, season, episode, status, added_at`
- **library** -- from MP4s + their NFO files (XML already carries the TMDB ID, so
  matching here is trivial). Replaces `wishlist` in phase 10 (wishlist entries
  become library entries with status="wishlist").
  `status, type (movie/episode), title, year, tmdb_id, season, episode,
  path, resolution, languages, added_at`

## Stage details

**Manual pick (phase 4):** until the web UI exists, browsing the catalog is done
externally (DBBrowser for SQLite, read-only). A pick is a `mediathek_id` handed
to a CLI command that creates a queue entry and triggers TMDB resolution **on
demand** for that row (cached afterwards). Same queue the wishlist later feeds.
The browsable search itself (over the classify-normalized columns, indexes / FTS5
to evaluate) lands with the web UI (last phase).

**Matching (core problem):** the hard part is fuzzy-matching messy German
film-list free text against a canonical title -- that cost is unavoidable; the
design only controls *when* and *how often* it runs. Theke runs it **wish-first
and lazy**: drive from a canonical TMDB ID (a wishlist entry or a resolved manual
pick), take that entry's title variants (DE + original + alternates) + year + (for
series) the season/episode list from TMDB, and search the classify-normalized
columns for matching mediathek rows -- instead of bulk-enriching the whole catalog
catalog-first (MV-row --> which ID?). Confirm a candidate with duration, release
year, optional synopsis; compute a confidence score, below threshold -> manual
review (goal: few false positives). For series, season/episode matching is far
more reliable than title alone. One TMDB ID maps to **many** mediathek rows
(senders, SD/HD, repeats); the resolved IDs are cached on those rows, the queue
dedups on target (tmdb id + language + resolution). AI (embeddings/classifier) or
web search is a **later** fallback only if the classic hit rate is poor.

**Download (phase 5):** source is the film list (direct media URLs). Transport:
plain HTTP GET for mp4/mp3/m4v/flv/m4a streamed to disk; ffmpeg for m3u8 (like
MV's "Programmset"). HTTP is needed in exactly three places: film list, TMDB REST
API, media download. DE/EN downloaded **separately** when present; subtitles
DE/EN when present (external subtitle providers later). Failed downloads retry,
status in DB, no silent loss.

**Remux convention (important)** -- never store **duplicate video**:

- One language only -> file stored unchanged.
- DE **and** EN with equal-length video streams:
  - The **original-language** MP4 stays the main file, unchanged. Original
    language comes from the TMDB match (`original_language`); for these
    broadcasters it is almost always German -> German is the fallback.
  - From the secondary file, FFmpeg extracts **only the audio** as an external
    track next to the video, named `<basename>.<lang>.<codec>` (e.g.
    `Tatort (2024).en.aac/ac3/m4a/mka`); its video is discarded. Jellyfin picks
    the external audio up as an extra track.
- Verify on implementation: exact codec/extension; that Jellyfin maps the
  external audio with this naming; set correct **language tags** on remux (else
  Jellyfin mislabels); check naming against current Jellyfin docs.

**Quality upgrades (phase 11):** track current resolution (from the Jellyfin
cache); a higher resolution in the film list -> upgrade proposal. After approval:
download + verify the new file, then swap **atomically** (library never broken).

**Wishlist (phase 7):** entries by TMDB ID, periodically checked against the
mirror; a hit -> download proposal in the queue (subject to the gate). Just the
automated producer reusing the manual-path machinery (phases 4-6); for movies and
(phase 11) missing episodes.

## Tech stack

- **CLI (Python)** holds *all* logic -- stages config, mirror, classify, enrich,
  match, review, download, remux, run (`config`/`mirror`/`classify` exist; the
  rest land with their phases). One command per stage; the only thing on the NAS
  and the Docker entrypoint. A stage with several modes nests them as sub-actions:
  `classify run` writes the columns; `classify report`/`audit`/`show`/`dist` are
  read-only inspection tools for iterating the heuristics (per-sender coverage
  incl. `--by-confidence`/`--diff`, findings scan, row sampler, value
  distribution). **Machine-readable mode** (`--json`, stable exit codes, stable
  grammar) so the REST layer / web UI can drive and parse it.
  - **stdout vs stderr:** stdout carries only the result (the single JSON object
    in `--json`); progress/diagnostics go to **stderr** as plain text, so a long
    stage stays visible without polluting the parseable result.
- **Web UI (phase 12):** the only UI -- review dashboard, settings, history, and
  read-only browse/search over the catalog. Talks to the CLI through a **REST
  API** (REST over the `--json` layer); holds no logic. May later be wrapped
  (Electron/Tauri) to run on the PC against the same API -- not formulated
  further yet.
- **DB:** one SQLite file. The **CLI is the only writer**; other tools may open it
  **read-only** (DBBrowser, the web UI). **Single-writer:** at most one process
  writes at a time -- during a scheduled `theke run` no other CLI write runs;
  concurrent readers are fine (WAL).
- **Video:** FFmpeg as an external process (remux/extraction) -- the only
  external runtime dependency besides Python; must be present on NAS / in the
  image.

## Scheduler (in-app, phase 9)

- `theke run` loops the stages at configured intervals in pipeline order
  (mirror -> enrich -> match -> wishlist check -> ...). Part of the app, not the
  deployment.
- **One entrypoint, two modes:** a single stage once (`theke <stage>`) or the
  full loop (`theke run`). One-shot exists from the start so the image ships and
  smoke-tests before the loop lands; the entrypoint never changes.
- **Runs locally without Docker** -- same code/behavior on the dev PC as in the
  container (Docker is packaging only). No host cron / Compose scheduling: that
  would put config outside the app and make local no-Docker debugging
  impossible.

## Project structure

Flat on purpose -- no folder sprawl; modules may grow long rather than split into
many tiny files.

```
Theke/
+-- theke/            Python package (all logic)
|   +-- __init__.py   config, DB layer, mirror, CLI
|   +-- classify.py   metadata extraction (phase 3, part 1)
+-- pyproject.toml    package + console-script `theke`, dependencies
+-- tests/            pytest suite (test_theke.py, test_classify.py)
+-- webui/            Web UI + REST API (phase 12, not present yet)
+-- docker/           CLI image entrypoint (phase 8+, not present yet)
+-- analysis/         temporary review notes + scratch scripts (committed, not shipped)
+-- CLAUDE.md
+-- README.md
```

Build analysis scripts under `analysis/` whenever useful (e.g. probing
`build/theke.db`); `_`-prefix throwaway tooling.

## Development and deployment

- **Dev:** locally on Windows, CLI in a Python **venv**; everything native,
  scheduler included -- no Docker for dev/debug.
- **Delivery:** CLI as a **Docker container** on the **NAS** (phase 8; runs
  `theke run` from phase 9). The web UI (phase 12) is served against the same CLI
  over REST; packaging it for the PC (Electron/Tauri) is undecided and deferred.
- **All paths/secrets via CLI params or config** (media folders, DB path, TMDB
  key) -- nothing hard-coded. Precedence: **CLI params > config file**; config is
  JSON (`theke.json` by default). Docker (phase 8) adds env vars as a third
  source.

## Coding Guidelines

ALWAYS:
- **TDD.** Tests first (incl. edge cases), watch them fail -> commit -> write
  code until green -> commit (2 commits per step). Refactor while green (1
  commit).
- **Green before commit.** Run the suite; never commit failing or skipped tests.
- **When in doubt, ask.** Ambiguous requirement or a genuinely user-owned
  decision -> ask, don't guess.
- **Minimal dependencies.** Justify every one; prefer the stdlib. FFmpeg is the
  only runtime dependency besides Python; HTTP only in three places (film list,
  TMDB API, media download).
- **Compact code.** Let it speak; one short comment per unit, no comment blocks
  inside functions.
- **Files may grow long** -- a few clear longer units over many tiny ones.
- **Thin command handlers.** `cmd_*` orchestrate (parse args, wire stages, emit
  JSON); real work lives in helpers taking plain inputs and returning values
  (unit-testable without a CLI invocation). Logic in the CLI; the UI stays a
  shell.
- **Idempotent stages,** re-runnable; state in the DB, not memory.
- **CP-1252 content only** in every text file (see encoding rule on top).
- **Python formatting:**
  - Section dividers, label first, dashes to col 80: `# -- config ----...`.
  - Runs of parallel calls (e.g. `add_argument`) stay one per line (long lines
    fine), arguments vertically aligned across the run.
  - Blank lines between logical blocks in longer functions.
  - Unused unpacking slots are a bare `_`.
- **venv,** never global pip.
- **Tests:** expected values **hard-coded / pre-calculated**, never recomputed in
  the test (paste the literal with a note how it was derived). Only exception:
  relational assertions needing no concrete value (e.g. "id of A != id of B").
- **Language:** comments and names in English; README.md is the only German file.

KEEP IN MIND:
- All paths, URLs, settings **configurable**, never hard-coded.
- **Command wiring** in two places: a subparser in `build_parser` (flags inline)
  and a `case` in `main`. Global options (e.g. `--config`) and the command name
  read directly off `args`; subcommand flags only in that command's handler (e.g.
  `args.force` in `cmd_mirror`).

NEVER:
- Describe problems that no longer exist (or never applied). State the design as
  it is; don't justify a choice against a non-problem.
