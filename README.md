# Theke

Selbstgehosteter Medienmanager, der deutsche öffentlich-rechtliche Inhalte
automatisch aus der MediathekView-Filmliste bezieht und in eine Filmbibliothek
auf der Platte einsortiert (Layout `Titel (Jahr)/Titel (Jahr).ext`, optional mit
Kodi-nfo-Sidecars -- von gängigen Medienservern (Kodi, Emby, Jellyfin, Plex)
gleichermaßen gelesen; Theke zielt auf das Layout, nicht auf einen bestimmten
Medienserver). Die gesamte Logik
steckt in einer Python-CLI; eine dünne Delphi-Desktop-GUI steuert dieselbe CLI.

Architektur und Phasenplan siehe `CLAUDE.md`.

Status: Phasen 1-10 + 12 fertig -- verfügbar sind die Befehle `config`, `fetch`,
`enrich`, `match`, `queue`, `file`, `library` (inkl. `scan`) und `run`.

## Voraussetzungen

- Python >= 3.11
- FFmpeg installiert (erst sobald das Remuxing kommt -- noch nicht nötig)

## Installation (Entwicklung)

Im Projekt-Wurzelverzeichnis ein virtuelles Environment anlegen und Theke im
editierbaren Modus zusammen mit den Dev-Werkzeugen (pytest) installieren:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Falls PowerShell das Aktivierungsskript blockiert, einmalig pro Sitzung erlauben:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Alternativ ohne Aktivieren: den folgenden Befehlen einfach `.\.venv\Scripts\`
voranstellen.

## Ausführen

Bei aktiviertem virtuellem Environment:

```powershell
theke --help          # Befehlsübersicht
theke <befehl> --help # Hilfe zu einem einzelnen Befehl
```

Die einzelnen Befehle sind unten unter [CLI-Dokumentation](#cli-dokumentation)
beschrieben.

## Tests

```powershell
pytest
```

Aus dem Projekt-Wurzelverzeichnis ausführen; die pytest-Konfiguration in
`pyproject.toml` zeigt auf den Ordner `tests/`.


# CLI-Dokumentation

Die gesamte Logik steckt in der CLI; jeder Befehl ist eine Pipeline-Stufe und
für sich allein aufrufbar (idempotent, Zustand in der DB). Aufruf immer über das
Konsolen-Skript `theke`.

## Globale Optionen

Vor dem Befehl angegeben, gelten für alle Befehle:

| Option               | Wirkung                                                |
| -------------------- | ------------------------------------------------------ |
| `-c`, `--config PATH`| Konfigurationsdatei (Standard: `theke.json`).          |
| `-d`, `--db PATH`    | DB-Datei; überschreibt `db_path` aus der Konfiguration.|
| `-j`, `--json`       | Maschinenlesbar: genau ein JSON-Objekt auf stdout.     |
| `-h`, `--help`       | Hilfe (auch je Befehl: `theke <befehl> --help`).       |

**Kürzel:** Fast jede Option hat ein Ein-Buchstaben-Kürzel (wie `-h` für
`--help`); Schalter lassen sich bündeln, z. B. `theke queue delete -cdf` für
`--cancelled --done --failed`. Die jeweiligen Buchstaben stehen in den
Options-Tabellen der Befehle. (Die Query-Filter `--like/--eq/--null/--not-null`
von `enrich show` haben bewusst keines.)

**stdout vs. stderr:** stdout trägt nur das Ergebnis (im `--json`-Modus das eine
JSON-Objekt). Fortschritt und Diagnose laufen als Klartext (`-> ...`) über
stderr -- eine lange Stufe bleibt sichtbar, ohne das parsebare Ergebnis zu
verschmutzen. Lange Übertragungen melden Fortschritt: Downloads (HTTP wie HLS)
je 100 MiB eine Zeile (mit Prozent, wenn die Größe bekannt ist), ffmpeg-Läufe
(remux, HLS-Fallback) alle 10 % der Mediendauer (`HH:MM:SS / HH:MM:SS (P%)`).

**Präzedenz der Konfiguration:** CLI-Parameter > Umgebungsvariablen
(`THEKE_<FELD>`) > Konfigurationsdatei > Defaults. Jedes Config-Feld ist über
`THEKE_<FELD>` (Großbuchstaben) setzbar, z.B. `THEKE_TMDB_API_KEY`,
`THEKE_DB_PATH`. Text-Felder werden roh übernommen (kein Quoting nötig), alle
anderen Typen als JSON (`THEKE_QUEUE_AUTO_APPROVE=true`,
`THEKE_LANGUAGES='["de","en"]'`). Gedacht v.a. für Secrets und das
Docker-Deployment (siehe unten).

**Exit-Codes** (stabil, für die GUI):

| Code | Bedeutung                                        |
| ---- | ------------------------------------------------ |
| `0`  | Erfolg.                                          |
| `1`  | Fehler (Ausnahme; im `--json` `{"error": ...}`). |
| `2`  | Aufruf-/Syntaxfehler (argparse).                 |
| `3`  | DB von einem anderen Prozess gesperrt.           |

## `theke config`

Zeigt die effektive Konfiguration oder bearbeitet die Konfigurationsdatei. Ein
Unterbefehl wählt die Aktion: `show` (Default) und `get` lesen die effektive
Konfiguration nach Präzedenz-Auflösung; `set` und `unset` schreiben die
`--config`-Datei (Standard: `theke.json`) und mergen in vorhandene Schlüssel.
Ohne Aktion läuft `show`, d. h. `theke config` entspricht `theke config show`.

| Aktion         | Wirkung                                                       |
| -------------- | ------------------------------------------------------------- |
| `show`         | Volle effektive Konfiguration ausgeben (Default).             |
| `get KEY`      | Einen effektiven Wert ausgeben (`{KEY: value}`).              |
| `set KEY VALUE`| `VALUE` typgerecht in die Datei schreiben (mergt).           |
| `unset KEY`    | `KEY` aus der Datei entfernen (zurück auf Default).           |

`set` nimmt Strings roh; alle anderen Feldtypen werden als JSON geparst und gegen
den Feldtyp geprüft (ein Integer wird für ein Float-Feld akzeptiert). Unbekannte
Schlüssel sind ein Fehler. Nur die effektive Konfiguration wird verändert -- der
Rest bleibt bei seinen Defaults und steht nicht in der Datei.

```powershell
theke config                                 # db_path = theke.db, filmliste_url = ...
theke --db build/theke.db --json config
theke config set tmdb_api_key abc123         # String roh
theke config set queue_auto_approve true     # JSON: bool
theke config set languages '[\"de\",\"en\"]' # JSON: Liste (PowerShell-Escaping)
theke config get languages                   # {"languages": ["de", "en"]}
theke config unset match_min_confidence      # zurück auf Default
```

## `theke fetch`

Aktualisiert den Filmlisten-Spiegel (Tabelle `mediathek`) nach der
MediathekView-Update-Logik: Server-Listen-ID prüfen -> bei Gleichstand
überspringen, sonst Diff-Liste anwenden (wenn brauchbar), sonst Vollständig
laden. Voller Download + Import dauert ca. 30 s. Der Spiegel wächst nur und wird
aktualisiert, gelöscht wird nie.

| Option          | Wirkung                                             |
| --------------- | --------------------------------------------------- |
| `-f`, `--force` | Immer die volle Liste laden (Diff/Skip übergehen).  |

```powershell
theke --db build/theke.db fetch           # action = full|diff|skip, imported = N
theke --db build/theke.db fetch --force   # erzwingt vollen Download
```

## `theke enrich`

Stufe 3 (Teil 1): extrahiert strukturierte Metadaten aus den Freitextfeldern.
Ein Unterbefehl wählt die Aktion: `run` schreibt, die übrigen
(`report`/`audit`/`show`/`dist`) sind reine Lese-Werkzeuge zum Iterieren an der
Heuristik. Ohne Aktion läuft der Default `run`, d. h. `theke enrich` entspricht
`theke enrich run` (etwaige Flags inklusive, z. B. `theke enrich --force`).

### `enrich run`

Reichert Zeilen an: füllt die enrich-Spalten (`clean_title`, `series_name`,
`genre`, `slot`, `season`, `episode`, `episode_count`, `category`, `year`,
`country`, `language`, `flags`, `enrich_confidence`) und setzt `status` 0 -> 1.
Standardmäßig nur neue Zeilen (`status='0'`). `series_name` trägt nur echte
Serien-/Sendungsnamen; Rubriken landen in `genre` (kuratiertes Set), Dachmarken/
Sendeplätze in `slot`, reine Format-Topics in `category` (Wert `Events` für
Festivals/Preise).

| Option          | Wirkung                                          |
| --------------- | ------------------------------------------------ |
| `-f`, `--force` | Alle Zeilen neu anreichern, nicht nur neue.      |

```powershell
theke --db build/theke.db enrich run            # enriched = N
theke --db build/theke.db enrich run --force    # alles neu
```

Bekannte Spielfilm-/Krimi-Reihen-Topics (Tatort, Polizeiruf 110, die benannten
Krimi-/Fernsehfilm-Reihen) heben eine sonst leere `category` auf `Movie` an, damit
eine Reihe einheitlich bleibt -- auch Ausstrahlungen ohne Film-Metazeile. Die
eingebaute Liste ist über die Konfiguration erweiterbar:

| Schlüssel        | Wirkung                                                            |
| ---------------- | ----------------------------------------------------------------- |
| `fiction_topics` | Zusätzliche Reihen-Topics (Liste), ergänzen die eingebaute Liste (Std. `[]`). |

`flags` ist ein sortierter Buchstaben-String: `A` Audiodeskription, `E` Einfache/
Leichte Sprache, `I` Interview/Gespräch (Begleitstück), `M` Making-of, `S`
Gebärdensprache, `T` Trailer/Vorschau, `U` eingebrannte Untertitel. Ein kurzes
Begleitstück (Trailer/Interview/Making-of, < 300s bzw. < 900s) wird auf `Clip`
herabgestuft, statt als Film/Episode zu zählen.
`enrich_confidence` ist deterministisch: `0.9` (Metazeile/ARTE-Topic), `0.8`
(Topic ist selbst ein Kategoriewort oder ein Event), `0.5` (Dauer-Prior), `0.2`
(`category` = `unklar`).

### `enrich reset`

Macht das Anreichern rückgängig: setzt angereicherte/gematchte Zeilen
(`status='1'`/`'2'`) zurück auf `'0'`, als wären sie frisch geholt. Leert dabei
die enrich-Spalten **und** die match-Spalten (`tmdb_id`, `match_confidence`).
Gibt `reset = N` (Anzahl betroffener Zeilen) aus.

| Option                | Wirkung                                                      |
| --------------------- | ----------------------------------------------------------- |
| `-s`, `--status-only` | Nur `status` zurücksetzen, alle Spalten unverändert lassen. |

```powershell
theke --db build/theke.db enrich reset                # reset = N (Spalten geleert)
theke --db build/theke.db enrich reset --status-only  # nur status 1/2 -> 0
```

### `enrich report`

Per-Sender-Abdeckung der enrich-Felder (% gefüllter Zeilen). Liest standardmäßig
die gespeicherten Spalten.

| Option                  | Wirkung                                                          |
| ----------------------- | ---------------------------------------------------------------- |
| `-s`, `--sender X[,Y]`  | Nur diese Sender (kommagetrennt).                                |
| `-m`, `--min-rows N`    | Sender mit weniger Zeilen weglassen (Standard 1000; `0` = alle). |
| `-l`, `--live`          | `enrich()` live ausführen statt gespeicherte Spalten zu lesen. |
| `-d`, `--diff`          | Churn je Feld: gespeicherte Spalten vs. ein Live-Lauf.           |
| `-b`, `--by-confidence` | Die `cat`-Spalte in Spalten je Konfidenzstufe aufteilen.         |

```powershell
theke --db build/theke.db enrich report                     # alle Sender (>=1000)
theke --db build/theke.db enrich report --sender ZDF,ARTE.DE --by-confidence
theke --db build/theke.db enrich report --live --diff       # Wirkung einer Heuristik-Änderung
```

### `enrich audit`

Findet Zeilen, die eine Heuristik sichtbar falsch behandelt hat (Abdeckung zählt
als gefüllt, aber nicht korrekt). Je Sender/Check `count` + Beispiele. Die Checks
`country-shape`, `title-credit`, `episodic-unparsed` greifen nur auf bereits
angereicherten Zeilen.

| Option                     | Wirkung                                            |
| -------------------------- | -------------------------------------------------- |
| `-s`, `--sender X[,Y]`     | Nur diese Sender.                                  |
| `-c`, `--check NAME[,...]` | Nur diese Checks (Standard alle).                  |
| `-l`, `--limit N`          | Beispiele je Befund (Standard 5).                  |

Checks: `bare-topic`, `case-variants`, `topic-pipe`, `topic-marker`,
`country-shape`, `title-credit`, `episodic-unparsed`.

```powershell
theke --db build/theke.db enrich audit
theke --db build/theke.db enrich audit --check country-shape,title-credit --sender ZDF
```

### `enrich show`

Stichprobe: gibt die enrich-Spalten passender Zeilen aus. Filter werden
UND-verknüpft; `FIELD` muss eine `mediathek`-Spalte sein (Werte werden gebunden,
nie interpoliert).

| Option                  | Wirkung                                          |
| ----------------------- | ------------------------------------------------ |
| `-s`, `--sender X[,Y]`  | Nur diese Sender.                                |
| `--like FIELD PATTERN`  | `FIELD LIKE PATTERN` (wiederholbar).             |
| `--eq FIELD VALUE`      | `FIELD = VALUE` (wiederholbar).                  |
| `--null FIELD`          | `FIELD IS NULL` (wiederholbar).                  |
| `--not-null FIELD`      | `FIELD IS NOT NULL` (wiederholbar).              |
| `-m`, `--min-conf X`    | `enrich_confidence >= X`.                      |
| `-M`, `--max-conf X`    | `enrich_confidence <= X`.                      |
| `-l`, `--limit N`       | Maximale Zeilenzahl (Standard 20).               |

```powershell
theke --db build/theke.db enrich show --eq category unklar --limit 10
theke --db build/theke.db enrich show --sender ARTE.DE --not-null season --like title "%Staffel%"
```

### `enrich dist`

Top-N-Häufigkeiten der Werte eines Feldes (absteigend), z. B. zum Sichten der
Kategorie- oder Länder-Verteilung.

| Option                 | Wirkung                                  |
| ---------------------- | ---------------------------------------- |
| `-f`, `--field NAME`   | Zu zählende Spalte (Pflicht).            |
| `-s`, `--sender X[,Y]` | Nur diese Sender.                        |
| `-l`, `--limit N`      | Top-N Werte (Standard 30).               |

```powershell
theke --db build/theke.db enrich dist --field category
theke --db build/theke.db enrich dist --field country --sender ARTE.DE --limit 15
```

## `theke match`

Stufe 4: löst eine TMDB-ID auf (Titelvarianten/Jahr/Laufzeit über die TMDB-API)
und markiert die passenden `mediathek`-Zeilen mit `tmdb_id` + `match_confidence`,
`status` 1 -> 2. Ein Unterbefehl wählt die Aktion: `run` schreibt, `show` erklärt
die Kandidaten-Scores schreibfrei. Ohne Aktion läuft der Default `run`, d. h.
`theke match --tmdb 1474601` entspricht `theke match run --tmdb 1474601`.

Mit `--type series` werden statt Filmen Serien-**Episoden** gematcht: TMDB führt
pro Serie nur eine ID, daher identifiziert erst das Tripel aus Serien-ID, Staffel
und Folge (`--tmdb` + `--season` + `--episode`) eine Episode eindeutig. Gematcht
wird über Serienname-Ähnlichkeit **und** exakte Staffel/Folge; Episodentitel und
Laufzeit bestätigen weich. (Bulk-Modus für ganze Staffeln/Serien ist geplant.)

### `match run`

Schreibt `tmdb_id` + `match_confidence` auf die Treffer. Eine bereits gesetzte,
abweichende `tmdb_id` bleibt erhalten (wird nicht überschrieben).

**Arte-Zweiter-Durchgang:** Landet ein Treffer auf einem Arte-Sprachsender
(`ARTE.XX`), folgt automatisch ein zweiter Durchgang für alle Sprachvarianten
desselben Films. Arte strahlt einen Film unter mehreren Sendern (`ARTE.DE/FR/ES/
EN/IT/PL`) mit lokalisierten -- und damit nicht über den Titel auffindbaren --
Titeln aus; alle teilen sich dieselbe Programm-ID in `url_website`. Über diese
exakte ID werden die übrigen Sprachvarianten verknüpft und mit derselben
`tmdb_id` markiert; ihre Confidence erben sie vom auslösenden Treffer. Das
Ergebnis meldet `arte_linked` (Zahl der so verknüpften Zeilen) -- wie
`candidates` auch bei `--dry-run` gefüllt; `written` bleibt dann 0.

Bei `--type series` trägt das Ergebnis den **Episodentitel** in `title` und den
Serientitel zusätzlich in `series`.

| Option               | Wirkung                                                       |
| -------------------- | ------------------------------------------------------------- |
| `-t`, `--tmdb ID`    | Zu matchende TMDB-ID (Film-ID, bzw. Serien-ID bei `series`).  |
| `-T`, `--type T`     | `movie` (Standard) oder `series`.                             |
| `-s`, `--season N`   | Staffelnummer (Pflicht bei `--type series`).                  |
| `-e`, `--episode N`  | Folgennummer (Pflicht bei `--type series`).                   |
| `-d`, `--dry-run`    | Treffer berechnen, nichts schreiben.                          |
| `-m`, `--min-conf X` | Mindest-Confidence zum Markieren (Standard: Config).          |
| `-y`, `--year-tolerance N` | Erlaubte Jahresdifferenz bei Filmen (Standard: Config `match_year_tolerance`). |

```powershell
theke --db build/theke.db match run --tmdb 1474601   # candidates/written/arte_linked
theke --db build/theke.db match --tmdb 1474601 --dry-run
theke --db build/theke.db match run --type series --tmdb 290 --season 2 --episode 6
```

### `match show`

Reines Lese-Werkzeug: listet die Kandidaten-Zeilen mit Score-Aufschlüsselung
(Titelähnlichkeit, Jahr-/Laufzeit-Differenz), ohne zu schreiben. Standardmäßig
alles, was nicht verworfen wurde -- zum Justieren der Match-Heuristik.

| Option               | Wirkung                                                     |
| -------------------- | ----------------------------------------------------------- |
| `-t`, `--tmdb ID`    | Zu inspizierende TMDB-ID (Film-ID, bzw. Serien-ID).         |
| `-T`, `--type T`     | `movie` (Standard) oder `series`.                           |
| `-s`, `--season N`   | Staffelnummer (Pflicht bei `--type series`).                |
| `-e`, `--episode N`  | Folgennummer (Pflicht bei `--type series`).                 |
| `-m`, `--min-conf X` | Mindest-Confidence zum Listen (Standard 0.0).               |
| `-y`, `--year-tolerance N` | Erlaubte Jahresdifferenz bei Filmen (Standard: Config `match_year_tolerance`). |
| `-l`, `--limit N`    | Maximale Kandidatenzahl (Standard 20).                      |

```powershell
theke --db build/theke.db match show --tmdb 1474601
theke --db build/theke.db match show --type series --tmdb 290 --season 2 --episode 6
```

### `match reset`

Macht das Matching rückgängig: setzt gematchte Zeilen (`status='2'`) zurück auf
`'1'` (angereichert). Leert dabei `tmdb_id` und `match_confidence`. Reine
DB-Operation -- kein TMDB-Key nötig. Gibt `reset = N` aus.

| Option                | Wirkung                                                       |
| --------------------- | ------------------------------------------------------------- |
| `-s`, `--status-only` | Nur `status` zurücksetzen, `tmdb_id`/`match_confidence` lassen. |

```powershell
theke --db build/theke.db match reset                # reset = N (IDs geleert)
theke --db build/theke.db match reset --status-only  # nur status 2 -> 1
```

## `theke queue`

Stufen 5-8: stellt Downloads in die Tabelle `queue` (Review-Queue + Download-Akte
in einem) und führt sie aus. `add`/`list`/`approve`/`cancel`/`delete` sind reine
DB-Operationen; `download` ist die einzige Aktion, die das Dateisystem berührt
(das Tor). Ohne Aktion läuft der Default `list`, d. h. `theke queue` entspricht
`theke queue list`.

Der Lebenszyklus einer Zeile (Spalte `status`, ein Zeichen; ASCII-aufsteigend in
Ablaufreihenfolge, damit eine einfache Sortierung dem Fortschritt folgt):
`proposed` (`0`) -> `approved` (`A`) -> `busy`/downloading (`B`) -> `done` (`D`),
daneben `cancelled` (`C`) und `failed` (`F`). Jede Zeile ist **selbsttragend**:
sie enthält alles, was `download` braucht, ohne erneut in die `mediathek`-Tabelle
oder die Konfiguration zu schauen -- `language`,
`resolution` (`HD`/`SD`/`LQ`), `remux` (`A` = nur Audio, `V` = nur Video,
`AV` = beides), `url` (Quell-Medien-URL), `url_subtitle` (optional), `path`
(vollständiges Zielverzeichnis in der Bibliothek) und `year` (Erscheinungsjahr,
für die Bibliotheks-Akte). Sie werden beim `add` aufgelöst; `url`/`url_subtitle`/
`path` sind dort per CLI überschreibbar.

**Konfiguration** (in `theke.json`):

| Schlüssel             | Wirkung                                                            |
| --------------------- | ----------------------------------------------------------------- |
| `queue_auto_approve`  | `true` stellt direkt auf `approved` statt `proposed` (Std. `false`). |
| `languages`           | Sprach-Whitelist **und** Präferenzreihenfolge (Std. `["de"]`).    |
| `library_path`        | Vorlage für `path` (Std. `"movies/{Title} ({Year})/{Title} ({Year}).mp4"`). |
| `video_ext`           | Endung der Videodatei (Std. `"mp4"`).                             |
| `audio_ext`           | Endung der Audiodatei (Std. `"aac"`).                             |
| `subtitle_formats`    | Sidecar-Formate je Untertitel (Liste, Std. `["srt","ass","ttml"]`). |
| `temp_path`           | Scratch-Verzeichnis für Download/Remux (Std. `""` = System-Temp). |

Die `library_path`-Platzhalter sind **case-insensitiv**: `{Title}`, `{Year}` (für
Filme) sowie -- für das spätere Serien-Layout reserviert -- `{Series}`,
`{Season}`, `{Episode}`; ein `:N` füllt eine Zahl auf N Stellen mit führenden
Nullen (`{Season:2}` -> `03`). Die Endung der Vorlage wird verworfen und durch
`video_ext` ersetzt (bzw. `audio_ext` bei reinen Audio-Zeilen). Nicht-Anker-Picks
(weitere Sprachen desselben Films) erhalten ein Sprachkürzel vor der Endung,
z. B. `Film (2020).fr.mp4` bzw. `Film (2020).fr.aac`.

### `queue add`

Stellt Downloads ein. `--tmdb` löst einen gematchten Film auf (ein TMDB-Aufruf
für Titel/Jahr/Originalsprache) und dedupliziert seine vielen `mediathek`-Zeilen
zur minimalen Download-Menge: beste Qualität je Whitelist-Sprache; teilen sich
Sprachvarianten denselben Videostream (gleiche Arte-Programm-ID oder identische
Dauer), wird das Video nur einmal geladen (`AV`), die übrigen nur als Audio
(`A`). Die Sprache `ov` (Originalversion) wird dabei über die TMDB-Originalsprache
aufgelöst. `--mediathek-id` stellt genau eine Zeile direkt ein (`AV`, keine
Deduplizierung). Neue Einträge sind `proposed`, sofern `queue_auto_approve` nicht
gesetzt ist. Eine bereits aktiv (P/A/D) eingereihte `mediathek_id` wird
übersprungen; eine abgeschlossene/stornierte blockiert ein erneutes Einstellen
nicht. Beide Optionen sind wiederholbar. `deduplicated` meldet die dabei
zusammengefassten/herausgefilterten Quellzeilen.

Dabei werden zugleich die download-relevanten Spalten aufgelöst: `url` aus der
zur `resolution` passenden Medien-URL, `url_subtitle` aus der Quellzeile, `path`
aus `library_path` (mit TMDB-Titel/-Jahr, sonst `clean_title`/`year`) und `year`
(TMDB-Jahr bei gematchten Picks, sonst das angereicherte `year`) -- letzteres
landet beim Download in der Bibliotheks-Akte. `url`/`url_subtitle`/`path` lassen
sich per Option überschreiben (Escape-Hatch, z. B. ein manueller Zielpfad).

| Option                    | Wirkung                                                  |
| ------------------------- | -------------------------------------------------------- |
| `-t`, `--tmdb ID`         | TMDB-ID einstellen, dedupliziert (wiederholbar).         |
| `-m`, `--mediathek-id ID` | `mediathek_id` direkt einstellen (wiederholbar).         |
| `--language CODE`         | `language` überschreiben.                                |
| `--resolution {HD,SD,LQ}` | `resolution` überschreiben.                              |
| `--remux {AV,A,V}`        | `remux`-Modus überschreiben.                             |
| `--url URL`               | Quell-Medien-URL überschreiben.                          |
| `--url-subtitle URL`      | Untertitel-URL überschreiben.                            |
| `--path PATH`             | Zielpfad in der Bibliothek überschreiben.                |

```powershell
theke --db build/theke.db queue add --tmdb 1474601     # queued/skipped/deduplicated
theke --db build/theke.db queue add --mediathek-id <id>
theke --db build/theke.db queue add --mediathek-id <id> --path "M:/Filme/X (2020)/X (2020).mp4"
```

### `queue list`

Listet Einträge (älteste Erstellung zuerst), optional nach Lebenszyklus-Zustand
gefiltert. `--json` gibt die Zeilen zurück, sonst eine Tabelle auf stdout.

| Option                 | Wirkung                                                                        |
| ---------------------- | ------------------------------------------------------------------------------ |
| `-s`, `--status STATE` | Nur diesen Zustand: `proposed`, `approved`, `busy`, `cancelled`, `done`, `failed`. |

```powershell
theke --db build/theke.db queue list
theke --db build/theke.db --json queue list --status proposed
```

### `queue approve`

Hebt `proposed`-Einträge auf `approved` (das Tor zum Download). Nur Zeilen im
Zustand `proposed` werden berührt -- mit `--force` dagegen aus jedem Zustand
(z. B. ein `cancelled`- oder `done`-Eintrag zurück auf `approved`). Gibt
`approved = N` aus.

| Option          | Wirkung                                                      |
| --------------- | ------------------------------------------------------------ |
| `ID ...`        | Zu genehmigende Eintrags-IDs.                                |
| `-a`, `--all`   | Alle (mit `--force`: alle, sonst nur `proposed`) genehmigen. |
| `-f`, `--force` | Unabhängig vom aktuellen Zustand zurück auf `approved`.       |

```powershell
theke --db build/theke.db queue approve 3 4
theke --db build/theke.db queue approve --all
theke --db build/theke.db queue approve 7 --force   # z. B. storniert -> approved
```

### `queue cancel`

Storniert aktive Einträge (`proposed`/`approved`/`busy`) -- eine weiche
Zustandsänderung, die den Datensatz behält. Abgeschlossene Einträge bleiben
unberührt. Gibt `cancelled = N` aus.

| Option        | Wirkung                              |
| ------------- | ------------------------------------ |
| `ID ...`      | Zu stornierende Eintrags-IDs.        |
| `-a`, `--all` | Alle aktiven Einträge stornieren.    |

```powershell
theke --db build/theke.db queue cancel 3
theke --db build/theke.db queue cancel --all
```

### `queue delete`

Löscht Einträge **endgültig** aus der Tabelle (anders als `cancel`, das den
Datensatz behält). Genau ein Selektor: IDs, `--all`, oder ein bzw. mehrere
Endzustands-Schalter (`--cancelled`/`--done`/`--failed`, kombinierbar). Gibt
`deleted = N` aus.

| Option              | Wirkung                                  |
| ------------------- | ---------------------------------------- |
| `ID ...`            | Zu löschende Eintrags-IDs.               |
| `-a`, `--all`       | Alle Einträge löschen.                   |
| `-c`, `--cancelled` | Alle stornierten Einträge löschen.       |
| `-d`, `--done`      | Alle fertigen Einträge löschen.          |
| `-f`, `--failed`    | Alle fehlgeschlagenen Einträge löschen.  |

```powershell
theke --db build/theke.db queue delete 3 4
theke --db build/theke.db queue delete --cancelled --done   # Aufräumen
theke --db build/theke.db queue delete --all
```

### `queue download`

Führt für `approved`-Einträge die Kette **Download -> Remux -> Move** aus (das
Tor: die einzige Queue-Aktion, die Dateien schreibt). Quelle ist allein die
Queue-Zeile: `url` wird nach `temp_path` geladen (HLS-Playlist oder direkter
HTTP-Download), gemäß `remux` mit dem Sprach-Tag aus `language` remuxt (beides
unter einem eindeutigen Temp-Präfix, damit parallele Downloads nicht
kollidieren), nach `path` verschoben und ein etwaiger `url_subtitle` in die
konfigurierten Formate (`subtitle_formats`) konvertiert und als
`<stem>.<lang>.<ext>`-Sidecar(s) neben den Film gelegt (TTML/EBU-TT oder WebVTT
rein, SRT/ASS/TTML raus, ffmpeg-frei; ein nicht erkanntes Format wird
übersprungen). Nach erfolgreichem Move werden alle Temp-Dateien des
Eintrags gelöscht. Nur `approved`-Zeilen sind berechtigt; eine fehlgeschlagene
Zeile wird `failed` (mit Fehlertext) markiert und bricht den Lauf nicht ab --
zum Wiederholen erneut `approve` (`--force`). Gibt `downloaded`/`failed` aus.

Nach erfolgreichem Move wird der Film in der Bibliothek vermerkt: trägt die Zeile
eine `tmdb_id`, wird der zugehörige `library`-Eintrag auf `L` (in Bibliothek)
gesetzt und sein `path` auf den Zielordner sowie `year` aus der Queue-Zeile
übernommen (ein bereits beim Wunsch erfasstes Jahr bleibt erhalten) -- ein offener
Wunsch (`W`) kippt damit auf `L`, sonst wird ein neuer `L`-Eintrag angelegt (siehe
`theke library`). So lädt ein erneuter `theke run`-Durchlauf einen bereits
geholten Wunsch nicht noch einmal.

| Option          | Wirkung                                          |
| --------------- | ------------------------------------------------ |
| `ID ...`        | Herunterzuladende (genehmigte) Eintrags-IDs.     |
| `-a`, `--all`   | Alle genehmigten Einträge herunterladen.         |
| `-f`, `--force` | Vorhandene Zieldatei überschreiben.              |

```powershell
theke --db build/theke.db queue download 3
theke --db build/theke.db queue download --all
```

## `theke file`

Stufen 6-8: die dateibezogenen Primitive -- `download` (Stufe 6), `remux`
(Stufe 7) und `move` (Stufe 8). Sie arbeiten **unabhängig von der Queue** auf
expliziten URLs/Pfaden und berühren die DB nicht (die queue-gesteuerte
Verkettung leistet `theke queue download`); nützlich für Tests und Einzelfälle.
Ein Unterbefehl wählt die Aktion; einen Default gibt es nicht. Fortschritt geht
nach stderr, das Ergebnis (in `--json` ein Objekt) nach stdout.

**Konfiguration** (in `theke.json`):

| Schlüssel          | Wirkung                                                              |
| ------------------ | ------------------------------------------------------------------- |
| `ffmpeg_path`      | Pfad/Name des ffmpeg-Binaries (Std. `"ffmpeg"`, nutzt den PATH).    |
| `download_retries` | Wiederholungen bei Download-Fehlern (Std. `3`).                     |
| `download_timeout` | Netzwerk-Timeout in Sekunden je Socket-Vorgang -- bricht hängende Downloads und API-Abfragen ab statt ewig zu warten (Std. `60`). Gilt für alle Downloads (fetch, Filmliste, HLS, Untertitel) und alle TMDB-Abfragen. |
| `download_stall_timeout` | Durchsatz-Untergrenze in Sekunden (Std. `120`, `0` = aus): bricht einen direkten Download ab, der pro Zeitfenster weniger als 64 KiB liefert -- fängt einen Trickle, den der Socket-Timeout nie auslöst. |

### `file download`

Lädt `--url` nach `--out`. Eine `.m3u8`-URL wird als HLS behandelt:
Master-Playlist -> Variante mit höchster Bandbreite -> Segmente einzeln laden
und zu `--out` zusammenfügen (ein Init-Segment wird vorangestellt); bereits
geladene Segmente bleiben beim Wiederholen liegen. Kann HLS nativ nicht
verarbeitet werden -- verschlüsselt (AES-128) oder Segment-Download endgültig
fehlgeschlagen --, übernimmt ffmpeg (`-c copy`). Jede andere URL ist ein
einfacher HTTP-Download, der eine liegengebliebene `.part`-Datei per
Range-Header fortsetzt (sofern der Server es unterstützt, sonst Neustart).
Fehlgeschlagene Versuche werden bis `download_retries` mal wiederholt. Jeder
Socket-Vorgang ist auf `--timeout` Sekunden begrenzt, sodass eine abgebrochene
Verbindung in die Wiederholung läuft statt hängen zu bleiben.

| Option              | Wirkung                                                  |
| ------------------- | -------------------------------------------------------- |
| `-u`, `--url URL`   | Herunterzuladende Medien-URL.                            |
| `-o`, `--out PATH`  | Zieldatei.                                               |
| `-r`, `--retries N` | Wiederholungen bei Fehler (Std. `download_retries`).    |
| `-t`, `--timeout SEC` | Netzwerk-Timeout in Sekunden (Std. `download_timeout`). |

```powershell
theke file download --url https://.../film.mp4 --out build/film.mp4
theke file download --url https://.../master.m3u8 --out build/film.ts
```

### `file remux`

Stream-Copy von `--in` nach `--out` via ffmpeg (kein Transcoding). `--mode`
bestimmt, was übernommen wird: `AV` (Audio+Video), `A` (nur Audio), `V` (nur
Video). `--language` setzt den Sprach-Tag der ersten Audiospur. `--check-ffmpeg`
prüft nur, ob das konfigurierte ffmpeg (`ffmpeg_path`) aufrufbar ist, und endet
danach -- gibt bei Erfolg die Versionszeile aus, sonst Exit-Code 1.

| Option                 | Wirkung                                                |
| ---------------------- | ------------------------------------------------------ |
| `-i`, `--in PATH`      | Eingabedatei.                                          |
| `-m`, `--mode MODE`    | Was übernehmen: `AV`, `A` (nur Audio), `V` (nur Video).|
| `-o`, `--out PATH`     | Ausgabedatei (Endung bestimmt den Container).          |
| `-l`, `--language CODE`| Sprach-Tag der ersten Audiospur (z. B. `deu`).         |
| `--check-ffmpeg`       | ffmpeg-Binary via `-version` prüfen und beenden.       |

```powershell
theke file remux --in build/film.ts --mode AV --out build/film.mp4
theke file remux --in build/film.ts --mode A --language fra --out build/film.aac
theke file remux --check-ffmpeg
```

### `file remux-subtitle`

Konvertiert eine Untertiteldatei (`--in`: TTML/EBU-TT(-D)-XML oder WebVTT) in je
ein `<base>.<lang>.<ext>`-Sidecar pro Format. **ffmpeg-frei** -- ffmpeg kann TTML
nicht decodieren und verliert beim WebVTT-Decode Farbe und Position; daher ein
eigener Parser (TTML als Kanon, WebVTT wird hochnormalisiert) mit eigenen SRT-
(`<font>`-Farbe), ASS- (`\pos`-Platzierung + Farbe) und TTML-Exportern. `--format`
überschreibt die konfigurierten `subtitle_formats`; ohne `--out` ist die Basis der
Eingabepfad ohne Endung. Ein nicht erkanntes Eingabeformat schreibt nichts.

| Option                 | Wirkung                                                       |
| ---------------------- | ------------------------------------------------------------- |
| `-i`, `--in PATH`      | Eingabe-Untertitel (`.ttml`/`.xml`/`.vtt`).                   |
| `-o`, `--out BASE`     | Ausgabe-Basispfad (Std.: Eingabepfad ohne Endung).            |
| `-l`, `--language CODE`| Sprach-Tag im Sidecar-Namen (Std. `de`).                      |
| `--format LIST`        | Komma-Liste der Formate (Std.: `subtitle_formats`).           |
| `-f`, `--force`        | Vorhandene Sidecars überschreiben.                            |

```powershell
theke file remux-subtitle --in build/movies/Mobbing.xml --language de
theke file remux-subtitle --in sub.vtt --format srt --out "M:/Filme/Film (2020)/Film (2020)"
```

### `file move`

Verschiebt `--in` nach `--out` und legt fehlende Zielverzeichnisse an. Ein
vorhandenes Ziel ist ein Fehler, außer mit `--force` (dann wird es ersetzt).

| Option            | Wirkung                                |
| ----------------- | -------------------------------------- |
| `-i`, `--in PATH` | Quelldatei.                            |
| `-o`, `--out PATH`| Zieldatei.                             |
| `-f`, `--force`   | Vorhandenes Ziel überschreiben.        |

```powershell
theke file move --in build/film.mp4 --out "M:/Filme/Film (2020)/Film (2020).mp4"
```

## `theke library`

Stufe 9 + 12: verwaltet die **Wunschliste** und den **Bibliotheks-Index**. Die
Tabelle `library` ist Wunschliste und Bibliotheks-Akte in einem (Schlüssel
`tmdb_id`); die Spalte `status` (ein Zeichen) ist `W` (Wunsch), `M` (fehlende
Folge, später), `L` (in Bibliothek) oder `D` (gelöscht -- ein `L`, dessen Datei
von der Platte verschwunden ist). Daneben hält der Eintrag das TMDB-Erscheinungs-
jahr `year` (beim Hinzufügen erfasst) und -- nach Download oder `scan` -- den
`path` zum Bibliotheksordner sowie die physischen Attribute der Datei
(`resolution`, `languages`, `duration`, `file_size`, `indexed_at`, `source`).
Alle Aktionen außer `scan` sind reine DB-Operationen; `scan` liest das Dateisystem,
schreibt aber nur in die DB. Ohne Aktion läuft der Default `list`, d. h.
`theke library` entspricht `theke library list`.

Ein Wunsch verlässt `W` erst, wenn sein Download tatsächlich fertig ist: dann
vermerkt `theke queue download` ihn als `L` und trägt seinen Bibliotheksordner als
`path` ein. `theke run` arbeitet nur offene Wünsche (`W`) ab.

### `library add`

Fügt Filmwünsche (`W`) hinzu -- über TMDB-IDs direkt (`--tmdb`), über einen Titel
(`--title`), der per TMDB-Suche (`/search/movie`) in eine ID aufgelöst wird, oder
durch Import einer **ganzen TMDB-Liste** (`--tmdb-list`). Bei `--title` darf das
Jahr (`--year`) -- wie in `theke match` -- um ein paar Jahre danebenliegen: Aus den
Treffern wird der mit der kleinsten Jahresdifferenz innerhalb der Toleranz gewählt
(bei Gleichstand der populärste); ohne `--year` der populärste Treffer. Die erlaubte
Differenz steuert `--year-tolerance` (Default: Config `match_year_tolerance`, ab
Werk `2`). Liefert die Suche zum vollen Titel nichts und beginnt er mit einem
Artikel (`der`/`die`/`das`/`ein`/`eine`/`the`/`a`/`an`), wird einmal ohne den Artikel
nachgesucht (z. B. `Der Pate` -> `Pate`) -- gilt ebenso für `library import`.
`--tmdb`, `--title` und `--tmdb-list` schließen sich gegenseitig aus.

**Listen-Import** (`--tmdb-list ID`): liest den v3-Endpoint `/list/{id}` und legt
alle enthaltenen **Filme** als Wünsche an. Titel und Jahr stammen direkt aus der
Liste (kein Einzelabruf je Film). **Serien werden übersprungen** und auf stderr
gemeldet (die Library ist bis Phase 13 reine Filmsache); ihre Zahl steht in
`series_skipped`. Öffentliche Listen liest schon der `tmdb_api_key`; für **private**
Listen wird ein `tmdb_read_token` (das "API Read Access Token" von TMDB, als
Bearer-Header) benötigt. Die Listen-ID ist die Zahl in der Listen-URL
(`themoviedb.org/list/{id}`). Konfigurierte Listen zieht `theke run`
automatisch nach (s. u.).

**Idempotent**: eine bereits vorhandene ID bleibt unangetastet (zählt als
`skipped`, wird nie von `L` zurück auf `W` gesetzt). Ist ein TMDB-Key
konfiguriert, werden Filmtitel und Erscheinungsjahr (`year`) als Label erfasst
(bei `--title` aus dem gefundenen Treffer; bei `--tmdb` per Abruf, der zugleich
die ID prüft: eine ungültige ID liefert einen TMDB-404 und lässt `add` mit einem
Fehler abbrechen, statt einen ungültigen Wunsch anzulegen). Ohne TMDB-Key bleiben
Titel/Jahr leer (keine Prüfung möglich). `--title` und `--tmdb-list` erfordern
einen TMDB-Key (oder `tmdb_read_token`). Gibt `added`/`skipped` aus (beim
Listen-Import zusätzlich `series_skipped`).

Jeder Wunsch meldet auf stderr, worauf er aufgelöst wurde (TMDB-ID + Titel +
Jahr): bei `--tmdb`/`--tmdb-list` sieht man so, was hinter einer ID steckt, bei
`--title` eine Gegenüberstellung gesuchter Titel (+ Jahr) `->` gefundener Treffer.

| Option                  | Wirkung                                                  |
| ----------------------- | -------------------------------------------------------- |
| `-t`, `--tmdb ID`       | TMDB-Film-ID als Wunsch (wiederholbar).                  |
| `--tmdb-list ID`        | TMDB-Listen-ID importieren (nur Filme; wiederholbar).    |
| `--title TITLE`         | Filmtitel, per Suche in eine TMDB-ID aufgelöst.          |
| `-y`, `--year YEAR`     | Erscheinungsjahr zur Disambiguierung von `--title`.      |
| `--year-tolerance N`    | Erlaubte Jahresdifferenz (Default: `match_year_tolerance`). |
| `--deleted`             | Jeden `D`-Eintrag zurück auf `W` setzen (ohne TMDB-Abruf). |

Eine Ausnahme zur Idempotenz: ein `D`-Eintrag (von `scan` als gelöscht markiert)
wird durch `add --tmdb <id>` **wieder zu `W`** (zählt als `added`, die alten
Scan-Attribute werden geleert). `--deleted` macht das in einem Rutsch für alle
`D`-Einträge (gibt `rewished = N` aus). `--deleted` steht allein, nicht mit
`--tmdb`/`--title`/`--tmdb-list`.

```powershell
theke --db build/theke.db library add --tmdb 1474601
theke --db build/theke.db library add --title "Die Klapperschlange" --year 1981
theke --db build/theke.db library add --tmdb-list 8334221
theke --db build/theke.db library add --deleted
```

### `library import`

Fügt **mehrere** Filmwünsche aus einer Datei hinzu (Massen-Import). Jeder Eintrag
wird auf eine `tmdb_id` aufgelöst -- direkt angegeben oder per Titel/Jahr-Suche
(inkl. Jahres-Toleranz). Einträge, die sich nicht auflösen lassen, landen in einem
**Fehlerprotokoll**, statt den Import abzubrechen; der Rest wird angelegt (`W`,
idempotent). Erfordert einen TMDB-Key.

Eine Titel-Zeile braucht **immer ein Jahr** (anders als interaktiv bei
`add --title`): ohne Jahr ist ein Titel zu mehrdeutig, daher wird er nicht auf
den populärsten Treffer geraten, sondern als Fehler `year missing` protokolliert.

Das Format ergibt sich aus der Endung (`.txt`/`.csv`), `--format` überschreibt:

- **txt**: eine Zeile je Eintrag, entweder `Titel (Jahr)` oder eine `tmdb_id`;
  Leerzeilen werden übersprungen. `--mode` steuert die Deutung: `auto` (Default --
  reine Ziffern = `tmdb_id`, sonst Titel), `id` (alles als IDs) oder `title`
  (alles als Titel). Eine Titel-Zeile ohne `(Jahr)` ist ein Fehler.
- **csv**: Kopfzeile aus den Spalten `tmdb_id`, `title`, `year` (jede darf
  fehlen). `title` und `year` müssen **beide** oder **keine** vorhanden sein;
  Spalten namens `dummy` werden ignoriert, andere Namen sind ein Fehler. Pro
  Zeile gewinnt eine gefüllte `tmdb_id`, sonst der Titel; eine Zeile ohne beides,
  mit ungültigem oder mit fehlendem Jahr (bei vorhandenem Titel) kommt ins
  Fehlerprotokoll. Der Trenner (`,` oder `;`) wird aus der Kopfzeile erkannt; die
  Datei darf UTF-8 (auch mit BOM) **oder** ANSI/CP-1252 sein.

Direkt angegebene IDs werden gegen TMDB geprüft (eine ungültige ID landet im
Fehlerprotokoll). Während des Imports meldet jede Zeile ihren Fortschritt
(`[n/total]`) auf stderr, damit ein langer Import sichtbar bleibt, gefolgt von
ihrer Auflösung (TMDB-ID + Titel + Jahr) -- so sieht man je Zeile, was hinter
einer ID steckt bzw. welchen Treffer eine Titel-Suche gewählt hat. Eine Titel-
Suche ohne Treffer nennt den Grund: gar kein Titel-Treffer vs. Treffer, deren
Jahre alle außerhalb der Toleranz liegen (mit Auflistung der gefundenen Jahre).
Am Ende kommen `added`/`skipped`/`failed` und die `errors`-Liste (`line`,
`input`, `reason`) auf stdout; mit `--json` als Objekt, sonst als Bericht.

| Option                       | Wirkung                                            |
| ---------------------------- | -------------------------------------------------- |
| `PATH`                       | Die zu importierende txt/csv-Datei.                |
| `-F`, `--format {txt,csv}`   | Format erzwingen (Default: aus der Endung).        |
| `-m`, `--mode {auto,id,title}` | txt-Deutung je Zeile (Default `auto`).           |
| `--year-tolerance N`         | Erlaubte Jahresdifferenz für Titel-Zeilen.         |

```powershell
theke --db build/theke.db library import wishes.txt
theke --db build/theke.db --json library import wishes.csv
theke --db build/theke.db library import liste.dat --format csv
```

### `library list`

Listet Einträge (älteste Erstellung zuerst), optional nach Zustand gefiltert.
`--json` gibt die Zeilen zurück, sonst eine Tabelle auf stdout.

| Option                 | Wirkung                                          |
| ---------------------- | ------------------------------------------------ |
| `-s`, `--status STATE` | Nur diesen Zustand: `wish`, `missing`, `library`, `deleted`. |

```powershell
theke --db build/theke.db library list
theke --db build/theke.db --json library list --status wish
theke --db build/theke.db library list --status deleted
```

### `library remove`

Löscht Einträge über genau einen Selektor: angegebene `tmdb_id`s, `--all` oder
alle `D`-Einträge (`--deleted`). Gibt `removed = N` aus.

| Option            | Wirkung                                     |
| ----------------- | ------------------------------------------- |
| `-t`, `--tmdb ID` | Zu entfernende `tmdb_id` (wiederholbar).    |
| `-a`, `--all`     | Alle Einträge entfernen.                    |
| `--deleted`       | Alle `D`-Einträge (gelöscht) entfernen.     |

```powershell
theke --db build/theke.db library remove --tmdb 1474601
theke --db build/theke.db library remove --all
theke --db build/theke.db library remove --deleted
```

### `library scan`

Stufe 12: durchläuft `library_root` und gleicht die Platte mit der `library`-
Tabelle ab. Jeder Filmordner wird identifiziert -- in dieser Reihenfolge: (1) ein
bereits bekannter `L`-Eintrag mit genau diesem Pfad (so wird ein noch vorhandener
Film nie neu aufgelöst und nie fälschlich gelöscht), (2) eine Kodi-nfo mit
`<uniqueid type="tmdb">`, (3) der Ordnername `Titel (Jahr)` + TMDB-Suche. Zu jedem
erkannten Film werden via **ffprobe** die physischen Attribute ermittelt
(Auflösung, Audiosprachen, Laufzeit, Größe) und als `L` eingetragen; unveränderte
Dateien (gleicher Pfad, gleiche Größe, mtime <= letztem Scan) werden **nicht** neu
geprobt. Ein verschobener Film (alter Pfad weg, neuer gefunden) wird als Move-
Aktualisierung verbucht; ein **zweiter** Pfad zum selben Film als Duplikat gemeldet,
ohne den vorhandenen Eintrag zu überschreiben.

**Löscherkennung (Mark-and-Sweep):** Jeder beim Scan gesehene `L`-Eintrag wird mit
`indexed_at` gestempelt; danach werden alle `L`-Einträge mit veraltetem Stempel
(Pfad nicht mehr gefunden) auf `D` gesetzt. `D`-Einträge lassen sich mit
`library remove --deleted` löschen oder mit `library add --deleted` wieder zu
Wünschen machen.

**Ignorieren:** Ein Ordner mit einer Datei `.thekeignore` (samt Unterbaum) wird
übersprungen (gezählt als `ignored`, nicht als unresolved). Anders als `.nomedia`
bleibt der Ordner für Kodi & Co. sichtbar -- nur Theke ignoriert ihn. Nützlich für
Filme, die es in keiner Datenbank gibt (Eigenproduktionen, Parodien).

**Nicht identifizierte** Ordner werden nur **gemeldet** (`unresolved`-Liste), nicht
in die DB geschrieben. Sie lassen sich auflösen, indem man eine nfo mit TMDB-ID
hineinlegt oder den Ordner auf `Titel (Jahr)` umbenennt und erneut scannt.

**Sicherungen gegen Massen-Löschung:** Ist `library_root` nicht gesetzt oder nicht
lesbar (z. B. abgehängter NAS-Mount), bricht der Scan **vor** jeder Änderung ab.
Findet der Durchlauf **gar keinen** Film, während die Bibliothek nicht leer ist,
bleibt der Bestand unangetastet (Ergebnis `library_empty`) -- eine absichtlich
geleerte Bibliothek erzwingt man mit `--allow-empty`.

Braucht `library_root` (Wurzelverzeichnis der Bibliothek, per `--root` überschreibbar)
und ein `ffprobe` (`ffprobe_path`, ab Werk `ffprobe`). Gibt die Zähler `scanned`,
`added`, `updated`, `moved`, `duplicates`, `unresolved`, `ignored`, `deleted` aus.
Pro Ordner geht das Ergebnis (`added`/`updated`/`moved`/`duplicate`/`unresolved`/
`ignored`) auf **stderr** -- so ist nachvollziehbar, woher jeder Zähler im stdout
stammt (analog `library import`).

| Option          | Wirkung                                                       |
| --------------- | ------------------------------------------------------------ |
| `--root PATH`   | Zu durchlaufendes Verzeichnis, überschreibt `library_root` aus der Config. |
| `--allow-empty` | Auch dann aufräumen (sweepen), wenn der Durchlauf keinen Film findet. |

```powershell
theke --db build/theke.db library scan
theke --db build/theke.db library scan --root D:\Filme
theke --db build/theke.db --json library scan
```

## `theke run`

Stufe 9+10: **ein unbeaufsichtigter Durchlauf** der gesamten Pipeline für die
Wunschliste -- einmalig (`--once`) oder **wiederholt nach einem Zeitplan** (der
In-App-Scheduler). Ein Durchlauf (Pass) der Reihe nach: `fetch` (Filmliste
aktualisieren), `enrich` (Metadaten extrahieren), dann -- sofern `tmdb_lists`
konfiguriert ist -- jede **konfigurierte TMDB-Liste** additiv in die Library
nachziehen (nur Filme, wie `library add --tmdb-list`; gezählt in `list_added`),
dann je offenem Wunsch (`W`) `match` (TMDB-ID auflösen und passende
`mediathek`-Zeilen taggen) und `queue add` (deduplizierte Download-Menge einreihen).
Ist `queue_auto_approve` gesetzt, werden die genehmigten Einträge anschließend
gleich heruntergeladen (jeder fertige Wunsch wird dabei als `L` vermerkt); sonst
endet der Pass am Genehmigungs-Tor mit `proposed`-Einträgen. Ein einzelner
fehlschlagender Wunsch oder eine fehlschlagende Liste (z. B. ein TMDB-Fehler)
bricht den Pass nicht ab, und ein fehlschlagender Pass bricht die Schleife nicht
ab. Das Pass-Ergebnis fasst `fetch`/`enriched`/`list_added`/`wishes`/`queued`/
`skipped`/`deduplicated`/`failed`/`downloaded` zusammen; im Loop wird je Pass eine
Zeile geschrieben (in `--json` ein JSON-Objekt pro Pass, JSONL), Fortschritt geht
nach stderr.

Der Listen-Abgleich ist **nur additiv**: aus einer Liste entfernte Filme werden
**nicht** aus der Library gelöscht (die Library hat mehrere Quellen, und ein
einmal gestarteter Wunsch soll nicht still verschwinden). Bereits geladene Filme
(`L`) bleiben ohnehin unberührt.

**Zeitplan (`run_schedule`).** Eine einzige Liste aus Triggern; der nächste Lauf
ist der früheste über alle. Alle Trigger sind **fixed-rate** (an der Wanduhr
ausgerichtet, nicht am Ende des letzten Passes). Einträge:

| Eintrag         | Bedeutung                                                     |
| --------------- | ------------------------------------------------------------- |
| `"start"`       | ein Pass sofort beim Start des Prozesses                      |
| `3600` (Zahl)   | alle N **Sekunden** (an Mitternacht verankert: `3600` = jede volle Stunde) |
| `"03:00"`       | täglich um 03:00 (lokale Uhrzeit)                             |
| `"Mon 20:00"`   | wöchentlich montags 20:00 (`Mon`..`Sun`)                     |

Default: `["start", 3600]` (sofort, dann stündlich). Überrennt ein langer Pass
mehrere Ticks, werden die verpassten zu **einem** Folgepass zusammengefasst
(Überlappung ist ausgeschlossen -- der Loop ist Single-Thread).

Die config wird **vor jedem Pass neu aus der Datei gelesen**, sodass Änderungen
(z. B. an `tmdb_lists` oder `queue_auto_approve`) ohne Neustart wirken; eine
gerade unlesbare Datei (mitten im Speichern erwischt) beendet die Schleife nicht,
sondern behält die letzte gültige config bei. `run_schedule` und die
DB-Verbindung stehen dagegen für die Prozesslaufzeit fest (eine Zeitplanänderung
greift erst nach Neustart).

Der Prozess hält die **einzige DB-Schreibverbindung** für seine Laufzeit (eine
spätere Web-UI im selben Prozess teilt sie sich); ein zweiter schreibender
`theke`-Aufruf scheitert solange am DB-Lock. `SIGINT`/`SIGTERM` (Strg+C bzw.
Docker-Stop) beenden ihn **sauber nach dem laufenden Pass**.

Erfordert einen TMDB-Key (`tmdb_api_key`) für `match` und `queue add` (für private
Listen zusätzlich `tmdb_read_token`).

| Option     | Wirkung                                              |
| ---------- | ---------------------------------------------------- |
| `--once`   | genau ein Pass, dann Ende (kein Scheduling).         |

```powershell
theke --db build/theke.db run --once          # ein Durchlauf
theke --db build/theke.db --json run --once
theke --db build/theke.db run                 # Daemon nach run_schedule
```

# Docker-Deployment (Synology-NAS)

Ausgeliefert wird Theke als Container, der den Scheduler (`theke run`) als Daemon
fährt. Die Dateien liegen in drei gemounteten Volumes:

| Volume     | Inhalt                                                       |
| ---------- | ------------------------------------------------------------ |
| `/config`  | `theke.json` **und** `theke.db`.                            |
| `/temp`    | Download-Scratch (`temp_path`).                             |
| `/library` | die Film-Library (`library_root`).                          |

**`/temp` und `/library` müssen auf demselben physischen Volume liegen** (im
compose beide unter `/volume1`) -- dann ist das Einsortieren eines fertigen
Downloads ein schnelles `rename` auf einem Dateisystem statt einer langsamen
Kopie über Volume-Grenzen.

Das Image bringt **FFmpeg** und **tzdata** mit; `TZ` steuert die lokale Uhrzeit
für die `run_schedule`-Trigger. Die **Pfade** setzt das compose auf die Volumes:
`theke.db` über `--db /config/theke.db` im Container-Kommando, `temp_path` /
`library_root` / `library_path` per Env (`THEKE_TEMP_PATH`, `THEKE_LIBRARY_ROOT`,
`THEKE_LIBRARY_PATH`). Der **TMDB-Key** kommt als `THEKE_TMDB_API_KEY` per Env
(gehört nicht in die Datei). In der `theke.json` pflegst du den Rest (v.a.
`tmdb_lists`, `run_schedule`, `languages`).

## Build auf dem NAS (Container Manager)

Der Build läuft direkt auf dem NAS -- kein lokales Docker nötig:

1. Repo aufs NAS kopieren (z.B. nach `/volume1/docker/theke`).
2. In `docker-compose.yml` die Host-Pfade anpassen: der `/library`- und der
   `/temp`-Mount auf **dasselbe** Volume (z.B. beide unter `/volume1/video/...`).
3. TMDB-Key setzen: entweder eine Datei `.env` neben der `docker-compose.yml`
   mit `THEKE_TMDB_API_KEY=dein_key` anlegen, oder im Container Manager das
   Env-Feld setzen.
4. **Container Manager -> Projekt -> Erstellen**, als Pfad das Repo-Wurzel-
   verzeichnis wählen (nutzt die `docker-compose.yml`). Container Manager baut
   das Image auf dem NAS und startet den Container.

Alternativ per SSH aus dem Repo-Wurzelverzeichnis:

```sh
docker compose up -d --build      # baut das Image und startet den Daemon
docker compose logs -f            # Scheduler-Ausgabe (ein JSON-Objekt pro Pass)
```

Beim ersten Start legt `theke` selbst eine Start-`theke.json` mit Defaults in
`/config` an (die per `--config` angegebene Datei fehlt noch). Danach: Datei
anpassen (v.a. `tmdb_lists` / `run_schedule`) -- der Scheduler liest sie vor
jedem Pass neu; nur eine Änderung an `run_schedule` erfordert einen Neustart des
Containers. Einzelne Kommandos lassen sich im laufenden Container ausführen, z.B.:

```sh
docker compose exec theke theke --config /config/theke.json --json config
docker compose exec theke theke --config /config/theke.json run --once
```

`docker compose stop` beendet den Scheduler über `SIGTERM` sauber nach dem
laufenden Pass.
