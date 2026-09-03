from keelson.errors import QueryError, StoreError
from keelson.model import load
from keelson.session import Session, open_session
from keelson.stores.memory import MemoryStore
from keelson.stores.relational import RelationalStore
from keelson.stores.tsdb import TimeSeriesStore

from .fixtures import SENSORS, SITES, TINY, TURBINES, fleet_source
from .runner import close, eq, ok, raises


def session(backend="relational"):
    ses = open_session(TINY, backend=backend)
    ses.type("Site").create(SITES)
    ses.type("Turbine").create(TURBINES)
    ses.type("Sensor").create(SENSORS)
    return ses


def test_open_session_accepts_source_or_model():
    ok(open_session(TINY).model["Site"] is not None)
    m = load(TINY)
    ok(open_session(m).model is m)


def test_unknown_backend_is_rejected():
    raises(ValueError, lambda: open_session(TINY, backend="mongo"), "unknown backend")


def test_session_requires_a_resolved_model():
    raises(TypeError, lambda: Session(TINY), "resolved Model")


def test_abstract_types_have_no_proxy():
    ses = open_session(fleet_source())
    raises(QueryError, lambda: ses.type("Asset"), "abstract")
    ok(ses.type("Turbine") is not None)


def test_type_proxies_are_cached():
    ses = session()
    ok(ses.type("Site") is ses.type("Site"))
    ok(ses["Site"] is ses.type("Site"))


def test_create_rejects_unknown_fields():
    ses = session()
    raises(
        QueryError,
        lambda: ses.type("Site").create([{"id": "x", "name": "n", "region": "r", "bogus": 1}]),
        "unknown field",
    )


def test_create_requires_an_id():
    ses = session()
    raises(StoreError, lambda: ses.type("Site").create([{"name": "n", "region": "r"}]), "needs an id")


def test_create_accepts_a_single_dict():
    ses = session()
    eq(ses.type("Site").create({"id": "s9", "name": "N", "region": "r", "version": 1}), 1)


def test_fetch_one_and_get():
    ses = session()
    eq(ses.type("Turbine").fetch_one(filter='id == "t3"')["serial"], "B-1")
    eq(ses.type("Turbine").fetch_one(filter='id == "nope"'), None)
    eq(ses.type("Turbine").get("t1")["serial"], "A-1")
    eq(ses.type("Turbine").get("nope"), None)


def test_count():
    ses = session()
    eq(ses.type("Turbine").count(), 5)
    eq(ses.type("Turbine").count('status == "ACTIVE"'), 3)


def test_count_rejects_a_joined_filter():
    ses = session()
    raises(QueryError, lambda: ses.type("Turbine").count('site.region == "north"'), "crosses a join")


def test_remove_accepts_a_scalar_or_a_list():
    ses = session()
    ses.type("Turbine").remove("t1")
    eq(ses.type("Turbine").count(), 4)
    ses.type("Turbine").remove(["t2", "t3"])
    eq(ses.type("Turbine").count(), 2)


def test_append_series_and_read_back():
    ses = session()
    pts = [(i * 10, float(i)) for i in range(50)]
    eq(ses.type("Sensor").append_series("readings", "t1-temp", pts), 50)
    eq(ses.type("Sensor").series("readings", "t1-temp")[:3], [(0, 0.0), (10, 1.0), (20, 2.0)])
    eq(len(ses.type("Sensor").series("readings", "t1-temp", 100, 200)), 11)


def test_series_on_a_non_series_field_is_rejected():
    ses = session()
    raises(QueryError, lambda: ses.type("Sensor").series("channel", "x"), "not a time series")
    raises(
        QueryError,
        lambda: ses.type("Sensor").append_series("channel", "x", []),
        "not a time series",
    )


def test_evaluate_groups_and_aggregates():
    ses = session()
    rows = ses.type("Turbine").evaluate(
        group=["siteId"],
        metrics={"n": "count()", "kw": "sum(ratedPowerKw)", "avgKw": "avg(ratedPowerKw)"},
        order=["siteId"],
    )
    eq([r["siteId"] for r in rows], ["s1", "s2", "s3"])
    eq([r["n"] for r in rows], [2, 2, 1])
    close(rows[0]["kw"], 5200.0)
    close(rows[1]["avgKw"], 4100.0)


