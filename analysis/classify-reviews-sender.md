# Phase 3 Heuristik-Review -- Befunde pro Sender

> Sichtung der `theke classify`-Ergebnisse je Sender in `build/theke.db`.
> **Befund-Definitionen, Loesungen und die Sender x Befund-Matrix:**
> siehe `classify-reviews-summary.md`. Hier nur die *zutreffenden* Befunde je
> Sender mit Zahlen; nicht genannte Befunde treffen nicht (nennenswert) zu.
> Audit-Skripte im Anhang.

## Befund-Legende (Kurzform)

- **B1** `series_name` ist Format/Genre/Container statt Serie (Topic woertlich uebernommen).
- **B2** `category` vermischt Format und Genre (ein Feld, zwei Konzepte).
- **B3** Schreibvarianten (Case/Diakritika/Abk./Tippfehler) zersplittern denselben `series_name`.
- **B4** `- Film von <Regie>`-Credit bleibt im `clean_title` (bare "Film" nicht in `CATWORD`).
- **B5** Episoden-Notation ohne Klammern (`n/m`, `Teil n`, `Staffel n, Folge m`) nicht erkannt.
- **B6** Metazeile-Falschtreffer auf Sendedatum (`... CATWORD vom DD. Monat YYYY`).
- **B7** Pipe-Suffix im Topic bleibt im `series_name` (`PIPESUF` laeuft nur auf dem Titel).
- **B8** Beschreibungs-Metazeile zieht Satzfragmente -> falsche category/country/year @ conf 0.9 (Sender ausserhalb `TITLE_META_SENDERS`).
- **B9** Klammer-Marker im Topic (`(Gebärdensprache)`/`(ÖGS)`) nicht erkannt -> `series_name` verschmutzt, Flag fehlt.
- **B10** Barrierefreiheits-Marker als **Suffix** ohne Klammern (` in Gebärdensprache`, ` in Einfacher Sprache`); teils Vokabel-Luecke in `MARKERS`.
- **B11** ARTE-Taxonomie (`ARTE_CAT`/`ARTE_SUB`) nur deutsch -> fremdsprachige ARTE-Kanaele fallen durch.

`TITLE_META_SENDERS` = {ZDF, 3Sat} (Metazeile aus dem Titel); alle anderen aus
der Beschreibung. ARTE-Sender: `series_name` per Design NULL, `category` aus der
Taxonomie.

---

## 3sat -- 19.277 Z. / 377 Topics

`series_name` nur zu **63 %** eine echte Serie; Rest sind Rubriken. Routing
noetig (Format->category, Genre->genre, Serie->series_name).

- **B1** 7.125 (37 %): format 1.682 / genre 4.872 / strand 266 / sender 305 (`3sat`); 1.076 davon per Doppelpunkt-Muster (`Der andere Blick: ...`) im 2. Pass rueckgewinnbar. Wortlisten der Buckets -> summary (Loesung B1). `series_name == category` bei format-Topics.
- **B2** Kronzeuge: Genre-Signal (`Natur`, `Reise`, ...) geht heute verloren.
- **B3** `nano`/`NANO`, `makro`/`MAKRO`, `Grimme Preis`/`Grimme-Preis`, Tippfehler `Insepktor Jury`, Apostroph-Varianten.
- **B4** 174 (`Film von`; 0 davon mit `, Land Jahr` -> reiner Credit).
- **B5** 176 (+42 mit `n/m` im `clean_title`). Vorsicht: `3 1/2 Stunden` ist ein Titel.
- **B6** 18 (alle `Slowenien Magazin vom <Datum>`).
- Klein: country-Tippfehler/Spacing 11 (`Grossbritanien`, `.../ Deutschland`).

## ARD -- 154.478 Z. / 1.758 Topics

Topics fast immer echte Programmnamen; Hauptlast bei Pipe-Suffix und Case.

- **B7** 12.176: `buten un binnen | regionalmagazin`, `hr Retro | hessenschau`, `... | ARD Wissen`. (Serie mal vor, mal hinter dem Pipe.)
- **B3** ~9.074 (reine Case): `aktuell`/`Aktuell (18 Uhr)`, `tagesschau`/`Tagesschau`, `ZAPP`/`Zapp`, `NACHTCAFÉ`/`NACHTCAFé`.
- **B5** 911 (inkl. roem. `Teil III`).
- **B9** 812 (`tagesschau (mit Gebärdensprache)`).
- **B8** 43 sichtbar (`von`, `· Deutschland`, `über den Klimawandel aus dem Jahr`); conf 0.9 trotz Muell.
- **B1** ~1.529: `Filme in der ARD` 1.069, `Film` 375, `Dokumentarfilm` 82. **B4** 10.

## SRF -- 102.286 Z. / 895 Topics

Von Clip-Sammeltopics dominiert.

