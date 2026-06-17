# -- metadata extraction (phase 3, part 1) ----------------------------------
# Pure, deterministic: one mediathek row -> structured metadata, following the
# per-sender broadcaster conventions reverse-engineered for phase 3 (see git
# history). Conservative: a field stays None when no convention applies.
import re

CATWORD = (r'Spielfilm|Fernsehfilm|TV-Film|Dokumentarfilm|Dokumentation|Dokudrama'
           r'|Doku-Reihe|Kurzfilm|Animationsfilm|Zeichentrickfilm|Trickfilm'
           r'|Komödie|Drama|Thriller|Krimi|Spielfilmreihe|Kinderfilm|Serie|Reportage|Magazin')
META    = re.compile(r'\b(' + CATWORD + r'),?\s+(.{2,40}?)\s+((?:19|20)\d\d)\b')

SE_A    = re.compile(r'\(S(\d+)/E(\d+)\)')                    # ZDF family + thirds
SE_B    = re.compile(r'\((?:Staffel\s*(\d+),\s*)?Folge\s*(\d+)\)')   # SRF
LEADC   = re.compile(r'^\s*(\d{1,4})\.\s+')                   # KiKA leading "NN."
PART    = re.compile(r'\((\d{1,2})/(\d{1,2})\)')             # Mehrteiler "(n/m)"
PIPESUF = re.compile(r'\s*\|\|?\s*[^|]+(?:\|\|?\s*[^|]+)*$')  # trailing " | Reihe"
TRAILER = re.compile(r'\b(Trailer|Teaser|Vorschau|Vorab|Preview|Präview)\b', re.I)

# Parenthetical marker vocabulary -> (target, value). 'flag' adds a letter to
# the flags string (A audio-description, S sign-language, U burned-in subs;
# T trailer is added separately). 'language' sets the spoken language. 'strip'
# removes a known noise marker from the title without storing anything (no field
# for it yet).
MARKERS = [
    (re.compile(r'^Audiodeskription$|^Hörfassung$', re.I),                       ('flag', 'A')),
    (re.compile(r'Gebärdensprache$|^ÖGS$', re.I),                                ('flag', 'S')),
    (re.compile(r'^Originalversion mit Untertitel$|^mit Untertitel$|^OmU$|^OmdU$', re.I), ('flag', 'U')),
    (re.compile(r'^Originalversion$|^OV$', re.I),                                 ('language', 'ov')),
    (re.compile(r'^engl\.?$|^Englisch$|^English$', re.I),                         ('language', 'en')),
    (re.compile(r'^frz\.?$|^franz\.?$|^französisch$', re.I),                      ('language', 'fr')),
    (re.compile(r'^stumm$|^ohne Ton$|^tlw\. stumm$', re.I),                       ('strip', None)),
]
ARTE_LANG = {'ARTE.DE':'de','ARTE.FR':'fr','ARTE.EN':'en','ARTE.ES':'es','ARTE.IT':'it','ARTE.PL':'pl'}
TITLE_META_SENDERS = {'ZDF', '3Sat'}
ARTE_CAT = {'Kino':'Film','Fernsehfilme und Serien':'Serie/Fernsehfilm','ARTE Concert':'Konzert',
            'Geschichte':'Doku','Wissenschaft':'Doku','Entdeckung der Welt':'Doku','Entdeckung':'Doku',
            'Aktuelles und Gesellschaft':'Reportage','Kultur und Pop':'Kultur'}
ARTE_SUB = {'Filme':'Spielfilm','Kurzfilme':'Kurzfilm','Stummfilme':'Stummfilm',
            'Serien':'Serie','Fernsehfilme':'Fernsehfilm'}

# Columns classify writes; the returned dict has exactly these keys. status and
# mediathek_id are handled by the DB layer, not here.
CLASSIFY_COLS = ['clean_title', 'series_name', 'season', 'episode', 'episode_count',
                 'category', 'year', 'country', 'language', 'flags', 'classify_confidence']


def _confidence(kat_src, category):
    """Deterministic confidence from how the category was found."""
    if kat_src in ('metazeile', 'arte-topic'): return 0.9
    if kat_src == 'topic':                      return 0.8
    return 0.2 if category == 'unklar' else 0.5


