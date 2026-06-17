Habe mir `theke classify --analyze`/`--dry-run` und meine eigenen Audit-Skripte nochmal nebeneinander angeschaut. Ehrliche Antwort:

## Hat `--analyze` geholfen?

**Als Orientierung ja, für die eigentliche Befund-Jagd kaum.** `--analyze`/`--dry-run` liefern eine *Coverage*-Matrix (pro Sender: % Zeilen mit year/country/S-E/cat gefüllt, unklar-Quote, Flag-Quoten). Das war gut, um zu sehen *ob* eine Heuristik überhaupt feuert und ob ein Sender auffällig aus der Reihe tanzt. Aber praktisch jeder der 11 Befunde brauchte mein eigenes `_audit_sender.py`, weil `--analyze` drei Dinge prinzipiell nicht kann:

1. **Coverage ≠ Korrektheit.** Der Report zählt *gefüllt*, nicht *richtig*. `country="vom 3. Mai"` (B6), `series_name="Film"` (B1), Pipe-Reste (B7), Marker in series_name (B9) zählen alle als „gefüllt" und sehen im Report gesund aus. Die Befunde sind genau die Fälle, die der Report **wegmittelt**.
2. **Kein Drill-down.** Der Report zeigt nie einen echten String. Das mit Abstand häufigste, was ich manuell tat: „zeig mir 20 Beispielzeilen, wo Feld X komisch aussieht" + „top-30 topics dieses Senders" + „Werteverteilung von category/country".
3. **`REPORT_MIN_ROWS=1000` blendet die Mini-Sender aus** — genau die, die wir als Kurznotiz machen mussten. Für die war `--analyze` blind.

## Vorschläge (nach Nutzen sortiert)

Im Kern: die Logik aus `_audit_sender.py` ist eigentlich CLI-reif. Ich würde sie nicht als Skript belassen, sondern als read-only Sub-Optionen einziehen — alle hinter `--json`, alle ohne Schreibzugriff, passend zur „DB-only läuft automatisch"-Grenze.

1. **`--sender X[,Y]` + `--min-rows N`** auf den bestehenden Report. Billigste, größte Wirkung: endlich pro Sender filtern und den 1000er-Floor abschalten, um Mini-Sender überhaupt zu sehen. (Heute muss ich dafür ein Skript schreiben.)

2. **`classify --audit`** — read-only Befund-Scan statt Coverage. Läuft genau die Checks, die ich von Hand baute: bare-Format-Wort als topic (B1), Case-Varianten-Gruppen (B3), Pipe im topic (B7), Paren-Marker im topic (B9), „nicht-Land-aussehende" country-Werte (B6), „Film von"-Reste (B4), episodisch-aber-nichts-extrahiert (B5). Output: pro Sender Trefferzahl + ein paar Beispiele. Das hätte ~80 % von `_audit_sender.py` ersetzt.

3. **`classify show --sender X --where "<pred>" --limit N`** — ein simpler Sampler, der die Klassifikationsspalten roher Zeilen dumpt (`clean_title, series_name, category, country, year, season/episode, flags, conf`). Der Arbeitspferd-Befund-Workflow „filter auf Verdacht, schau Beispiele an" in einem Befehl.

4. **`classify dist --sender X --field category`** — Werteverteilung eines Felds (top-N topics, category-, country-, flags-Häufigkeiten). Habe ich ständig getippt.

5. **Confidence-Aufschlüsselung im Report.** Heute kollabiert `cat%` alles mit conf≥0.8 in eine Zahl. Eine Spalte je Stufe (0.9/0.8/0.5/0.2) würde **B8 überhaupt erst sichtbar machen** — eine description-Metazeile liefert 0.9, ist aber oft Müll; im jetzigen Report nicht von einer sauberen title-Metazeile zu unterscheiden.

6. **`--dry-run --diff` (Iterations-Killer).** Statt zwei Coverage-Tabellen per Auge zu vergleichen: pro Sender/Feld die *Änderungs-Churn* gegen die gespeicherten Spalten zeigen (wie viele Zeilen kippen, mit Vorher/Nachher-Sample). Das beantwortet direkt die Praxis, die ich in `classify-reviews-summary.md` notiert habe — „bricht meine Änderung einen nicht anvisierten Sender?" — heute der mühsamste manuelle Schritt.

Wenn du willst, kann ich (1)+(2) als kleinen, abgegrenzten Patch umsetzen, wenn's an die Heuristik-Umsetzung geht — die fügen sich sauber in das bestehende `classify_report`-Gerüst und brauchen kein neues Schema.