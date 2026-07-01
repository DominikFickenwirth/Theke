# Enrich-Verbesserungen -- Befund aus build/theke.db (status='2', Movie)

> Analysebasis: `build/theke.db` read-only, waehrend der Bulk-Match noch lief
> (status '2' = Movie-Zeilen, die der Bulk-Match nicht sicher matchen konnte).
> Snapshot: 2493 status-'2'-Movie-Zeilen. Alle Zahlen sind aus dieser Menge, wo
> nicht anders vermerkt. Zaehlungen koennen sich leicht aendern, da der Lauf noch
> lief.
>
> HINWEIS ZU BEISPIELEN: der Halbgeviertstrich (U+2013) aus den Filmtiteln ist
> hier als normaler Bindestrich ` - ` wiedergegeben, damit die Datei CP-1252 bleibt.
> In den Rohdaten ist es U+2013 -- genau der Punkt bei Artefakt A1.

## Kernaussage

Das eigentliche Match-Gate ist solide. Die verbesserungsfaehigen Faelle sind
ueberwiegend **Enrichment-Artefakte im `clean_title`** und ein paar **fehlende
Marker**, nicht das Scoring. Vier Artefakte erklaeren den Grossteil und sind
einzeln, sicher und lokal fixbar (keine API). Ein fuenfter Punkt (Kategorie:
Krimi-Reihen sind TMDB-Serien) ist der bewusst nach hinten geschobene Serien-Fall.

## Priorisierte Artefakte im clean_title

| # | Artefakt | Zeilen | Fix-Ort | Sicherheit |
|---|----------|-------:|---------|-----------|
| A1 | Redundanter Reihen-Suffix `"<Episode> - <Serienname>"` | 366 | neuer Pass nach Topic-Routing | hoch (tail == series_name pruefbar) |
| A2 | `"- Audiodeskription"` als blanker Suffix (nicht geklammert) | 140 | `SUFFIX_MARKERS` | hoch |
| A3 | `"(klare Sprache)"` / `"(Klare Sprache)"` nicht erkannt | 38 | `MARKERS` | hoch |
| A4 | Blanke Episoden-Nummer `"(n)"` am Ende | 164 | Episode-Notation (Pass 2) | mittel-hoch |
| A5 | ARTE `"<Titel im Zitat>" vu par / analyse par ...` | 15 (+17 Zitat) | eigener ARTE-Pass | mittel |

### A1 -- Redundanter Reihen-Suffix (366 Zeilen)

Format der ARD-Degeto-Reihen: `<Episodentitel> - <Serienname>`, wobei der
Serienname exakt dem via Topic gerouteten `series_name` entspricht.

```
clean_title = 'Volles Haus - Anna und ihr Untermieter'      series_name = 'Anna und ihr Untermieter'
clean_title = 'Mutterliebe - Billy Kuckuck'                 series_name = 'Billy Kuckuck'
clean_title = 'Aber bitte mit Sahne! - Billy Kuckuck'       series_name = 'Billy Kuckuck'
```

Der echte Filmtitel ist der Kopf ('Volles Haus'); der Schwanz dupliziert nur den
schon bekannten `series_name`. Fix: wenn `clean_title` auf ` <sep> <series_name>`
endet (sep = Halbgeviertstrich/Bindestrich, series_name case-insensitiv gleich),
den Suffix abschneiden. Weil die Gleichheit mit `series_name` geprueft wird, ist
das treffsicher.

ACHTUNG Abgrenzung: 226 weitere em-dash-Zeilen haben tail != series_name -- die
NICHT anfassen. Das sind zwei andere Konventionen:
- Titel-Untertitel bei Dokus: `'Stealing Giants - Der grausame Handel mit
  lebenden Elefanten'` (der Strich trennt Titel/Untertitel, kein Reihenname).
- Umgekehrte Reihenfolge `<Reihe> - <Episode>`: `'Der Kommissar und die Alpen -
  Sturz in den Tod'` (topic='Fernsehfilm', daher series_name leer). Hier ist der
  KOPF die Reihe. Ohne series_name-Anker nicht sicher trennbar -> in Ruhe lassen.

### A2 -- "- Audiodeskription" als blanker Suffix (140 Zeilen)

`MARKERS` faengt nur die geklammerte Form `(Audiodeskription)`. Die haeufige
blanke Suffix-Form ` - Audiodeskription` faellt durch: weder als Flag 'A' gesetzt
noch aus dem Titel entfernt.

