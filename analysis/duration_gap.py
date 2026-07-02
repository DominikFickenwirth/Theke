# -- duration gap: TMDB runtime vs mediathek duration -----------------------
# Scratch tooling (phase 3/15): for every confirmed match (status '3') in
# build/theke.db, fetch the TMDB runtime once per distinct tmdb_id and tabulate
# it against the mediathek duration. The question this answers: how tight a SQL
# duration prefilter could be without dropping true matches (cf. the year gate).
#
# CAVEAT the summary must be read with: bulk_match already gated its hits on
# RUNTIME_TOLERANCE (0.15), so the status-'3' sample is self-selected toward a
# small gap. The observed spread is a LOWER bound on the true spread a prefilter
# would have to survive -- treat the tails as "at least this wide".
#
# NOT production code. Run from the repo root:  python analysis/duration_gap.py
import csv
import io
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import sqlite3

from theke import core, match

DB          = "build/theke.db"
CONFIG      = "build/theke.json"
CSV_OUT     = "analysis/duration_gap.csv"
SUMMARY_OUT = "analysis/duration_gap_summary.txt"

MIN_BANDS = [1, 2, 3, 5, 10, 15, 20, 30]        # +/- N minutes
REL_BANDS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.50]  # +/- share of runtime


def fetch_runtimes(cfg, tmdb_ids):
    """Runtime (minutes) per distinct tmdb_id via one TMDB call each; None when
    TMDB reports no/zero runtime. Prints progress to stderr."""
    out = {}
    total = len(tmdb_ids)
    for i, tid in enumerate(sorted(tmdb_ids), 1):
        try:
            out[tid] = match.tmdb_movie(cfg, tid).get("runtime")
        except Exception as exc:            # a dead id must not abort the sweep
            out[tid] = None
            print(f"  !! {tid}: {exc}", file=sys.stderr)
        if i % 100 == 0 or i == total:
            print(f"  fetched {i}/{total} tmdb runtimes", file=sys.stderr)
    return out


def percentiles(values, ps):
    """Value at each requested percentile (0..100) of a sorted-on-demand list."""
    s = sorted(values)
    out = {}
    for p in ps:
        k = min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))
        out[p] = s[k]
    return out


def main():
    cfg = core.load_config(CONFIG)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    rows = [dict(r) for r in conn.execute(
        "SELECT mediathek_id, tmdb_id, sender, clean_title, year, duration, "
        "match_confidence FROM mediathek WHERE status='3' AND tmdb_id IS NOT NULL "
        "AND tmdb_id!='' AND duration IS NOT NULL")]
    tmdb_ids = {r["tmdb_id"] for r in rows}

    print(f"rows={len(rows)}  distinct_tmdb_ids={len(tmdb_ids)}", file=sys.stderr)
    start = time.perf_counter()
    runtimes = fetch_runtimes(cfg, tmdb_ids)
    print(f"fetched in {time.perf_counter() - start:.1f}s", file=sys.stderr)

    recs, no_runtime = [], 0
    for r in rows:
        rt = runtimes.get(r["tmdb_id"])
        med_min = r["duration"] / 60
        if not rt:
            no_runtime += 1
            delta = rel = None
        else:
            delta = med_min - rt
            rel = delta / rt
        recs.append({**r, "tmdb_runtime": rt, "med_min": round(med_min, 1),
                     "delta_min": None if delta is None else round(delta, 1),
                     "rel": None if rel is None else round(rel, 4)})

    with open(CSV_OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["mediathek_id", "tmdb_id", "clean_title",
            "year", "sender", "med_min", "tmdb_runtime", "delta_min", "rel",
            "match_confidence"])
        w.writeheader()
        for rec in sorted(recs, key=lambda x: (abs(x["delta_min"]) if x["delta_min"]
                                               is not None else -1), reverse=True):
            w.writerow({k: rec.get(k) for k in w.fieldnames})

    paired = [rec for rec in recs if rec["delta_min"] is not None]
    deltas = [rec["delta_min"] for rec in paired]
    abs_d  = [abs(d) for d in deltas]
    rels   = [abs(rec["rel"]) for rec in paired]

    lines = []
    lines.append("== duration gap: TMDB runtime vs mediathek duration ==")
    lines.append(f"rows with a mediathek duration : {len(rows)}")
    lines.append(f"distinct tmdb_ids fetched      : {len(tmdb_ids)}")
    lines.append(f"rows with a usable TMDB runtime: {len(paired)}")
    lines.append(f"rows with no/zero TMDB runtime : {no_runtime}")
    lines.append("")
    lines.append("NOTE: status-'3' rows were themselves runtime-gated by bulk_match")
    lines.append("(RUNTIME_TOLERANCE=0.15). This spread is a LOWER bound on reality.")
    lines.append("")
    lines.append("-- signed delta (mediathek_min - tmdb_runtime_min) --")
    lines.append(f"  mean={statistics.mean(deltas):+.2f}  median={statistics.median(deltas):+.2f}"
                 f"  min={min(deltas):+.1f}  max={max(deltas):+.1f}")
    pc = percentiles(deltas, [1, 5, 25, 50, 75, 95, 99])
    lines.append("  signed percentiles: " + "  ".join(f"p{p}={pc[p]:+.1f}" for p in
                 [1, 5, 25, 50, 75, 95, 99]))
    pa = percentiles(abs_d, [50, 75, 90, 95, 99])
    lines.append("  |delta| percentiles: " + "  ".join(f"p{p}={pa[p]:.1f}" for p in
                 [50, 75, 90, 95, 99]))
    lines.append("")
    lines.append("-- coverage: share of pairs within +/- N minutes --")
    for b in MIN_BANDS:
        n = sum(1 for d in abs_d if d <= b)
        lines.append(f"  |delta| <= {b:2d} min : {n:5d}  ({100 * n / len(paired):5.1f}%)")
    lines.append("")
    lines.append("-- coverage: share of pairs within +/- P% of runtime --")
    for b in REL_BANDS:
        n = sum(1 for x in rels if x <= b)
        lines.append(f"  |rel|   <= {int(b * 100):3d}% : {n:5d}  ({100 * n / len(paired):5.1f}%)")
    lines.append("")
    lines.append(f"CSV (per row, widest gap first): {CSV_OUT}")

    report = "\n".join(lines)
    print(report)
    with open(SUMMARY_OUT, "w", encoding="utf-8") as fh:
        fh.write(report + "\n")


if __name__ == "__main__":
    main()
