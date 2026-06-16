# Temporary analysis helper (phase 3 heuristic review, 3Sat only).
# Buckets every 3Sat topic into format / genre / strand / sender / series and
# measures how many rows each bucket touches. Not shipped; lives under analysis/.
import sqlite3

DB = 'build/theke.db'

# -- curated 3Sat topic buckets (judgment-based, see _topics_dump.txt) -------
FORMAT = {                       # format/type word -> belongs in `category`
    'Film', 'Spielfilm', 'Fernsehfilm', 'Dokumentarfilm', 'Dokumentation',
}
GENRE = {                        # editorial theme rubric -> belongs in `genre`
    'Reise', 'Natur', 'Musik', 'Tiere', 'Geschichte', 'Politik und Gesellschaft',
    'Esskulturen', 'Kulturdoku', 'Kultur', 'Gesellschaft', 'Wissen', 'Buch',
    'Wissenschaftsdoku', 'Theater', 'Maerchen', 'Kabarett', 'Kabarett & Comedy',
    'Kabarett / Comedy', 'mehr Kabarett', 'Kulturdoku in 3sat', '3sat-Kulturdoku',
}
SENDER = {'3sat', '3Sat'}        # sender name as topic -> junk, NULL

# Programming strands / event brands: a slot, not a series. The real series or
# film title sits in the clean_title. Gray zone -> flagged for the user.
STRAND = {
    'Der Fernsehfilm der Woche', 'ZDF-Fernsehfilm', 'Das kleine Fernsehspiel',
    'Dokumentarfilmzeit', 'Herzkino', 'Krimisommer', 'Retro-Serie: Lederstrumpf',
    '3satPublikumspreis', '3satZuschauerpreis', 'Festspielsommer',
}

c = sqlite3.connect(DB)
rows = c.execute("SELECT topic, count(*) FROM mediathek WHERE sender='3Sat' "
                 "GROUP BY topic").fetchall()
total = sum(n for _, n in rows)

def bucket(t):
    if t in FORMAT: return 'format'
    if t in GENRE:  return 'genre'
    if t in SENDER: return 'sender'
    if t in STRAND: return 'strand'
    return 'series'

agg = {}
for t, n in rows:
    b = bucket(t)
    agg.setdefault(b, [0, 0])
    agg[b][0] += n          # rows
    agg[b][1] += 1          # distinct topics

print(f'total rows={total}  distinct topics={len(rows)}\n')
for b in ('format', 'genre', 'strand', 'sender', 'series'):
    r, d = agg.get(b, [0, 0])
    print(f'{b:8s}  rows={r:6d} ({100*r/total:4.1f}%)  topics={d}')

# How much of the format/genre/strand material has a colon-embedded series the
# planned 2nd pass could lift into series_name?
cur = c.cursor()
nonseries = FORMAT | GENRE | STRAND | SENDER
qmarks = ','.join('?' * len(nonseries))
withcolon = cur.execute(
    f"SELECT count(*) FROM mediathek WHERE sender='3Sat' AND topic IN ({qmarks}) "
    f"AND clean_title LIKE '%: %'", tuple(nonseries)).fetchone()[0]
affected = cur.execute(
    f"SELECT count(*) FROM mediathek WHERE sender='3Sat' AND topic IN ({qmarks})",
    tuple(nonseries)).fetchone()[0]
print(f'\nseries_name corrected to NULL: {affected} rows '
      f'({100*affected/total:.1f}%)')
print(f'  of those, colon-pattern (2nd pass can recover series): {withcolon}')
