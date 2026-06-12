# CLAUDE.md -- Theke

> Context file for Claude Code. Captures the architecture, domain knowledge and
> conventions of this project. Keep it in sync whenever a core assumption changes.
>
> ENCODING RULE: every text file in this repo (this one included) is stored as
> UTF-8, but its content stays within the CP-1252 character set. Umlauts and
> other CP-1252 characters are fine; emojis, Unicode arrows and box-drawing
> glyphs are not -- draw arrows with "< - > | ^ V" (e.g. -->, <->, ^, V).
> This rule covers repo text files (source, config, docs) only -- NOT runtime
> data: film list metadata is UTF-8 and may carry characters outside CP-1252,
> which is stored and processed as-is.

## Overview

**Theke** is a self-hosted media manager that automatically acquires, processes
and files German public-broadcaster content into a **Jellyfin** library. The
content catalog comes from the **MediathekView film list** (a plain download
from liste.mediathekview.de) -- not from scraping or querying a web API.

The name comes from Media-*thek*. The project has **nothing** to do with the
*arr stack (Sonarr/Radarr/...) and must not be confused with it -- the only
source is the public Mediatheken, never Usenet/Torrents.

**Two languages, one home for the logic:** the **CLI is Python** and holds all
logic; the **desktop GUI is Delphi** and owns nothing of its own -- it shells
out to the CLI for everything. The CLI is the only thing that runs on the NAS.
See Tech stack.

## Phases (implementation order)

Each phase ends with something usable on its own. Only phases 1 and 2 are planned
out. **Everything from phase 3 (enrichment) onward is tentative** -- design
decisions there and phase order are not set in stone and will be revisited when
(and if) work gets that far. Do not over-engineer for the later phases.

The ordering principle for the middle phases: build the full **manual
acquisition path** first as one complete, testable vertical slice (search -->
queue --> approve --> download --> remux), and only then add the **wishlist** as
automation on top of the exact same machinery.

**Phase 1 -- scaffolding:** config, DB layer, CLI skeleton.

**Phase 2 -- film list mirror:** download the MediathekView film list from
liste.mediathekview.de and import it into the local SQLite DB (table `mediathek`),
including a hash over "sender+thema+titel+url" as "mediathek_id".
Standalone useful: a locally searchable Mediathek database.

**Phase 3 -- enrichment + matching:** add TMDB/IMDB IDs and language codes to
mediathek rows, fuzzy matching with confidence scores. DB becomes
inspectable/curatable. On a mediathek refresh, existing ID assignments must
**not** be lost. Deleted entries shall be deleted.

**Phase 4 -- DB search:** read-only query/browse of the enriched catalog
(by title, topic, IDs, match status). A found entry can be picked by ID. No
downloads yet -- a pick just becomes a review-queue entry once phase 5 exists.

**Phase 5 -- review queue + gate:** staging of proposals/picks and the approval
gate. Manual picks from search land here; the user approves. Still no download.

**Phase 6 -- downloader:** plain HTTP download of approved items (DE/EN
separate, subtitles), triggered manually via CLI.

**Phase 7 -- remuxer:** FFmpeg pipeline, files land in the Jellyfin folders.
**The manual acquisition path is complete here** (search --> queue --> approve
--> download --> remux works end to end).

**Phase 8 -- wishlist:** wish entries by TMDB/IMDB ID, automatic availability
checks that feed the same review queue. Pure automation on top of phases 4-7.

**Phase 9 -- Docker + NAS deployment:** containerize, deploy to the NAS,
still manually triggered (smoke test).

**Phase 10 -- scheduler:** in-app scheduler goes live; after this step, the
system is able to run unattended on the NAS.

**Phase 11 -- Jellyfin indexer:** parse NFO files, cache the existing library.
Prerequisite for phase 12.

**Phase 12 -- quality upgrades + series completion:** detect higher
resolutions / missing episodes (needs the indexer).

**Phase 13 -- web UI:** review dashboard, settings, history.

