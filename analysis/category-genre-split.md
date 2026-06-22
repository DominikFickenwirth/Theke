# category / genre split -- implementation reference

Phase 3 classify produced a single muddled `category` axis that mixed *medium*
(Film, Kurzfilm, Serie ...) with *content genre* (Dokumentation, Komoedie,
Krimi, Kultur ...). This note is the agreed taxonomy for splitting it into two
orthogonal English axes. No new DB columns -- the existing `category` and
`genre` columns are repurposed.

## Axes

- `category` -- the **medium/form**. Reduced custom set (NOT the full IMDB list):
  `Movie`, `Episode`, `Clip`, `Event`, `NULL`.
- `genre` -- the **content genre**, **TMDB values only** (no custom genres, so
  later TMDB search stays clean). Multiple allowed, comma-joined in canonical
  TMDB order. `NULL` when none applies.

### category meaning

| value   | covers                                                              |
|---------|--------------------------------------------------------------------|
| `Movie` | films and short films (Spielfilm, Fernsehfilm, Kurzfilm, ...)       |
| `Episode` | series episodes and TV contributions (Beitrag, Doku, Reportage)  |
| `Clip`  | short clips, trailers, TV-specials (old Clip + Konzert)            |
| `Event` | one-off events (Berlinale, Filmpreis, Festival -- EVENT_RX)        |
| `NULL`  | unknown (old "unklar"; ARTE row with super-label but no medium sub) |

### genre vocabulary

TMDB only: Action, Adventure, Animation, Comedy, Crime, Documentary, Drama,
Family, Kids, Fantasy, History, Horror, Music, Mystery, News, Reality, Romance,
SciFi, Soap, Talk, Thriller, War, Western. (TMDB's "TV Movie" is dropped -- that
is a medium, it lives on the category axis.) Multi-genre output is sorted by this
canonical order, e.g. `Documentary, History`.

## Mapping: German detected label --> (category, genre)

Format/CATWORD words (metazeile + format topics):

| label                          | category | genre               |
|--------------------------------|----------|---------------------|
| Spielfilm / Spielfilmreihe     | Movie    | --                  |
| Fernsehfilm / TV-Film          | Movie    | --                  |
| Kurzfilm / Stummfilm           | Movie    | --                  |
| Film (generic format topic)    | Movie    | --                  |
| Animationsfilm / Zeichentrickfilm / Trickfilm | Movie | Animation |
| Kinderfilm                     | Movie    | Family              |
| Komoedie                       | Movie    | Comedy              |
| Drama                          | Movie    | Drama               |
| Thriller                       | Movie    | Thriller            |
| Krimi                          | Movie    | Crime               |
| Dokumentarfilm                 | Movie    | Documentary         |
| Dokudrama                      | Movie    | Documentary, Drama  |
| Dokumentation / Doku / Doku-Reihe / Doku/Reportage | Episode | Documentary |
| Reportage                      | Episode  | Documentary         |
| Serie                          | Episode  | --                  |
| Magazin                        | Episode  | --                  |
| Konzert (ARTE Concert)         | Clip     | Music               |
| Events                         | Event    | --                  |

## Mapping: topic genre rubrics (GENRE_SET) --> genre

category stays from the duration prior; only `genre` is set.

| rubric                                   | genre                |
|------------------------------------------|----------------------|
| Musik                                    | Music                |
| Geschichte                               | Documentary, History |
| Nachrichten                              | News                 |
| Politik / Politik und Gesellschaft       | News                 |
| Europa / Nahost / Deutschland            | News                 |
| Wirtschaft                               | News                 |
| Maerchen                                 | Family, Fantasy      |
| Reise / Natur / Tiere                    | Documentary          |
| Esskulturen                              | Documentary          |
| Kultur / Kulturdoku                      | Documentary          |
| Wissen / Wissenschaftsdoku               | Documentary          |
| Gesellschaft                             | Documentary          |
| Buch / Theater                           | Documentary          |
| Sport                                    | Documentary          |

Most non-fiction theme rubrics with no specific TMDB genre collapse to
`Documentary` -- deliberately lossy but TMDB-searchable.

## ARTE: super-label = genre, sub-label = medium

ARTE topics are "Ober - Unter". The super-label carries the genre, the sub-label
the medium (category). Recognized super-label suppresses the duration prior:
if the sub-label is unknown the category stays `NULL` (honest), never a
duration guess.

Super-label --> (category, genre):

| super-label (all ARTE UI languages)            | category | genre                |
|------------------------------------------------|----------|----------------------|
| Kino / Cinema / Cine ...                        | --       | --                   |
| Fernsehfilme und Serien / Series ...            | --       | --                   |
| ARTE Concert                                    | Clip     | Music                |
| Geschichte / Histoire / Storia ...              | --       | Documentary, History |
| Wissenschaft / Sciences ...                     | --       | Documentary          |
| Entdeckung der Welt / Voyages ...               | --       | Documentary          |
| Aktuelles und Gesellschaft / Politics ...       | --       | News                 |
| Kultur und Pop / Culture ...                    | --       | Documentary          |

Sub-label --> category: Filme/Films/... --> Movie; Kurzfilme/... --> Movie;
Stummfilme --> Movie; Fernsehfilme --> Movie; Serien/Series/Seriale/Webseries
--> Episode.

## Duration prior (no other signal)

`< 120s` --> `Clip` (conf 0.5) -- `120-1800s` --> `Episode` (conf 0.5) --
`> 1800s` --> `NULL` (conf 0.2). Skipped when an ARTE super-label matched.

## Confidence

Unchanged shape: metazeile / arte-topic 0.9; topic / event 0.8; duration prior
0.5, or 0.2 when the result is `NULL`.

## Report metric notes (theke/__init__.py)

The internal coverage metric key `unklar` now means `category IS NULL`; the
`events` metric now counts `category == 'Event'`. Metric *keys* are kept; only
the data values change.