def classify(sender, topic, title, description, duration) -> dict:
    """A mediathek row -> extracted metadata dict (keys == CLASSIFY_COLS)."""
    t = title or ''; d = (description or '').strip(); tp = topic or ''
    flags = set()
    kat_src = None
    r = dict(clean_title=None, series_name=None, season=None, episode=None,
             episode_count=None, category=None, year=None, country=None,
             language=ARTE_LANG.get(sender, 'de'), flags='', classify_confidence=None)

    # -- Pass 1: parenthetical markers (extract + strip) -------------------
    def take_parens(s):
        def repl(m):
            inner = m.group(1)
            for rx, (target, val) in MARKERS:
                if rx.match(inner):
                    if target == 'flag':       flags.add(val)
                    elif target == 'language': r['language'] = val
                    return ''                  # strip recognized marker
            return m.group(0)                  # keep unrecognized
        return re.sub(r'\s*\(([^()]{1,40})\)', repl, s)
    t = take_parens(t)
    if TRAILER.search(title or '') or TRAILER.search(tp):
        flags.add('T')

    # -- Pass 2: episode notation -----------------------------------------
    m = SE_A.search(t)
    if m:
        if len(m.group(1)) == 4:               # 4-digit season = broadcast year
            r['year'] = r['year'] or int(m.group(1))
        else:
            r['season'] = int(m.group(1)); r['episode'] = int(m.group(2))
        t = SE_A.sub('', t)
    else:
        m = SE_B.search(t)                     # SRF "(Staffel N, Folge M)"/"(Folge M)"
        if m:
            if m.group(1): r['season'] = int(m.group(1))
            r['episode'] = int(m.group(2)); t = SE_B.sub('', t)
        elif sender == 'KiKA':
            m = LEADC.search(t)                # KiKA leading "NN."
            if m: r['episode'] = int(m.group(1)); t = LEADC.sub('', t)
    pm = PART.search(t)                        # Mehrteiler "(n/m)": n->episode, m->count
    if pm:
        r['episode_count'] = int(pm.group(2))
        if r['episode'] is None: r['episode'] = int(pm.group(1))
        t = PART.sub('', t)

    # -- Pass 3: metazeile (category + country + year) --------------------
    src = t if sender in TITLE_META_SENDERS else d
    m = META.search(src) if src else None
    if m and len(m.group(2)) < 40:
        r['category'] = m.group(1); kat_src = 'metazeile'
        country = m.group(2).strip(' ,-')
        if ',' in country: country = country.split(',')[-1].strip()   # drop "von <Regisseur>," prefix
        r['country'] = country; r['year'] = r['year'] or int(m.group(3))
        if sender in TITLE_META_SENDERS:       # strip "- Spielfilm, ... YEAR" from title
            t = t[:m.start()].rstrip(' -–')

    # -- Pass 4: series_name ----------------------------------------------
    if sender not in ARTE_LANG:
        r['series_name'] = tp
        t = PIPESUF.sub('', t)                 # drop " | Reihe" suffix from title

    # -- Pass 5: category from ARTE taxonomy / duration prior -------------
    if sender in ARTE_LANG and ' - ' in tp:
        ober, _, unter = tp.partition(' - ')
        r['category'] = ARTE_SUB.get(unter.strip(), ARTE_CAT.get(ober.strip()))
        kat_src = 'arte-topic'
    if not r['category'] and re.fullmatch(r'(' + CATWORD + r')', tp.strip(), re.I):
        r['category'] = tp.strip(); kat_src = 'topic'   # topic itself is a category word
    if not r['category']:                      # no reliable signal -> honest low-conf prior
        s = duration or 0
        r['category'] = ('Clip' if s < 120 else 'Beitrag/Episode' if s < 1800 else 'unklar')
        kat_src = 'duration-prior'

    my = re.search(r'\s*\((?:19|20)\d\d\)\s*$', t)       # trailing "(YYYY)" disambiguation
    if my:
        if not r['year']: r['year'] = int(my.group(0).strip('() '))
        t = t[:my.start()]
    r['clean_title'] = re.sub(r'\s{2,}', ' ', t).strip(' -–|:')

    r['flags'] = ''.join(sorted(flags))        # canonical alphabetical order (A<S<T<U)
    r['classify_confidence'] = _confidence(kat_src, r['category'])
    return r
