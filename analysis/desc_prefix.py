# -- leading category token in description -----------------------------------
# ARD/ZDF often start the description with "Spielfilm Deutschland 2015" etc.
# Measure: how often does description start with a known category word, and
# how often is it followed by a country + 4-digit year.
import re, collections
from lib import conn, senders

CAT = re.compile(
    r'^(Spielfilm|Fernsehfilm|Dokumentarfilm|Dokumentation|Doku(?:-Reihe|mentarreihe|drama|fiktion)?'
    r'|Kurzfilm|Animationsfilm|Zeichentrick(?:film)?|Trickfilm|Kom[öo]die|Drama|Thriller|Krimi'
    r'|Serie|Reportage|Magazin|Reihe|Kinderfilm|Spielfilmreihe)\b', re.I)
# category word, optional comma, country (letters/slashes/spaces), 4-digit year
FULL = re.compile(
    r'^([A-Za-zÄÖÜäöü-]+),?\s+([A-Za-zÄÖÜäöü/ .]+?)\s+((?:19|20)\d\d)\b')

def run():
    c = conn().cursor()
    sl = senders(c, 1000)
    print('SENDER          desc_w/_catword%%  desc_w/_<word,country,year>%   top leading words')
    for s in sl:
        rows = c.execute('SELECT description FROM mediathek WHERE sender=? AND description IS NOT NULL', (s,)).fetchall()
        n = len(rows)
        if not n: continue
        cat = full = 0
        firstword = collections.Counter()
        catwords = collections.Counter()
        for (d,) in rows:
            d = d.strip()
            if CAT.match(d):
                cat += 1
                catwords[CAT.match(d).group(1).title()] += 1
            m = FULL.match(d)
            if m and len(m.group(2)) < 40:
                full += 1
            firstword[d.split()[0] if d.split() else ''] += 1
        topcat = ', '.join(f'{w}:{k}' for w,k in catwords.most_common(4))
        print(f'{s:14} {100*cat/n:6.1f}%          {100*full/n:6.1f}%        {topcat}')

if __name__ == '__main__':
    run()
