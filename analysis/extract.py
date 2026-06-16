# -- prototype metadata extractor (implements EXTRACTION_SCHEMA.md) ----------
# Scratch / validation only -- NOT production code. Pure function: a mediathek
# row -> extracted dict. Conservative: leaves a field None when unsure.
import re

CATWORD = (r'Spielfilm|Fernsehfilm|TV-Film|Dokumentarfilm|Dokumentation|Dokudrama'
           r'|Doku-Reihe|Kurzfilm|Animationsfilm|Zeichentrickfilm|Trickfilm'
           r'|Komödie|Drama|Thriller|Krimi|Spielfilmreihe|Kinderfilm|Serie|Reportage|Magazin')
META   = re.compile(r'\b(' + CATWORD + r'),?\s+(.{2,40}?)\s+((?:19|20)\d\d)\b')

SE_A   = re.compile(r'\(S(\d+)/E(\d+)\)')
SE_B   = re.compile(r'\((?:Staffel\s*(\d+),\s*)?Folge\s*(\d+)\)')
LEADC  = re.compile(r'^\s*(\d{1,4})\.\s+')
PART   = re.compile(r'\((\d{1,2})/(\d{1,2})\)')
PIPESUF= re.compile(r'\s*\|\|?\s*[^|]+(?:\|\|?\s*[^|]+)*$')   # trailing " | Reihe" run(s)
TRAILER= re.compile(r'\b(Trailer|Teaser|Vorschau|Vorab|Preview|Präview)\b', re.I)

# parenthetical marker vocabulary -> (field, value)
MARKERS = [
    (re.compile(r'^Audiodeskription$|^Hörfassung$', re.I),                ('hoerfassung', True)),
    (re.compile(r'Gebärdensprache$|^ÖGS$', re.I),                         ('gebaerdensprache', True)),
    (re.compile(r'^Originalversion mit Untertitel$|^mit Untertitel$|^OmU$|^OmdU$', re.I), ('eincod_ut', True)),
    (re.compile(r'^Originalversion$|^OV$', re.I),                         ('sprache', 'ov')),
    (re.compile(r'^engl\.?$|^Englisch$|^English$', re.I),                 ('sprache', 'en')),
    (re.compile(r'^frz\.?$|^franz\.?$|^französisch$', re.I),              ('sprache', 'fr')),
    (re.compile(r'^stumm$|^ohne Ton$|^tlw\. stumm$', re.I),               ('stumm', True)),
]
ARTE_LANG = {'ARTE.DE':'de','ARTE.FR':'fr','ARTE.EN':'en','ARTE.ES':'es','ARTE.IT':'it','ARTE.PL':'pl'}
TITLE_META_SENDERS = {'ZDF','3Sat'}
ARTE_CAT = {'Kino':'Film','Fernsehfilme und Serien':'Serie/Fernsehfilm','ARTE Concert':'Konzert',
            'Geschichte':'Doku','Wissenschaft':'Doku','Entdeckung der Welt':'Doku','Entdeckung':'Doku',
            'Aktuelles und Gesellschaft':'Reportage','Kultur und Pop':'Kultur'}
ARTE_SUB = {'Filme':'Spielfilm','Kurzfilme':'Kurzfilm','Stummfilme':'Stummfilm',
            'Serien':'Serie','Fernsehfilme':'Fernsehfilm'}

def extract(sender, topic, title, description, duration):
    t = title or ''; d = (description or '').strip(); tp = topic or ''
    r = dict(titel=None, serie_name=None, staffel=None, episode=None, episode_count=None,
             kategorie=None, kat_src=None, jahr=None, land=None,
             sprache=ARTE_LANG.get(sender,'de'), gebaerdensprache=False,
             trailer=False, hoerfassung=False, eincod_ut=False, stumm=False)

    # -- Pass 1: parenthetical markers (extract + strip) --------------------
    def take_parens(s):
        out=[]
        def repl(m):
            inner=m.group(1)
            for rx,(f,v) in MARKERS:
                if rx.match(inner):
                    r[f]=v; return ''      # strip recognized marker
            pm=PART.fullmatch('('+inner+')')
            return m.group(0)              # keep unrecognized
        return re.sub(r'\s*\(([^()]{1,40})\)', repl, s)
    t = take_parens(t)
    if TRAILER.search(title or '') or TRAILER.search(tp):
        r['trailer']=True

    # -- Pass 2: episode notation ------------------------------------------
    m = SE_A.search(t)
    if m:
        if len(m.group(1))==4:            # 4-digit season = broadcast year
            r['jahr']=r['jahr'] or int(m.group(1))
        else:
            r['staffel']=int(m.group(1)); r['episode']=int(m.group(2))
        t = SE_A.sub('', t)
    else:
        m = SE_B.search(t)                # SRF "(Staffel N, Folge M)"/"(Folge M)"
        if m:
            if m.group(1): r['staffel']=int(m.group(1))
            r['episode']=int(m.group(2)); t = SE_B.sub('', t)
        elif sender=='KiKA':
            m = LEADC.search(t)           # KiKA leading "NN."
            if m: r['episode']=int(m.group(1)); t = LEADC.sub('', t)
    pm = PART.search(t)                    # Mehrteiler "(n/m)": n->episode, m->episode_count
    if pm:
        r['episode_count']=int(pm.group(2))
        if r['episode'] is None: r['episode']=int(pm.group(1))
        t = PART.sub('', t)

    # -- Pass 3: metazeile (category + country + year) ---------------------
    src = t if sender in TITLE_META_SENDERS else d
    m = META.search(src) if src else None
    if m and len(m.group(2))<40:
        r['kategorie']=m.group(1); r['kat_src']='metazeile'
        land=m.group(2).strip(' ,-')
        if ',' in land: land=land.split(',')[-1].strip()   # drop "von <Regisseur>," prefix
        r['land']=land; r['jahr']=r['jahr'] or int(m.group(3))
        if sender in TITLE_META_SENDERS:  # strip "- Spielfilm, ... YEAR" from title
            t = t[:m.start()].rstrip(' -–')

    # -- Pass 4: serie_name ------------------------------------------------
    if sender not in ARTE_LANG:
        r['serie_name']=tp
        t = PIPESUF.sub('', t)            # drop " | Reihe" suffix from title

    # -- Pass 6: kategorie from ARTE taxonomy / duration prior -------------
    if sender in ARTE_LANG and ' - ' in tp:
        ober, _, unter = tp.partition(' - ')
        r['kategorie']=ARTE_SUB.get(unter.strip(), ARTE_CAT.get(ober.strip()))
        r['kat_src']='arte-topic'
    if not r['kategorie'] and re.fullmatch(r'('+CATWORD+r')', tp.strip(), re.I):
        r['kategorie']=tp.strip(); r['kat_src']='topic'   # topic itself is a category word
    if not r['kategorie']:                 # no reliable signal -> honest low-conf prior
        s=duration or 0
        r['kategorie']=('Clip' if s<120 else 'Beitrag/Episode' if s<1800 else 'unklar')
        r['kat_src']='duration-prior'

    my=re.search(r'\s*\((?:19|20)\d\d\)\s*$', t)              # trailing "(YYYY)" disambiguation
    if my:
        if not r['jahr']: r['jahr']=int(my.group(0).strip('() '))
        t=t[:my.start()]
    r['titel']=re.sub(r'\s{2,}',' ',t).strip(' -–|:')
    return r
