"""Differential tests: the engine against brute force.

Each test builds the same fleet in a Session and in an Oracle, runs the same
query against both, and asserts the answers match exactly. The randomized
sweep at the bottom is the one that actually finds things, because it composes
filters, includes, orders and limits in combinations nobody would write by
hand, and it runs against both entity backends so a bug that only shows up
under SQL pushdown cannot hide.
"""

import random

from keelson.model import load
from keelson.oracle import Oracle
from keelson.session import open_session

from .fixtures import fleet_source
from .runner import bump, eq, ok

MODEL = load(fleet_source())

FILTERS = [
    None,
    'status == "ACTIVE"',
    'status in ["ACTIVE", "DERATED"]',
    "ratedPowerKw > 3400",
    "ratedPowerKw >= 3000 and hubHeightM < 110",
    'serial like "SN-0000%"',
    'not (status == "OFFLINE")',
    'status == "ACTIVE" or ratedPowerKw > 4000',
    'site.region == "north"',
    'site.region == "north" and status == "ACTIVE"',
    'status == "ACTIVE" and site.region in ["north", "coastal"]',
    "commissionedOn > 1500000000",
]

INCLUDES = [None, ["site"], ["sensors"], ["site", "sensors"]]
ORDERS = [None, ["id"], ["-ratedPowerKw", "id"], ["status", "-hubHeightM", "id"]]


def build(backend="relational", seed=3):
    from tools.generate import generate

    ses = open_session(MODEL, backend=backend, segment_points=128)
    oracle = Oracle(MODEL)
    fleet = generate(seed=seed, n_sites=4, turbines_per_site=6, points_per_series=300, work_orders_per_site=12)
    fleet.load_into(ses, oracle)
    return ses, oracle, fleet


def strip(rows):
    """Compare only ids at each level, so dict ordering cannot matter."""
    out = []
    for r in rows:
        rec = {"id": r["id"]}
        if isinstance(r.get("site"), dict):
            rec["site"] = r["site"]["id"]
        elif "site" in r:
            rec["site"] = None
        if isinstance(r.get("sensors"), list):
            rec["sensors"] = sorted(s["id"] for s in r["sensors"])
        out.append(rec)
    return out


def test_generated_fleet_has_the_expected_shape():
    _ses, _oracle, fleet = build()
    c = fleet.counts()
    eq(c["sites"], 4)
    eq(c["turbines"], 24)
    eq(c["sensors"], 96)
    eq(c["work_orders"], 48)
    eq(c["points"], 96 * 300)


def test_plain_fetch_matches_the_oracle():
    ses, oracle, _ = build()
    eq(
        strip(ses.type("Turbine").fetch(order=["id"])),
        strip(oracle.fetch("Turbine", order=["id"])),
    )


def test_every_filter_matches_the_oracle():
    ses, oracle, _ = build()
    for f in FILTERS:
        include = ["site"] if f and "site." in f else None
        got = ses.type("Turbine").fetch(filter=f, include=include, order=["id"])
        want = oracle.fetch("Turbine", filter=f, include=include, order=["id"])
        eq([r["id"] for r in got], [r["id"] for r in want], "filter %r" % f)


def test_includes_match_the_oracle():
    ses, oracle, _ = build()
    for inc in INCLUDES:
        got = ses.type("Turbine").fetch(include=inc, order=["id"])
        want = oracle.fetch("Turbine", include=inc, order=["id"])
        eq(strip(got), strip(want), "include %r" % (inc,))


def test_nested_include_matches_the_oracle():
    ses, oracle, _ = build()
    got = ses.type("Site").fetch(include=["assets"], order=["id"])
    want = oracle.fetch("Site", include=["assets"], order=["id"])
    eq(
        [sorted(a["id"] for a in r["assets"]) for r in got],
        [sorted(a["id"] for a in r["assets"]) for r in want],
    )


def test_orders_match_the_oracle():
    ses, oracle, _ = build()
    for o in ORDERS:
        got = ses.type("Turbine").fetch(order=o)
        want = oracle.fetch("Turbine", order=o)
        eq([r["id"] for r in got], [r["id"] for r in want], "order %r" % (o,))


def test_limit_and_offset_match_the_oracle():
    ses, oracle, _ = build()
    for limit, offset in [(1, 0), (5, 0), (5, 5), (100, 20), (0, 0)]:
        got = ses.type("Turbine").fetch(order=["id"], limit=limit, offset=offset)
        want = oracle.fetch("Turbine", order=["id"], limit=limit, offset=offset)
        eq([r["id"] for r in got], [r["id"] for r in want], "limit %d offset %d" % (limit, offset))


def test_count_matches_the_oracle():
    ses, oracle, _ = build()
    for f in [f for f in FILTERS if not f or "site." not in f]:
        eq(ses.type("Turbine").count(f), oracle.count("Turbine", f), "count %r" % f)