```
'Exil - Audiodeskription'                       flags=''   (sollte 'A')
'Haus ohne Dach - Audiodeskription'             flags=''
'Raeuberhaende - Audiodeskription - Audiodeskription'      (sogar doppelt)
'Daheim in den Bergen:Vaeter - Audiodeskription'
```

Fix: `SUFFIX_MARKERS` um ` - Audiodeskription$` (und ` - Hoerfassung$`, 2 Zeilen)
erweitern -> Flag 'A', abschneiden. Auf doppeltes Vorkommen achten (wiederholt
strippen oder greedy).

### A3 -- "(klare Sprache)" nicht erkannt (38 Zeilen, alle Tatort)

Kein `MARKERS`-Eintrag. Folge: Klammer bleibt stehen UND blockiert den
Trailing-`(YYYY)`-Strip, weil `(2024)` dadurch nicht mehr am Zeilenende steht:

```
'Siebte Etage (2024) (klare Sprache)'          -> soll: 'Siebte Etage', year=2024, flag
'Feuer (2025) (klare Sprache)'
'Hubertys Rache (2022) (Klare Sprache)'        (auch Grossschreibung)
```

Ein einziger Fix loest zwei Artefakte (Titel + Jahr). Vorschlag: neuer
`MARKERS`-Eintrag `^Klare Sprache$` (case-insensitiv). Flag: passt semantisch zu
'E' (einfache/leichte Sprache) -- oder ein eigenes Flag, falls die Unterscheidung
klare/leichte Sprache spaeter zaehlt. Das ist eine kleine Design-Entscheidung.

### A4 -- Blanke Episoden-Nummer "(n)" am Ende (164 Zeilen)

`PART` matcht nur `(n/m)`; die blanke `(n)` wird weder als `episode` extrahiert
noch gestrippt. Nur 49 der 164 haben `episode` ueberhaupt gesetzt (aus anderer
Quelle).

```
'Volles Haus - Anna und ihr Untermieter (5)'   -> episode 5
'Das Konto (2)'
'Das Weisse Haus am Rhein (1)' / '... (2)'
'Tod im Hafenbecken - Der Amsterdam-Krimi (3)'
```

Fix: geklammerte `(n)` (1-2 Ziffern) am Titelende als `episode` uebernehmen (nur
falls noch unbesetzt) und strippen. Vorsicht/Guard: kein Jahr (`(19|20)dd` ist
schon separat behandelt), nur am Ende, 1-2 Ziffern. Zusammen mit A1 wird
`'Volles Haus - Anna und ihr Untermieter (5)'` zu `clean_title='Volles Haus'`,
`series_name='Anna und ihr Untermieter'`, `episode=5`.

### A5 -- ARTE "<Zitat> vu par / analyse par ..." (15 + 17 Zeilen)

ARTE-Kurzformate mit franzoesischer Kommentar-Syntax; der echte Titel steht im
Anfangszitat:

```
'"Overnight" vu par Sofiane Merabet - Anthropologue, ...'
'"45th Parallel" analyse par Wolfgang M. Schmitt'
'"Von Wundern und Superhelden" am Stuttgarter Ballett'   (Zitat ohne "vu par")
```

Fragwuerdig, ob diese Kurz-Kommentarstuecke ueberhaupt als Movie zaehlen sollten
(oft < 6 min, mehrsprachige ARTE-Dubletten). Zwei Optionen: (a) den Zitat-Inhalt
als Titel nehmen und `" ... vu par/analyse par ..."`-Schwanz strippen, oder
(b) diese ARTE-Kurzformate gar nicht als Movie fuehren (Kategorie/Filter). Klein
in der Zahl, niedrige Prioritaet -- als Notiz festgehalten.

## B -- Kategorie: Krimi-Reihen sind in TMDB Serien (grosser Block)

Der groesste zusammenhaengende Block der status-'2'-Fehlschlaege sind
Fernseh-Krimireihen, die `enrich` per `FICTION_TOPICS` bewusst auf Movie hebt,
die TMDB aber als **Serie** fuehrt -- daher kein Movie-Match moeglich:

```
Tatort                533     (allein 21 % aller status-2-Movies)
Polizeiruf 110         77
Praxis mit Meerblick   74
Nord bei Nordwest      49
Der Bozen-Krimi        42
Der Usedom-Krimi       41
Maria Wern, Kripo Gotland  33
Kommissar Van der Valk 32
Kommissar Dupin        25
Donna Leon             24
```

