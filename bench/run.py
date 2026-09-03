"""Benchmarks. Every number in README.md and RESULTS.md comes from here.

Run with `python bench/run.py`. Results are written to results/bench.json so
the documentation can be checked against the file rather than against memory.

The comparisons are deliberately like for like. The pushdown benchmark runs
the identical query through the identical planner twice, changing only which
entity store is underneath, so the difference is the pushdown and not a
different code path. The join benchmark compares the batched planner against a
loop that issues one lookup per parent row, which is what an object mapper
without a batching layer does.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from keelson.model import load
from keelson.session import open_session
from keelson.stores.tsdb import Series
from tools.generate import EPOCH, generate

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(HERE, "results")


def model():
    with open(os.path.join(HERE, "models", "fleet.ks")) as fh:
        return load(fh.read(), "fleet.ks")


def timed(fn, repeat=1):
    best = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return best, out


def bench_ingest(m):
    fleet = generate(seed=1, n_sites=20, turbines_per_site=40, points_per_series=0, work_orders_per_site=200)
    ses = open_session(m)
    rows = len(fleet.sites) + len(fleet.turbines) + len(fleet.substations) + len(fleet.sensors) + len(fleet.work_orders)
    dt, _ = timed(lambda: fleet.load_into(ses))
    return {
        "entity_rows": rows,
        "seconds": dt,
        "rows_per_sec": rows / dt,
        "counts": fleet.counts(),
    }, ses, fleet


def bench_series_ingest(m):
    fleet = generate(seed=2, n_sites=4, turbines_per_site=10, points_per_series=2000)
    ses = open_session(m, segment_points=512)
    ses.type("Site").upsert(fleet.sites)
    ses.type("Turbine").upsert(fleet.turbines)
    ses.type("Sensor").upsert(fleet.sensors)

    def load_points():
        for (field, sid), pts in fleet.series.items():
            ses.type("Sensor").append_series(field, sid, pts)
        ses.flush()

    dt, _ = timed(load_points)
    stats = ses.timeseries.stats()
    return {
        "points": stats["points"],
        "series": stats["series"],
        "seconds": dt,
        "points_per_sec": stats["points"] / dt,
        "encoded_bytes": stats["encoded_bytes"],
        "raw_bytes": stats["raw_bytes"],
        "bytes_per_point": stats["bytes_per_point"],
        "compression_ratio": stats["compression_ratio"],
    }, ses, fleet


def bench_pushdown(m):
    """Same planner, same query, two entity stores: SQL pushdown vs full scan."""
    fleet = generate(seed=3, n_sites=20, turbines_per_site=40, points_per_series=0, work_orders_per_site=0)
    query = dict(filter='status == "ACTIVE" and ratedPowerKw > 3400', order=["-ratedPowerKw"], limit=20)

    out = {}
    for backend in ("relational", "memory"):
        ses = open_session(m, backend=backend)
        fleet.load_into(ses)
        ses.reset_counters()
        dt, rows = timed(lambda: ses.type("Turbine").fetch(**query), repeat=5)
        out[backend] = {
            "seconds": dt,
            "rows_returned": len(rows),
            "rows_scanned": ses.store.rows_scanned,
            "store_queries": ses.store.queries,
            "pushed_filter": ses.last_stats.pushed_filter is not None,
            "pushed_order": ses.last_stats.pushed_order,
            "pushed_limit": ses.last_stats.pushed_limit,
        }

    total = len(fleet.turbines)
    out["turbines_in_fleet"] = total
    out["scan_reduction"] = out["memory"]["rows_scanned"] / max(1, out["relational"]["rows_scanned"])
    out["speedup"] = out["memory"]["seconds"] / out["relational"]["seconds"]
    return out


def bench_join(m):
    """Batched IN joins against the per parent lookup an unbatched mapper does."""
    fleet = generate(seed=4, n_sites=25, turbines_per_site=40, points_per_series=0, work_orders_per_site=0)
    ses = open_session(m)
    fleet.load_into(ses)
    turbine = ses.model["Turbine"]
    site = ses.model["Site"]

    ses.reset_counters()
    dt_batched, rows = timed(lambda: ses.type("Turbine").fetch(include=["site"]), repeat=3)
    batched_queries = ses.store.queries // 3

    def naive():
        roots = ses.store.fetch(turbine)
        for r in roots:
            ses.store.fetch(site, '"id" = ?', (r["siteId"],))
        return roots

    ses.reset_counters()
    dt_naive, naive_rows = timed(naive, repeat=1)
    naive_queries = ses.store.queries

    return {
        "parents": len(fleet.turbines),
        "batched_seconds": dt_batched,
        "batched_queries": batched_queries,
        "naive_seconds": dt_naive,
        "naive_queries": naive_queries,
        "query_reduction": naive_queries / max(1, batched_queries),
        "speedup": dt_naive / dt_batched,
        "rows_match": len(rows) == len(naive_rows),
    }


def bench_segment_skipping(ses, fleet):
    """A narrow window should decode a handful of segments, not the series."""
    sensor_ids = sorted({sid for _f, sid in fleet.series})[:20]
    total_points = sum(len(fleet.series[("readings", sid)]) for sid in sensor_ids)

    ses.timeseries.reset_counters()
    dt_full, _ = timed(lambda: [ses.type("Sensor").series("readings", s) for s in sensor_ids])
    full = {
        "seconds": dt_full,
        "segments_scanned": ses.timeseries.segments_scanned,
        "points_decoded": ses.timeseries.points_decoded,
    }

    start = EPOCH + 600 * 1000
    end = start + 600 * 100
    ses.timeseries.reset_counters()
    dt_win, windows = timed(
        lambda: [ses.type("Sensor").series("readings", s, start, end) for s in sensor_ids]
    )
    windowed = {
        "seconds": dt_win,
        "segments_scanned": ses.timeseries.segments_scanned,
        "points_decoded": ses.timeseries.points_decoded,
        "points_returned": sum(len(w) for w in windows),
    }

    return {
        "sensors": len(sensor_ids),
        "points_in_scope": total_points,
        "full_scan": full,
        "windowed": windowed,
        "segment_reduction": full["segments_scanned"] / max(1, windowed["segments_scanned"]),
        "decode_reduction": full["points_decoded"] / max(1, windowed["points_decoded"]),
    }


def bench_codec_shapes():
    """Compression is a property of the signal, so report several shapes."""
    import math
    import random

    rnd = random.Random(9)
    shapes = {}

    def measure(name, points):
        s = Series((name, "readings", "x"), segment_points=512)
        for ts, v in points:
            s.append(ts, v)
        s.flush()
        shapes[name] = {
            "points": len(points),
            "encoded_bytes": s.encoded_bytes(),
            "raw_bytes": len(points) * 16,
            "bytes_per_point": s.encoded_bytes() / len(points),
            "compression_ratio": (len(points) * 16) / s.encoded_bytes(),
        }

    n = 8192
    measure("constant", [(i * 600, 42.0) for i in range(n)])
    measure("stepwise", [(i * 600, float(40 + (i // 500))) for i in range(n)])

    v = 300.0
    walk = []
    for i in range(n):
        v += rnd.uniform(-0.2, 0.2)
        walk.append((i * 600, round(v, 3)))
    measure("slow_walk", walk)

    measure(
        "diurnal",
        [(i * 600, round(50 + 8 * math.sin(2 * math.pi * (i * 600 % 86400) / 86400), 3)) for i in range(n)],
    )
    measure("white_noise", [(i * 600, rnd.uniform(-1e6, 1e6)) for i in range(n)])

    jitter = []
    t = 0
    for i in range(n):
        t += 600 + rnd.randint(-3, 3)
        jitter.append((t, round(300 + rnd.uniform(-1, 1), 3)))
    measure("jittered_cadence", jitter)
    return shapes


def bench_query_throughput(m):
    fleet = generate(seed=6, n_sites=10, turbines_per_site=40, points_per_series=0, work_orders_per_site=100)
    ses = open_session(m)
    fleet.load_into(ses)
    queries = [
        lambda: ses.type("Turbine").fetch(filter='status == "ACTIVE"', limit=50),
        lambda: ses.type("Turbine").fetch(filter="ratedPowerKw > 3400", order=["-ratedPowerKw"], limit=20),
        lambda: ses.type("Turbine").evaluate(group=["siteId"], metrics={"n": "count()", "kw": "avg(ratedPowerKw)"}),
        lambda: ses.type("WorkOrder").evaluate(group=["category"], metrics={"n": "count()", "d": "sum(downtimeMinutes)"}),
        lambda: ses.type("Site").fetch(include=["assets"]),
    ]
    out = {}
    for i, q in enumerate(queries):
        dt, rows = timed(q, repeat=5)
        out["q%d" % (i + 1)] = {"seconds": dt, "rows": len(rows), "qps": 1.0 / dt}
    return out


def main():
    m = model()
    report = {}

    ingest, _ses, _fleet = bench_ingest(m)
    report["entity_ingest"] = ingest

    series_ingest, ses_ts, fleet_ts = bench_series_ingest(m)
    report["series_ingest"] = series_ingest

    report["pushdown"] = bench_pushdown(m)
    report["joins"] = bench_join(m)
    report["segment_skipping"] = bench_segment_skipping(ses_ts, fleet_ts)
    report["codec_shapes"] = bench_codec_shapes()
    report["query_throughput"] = bench_query_throughput(m)

    if not os.path.isdir(RESULTS):
        os.makedirs(RESULTS)
    with open(os.path.join(RESULTS, "bench.json"), "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)

    _print(report)
    return 0


def _print(r):
    print("entity ingest")
    print(
        "  %d rows in %.3fs  (%.0f rows/sec)"
        % (r["entity_ingest"]["entity_rows"], r["entity_ingest"]["seconds"], r["entity_ingest"]["rows_per_sec"])
    )

    si = r["series_ingest"]
    print("series ingest")
    print("  %d points across %d series in %.3fs (%.0f points/sec)" % (si["points"], si["series"], si["seconds"], si["points_per_sec"]))
    print("  %.2f bytes/point encoded vs 16 raw  (%.2fx)" % (si["bytes_per_point"], si["compression_ratio"]))

    p = r["pushdown"]
    print("filter pushdown  (%d turbines)" % p["turbines_in_fleet"])
    print("  sql   : %6d rows scanned, %.4fs" % (p["relational"]["rows_scanned"], p["relational"]["seconds"]))
    print("  scan  : %6d rows scanned, %.4fs" % (p["memory"]["rows_scanned"], p["memory"]["seconds"]))
    print("  %.1fx fewer rows scanned, %.1fx faster" % (p["scan_reduction"], p["speedup"]))

    j = r["joins"]
    print("join batching  (%d parents)" % j["parents"])
    print("  batched: %3d queries, %.4fs" % (j["batched_queries"], j["batched_seconds"]))
    print("  per row: %3d queries, %.4fs" % (j["naive_queries"], j["naive_seconds"]))
    print("  %.0fx fewer queries, %.1fx faster" % (j["query_reduction"], j["speedup"]))

    s = r["segment_skipping"]
    print("segment skipping  (%d sensors, %d points)" % (s["sensors"], s["points_in_scope"]))
    print("  full   : %d segments, %d points decoded, %.4fs" % (s["full_scan"]["segments_scanned"], s["full_scan"]["points_decoded"], s["full_scan"]["seconds"]))
    print("  window : %d segments, %d points decoded, %.4fs" % (s["windowed"]["segments_scanned"], s["windowed"]["points_decoded"], s["windowed"]["seconds"]))
    print("  %.1fx fewer segments, %.1fx fewer points decoded" % (s["segment_reduction"], s["decode_reduction"]))

    print("codec by signal shape (8192 points each)")
    for name, c in sorted(r["codec_shapes"].items(), key=lambda kv: kv[1]["bytes_per_point"]):
        print("  %-18s %5.2f bytes/point  %5.2fx" % (name, c["bytes_per_point"], c["compression_ratio"]))

    print("query latency")
    for name, q in sorted(r["query_throughput"].items()):
        print("  %-4s %8.3f ms  (%d rows)" % (name, q["seconds"] * 1000, q["rows"]))


if __name__ == "__main__":
    sys.exit(main())
