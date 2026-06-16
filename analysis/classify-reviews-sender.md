# Phase 3 Heuristik-Review

> Sichtung der `theke classify`-Ergebnisse fuer die Sender in
> `build/theke.db` (19.277 Zeilen, 377 verschiedene Topics). Nur Ueberblick und
> Befunde -- noch keine Code-Aenderung. Reproduktion siehe Abschnitt "Anhang".

## 3sat

### Kernbefund

`classify` setzt fuer jeden Nicht-ARTE-Sender blind `series_name = topic`
(`classify.py:112-115`, Pass 4). Bei 3Sat ist `topic` aber nur in ~63 % der
Faelle ein echter Serientitel. Der Rest sind **Format-** oder **Genre-Rubriken**,
die faelschlich als Serie landen.

**~7.125 von 19.277 Zeilen (37 %) tragen einen falschen `series_name`.**

Beispiel (das vom Sichten bekannte):

```
topic="Film"  title="Der andere Blick: Die Beschuetzer"
  -> clean_title="Der andere Blick: Die Beschuetzer"
     series_name="Film"      <-- falsch, muss NULL sein
     category="Beitrag/Episode"
```

Die echte Serie ("Der andere Blick") steckt hier im `clean_title` vor dem
Doppelpunkt -- genau das Material, das der geplante **2. Durchlauf** (Vergleich
mehrerer Datensaetze) spaeter nach `series_name` hebt. Damit das funktioniert,
**muss `series_name` jetzt NULL bleiben** statt mit der Rubrik gefuellt zu werden.

### Befund 1 -- `series_name` enthaelt Format/Genre statt Serie

Die 3Sat-Topics zerfallen in drei Klassen. Vorschlag, das Topic je nach Klasse
unterschiedlich zu routen statt es immer in `series_name` zu schreiben:

```
3Sat-Topic --+--> Format-Wort       --> category (Format),  series_name=NULL, genre=NULL
             +--> Genre-Rubrik       --> genre,              series_name=NULL
             +--> echter Programmtitel --> series_name        (der lange Tail)
```

#### Mengengeruest

| Bucket  | Zeilen | Anteil | Topics | series_name heute |
|---------|-------:|-------:|-------:|-------------------|
| format  |  1.682 |  8,7 % |   5    | falsch (= category) |
| genre   |  4.872 | 25,3 % |  20    | falsch (Rubrik)     |
| strand  |    266 |  1,4 % |  10    | falsch (Programmslot, Graubereich) |
| sender  |    305 |  1,6 % |   1    | falsch ("3sat")     |
| series  | 12.152 | 63,0 % | 341    | korrekt             |

Von den ~7.125 zu korrigierenden Zeilen haben **1.076** das Doppelpunkt-Muster
`Serie: Episode` im `clean_title` -- diese Serie kann der 2. Pass spaeter
zurueckgewinnen. Der Rest sind echte Einzelfilme/-beitraege ohne Serienbezug,
fuer die `series_name=NULL` schlicht korrekt ist.

#### Bucket "format" (5 Topics, -> spaeter Spalte `category`)

`Film`, `Spielfilm`, `Fernsehfilm`, `Dokumentarfilm`, `Dokumentation`