- **B1 dominant** 48.996 (48 %): `Sport-Clip` 41.657, `Sportflash`, `SRF News Videos`, `*Clips`.
- **B10** 6.239: Suffix ` in Gebärdensprache` (`Tagesschau in Gebärdensprache` 1.689, ...); Flag `S` nur **1x** gesetzt.
- **B5** 439. **B8** winzig, aber hohe Fehlerquote (Metazeile feuert 65x, ~56 Muell-country).

## ZDF -- 94.875 Z. / 1.448 Topics

Schwester von 3Sat (in `TITLE_META_SENDERS`, gleiche Genre-Rubriken) -- B1-B6 wie dort.

- **B1** 5.222 Genre (`Politik` 2.392, `Sport` 1.261, `Nachrichten` 980, + 3Sat-Garnitur) + Strand `ZDFinfo Doku` 3.480.
- **B3** 6.192: `ZDFinfo Doku`/`doku`, `NANO`/`nano` (1206/246), `Scobel`/`scobel`.
- **B4** 68. **B5** 170. **B6** 12.

## NDR -- 49.318 Z. / 432 Topics

Gut klassifiziert: Topics durchweg echte Programm-/Serien-Namen.

- **B8** winzig (11; `über den Klimawandel aus dem Jahr`). **B5** 36. **B3** 19.
- Beobachtung: Editions-Suffixe (`Hamburg Journal` vs `Hamburg Journal 18:00 Uhr`) -- evtl. echte Ausgaben, kein klarer Fehler.

## SWR -- 37.077 Z. / 477 Topics

- **B3** 1.032: `planet schule`/`Planet Schule` (636/61), Akzent-Case `NACHTCAFÉ`/`é`, `SWR Extra`/`extra`.
- **B1** ~520: `Film` 219, `Dokumentarfilm` 80, Rubrik `Doku & Reportage` 221.
- **B5** 148. **B7** 16 (`... | ARD Wissen`). **B8** 4.

## SR -- 23.794 Z. / 681 Topics

- **B1** Container/Rubrik: `Beiträge` 3.514, `SR` 748, `aktuell` 700, `SR 3 Videos` 256, `Dokumentationen und Reportagen` 226.
- **B3 stark** 2.917: `das saarlandwetter`/`Das ...` (1796/1), `AUS CHRISTLICHER SICHT`/`aus ...` (179/89); + **Abkuerzung** `WimS` (543) == `Wir im Saarland`.
- **B5** 95.

## KiKA -- 22.827 Z. / 639 Topics

Sehr sauber (Kinderkanal); `LEADC`-Regel liefert 5.719 `episode`.

- **B3** 540: `KiKANiNCHEN`/`Kikaninchen` (52/463). **B5** 89.

## WDR -- 22.533 Z. / 284 Topics

Ueberwiegend sauber; regionale `Lokalzeit`-Ausgaben sind echt getrennt.

- **B3** 275 (`planet schule`). **B7** 6 (`... | ARD Wissen` + Untertitel-Pipe). **B1** ~27 (`Fernsehfilm` 24). **B5** 86. **B8** 2.
- Beobachtung: `WDR Retro`-Praefix mit Mittelpunkt-Trenner (955 Z.).

## BR -- 20.787 Z. / 429 Topics

- **B1** Sender-Container `BR` 2.191.
- **B7** 36 (`... | ARD Wissen`; `... | Bergfreundinnen` -- Serie *hinter* dem Pipe).
- **B3** 128 (`Auf bairisch g'lacht!`, `report München`). **B5** 56. **B8** 9.

## MDR -- 20.023 Z. / 359 Topics

Sehr sauber; `episode` 3.998 via `(S/E)`.

- **B7** 35 (`... | ARD Wissen`). **B3** 96 (`#hinREISEND`). **B5** 13. Strand `Kurzfilme im MDR` 78.

## PHOENIX -- 17.021 Z. / 122 Topics

- **B3 stark** 1.539: Versalien vs Kleinschreibung (`PHOENIX RUNDE`/`phoenix runde` 31/830, `UNTER DEN LINDEN`/`unter den linden`, `PRESSECLUB`/`Presseclub`).
- **B1** `Dokumentationen` 1.086 (+ `Beitrag` 44). **B5** 50.

## ORF -- 15.771 Z. / 1.383 Topics

- **B9** 277: `(ÖGS)` (oesterr. Gebaerdensprache); Flag `S` hier meist gesetzt (269/277), Schaden v. a. verschmutzter `series_name`.
- **B8** hohe Fehlerquote (Metazeile feuert 49x, ~43 Muell-country, `aus dem Jahr` 26).
- **B5** 136. Clip-Rubriken `Vintage Videos` 220, `ZIB Flash` 196.

## RBB -- 10.670 Z. / 275 Topics

Sauber.

