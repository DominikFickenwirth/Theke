# -- topic cardinality + duration profile per sender ------------------------
from lib import conn, senders

def run():
    c = conn().cursor()
    sl = senders(c, 1000)
    print('SENDER          rows  distinct_topics  reuse  top1_topic(share)            med_dur_min')
    for s in sl:
        n = c.execute('SELECT COUNT(*) FROM mediathek WHERE sender=?', (s,)).fetchone()[0]
        dt = c.execute('SELECT COUNT(DISTINCT topic) FROM mediathek WHERE sender=?', (s,)).fetchone()[0]
        top = c.execute('SELECT topic, COUNT(*) k FROM mediathek WHERE sender=? GROUP BY topic ORDER BY k DESC LIMIT 1', (s,)).fetchone()
        # median duration (seconds) -> minutes
        durs = [r[0] for r in c.execute('SELECT duration FROM mediathek WHERE sender=? AND duration IS NOT NULL ORDER BY duration', (s,))]
        med = durs[len(durs)//2]//60 if durs else 0
        reuse = n/dt if dt else 0
        topshare = 100*top[1]/n
        print(f'{s:14} {n:>6} {dt:>9} {reuse:>9.1f}x  {top[0][:24]:24}({topshare:4.1f}%) {med:>10}')

    # duration buckets overall
    print('\n=== duration buckets (all senders) ===')
    buckets = [(0,2),(2,5),(5,15),(15,30),(30,50),(50,75),(75,95),(95,140),(140,9999)]
    for lo,hi in buckets:
        k = c.execute('SELECT COUNT(*) FROM mediathek WHERE duration>=? AND duration<?', (lo*60,hi*60)).fetchone()[0]
        print(f'  {lo:>3}-{hi:<4} min: {k:>8}')

if __name__ == '__main__':
    run()
