# -- per-sender profiler -----------------------------------------------------
# For EVERY sender: where do the agreed metadata live and how reliably?
# No family assumptions -- numbers per sender, grouping decided afterwards.
import re, collections
from lib import conn

# -- regexes ----------------------------------------------------------------
CATWORD = (r'(?:Spielfilm|Fernsehfilm|TV-Film|Dokumentarfilm|Dokumentation|Dokudrama'
           r'|Doku-Reihe|Doku|Kurzfilm|Animationsfilm|Zeichentrickfilm|Trickfilm'
           r'|Komödie|Drama|Thriller|Krimi|Spielfilmreihe|Kinderfilm|Serie|Reportage|Magazin)')
META_TITLE = re.compile(CATWORD + r',\s+[A-ZÄÖÜ][^,]{2,45}?\s+((?:19|20)\d\d)\b')
META_DESC  = re.compile(r'^' + CATWORD + r'\s+[A-ZÄÖÜ][\wÄÖÜäöü/. -]{2,45}?\s+((?:19|20)\d\d)\b')
SE         = re.compile(r'\(S(\d+)/E(\d+)\)')
LEADNUM    = re.compile(r'^\s*(\d{1,4})\.\s')
PIPE       = re.compile(r'\s\|\s')
TRAILER    = re.compile(r'\b(Trailer|Teaser|Vorschau|Vorab|Nächste Woche|Präview|Preview)\b', re.I)
PAREN      = re.compile(r'\(([^()]{1,40})\)')
# known version markers (normalize)
VMARK = re.compile(r'Audiodeskription|Hörfassung|Gebärden|Originalversion|mit Untertitel'
                   r'|\bOmU\b|\bOmdU\b|\(engl\.?\)|\(Englisch\)|\(stumm\)|\(ohne Ton\)', re.I)

def run():
    c = conn().cursor()
    sl = [r[0] for r in c.execute('SELECT sender,COUNT(*) n FROM mediathek GROUP BY sender ORDER BY n DESC')]
    for s in sl:
        rows = c.execute('SELECT topic,title,description,duration FROM mediathek WHERE sender=?', (s,)).fetchall()
        n=len(rows)
        if n<50:
            print(f'\n### {s}  (n={n})  -- too small, skipped'); continue
        mt=md=se=lead=pipe=trail=vm=0
        se4=0   # 4-digit season (year-as-season)
        topicdash=0  # topic looks like "X - Y" taxonomy
        parenc=collections.Counter()
        for topic,title,desc,dur in rows:
            t=title or ''; d=desc or ''; tp=topic or ''
            if META_TITLE.search(t): mt+=1
            if META_DESC.search(d.strip()): md+=1
            m=SE.search(t)
            if m:
                se+=1
                if len(m.group(1))==4: se4+=1
            if LEADNUM.search(t): lead+=1
            if PIPE.search(t): pipe+=1
            if TRAILER.search(t) or TRAILER.search(tp): trail+=1
            if VMARK.search(t): vm+=1
            if ' - ' in tp: topicdash+=1
            for pm in PAREN.findall(t):
                if not re.fullmatch(r'\d{1,4}', pm): parenc[pm]+=1
        p=lambda x:f'{100*x/n:4.1f}'
        print(f'\n### {s}  (n={n})')
        print(f'  metaTITLE={p(mt)}%  metaDESC={p(md)}%  S/E={p(se)}% (4-digit-season {se4}/{se if se else 1})  '
              f'leadNum={p(lead)}%  pipe={p(pipe)}%  trailer={p(trail)}%  versionMark={p(vm)}%  topic-with-dash={p(topicdash)}%')
        print('  top title-parentheticals: ' + ', '.join(f'{m}:{k}' for m,k in parenc.most_common(8)))

if __name__ == '__main__':
    run()
