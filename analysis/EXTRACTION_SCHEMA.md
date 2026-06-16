# Phase 3 -- Extraktions-Schema je Sender (Filme + Serien-Episoden)

Datenbasis build/theke.db (698.554 Zeilen). Alle Quoten sind an den Echtdaten
gemessen (Skripte analysis/per_sender.py, film_conv.py, markers.py) und taugen
als hartkodierte Erwartungswerte fuer Tests. Familien wurden NUR gebildet, wo
die Konvention nachweislich identisch ist; Abweichler stehen einzeln.

Zielfelder: titel, serie_name, staffel, episode, kategorie, jahr, land,
sprache, gebaerdensprache(bool), trailer(bool), hoerfassung(bool),
eincod_ut(bool).

## A. Extraktions-Pipeline (Reihenfolge, sender-unabhaengiges Geruest)

Pass 1 -- **Marker aus dem title ziehen UND abschneiden** (Klammer-Suffixe).
  Liefert: hoerfassung, gebaerdensprache, eincod_ut, sprache-Hinweis, trailer,
  Mehrteiler. Reihenfolge wichtig: erst Marker entfernen, dann bleibt der
  saubere Titel. Controlled Vocabulary siehe B1 (enthaelt die Sender-Varianten!).
Pass 2 -- **Episodennotation** aus dem (noch markierten) title (siehe B2),
  abschneiden. Liefert staffel, episode. 4-stellige "Staffel" = Sendejahr, nicht
  Staffel -> staffel=NULL, episode=NULL, jahr ggf. setzen.
Pass 3 -- **Metazeile** (Kategorie+Land+Jahr) am sender-spezifischen Ort lesen
  (title-Suffix ODER description-Prefix ODER ARTE-topic), siehe C.
Pass 4 -- **serie_name** = bereinigter topic (ausser ARTE), siehe B3.
Pass 5 -- **sprache** final aus Sender-Default + Markern aus Pass 1 (B4).
Pass 6 -- **kategorie** aus Metazeile sonst duration-Prior + topic-Keywords (B5).

## B. Controlled Vocabularies (mit Sender-Varianten -- NICHT uniform!)

### B1. title-Klammer-Marker (case-insensitive, exakte Schreibweisen gemessen)
- hoerfassung=true:  `(Audiodeskription)` (Standard) | `(Hörfassung)`
  -> KiKA nutzt fast nur `(Hörfassung)` (1220x), Audiodeskription dort selten.
     ARD/ZDF/Dritte nutzen `(Audiodeskription)`. Beide Begriffe = Hoerfassung.
- gebaerdensprache=true: `(Gebärdensprache)` | `(mit Gebärdensprache)`
  -> AUSNAHME ORF: `(ÖGS)` (268x, Oesterr. Gebaerdensprache) -- sonst nirgends.
- eincod_ut=true: `(mit Untertitel)` | `(OmU)` | `(OmdU)`
  | `(Originalversion mit Untertitel)` (impliziert zugleich sprache!=de)
- sprache-Hinweis: `(Originalversion)`/`(OV)` (Audio != de, genaue Sprache offen)
  | `(engl.)` | `(Englisch)` | `(English)` -> en
  | `(frz.)` | `(franz.)` -> fr   (beide nur SRF in Menge)
- stumm/ohne Ton (nur Info, kein Zielfeld): `(stumm)` | `(ohne Ton)` |
  `(tlw. stumm)` -> v.a. NDR/SWR (historisches Material)
- trailer=true: title/topic enthaelt `Trailer|Teaser|Vorschau|Vorab|Preview`
  (insgesamt selten, <1%); zusaetzlich Heuristik duration < 120 s als Stuetze.
- Mehrteiler-Part: `(n/m)` z.B. `(1/2)`,`(2/3)`,`(5/6)` -> Teil n von m (Reihe
  ohne echte Staffel). ENTSCHEIDUNG: n -> episode, m -> neues Feld episode_count
  (episode nur setzen, wenn nicht schon eine echte Sxx/Exx-Notation griff).