- **B1** klein (`Dokumentation und Reportage` 100, Strand `Märchen in der ARD` 36). **B5** 68. **B8** klein (feuert 390x, 2 sichtbar Muell).

## HR -- 9.571 Z. / 192 Topics

- **B7 stark** 2.447 (~26 %): `hr Retro | hessenschau` 1.807, `| Abendschau` 449, `| Archivschätze` 95, ... (Basis `hr Retro` vorne).
- **B5 stark** 990: viele `- Teil n` / `- Folge n` (Podcasts, Ratgeber).
- **B1** klein (`Dokus & Reportagen` 170, `Reisen` 97).

## ARTE.DE / ARTE.FR -- 29.553 Z. / 105 Topics

Sonderfall: `series_name` per Design NULL, `category` aus Taxonomie. ARTE.DE
exzellent (97 % Taxonomie @ conf 0.9).

- **B11** ARTE.FR nur **3 %** Taxonomie-Treffer (457, nur Marke `ARTE Concert`); ~14.600 Z. fallen auf Dauer-Prior. Franzoesische Labels (`Histoire - XXe siècle`, ...) matchen die deutsche Map nicht.
- Flag `U` 8.420 (OmU, korrekt). **B8** 16.

## ARTE.EN / ARTE.ES / ARTE.IT / ARTE.PL -- 6.903 Z. / 114 Topics

- **B11 voll**: Taxonomie-Treffer EN 58/1.751, ES 49/1.867, IT 50/1.542, PL 124/1.743 (je ~3 %, nur `ARTE Concert`). `language` (en/es/it/pl) korrekt.

## tagesschau24 -- 7.601 Z. / 8 Topics

- **B1 dominant** Container `tagesschau24` 6.661 (88 %).
- **B10** 402: `in Einfacher Sprache` -- Marker fehlt zudem ganz im `MARKERS`-Vokabular.

## ZDF-tivi / ZDFinfo / ZDFneo -- 12.510 Z. / 581 Topics

Sauber (nicht in `TITLE_META_SENDERS`); `episode` 9.509 via `(S/E)`.

- **B1** klein (`ZDFinfo - die Einzeldokus` 379, `Filme` 147). **B3** 403 (`PUR+`/`pur+`).

## rbtv -- 7.109 Z. / 56 Topics

Radio Bremen. **Extremfall B7**.

- **B7 dominant** 6.100 (86 %): `buten un binnen | regionalmagazin` 4.932, `| sportblitz` 951, `| wetter` 211.
- **B10** 115 (`... in Gebärdensprache`). **B1** klein (`Radio Bremen Retro ...` 101).

## DW -- 6.766 Z. / 79 Topics

- **B1 mittel** Genre-Rubriken: `Wirtschaft` 290, `Europa` 133, `THEMEN` 85, `Wissenschaft` 30, `Deutschland` 28, `Nahost` 24, `Kultur` 14, `Sport` 8, `Reise` 2.
- **B3** 51 (`PopXport`). Keine Episodennotation (`episode` 0).

## Funk.net -- 6.068 Z. / 128 Topics

Saubererster mittelgrosser Sender (junges Online-Netzwerk). Nur **B5** 58.

## ARD-alpha -- 2.914 Z. / 117 Topics

- **B7** 297 (~10 %): `alpha Lernen | <Fach>` (Physik/Deutsch/Englisch/...) -- Reihe *hinter* dem Pipe. **B5** 64.

## ONE -- 2.376 Z. / 60 Topics

Seriensender; `series_name`/`episode` (401) sauber. `ov` 158 korrekt.

- **Beobachtung:** 96 % `category=unklar` -- Dauer-Prior hat fuer ~45-min-Serienfolgen kein Signal (generelle Schwaeche, nicht ONE-spezifisch). **B1** marginal (`ONE` 6).

## Radio Bremen TV -- 20 Z.

Kurznotiz: identisch mit `rbtv` (derselbe Sender, anderer Sender-String); B7 in Mini-Menge.

## RBTV -- 1 Z.

Kurznotiz: einziger Eintrag `topic="Livestream"`. Auch dies Radio Bremen.

> **Sender-String-Split (Daten-Befund):** `rbtv` (7.109), `Radio Bremen TV` (20)
> und `RBTV` (1) sind derselbe Sender unter drei Strings -- fuer Suche/Statistik
> zu vereinheitlichen.

---

## Anhang -- Reproduktion

Temporaere Hilfsdateien (Praefix `_`), kein CLI-Bestandteil. Ausfuehren mit
`PYTHONIOENCODING=utf-8 python analysis/<datei>`.

- `_audit_sender.py "SENDER[,SENDER2]"` -- generisches Sender-Audit (Battery fuer alle Befunde).
- `_topics_dump.txt`, `_bucket_3sat.py`, `_audit_3sat.py`, `_audit_ard.py` -- frueheres 3Sat-/ARD-Detailaudit.
