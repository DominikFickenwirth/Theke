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
public-broadcaster content and files it into a movie library (e.g. Jellyfin). The
only source is the public Mediatheken via the **MediathekView film list** (a plain
download from liste.mediathekview.de).

## Phases (implementation order)

- Phases are decoupled and idempotent -- each persists its state to the DB, so
  aborts/re-runs are safe and every phase is callable on its own.
- Non-implemented phases are **tentative** -- don't over-engineer ahead.
- Middle-phase ordering: build the full **manual acquisition path** as one vertical slice
  (fetch --> enrich --> pick by id --> queue --> approve --> download --> remux --> copy),
  then add the **wishlist** as automation on the same machinery.

1. **Scaffolding** (done) -- config (`theke.json`), DB layer (`theke.db`), CLI skeleton.
2. **Film list fetch** (done) -- `theke fetch` downloads and imports the MediathekView
   film list into SQLite table `mediathek`, keyed by `mediathek_id`. Follows MV's
   update logic: check the server list id, skip if unchanged; else apply the diff list
   (`Filmliste-diff.xz`) when usable, else full download (`Filmliste-fill.xz`).
3. **Enrich** (done) -- `theke enrich` extracts structured metadata (clean title,
   series/season/episode, category, year, country, language, flags) from free text,
   flips `status` '0' -> '1'. It is local and cheap (no API) and is the search base
   for everything below.
4. **Match** (done) -- `theke match` matches a given TMDB ID with mediathek rows
   by resolving the ID's metadata via TMDB API, and flips `status` '1' -> '2'.
5. **Download queue** -- staging of downloads via `mediathek_id` or `tmdb_id`
   into SQLite table `queue`. Depending on config, queue entries may need manual
   approval before download. Staging via `tmdb_id` is deduplicated automatically.
6. **Download** -- plain HTTP download of approved items.
7. **Remux** -- FFmpeg pipeline, extracting audio and converting containers to mp4.
8. **Copy** -- Copy into movie library. **Manual path complete here.**
9. **Wishlist** -- entries by TMDB ID, auto availability checks feeding the same
   queue. Pure automation on earlier phases.
10. **Scheduler** -- in-app scheduler; runs unattended.
11. **Docker + NAS deployment** -- containerize, deploy (smoke test, manual).
12. **Library indexer** -- cache current library by parsing nfo files and reading
    MP4 file names and folder names. Needed for phase 13.
13. **Quality upgrades + series completion** -- detect higher resolutions /
    missing episodes (needs the indexer).
14. **Web UI** -- user friendly wrapper for the CLI (via REST API): dashboard,
    approvals, settings, browsing mediathek and wishlist.
15. **Catalog-wide ID browse** (`theke match --bulk`) -- eager bulk-match of the
    movie subset, so the whole movie catalog is browsable by TMDB ID, not just
    wish-resolved rows.

## Architecture / pipeline

```
mediathek table  <---[Fetch]--- liste.mediathekview.de
   |
   | [Enrich] (local, bulk)
   V
mediathek table (with metadata)
   |
   | [Match] (via TMDB-API, on demand or bulk)
   V
mediathek table (with metadata and tmdb_id)
   |
   | [Manual pick] (by mediathek_id or tmdb_id)
   |   or
   | [Wishlist] (auto)
   |   or
   | [Library Upgrade] (auto) <------ library table
   V                                     ^
queue table                              |
   |                                     | [Indexer]
   | [Approval]                          |
   V                                     |
[Download] --> [Remux] --> [Copy] --> media folders
```

## Data model

Single SQLite file `theke.db`. Field lists and status names are indicative.

- **mediathek** -- film-list mirror, refreshed periodically. `status` is one char
  ('0' new, '1' enriched, '2' matched, possibly: 'D' downloaded, 'N' not needed).
  `enrich` fills language/clean_title/series_name/season/episode/episode_count/
  category/year/country/flags/enrich_confidence and flips status to '1';
  `match` fills tmdb_id/match_confidence on demand (only for rows a wishlist entry
  or manual pick resolves) and flips status to '2'. All enriched columns survive
  refreshes via `fetch`.
- **queue** -- review queue + download record in one. `status` is one char
  (Lifecycle `proposed -> approved -> downloading -> done / failed / cancelled`).
- **library** -- wishlist + current library record in one. `status` is one char
  ('W' wish, 'M' missing episode, 'L' library). Primary key based on `tmdb_id`.
- **meta** -- key/value table for metadata (filmliste_id, filmliste_created).

## Stage details

**Matching (core problem, phase 3+4):** the hard part is fuzzy-matching messy
German film-list free text against a canonical title or tmdb_id. We do this in
two steps:
1. `enrich` extracts clean metadata from the free text mediathek entries, and
   stores the metadata in separate columns. The algorithm is purely heuristic,
   runs locally (no API needed), and runs **in bulk** on all entries.
2. `match` enriches the metadata table with TMDB IDs. This is done **lazy**:
   The algorithm takes a TMDB ID (a wishlist entry or a manual pick), takes
   that entry's title variants + year + season/episode info (via TMDB API),
   searches the enrich-normalized columns for matching mediathek rows, and
   caches the ID in those rows.
   Bulk matching the whole (movie) catalog is planned for phase 15.

