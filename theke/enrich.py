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
# Shared with the enrich audit's country-shape check.
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
TEIL      = re.compile(r'\s*[-–(,]?\s*\bTeil\s+(\d{1,2}|[IVXLC]+)(?:\s*/\s*(\d{1,2}))?\b\)?', re.I)
NPART     = re.compile(r'\s*(?<![\d./])(\d{1,2})\s*/\s*(\d{1,2})\s*$')
# Leading "Folge N[/M]: <Subtitle>" (also "Episode N", separators : · | - ): the
# running number is the episode (M the count), and the subtitle is the real
# title. \s+\d after the word rejects "Sonderfolge"/"Folge der Spur"/"Folger".
FOLGE_PRE  = re.compile(r'^(?:Folge|Episode)\s+(\d{1,4})(?:\s*/\s*(\d{1,4}))?'
                        r'(?:\s*:\s*|\s+[·|–-]\s+)(.+)$', re.I)
FOLGE_ONLY = re.compile(r'^(?:Folge|Episode)\s+(\d{1,4})(?:\s*/\s*(\d{1,4}))?\s*$', re.I)
# Season markers. STAFFEL_TITLE: a dash-introduced "- Staffel N" segment in the
# title (the dash distinguishes the real season marker from review-clip content
# like "BLACK MIRROR Staffel 6: ..."). STAFFEL_SERIES: a trailing "Staffel N" on
# the topic/series_name (the ORF "<Series> Staffel N" convention).
STAFFEL_TITLE  = re.compile(r'\s*[-–]\s*Staffel\s+(\d{1,2})\b', re.I)
STAFFEL_SERIES = re.compile(r'\s+Staffel\s+(\d{1,2})$', re.I)
# "Titel n/m - Untertitel"; the (?<!\d\s) rejects a mixed fraction "8 1/2 - ..."
# (a whole number + space before the n/m), which is a film runtime, not a part.
MIDPART   = re.compile(r'(?<![\d./])(?<!\d\s)(\d{1,2})/(\d{1,2})\s+(?=[-–]\s)')
ROMAN   = {'I':1,'V':5,'X':10,'L':50,'C':100}
PIPESUF = re.compile(r'\s*\|\|?\s*[^|]+(?:\|\|?\s*[^|]+)*$')  # trailing " | Reihe"
# Trailing "- <Format> von <Name>" director credit (B4); no year -> not a
# metazeile, just strip it off the title (no country/year extraction).
CREDIT  = re.compile(r'\s+[-–]\s+(?:Film|' + CATWORD + r')\s+von\s+\S.*$', re.I)
TRAILER = re.compile(r'\b(Trailer|Teaser|Vorschau|Vorab|Preview|Präview)\b', re.I)
# Companion pieces ABOUT a work (not the work): a making-of (M flag) or an
# interview/Rencontre/Entretien with its makers (I flag). Short pieces only, so a
# feature film merely titled "Interview mit ..." is never caught (see enrich()).
MAKINGOF  = re.compile(r'\bMaking[ -]of\b', re.I)
INTERVIEW = re.compile(r'^\s*(?:Interview mit|Rencontre avec|Entretien avec|Gespräch mit)\b', re.I)