Das ist der bewusst nach Phase 13 geschobene Serien-Fall. Wenn man frueh etwas
mitnehmen will: die eindeutigsten TV-Reihen (allen voran **Tatort**, TMDB-Serie
1090) schon jetzt auf `category='Episode'` stellen wuerde ~800+ Zeilen aus dem
Movie-Bulk-Fehlerraum ziehen und sie ueber `find_episode_matches` matchbar machen
-- ABER dafuer fehlt bei fast allen die (season, episode), die
`find_episode_matches` als Gate braucht. Ohne Episodennummer bringt der Umzug
nach Episode noch keinen Match. Empfehlung: als Serien-Arbeit in Phase 13 lassen;
A4 (Episodennummern) ist ein sinnvoller Vorbau dazu.

## C -- Genre bei Enrich: Obergrenze durch die Datenquelle (nachgeprueft)

Klarstellung des Designs (Nutzer): "genre ist TMDB-only" ist schlampig -- gemeint
ist: `genre` wird NUR mit TMDB-Genre-Werten befuellt, aber das Befuellen soll
schon bei `enrich` passieren (kein Backfill nach Match vorgesehen). Frage war
also: ist Genre besser enrich-bar als die aktuellen ~4-5 %?

Nachgeprueft an 4722 Movie-Zeilen (status 2+3), aktuell 211 mit Genre (4 %):

- Die Beschreibungen sind ganz ueberwiegend **Handlungs-Prosa, keine
  strukturierte Metazeile mit Genre**. `META` greift ein CATWORD; ist das
  "Spielfilm"/"Fernsehfilm" (das Medium), gibt es kein Genre -- und ein zweites,
  echtes Genre-Wort steht meist gar nicht da. Nur ~7 % der genre-losen Filme
  haben ueberhaupt ein Genre-Wort in der Beschreibung, und das oft nur in Prosa
  ("Anruehrende Komoedie mit ...").
- Der EINE grosse verlaessliche Hebel ist **Krimi**: Topics/series_name mit
  `Krimi|Tatort|Polizeiruf|Kommissar|Mord|Wallander|Van der Valk|<Ort>-Krimi`
  sind praktisch immer Crime. Eine solche Regel (plus ein paar Kleinigkeiten:
  Herzkino->Romance, Komoedie/Thriller/Western als Titel-Kompositum) hebt die
  Coverage von **4 % auf ~29 %** -- davon 1141 von 1171 neuen = Crime.
- Wichtig: **Genre ist orthogonal zur Kategorie.** 660 der Crime-Treffer sind
  Tatort/Polizeiruf, die unter B nach Episode wandern -- sie behalten trotzdem
  Genre='Crime'. Die Krimi-Genre-Regel und die Serien-Liste (B) speisen sich aus
  derselben Vokabel und sollten dieselbe Erkennung teilen.

Fazit: Genre ist begrenzt besser enrich-bar (Crime-Hebel ~x7), ABER die
verbleibenden ~70 % sind **inhaerent genre-los** -- die Filmliste traegt fuer
gewoehnliche Spielfilme schlicht kein strukturiertes Genre. "95 % genre-los" ist
also groesstenteils Datenlage, keine Heuristik-Luecke. Empfehlung: die
Krimi/Romance-Topic-Regel bei `enrich` ergaenzen (holt den grossen Block), den
Rest als Datengrenze akzeptieren. Formulierung im Code/CLAUDE.md nachziehen
("genre wird bei enrich mit TMDB-Genre-Werten befuellt; kein Backfill").

## Umsetzungsreihenfolge (Kurzform)

A3 -> A2 -> A1 -> A4 -> B (Detailplan mit Tests weiter unten). A5 (ARTE vu par)
bleibt optionale Restarbeit, klein. C (Genre) entfaellt. Alle lokal, TDD.

## Validierung

Gegengeprueft mit den `theke enrich`-Tools und der Live-Funktion (nicht nur dem
DB-Snapshot):

- `enrich audit -c topic-marker` findet die Marker `klare Sprache`/
  `Audiodeskription` bereits als bekannte Vokabel -- aber nur im TOPIC. Die
  Regex `_TOPIC_MARKER` (theke/__init__.py) listet genau die Marker, die
  `enrich`s `MARKERS` im TITEL noch nicht strippt. Die Audit-Vokabel ist der
  natuerliche Wiederverwendungs-Kandidat fuer A2/A3.
- `enrich()` direkt auf die Beispiele laufen lassen bestaetigt alle vier:

