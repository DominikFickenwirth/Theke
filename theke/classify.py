# -- metadata extraction (phase 3, part 1) ----------------------------------
# Pure, deterministic: one mediathek row -> structured metadata, following the
# per-sender broadcaster conventions. A field stays None when no convention applies.
import re

CATWORD = (r'Spielfilm|Fernsehfilm|TV-Film|Dokumentarfilm|Dokumentation|Dokudrama'
           r'|Doku-Reihe|Kurzfilm|Animationsfilm|Zeichentrickfilm|Trickfilm'
           r'|Komödie|Drama|Thriller|Krimi|Spielfilmreihe|Kinderfilm|Serie|Reportage|Magazin')
META    = re.compile(r'\b(' + CATWORD + r'),?\s+(.{2,40}?)\s+((?:19|20)\d\d)\b')
# A metazeile country slot only counts when it actually looks like a country (or
# a list of them): an uppercase start and no function words / date fragments.
# Shared with the classify audit's country-shape check.
COUNTRY_BAD = re.compile(r'^[a-zäöü·"]|\b(von|über|aus|im|mit|der|die|das|und'
                         r'|dem|den|eine?r?|Jahr|vom)\b')


def looks_like_country(s) -> bool:
    return bool(s) and not COUNTRY_BAD.search(s)


def _to_int(s):
    """An arabic or roman 'Teil' number to int."""
    if s.isdigit():
        return int(s)
    total = prev = 0
    for ch in reversed(s.upper()):
        v = ROMAN.get(ch, 0)
        total += -v if v < prev else v; prev = max(prev, v)
    return total

SE_A    = re.compile(r'\(S(\d+)/E(\d+)\)')                    # ZDF family + thirds
SE_B    = re.compile(r'\((?:Staffel\s*(\d+),\s*)?Folge\s*(\d+)\)')   # SRF
LEADC   = re.compile(r'^\s*(\d{1,4})\.\s+')                   # KiKA leading "NN."
PART    = re.compile(r'\((\d{1,2})/(\d{1,2})\)')             # Mehrteiler "(n/m)"
# Paren-less episode notation (B5), all guarded: "Staffel n, Folge m";
# "- Teil n" (arabic or roman); a bare "n/m" only at the very end (so dates
# like "10/06/2026" and "3 1/2 Stunden" never match).
STAFFOLGE = re.compile(r'\bStaffel\s+(\d{1,2}),?\s+Folge\s+(\d{1,3})\b', re.I)
TEIL      = re.compile(r'\s*[-–(]?\s*\bTeil\s+(\d{1,2}|[IVXLC]+)(?:\s*/\s*(\d{1,2}))?\b\)?', re.I)
NPART     = re.compile(r'\s*(?<![\d./])(\d{1,2})\s*/\s*(\d{1,2})\s*$')
ROMAN   = {'I':1,'V':5,'X':10,'L':50,'C':100}
PIPESUF = re.compile(r'\s*\|\|?\s*[^|]+(?:\|\|?\s*[^|]+)*$')  # trailing " | Reihe"
# Trailing "- <Format> von <Name>" director credit (B4); no year -> not a
# metazeile, just strip it off the title (no country/year extraction).
CREDIT  = re.compile(r'\s+[-–]\s+(?:Film|' + CATWORD + r')\s+von\s+\S.*$', re.I)
TRAILER = re.compile(r'\b(Trailer|Teaser|Vorschau|Vorab|Preview|Präview)\b', re.I)

