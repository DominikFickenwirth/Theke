# Theke

Selbstgehosteter Medienmanager, der deutsche öffentlich-rechtliche Inhalte
automatisch aus der MediathekView-Filmliste bezieht und in eine
Jellyfin-Bibliothek einsortiert. Die gesamte Logik steckt in einer Python-CLI;
eine dünne Delphi-Desktop-GUI steuert dieselbe CLI.

Architektur und Phasenplan siehe `CLAUDE.md`.

Status: Phasen 1-5 fertig -- verfügbar sind die Befehle `config`, `fetch`,
`enrich`, `match` und `queue`.

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
verschmutzen.

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

Stufe 5: stellt Downloads in die Tabelle `queue` (Review-Queue + Download-Akte in
einem). Reine DB-Stufe -- nichts hier berührt das Dateisystem; der eigentliche
Download ist Stufe 6. Ein Unterbefehl wählt die Aktion: `add` stellt ein,
`list`/`approve`/`cancel` verwalten. Ohne Aktion läuft der Default `list`, d. h.
`theke queue` entspricht `theke queue list`.

Der Lebenszyklus einer Zeile (Spalte `status`, ein Zeichen; ASCII-aufsteigend in
Ablaufreihenfolge, damit eine einfache Sortierung dem Fortschritt folgt):
`proposed` (`0`) -> `approved` (`A`) -> `busy`/downloading (`B`) -> `done` (`D`),
daneben `cancelled` (`C`) und `failed` (`F`). Jede Zeile trägt zudem `name`
(Bibliotheks-Dateiname), `language`,
`resolution` (`HD`/`SD`/`LQ`) und `remux` (`A` = nur Audio, `V` = nur Video,
`AV` = beides) für die Remux-Stufe.

**Konfiguration** (in `theke.json`):

| Schlüssel             | Wirkung                                                            |
| --------------------- | ----------------------------------------------------------------- |
| `queue_auto_approve`  | `true` stellt direkt auf `approved` statt `proposed` (Std. `false`). |
| `languages`           | Sprach-Whitelist **und** Präferenzreihenfolge (Std. `["de"]`).    |
| `name_template`       | Vorlage für `name`, gefüllt mit TMDB-Titel + -Jahr (Std. `"{title} ({year})"`). |

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

| Option                    | Wirkung                                                  |
| ------------------------- | -------------------------------------------------------- |
| `-t`, `--tmdb ID`         | TMDB-ID einstellen, dedupliziert (wiederholbar).         |
| `-m`, `--mediathek-id ID` | `mediathek_id` direkt einstellen (wiederholbar).         |

```powershell
theke --db build/theke.db queue add --tmdb 1474601     # queued/skipped/deduplicated
theke --db build/theke.db queue add --mediathek-id <id>
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