## Guiding principle: optional human-in-the-loop

Review is a **configurable gate**, not a hard rule:

1. **Proposal stage** -- Theke generates match candidates plus confidence scores
   (or the user picks an entry via search) and collects them in a review queue.
2. **Approval stage** -- the user inspects, optionally corrects, approves or deletes.
  Only then does the download start.

The gate is **set in config** (e.g. `REQUIRE_REVIEW=true|false`, optionally an
auto-approve confidence threshold). Default: manual review on. Every code path
that can trigger a download must honor the current gate setting -- no hard-coded
"always download", no hard-coded "always ask".

## Automatic vs. manual (side-effect boundary)

The rule of thumb: **everything that only touches the database runs
automatically; everything that writes or deletes files in the media library
sits behind the gate.**

- Automatic (harmless, DB-only): mediathek refresh, enrichment, matching,
  DB search (read-only), wishlist checks, proposal generation.
- Gated (touches the library): starting downloads, replacing files.
- Automatic again after approval: download, remux, placement.
- Quality upgrades are the most sensitive case (they delete files): proposal is
  automatic, replacement only after approval -- even if the gate is otherwise
  relaxed.

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

Three producers feed the review queue: the matcher, a manual pick from DB
search, and the wishlist. Stages are decoupled: each writes its state to the
DB, so aborts and re-runs are idempotent. Every stage is callable individually
from the CLI.

## Data model

A **single SQLite database file** (`theke.db`) holds everything -- all tables
live in that one file. Keep DB access behind a thin layer in the DB unit so the
storage backend could be swapped later; do not scatter SQLite specifics across
the code.

Tables (field lists are indicative):

**mediathek** -- mirror of the MediathekView film list, refreshed periodically.
`status, mediathek_id, sender, topic, title, description, date, duration, url_video,
url_video_hd, url_subtitle, language, tmdb_id, imdb_id, match_confidence`

**queue** -- the single acquisition table. Every item runs through one status
lifecycle: `proposed -> approved -> downloading -> done / failed / cancelled`. This is the
review queue (there is no separate queue table) and the download record in one.
`status, id, mediathek_id, source (match/search/wishlist), language, confidence,
target_path, resolution, error, created_at, updated_at`

**wishlist** (phase 8) -- `tmdb_id, imdb_id, type, season, episode, status, added_at`

**library** -- filled by parsing MP4 files as well as the NFO files (XML) that sit
in each media folder and already carry TMDB/IMDB IDs, which makes matching on this
side trivial. This table replaces the wishlist in phase 11, with wishlist entries being
library entries with a certain status (e.g. status="wishlist").
`status, type (movie/episode), title, year, tmdb_id, imdb_id, season, episode,
path, resolution, languages, added_at`

## DB search (phase 4)

- Read-only query over the enriched catalog: by title/topic substring, by
  TMDB/IMDB ID, by sender, date, or match status.
- A result can be picked by ID; the pick is handed to the review queue (phase 5)
  rather than triggering a download directly.
- This is the minimal acquisition path -- a human doing manually what the
  wishlist later automates. Both feed the same review queue.
- Needs proper indexes on the searched columns; full-text search over
  title/topic/description (SQLite FTS5) is an option to evaluate.

## The matching (core problem)

The trickiest part: film list row <-> TMDB/IMDB ID. Pragmatic approach:

- **Fuzzy match** on title + year (token-based / Levenshtein similarity).
- **For series** also use season/episode -- matching is far more reliable there.
- Extra signals to confirm: duration, release year, optional synopsis compare.
- Compute a **confidence score**. Below a threshold -> flag for manual review
  instead of accepting blindly. Goal: few false positives.

TMDB returns the IMDB ID as an external ID, so matching against TMDB and
taking the IMDB ID from there is usually enough.

One TMDB/IMDB ID maps to **many** mediathek rows -- the same title shows up
across senders, as SD and HD, and as repeats. The queue must deduplicate on the
target (tmdb/imdb id + language + resolution) so the same item is not proposed
or downloaded several times; search may still list all the underlying rows.