Doppelt falsch: hier ist heute `series_name == category` (z. B. "Good Bye,
Lenin!" hat `series_name="Spielfilm"` **und** `category="Spielfilm"`). Diese
Topics matchen bereits `CATWORD` in `classify.py` -- der Format-Teil ist also
schon erkannt, nur eben zusaetzlich faelschlich als Serie dupliziert.

#### Bucket "genre" (20 Topics, -> spaeter Spalte `genre`)

`Reise`, `Natur`, `Musik`, `Tiere`, `Geschichte`, `Politik und Gesellschaft`,
`Esskulturen`, `Kulturdoku`, `Kultur`, `Gesellschaft`, `Wissen`, `Buch`,
`Wissenschaftsdoku`, `Theater`, `Maerchen`, `Kabarett`, `Kabarett & Comedy`,
`Kabarett / Comedy`, `mehr Kabarett`, `Kulturdoku in 3sat`, `3sat-Kulturdoku`

Diese editorialen Themen-Rubriken sammeln voellig unterschiedliche Sendungen
(unter `Natur` z. B. "Faszinierende Erde: Gletscher", "mareTV: Norderney",
"Geheimnisvolle Wiesenwelt"). Als Serie unbrauchbar, als Genre-Signal aber
wertvoll -- heute geht diese Information komplett verloren (siehe Befund 2).

#### Graubereich "strand" (10 Topics) -- Entscheidung offen

Programmslots/Sendeplaetze, keine Serien: der Inhalt ist meist ein
eigenstaendiger Film, dessen Titel im `clean_title` steht.

`Der Fernsehfilm der Woche`, `ZDF-Fernsehfilm`, `Das kleine Fernsehspiel`,
`Dokumentarfilmzeit`, `Herzkino`, `Krimisommer`, `3satPublikumspreis`,
`3satZuschauerpreis`, `Festspielsommer`, `Retro-Serie: Lederstrumpf`

Beispiel: `topic="Herzkino"` -> Film "Das Maedchen mit dem indischen Smaragd".
"Herzkino" ist weder Serie noch Genre noch Format. Kandidaten:
`series_name=NULL` (mein Vorschlag) **oder** ein eigenes Feld "strand/slot".
`Retro-Serie: Lederstrumpf` ist ein Sonderfall -- die echte Serie ("Lederstrumpf")
steckt nach dem Praefix.

> **Offene Frage 1:** Strands wie Einzelfilme behandeln (`series_name=NULL`) oder
> als eigene Dimension erfassen?

#### Graubereich "event" (innerhalb "series" belassen) -- Entscheidung offen

Wiederkehrende Marken-Events, die ich vorerst als legitimen `series_name`
eingestuft habe, aber diskutabel: `Berlinale`, `Buchmesse`, `Grimme Preis`,
`Wiener Opernball`, `MuVi-Preis`, `Tage der deutschsprachigen Literatur`,
`3satFestival`, `Festspielsommer`.

> **Offene Frage 2:** Events als Serie zaehlen oder ausnehmen?

### Befund 2 -- `category` vermischt Format und Genre

`category` traegt heute zwei verschiedene Konzepte im selben Feld:

| Konzept | Werte | Quelle im Code |
|---------|-------|----------------|
| **Format/Typ** (was es *ist*) | Spielfilm, Fernsehfilm, Dokumentarfilm, Kurzfilm, Magazin, Reportage, Clip, Beitrag/Episode, unklar | Metazeile, CATWORD, Dauer-Prior |
| **Genre/Thema** (wovon es *handelt*) | Reise, Natur, Musik, Geschichte, ... | 3Sat-Topic-Rubrik (geht heute verloren) |

Heutige `category`-Verteilung fuer 3Sat:

```
8016  Beitrag/Episode      <- Dauer-Prior (1801-1800s), schwaches Signal
7212  unklar               <- Dauer-Prior, kein Signal
2187  Clip
 886  Fernsehfilm          \
 660  Spielfilm             |  echte Format-Signale aus Metazeile/CATWORD
 226  Dokumentarfilm        |
  35  Kurzfilm             /
 ...  (Magazin, Thriller, Drama, Komoedie, Krimi, ...)
  20  None
```

Solange beide Konzepte um ein Feld konkurrieren, geht das Genre-Signal aus dem
Topic verloren bzw. landet faelschlich in `series_name`. **Eine getrennte Spalte
`genre` loest Befund 1 und Befund 2 in einem Zug:** ein Topic wird dann sauber
dreigeteilt geroutet (Format -> `category`, Genre -> `genre`, Serie ->
`series_name`), nichts kollidiert mehr.

Empfehlung (Schema): neue Spalte `genre` in Tabelle `mediathek` und in
`CLASSIFY_COLS`. `category` bleibt rein Format/Typ.

### Befund 3 -- Schreibvarianten zersplittern denselben Serientitel

Die Quelle liefert denselben Programmtitel uneinheitlich; jede Variante wird
heute ein eigener `series_name`. Relevant fuer den 2. Pass (Normalisierung) und
die spaetere Queue-Deduplizierung:

| Variante A | Variante B | weitere |
|------------|------------|---------|
| `nano` (1296) | `NANO` (824) | `NANO Doku`, `NANO Talk` |
| `makro` (107) | `MAKRO` (54) | |
| `Schweizweit` (50) | `SCHWEIZWEIT` (57) | |
| `kinokino` (120) | `KinoKino` (8) | |
| `Pop Around the Clock` (85) | `Pop around the clock` (2) | |
| `NETZ NATUR` (8) | `Netz Natur` (2) | |
| `Museums-Check` (36) | `Museumscheck` (25) | |
| `Grimme Preis` (23) | `Grimme-Preis` (17) | |
| `Inspektor Jury` (7) | `Insepktor Jury` (1) | Tippfehler in der Quelle |

Auch Apostroph-Varianten (`Liebesg´schichten`, `Fraueng'schichten` vs.
`Fraueng´schichten`) und Tippfehler (`Traumfhafte Bahnstrecken der Schweiz`)
kommen vor. -> Case-/Diakritika-Normalisierung beim Serien-Matching im 2. Pass.

### Befund 4 -- "- Film von <Regisseur>"-Credit bleibt im clean_title

Das bare Wort **"Film"** steht *nicht* in `CATWORD`, nur die Komposita
(Spielfilm, Fernsehfilm, ...). 3Sat haengt aber an Doku-Beitraege haeufig einen
Regie-Credit `- Film von <Name>`/`- Dokumentarfilm von <Name>` an -- der wird weder als Metazeile erkannt noch
abgeschnitten und bleibt im `clean_title` stehen:

```
title="Schwanger auf Norderney - Film von Birgit Stamerjohanns"
  -> clean_title="Schwanger auf Norderney - Film von Birgit Stamerjohanns"  <-- Credit-Rest
```

**174 clean_titles** enthalten "Film von". Anders als bei der echten Metazeile
ist hier *nichts* zu gewinnen: von 347 "Film von"-Titeln traegt **kein einziger**
ein nachgestelltes ", Land Jahr" -- es ist reiner Regie-Credit. Also nur
abschneiden (wie der `PIPESUF`-Suffix), keine country/year-Extraktion.
Betroffen v. a. `Die Nordreportage`, `Terra X`, `ZDF.reportage`.

### Befund 5 -- Episoden-Notation ohne Klammern wird nicht erkannt

`PART` (`(n/m)`) und `SE_B` (`(Staffel N, Folge M)`) verlangen **Klammern**.
3Sat schreibt die Staffel/Folge-Angabe aber oft *ohne*:

```
"Unsere wilde Schweiz 3/4"              -> episode/episode_count NULL, "3/4" bleibt im clean_title
"Wunderwelt Schweiz 2/4 - Winterliches Graubuenden"
"Die wilden Philippinen - Teil 1"      -> episode NULL
"Wilder: Frost, Staffel 3, Folge 3"    -> season/episode NULL
```

Vorsicht: "3 1/2 Stunden" ist ein Filmtitel!

**176 episodische Titel** liefern weder `season` noch `episode`; bei **42**
bleibt zusaetzlich die "n/m"-Angabe im `clean_title` stehen. Kandidaten fuer
zusaetzliche Muster: ` <n>/<m>` (ohne Klammern, am Wortende), `- Teil <n>`,
`Staffel <n>, Folge <m>` (ohne Klammern).

### Befund 6 -- Metazeile-Falschtreffer auf das Sendedatum

`META` matcht `CATWORD <country> <year>`. Ein Titel `... <CATWORD> vom
DD. Monat YYYY` triggert das faelschlich: das Sendedatum wird als Land+Jahr
gelesen.

```
title="Slowenien Magazin vom 21. September 2023"
  -> category="Magazin"  country="vom 21. September"  year=2023   (alles falsch)
```

**18 Zeilen** (alle `Slowenien Magazin`) bekommen so ein `country="vom ..."`.
Das `year` ist hier ausserdem das **Sende-**, nicht das Produktionsjahr -- ein
generelleres Problem: `year` aus Metazeile/`(YYYY)` mischt beide Bedeutungen,
was das spaetere TMDB-Matching stoeren kann. Fix-Ansatz: Metazeile verwerfen,
wenn der country-Slot mit "vom" beginnt bzw. wie ein Datum aussieht.

### Kleinere Beobachtungen

- **country-Normalisierung:** Quell-Tippfehler `Grossbritanien` (statt
  Grossbritannien) und Leerzeichen-Artefakte wie `Australien/China/ Deutschland`
  (zusammen 11 Zeilen). Kosmetisch, erst beim Land-Mapping relevant.
- **degenerierter clean_title:** die 20 unklassifizierten Zeilen aus Befund 7
  sind zugleich die einzigen mit `clean_title IS NULL`; sonst keine leeren Titel.

### Empfehlung (Reihenfolge)

1. **classify-State entkoppeln** (Befund 7): nicht mehr `status` recyceln, sonst
   bleiben 3.873 Zeilen unklassifiziert. Vorbedingung fuer alles Weitere, da
   sonst Reklassifikation unvollstaendig ist.
2. **Spalte `genre`** zu `mediathek` + `CLASSIFY_COLS` hinzufuegen; `category`
   auf Format/Typ reduzieren (Befund 1 + 2).
3. **Topic-Routing pro Sender**: 3Sat-Topic nach Format / Genre / Serie
   aufteilen (Format- und Genre-Set kuratiert, analog `ARTE_CAT`/`ARTE_SUB`).
   Format/Genre/Strand/Sender -> `series_name=NULL`.
4. **Heuristik-Feinschliff**: `- Film von <Name>`-Credit abschneiden (Befund 4);
   klammerlose Episoden-Notation `n/m` / `Teil n` / `Staffel n, Folge m` erkennen
   (Befund 5); Metazeile bei `vom <Datum>` verwerfen (Befund 6).
5. Strand- und Event-Behandlung klaeren (offene Fragen 1 + 2).
6. Schreibvarianten + country-Normalisierung erst im 2. Pass (nicht jetzt).

> **Offene Frage 3 (Genre-Quelle):** vom Nutzer bewusst zurueckgestellt -- wir
> machen erst 3Sat, andere Sender spaeter. Die Wahl zwischen "kuratiertes Set
> pro Sender" und "Heuristik + kleines Set" faellt, wenn mehr Sender gesichtet
> sind.

### Anhang -- Reproduktion

- `analysis/_topics_dump.txt` -- alle 377 Topics mit Haeufigkeit + 2
  Beispiel-Titeln (Basis der Bucket-Einteilung).
- `analysis/_bucket_3sat.py` -- Bucket-Logik + Mengengeruest (Befund 1).
- `analysis/_audit_3sat.py` -- Tiefen-Audit der uebrigen Felder (Befund 4-7:
  clean_title-Reste, country, year, season/episode, language, flags, confidence).

Ausfuehren mit `PYTHONIOENCODING=utf-8 python analysis/<datei>.py`. Alle drei
sind temporaere Hilfsdateien (Praefix `_`), kein Bestandteil des CLI.

## ARD

### Kernbefund

ARD ist mit **154.478 Zeilen** der groesste Sender und ein Dachsender mit
**1.758** verschiedenen Topics. Anders als bei 3Sat sind die Topics fast immer
**echte Programmnamen** (Nachrichten, Regionalmagazine, Telenovelas, Tatort) --
der 3Sat-Hauptbefund (Topic = Format/Genre-Rubrik) trifft hier also nur am Rand
zu. Dafuer treten drei neue Muster auf, alle mit derselben Wurzel:

> **Gemeinsame Ursache (Befund 7 + 9, Teil von 3):** `series_name = topic` wird
> *woertlich* uebernommen. Die Titel-Reinigungspaesse -- Pass 1 `take_parens`
> (Klammer-Marker) und der `PIPESUF`-Suffixschnitt -- laufen nur auf dem
> **Titel**, nie auf dem Topic. Jede Verunreinigung im Topic (Marker, Pipe,
> Gross-/Kleinschreibung, Untertitel) landet damit ungefiltert im `series_name`.

### Treffen die 3Sat-Befunde 1-6 zu?

| Befund | ARD | Belege |
|--------|-----|--------|
| 1 series_name=Format/Genre | **teilweise**, klein | Format/Strand-Topics ~1.529 Z.: `Filme in der ARD` (1.069), `Film` (375), `Dokumentarfilm` (82). Genre-Rubriken wie bei 3Sat: praktisch keine. |
| 2 category mischt Format/Genre | **strukturell ja** | Gilt sender-uebergreifend; bei ARD liefern die Topics aber kaum Genre-Signal, die `genre`-Spalte bliebe meist leer. |
| 3 Schreibvarianten | **ja, stark** | Reine Case-Varianten, 20 Gruppen, **~9.074 Z.** (siehe Befund-Erweiterung unten). |
| 4 "Film von"-Credit | **kaum** (10 Z.) | ARD ist nicht in `TITLE_META_SENDERS`, zieht Metazeile aus der Beschreibung -- "Film von" im Titel wird gar nicht angefasst. |
| 5 Episoden ohne Klammern | **ja** (911 Z.) | u. a. `- Teil 2/2`, roemisch `Teil III`, `Teil 5`. |
| 6 Datum-Falschtreffer "vom" | **nein** (0 Z.) | Date-Titel treffen die Beschreibungs-Metazeile nicht. |

Erweiterung zu **Befund 3**: bei ARD ist die Zersplitterung fast rein
**Gross-/Kleinschreibung** und damit mechanisch normalisierbar:

```
aktuell (18 Uhr) (1153)   vs  Aktuell (18 Uhr) (867)
tagesschau (1654)         vs  Tagesschau (5)
tagesthemen (986)         vs  Tagesthemen (3)
ZAPP (325)                vs  Zapp (23)
NACHTCAFÉ (85)            vs  NACHTCAFé (19)      <- Akzent-Case
report MÜNCHEN (41)       vs  report München (49)
```

### Befund 7 -- Pipe-Suffix im Topic bleibt im series_name

`PIPESUF` schneidet einen `| Reihe`-Suffix nur vom **Titel**, nicht vom Topic.
Da `series_name = topic`, behalten **12.176 Zeilen** den Pipe-Suffix:

```
series_name="buten un binnen | regionalmagazin"  (1703)  -> sollte "buten un binnen"
            "hr Retro | hessenschau"             (899)
            "buten un binnen | sportblitz"       (283)
            "alpha Lernen | Physik"              (10)
```

Das zersplittert dieselbe Sendung (`buten un binnen | regionalmagazin / sportblitz
/ wetter`; `hr Retro | hessenschau / Abendschau / Der Markt`).

> **Offene Frage 4:** Die Serie steht **mal vor, mal hinter** dem Pipe:
> bei `buten un binnen | regionalmagazin` ist die Serie *vorne*, bei
> `Auf Spurensuche | ARD Wissen` (23) bzw. `Mein Körper | ARD Wissen` (7) ist
> `ARD Wissen` die *Dachmarke* und der Show-Titel steht vorne. Blindes Abschneiden
> des Suffixes waere also nicht immer richtig.

### Befund 8 -- Beschreibungs-Metazeile zieht Satzfragmente (falsche category/country/year bei hoher Confidence)

Weil ARD nicht in `TITLE_META_SENDERS` steht, sucht `META` die
`CATWORD <country> <year>`-Sequenz in der **Beschreibung** -- also in
Fliesstext/Credits. Das produziert frei erfundene Felder, und zwar mit
**Confidence 0.9** (Metazeile gilt als sicher), was den Fehler besonders
heimtueckisch macht:

```
title="Deutschland 2050: Die Zukunft und die Klimakrise"
  -> category="Serie"  country="über den Klimawandel aus dem Jahr"  year=2019   (frei erfunden)

title="Puccini · Magier der Leidenschaft · Doku · ... · SR · 2008"
  -> category="Dokumentation"  country="von"  year=2008
```

Sichtbar falsch sind **43 Zeilen** mit Satzfragment-`country` (`von` 12,
`· Deutschland` 8, `über den Klimawandel aus dem Jahr` 5, ...). Die Dunkelziffer
bei `category`/`year` ist hoeher, da jede Beschreibung mit einem CATWORD + Jahr
falsch greifen kann. Fix-Ansatz: Metazeile nur akzeptieren, wenn der
country-Slot wie ein Land aussieht (Grossbuchstabe, keine Funktionswoerter wie
`von/über/aus/im`).

### Befund 9 -- Klammer-Marker im Topic nicht erkannt

Pass 1 `take_parens` (Audiodeskription/Gebaerdensprache/OmU -> Flags/Sprache)
laeuft nur auf dem Titel. Im Topic bleibt der Marker stehen:

```
topic="tagesschau (mit Gebärdensprache)" (812)
  -> series_name="tagesschau (mit Gebärdensprache)"   (statt series_name="tagesschau", flag S)
```

Doppelter Schaden: der `series_name` wird vom regulaeren `tagesschau` (1654)
abgespalten, **und** das Flag `S` (Gebaerdensprache) wird nicht gesetzt. Bei ARD
betrifft das nur dieses eine Topic (812 Z.), das Muster ist aber generisch und
duerfte bei anderen Sendern wiederkehren.

### Anhang (ARD)

- `analysis/_audit_ard.py` -- ARD-Audit: Befund-1-6-Recurrence + Befunde 7-9
  (Pipe-Topics, Beschreibungs-Metazeile, Marker/Case-Varianten).
  Ausfuehren mit `PYTHONIOENCODING=utf-8 python analysis/_audit_ard.py`.

## SRF

> Ab hier laeuft das Audit ueber das generische `analysis/_audit_sender.py`
> (siehe Anhang). Die Befunde 1-9 sind der etablierte Katalog (Definitionen in
> Abschnitt 3Sat/ARD); pro Sender wird ihr Zutreffen geprueft und Neues ab 10
> nummeriert.

### Kernbefund

SRF (102.286 Z., 895 Topics) wird massiv von **Clip-Sammeltopics** dominiert:
allein `Sport-Clip` hat **41.657** Zeilen, alle Clip/Flash/Videos-Sammeltopics
zusammen **48.996 (48 %)**. Das ist Befund 1 in Reinform und im groessten
Massstab: `series_name="Sport-Clip"` / `"Sportflash"` / `"SRF News Videos"` /
`"Paris 2024 Clips"` sind Format-/Clip-Rubriken, keine Serien. Routing-Loesung
wie gehabt: -> `category` (meist `Clip`), `series_name=NULL`.

### Treffen die Befunde 1-9 zu?

| Befund | SRF | Belege |
|--------|-----|--------|
| 1 series_name=Format/Genre | **ja, dominant** | 48.996 Z. Clip/Flash/Videos-Sammeltopics; `Film` 83. |
| 2 category mischt Format/Genre | strukturell ja | Topics liefern kaum Genre. |
| 3 Schreibvarianten (Case) | **nein** | 1 Gruppe, 6 Z. |
| 4 "Film von"-Credit | **kaum** (5 Z.) | SRF nicht in `TITLE_META_SENDERS`. |
| 5 Episoden ohne Klammern | **ja** (439 Z.) | |
| 6 Datum "vom" | **nein** (0 Z.) | |
| 7 Pipe-Suffix | **nein** (0 Z.) | |
| 8 Beschreibungs-Metazeile | **ja, winzig** | Metazeile feuert nur 65x, davon ~56 Müll-`country` (`zur Women’s EURO`, `der FIFA erzählt den Weg zur WM`) -- hohe Fehlerquote, kleine Menge. |
| 9 Marker im Topic | **ja, Suffix-Variante** | siehe Befund 10. |

### Befund 10 -- Barrierefreiheits-Marker als Suffix ohne Klammern (" in Gebärdensprache")

Verwandt mit Befund 9, aber mechanisch anders: SRF haengt den Marker **ohne
Klammern** als Suffix an, daher greift weder `take_parens` (sucht Klammern) noch
sonst etwas:

```
topic="Tagesschau in Gebärdensprache" (1689)
  -> series_name="Tagesschau in Gebärdensprache"   (statt "Tagesschau", flag S)
```

**6.239 Zeilen** tragen das Suffix ` in Gebärdensprache` (Tagesschau 1689,
Schweiz aktuell 1097, Boerse 839, ...); das Flag `S` ist dabei nur **1x** gesetzt.
Doppelschaden wie Befund 9: Abspaltung vom Basis-`series_name` plus fehlendes
Flag. Fix-Ansatz: Marker-Erkennung nicht nur in Klammern, sondern auch als
Titel-/Topic-Suffix (` in Gebärdensprache`, ` mit Gebärdensprache`).

### Anhang (SRF)

- `analysis/_audit_sender.py "SRF"` -- generisches Sender-Audit (alle Sender
  ab hier). Ausfuehren mit `PYTHONIOENCODING=utf-8 python analysis/_audit_sender.py "SRF"`.

## ZDF

### Kernbefund

ZDF (94.875 Z., 1.448 Topics) ist der Schwester-Sender von 3Sat und verhaelt
sich auch so: er steht mit in `TITLE_META_SENDERS` (Metazeile aus dem **Titel**)
und nutzt **dieselben Genre-Rubriken** als Topic. Alle 3Sat-Befunde kehren
wieder, kein neues Muster. `series_name`-Rubriken: **5.222 Z.** Genre
(`Politik` 2.392, `Sport` 1.261, `Nachrichten` 980, dann die 3Sat-Garnitur
`Politik und Gesellschaft`/`Kultur`/`Wissen`/`Musik`/`Geschichte`/...) plus den
Strand `ZDFinfo Doku` (3.480).

### Treffen die Befunde 1-9 zu?

| Befund | ZDF | Belege |
|--------|-----|--------|
| 1 series_name=Format/Genre | **ja** | 5.222 Z. Genre-Rubriken + 3.480 Z. Strand `ZDFinfo Doku`. |
| 2 category mischt Format/Genre | **ja** | Genre-Rubriken wie bei 3Sat -- `genre`-Spalte gut befüllbar. |
| 3 Schreibvarianten (Case) | **ja** (7 Gruppen, 6.192 Z.) | `ZDFinfo Doku`/`ZDFinfo doku`, `NANO`/`nano` (1206/246), `Scobel`/`scobel`. |
| 4 "Film von"-Credit | **ja** (68 Z.) | ZDF in `TITLE_META_SENDERS`, bare "Film" nicht in CATWORD. |
| 5 Episoden ohne Klammern | **ja** (170 Z.) | season/episode sonst gut (14.114/14.818 via `(S/E)`). |
| 6 Datum "vom" | **ja** (12 Z.) | `vom 1. Februar`, plus Sport-Fragmente `gegen Norderstedt - DFB-Pokal`. |
| 7 Pipe-Suffix | **nein** (0 Z.) | |
| 8 Beschreibungs-Metazeile | **n. a.** | ZDF zieht Metazeile aus dem Titel, nicht der Beschreibung. |
| 9/10 Marker im Topic | **kaum** (2 Z.) | ZDF setzt Marker im Titel -- Flags `S` 4.083, `A` 3.583 korrekt. |

Keine neuen Befunde: ZDF ist der Modellfall, fuer den die 3Sat-Heuristik
gebaut wurde. Bestaetigt die Genre-Rubrik-Liste (gemeinsam mit 3Sat) und liefert
drei haeufige Zusatz-Rubriken (`Politik`, `Sport`, `Nachrichten`) fuers
kuratierte Genre-Set.

## NDR

### Kernbefund

NDR (49.318 Z., 432 Topics) ist ein **gut klassifizierter** Sender: die Topics
sind durchweg echte Programm-/Serien-Namen (Regionalmagazine `NDR Info`,
`Nordmagazin`, `Hamburg Journal`; Serien `Morden im Norden`, `Großstadtrevier`,
`Die Kanzlei`). **Keine** Format-/Genre-Rubriken als Topic -- Befund 1 entfaellt.

### Treffen die Befunde 1-9 zu?

| Befund | NDR | Belege |
|--------|-----|--------|
| 1 series_name=Format/Genre | **nein** (0) | keine Rubrik-Topics. |
| 3 Schreibvarianten (Case) | **nein** (19 Z.) | nur `NDR Talk Show Classics`/`classics`. |
| 4 "Film von" | **nein** (1) | NDR zieht Metazeile aus der Beschreibung. |
| 5 Episoden ohne Klammern | **marginal** (36 Z.) | |
| 6 Datum "vom" | **nein** (0) | |
| 7 Pipe-Suffix | **nein** (0) | |
| 8 Beschreibungs-Metazeile | **ja, winzig** (11 Z.) | `über den Klimawandel aus dem Jahr` u. a.; Metazeile feuert nur 57x. |
| 9/10 Marker im Topic | **nein** (0) | |

### Kleinere Beobachtung -- Editions-/Zeitslot-Suffixe

Regionalmagazine erscheinen unter Haupt- **und** Ausgaben-Topic
(`Hamburg Journal` 5.991 vs `Hamburg Journal 18:00 Uhr` 1.129;
`Nordmagazin` 6.922 vs `Nordmagazin - Land und Leute` 489;
`Hallo Niedersachsen` 5.425 vs `Niedersachsen 18.00` 1.038). Ob das echte
getrennte Ausgaben sind oder zu vereinheitlichen waere, ist eine inhaltliche
Frage fuer den 2. Pass -- kein klarer Fehler. Sonst keine neuen Befunde.
