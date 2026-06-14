# CLAUDE.md -- Theke

> Context file for Claude Code: architecture, domain knowledge and conventions.
> Keep it in sync whenever a core assumption changes.
>
> ENCODING RULE: every repo text file (this one included) is stored UTF-8 but
> kept CP-1252-only in content (valid in both). No emojis, no Unicode arrows, no
> box-drawing glyphs; draw arrows with "< - > | ^ V" (e.g. -->, <->, ^, V).
> Applies to repo text files (source, config, docs) only -- NOT runtime data:
> film list metadata is UTF-8 and may carry non-CP-1252 characters, stored as-is.

## Overview

**Theke** (from Media-*thek*) is a self-hosted media manager that acquires,
processes and files German public-broadcaster content into a **Jellyfin**
library. The catalog is the **MediathekView film list** -- a plain download from
liste.mediathekview.de. The only source is the public Mediatheken.

**Two languages, one home for the logic:** the **CLI is Python** and holds all
logic; the **Delphi desktop GUI** owns nothing and shells out to the CLI for
everything. The CLI is the only thing that runs on the NAS.

## Phases (implementation order)

Each phase ends with something usable on its own. Only phases 1-2 are planned;
**phase 3 onward is tentative** (designs and ordering will be revisited if work
gets there) -- do not over-engineer for later phases. Ordering principle for the
middle phases: build the full **manual acquisition path** as one testable
vertical slice (search --> queue --> approve --> download --> remux), then add
the **wishlist** as automation on the same machinery.

1. **Scaffolding** -- config, DB layer, CLI skeleton.
2. **Film list mirror** -- download the film list and import into SQLite table
   `mediathek`, keyed by `mediathek_id` = MediathekView's own film identity
   (SHA-256 over sender+thema+url+website, each UTF-16LE-encoded).
   Standalone: a locally searchable Mediathek DB.
3. **Enrichment + matching** -- add TMDB/IMDB IDs and language codes, fuzzy match
   with confidence scores. On refresh, existing ID assignments must **not** be
   lost; deleted entries shall be deleted.
4. **DB search** -- read-only query/browse of the enriched catalog. A pick by ID
   becomes a review-queue entry (once phase 5 exists), not a download.
5. **Review queue + gate** -- staging of proposals/picks and the approval gate.
   Manual picks land here; user approves. Still no download.
6. **Downloader** -- plain HTTP download of approved items (DE/EN separate,
   subtitles), triggered manually via CLI.
7. **Remuxer** -- FFmpeg pipeline, files land in the Jellyfin folders. **Manual
   path complete here** (search --> ... --> remux works end to end).
8. **Wishlist** -- wish entries by TMDB/IMDB ID, automatic availability checks
   feeding the same review queue. Pure automation on phases 4-7.
9. **Docker + NAS deployment** -- containerize, deploy, still manually triggered
   (smoke test).
10. **Scheduler** -- in-app scheduler goes live; system can run unattended.
11. **Jellyfin indexer** -- parse NFO files, cache the existing library.
    Prerequisite for phase 12.
12. **Quality upgrades + series completion** -- detect higher resolutions /
    missing episodes (needs the indexer).
13. **Web UI** -- review dashboard, settings, history.

## Guiding principle: optional human-in-the-loop

Review is a **configurable gate**, not a hard rule:

1. **Proposal** -- Theke generates match candidates + confidence scores (or the
   user picks via search) into a review queue.
2. **Approval** -- user inspects, optionally corrects, approves or deletes. Only
   then does the download start.

The gate is set in config (optional auto-approve confidence threshold).

## Automatic vs. manual (side-effect boundary)

Rule of thumb: **DB-only operations run automatically; anything that writes or
deletes files in the media library sits behind the gate.**

- Automatic (DB-only): mirror, enrichment, matching, DB search (read-only),
  wishlist checks, proposal generation.
- Gated (touches the library): starting downloads, replacing files.
- Automatic after approval: download, remux, placement.
- Quality upgrades are most sensitive (they delete files): proposal automatic,
  replacement only after approval -- even if the gate is otherwise relaxed.

## Architecture / pipeline