# Parenthetical marker vocabulary -> (target, value). 'flag' adds a letter to
# the flags string (A audio-description, S sign-language, U burned-in subs;
# T trailer is added separately). 'language' sets the spoken language. 'strip'
# removes a known noise marker from the title without storing anything (no field
# for it yet).
MARKERS = [
    (re.compile(r'^Audiodeskription$|^Hörfassung$', re.I),                       ('flag', 'A')),
    (re.compile(r'^(?:mit\s+)?Gebärdensprache$|^ÖGS$', re.I),                     ('flag', 'S')),
    (re.compile(r'^(?:in\s+)?(?:Einfache[r]?|Leichte[r]?)\s+Sprache$', re.I),     ('flag', 'E')),
    (re.compile(r'^Originalversion mit Untertitel$|^mit Untertitel$|^OmU$|^OmdU$', re.I), ('flag', 'U')),
    (re.compile(r'^Originalversion$|^OV$', re.I),                                 ('language', 'ov')),
    (re.compile(r'^engl\.?$|^Englisch$|^English$', re.I),                         ('language', 'en')),
    (re.compile(r'^frz\.?$|^franz\.?$|^französisch$', re.I),                      ('language', 'fr')),
    (re.compile(r'^stumm$|^ohne Ton$|^tlw\. stumm$', re.I),                       ('strip', None)),
]
# Accessibility markers that appear as a bare (non-parenthesized) trailing suffix
# on the title/topic -> (regex, flag). Stripped off and flagged like the
# parenthetical MARKERS above.
SUFFIX_MARKERS = [
    (re.compile(r'\s+in\s+Gebärdensprache$', re.I),                          'S'),
    (re.compile(r'\s+in\s+(?:Einfacher|Leichter)\s+Sprache$', re.I),         'E'),
]
# -- topic routing vocabulary (B1/B2/B7) ------------------------------------
# A non-ARTE topic is usually a series, but is often a rubric: a bare format
# word, a curated genre, a clip/container bucket, an event, or a Dachmarke|series
# pipe. route_topic() sorts these out so series_name stays a real show name.

# Genre rubrics, matched EXACTLY (never as substring): these appear as a whole
# topic only on the rubric senders (3sat/ZDF/DW/ARTE.DE) and are never a real
# series elsewhere ("Sport" is a rubric, "Sport im Osten" is a series). Shared
# with the classify audit's bare-topic check.
GENRE_SET = {'Reise', 'Natur', 'Musik', 'Tiere', 'Geschichte', 'Politik',
             'Politik und Gesellschaft', 'Sport', 'Nachrichten', 'Wirtschaft',
             'Europa', 'Nahost', 'Deutschland', 'Esskulturen', 'Kultur',
             'Kulturdoku', 'Gesellschaft', 'Wissen', 'Wissenschaftsdoku', 'Buch',
             'Theater', 'Märchen'}

# Topic that is itself a format -> category, no series. Bare/compound rubrics map
# to a canonical category; plain CATWORD topics keep their own word.
FORMAT_TOPICS = {'film':'Film', 'filme':'Film', 'filme in der ard':'Film',
                 'doku':'Dokumentation', 'dokus':'Dokumentation',
                 'dokumentationen':'Dokumentation',
                 'doku & reportage':'Doku/Reportage',
                 'dokus & reportagen':'Doku/Reportage',
                 'dokumentationen und reportagen':'Doku/Reportage'}

# Clip/container sammeltopics: series=None, category left to the duration prior.
CONTAINER_TOPICS = {'tagesschau24', 'beiträge', 'br', 'sr', '3sat', 'sportflash',
                    'zib flash', 'srf news videos', 'sr 3 videos', 'vintage videos'}
EVENT_RX = re.compile(r'\b(Berlinale|Grimme[- ]Preis|Filmpreis|Filmfest'
                      r'|Goldene Kamera|Festival)\b')

# Pipe split: the side carrying a sender token, a Dachmarke or a section word is
# the slot; the other side is the series. Neither -> title|subtitle, do not split.
SENDER_TOKENS = {'ard', 'zdf', '3sat', 'hr', 'br', 'wdr', 'ndr', 'swr', 'sr',
                 'mdr', 'rbb', 'orf', 'srf', 'rbtv', 'alpha', 'arte', 'phoenix',
                 'dw', 'kika'}
BRANDS = ['ard wissen', 'radio bremen', 'alpha lernen']
SECTION_WORDS = {'regionalmagazin', 'sportblitz', 'wetter', 'doku', 'extra',
                 'retro', 'geschichten', 'spezial'}


def _format_category(tp):
    c = FORMAT_TOPICS.get(tp.casefold())
    if c: return c
    return tp if re.fullmatch(r'(' + CATWORD + r')', tp, re.I) else None


def _is_container(tp):
    low = tp.casefold()
    return (low in CONTAINER_TOPICS or bool(re.search(r'clips?$', low))
            or bool(re.search(r'\bvideos?\b', low)))


def _side_is_slot(s):
    low = s.casefold()
    toks = set(re.findall(r'[a-zäöüß0-9]+', low))
    return (bool(toks & SENDER_TOKENS) or any(b in low for b in BRANDS)
            or bool(toks & SECTION_WORDS))


