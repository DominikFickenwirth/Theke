# Theke

Selbstgehosteter Medienmanager, der deutsche öffentlich-rechtliche Inhalte
automatisch aus der MediathekView-Filmliste bezieht und in eine
Jellyfin-Bibliothek einsortiert. Die gesamte Logik steckt in einer Python-CLI;
eine dünne Delphi-Desktop-GUI steuert dieselbe CLI.

Architektur und Phasenplan siehe `CLAUDE.md`.

Status: Phasen 1-8 fertig -- verfügbar sind die Befehle `config`, `fetch`,
`enrich`, `match`, `queue` und `file`.

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

**Präzedenz der Konfiguration:** CLI-Parameter > Konfigurationsdatei > Defaults.

**Exit-Codes** (stabil, für die GUI):

| Code | Bedeutung                                        |
| ---- | ------------------------------------------------ |
| `0`  | Erfolg.                                          |
| `1`  | Fehler (Ausnahme; im `--json` `{"error": ...}`). |
| `2`  | Aufruf-/Syntaxfehler (argparse).                 |
| `3`  | DB von einem anderen Prozess gesperrt.           |

## `theke config`

Zeigt die effektive Konfiguration nach Auflösung der Präzedenz.

```powershell
theke config                     # db_path = theke.db, filmliste_url = ...
theke --db build/theke.db --json config
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
`AV` = beides), `url` (Quell-Medien-URL), `url_subtitle` (optional) und `path`
(vollständiges Zielverzeichnis in der Bibliothek). Alle drei werden beim `add`
aufgelöst und sind dort per CLI überschreibbar.

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
zur `resolution` passenden Medien-URL, `url_subtitle` aus der Quellzeile und
`path` aus `library_path` (mit TMDB-Titel/-Jahr, sonst `clean_title`/`year`).
Jede dieser Spalten lässt sich per Option überschreiben (Escape-Hatch, z. B. ein
manueller Zielpfad).

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

### `file download`

Lädt `--url` nach `--out`. Eine `.m3u8`-URL wird als HLS behandelt:
Master-Playlist -> Variante mit höchster Bandbreite -> Segmente einzeln laden
und zu `--out` zusammenfügen (ein Init-Segment wird vorangestellt); bereits
geladene Segmente bleiben beim Wiederholen liegen. Kann HLS nativ nicht
verarbeitet werden -- verschlüsselt (AES-128) oder Segment-Download endgültig
fehlgeschlagen --, übernimmt ffmpeg (`-c copy`). Jede andere URL ist ein
einfacher HTTP-Download, der eine liegengebliebene `.part`-Datei per
Range-Header fortsetzt (sofern der Server es unterstützt, sonst Neustart).
Fehlgeschlagene Versuche werden bis `download_retries` mal wiederholt.

| Option              | Wirkung                                                  |
| ------------------- | -------------------------------------------------------- |
| `-u`, `--url URL`   | Herunterzuladende Medien-URL.                            |
| `-o`, `--out PATH`  | Zieldatei.                                               |
| `-r`, `--retries N` | Wiederholungen bei Fehler (Std. `download_retries`).    |

```powershell
theke file download --url https://.../film.mp4 --out build/film.mp4
theke file download --url https://.../master.m3u8 --out build/film.ts
```

### `file remux`

Stream-Copy von `--in` nach `--out` via ffmpeg (kein Transcoding). `--mode`
bestimmt, was übernommen wird: `AV` (Audio+Video), `A` (nur Audio), `V` (nur
Video). `--language` setzt den Sprach-Tag der ersten Audiospur.

| Option                 | Wirkung                                                |
| ---------------------- | ------------------------------------------------------ |
| `-i`, `--in PATH`      | Eingabedatei.                                          |
| `-m`, `--mode MODE`    | Was übernehmen: `AV`, `A` (nur Audio), `V` (nur Video).|
| `-o`, `--out PATH`     | Ausgabedatei (Endung bestimmt den Container).          |
| `-l`, `--language CODE`| Sprach-Tag der ersten Audiospur (z. B. `deu`).         |

```powershell
theke file remux --in build/film.ts --mode AV --out build/film.mp4
theke file remux --in build/film.ts --mode A --language fra --out build/film.aac
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
