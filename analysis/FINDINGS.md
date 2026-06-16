# Phase 3 -- Reverse engineering der Sender-Konventionen

Datenbasis: build/theke.db, mediathek-Tabelle, 698.554 Zeilen, 30 Sender mit
>=1000 Eintraegen (plus 3 Mini-Sender). Reine Bestandsanalyse -- kein Produktiv-
Code angefasst. Skripte: analysis/*.py (re-runnbar). Prozentwerte unten sind
gerundet.

## 1. Feld-Semantik (was steckt wo)

- **topic** = Sendungs-/Reihenname, NICHT der Episodentitel. Stark wieder-
  verwendet (Reuse-Faktor 35x..270x je Sender). Beispiel ARD: 1758 distinct
  topics auf 153k Zeilen. Nutzbar als Reihen-Schluessel.
  - **Ausnahme ARTE**: topic ist eine zweistufige **Genre-Taxonomie**
    "Oberkategorie - Unterkategorie" (nur 30..57 distinct Werte je Kanal),
    z.B. "Kino - Filme", "Kino - Kurzfilme", "Kino - Stummfilme",
    "Fernsehfilme und Serien - Serien/Fernsehfilme", "ARTE Concert - Klassik".
- **title** = Episoden-/Filmtitel + angehaengte Marker (Klammer-Suffixe,
  S/E-Notation, Versionskennzeichen). Bei Tages-/Nachrichtensendungen oft mit
  Datum/Uhrzeit im Titel.
- **description** = Freitext-Inhaltsangabe. Bei der ARD-Familie haeufig mit einer
  vorangestellten **Metazeile** "Spielfilm <Land> <Jahr>". Cast steht in
  Spielfilm-Beschreibungen inline in Klammern, z.B. "Nora Kaminski
  (Tanja Wedhorn)" -- aber als Prosa, ohne feste Struktur.
- **duration** = starker Kategorie-Prior. Verteilung gesamt: 80% < 30 min
  (News/Clips/Magazine), echte Spielfilm-Laenge (75..140 min) nur ~24k Zeilen.
- **geo** = Geoblocking-Region (DE, DE-AT-CH, DE-AT-CH-EU, AT ...), zu 89% leer.
  Kein Kategorie-Signal.
- **url_subtitle** (36,7% gefuellt) = separate Untertitel-Sidecar-Datei. Klar zu
  trennen von *eincodierten* Untertiteln (siehe Marker "(mit Untertitel)").

## 2. Welche Metadaten existieren -- und in welchem Feld

| Metadatum            | Wo                                  | Bemerkung |
|----------------------|-------------------------------------|-----------|
| Reihen-/Sendungsname | topic                               | ausser ARTE (dort Genre) |
| Kategorie/Genre      | ARTE: topic-Taxonomie; ARD-Fam: Leitwort in description; ZDF/3Sat: im title nach " - " | siehe 3. |
| Erscheinungsjahr     | Metazeile "<Kat> <Land> <Jahr>"     | je nach Sender title oder description |
| Produktionsland      | dieselbe Metazeile                  | "Deutschland", "Spanien/Frankreich/Italien" |
| Sprache/Version      | title-Klammer-Marker                | Katalog siehe 4. |
| eincodierte UT       | title "(mit Untertitel)"/"(OmU)"    | vs. url_subtitle (sidecar) |
| Staffel/Folge        | title "(Sxx/Exx)"; KiKA Leit-"NN."; "(1/2)" Mehrteiler | siehe 5. |
| Hauptdarsteller      | nur description-Prosa, Cast in "( )" | keine feste Konvention -> NER noetig |

## 3. Wo lebt die Kategorie+Land+Jahr-Zeile (Spielfilme, >=60 min)

Gemessen nur an Eintraegen >=60 min (n je Sender), "cat" = Kategorie-Leitwort,
"c+y" = Kategorie+Land+Jahr-Muster, "yr" = irgendein 4-stelliges Jahr:

| Sender   | n    | cat in TITLE | cat in DESC | c+y | yr  | Audiodeskr. | S/E |
|----------|------|--------------|-------------|-----|-----|-------------|-----|
| ARD      | 7631 | 5%           | **39%**     | 37% | 59% | 22%         | 7%  |
| RBB      | 527  | 0%           | **26%**     | 23% | 66% | 6%          | 3%  |
| WDR      | 1505 | 1%           | **17%**     | 15% | 71% | 6%          | 0%  |
| 3Sat     | 2886 | **34%**      | 4%          | 26% | 47% | 23%         | 3%  |
| ARTE.DE  | 1490 | 0%           | 12%         | 0%  | 51% | 7%          | 0%  |
| ZDF      | 5230 | 2%           | 1%          | 1%  | 45% | 18%         | **72%** |
| ZDFinfo  | 51   | 2%           | 0%          | 0%  | 31% | 0%          | **98%** |
| ZDFneo   | 50   | 0%           | 0%          | 0%  | 0%  | 10%         | **64%** |
| ONE      | 51   | 0%           | 2%          | 0%  | 8%  | 0%          | **47%** |

Drei klar getrennte Schulen:

- **ARD-Familie** (ARD, RBB, WDR, NDR, BR, SWR, MDR): Metazeile am **Anfang der
  description**: "Spielfilm Deutschland 2015", "Fernsehfilm ...". Land ohne
  Komma, dann Jahr.
- **ZDF / 3Sat**: Metazeile im **title** nach " - ", mit Komma:
  "Offenes Geheimnis - Spielfilm, Spanien/Frankreich/Italien 2018".
- **ARTE**: keine Metazeile -- Genre kommt aus der **topic-Taxonomie**, Jahr nur
  manchmal im description-Fliesstext.

## 4. Versions-/Sprach-Marker im title (gesamter Bestand, Top-Klammern)

Diese Klammer-Suffixe sind hochgradig standardisiert (sender-uebergreifend
gleiche Schreibweise) und direkt als Enum extrahierbar:

```
14835  (Audiodeskription)         -> Hoerfassung fuer Sehbehinderte
 9176  (Gebaerdensprache)  +1166  (mit Gebaerdensprache)
 9063  (Originalversion mit Untertitel)
 6025  (mit Untertitel)           -> EINCODIERTE UT (nicht sidecar)
 1524  (Originalversion)          1266 (engl.)   340 (Englisch)
 1240  (Hoerfassung)
  561  (stumm)                     450 (ohne Ton)
```

ARTE-Fremdsprachenkanaele (ARTE.ES/EN/PL/IT) tragen zu 95..100% einen
UT-/Originalversion-Marker -- praktisch der gesamte Kanal ist OmU. ARTE.DE/FR
~26..31%. Politik-Tags wie "(SPD)"/"(CDU)" tauchen bei Wahl-Content auf.

## 5. Staffel/Folge-Notation

- Primaerform "(Sxx/Exx)": 55k Treffer. **Achtung Variante**: ~18k davon haben
  eine **4-stellige "Season" = Jahr** ("(S2025/E221)") fuer taegliche Sendungen
  (heute, Mittagsmagazin). Dann ist E die laufende Nummer im Jahr, NICHT eine
  echte Episode. Parser muss 4-stellige Season als Jahr behandeln.
- **KiKA**: fuehrende "NN. " Episodennummer im title (25% der KiKA-Titel),
  zusaetzlich " | Reihenname"-Suffix.
- **Mehrteiler** unabhaengig von Serien: "(1/2)", "(2/3)", "(5/6)" -- Teil x von n.

## 6. Rigorositaet je Sender (Konventionstreue)

- **Sehr strikt**: ARTE (topic-Taxonomie ~98% konform, kleiner Legacy-Schwanz
  mit einwortigen topics wie "Kino", "Geschichte"); ZDF-Spartensender
  (ZDFinfo/ZDFneo/ONE: S/E ~50..98%).
- **Strikt in der Nische**: ARD-Familie -- die Spielfilm-Metazeile in der
  description ist sauber, gilt aber nur fuer den Spielfilm-Anteil; der Rest
  (News, Regionalmagazine) folgt eigenen Datum/Uhrzeit-Mustern im title.
- **Mittel**: KiKA (Episodennummern konsistent, aber Reihen heterogen).
- **Wildwuchs**: ARD-topic ist ein Sammelbecken (1758 Werte, viele Eintags-
  fliegen); BR nutzt oft nur "BR" als topic; SRF kategorisiert fast nichts
  explizit (cat ~0%), alles steckt im Freitext.

## 7. Empfehlung fuer die Phase-3-Heuristik

1. **Sender-spezifische Profile** statt einer globalen Regel: pro Sender(-Familie)
   ein Extraktor, der weiss, wo Kategorie/Land/Jahr liegen (ARD->description,
   ZDF/3Sat->title, ARTE->topic-Taxonomie).
2. **Marker-Extraktion zuerst** (Pass 1, sender-unabhaengig): Klammer-Marker aus
   Abschnitt 4 + S/E aus Abschnitt 5 sind die zuverlaessigsten Signale -> Sprache,
   Version, eincodierte UT, Staffel/Folge. Diese vor der Titel-Bereinigung ziehen
   und vom title abschneiden, damit der "saubere" Titel fuer das TMDB-Matching
   uebrig bleibt.
3. **Kategorie** aus drei Quellen mit Prioritaet: ARTE-Taxonomie > Metazeilen-
   Leitwort (Spielfilm/Fernsehfilm/Dokumentarfilm/Kurzfilm/...) > duration-Prior.
4. **Jahr/Land** aus der Metazeile; Jahr ist in 25..71% der Filme vorhanden.
5. **Cast** vorerst zuruueckstellen (nur Prosa, NER noetig) -- niedrige
   Prioritaet, fuer das ID-Matching liefert TMDB ohnehin die Besetzung.
6. Konfidenz aus Anzahl uebereinstimmender Signale (Kategorie + Jahr + Land +
   bereinigter Titel) -- Schwelle pro Sender kalibrieren, weil die Treffer-
   Dichte stark variiert.