### B2. Episodennotation -- drei disjunkte Formen (sender-spezifisch, siehe C)
- Form A `(S<n>/E<m>)`: 4-stellige n = Jahr (Tagesformat) -> verwerfen.
  Beispiel echt: `(S03/E12)`; Jahr-Form: `(S2025/E221)`.
- Form B `(Staffel <n>, Folge <m>)` oder `(Folge <m>)` -- NUR SRF.
- Form C fuehrendes `^<m>\. ` (Episodennummer, evtl. ohne Staffel) -- NUR KiKA.
  Sonderfall `^<m>\. Folge` = nur Nummer, kein Episodentitel.

### B3. serie_name = topic, bereinigt
- Standard: serie_name = topic; bei Episoden ist topic der Reihen-/Sendungsname.
- Suffix `| <Reihe>` aus title entfernen (WDR 41%, Funk.net 35%, BR 18%, NDR 15%
  haengen den Reihennamen mit ` | ` an den title; identisch mit topic).
- AUSNAHME ARTE (alle Kanaele): topic ist KEINE Reihe, sondern Genre-Taxonomie
  -> serie_name aus topic NICHT ableiten (siehe C / B5).

### B4. sprache-Default je Sender, dann von B1-Markern ueberschrieben
- ARTE.DE->de, ARTE.FR->fr, ARTE.EN->en, ARTE.ES->es, ARTE.IT->it, ARTE.PL->pl
  (Kanal-Suffix = Sprache der Fassung/Untertitel; title+desc sind in dieser
  Sprache verfasst).
- alle anderen Sender Default de; en/fr nur wenn B1-Marker es sagt.

### B5. kategorie-Quelle (Prioritaet)
1. ARTE: topic vor ` - ` (Oberkategorie). Mapping:
   `Kino`/`Fernsehfilme und Serien`->Film bzw. Serie (Unterkat `Serien`->Serie,
   `Fernsehfilme`->Fernsehfilm, `Filme`->Spielfilm, `Kurzfilme`->Kurzfilm,
   `Stummfilme`->Stummfilm), `ARTE Concert`->Konzert, `Geschichte`/`Wissenschaft`
   /`Entdeckung der Welt`->Doku, `Aktuelles und Gesellschaft`->Reportage/Aktuell.
2. Metazeilen-Leitwort (Spielfilm/Fernsehfilm/Dokumentarfilm/Kurzfilm/...).
3. Fallback duration-Prior: <2min Clip/Trailer, 2-30min Beitrag/Magazin/Episode,
   30-75min Doku/Reportage/Episode, 75-160min Spielfilm.

## C. Per-Sender-Spezifikation (alle Sender abgedeckt)

Spalten: Episodennotation | Metazeile-Ort (Kategorie+Land+Jahr) | Besonderheiten
| Rigorositaet. serie_name=topic gilt ueberall ausser ARTE; Marker B1 gelten
ueberall (mit den dort genannten Sender-Varianten).

### Gruppe ZDF-Spartenkanaele -- VERIFIZIERT identisch
ZDFinfo, ZDFneo, ZDF-tivi -- Episoden fast vollstaendig in Form A `(Sxx/Exx)`:
ZDFinfo 99,2% | ZDFneo 98,6% | ZDF-tivi 90,7%. Metazeile praktisch nie
(Kategorie aus topic/duration). Sehr strikt.
- ZDFneo zusaetzlich `(Englisch)` (engl. Originalfassung) + `(Audiodeskription)`.
- ZDF-tivi Kinder: viele `(Gebärdensprache)`/`(Audiodeskription)`.

### ZDF (Hauptkanal) -- einzeln
Episodennotation Form A, aber nur 30,7% (Halbe davon 4-stellig=Jahr!) -> bei
Tagesformaten (heute, Mittagsmagazin) S=Jahr verwerfen. Metazeile fuer echte
Spielfilme im **title** mit Komma: `... - Spielfilm, Deutschland/Frankreich 1964`
(mehrere Laender per `/`). Rigorositaet: hoch bei Serien-S/E, mittel bei Filmen.

