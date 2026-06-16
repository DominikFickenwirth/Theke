# Temporary audit for sender ARD (phase 3). Checks whether 3Sat findings 1-6
# recur and probes ARD-specific patterns. Not shipped.
import sqlite3, re

c = sqlite3.connect('build/theke.db'); cur = c.cursor(); W = "sender='ARD'"
def n(s, *a): return cur.execute(f"SELECT count(*) FROM mediathek WHERE {W} AND {s}", a).fetchone()[0]
def show(s, lim=8):
    for r in cur.execute(f"SELECT clean_title, series_name, category, country, year, season, episode FROM mediathek WHERE {W} AND {s} LIMIT {lim}"): print('   ', r)
TOT = n('1=1')
print('total ARD:', TOT)

print('\n== B1. format/genre-like topics used as series_name ==')
fmt = ['Film','Spielfilm','Fernsehfilm','Dokumentarfilm','Dokumentation','Filme in der ARD','Reportage','Doku','Kurzfilm','Drama','Krimi']
for t in fmt:
    cn = n('series_name=?', t)
    if cn: print(f'   {cn:6d}  series_name={t!r}')
# any topic that is exactly a CATWORD
print('   -- topics that ARE a bare category word --')
for r in cur.execute(f"SELECT topic,count(*) FROM mediathek WHERE {W} AND topic IN ('Spielfilm','Fernsehfilm','Dokumentarfilm','Dokumentation','Reportage','Magazin','Krimi','Drama','Doku','Film') GROUP BY topic ORDER BY 2 DESC"): print('     ', r)

print('\n== B3. case/spelling variants of same series (Aktuell vs aktuell etc.) ==')
for grp in [['aktuell (18 Uhr)','Aktuell (18 Uhr)'],['aktuell (21:45 Uhr)','Aktuell (21:45 Uhr)'],['Panorama','Panorama 3'],['tagesschau','tagesschau (mit Gebärdensprache)','tagesschau (mit Gebaerdensprache)']]:
    qs=','.join('?'*len(grp)); got=cur.execute(f"SELECT topic,count(*) FROM mediathek WHERE {W} AND topic IN ({qs}) GROUP BY topic",grp).fetchall()
    if got: print('   ',got)

print('\n== NEW: parenthetical markers left inside series_name (from topic) ==')
mark = r"series_name LIKE '%(mit Gebärdensprache)%' OR series_name LIKE '%(Audiodeskription)%' OR series_name LIKE '%(Hörfassung)%' OR series_name LIKE '%(Originalversion%' OR series_name LIKE '%(OmU)%' OR series_name LIKE '%(klare sprache)%' OR series_name LIKE '%(mit Untertitel%'"
print('   rows:', n(mark))
for r in cur.execute(f"SELECT series_name,count(*) FROM mediathek WHERE {W} AND ({mark}) GROUP BY series_name ORDER BY 2 DESC LIMIT 12"): print('     ', r)

print('\n== B4. "Film von" residue in clean_title ==')
print('   clean_title has "Film von":', n("clean_title LIKE '%Film von %'"))

print('\n== B6. metazeile date false positive (country LIKE vom) ==')
print('   country LIKE vom%:', n("country LIKE 'vom %'"))
for r in cur.execute(f"SELECT country,count(*) FROM mediathek WHERE {W} AND country LIKE 'vom %' GROUP BY country LIMIT 6"): print('     ',r)

print('\n== category distribution ==')
for r in cur.execute(f"SELECT category,count(*) FROM mediathek WHERE {W} GROUP BY category ORDER BY 2 DESC LIMIT 20"): print(f'   {r[1]:6d}  {r[0]}')

print('\n== country sanity: non-country-looking values ==')
for r in cur.execute(f"SELECT country,count(*) FROM mediathek WHERE {W} AND country IS NOT NULL GROUP BY country ORDER BY 2 DESC LIMIT 30"): print(f'   {r[1]:5d}  {r[0]!r}')

print('\n== language / flags ==')
for r in cur.execute(f"SELECT language,count(*) FROM mediathek WHERE {W} GROUP BY language ORDER BY 2 DESC"): print('   lang',r)
for r in cur.execute(f"SELECT flags,count(*) FROM mediathek WHERE {W} AND flags<>'' GROUP BY flags ORDER BY 2 DESC"): print('   flags',r)

print('\n== confidence ==')
for r in cur.execute(f"SELECT classify_confidence,count(*) FROM mediathek WHERE {W} GROUP BY classify_confidence ORDER BY 1"): print('   conf',r)
