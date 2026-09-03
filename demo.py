"""End to end walkthrough against the wind fleet model.

Run with `python demo.py`. It compiles models/fleet.ks, shows the DDL that
falls out of it, loads a generated fleet, then runs the query shapes the
engine exists for. Nothing here is a mock: every number printed comes from the
code in keelson/.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from keelson import migrate
from keelson.model import load
from keelson.session import open_session
from keelson.stores.relational import RelationalStore
from tools.generate import EPOCH, generate

HERE = os.path.dirname(os.path.abspath(__file__))


def rule(title):
    print("")
    print("=" * 72)
    print(title)
    print("=" * 72)


def main():
    with open(os.path.join(HERE, "models", "fleet.ks")) as fh:
        source = fh.read()

    rule("1. compile the model")
    model = load(source, "fleet.ks")
    print("types: %s" % ", ".join(model.names()))
    turbine = model["Turbine"]
    print("")
    print("Turbine flattens to %d fields across an extends chain %s and mixins %s" % (
        len(turbine.fields), turbine.bases or ["(none)"], turbine.mixins or ["(none)"]))
    for f in turbine.fields.values():
        print("  %-16s %-14s %-10s from %-12s -> %s" % (
            f.name, f.type_name, f.kind, f.declared_on, f.store))

    rule("2. the same model, rendered as relational schema")
    store = RelationalStore(model)
    for stmt in store.ddl_for(turbine):
        print(stmt)
    print("")
    print("Note WTG_MASTER / RATED_KW: the model maps onto a legacy naming")
    print("scheme, and nothing above the store ever sees those names.")

    rule("3. load a generated fleet")
    ses = open_session(model, segment_points=512)
    fleet = generate(seed=11, n_sites=6, turbines_per_site=12, points_per_series=1500)
    fleet.load_into(ses)
    counts = fleet.counts()
    print("loaded %d sites, %d turbines, %d substations, %d sensors, %d work orders"
          % (counts["sites"], counts["turbines"], counts["substations"],
             counts["sensors"], counts["work_orders"]))
    ts = ses.timeseries.stats()
    print("time series: %d points across %d series, %.2f bytes/point (%.2fx vs raw)"
          % (ts["points"], ts["series"], ts["bytes_per_point"], ts["compression_ratio"]))

    rule("4. a filter that is entirely local: pushed into SQL")
    rows = ses.type("Turbine").fetch(
        filter='status == "ACTIVE" and ratedPowerKw > 3400',
        order=["-ratedPowerKw", "id"],
        limit=5,
    )
    st = ses.last_stats
    print("pushed to SQL : %s" % (st.pushed_filter is not None))
    print("residual      : %s" % (st.residual_filter is not None))
    print("order pushed  : %s   limit pushed: %s" % (st.pushed_order, st.pushed_limit))
    print("rows read from the store: %d" % st.root_rows)
    for r in rows:
        print("  %-16s %-10s %7.0f kW" % (r["id"], r["status"], r["ratedPowerKw"]))

    rule("5. a filter that crosses a join: split, not refused")
    rows = ses.type("Turbine").fetch(
        filter='status == "ACTIVE" and site.region == "north"',
        include=["site"],
        order=["id"],
        limit=5,
    )
    st = ses.last_stats
    print("pushed   : %s" % st.pushed_filter)
    print("residual : %s" % st.residual_filter)
    print("the pushdown cut the root set to %d rows before the join ran" % st.root_rows)
    print("limit was applied after the residual (%s), which is the only correct order"
          % (not st.pushed_limit))
    for r in rows:
        print("  %-16s site=%-10s region=%s" % (r["id"], r["site"]["id"], r["site"]["region"]))

    rule("6. joins are batched, not one query per row")
    ses.reset_counters()
    sites = ses.type("Site").fetch(include=["assets"], order=["id"])
    print("fetched %d sites and %d assets in %d store queries"
          % (len(sites), sum(len(s["assets"]) for s in sites), ses.store.queries))
    print("`assets` targets the abstract Asset type, so the planner fanned out")
    print("over its concrete subtypes and unioned the result:")
    kinds = {}
    for a in sites[0]["assets"]:
        kinds[("capacityMva" in a) and "Substation" or "Turbine"] = \
            kinds.get(("capacityMva" in a) and "Substation" or "Turbine", 0) + 1
    print("  site-000 owns %s" % ", ".join("%d %s" % (v, k) for k, v in sorted(kinds.items())))

    rule("7. grouped aggregation, pushed into SQL")
    for row in ses.type("Turbine").evaluate(
        group=["siteId"],
        metrics={"n": "count()", "avgKw": "avg(ratedPowerKw)", "models": "countDistinct(model)"},
        order=["siteId"],
    ):
        print("  %-10s %2d turbines  avg %7.1f kW  %d models"
              % (row["siteId"], row["n"], row["avgKw"], row["models"]))

    print("")
    for row in ses.type("WorkOrder").evaluate(
        group=["category"],
        metrics={"n": "count()", "downtime": "sum(downtimeMinutes)"},
        order=["-downtime"],
    ):
        print("  %-12s %3d orders  %6d minutes of downtime"
              % (row["category"], row["n"], row["downtime"]))

    rule("8. a time window only decodes the segments it overlaps")
    sensor = sorted({sid for _f, sid in fleet.series})[0]
    ses.timeseries.reset_counters()
    full = ses.type("Sensor").series("readings", sensor)
    print("full series : %d points, %d segments touched"
          % (len(full), ses.timeseries.segments_scanned))
    start = EPOCH + 600 * 400
    ses.timeseries.reset_counters()
    window = ses.type("Sensor").series("readings", sensor, start, start + 600 * 50)
    print("one window  : %d points, %d segments touched"
          % (len(window), ses.timeseries.segments_scanned))
    print("first three points: %s" % (window[:3],))

    rule("9. the same script, a different entity store")
    def script(s):
        s.type("Site").upsert(fleet.sites)
        s.type("Turbine").upsert(fleet.turbines)
        out = s.type("Turbine").fetch(
            filter='ratedPowerKw > 3400 and site.region == "north"',
            include=["site"],
            order=["-ratedPowerKw", "id"],
            limit=5,
        )
        return [(r["id"], round(r["ratedPowerKw"])) for r in out]

    a = script(open_session(model))
    b = script(open_session(model, backend="memory"))
    print("relational: %s" % (a,))
    print("memory    : %s" % (b,))
    print("identical : %s" % (a == b))

    rule("10. changing the model is a planned migration, not a guess")
    changed = load(source.replace(
        "  rotorDiameterM: double",
        "  rotorDiameterM: double\n  gridCode: string = \"UNSET\"",
    ), "fleet.ks")
    plan = migrate.diff(model, changed)
    for change in plan:
        print("  %s" % change)
    migrate.apply(ses, changed)
    print("applied. WTG_MASTER now has: %s" % ", ".join(ses.store.table_columns("WTG_MASTER")))
    print("existing rows survived: %d turbines, gridCode defaulted to %r"
          % (ses.type("Turbine").count(), ses.type("Turbine").fetch(limit=1)[0]["gridCode"]))

    destructive = load(source.replace("  hubHeightM: double schema name \"HUB_HT_M\"\n", ""), "fleet.ks")
    bad = migrate.diff(changed, destructive)
    print("")
    print("a model edit that drops a column is refused by default:")
    for change in bad.unsafe():
        print("  %s" % change.detail)

    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