```
liste.mediathekview.de --> [Mirror] --> mediathek table --> [ID Enricher (TMDB/IMDB)]
                                              |
Jellyfin (NFO files) --> [Indexer] -----------+  (phase 11)
                                              |
                                              V
                                         [Matcher]
                                              |
     [DB search] (manual pick) ------------>  +  <------------ [Wishlist] (auto)
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

Two producers feed the review queue: a manual pick from DB search, and the
wishlist. Stages are decoupled -- each writes its state to the DB, so aborts and
re-runs are idempotent. Every stage is callable individually.

## Data model

A **single SQLite file** (`theke.db`) holds all tables. Keep DB access behind a
thin layer in the DB unit so the backend could be swapped later; do not scatter
SQLite specifics across the code. Field lists are indicative.

- **mediathek** -- mirror of the film list, refreshed periodically.
  `mediathek_id` is MediathekView's identity (SHA-256 over
  sender+thema+url+website, UTF-16LE); `status` is one character
  ('0' new, '1' old). language/tmdb_id/imdb_id/match_confidence are filled by
  phase 3 and preserved across refreshes.
  `status, mediathek_id, sender, topic, title, description, date, duration,
  size_mb, url_video, url_video_small, url_video_hd, url_subtitle, url_website,
  url_history, geo, language, tmdb_id, imdb_id, match_confidence`
  Plus a `meta` key/value table (filmliste_id, filmliste_created).
- **queue** -- the single acquisition table = review queue + download record.
  Lifecycle: `proposed -> approved -> downloading -> done / failed / cancelled`.
  `status, id, mediathek_id, source (match/search/wishlist), language,
  confidence, target_path, resolution, error, created_at, updated_at`
- **wishlist** (phase 8) -- `tmdb_id, imdb_id, type, season, episode, status,
  added_at`
- **library** -- filled by parsing MP4 files plus the NFO files (XML) in each
  media folder, which already carry TMDB/IMDB IDs (trivial matching on this
  side). Replaces `wishlist` in phase 11 (wishlist entries become library
  entries with status="wishlist").
  `status, type (movie/episode), title, year, tmdb_id, imdb_id, season, episode,
  path, resolution, languages, added_at`

## DB search (phase 4)

- Read-only query over the enriched catalog: title/topic substring, TMDB/IMDB
  ID, sender, date, or match status.
- A result picked by ID is handed to the review queue (phase 5), not downloaded
  directly. This is the minimal manual acquisition path -- what the wishlist
  later automates; both feed the same queue.
- Needs proper indexes on searched columns; SQLite FTS5 over
  title/topic/description is an option to evaluate.

## Matching (core problem)

Film list row <-> TMDB/IMDB ID. Pragmatic approach:

- **Fuzzy match** on title + year (token-based / Levenshtein).
- **For series** also use season/episode (far more reliable).
- Confirm with extra signals: duration, release year, optional synopsis compare.
- Compute a **confidence score**; below threshold -> flag for manual review.
  Goal: few false positives.
- TMDB returns the IMDB ID as an external ID, so matching against TMDB and taking
  the IMDB ID from there is usually enough.
- One TMDB/IMDB ID maps to **many** mediathek rows (across senders, SD/HD,
  repeats). The queue deduplicates on the target (tmdb/imdb id + language +
  resolution) so an item is not proposed/downloaded twice; search may still list
  all underlying rows.
- AI approaches (embeddings, learned classifier) or web search are a **later**
  fallback if the classic hit rate is poor -- not in the first cut.

## Download and processing

- **Source:** the film list (direct media URLs included).
- **Transport:** plain HTTP GET for mp4/mp3/m4v/flv/m4a, streamed to disk;
  ffmpeg for m3u8 (like MediathekView's "Programmset"). HTTP is needed in
  exactly three simple places: fetching the film list, the TMDB REST API
  (enrichment), and downloading media files.
- **Languages:** DE and EN downloaded **separately** when present.
- **Subtitles:** DE/EN when present (external subtitle providers: later).
- Robust error handling: failed downloads retry, status in DB, no silent loss.

## Remux convention (important)

Goal: never store **duplicate video files**.

- Only one language present -> file stored unchanged.
- DE **and** EN present with equal-length video streams:
  - The **original-language** MP4 stays the main file, **unchanged**. Original
    language comes from the TMDB match (`original_language`); for
    public-broadcaster content this is almost always German, so German is the
    fallback when no match says otherwise.
  - From the **secondary-language** file, FFmpeg extracts **only the audio
    track** as an **external audio file** next to the main video, named
    `<basename>.<lang>.<codec>` (e.g. `Tatort (2024).en.aac/ac3/m4a/mka`); its
    video stream is discarded.
  - Jellyfin then picks up the secondary audio as an extra track, no second
    video stored.

Verify during implementation: exact codec/extension (`.m4a` vs `.ac3` ...) and
that Jellyfin reliably maps the external audio with that naming scheme; set
correct **language metadata/tags** on remux (else Jellyfin mislabels the track);
check the naming convention against current Jellyfin docs.

## Quality upgrades (phase 12, tentative)

- Track current resolution per entry (via the Jellyfin cache).
- Higher resolution appears in the film list -> queue an upgrade proposal.
- After approval: download + verify the new file first, then swap **atomically**,
  so the library is never left broken.

## Wishlist (phase 8)

- Entries by **TMDB** or **IMDB ID**; periodic check against the film list
  mirror. A hit -> download proposal in the review queue (subject to the gate).
- Reuses the exact manual-path machinery (phases 4-7); it is just the automated
  producer of queue entries. Works for movies and (phase 12) missing episodes.

## Tech stack

- **Languages:** CLI in **Python** holds *all* logic (mirror, enrich, match,
  search, review, download, remux, run); the **Delphi desktop GUI** holds none
  and shells out for everything.
- **Front-ends:**
  - **CLI** (primary, Python): one command per stage; the only thing on the NAS
    and the Docker entrypoint (smoke test in phase 9, `theke run` from phase 10).
    Must offer a **machine-readable mode** (`--json`, stable exit codes, stable
    command grammar) so the GUI can drive and parse it.
  - **Desktop GUI** (Delphi): thin presentation shell for the test phase and
    non-technical users; runs every action as a CLI call and renders the JSON.
  - On the PC the CLI ships as a **PyInstaller-frozen `.exe`** bundled with the
    GUI (no Python install needed); the GUI locates and invokes that exe.
  - **Web UI** (phase 13, tentative): review dashboard, settings; possibly a REST
    service over the `--json` output.
- **DB:** a single SQLite file, accessed **only by the CLI** (the GUI has no DB
  dependency). **Single-user design:** only one process at a time may open the
  DB -- so during a scheduled `theke run`, no other `theke` CLI process works.
- **Video:** FFmpeg as an external process (remux/extraction) -- the **only
  external runtime dependency besides Python**; must be present on the NAS / in
  the Docker image.

## Scheduler (in-app, phase 10)

- Part of the application, not the deployment: `theke run` loops over the stages
  at configured intervals in pipeline order (mirror -> enrich -> match ->
  wishlist check -> ...).
- **Works locally without Docker** -- same code and behavior on the dev PC as in
  the container; Docker is packaging only, using `theke run` as its entrypoint.
- **One entrypoint, two modes:** single stage once (`theke <stage>`, e.g.
  `theke mirror`) or the full loop (`theke run`). One-shot exists from the start,
  so the image can ship and be smoke-tested before the loop lands -- the
  entrypoint never changes.
- No host cron, no Compose-level scheduling (rejected: config would live outside
  the app and local no-Docker debugging would be impossible). Every stage stays
  individually callable for targeted debugging.

## Project structure

Flat and tidy on purpose -- no folder sprawl. Two artifacts side by side: the
Python CLI (all logic) and the Delphi GUI (thin shell). Module names are
indicative; modules may grow long rather than splitting into many tiny files.

```
Theke/
+-- theke/                    Python package for the CLI
|   +-- __init__.py           all logic (split into more files later?)
+-- pyproject.toml            package + console-script `theke`, dependencies
+-- tests/                    pytest suite for the CLI
+-- gui/                      Delphi desktop GUI (shells out to the CLI)
+-- docker/                   image runs the Python CLI as entrypoint
+-- CLAUDE.md
+-- README.md
```

## Development and deployment

- **Development** locally on Windows: CLI in a Python venv, GUI in the Delphi
  IDE. Everything runs natively, scheduler included -- no Docker for dev/debug.
- **Delivery is split:** the **CLI** ships as a **Docker container** on the
  **NAS** (phase 9; runs `theke run` once phase 10 exists), and on the **PC** the
  same CLI ships as a **PyInstaller `.exe`** bundled with the Delphi GUI. The GUI
  targets the PC only and is not part of the container.
- From the start, **all paths and secrets via CLI parameters or config file**
  (media folders, DB path, TMDB API key) -- nothing hard-coded, keeping the move
  into the container painless. Precedence: **CLI parameters override the config
  file**; the config is an `.ini`/`.json`-style file. Docker (phase 9) adds
  environment variables as a third source.

## Coding Guidelines

ALWAYS:
- **Compact code.** Let the code speak; a short comment per unit/routine is
  enough, no sprawling comment blocks inside functions.
- **Files may grow long** -- prefer a few clear, longer units over many tiny
  ones. No file/folder sprawl.
- All logic in the Python CLI; the Delphi GUI stays a thin shell with no logic.
- Stages are **idempotent** and re-runnable; state lives in the DB, not memory.
- ANSI / CP-1252 content only in every text file (see encoding rule on top).
- **Python formatting:**
  - Section dividers with the label up front, dashes filling the line:
    `# -- config ------... (to col 80)`.
  - Runs of parallel calls (e.g. `parser.add_argument(...)` lines) stay one
    call per line -- no wrapping, long lines are fine there -- with equal
    arguments vertically aligned across the run.
  - Blank lines between logical blocks inside longer functions.
  - Unused unpacking slots are a bare `_`, not a named placeholder.
- **Python:** use **venv**, never pip install globally.
- **Tests:** the expected value is always **hard-coded / pre-calculated**, never
  computed in the test (a test that recomputes the result with the same logic
  proves nothing). Compute hashes, dates, etc. once and paste the literal, with
  a comment noting how it was derived. Only exception: relational assertions
  that do not need a concrete value (e.g. "id of A differs from id of B").
- **Delphi (GUI):** 3 empty lines between methods; nested function names in
  snake_case.
- **Language:** comments and variable names in English. README.md is the **only**
  German file.

KEEP IN MIND:
- All paths, URLs and settings **must be configurable**, never hard-coded.

NEVER:
- Spell out problems that no longer exist (or never applied). Describe the design
  as it is -- do not justify a choice by contrasting it against a non-problem.
