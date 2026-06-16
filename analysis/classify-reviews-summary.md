# Phase 3 Heuristik-Review -- Zusammenfassung, Matrix und Loesungen

> Querschnitt ueber alle Sender (Detail je Sender: `classify-reviews-sender.md`).
> Enthaelt: vorgeschlagene Schema-Aenderungen, die Sender x Befund-Matrix und
> alle 11 Befunde mit Loesungsansatz **plus Pruefung, dass die Loesung die nicht
> betroffenen Sender nicht verschlechtert** ("bricht nichts").

## Vorgeschlagene Schema-Aenderungen

Mehrere Loesungen teilen sich wenige neue Felder (additive Migration der
`mediathek`-Tabelle + `CLASSIFY_COLS`):

- **`genre`** (neu) -- Thema/Sujet (Reise, Natur, Musik, ...). Trennt Befund 1+2.
- **`slot`** (neu) -- Programmstrand/Sendeplatz/Dachmarke (Herzkino, hr Retro,
  ARD Wissen, regionalmagazin). Ergebnis von Offene Frage 1 (Strands) und 4 (Pipe).
- **`category`** bleibt, wird aber **rein Format/Typ** (Spielfilm, Magazin, Clip,
  Beitrag/Episode, ...) und bekommt **einen neuen Wert `Events`** (Offene Frage 2).
- `series_name`, `clean_title` u. a. unveraendert.

Routing eines Nicht-ARTE-Topics (loest Befund 1, 7 und Teile von 2):

```
Topic --+-- CATWORD-Wort (Spielfilm, Doku, ...) ----> category(Format),  series=NULL
        +-- Clip/Container (Sport-Clip, Beiträge, Sendername) -> category(Clip)/-,  series=NULL
        +-- Genre-Wort (exakt, kuratiert) ----------> genre,              series=NULL
        +-- Event (Berlinale, Grimme Preis, ...) ---> series=Topic + category="Events"
        +-- Strand/Pipe-Dachmarke ------------------> slot,               series=ggf. anderer Pipe-Teil
        +-- sonst (langer Tail) --------------------> series=Topic        (heutiges Verhalten)
```

**Warum das die "sauberen" Sender nicht kaputt macht:** NDR, KiKA, MDR, RBB,
Funk.net, ONE, ZDF-Sparten haben *keine* solchen Rubrik-/Container-/Pipe-Topics
-- ihre Topics fallen in den `sonst`-Zweig und behalten exakt das heutige
`series=Topic`. Die Zweige greifen nur bei exakt erkannten Mustern.

## Sender x Befund-Matrix

Intensitaet: `#` stark/dominant, `+` deutlich vorhanden, `.` marginal, leer = nicht.

| Sender | B1 | B2 | B3 | B4 | B5 | B6 | B7 | B8 | B9 | B10 | B11 |
|--------|----|----|----|----|----|----|----|----|----|-----|-----|
| 3sat | # | # | + | + | + | + |   |   |   |   |   |
| ARD | + |   | # | . | # |   | # | + | + |   |   |
| SRF | # |   |   |   | + |   |   | . |   | # |   |
| ZDF | # | # | # | + | + | + |   |   |   |   |   |
| NDR |   |   | . |   | . |   |   | . |   |   |   |
| SWR | + |   | + |   | + |   | . | . |   |   |   |
| SR | # |   | # |   | . |   |   |   |   |   |   |
| KiKA |   |   | . |   | . |   |   |   |   |   |   |
| WDR | . |   | + |   | . |   | . | . |   |   |   |
| BR | + |   | . |   | . |   | . | . |   |   |   |
| MDR |   |   | . |   | . |   | . |   |   |   |   |
| PHOENIX | + |   | # |   | . |   |   |   |   |   |   |
| ORF |   |   |   |   | + |   |   | + | + |   |   |
| RBB | . |   |   |   | . |   |   | . |   |   |   |
| HR | . |   |   |   | # |   | # |   |   |   |   |
| ARTE.DE/FR |   |   |   |   |   |   |   | . |   |   | # |
| ARTE.EN/ES/IT/PL |   |   |   |   |   |   |   |   |   |   | # |
| tagesschau24 | # |   |   |   |   |   |   |   |   | + |   |
| ZDF-tivi/info/neo | . |   | + |   |   |   |   |   |   |   |   |
| rbtv | . |   |   |   |   |   | # |   |   | + |   |
| DW | + | + | . |   | . |   |   |   |   |   |   |
| Funk.net |   |   |   |   | . |   |   |   |   |   |   |
| ARD-alpha |   |   |   |   | . |   | + |   |   |   |   |
| ONE | . |   |   |   |   |   |   |   |   |   |   |

(Radio Bremen TV / RBTV: trivial, = rbtv.)

## Die 11 Befunde mit Loesungsansatz