def route_topic(topic) -> dict:
    """Route a non-ARTE topic. Returns dict(series_name, genre, slot, category,
    kat_src) with None where a slot does not apply."""
    out = dict(series_name=None, genre=None, slot=None, category=None, kat_src=None)
    tp = (topic or '').strip()
    if not tp:
        return out
    if '|' in tp:                              # Dachmarke|series pipe
        parts = [p.strip() for p in tp.split('|')]
        if len(parts) == 2 and parts[0] and parts[1]:
            a, b = parts
            sa, sb = _side_is_slot(a), _side_is_slot(b)
            if sa and not sb: out['slot'], out['series_name'] = a, b; return out
            if sb and not sa: out['slot'], out['series_name'] = b, a; return out
        out['series_name'] = tp                # both/neither slot -> keep whole
        return out
    cat = _format_category(tp)
    if cat:
        out['category'] = cat; out['kat_src'] = 'topic'; return out
    if _is_container(tp):
        return out                             # series None, category from prior
    if tp in GENRE_SET:
        out['genre'] = tp; return out
    if EVENT_RX.search(tp):
        out['series_name'] = tp; out['category'] = 'Events'; out['kat_src'] = 'event'
        return out
    out['series_name'] = tp                    # long tail: today's behavior
    return out


ARTE_LANG = {'ARTE.DE':'de','ARTE.FR':'fr','ARTE.EN':'en','ARTE.ES':'es','ARTE.IT':'it','ARTE.PL':'pl'}
TITLE_META_SENDERS = {'ZDF', '3Sat'}
# ARTE genre taxonomy "Ober - Unter"; the sub-label wins, else the super-label.
# Keys are the source labels in every ARTE UI language (DE/FR/EN/ES/IT/PL); the
# category values stay the German canonical set.
ARTE_CAT = {'Kino':'Film','Fernsehfilme und Serien':'Serie/Fernsehfilm','ARTE Concert':'Konzert',
            'Geschichte':'Doku','Wissenschaft':'Doku','Entdeckung der Welt':'Doku','Entdeckung':'Doku',
            'Aktuelles und Gesellschaft':'Reportage','Kultur und Pop':'Kultur',
            # FR
            'Cinéma':'Film','Histoire':'Doku','Sciences':'Doku','Info et société':'Reportage',
            'Séries et fictions':'Serie/Fernsehfilm','Culture et pop':'Kultur','Voyages et découvertes':'Doku',
            # EN
            'Cinema':'Film','History':'Doku','Politics and society':'Reportage',
            'Series':'Serie/Fernsehfilm','Culture':'Kultur',
            # ES
            'Cine':'Film','Historia':'Doku','Ciencias':'Doku','Política y sociedad':'Reportage',
            'Series y ficciones':'Serie/Fernsehfilm','Cultura y pop':'Kultur','Viajes y naturaleza':'Doku',
            # IT
            'Storia':'Doku','Scienze':'Doku','Politica e società':'Reportage',
            'Serie e fiction':'Serie/Fernsehfilm','Cultura':'Kultur','Viaggi e scoperte':'Doku',
            # PL
            'Kino':'Film','Nauka':'Doku','Polityka i społeczeństwo':'Reportage',
            'Seriale i filmy fabularne':'Serie/Fernsehfilm','Kultura':'Kultur','Odkrycia':'Doku'}
ARTE_SUB = {'Filme':'Spielfilm','Kurzfilme':'Kurzfilm','Stummfilme':'Stummfilm',
            'Serien':'Serie','Fernsehfilme':'Fernsehfilm',
            'Films':'Spielfilm','Film':'Spielfilm','Películas':'Spielfilm','Filmy':'Spielfilm',
            'Courts métrages':'Kurzfilm','Short films':'Kurzfilm','Cortometrajes':'Kurzfilm',
            'Cortometraggi':'Kurzfilm','Filmy krótkometrażowe':'Kurzfilm',
            'Séries':'Serie','Series':'Serie','Serie':'Serie','Seriale':'Serie',
            'Webseries':'Serie','Webseriale':'Serie'}

# Columns classify writes; the returned dict has exactly these keys. status and
# mediathek_id are handled by the DB layer, not here.
CLASSIFY_COLS = ['clean_title', 'series_name', 'genre', 'slot', 'season', 'episode',
                 'episode_count', 'category', 'year', 'country', 'language', 'flags',
                 'classify_confidence']