def test_evaluate_without_a_group_is_a_single_row():
    ses = session()
    rows = ses.type("Turbine").evaluate(metrics={"n": "count()", "top": "max(ratedPowerKw)"})
    eq(len(rows), 1)
    eq(rows[0]["n"], 5)
    close(rows[0]["top"], 4100.0)


def test_evaluate_min_max_and_count_distinct():
    ses = session()
    rows = ses.type("Turbine").evaluate(
        metrics={
            "lo": "min(ratedPowerKw)",
            "hi": "max(ratedPowerKw)",
            "sites": "countDistinct(siteId)",
            "statuses": "countDistinct(status)",
        }
    )
    close(rows[0]["lo"], 1800.0)
    close(rows[0]["hi"], 4100.0)
    eq(rows[0]["sites"], 3)
    eq(rows[0]["statuses"], 3)


def test_evaluate_honours_a_filter():
    ses = session()
    rows = ses.type("Turbine").evaluate(
        group=["siteId"], metrics={"n": "count()"}, filter='status == "ACTIVE"', order=["siteId"]
    )
    eq([(r["siteId"], r["n"]) for r in rows], [("s1", 1), ("s2", 1), ("s3", 1)])


def test_evaluate_limit():
    ses = session()
    rows = ses.type("Turbine").evaluate(
        group=["siteId"], metrics={"kw": "sum(ratedPowerKw)"}, order=["-kw"], limit=1
    )
    eq(len(rows), 1)
    eq(rows[0]["siteId"], "s2")


def test_evaluate_rejects_bad_specs():
    ses = session()
    t = ses.type("Turbine")
    raises(QueryError, lambda: t.evaluate(metrics={}), "at least one metric")
    raises(QueryError, lambda: t.evaluate(metrics={"x": "median(ratedPowerKw)"}), "unknown aggregate")
    raises(QueryError, lambda: t.evaluate(metrics={"x": "sum()"}), "requires a field")
    raises(QueryError, lambda: t.evaluate(metrics={"x": "count(id)"}), "takes no argument")
    raises(QueryError, lambda: t.evaluate(metrics={"x": "not a spec"}), "cannot parse metric")
    raises(QueryError, lambda: t.evaluate(group=["site"], metrics={"n": "count()"}), "not a stored column")
    raises(
        QueryError,
        lambda: t.evaluate(metrics={"n": "count()"}, filter='site.region == "north"'),
        "crosses a join",
    )


def test_evaluate_matches_across_backends():
    a = session("relational").type("Turbine").evaluate(
        group=["status"],
        metrics={"n": "count()", "avgKw": "avg(ratedPowerKw)", "sites": "countDistinct(siteId)"},
        order=["status"],
    )
    b = session("memory").type("Turbine").evaluate(
        group=["status"],
        metrics={"n": "count()", "avgKw": "avg(ratedPowerKw)", "sites": "countDistinct(siteId)"},
        order=["status"],
    )
    eq(a, b)


def test_the_same_script_runs_unchanged_on_both_backends():
    """The model driven claim, as an actual test rather than a README bullet."""

    def script(ses):
        ses.type("Site").create(SITES)
        ses.type("Turbine").create(TURBINES)
        rows = ses.type("Turbine").fetch(
            filter='ratedPowerKw > 2000 and site.region == "north"',
            include=["site"],
            order=["-ratedPowerKw"],
        )
        return [(r["id"], r["site"]["region"]) for r in rows]

    eq(script(open_session(TINY, backend="relational")), script(open_session(TINY, backend="memory")))


def test_stats_report_both_stores():
    ses = session()
    ses.type("Sensor").append_series("readings", "t1-temp", [(i, float(i)) for i in range(100)])
    ses.flush()
    st = ses.stats()
    eq(st["store"]["backend"], "relational")
    eq(st["timeseries"]["points"], 100)
    ok(st["timeseries"]["encoded_bytes"] > 0)


def test_session_can_be_constructed_with_explicit_stores():
    m = load(TINY)
    ses = Session(m, MemoryStore(m), TimeSeriesStore(32))
    eq(ses.store.name, "memory")
    ses.type("Site").create(SITES)
    eq(ses.type("Site").count(), 3)


def test_close_is_safe():
    ses = open_session(TINY)
    ses.type("Site").create(SITES)
    ses.close()
    ok(True)