### 3Sat -- einzeln (gleiche Metazeile-FORM wie ZDF, aber eigener Kanal)
Metazeile im **title** mit Komma `- Spielfilm, Deutschland 2017` (34% der Filme).
Episodennotation Form A selten (4,5%), davon 740/859 Jahr-Form. Viel Mehrteiler
`(1/2)`. Audiodeskription haeufig. Strikt bei Filmen.

### ARD (Das Erste) -- einzeln
Metazeile im **description-Prefix** ohne Komma: `Spielfilm Deutschland 2022 ...`
(39% der Filme). title-Metazeile 0%. Episodennotation Form A nur 1,8%. topic ist
ein Sammelbecken (1758 Werte) -> serie_name brauchbar, aber Reihe oft unscharf.
Marker `(Audiodeskription)`,`(Gebärdensprache)`,`(mit Gebärdensprache)`,`(stumm)`.

### ARD-Dritte mit description-Metazeile -- gleiche FORM, Abdeckung pro Sender
Alle nutzen, WENN vorhanden, dieselbe description-Prefix-Form wie ARD (kein
Komma, kein title-Meta). KEINE Familie bei der *Abdeckung* -- einzeln gemessen
(Anteil der Filme mit Metazeile):
- WDR 18,9% | NDR 9,6% | RBB ~26% | BR 6,8% | SWR 3,3% | HR 3,2% | MDR 0,6%.
=> Parser-Regel gemeinsam, aber Erwartungswert/Confidence pro Sender kalibrieren.
Sender-Eigenheiten:
- WDR: ` | <Reihe>`-Suffix sehr haeufig (41%); `(klare Sprache)`-Marker.
- NDR: viel `(stumm)`/`(tlw. stumm)`; ` | `-Suffix 15%.
- SWR: `(ohne Ton)` 420x; topic teils `SWR Retro - ...` (kein Genre, Teil des
  Reihennamens -> NICHT als Taxonomie splitten).
- MDR: Episodennotation Form A relativ haeufig (20,5%); Metazeile fast nie.
- BR: ` | `-Suffix 18%; topic teils nur `BR` (serie_name dann unbrauchbar).
- RBB: topic `rbb Retro - ...`/`sorbisch`-Marker; Metazeile wie ARD.
- HR: topic `hr Retro | hessenschau`-artig.

### SRF -- einzeln (voellig eigene Konventionen)
- Episodennotation **Form B** `(Staffel N, Folge M)` / `(Folge M)` -- NICHT
  `(Sxx/Exx)` (dort 0%). Auch ` ... Folge 3` ohne Klammern kommt vor.
- Sprachmarker `(engl.)`,`(frz.)`,`(franz.)` (statt Originalversion).
- Datumssendungen `<topic> vom TT.MM.JJJJ`.
- Kaum Metazeile/Kategorie explizit (cat ~0%) -> Kategorie nur duration/topic.
- Schweizer Begriffe/Guillemets im Freitext. serie_name=topic gut nutzbar.

### KiKA -- einzeln
- Episodennotation **Form C** fuehrendes `NN. ` (25% der Titel); `NN. Folge` =
  nur Nummer. Keine Staffel.
- Hoerfassung-Marker fast immer `(Hörfassung)` statt Audiodeskription.
- FSK-Hinweise `(FSK 12)`, Sprachmarker `(sorbisch)` vereinzelt.
- serie_name=topic sehr sauber (Reihen). Mittlere Rigorositaet.

### ARTE.DE / ARTE.FR -- gleiche Struktur, Sprache aus Suffix
- Kategorie/Genre ausschliesslich aus topic-Taxonomie `Ober - Unter` (97,7%
  konform; ~2% Legacy-Einwort-topics wie `Kino`,`Geschichte`).
- Marker `(Originalversion mit Untertitel)`,`(mit Untertitel)`,`(Originalversion)`
  sehr haeufig (33-36%); eincod_ut/sprache daraus.