### Befund 1 -- `series_name` ist Format/Genre/Container statt Serie

`series_name = topic` wird woertlich gesetzt; Topic ist aber oft eine Rubrik.
Drei Spielarten: Genre-Rubriken (3sat/ZDF/DW), Clip-/Container-Sammeltopics
(SRF `Sport-Clip` 41.657, SR `Beiträge`, BR/SR/`tagesschau24` als Sendername),
Format-Woerter (`Film`, `Dokumentarfilm`).

**Loesung:** Topic-Routing (s. o.) statt woertlicher Uebernahme. Buckets fuer
3Sat (Kron-Beispiel): format `Film/Spielfilm/Fernsehfilm/Dokumentarfilm/
Dokumentation`; genre s. Befund-2-Set; strand/event s. Offene Fragen.
**Bricht nichts:** nur exakt erkannte Rubrik-/Container-Topics werden umgeleitet;
alles andere bleibt `series=Topic`. Saubere Sender (NDR/KiKA/...) sind nicht
betroffen.

### Befund 2 -- `category` vermischt Format und Genre

**Loesung:** neue Spalte `genre`; `category` bleibt rein Format. Genre-Quelle
(**Offene Frage 3: Heuristik + kleines Set**) -- kuratiertes, **exakt** gematchtes
Set:
`Reise, Natur, Musik, Tiere, Geschichte, Politik, Politik und Gesellschaft,
Sport, Nachrichten, Wirtschaft, Europa, Nahost, Deutschland, Esskulturen, Kultur,
Kulturdoku, Gesellschaft, Wissen, Wissenschaftsdoku, Buch, Theater, Märchen`.
**Bricht nichts (geprueft, `_verify_heuristics.py`):** diese Woerter kommen als
*exaktes* Topic nur in 3sat/ZDF/DW/ARTE.DE vor und sind dort durchweg Rubriken --
**nie** eine echte Serie in anderen Sendern. Entscheidend ist **Exakt-Match**:
`Sport` ist Rubrik, aber `Sportschau`/`Sport im Osten`/`BR24Sport` sind Serien und
duerfen nicht ueber Substring getroffen werden.

### Befund 3 -- Schreibvarianten zersplittern denselben `series_name`

Case-/Diakritika-/Abkuerzungs-/Tippfehler-Varianten (`nano`/`NANO`,
`phoenix runde`/`PHOENIX RUNDE`, `WimS`/`Wir im Saarland`, `Insepktor Jury`).
**Loesung:** Normalisierung (case-fold + Diakritika + Trim) **nur fuer
Matching/Dedup im 2. Pass**, nicht den Rohwert ueberschreiben.
**Bricht nichts:** reine Vergleichsnormalisierung; Case-only-Differenzen sind
immer dieselbe Sendung. Abkuerzungen (`WimS`) brauchen ein kleines Alias-Mapping.

### Befund 4 -- `- Film von <Regie>`-Credit bleibt im `clean_title`

**Loesung:** trailing `- Film von <Name>` (allg. `- <CATWORD> von <Name>` **ohne**
folgendes Jahr) wie der `PIPESUF`-Suffix abschneiden; **keine** country/year-
Extraktion (geprueft: 0 dieser Credits tragen `, Land Jahr`).
**Bricht nichts:** greift nur als nachgestellter ` - ... von ...`-Suffix; Sender
ohne dieses Muster matchen nicht. ZDF/3sat profitieren, sonst neutral.

### Befund 5 -- Episoden-Notation ohne Klammern nicht erkannt

`n/m`, `- Teil n` (auch roemisch), `Staffel n, Folge m` ohne Klammern.
**Loesung:** Episoden-Regex um diese Formen erweitern, **mit Guards**: `n/m` nur
am Wortende bzw. vor ` - `, kleine Zahlen, Bruch-/Zeit-Ausnahmen (`3 1/2 Stunden`,
`24/7`) ausschliessen.
**Bricht nichts (mit Guards):** ohne Treffer keine Aenderung; Risiko sind
Falsch-Positive *innerhalb* derselben Extraktion -- daher die Guards. Betrifft v. a.
HR (990), ARD (911), 3sat (176).

### Befund 6 -- Metazeile-Falschtreffer auf das Sendedatum

`... CATWORD vom DD. Monat YYYY` -> Datum als Land/Jahr. **Loesung:** Metazeile
verwerfen, wenn der country-Slot mit `vom` beginnt / wie ein Datum aussieht (Teil
des country-Shape-Filters in Befund 8).
**Bricht nichts:** kein echtes Land heisst `vom ...`; rein additive Ablehnung.
Nur ZDF/3sat (Titel-Metazeile) betroffen.

### Befund 7 -- Pipe-Suffix im Topic bleibt im `series_name`