**Deduplication rules (phase 5):** One TMDB ID maps to **many** mediathek rows
(senders, SD/HD, languages, repeats). When being added to the queue, a TMDB ID
needs to be deduplicated, criteria being: quality, language, identical length
entries (for audio remuxing), subtitles.
Identical length entries in different languages are assumed to share the same
video stream (which is only needed once). The downloads are flagged for remuxing
with 'A','V' or 'AV' (= audio/video needed).

**The gate (phase 5-6):** Concept: DB-only operations run automatically; anything
that downloads or writes/deletes files in the library sits behind the gate.
- Automatic (DB-only): fetch, enrich, match, wishlist checks, queue proposals.
- Gated (touches the library): starting downloads.
- After approval, automatic: download, remux, replacing files.
- Quality upgrades are most sensitive (they delete files): proposal automatic,
  replacement only after approval -- even when the gate is otherwise relaxed.
- All actions performed after approval are already defined in the gated stage.

**Download (phase 6):** plain HTTP GET for mp4/mp3/m4v/flv/m4a directly streamed
to disk; m3u8-playlists are parsed, and the segments then streamed to disk.
Subtitles are downloaded when present. Downloads resume automatically, failed
downloads retry (a few times), status in DB, no silent loss.

**Scheduler (in-app, phase 10):** `theke run` loops the stages at configured
intervals in pipeline order (fetch -> enrich -> match -> wishlist check -> ...).

## Tech stack

- **CLI (Python)** holds *all* logic -- stages config, fetch, enrich, match, add,
  download, remux, run, etc. Command names follow **git-subcommand conventions**
  (`fetch`, `add`, ...). Mostly one command per stage. A stage with several modes
  nests them as sub-actions. **Machine-readable mode** (`--json`, stable exit
  codes, stable grammar) so the REST layer / Web UI can drive and parse it.
  - **stdout vs stderr:** stdout carries only the result (the single JSON object
    in `--json`); progress/diagnostics go to **stderr** as plain text, so a long
    stage stays visible without polluting the parseable result.
- **Web UI (phase 14):** the only UI -- review dashboard, settings, history, and
  read-only browse/search over the catalog. Talks to the CLI through a **REST
  API** (REST over the `--json` layer); holds no logic. May later be wrapped
  (Electron/Tauri) to run on the PC against the same API.
- **DB:** one SQLite file. **Single-writer:** The CLI is the only writer; other
  tools may open it **read-only** (DBBrowser, the web UI).
- **Video:** FFmpeg as an external process (remux / audio extraction) -- the only
  external runtime dependency besides Python; must be present on NAS / in the
  image.

## Project structure

Flat on purpose -- no folder sprawl; modules may grow long rather than split into
many tiny files.

```
Theke/
+-- theke/            Python package (all logic)
|   +-- __init__.py   config, DB layer, fetch, match, CLI
|   +-- enrich.py     metadata extraction
|   +-- match.py      tmdb_id matching
|   +-- ...           (more files as needed)
+-- pyproject.toml    package + console-script `theke`, dependencies
+-- tests/            pytest suite
+-- webui/            Web UI + REST API
+-- docker/           CLI image entrypoint
+-- analysis/         temporary review notes + scratch scripts (committed, not shipped)
+-- CLAUDE.md
+-- README.md
```

## Development and deployment

- **Dev:** locally on Windows, CLI in a Python **venv**; everything native,
  scheduler included -- no Docker for dev/debug.
- **Delivery:** CLI as a **Docker container** on the **NAS**. The web UI is
  served against the same CLI over REST; packaging it for the PC (Electron/Tauri)
  is undecided and deferred.
- **All paths/secrets via CLI params or config** (media folders, DB path, TMDB
  key) -- nothing hard-coded. Precedence: **CLI params > config file**; config is
  JSON (`theke.json` by default). Docker (phase 11) adds env vars as a third
  source.

## Coding Guidelines

ALWAYS:
- **TDD.** Tests first (incl. edge cases), watch them fail -> commit -> write
  code until green -> commit (2 commits per step). Refactor while green (1 commit).
- **Green before commit.** Run the suite; never commit failing or skipped tests.
- **When in doubt, ask.** Ambiguous requirement or a genuinely user-owned
  decision -> ask, don't guess.
- **Minimal dependencies.** Justify every one; prefer the stdlib. FFmpeg is the
  only runtime dependency besides Python.
- **Compact code.** Let it speak; one short comment per unit, no comment blocks
  inside functions.
- **Files may grow long** -- a few clear longer units over many tiny ones.
- **Thin command handlers.** `cmd_*` orchestrate (parse args, wire stages, emit
  JSON); real work lives in helpers taking plain inputs and returning values
  (unit-testable without a CLI invocation).
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
- Build analysis scripts under `analysis/` whenever useful (e.g. probing
  `build/theke.db`); `_`-prefix throwaway tooling.
- **Command wiring** in two places: a subparser in `build_parser` (flags inline)
  and a `case` in `main`. Global options (e.g. `--config`) and the command name
  read directly off `args`; subcommand flags only in that command's handler (e.g.
  `args.force` in `cmd_fetch`).
- Update README.md after adding/changing subcommands.

NEVER:
- Describe problems that no longer exist (or never applied). State the design as
  it is; don't justify a choice against a non-problem.