- serie_name NICHT aus topic. Staffel/Folge nur als Mehrteiler `(n/m)`.
- Sprache: DE bzw. FR (Kanal). Sehr strikt.

### ARTE.EN / ARTE.ES / ARTE.IT / ARTE.PL -- VERIFIZIERT identisch
- topic-Taxonomie 95-100%, `(Originalversion mit Untertitel)` 93-100% (praktisch
  der ganze Kanal ist OmU) -> eincod_ut=true Default, sprache = en/es/it/pl.
- title/description in der Kanalsprache. Sehr strikt.

### PHOENIX -- einzeln
Politik/Doku. title-Marker oft Partei-Tags `(SPD)`,`(CDU)`,`(FDP)`,`(AfD)` (kein
Zielfeld; ggf. Themen-Tag). `(Gebärdensprache)` vorhanden. Kaum Metazeile.
Kategorie ueber duration/topic. topic `phoenix vor ort` etc.

### ORF -- einzeln
Gebaerdensprache-Marker **`(ÖGS)`** (nicht `Gebärdensprache`!). Mehrteiler `(n/m)`,
`(in voller Länge)`-Hinweis. Keine Metazeile, kein S/E. serie_name=topic.

### tagesschau24 -- einzeln
Reine Nachrichten, nur 8 topics, fast keine Marker/Metazeile. Kategorie=Nachricht
(duration/topic). Praktisch keine Filme/Serien.

### DW (Deutsche Welle) -- einzeln
Hier deutschsprachig, kaum Marker, keine Metazeile, kein S/E, topic=Einzeltitel
(reuse niedrig). Kategorie ueber duration/topic. sprache=de.

### Funk.net -- einzeln
Web-/Musik-Content. ` | `-Suffix 35%; Marker `(Official Video)`,`(LIVE IN
CONCERT)`,`(Teil 1/2)`. Kaum Metazeile. trailer/Clip via duration.

### rbtv (Radio Bremen TV) -- einzeln
Regional. `(Gebärdensprache)` vorhanden, sonst wenig. serie_name=topic
(`buten un binnen | regionalmagazin`). Kategorie duration/topic.

### ARD-alpha -- einzeln
Bildung. Mehrteiler `(n/m)` (Vorlesungsreihen). Kaum Metazeile/S-E.

### ONE -- einzeln
ARD-Serienkanal. Episodennotation Form A 16,5%; `(Originalversion)` 155x (engl.
OV Serien). serie_name=topic (`Sturm der Liebe` 59%). Mittel.

### Mini-Sender (n<50): "Radio Bremen TV" (20), "RBTV" (1)
Vernachlaessigbar; wie rbtv / Default behandeln.

## D. Gruppierungs-Begruendung (Pruefung der Familien)

- ZUSAMMENGEFASST nur: {ZDFinfo,ZDFneo,ZDF-tivi} (S/E 91-99%, identisch) und
  {ARTE.EN,ARTE.ES,ARTE.IT,ARTE.PL} (OmU 93-100%, identisch). ARTE.DE/FR teilen
  die Struktur, aber andere Sprache/Marker-Dichte -> getrennt notiert.
- NICHT zusammengefasst trotz scheinbarer "ARD-Familie": ARD/WDR/NDR/BR/SWR/MDR/
  RBB/HR teilen die Metazeilen-FORM (description, kein Komma), aber die Abdeckung
  reicht von 0,6% (MDR) bis 39% (ARD) -- Confidence/Erwartung pro Sender.
- SRF, KiKA, ORF haben EIGENE Episoden- bzw. Gebaerden-/Sprachmarker
  (Form B / Form C / ÖGS) und duerfen NICHT mit den anderen verschmolzen werden.

## E. Geklaerte Design-Entscheidungen

1. `(Originalversion)` ohne Sprachzusatz -> sprache = Sentinel **"ov"** (Audio
   != de, genaue Sprache offen) bis TMDB `original_language` greift. (umgesetzt)
2. 4-stellige S/E-Season = Sendejahr -> staffel/episode verwerfen, Jahr als jahr
   uebernehmen wenn keine Metazeile. (umgesetzt)
