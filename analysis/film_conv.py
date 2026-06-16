# -- film convention conformance (film-length entries only) ------------------
# Among entries >= 60 min, where does the "<category> <country> <year>" line
# live -- title or description -- and how consistent is it per sender?
import re
from lib import conn, senders

CATWORD = (r'(?:Spielfilm|Fernsehfilm|Dokumentarfilm|Dokumentation|Dokudrama|Doku'
           r'|Kurzfilm|Animationsfilm|Zeichentrickfilm|Trickfilm|Komödie|Drama'
           r'|Thriller|Krimi|Spielfilmreihe|Kinderfilm)')
COUNTRYYEAR = re.compile(CATWORD + r',?\s+[A-ZÄÖÜ][\wÄÖÜäöü/. -]{2,40}?\s+((?:19|20)\d\d)\b')
CATANY      = re.compile(r'\b' + CATWORD + r'\b')
YEAR        = re.compile(r'\b((?:19|20)\d\d)\b')
AD          = re.compile(r'Audiodeskription|H[öo]rfassung', re.I)
SUB         = re.compile(r'mit Untertitel|OmU|OmdU', re.I)
SE          = re.compile(r'\(S\d+/E\d+\)|\(S\d{4}/E\d+\)')

def run():
    c = conn().cursor()
    sl = senders(c, 1000)
    print('Among >=60min entries:')
    print(f'{"SENDER":14}{"n":>6}{"catTITLE":>9}{"catDESC":>8}{"cntry+yr":>9}{"yrAny":>7}{"AD":>5}{"sub":>5}{"S/E":>5}')
    for s in sl:
        rows = c.execute('SELECT title,description FROM mediathek WHERE sender=? AND duration>=3600', (s,)).fetchall()
        n = len(rows)
        if n < 20: continue
        ct=cd=cy=ya=ad=sub=se=0
        for title, desc in rows:
            t=title or ''; d=desc or ''
            if CATANY.search(t): ct+=1
            if CATANY.search(d): cd+=1
            if COUNTRYYEAR.search(t) or COUNTRYYEAR.search(d): cy+=1
            if YEAR.search(t) or YEAR.search(d): ya+=1
            if AD.search(t): ad+=1
            if SUB.search(t): sub+=1
            if SE.search(t): se+=1
        pc=lambda x:f'{100*x/n:.0f}'
        print(f'{s:14}{n:>6}{pc(ct):>9}{pc(cd):>8}{pc(cy):>9}{pc(ya):>7}{pc(ad):>5}{pc(sub):>5}{pc(se):>5}')

if __name__ == '__main__':
    run()
