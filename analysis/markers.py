# -- marker prevalence per sender -------------------------------------------
# How often do known metadata markers appear in title / topic, per sender?
import re, collections
from lib import conn, senders

# each marker: name -> compiled regex (case-insensitive), tested on TITLE
TITLE_MARKERS = {
    'OmU/OmdU'        : re.compile(r'\bom[dn]?u\b', re.I),
    'mit Untertitel'  : re.compile(r'mit Untertitel', re.I),
    'Originalversion' : re.compile(r'Originalversion|Originalfassung|\bOV\b|\bOF\b', re.I),
    'Hoerfassung/AD'  : re.compile(r'H[oö]rfassung|Audiodeskription|\bAD\b', re.I),
    'Gebaerdensprache': re.compile(r'Geb[aä]rden|\bDGS\b', re.I),
    'Leichte/Klare Spr': re.compile(r'Leichte Sprache|Klare Sprache', re.I),
    'engl. Fassung'   : re.compile(r'englische[rn]? (Fassung|Version|Originalfassung)|english', re.I),
    'franz. Fassung'  : re.compile(r'franz[oö]sische', re.I),
    'S/E paren (S01/E02)': re.compile(r'\(S\d+/E\d+\)', re.I),
    'Staffel/Folge'   : re.compile(r'Staffel\s*\d+|Folge\s*\d+', re.I),
    'lead "NN."'      : re.compile(r'^\s*\d{1,4}\.\s'),
    'trail "(NNN)"'   : re.compile(r'\(\d{1,4}\)\s*$'),
    'pipe " | "'      : re.compile(r'\s\|\s'),
    'colon " : "'     : re.compile(r':\s'),
    'year (YYYY)'     : re.compile(r'\((19|20)\d\d\)'),
    'date dd.mm.yyyy' : re.compile(r'\b\d{1,2}\.\d{1,2}\.\d{4}\b'),
    'date "vom ..."'  : re.compile(r'\bvom\s+\d', re.I),
    'time hh:mm/uhr'  : re.compile(r'\b\d{1,2}[:.]\d{2}\s*Uhr|\bUhr\b', re.I),
}

def run():
    c = conn().cursor()
    sl = senders(c, 1000)
    # collect per-sender totals and marker hits
    stats = {s: collections.Counter() for s in sl}
    totals = {s: 0 for s in sl}
    q = 'SELECT sender, title FROM mediathek WHERE sender IN (%s)' % ','.join('?'*len(sl))
    for sender, title in c.execute(q, sl):
        totals[sender] += 1
        t = title or ''
        for name, rx in TITLE_MARKERS.items():
            if rx.search(t):
                stats[sender][name] += 1

    names = list(TITLE_MARKERS)
    # print as a matrix: rows=markers, cols=senders, value=percent
    colw = 7
    hdr = 'MARKER (% of titles)'.ljust(22)
    for s in sl:
        hdr += s[:colw].rjust(colw)
    print(hdr)
    for name in names:
        row = name.ljust(22)
        for s in sl:
            p = 100*stats[s][name]/totals[s] if totals[s] else 0
            row += (f'{p:.0f}' if p>=1 else ('.' if p==0 else '0')).rjust(colw)
        print(row)
    print()
    print('sender totals:', {s: totals[s] for s in sl})

if __name__ == '__main__':
    run()
