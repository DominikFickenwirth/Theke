# Temporary verification of the proposed Q3 (genre set) and Q4 (pipe split)
# heuristics against the whole DB. Not shipped.
import sqlite3, re
from collections import defaultdict

c = sqlite3.connect('build/theke.db'); cur = c.cursor()

# ---------------------------------------------------------------- Q4: pipe split
# Hypothesis: the pipe side carrying a sender token or a generic section word is
# the Dachmarke (-> slot); the other side is the series. If neither side matches,
# it is probably title|subtitle -> do NOT split.
SENDER_TOKENS = {'ard', 'zdf', '3sat', 'hr', 'br', 'wdr', 'ndr', 'swr', 'sr',
                 'mdr', 'rbb', 'orf', 'srf', 'rbtv', 'alpha', 'arte', 'phoenix',
                 'dw', 'kika'}
BRANDS = ['ard wissen', 'radio bremen', 'alpha lernen']
SECTION_WORDS = {'regionalmagazin', 'sportblitz', 'wetter', 'doku', 'extra',
                 'retro', 'geschichten', 'spezial'}

def side_is_slot(s):
    low = s.lower()
    toks = set(re.findall(r"[a-zäöüß0-9]+", low))
    if toks & SENDER_TOKENS:            return True
    if any(b in low for b in BRANDS):   return True
    if toks & SECTION_WORDS:            return True
    return False

print("=== Q4: pipe topics, proposed split ===")
rows = cur.execute("SELECT sender, topic, count(*) FROM mediathek "
                   "WHERE topic LIKE '%|%' GROUP BY sender, topic ORDER BY 3 DESC").fetchall()
ok = noslot = bothslot = 0
total = 0
samples = []
for sender, topic, n in rows:
    total += n
    parts = [p.strip() for p in topic.split('|')]
    if len(parts) != 2:
        samples.append(('MULTI', sender, topic, n)); continue
    a, b = parts
    sa, sb = side_is_slot(a), side_is_slot(b)
    if sa and not sb:    ok += n; verdict = f'slot={a!r} series={b!r}'
    elif sb and not sa:  ok += n; verdict = f'slot={b!r} series={a!r}'
    elif sa and sb:      bothslot += n; verdict = f'BOTH-slot {a!r}|{b!r}'
    else:                noslot += n; verdict = f'NEITHER (subtitle?) {a!r}|{b!r}'
    if n >= 100 or sa == sb:
        samples.append((verdict, sender, topic, n))
print(f"pipe rows total={total}  resolved(one side slot)={ok}  "
      f"both-slot={bothslot}  neither/subtitle={noslot}")
print("-- notable cases --")
for v, s, t, n in samples[:40]:
    print(f"  {n:5d} [{s}] {v}")

# ---------------------------------------------------------------- Q3: genre set
GENRE_SET = {'Reise', 'Natur', 'Musik', 'Tiere', 'Geschichte', 'Politik',
             'Politik und Gesellschaft', 'Sport', 'Nachrichten', 'Esskulturen',
             'Kulturdoku', 'Kultur', 'Gesellschaft', 'Wissen', 'Buch',
             'Wissenschaftsdoku', 'Theater', 'Märchen', 'Wirtschaft', 'Europa',
             'Nahost', 'Deutschland', 'Reportage', 'Doku'}
print("\n=== Q3: genre-set words used as topic, per sender (is it ever a real series?) ===")
qs = ','.join('?' * len(GENRE_SET))
gw = list(GENRE_SET)
for word in sorted(GENRE_SET):
    rows = cur.execute("SELECT sender, count(*) FROM mediathek WHERE topic=? "
                       "GROUP BY sender ORDER BY 2 DESC", (word,)).fetchall()
    if rows:
        tot = sum(r[1] for r in rows)
        bys = ', '.join(f'{s}:{n}' for s, n in rows[:6])
        print(f"  {word!r:28} total={tot:5d}  [{bys}]")
