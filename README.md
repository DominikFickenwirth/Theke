# Theke

Selbstgehosteter Medienmanager, der deutsche öffentlich-rechtliche Inhalte
automatisch aus der MediathekView-Filmliste bezieht und in eine
Jellyfin-Bibliothek einsortiert. Die gesamte Logik steckt in einer Python-CLI;
eine dünne Delphi-Desktop-GUI steuert dieselbe CLI.

Architektur und Phasenplan siehe `CLAUDE.md`.

Status: Phasen 1-2 fertig, Phase 3 (classify) teilweise -- verfügbar sind die
Befehle `config`, `mirror` und `classify`.

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

| Option          | Wirkung                                                     |
| --------------- | ----------------------------------------------------------- |
| `--config PATH` | Konfigurationsdatei (Standard: `theke.json`).               |
| `--db PATH`     | DB-Datei; überschreibt `db_path` aus der Konfiguration.     |
| `--json`        | Maschinenlesbar: genau ein JSON-Objekt auf stdout.          |
| `-h`, `--help`  | Hilfe (auch je Befehl: `theke <befehl> --help`).            |

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

## `theke mirror`

Aktualisiert den Filmlisten-Spiegel (Tabelle `mediathek`) nach der
MediathekView-Update-Logik: Server-Listen-ID prüfen -> bei Gleichstand
überspringen, sonst Diff-Liste anwenden (wenn brauchbar), sonst Vollständig
laden. Voller Download + Import dauert ca. 30 s. Der Spiegel wächst nur und wird
aktualisiert, gelöscht wird nie.

| Option    | Wirkung                                             |
| --------- | --------------------------------------------------- |
| `--force` | Immer die volle Liste laden (Diff/Skip übergehen).  |

```powershell
theke --db build/theke.db mirror           # action = full|diff|skip, imported = N
theke --db build/theke.db mirror --force   # erzwingt vollen Download
```

## `theke classify`

Stufe 3 (Teil 1): extrahiert strukturierte Metadaten aus den Freitextfeldern.
Ein Pflicht-Unterbefehl wählt die Aktion: `run` schreibt, die übrigen
(`report`/`audit`/`show`/`dist`) sind reine Lese-Werkzeuge zum Iterieren an der
Heuristik.

### `classify run`

Klassifiziert Zeilen in die classify-Spalten (`clean_title`, `series_name`,
`genre`, `slot`, `season`, `episode`, `episode_count`, `category`, `year`,
`country`, `language`, `flags`, `classify_confidence`) und setzt `status` 0 -> 1.
Standardmäßig nur neue Zeilen (`status='0'`). `series_name` trägt nur echte
Serien-/Sendungsnamen; Rubriken landen in `genre` (kuratiertes Set), Dachmarken/
Sendeplätze in `slot`, reine Format-Topics in `category` (Wert `Events` für
Festivals/Preise).

| Option    | Wirkung                                          |
| --------- | ------------------------------------------------ |
| `--force` | Alle Zeilen neu klassifizieren, nicht nur neue.  |

```powershell
theke --db build/theke.db classify run            # classified = N
theke --db build/theke.db classify run --force    # alles neu
```

`flags` ist ein sortierter Buchstaben-String: `A` Audiodeskription, `E` Einfache/
Leichte Sprache, `S` Gebärdensprache, `U` eingebrannte Untertitel, `T`
Trailer/Vorschau.
`classify_confidence` ist deterministisch: `0.9` (Metazeile/ARTE-Topic), `0.8`
(Topic ist selbst ein Kategoriewort oder ein Event), `0.5` (Dauer-Prior), `0.2`
(`category` = `unklar`).

### `classify report`

Per-Sender-Abdeckung der classify-Felder (% gefüllter Zeilen). Liest standardmäßig
die gespeicherten Spalten.

| Option            | Wirkung                                                          |
| ----------------- | ---------------------------------------------------------------- |
| `--sender X[,Y]`  | Nur diese Sender (kommagetrennt).                                |
| `--min-rows N`    | Sender mit weniger Zeilen weglassen (Standard 1000; `0` = alle). |
| `--live`          | `classify()` live ausführen statt gespeicherte Spalten zu lesen. |
| `--diff`          | Churn je Feld: gespeicherte Spalten vs. ein Live-Lauf.           |
| `--by-confidence` | Die `cat`-Spalte in Spalten je Konfidenzstufe aufteilen.         |

```powershell
theke --db build/theke.db classify report                     # alle Sender (>=1000)
theke --db build/theke.db classify report --sender ZDF,ARTE.DE --by-confidence
theke --db build/theke.db classify report --live --diff       # Wirkung einer Heuristik-Änderung
```

### `classify audit`

Findet Zeilen, die eine Heuristik sichtbar falsch behandelt hat (Abdeckung zählt
als gefüllt, aber nicht korrekt). Je Sender/Check `count` + Beispiele. Die Checks
`country-shape`, `title-credit`, `episodic-unparsed` greifen nur auf bereits
klassifizierten Zeilen.

| Option               | Wirkung                                            |
| -------------------- | -------------------------------------------------- |
| `--sender X[,Y]`     | Nur diese Sender.                                  |
| `--check NAME[,...]` | Nur diese Checks (Standard alle).                  |
| `--limit N`          | Beispiele je Befund (Standard 5).                  |

Checks: `bare-topic`, `case-variants`, `topic-pipe`, `topic-marker`,
`country-shape`, `title-credit`, `episodic-unparsed`.

```powershell
theke --db build/theke.db classify audit
theke --db build/theke.db classify audit --check country-shape,title-credit --sender ZDF
```

### `classify show`

Stichprobe: gibt die classify-Spalten passender Zeilen aus. Filter werden
UND-verknüpft; `FIELD` muss eine `mediathek`-Spalte sein (Werte werden gebunden,
nie interpoliert).

| Option                  | Wirkung                                          |
| ----------------------- | ------------------------------------------------ |
| `--sender X[,Y]`        | Nur diese Sender.                                |
| `--like FIELD PATTERN`  | `FIELD LIKE PATTERN` (wiederholbar).             |
| `--eq FIELD VALUE`      | `FIELD = VALUE` (wiederholbar).                  |
| `--null FIELD`          | `FIELD IS NULL` (wiederholbar).                  |
| `--not-null FIELD`      | `FIELD IS NOT NULL` (wiederholbar).              |
| `--min-conf X`          | `classify_confidence >= X`.                      |
| `--max-conf X`          | `classify_confidence <= X`.                      |
| `--limit N`             | Maximale Zeilenzahl (Standard 20).               |

```powershell
theke --db build/theke.db classify show --eq category unklar --limit 10
theke --db build/theke.db classify show --sender ARTE.DE --not-null season --like title "%Staffel%"
```

### `classify dist`

Top-N-Häufigkeiten der Werte eines Feldes (absteigend), z. B. zum Sichten der
Kategorie- oder Länder-Verteilung.

| Option           | Wirkung                                  |
| ---------------- | ---------------------------------------- |
| `--field NAME`   | Zu zählende Spalte (Pflicht).            |
| `--sender X[,Y]` | Nur diese Sender.                        |
| `--limit N`      | Top-N Werte (Standard 30).               |

```powershell
theke --db build/theke.db classify dist --field category
theke --db build/theke.db classify dist --field country --sender ARTE.DE --limit 15
```