# Parenthetical marker vocabulary -> (target, value). 'flag' adds a letter to
# the flags string (A audio-description, S sign-language, U burned-in subs;
# T trailer is added separately). 'language' sets the spoken language. 'sub_ov'
# does both: an original-version-with-subtitles marker implies burned-in subs (U)
# AND the original spoken language (ov), so the ARTE sender (= subtitle) language
# does not stick. 'strip' removes a known noise marker without storing anything.
MARKERS = [
    (re.compile(r'^Audiodeskription$|^Hörfassung$', re.I),                       ('flag', 'A')),
    (re.compile(r'^(?:mit\s+)?Gebärdensprache$|^ÖGS$', re.I),                     ('flag', 'S')),
    (re.compile(r'^(?:in\s+)?(?:Einfache[r]?|Leichte[r]?)\s+Sprache$', re.I),     ('flag', 'E')),
    (re.compile(r'^Originalversion mit Untertitel$|^OmU$|^OmdU$', re.I),          ('sub_ov', 'U')),
    (re.compile(r'^mit Untertitel$', re.I),                                       ('flag', 'U')),
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
# -- output taxonomy: medium (category) + TMDB genre ------------------------
# Two orthogonal axes. category is the medium, a small custom set
# (Movie/Episode/Clip/Event/None); genre is TMDB-only (no custom genres, so a
# later TMDB lookup stays clean), multiple values comma-joined in this order.
TMDB_ORDER = ['Action', 'Adventure', 'Animation', 'Comedy', 'Crime', 'Documentary',
              'Drama', 'Family', 'Kids', 'Fantasy', 'History', 'Horror', 'Music',
              'Mystery', 'News', 'Reality', 'Romance', 'SciFi', 'Soap', 'Talk',
              'Thriller', 'War', 'Western']

# German format/CATWORD label -> (category, genre-tuple). The word carries the
# medium, sometimes also a genre (Dokumentarfilm = Movie + Documentary).
LABEL_MAP = {
    'Spielfilm':       ('Movie', ()),        'Spielfilmreihe':  ('Movie', ()),
    'Fernsehfilm':     ('Movie', ()),        'TV-Film':         ('Movie', ()),
    'Kurzfilm':        ('Movie', ()),        'Stummfilm':       ('Movie', ()),
    'Film':            ('Movie', ()),
    'Animationsfilm':  ('Movie', ('Animation',)),
    'Zeichentrickfilm':('Movie', ('Animation',)),
    'Trickfilm':       ('Movie', ('Animation',)),
    'Kinderfilm':      ('Movie', ('Family',)),
    'Komödie':         ('Movie', ('Comedy',)),
    'Drama':           ('Movie', ('Drama',)),
    'Thriller':        ('Movie', ('Thriller',)),
    'Krimi':           ('Movie', ('Crime',)),
    'Dokumentarfilm':  ('Movie', ('Documentary',)),
    'Dokudrama':       ('Movie', ('Documentary', 'Drama')),
    'Dokumentation':   ('Episode', ('Documentary',)),
    'Doku':            ('Episode', ('Documentary',)),
    'Doku-Reihe':      ('Episode', ('Documentary',)),
    'Doku/Reportage':  ('Episode', ('Documentary',)),
    'Reportage':       ('Episode', ('Documentary',)),
    'Serie':           ('Episode', ()),
    'Magazin':         ('Episode', ()),
    'Konzert':         ('Clip', ('Music',)),
}

# Topic genre rubric -> TMDB genre-tuple (category stays the duration prior).
# Non-fiction theme rubrics with no specific TMDB genre collapse to Documentary.
GENRE_MAP = {
    'Musik':            ('Music',),
    'Geschichte':       ('Documentary', 'History'),
    'Nachrichten':      ('News',),
    'Politik':          ('News',),  'Politik und Gesellschaft': ('News',),
    'Europa':           ('News',),  'Nahost': ('News',),  'Deutschland': ('News',),
    'Wirtschaft':       ('News',),
    'Märchen':          ('Family', 'Fantasy'),
    'Reise':            ('Documentary',),  'Natur': ('Documentary',),
    'Tiere':            ('Documentary',),  'Esskulturen': ('Documentary',),
    'Kultur':           ('Documentary',),  'Kulturdoku': ('Documentary',),
    'Wissen':           ('Documentary',),  'Wissenschaftsdoku': ('Documentary',),
    'Gesellschaft':     ('Documentary',),  'Buch': ('Documentary',),
    'Theater':          ('Documentary',),  'Sport': ('Documentary',),
}


def _genre_str(genres):
    """A set/iterable of TMDB genres -> canonical comma-joined string (or None)."""
    ordered = [g for g in TMDB_ORDER if g in set(genres)]
    return ', '.join(ordered) or None


# -- topic routing vocabulary (B1/B2/B7) ------------------------------------
# A non-ARTE topic is usually a series, but is often a rubric: a bare format
# word, a curated genre, a clip/container bucket, an event, or a Dachmarke|series
# pipe. route_topic() sorts these out so series_name stays a real show name.

# Genre rubrics, matched EXACTLY (never as substring): these appear as a whole
# topic only on the rubric senders (3sat/ZDF/DW/ARTE.DE) and are never a real
# series elsewhere ("Sport" is a rubric, "Sport im Osten" is a series). The set
# is the GENRE_MAP keys; shared with the enrich audit's bare-topic check.
GENRE_SET = set(GENRE_MAP)

# Topic that is itself a format -> category, no series. Bare/compound rubrics map
# to a canonical category; plain CATWORD topics keep their own word.
FORMAT_TOPICS = {'film':'Film', 'filme':'Film', 'filme in der ard':'Film',
                 'doku':'Dokumentation', 'dokus':'Dokumentation',
                 'dokumentationen':'Dokumentation',
                 'doku & reportage':'Doku/Reportage',
                 'dokus & reportagen':'Doku/Reportage',
                 'dokumentationen und reportagen':'Doku/Reportage'}

# Programming-slot topics (Sendeplätze): a film-type head + an end-anchored
# placement phrase ("Filme im Ersten", "Spielfilm in 3sat", "Der Fernsehfilm der
# Woche"). These are a slot, not a show -> slot=topic, series_name=None (category
# is left untouched: a fiction strand keeps its Movie lift via the raw topic). The
# film-type head guards real shows ("Nuhr im Ersten"); the `$` anchor guards
# "... im Dritten Reich" (a placement phrase that is not the tail).
SLOT_HEAD = (r'(?:Der\s+|Die\s+|Das\s+)?'
             r'(?:Spielfilme?|Fernsehfilme?|Filme?|Dokus?|Dokumentarfilme?|Dokumentation'
             r'|Kurzfilme?|Familienfilme?|Kinderfilme?|Kulturdoku|Krimis?|Debüt'
             r'|Sommerkino|FilmMittwoch|Filmdebüt)')
SLOT_TAIL = (r'(?:im\s+(?:Ersten|Zweiten|Dritten|MDR|WDR|NDR|BR|SWR|HR|RBB|SR|RBTV)'
             r'|in\s+3sat|der\s+Woche)')
SLOT_RX   = re.compile(rf'^{SLOT_HEAD}\b.*\b{SLOT_TAIL}$', re.I)

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


LABEL_CF = {k.casefold(): k for k in LABEL_MAP}   # case-fold -> canonical label


def _format_category(tp):
    """A format topic -> its canonical LABEL_MAP key, else None."""
    c = FORMAT_TOPICS.get(tp.casefold())
    if c: return c
    return LABEL_CF.get(tp.casefold()) if re.fullmatch(CATWORD, tp, re.I) else None


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
    kat_src); category is a medium value, genre a TMDB genre-tuple ()."""
    out = dict(series_name=None, genre=(), slot=None, category=None, kat_src=None)
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
    label = _format_category(tp)
    if label:
        out['category'], out['genre'] = LABEL_MAP[label]
        out['kat_src'] = 'topic'; return out
    if _is_container(tp):
        return out                             # series None, category from prior
    if tp in GENRE_SET:
        out['genre'] = GENRE_MAP[tp]; return out
    if EVENT_RX.search(tp):
        out['series_name'] = tp; out['category'] = 'Event'; out['kat_src'] = 'event'
        return out
    if SLOT_RX.match(tp):                       # programming strand -> slot, no series
        out['slot'] = tp; return out
    out['series_name'] = tp                    # long tail: today's behavior
    return out


# Known fiction-Reihe topics: a NULL medium (no film metazeile on this airing)
# is lifted to Movie, matching the labelled airings of the same brand so a Reihe
# is internally consistent. Derived from the live DB: every topic here already
# produces >=8 metazeile-labelled Movie rows (a film label never appears on talk/
# news/sports, so this set cannot pull in non-fiction). The lift fires only on a
# NULL category (never overriding Episode from Sxx/Exx or Clip from a trailer);
# the residual per-airing Movie/Episode scatter is regrouped by match via
# series_name. Generic film SLOTS in this set (Filme im Ersten, FilmMittwoch im
# Ersten, ...) keep series_name = slot for now; moving them to FORMAT_TOPICS for a
# cleaner NULL series_name is a separate later cleanup. Matched casefold == topic.
FICTION_TOPICS = {t.casefold() for t in (
    'Tatort', 'Polizeiruf 110', 'Märchen in der ARD', 'Debüt im Dritten',
    'Dokumentarfilmzeit', 'FilmMittwoch im Ersten', 'Spielfilm-Highlights',
    'Der Usedom-Krimi', 'Filme im Ersten', 'Donna Leon', 'Praxis mit Meerblick',
    'Der Kroatien-Krimi', 'Krause', 'Daheim in den Bergen', 'Rebecka Martinsson',
    'Kommissar Dupin', 'Kommissar Wallander', 'Harter Brocken Krimireihe',
    'Zimmer mit Stall', 'Pfarrer Braun', 'Anna und ihr Untermieter', 'Wolfsland',
    'Utta Danella', 'Steirerkrimi', 'Ein Krimi aus Passau',
    'Der Ranger - Paradies Heimat', 'Spielfilm in 3sat', 'Liebe am Fjord',
    'Käthe und ich', 'Kluftingerkrimis', 'Die drei von der Müllabfuhr',
    'Der Wien-Krimi: Blind ermittelt', 'Mankells Wallander', 'Die Diplomatin',
    'Die Bestatterin', 'Der Pate', 'Der Kommissar und die Alpen',
    'Toni, männlich, Hebamme', 'Nord bei Nordwest', 'Mordkommission Istanbul',
    'Mord in bester Gesellschaft', 'Krimis im Ersten', 'Die Inselärztin',
    'Der Bozen-Krimi')}

ARTE_LANG = {'ARTE.DE':'de','ARTE.FR':'fr','ARTE.EN':'en','ARTE.ES':'es','ARTE.IT':'it','ARTE.PL':'pl'}
TITLE_META_SENDERS = {'ZDF', '3Sat'}
# ARTE taxonomy "Ober - Unter": the super-label (Ober) carries the genre, the
# sub-label (Unter) the medium. A recognized super-label suppresses the duration
# prior, so an unknown sub-label leaves category NULL (honest), never a guess.
# Keys are the source labels in every ARTE UI language (DE/FR/EN/ES/IT/PL).
# Super-label -> (category, genre-tuple); category usually None (medium unknown).
ARTE_OBER = {'Kino':(None,()),'Cinéma':(None,()),'Cinema':(None,()),'Cine':(None,()),
             'Fernsehfilme und Serien':(None,()),'Séries et fictions':(None,()),
             'Series':(None,()),'Series y ficciones':(None,()),'Serie e fiction':(None,()),
             'Seriale i filmy fabularne':(None,()),
             'ARTE Concert':('Clip',('Music',)),
             'Geschichte':(None,('Documentary','History')),'Histoire':(None,('Documentary','History')),
             'History':(None,('Documentary','History')),'Historia':(None,('Documentary','History')),
             'Storia':(None,('Documentary','History')),
             'Wissenschaft':(None,('Documentary',)),'Sciences':(None,('Documentary',)),
             'Ciencias':(None,('Documentary',)),'Scienze':(None,('Documentary',)),
             'Nauka':(None,('Documentary',)),
             'Entdeckung der Welt':(None,('Documentary',)),'Entdeckung':(None,('Documentary',)),
             'Voyages et découvertes':(None,('Documentary',)),'Viajes y naturaleza':(None,('Documentary',)),
             'Viaggi e scoperte':(None,('Documentary',)),'Odkrycia':(None,('Documentary',)),
             'Aktuelles und Gesellschaft':(None,('News',)),'Info et société':(None,('News',)),
             'Politics and society':(None,('News',)),'Política y sociedad':(None,('News',)),
             'Politica e società':(None,('News',)),'Polityka i społeczeństwo':(None,('News',)),
             'Kultur und Pop':(None,('Documentary',)),'Culture et pop':(None,('Documentary',)),
             'Culture':(None,('Documentary',)),'Cultura y pop':(None,('Documentary',)),
             'Cultura':(None,('Documentary',)),'Kultura':(None,('Documentary',))}
# Sub-label -> medium category.
ARTE_SUB = {'Filme':'Movie','Films':'Movie','Film':'Movie','Películas':'Movie','Filmy':'Movie',
            'Kurzfilme':'Movie','Courts métrages':'Movie','Short films':'Movie','Cortometrajes':'Movie',
            'Cortometraggi':'Movie','Filmy krótkometrażowe':'Movie',
            'Stummfilme':'Movie','Fernsehfilme':'Movie',
            'Serien':'Episode','Séries':'Episode','Series':'Episode','Serie':'Episode',
            'Seriale':'Episode','Webseries':'Episode','Webseriale':'Episode'}

# Columns enrich writes; the returned dict has exactly these keys. status and
# mediathek_id are handled by the DB layer, not here.
ENRICH_COLS = ['clean_title', 'series_name', 'genre', 'slot', 'season', 'episode',
                 'episode_count', 'category', 'year', 'country', 'language', 'flags',
                 'enrich_confidence']


def _confidence(kat_src, category):
    """Deterministic confidence from how the category was found."""
    if kat_src in ('metazeile', 'arte-topic'): return 0.9
    if kat_src in ('topic', 'event'):           return 0.8
    return 0.2 if category is None else 0.5


def enrich(sender, topic, title, description, duration,
            fiction_topics=FICTION_TOPICS) -> dict:
    """A mediathek row -> extracted metadata dict (keys == ENRICH_COLS).

    fiction_topics is the casefolded fiction-Reihe allowlist (built-in default;
    the CLI passes the default unioned with config['fiction_topics'])."""
    t = title or ''; d = (description or '').strip(); tp = topic or ''
    flags = set()
    genres = set()
    kat_src = None
    r = dict(clean_title=None, series_name=None, genre=None, slot=None, season=None,
             episode=None, episode_count=None, category=None, year=None, country=None,
             language=ARTE_LANG.get(sender, 'de'), flags='', enrich_confidence=None)

    # -- Pass 1: parenthetical markers (extract + strip) -------------------
    def take_parens(s):
        def repl(m):
            inner = m.group(1)
            for rx, (target, val) in MARKERS:
                if rx.match(inner):
                    if target == 'flag':       flags.add(val)
                    elif target == 'sub_ov':   flags.add(val); r['language'] = 'ov'
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
    if m:                                      # 4-digit season kept as-is, never year
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
    fp = FOLGE_PRE.match(t)                     # leading "Folge N[/M]: Subtitle"
    fo = fp or FOLGE_ONLY.match(t)             # ... or a bare "Folge N" (no subtitle)
    if fo:                                      # fill episode/count only if still unset
        if r['episode'] is None: r['episode'] = int(fo.group(1))
        if fo.group(2) and r['episode_count'] is None: r['episode_count'] = int(fo.group(2))
        t = fp.group(3) if fp else ''          # subtitle, or empty for a bare "Folge N"
    sst = STAFFEL_TITLE.search(t)              # "- Staffel N" season segment in the title
    if sst:
        if r['season'] is None: r['season'] = int(sst.group(1))
        t = STAFFEL_TITLE.sub('', t, count=1)
    if r['episode'] is None:                   # paren-less notation (B5), guarded
        mf = STAFFOLGE.search(t)
        mt = TEIL.search(t)
        md = MIDPART.search(t)
        mn = NPART.search(t)
        if mf:
            r['season'] = int(mf.group(1)); r['episode'] = int(mf.group(2))
            t = STAFFOLGE.sub('', t)
        elif mt:
            r['episode'] = _to_int(mt.group(1))
            if mt.group(2): r['episode_count'] = int(mt.group(2))
            t = TEIL.sub('', t)
        elif md and int(md.group(1)) <= int(md.group(2)) <= 20:   # "Titel n/m - Untertitel"
            r['episode'] = int(md.group(1)); r['episode_count'] = int(md.group(2))
            t = t[:md.start()] + t[md.end():]
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
            cat, gset = LABEL_MAP.get(m.group(1), (m.group(1), ()))
            r['category'] = cat; genres.update(gset); kat_src = 'metazeile'
            r['country'] = country; r['year'] = r['year'] or int(m.group(3))
            if sender in TITLE_META_SENDERS:   # strip "- Spielfilm, ... YEAR" from title
                t = t[:m.start()].rstrip(' -–')

    # -- Pass 4: series_name via topic routing ----------------------------
    if sender not in ARTE_LANG:
        routed = route_topic(tp)
        r['series_name'] = routed['series_name']
        genres.update(routed['genre']); r['slot'] = routed['slot']
        if routed['category'] and not r['category']:   # metazeile (Pass 3) wins
            r['category'] = routed['category']; kat_src = routed['kat_src']
        t = PIPESUF.sub('', t)                 # drop " | Reihe" suffix from title
        if r['series_name']:                   # ORF "<Series> Staffel N" -> split off season
            ssn = STAFFEL_SERIES.search(r['series_name'])
            if ssn:
                if r['season'] is None: r['season'] = int(ssn.group(1))
                r['series_name'] = r['series_name'][:ssn.start()].rstrip(' -–:|,·') or None

    # -- Pass 5: ARTE taxonomy (Ober=genre, Unter=medium) / duration prior -
    if sender in ARTE_LANG and ' - ' in tp:
        ober, _, unter = tp.partition(' - ')
        if ober.strip() in ARTE_OBER:
            ocat, gset = ARTE_OBER[ober.strip()]
            cat = ARTE_SUB.get(unter.strip()) or ocat
            if cat and not r['category']: r['category'] = cat
            genres.update(gset); kat_src = 'arte-topic'
    if not r['category'] and kat_src != 'arte-topic':   # honest low-conf prior
        s = duration or 0
        r['category'] = 'Clip' if s < 120 else 'Episode' if s < 1800 else None
        kat_src = 'duration-prior'

    # Episodic markers, by kind. A Mehrteiler "(n/m)" count is a serialized multi-
    # part work (Mehrteiler / miniseries) -> Episode, overriding even a
    # "Fernsehfilm" label. An explicit Sxx/Exx is series numbering that fills an
    # unknown/clip medium with Episode but does NOT override a Movie label: a
    # feature-length film-reihe entry (Krimi-Reihe, Herzkino) keeps category Movie
    # with its series_name, so enrich stays internally consistent where TMDB is
    # not (Sarah Kohr = series, Rosamunde Pilcher = movies); match bridges the
    # split later. Trailers (T) and live Events are untouched.
    if 'T' not in flags and r['category'] != 'Event':
        if r['episode_count'] is not None:
            if r['category'] in (None, 'Movie', 'Clip'):
                r['category'] = 'Episode'; kat_src = 'mehrteiler'
        elif r['season'] is not None and r['episode'] is not None:
            if r['category'] in (None, 'Clip'):
                r['category'] = 'Episode'; kat_src = 'episodic'

    if r['category'] is None and tp.casefold() in fiction_topics:   # known fiction Reihe
        r['category'] = 'Movie'; kat_src = 'topic-fiction'

    if 'T' in flags and r['category'] in ('Movie', 'Episode') and (duration or 0) < 300:
        r['category'] = 'Clip'; kat_src = 'trailer'   # a short trailer is a clip, not a film/episode

    if (duration or 0) < 900:                          # short companion piece about a work
        if MAKINGOF.search(title or ''):      flags.add('M')
        elif INTERVIEW.search(title or ''):   flags.add('I')
        if flags & {'M', 'I'} and r['category'] in ('Movie', 'Episode'):
            r['category'] = 'Clip'; kat_src = 'companion'

    cm = CREDIT.search(t)                                 # trailing "- Film von <Name>" (B4)
    if cm and not re.search(r'(?:19|20)\d\d', cm.group(0)):
        t = t[:cm.start()]

    my = re.search(r'\s*\((?:19|20)\d\d\)\s*$', t)       # trailing "(YYYY)" disambiguation
    if my:
        if not r['year']: r['year'] = int(my.group(0).strip('() '))
        t = t[:my.start()]
    r['clean_title'] = re.sub(r'\s{2,}', ' ', t).strip(' -–|:,·') or None

    r['genre'] = _genre_str(genres)            # TMDB genres, canonical order
    r['flags'] = ''.join(sorted(flags))        # canonical alphabetical order (A<S<T<U)
    r['enrich_confidence'] = _confidence(kat_src, r['category'])
    return r
