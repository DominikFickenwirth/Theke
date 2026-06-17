# Temporary generic per-sender audit (phase 3). Reusable battery behind the
# classify-reviews-sender.md write-ups. Usage:
#   PYTHONIOENCODING=utf-8 python analysis/_audit_sender.py "SENDER[,SENDER2,...]"
# Not shipped.
import sqlite3, re, sys
from collections import defaultdict

senders = [s for s in sys.argv[1].split(',')] if len(sys.argv) > 1 else ['3Sat']
ph = ','.join('?' * len(senders))
c = sqlite3.connect('build/theke.db'); cur = c.cursor()
W = f"sender IN ({ph})"

def n(extra, *a):
    return cur.execute(f"SELECT count(*) FROM mediathek WHERE {W} AND {extra}", senders + list(a)).fetchone()[0]
def q(sql, *a):
    return cur.execute(sql, senders + list(a)).fetchall()

CATWORDS = ['Film','Spielfilm','Fernsehfilm','Dokumentarfilm','Dokumentation','Kurzfilm',
            'Reportage','Magazin','Doku','Drama','Krimi','Komödie','Thriller','Serie']
GENRES = ['Reise','Natur','Musik','Tiere','Geschichte','Politik und Gesellschaft','Esskulturen',
          'Kulturdoku','Kultur','Gesellschaft','Wissen','Buch','Wissenschaftsdoku','Theater','Sport']

TOT = n('1=1')
print(f"### senders={senders}  total={TOT}  distinct_topics="
      f"{cur.execute(f'SELECT count(DISTINCT topic) FROM mediathek WHERE {W}', senders).fetchone()[0]}")

print('\n-- top 30 topics (= series_name for non-ARTE) --')
for t, cnt in q(f"SELECT topic,count(*) FROM mediathek WHERE {W} GROUP BY topic ORDER BY 2 DESC LIMIT 30"):
    print(f'   {cnt:6d}  {t!r}')

print('\n-- B1: topics that are a bare format/genre word --')
for t in CATWORDS + GENRES:
    cn = n('topic=?', t)
    if cn: print(f'   {cn:6d}  topic={t!r}')

print('\n-- B3: case-only topic variant groups (rows in such groups) --')
groups = defaultdict(list)
for t, cnt in q(f"SELECT topic,count(*) FROM mediathek WHERE {W} GROUP BY topic"):
    groups[(t or '').lower()].append((t, cnt))
multi = [v for v in groups.values() if len(v) > 1]
print(f'   groups={len(multi)}  rows={sum(sum(x[1] for x in v) for v in multi)}')
for v in sorted(multi, key=lambda v: -sum(x[1] for x in v))[:8]:
    print('     ', v)

print('\n-- B7: pipe-suffix in topic/series_name --')
print('   rows:', n("topic LIKE '%|%'"))
for t, cnt in q(f"SELECT topic,count(*) FROM mediathek WHERE {W} AND topic LIKE '%|%' GROUP BY topic ORDER BY 2 DESC LIMIT 6"):
    print(f'     {cnt:6d}  {t!r}')

print('\n-- B9: parenthetical markers inside topic --')
mk = [t for (t,) in q(f"SELECT DISTINCT topic FROM mediathek WHERE {W} AND topic LIKE '%(%)%'")
      if re.search(r'\((mit )?(Gebärdensprache|Audiodeskription|Hörfassung|klare Sprache|Originalversion|OmU|mit Untertitel|ÖGS|OV)\)?', t or '', re.I)]
print('   distinct marker-topics:', len(mk))
for t in mk[:8]: print(f'     {n("topic=?", t):6d}  {t!r}')

print('\n-- B4: "Film von" residue in clean_title --', n("clean_title LIKE '%Film von %'"))
print('-- B6: metazeile date false-positive (country LIKE vom%) --', n("country LIKE 'vom %'"))

print('\n-- B5: episode notation --')
print('   season set:', n('season IS NOT NULL'), ' episode set:', n('episode IS NOT NULL'),
      ' ep_count:', n('episode_count IS NOT NULL'))
print('   unparen episodic, nothing extracted:',
      n("(title LIKE '%Staffel%Folge%' OR title LIKE '%, Folge %' OR title LIKE '% Teil %' OR title GLOB '* [0-9]/[0-9]*') AND season IS NULL AND episode IS NULL"))

print('\n-- category distribution --')
for cat, cnt in q(f"SELECT category,count(*) FROM mediathek WHERE {W} GROUP BY category ORDER BY 2 DESC LIMIT 18"):
    print(f'   {cnt:6d}  {cat}')

print('\n-- country sanity: suspicious (non-country) values --')
bad = [(t, cnt) for t, cnt in q(f"SELECT country,count(*) FROM mediathek WHERE {W} AND country IS NOT NULL GROUP BY country")
       if re.match(r'^[a-zäöü·"]', t) or re.search(r'\b(von|über|aus|im|mit|der|die|das|und|dem|den|eine?r?|Jahr|vom)\b', t)]
print('   suspicious values:', len(bad), ' rows:', sum(x[1] for x in bad))
for t, cnt in sorted(bad, key=lambda x: -x[1])[:12]: print(f'     {cnt:5d}  {t!r}')

print('\n-- language / flags / confidence --')
for r in q(f"SELECT language,count(*) FROM mediathek WHERE {W} GROUP BY language ORDER BY 2 DESC"): print('   lang', r)
for r in q(f"SELECT flags,count(*) FROM mediathek WHERE {W} AND flags<>'' GROUP BY flags ORDER BY 2 DESC LIMIT 8"): print('   flags', r)
for r in q(f"SELECT classify_confidence,count(*) FROM mediathek WHERE {W} GROUP BY classify_confidence ORDER BY 1"): print('   conf', r)
