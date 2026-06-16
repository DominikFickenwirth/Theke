# -- analysis helpers -------------------------------------------------------
# Scratch tooling for phase-3 reverse engineering. NOT production code.
import sqlite3, sys, io

# force UTF-8 stdout so broadcaster metadata prints readably on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DB = 'build/theke.db'

def conn():
    return sqlite3.connect(DB)

def senders(c, min_n=1000):
    return [r[0] for r in c.execute(
        'SELECT sender, COUNT(*) n FROM mediathek GROUP BY sender '
        'HAVING n>=? ORDER BY n DESC', (min_n,))]
