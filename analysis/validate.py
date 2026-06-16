# -- validation dump: raw row vs extracted metadata, per sender -------------
import sys, random
from lib import conn
from extract import extract

PER = int(sys.argv[1]) if len(sys.argv)>1 else 6
ONLY = sys.argv[2] if len(sys.argv)>2 else None
random.seed(42)

def fmt(r):
    bits=[]
    if r['serie_name'] and r['serie_name']!=r['titel']: bits.append(f"serie={r['serie_name']!r}")
    if r['staffel'] is not None: bits.append(f"S{r['staffel']}")
    if r['episode'] is not None: bits.append(f"E{r['episode']}")
    if r['episode_count']: bits.append(f"of{r['episode_count']}")
    bits.append(f"kat={r['kategorie']}({r['kat_src']})")
    if r['jahr']: bits.append(f"jahr={r['jahr']}")
    if r['land']: bits.append(f"land={r['land']!r}")
    bits.append(f"spr={r['sprache']}")
    for k in ('gebaerdensprache','hoerfassung','eincod_ut','trailer','stumm'):
        if r[k]: bits.append(k[:4]+'+')
    return '  '.join(bits)

def run():
    c = conn().cursor()
    sl = [r[0] for r in c.execute('SELECT sender,COUNT(*) n FROM mediathek GROUP BY sender HAVING n>=50 ORDER BY n DESC')]
    if ONLY: sl=[s for s in sl if s==ONLY]
    for s in sl:
        rows = c.execute('SELECT topic,title,description,duration FROM mediathek WHERE sender=?', (s,)).fetchall()
        # coverage stats over ALL rows
        n=len(rows); cov=dict(jahr=0,land=0,SE=0,meta=0,arte=0,ut=0,gs=0,hf=0)
        for tp,t,d,dur in rows:
            r=extract(s,tp,t,d,dur)
            if r['jahr']:cov['jahr']+=1
            if r['land']:cov['land']+=1
            if r['staffel'] is not None or r['episode'] is not None:cov['SE']+=1
            if r['kat_src']=='metazeile':cov['meta']+=1
            if r['kat_src']=='arte-topic':cov['arte']+=1
            if r['eincod_ut']:cov['ut']+=1
            if r['gebaerdensprache']:cov['gs']+=1
            if r['hoerfassung']:cov['hf']+=1
        p=lambda x:f'{100*x/n:.0f}'
        print('\n'+'='*100)
        print(f'### {s}  (n={n})  coverage: jahr={p(cov["jahr"])}% land={p(cov["land"])}% '
              f'S/E={p(cov["SE"])}% meta={p(cov["meta"])}% arteCat={p(cov["arte"])}% '
              f'UT={p(cov["ut"])}% Gebaerde={p(cov["gs"])}% Hoerf={p(cov["hf"])}%')
        print('='*100)
        for tp,t,d,dur in random.sample(rows, min(PER,n)):
            r=extract(s,tp,t,d,dur)
            print(f' RAW topic : {tp!r}')
            print(f' RAW title : {t!r}  [{(dur or 0)//60}min]')
            print(f' EXTRACT   : titel={r["titel"]!r}')
            print(f'             {fmt(r)}')
            print(' -'*40)

if __name__ == '__main__':
    run()