```
IN  'Volles Haus - Anna und ihr Untermieter (5)'   (Strich = U+2013)
OUT clean='Volles Haus - Anna und ihr Untermieter (5)'  series='Anna und ihr Untermieter'  ep=None   (A1 + A4)
IN  'Siebte Etage (2024) (klare Sprache)'
OUT clean='Siebte Etage (2024) (klare Sprache)'  year=None                                          (A3, Jahr blockiert)
IN  'Exil - Audiodeskription'
OUT clean='Exil - Audiodeskription'  flags=''                                                       (A2, kein 'A')
```

## Entscheidungen (vom Nutzer, 2026-07-02)

- A3: `klare Sprache` mit Flag **'E'** zusammenlegen (wie leichte/einfache Sprache).
- A4: blanke `(n)` als **episode uebernehmen UND die Zeile auf `category='Episode'`**
  stellen (blanke laufende Nummer ist ein Episoden-Signal wie die anderen).
- B: Tatort & Co. **raus aus `FICTION_TOPICS`**, eigene **`SERIES_TOPICS`**-Liste;
  ein Topic darin -> `category='Episode'` (automatisch, nicht erst Phase 13).
- C: **entfaellt.** Solange `genre` nirgends genutzt/gefuellt wird, lohnt der
  Aufwand nicht. Die C-Analyse (unten) bleibt als Beleg, ist aber kein
  Arbeitspaket. (Falls spaeter doch: Krimi-Topic-Regel hebt 4 % -> ~29 %.)

## SERIES_TOPICS-Split (B) -- TMDB-verifiziert (search/multi, media_type)

Jeden Fiction-Topic per TMDB geprueft. Wichtigster Befund: **viele "Krimi-Reihen"
sind bei TMDB als EINZELFILME katalogisiert** ("<Reihe> - <Episodenname>" als
Movie-Titel), nicht als Serie. Die muessen Movie BLEIBEN -- sie sollen als Movie
matchen (ihr Fehlschlag kommt von fehlendem Jahr / vom A1-Suffix, nicht von der
Kategorie).

-> SERIES_TOPICS (category Episode), TMDB media_type = tv:
  Tatort, Polizeiruf 110, Donna Leon, Praxis mit Meerblick, Der Kroatien-Krimi,
  Daheim in den Bergen, Rebecka Martinsson, Kommissar Wallander, Mankells Wallander,
  Pfarrer Braun, Der Ranger - Paradies Heimat, Die Diplomatin,
  Der Kommissar und die Alpen (TMDB: Rocco Schiavone), Nord bei Nordwest,
  Mordkommission Istanbul, Mord in bester Gesellschaft, Die Inselaerztin,
  Der Bozen-Krimi

-> BLEIBEN Movie (TMDB media_type = movie -- als Einzelfilme katalogisiert!):
  Der Usedom-Krimi, Kommissar Dupin, Harter Brocken, Zimmer mit Stall,
  Anna und ihr Untermieter, Wolfsland, Ein Krimi aus Passau, Liebe am Fjord,
  Die drei von der Muellabfuhr, Der Wien-Krimi: Blind ermittelt, Die Bestatterin,
  Der Pate, Utta Danella

-> BLEIBEN Movie (Slots/Anthologien, kein TMDB-Reihentreffer):
  Maerchen in der ARD, Debuet im Dritten, Dokumentarfilmzeit, FilmMittwoch im Ersten,
  Spielfilm-Highlights, Filme im Ersten, Spielfilm in 3sat, Krimis im Ersten

-> UNSICHER (kein klarer TMDB-Treffer; im Zweifel Movie lassen, spaeter pruefen):
  Steirerkrimi, Kaethe und ich, Kluftingerkrimis (TMDB: "Kluftinger" = movie),
  Toni, maennlich, Hebamme, Krause

NUANCE fuer die movie-katalogisierten Reihen (Match, nicht Enrich): TMDB betitelt
sie "<Reihe> - <Episodenname>" (z. B. "Anna und ihr Untermieter - Dicke Luft"),
die Filmliste dreht es um: "<Episodenname> - <Reihe>". A1 macht clean_title zum
reinen Episodennamen ("Volles Haus"); der Match muss den Reihennamen (aus
series_name) wieder anfuegen oder beide Formen probieren. Das ist Match-Arbeit,
kein Enrich-Blocker -- hier nur als Notiz.

## Umsetzungsplan (spaeter; TDD, 2 Commits pro Schritt; C entfaellt)