def _confidence(kat_src, category):
    """Deterministic confidence from how the category was found."""
    if kat_src in ('metazeile', 'arte-topic'): return 0.9
    if kat_src in ('topic', 'event'):           return 0.8
    return 0.2 if category == 'unklar' else 0.5


def classify(sender, topic, title, description, duration) -> dict:
    """A mediathek row -> extracted metadata dict (keys == CLASSIFY_COLS)."""
    t = title or ''; d = (description or '').strip(); tp = topic or ''
    flags = set()
    kat_src = None
    r = dict(clean_title=None, series_name=None, genre=None, slot=None, season=None,
             episode=None, episode_count=None, category=None, year=None, country=None,
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

    def take_suffix(s):                        # bare accessibility suffix (no parens)
        for rx, flag in SUFFIX_MARKERS:
            m = rx.search(s)
            if m: flags.add(flag); s = s[:m.start()]
        return s
    t = take_suffix(take_parens(t))
    tp = take_suffix(take_parens(tp))          # markers also live in the topic
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
    if r['episode'] is None:                   # paren-less notation (B5), guarded
        mf = STAFFOLGE.search(t)
        mt = TEIL.search(t)
        mn = NPART.search(t)
        if mf:
            r['season'] = int(mf.group(1)); r['episode'] = int(mf.group(2))
            t = STAFFOLGE.sub('', t)
        elif mt:
            r['episode'] = _to_int(mt.group(1))
            if mt.group(2): r['episode_count'] = int(mt.group(2))
            t = TEIL.sub('', t)
        elif mn and int(mn.group(1)) <= int(mn.group(2)) <= 50:   # n<=m, no dates
            r['episode'] = int(mn.group(1)); r['episode_count'] = int(mn.group(2))
            t = t[:mn.start()]

    # -- Pass 3: metazeile (category + country + year) --------------------
    src = t if sender in TITLE_META_SENDERS else d
    m = META.search(src) if src else None
    if m and len(m.group(2)) < 40:
        country = m.group(2).strip(' ,-')
        if ',' in country: country = country.split(',')[-1].strip()   # drop "von <Regisseur>," prefix
        if looks_like_country(country):        # else it is a sentence fragment/date -> reject
            r['category'] = m.group(1); kat_src = 'metazeile'
            r['country'] = country; r['year'] = r['year'] or int(m.group(3))
            if sender in TITLE_META_SENDERS:   # strip "- Spielfilm, ... YEAR" from title
                t = t[:m.start()].rstrip(' -–')

    # -- Pass 4: series_name via topic routing ----------------------------
    if sender not in ARTE_LANG:
        routed = route_topic(tp)
        r['series_name'] = routed['series_name']
        r['genre'] = routed['genre']; r['slot'] = routed['slot']
        if routed['category'] and not r['category']:   # metazeile (Pass 3) wins
            r['category'] = routed['category']; kat_src = routed['kat_src']
        t = PIPESUF.sub('', t)                 # drop " | Reihe" suffix from title

    # -- Pass 5: category from ARTE taxonomy / duration prior -------------
    if sender in ARTE_LANG and ' - ' in tp:
        ober, _, unter = tp.partition(' - ')
        r['category'] = ARTE_SUB.get(unter.strip(), ARTE_CAT.get(ober.strip()))
        kat_src = 'arte-topic'
    if not r['category']:                      # no reliable signal -> honest low-conf prior
        s = duration or 0
        r['category'] = ('Clip' if s < 120 else 'Beitrag/Episode' if s < 1800 else 'unklar')
        kat_src = 'duration-prior'

    cm = CREDIT.search(t)                                 # trailing "- Film von <Name>" (B4)
    if cm and not re.search(r'(?:19|20)\d\d', cm.group(0)):
        t = t[:cm.start()]

    my = re.search(r'\s*\((?:19|20)\d\d\)\s*$', t)       # trailing "(YYYY)" disambiguation
    if my:
        if not r['year']: r['year'] = int(my.group(0).strip('() '))
        t = t[:my.start()]
    r['clean_title'] = re.sub(r'\s{2,}', ' ', t).strip(' -–|:')

    r['flags'] = ''.join(sorted(flags))        # canonical alphabetical order (A<S<T<U)
    r['classify_confidence'] = _confidence(kat_src, r['category'])
    return r