AI approaches (embeddings, a learned classifier) or general web searches
are a **later** option if the classic hit rate is not good enough -- not
in the first cut.

## Download and processing

- **Catalog source:** the MediathekView film list (direct media URLs included).
- **Transport:** plain HTTP GET for mp4/mp3/m4v/flv/m4a, streamed to disk,
  and through ffmpeg for m3u8 files. (Like MediathekView's "Programmset").
- Note: HTTP is needed in exactly three simple places -- fetching the film
  list file, calling the TMDB REST API (enrichment), and downloading MP4 media
  files.
- **Languages:** DE and EN, when present, downloaded **separately**.
- **Subtitles:** DE/EN when present. (External subtitle providers: later to-do.)
- Robust error handling: downloads fail -> retries, status in DB, no silent loss.

## Remux convention (important)

Goal: never store **duplicate video files**.

- Only one language present -> file is stored unchanged.
- DE **and** EN present, and both video streams have the same length:
  - The **original-language** MP4 stays the main file, **unchanged**. The
    original language comes from the TMDB match (`original_language`); for
    public-broadcaster content this is almost always German, so German is the
    fallback when no match says otherwise.
  - From the **secondary-language** file, FFmpeg extracts **only the audio
    track**, written as an **external audio file** next to the main video, with
    a language suffix in the name (scheme: `<basename>.<lang>.<codec>`, e.g.
    `Tatort (2024).en.aac/ac3/m4a/mka`). That file's video stream is discarded.
- Jellyfin then picks up the secondary-language audio as an extra track without
  a second video file being stored.

To verify during implementation:
- Exact codec/extension (`.m4a` vs `.ac3` ...) and whether Jellyfin reliably
  maps the external audio file with that naming scheme to the movie.
- Set correct **language metadata/tags** on remux, otherwise Jellyfin mislabels
  the track. Check the naming convention against current Jellyfin docs.

## Quality upgrades (phase 12, tentative)

- Track the current resolution per entry (via the Jellyfin cache).
- A higher resolution appears in the film list -> queue an upgrade proposal.
- After approval: download the new file, replace the old one **atomically**
  (download + verify first, then swap), so the library is never left broken.

## Wishlist (phase 8)

- Entries by **TMDB** or **IMDB ID**.
- Periodic check against the film list mirror.
- Hit -> download proposal in the review queue (subject to the gate setting).
- Reuses the exact machinery of the manual path (phases 4-7); it is just the
  automated producer of review-queue entries.
- Works for movies **and** (phase 12) for missing series episodes.

## Tech stack

- **Languages:** the **CLI is Python** and holds *all* logic (every stage:
  mirror, enrich, match, search, review, download, remux, run); the **desktop
  GUI is Delphi** and holds none -- it shells out to the CLI **for everything**.
- **Front-ends:**
  - **CLI** (primary, Python): one command per stage. It is the only thing that
    runs on the NAS and the Docker entrypoint (smoke test in phase 9, `theke
    run` from phase 10 onwards). It must offer a **machine-readable mode**
    (`--json` output, stable exit codes, stable command grammar) so the GUI can
    drive it and parse the results.
  - **Desktop GUI** (Delphi): a thin presentation shell -- for the test phase
    and for non-technical users. It runs **every** action as a CLI call and
    renders the JSON it gets back.
  - On the PC the CLI ships as a **PyInstaller-frozen `.exe`** bundled with the
    GUI, so the PC needs no Python installation; the GUI locates and invokes
    that exe.
  - **Web UI** (phase 13, tentative): review dashboard, settings. Possibly
    REST service using the `--json` output from the cli.
- **DB:** a single SQLite file, accessed **only by the CLI**. The GUI carries
  no DB dependency at all. The DB follows a **single user** design: **only one**
  process at a time can open the database. This means:
  - During a scheduled run with `theke run`, neither the CLI nor another `theke`
    CLI process will work.
- **Video:** FFmpeg, called as an external process (remux/extraction). The
  **only external runtime dependency besides Python itself** -- it must be
  present on the NAS / in the Docker image.

## Scheduler (in-app, phase 10)

- The scheduler is **part of the application**, not of the deployment: a CLI
  command (`theke run`) that loops over the stages at configured intervals,
  in pipeline order (mirror -> enrich -> match -> wishlist check -> ...).
- **It must work locally without Docker** -- same code, same behavior on the
  dev PC as in the container. Docker is packaging only: the container simply
  uses `theke run` as its entrypoint.
- **One entrypoint, two modes:** the same CLI runs a single stage once
  (`theke <stage>`, e.g. `theke mirror`) or the full loop (`theke run`). The
  one-shot mode exists from the start, so the Docker image can ship and be
  smoke-tested with single commands before the loop lands -- the entrypoint
  never has to change.
- No host cron, no Compose-level scheduling tricks (considered and rejected:
  scheduling config would live outside the app and local no-Docker debugging
  would be impossible).
- Even with the scheduler present, every stage stays individually callable via
  CLI for targeted debugging.

## Project structure

Flat and tidy on purpose: no folder sprawl. Two artifacts sit side by side --
the Python CLI (all logic) and the Delphi GUI (thin shell). Module names are
indicative; modules may grow long rather than splitting into many tiny files.

```
Theke/
+-- theke/                    Python package for the CLI
|   +-- theke.py              all logic is in here (for now, split into more files later)
+-- pyproject.toml            package + console-script `theke`, dependencies
+-- tests/                    pytest suite for the CLI
+-- gui/                      Delphi desktop GUI (shells out to the CLI)
+-- docker/                   image runs the Python CLI as entrypoint
+-- CLAUDE.md
+-- README.md
```

## Development and deployment

- **Development** locally on the PC (Windows): the CLI in a Python environment,
  the GUI in the Delphi IDE. Everything runs natively there, including the
  scheduler -- no Docker needed for dev/debug.
- **Delivery is split:** the **CLI** ships as a **Docker container** on the
  **NAS** (phase 9; the container runs `theke run` as soon as phase 10 exists),
  and on the **PC** the same CLI ships as a **PyInstaller-frozen `.exe`** bundled
  with the Delphi GUI.
- The desktop GUI targets the PC only (test phase / non-technical users); it is
  not part of the container.
- From the start: **all paths and secrets via cli-parameters or config file**,
  nothing hard-coded (media folders, DB path, TMDB API key). That keeps the
  move into the container painless.
  Precedence: **cli parameters override the config file**. The config file is
  an `.ini`/`.json`-style file. For docker (phase 9), we will add environment
  variables as a third source.

## Coding Guidelines

ALWAYS DO:
- **Compact code.** No sprawling comment blocks inside functions; let the code
  speak. A short comment per unit/routine is enough.
- **Code files may grow long** -- prefer a few clear, longer units over many
  tiny ones. No file/folder sprawl.
- All logic lives in the Python CLI; the Delphi GUI stays a thin shell that
  only calls the CLI and renders its output -- no logic of its own.
- Stages are **idempotent** and re-runnable; state lives in the DB, not in
  memory.
- ANSI / CP-1252 characters only in every text file (see encoding rule at the
  top; all text files are UTF-8 but CP-1252-only in content).
- **Python:** Use **venv** for everything, never pip install globally.
- **Delphi (GUI):** 3 empty lines between methods; nested function names in
  snake_case.
- **Language**: write comments and variable names in English. README.md is the
  **only** file written in German.

KEEP IN MIND:
- All paths, urls and settings **have to be configurable**, never hard-coded.

NEVER DO:
- Spell out problems that no longer exist (or never applied). Describe the
  design as it is -- do not justify a choice by contrasting it against a
  non-problem (e.g. "xz-decompression uses stdlib lzma" implies there was a
  hurdle; just state what the code does, if it needs stating at all).