Entscheidung Nutzer: **C (Genre) faellt weg** -- solange genre nirgends genutzt/
gefuellt wird, bringt der Aufwand nichts. Die C-Analyse oben bleibt als Beleg
stehen, ist aber kein Arbeitspaket. Reihenfolge A3 -> A2 -> A1 -> A4 -> B:

1. **A3 -- "(klare Sprache)" -> MARKERS, Flag 'E'.**
   - Test: `enrich(...,'Siebte Etage (2024) (klare Sprache)',...)` -> clean_title
     'Siebte Etage', year 2024, flags enthaelt 'E'. Auch Grossschreibung
     ("(Klare Sprache)"). Rot sehen -> Marker `^Klare Sprache$` (re.I) in MARKERS
     als ('flag','E') -> gruen.
   - Nebeneffekt: entsperrt den Trailing-(YYYY)-Strip (38 Zeilen Jahr+Titel).

2. **A2 -- " - Audiodeskription"/" - Hoerfassung" blanker Suffix -> SUFFIX_MARKERS,
   Flag 'A'.**
   - Test: 'Exil - Audiodeskription' -> clean 'Exil', flags 'A'; doppelte Form
     'X - Audiodeskription - Audiodeskription' -> clean 'X' (mehrfach strippen).
   - SUFFIX_MARKERS ist schon die richtige Stelle (bare, kein Paren). Regex
     ` [-–] Audiodeskription$` / ` [-–] Hoerfassung$`, ggf. in Schleife bis kein
     Treffer mehr.

3. **A1 -- redundanten Reihen-Suffix "<Titel> - <series_name>" strippen.**
   - Laeuft NACH Pass 4 (series_name steht dann). Test: clean_title
     'Volles Haus - Anna und ihr Untermieter', series_name 'Anna und ihr
     Untermieter' -> clean 'Volles Haus'. Negativ-Test: tail != series_name bleibt
     unveraendert ('Stealing Giants - Der grausame Handel ...').
   - Guard: nur wenn series_name gesetzt UND clean_title endet auf
     ` <sep> <series_name>` (sep Halbgeviert/Bindestrich, case-insensitiv).

4. **A4 -- blanke "(n)" am Ende -> episode + category Episode.**
   - Test: 'Das Konto (2)' -> episode 2, clean 'Das Konto', category Episode.
     Zusammen mit A1: 'Volles Haus - Anna und ihr Untermieter (5)' -> clean
     'Volles Haus', series_name 'Anna und ihr Untermieter', episode 5, Episode.
   - Guard: 1-2 Ziffern, am Ende, KEIN Jahr (das (YYYY) hat schon eine eigene
     Regel). Reihenfolge im Code: die (n)-Extraktion in Pass 2 (Episode-Notation)
     VOR dem (YYYY)-Strip; category-Auf-Episode laeuft ueber die bestehende
     episodic-Regel (episode gesetzt -> Episode, falls category None/Clip).
   - Achtung Wechselwirkung mit A1: erst (n) abtrennen, dann Reihen-Suffix, sonst
     steht series_name nicht am echten Zeilenende.

5. **B -- FICTION_TOPICS aufteilen: neue SERIES_TOPICS (-> Episode).**
   - Test: enrich mit topic 'Tatort', film-lang, kein Medium-Signal -> category
     Episode (nicht mehr Movie). enrich mit topic 'Der Usedom-Krimi' -> weiterhin
     Movie (movie-katalogisiert, bleibt im Movie-Lift).
   - Code: SERIES_TOPICS = {casefold...} (verifizierte tv-Liste oben); in enrich
     VOR dem FICTION_TOPICS-Lift pruefen -> category 'Episode', kat_src
     'series-topic'. Die movie-Reihen aus FICTION_TOPICS entfernen ODER dort
     belassen (sie sollen Movie bleiben -> in FICTION_TOPICS lassen). Nur die
     tv-Topics wandern nach SERIES_TOPICS und RAUS aus FICTION_TOPICS.
   - Config: analog zu `fiction_topics` ein `series_topics` in der Config-Union
     vorsehen (Nutzer kann nachpflegen).
   - README/CLAUDE.md: Serien-Topics-Mechanik dokumentieren.

Reihenfolge-Begruendung: A3/A2 sind isolierte Marker-Fixes (kein Zusammenspiel).
A1 vor A4 waere falsch (A4 muss die (n) zuerst abtrennen); daher A1 als Regel
NACH series_name, A4 in Pass 2 -- im Code ist die Ausfuehrungsreihenfolge (n)
-> Suffix, die Implementierungsreihenfolge A1-dann-A4 nur die der Commits.