**Loesung (Offene Frage 4, geprueft):** Topic an `|` teilen; die Seite mit einem
**Sender-Token** (ARD, hr, alpha, ...), einer **Dachmarke** (ARD Wissen, Radio
Bremen, alpha Lernen) oder einem **Sektionswort** (regionalmagazin, sportblitz,
wetter, Retro, Doku, ...) wird `slot`, die andere Seite `series_name`. Matcht
keine Seite -> nicht teilen (Untertitel-Fall).
**Geprueft (`_verify_heuristics.py`):** 12.184/12.206 Pipe-Zeilen (99,8 %)
eindeutig aufgeloest, 0 Konflikte (beide Seiten slot), 21 echte Untertitel-Faelle
korrekt *nicht* getrennt (`Der Germanwings-Absturz | Chronologie ...`). Rest-Risiko:
wenige `... | Bergfreundinnen` (Serie hinterm Pipe ohne Token) bleiben unsplit --
der 2. Pass faengt sie. **Bricht nichts:** ohne Pipe keine Aenderung.

### Befund 8 -- Beschreibungs-Metazeile zieht Satzfragmente (falsch, conf 0.9)

Bei Sendern ausserhalb `TITLE_META_SENDERS` matcht `META` in Fliesstext und
erfindet category/country/year mit hoher Confidence (`country="über den
Klimawandel aus dem Jahr"`). **Loesung:** **country-Shape-Filter** -- Metazeile
nur akzeptieren, wenn der country-Slot wie ein Land aussieht (Grossbuchstabe,
keine Funktionswoerter `von/über/aus/im/vom/...`, kein Datum).
**Bricht nichts:** echte Credits (`Spielfilm, Deutschland 2017`) bestehen den
Filter; nur Satzfragmente fallen raus. Universell anwendbar (faengt auch Befund 6).
Hohe Fehlerquoten bei ORF/SRF (Metazeile dort fast nur Muell).

### Befund 9 -- Klammer-Marker im Topic nicht erkannt

`(mit Gebärdensprache)`/`(ÖGS)` im Topic -> `series_name` verschmutzt, Flag fehlt.
**Loesung:** `take_parens` (Marker-Extraktion) **auch auf den Topic** anwenden, vor
dem Routing. `ÖGS` ist bereits im `MARKERS`-Vokabular.
**Bricht nichts:** `take_parens` entfernt nur *erkannte* Marker, unbekannte
Klammern bleiben. Keine echte Serie heisst `(ÖGS)`. ORF/ARD betroffen.

### Befund 10 -- Barrierefreiheits-Marker als Suffix ohne Klammern

` in Gebärdensprache` (SRF 6.239, rbtv), ` in Einfacher Sprache` (tagesschau24).
**Loesung:** Marker auch als **nicht geklammerten Suffix** erkennen und
abschneiden + Flag setzen; `MARKERS` um `Einfache/Leichte Sprache` ergaenzen
(fehlt heute ganz).
**Bricht nichts:** die Phrasen treten nur als Barrierefreiheits-Tag auf; reine
Suffix-Erkennung. Sender ohne diese Suffixe neutral.

### Befund 11 -- ARTE-Taxonomie nur deutsch

`ARTE_CAT`/`ARTE_SUB` mappen nur deutsche Labels; ARTE.FR/EN/ES/IT/PL fallen auf
den Dauer-Prior (Trefferquote ~3 % statt 97 %). **Loesung:** fremdsprachige
Ober-/Unter-Labels in die Maps ergaenzen (im Datenbestand sichtbar: `Cinéma`,
`Histoire`, `Séries`, `Politics and society - ...`, `Política y sociedad - ...`).
**Bricht nichts:** rein additive neue Keys; ARTE.DE (deutsche Keys) und alle
Nicht-ARTE-Sender unveraendert.

## Vorgeschlagene Reihenfolge

1. **Schema** (`genre`, `slot`, `category`-Wert `Events`) -- Voraussetzung fuer 1/2/7.
2. **country-Shape-Filter** (Befund 6+8) -- klein, hoher Nutzen, rein filternd.
3. **Marker auf Topic + Suffix-Marker** (Befund 9+10) -- klein, additiv.
4. **Topic-Routing** (Befund 1+2+7) -- Kern; Genre-Set + Pipe-Split (beide geprueft).
5. **Heuristik-Feinschliff** (Befund 4+5) -- Credit-Schnitt, klammerlose Episoden (mit Guards).
6. **ARTE-Taxonomie mehrsprachig** (Befund 11).
7. **2. Pass**: Schreibvarianten-/Alias-Normalisierung (Befund 3), Serien-/Slot-
   Extraktion aus `clean_title` (Doppelpunkt-Muster).

## Anhang

- `analysis/_verify_heuristics.py` -- Verifikation Q3 (Genre-Set) + Q4 (Pipe-Split)
  gegen die ganze DB. `PYTHONIOENCODING=utf-8 python analysis/_verify_heuristics.py`.
