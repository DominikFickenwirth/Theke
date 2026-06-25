"""Re-enrich every mediathek row with the CURRENT enrich code and dump the
computed metadata into a scratch SQLite (analysis/_enr.db, table `enr`), so
misclassifications can be probed with plain SQL across the whole live DB.

Run from repo root:  .venv/Scripts/python.exe analysis/_reenrich.py
Re-run after each enrich change; it rebuilds the scratch table from scratch.
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from theke.enrich import enrich, ENRICH_COLS

SRC = "build/theke.db"
DST = "analysis/_enr.db"

RAW = ["mediathek_id", "sender", "topic", "title", "description", "duration", "url_website"]
OUT = ["category", "series_name", "slot", "season", "episode", "episode_count",
       "clean_title", "year", "country", "language", "genre", "flags",
       "enrich_confidence"]


def main():
    t0 = time.time()
    src = sqlite3.connect(SRC); src.row_factory = sqlite3.Row
    if os.path.exists(DST):
        os.remove(DST)
    dst = sqlite3.connect(DST)
    cols = RAW + OUT
    dst.execute(f"CREATE TABLE enr ({', '.join(c + ' TEXT' for c in cols)})")
    ins = f"INSERT INTO enr ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"

    n = 0
    batch = []
    for r in src.execute(f"SELECT {','.join(RAW)} FROM mediathek"):
        e = enrich(r["sender"], r["topic"], r["title"], r["description"], r["duration"])
        batch.append([r[c] for c in RAW] + [e[c] for c in OUT])
        n += 1
        if len(batch) >= 5000:
            dst.executemany(ins, batch); batch = []
    if batch:
        dst.executemany(ins, batch)
    dst.commit()
    print(f"re-enriched {n} rows into {DST} in {time.time() - t0:.1f}s")
    print("category dist:", dict(dst.execute(
        "SELECT category, COUNT(*) FROM enr GROUP BY category ORDER BY 2 DESC").fetchall()))


if __name__ == "__main__":
    main()