3. Mehrteiler `(n/m)` -> **n -> episode, m -> episode_count** (neues Feld);
   episode nur, wenn keine echte Sxx/Exx-Notation vorlag. (umgesetzt)
4. Produktionsland -> **Roh-String jetzt** (z.B. `Deutschland/Frankreich`),
   ISO-3166-Normalisierung inkl. historischer Staaten erst bei Matching/
   Enrichment. (umgesetzt: Roh-String)
5. Kategorie ohne verlaessliches Signal (langes Non-Fiction ohne Metazeile/
   topic-Kategorie) -> Label **"unklar"** + niedrige Confidence, NICHT raten;
   der Review-Gate (Phase 5) entscheidet. (umgesetzt: duration-Prior liefert nur
   noch Clip / Beitrag-Episode / unklar)

## F. Validierung an Stichprobe -- Befunde & Schema-Korrekturen

Prototyp analysis/extract.py gegen Zufalls- und film-lange Stichproben geprueft
(analysis/validate.py). BESTAETIGT korrekt: ARD desc-Metazeile inkl. Slash-Laender
(`Luther` -> Spielfilm/2003/`Deutschland/USA`), S/E + Audiodeskription zusammen
(`Reiterhof Wildenstein` -> S1/E6, Spielfilm 2021, Hoerf), 3Sat/ZDF title-Metazeile
mit Komma (`Rivale - Spielfilm, Deutschland 2020`), KiKA Leitnummer, ARTE.DE
Taxonomie (98%), ZDFinfo 4-stellige Season -> Jahr, SRF Form B, ARTE.EN
Sprache+OmU. Daraus abgeleitete KORREKTUREN am Schema:

1. **Kategorie steht oft im topic selbst** (ARD/3Sat topic=`Dokumentarfilm`/
   `Spielfilm`). Neue Quelle "topic-als-Kategorie" mit Prioritaet zwischen
   Metazeile und duration-Prior. (umgesetzt)
2. **3Sat/ZDF-Doku-Titelform** kann `Dokumentarfilm von <Regisseur>, <Land> <Jahr>`
   lauten -> beim Land-Parse ein `von <Name>,`-Praefix vor dem Land verwerfen.
   (umgesetzt: bei Komma im Land-Match nur den Teil nach dem letzten Komma nehmen)
3. **ARTE-Fremdsprachenkanaele brauchen lokalisierte Taxonomie-Maps**: die
   topic-Position (vor ` - `) ist stabil, aber die Begriffe sind in der
   Kanalsprache -- FR `Cinéma`/`Séries et fictions`/`Info et société`, EN
   `Politics and society`/`Culture`. Die deutsche Map greift nur fuer ARTE.DE.
   Zusatz: ARTE.FR/EN nutzen `Saison N` bzw. `Enquête N` im Titel fuer
   Staffel/Episode (statt Sxx/Exx). -> pro Kanal eigene Begriffstabelle.
4. **ORF fuehrt die Staffel im topic** (`Theodosia Staffel 1`), nicht im Titel
   -> fuer ORF `Staffel N` aus dem topic ziehen und vom serie_name abschneiden.
5. **duration-Prior ist schwach**: lange Talk-/News-/Magazin-/Konzert-Formate
   (Rockpalast, Hart aber fair, ZDF-Mittagsmagazin) werden faelschlich
   `Spielfilm?`. Mitigation: nur dann Film, wenn ein Film-Signal existiert
   (Metazeile ODER topic-Kategorie); sonst Label `Langformat (unklar)` statt
   `Spielfilm?`.
6. **Trailer nur per Schluesselwort** (Trailer/Teaser/Vorschau/Preview); die
   Dauer-Heuristik (<90 s) erzeugte zu viele Falsch-Positive (kurze News-Clips)
   und wurde entfernt. Trailer-Anteil real <1%.
7. Trailing `(JJJJ)` im Titel (WDR `Minenspiel (2005)`) als Jahr nutzen und vom
   sauberen Titel abschneiden. (umgesetzt)