def test_evaluate_matches_the_oracle():
    ses, oracle, _ = build()
    specs = [
        (["siteId"], {"n": "count()", "kw": "sum(ratedPowerKw)"}),
        (["status"], {"n": "count()", "avgKw": "avg(ratedPowerKw)"}),
        (["siteId", "status"], {"n": "count()", "hi": "max(hubHeightM)", "lo": "min(hubHeightM)"}),
        ([], {"n": "count()", "models": "countDistinct(model)"}),
    ]
    for group, metrics in specs:
        got = ses.type("Turbine").evaluate(group=group, metrics=metrics, order=group or None)
        want = oracle.evaluate("Turbine", group=group, metrics=metrics, order=group or None)
        eq(got, want, "evaluate group=%r" % (group,))


def test_timeseries_ranges_match_the_oracle():
    ses, oracle, fleet = build()
    sensor_ids = sorted({sid for _f, sid in fleet.series})[:6]
    for sid in sensor_ids:
        eq(
            ses.type("Sensor").series("readings", sid),
            oracle.series_range("Sensor", "readings", sid),
            "full range for %s" % sid,
        )
    base = 1_735_689_600
    for start, end in [(base, base + 6000), (base + 100000, base + 200000), (0, base)]:
        for sid in sensor_ids[:3]:
            eq(
                ses.type("Sensor").series("readings", sid, start, end),
                oracle.series_range("Sensor", "readings", sid, start, end),
                "window %d..%d" % (start, end),
            )


def test_timeseries_include_matches_the_oracle():
    ses, oracle, _ = build()
    window = (1_735_689_600, 1_735_689_600 + 30000)
    got = ses.type("Sensor").fetch(include=["readings"], order=["id"], limit=10, window=window)
    want = oracle.fetch("Sensor", include=["readings"], order=["id"], limit=10, window=window)
    eq([len(r["readings"]) for r in got], [len(r["readings"]) for r in want])
    eq(got[0]["readings"], want[0]["readings"])


def test_randomized_query_sweep_on_both_backends():
    rnd = random.Random(20260902)
    for backend in ("relational", "memory"):
        ses, oracle, _ = build(backend=backend)
        for trial in range(160):
            f = rnd.choice(FILTERS)
            inc = rnd.choice(INCLUDES)
            if f and "site." in f:
                inc = list(set((inc or []) + ["site"]))
            order = rnd.choice(ORDERS)
            limit = rnd.choice([None, 1, 3, 10, 50])
            offset = rnd.choice([0, 0, 2, 7]) if limit else 0
            got = ses.type("Turbine").fetch(
                filter=f, include=inc, order=order, limit=limit, offset=offset
            )
            want = oracle.fetch(
                "Turbine", filter=f, include=inc, order=order, limit=limit, offset=offset
            )
            if order is None:
                eq(
                    sorted(r["id"] for r in got),
                    sorted(r["id"] for r in want),
                    "%s trial %d: %r %r" % (backend, trial, f, inc),
                )
            else:
                eq(
                    strip(got),
                    strip(want),
                    "%s trial %d: filter=%r include=%r order=%r limit=%r offset=%r"
                    % (backend, trial, f, inc, order, limit, offset),
                )
            bump()


def test_write_paths_agree_after_upsert_and_merge():
    ses, oracle, _ = build()
    edits = [
        {"id": "wtg-000-000", "status": "MAINTENANCE"},
        {"id": "wtg-001-002", "ratedPowerKw": 9999.0},
    ]
    for e in edits:
        full = ses.type("Turbine").get(e["id"])
        merged = {k: v for k, v in full.items() if not isinstance(v, (dict, list))}
        merged.update(e)
        ses.type("Turbine").upsert([merged])
        oracle.load("Turbine", [merged])
    eq(
        strip(ses.type("Turbine").fetch(order=["id"])),
        strip(oracle.fetch("Turbine", order=["id"])),
    )
    eq(
        ses.type("Turbine").count('status == "MAINTENANCE"'),
        oracle.count("Turbine", 'status == "MAINTENANCE"'),
    )


def test_the_oracle_actually_disagrees_when_the_engine_is_wrong():
    """A negative control: if the oracle can never fail, it proves nothing."""
    ses, oracle, _ = build()
    oracle.load("Turbine", [{"id": "phantom", "siteId": "site-000", "serial": "X", "status": "ACTIVE", "version": 1}])
    got = [r["id"] for r in ses.type("Turbine").fetch(order=["id"])]
    want = [r["id"] for r in oracle.fetch("Turbine", order=["id"])]
    ok(got != want, "the oracle should have noticed the extra row")
    eq(len(want) - len(got), 1)
