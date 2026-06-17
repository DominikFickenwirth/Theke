# Temporary deep-audit of the remaining classify fields for 3Sat (phase 3).
# Looks past series_name (Befund 1-3) at clean_title residue, country, year,
# season/episode, language, flags, confidence. Not shipped.
import sqlite3, re

c = sqlite3.connect('build/theke.db')
cur = c.cursor()
W = "sender='3Sat'"
def n(sql, *a): return cur.execute(f"SELECT count(*) FROM mediathek WHERE {W} AND {sql}", a).fetchone()[0]
def show(sql, lim=8, *a):
    for r in cur.execute(f"SELECT clean_title, series_name, category, country, year, season, episode FROM mediathek WHERE {W} AND {sql} LIMIT {lim}", a):
        print('   ', r)

print('total 3Sat:', n('1=1'))

print('\n== A. metazeile residue left in clean_title ==')
# CATWORD or "Film von"/"Dokumentation"/"Reihe" survived into clean_title
resid = r"clean_title LIKE '%- Spielfilm%' OR clean_title LIKE '%- Fernsehfilm%' OR clean_title LIKE '%- Dokumentarfilm%' OR clean_title LIKE '%Film von %' OR clean_title LIKE '%- Dokumentation%' OR clean_title LIKE '%- Doku %'"
print('residue rows:', n(resid))
show(resid, 10)

print('\n== B. "Film von ..." metazeile (bare "Film" not in CATWORD) ==')
fv = r"(title LIKE '%Film von %' OR title LIKE '%- Film,%') "
print('title has "Film von"/"- Film,":', n(fv))
print('  of those year IS NULL:', n(fv + 'AND year IS NULL'))
show(fv, 8)

print('\n== C. country sanity (distinct values, suspicious ones) ==')
for r in cur.execute(f"SELECT country, count(*) FROM mediathek WHERE {W} AND country IS NOT NULL GROUP BY country ORDER BY 2 DESC"):
    print(f'   {r[1]:5d}  {r[0]!r}')

print('\n== D. year sanity ==')
print('year not NULL:', n('year IS NOT NULL'))
print('year < 1895 or > 2026:', n('year IS NOT NULL AND (year<1895 OR year>2026)'))
for r in cur.execute(f"SELECT year, count(*) FROM mediathek WHERE {W} AND year IS NOT NULL GROUP BY year ORDER BY year LIMIT 5"): print('   low ', r)
for r in cur.execute(f"SELECT year, count(*) FROM mediathek WHERE {W} AND year IS NOT NULL GROUP BY year ORDER BY year DESC LIMIT 5"): print('   high', r)

print('\n== E. season/episode extraction ==')
print('season not NULL:', n('season IS NOT NULL'), ' episode not NULL:', n('episode IS NOT NULL'))
print('episode_count not NULL:', n('episode_count IS NOT NULL'))
# unextracted explicit "Staffel N, Folge M" / "Folge N" / "Teil N" in title
miss = r"(title LIKE '%Staffel%Folge%' OR title LIKE '%, Folge %' OR title LIKE '% Teil %' OR title GLOB '* [0-9]/[0-9]*') AND season IS NULL AND episode IS NULL"
print('episodic title but nothing extracted:', n(miss))
show(miss, 10)

print('\n== F. language / flags distribution ==')
for r in cur.execute(f"SELECT language, count(*) FROM mediathek WHERE {W} GROUP BY language ORDER BY 2 DESC"): print('   lang', r)
for r in cur.execute(f"SELECT flags, count(*) FROM mediathek WHERE {W} AND flags<>'' GROUP BY flags ORDER BY 2 DESC"): print('   flags', r)

print('\n== G. clean_title degenerate (empty / very short / leftover punctuation) ==')
deg = r"clean_title IS NULL OR length(trim(clean_title))<=1 OR clean_title GLOB '*[-|:]'"
print('degenerate clean_title:', n(deg))
show(deg, 10)

print('\n== H. confidence distribution ==')
for r in cur.execute(f"SELECT classify_confidence, count(*) FROM mediathek WHERE {W} GROUP BY classify_confidence ORDER BY 1"): print('   conf', r)
