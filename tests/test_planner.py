from keelson.errors import QueryError
from keelson.session import open_session

from .fixtures import SENSORS, SITES, TINY, TURBINES
from .runner import eq, ok, raises


def session(backend="relational"):
    ses = open_session(TINY, backend=backend, segment_points=64)
    ses.type("Site").create(SITES)
    ses.type("Turbine").create(TURBINES)
    ses.type("Sensor").create(SENSORS)
    for i in range(200):
        ses.type("Sensor").append_series("readings", "t1-temp", [(1000 + i * 10, 20.0 + i)])
    ses.flush()
    return ses


def test_local_filter_is_pushed_down():
    ses = session()
    ses.type("Turbine").fetch(filter='status == "ACTIVE"')
    st = ses.last_stats
    ok(st.pushed_filter is not None)
    eq(st.residual_filter, None)
    eq(st.root_rows, 3)


def test_joined_filter_becomes_a_residual():
    ses = session()
    rows = ses.type("Turbine").fetch(filter='site.region == "north"', include=["site"])
    st = ses.last_stats
    eq(st.pushed_filter, None)
    ok(st.residual_filter is not None)
    eq(sorted(r["id"] for r in rows), ["t1", "t2", "t5"])


def test_mixed_filter_is_split():
    ses = session()
    rows = ses.type("Turbine").fetch(
        filter='status == "ACTIVE" and site.region == "north"', include=["site"]
    )
    st = ses.last_stats
    ok(st.pushed_filter is not None)
    ok(st.residual_filter is not None)
    # The pushdown cut the root set to the three ACTIVE turbines before the join.
    eq(st.root_rows, 3)
    eq(sorted(r["id"] for r in rows), ["t1", "t5"])


def test_order_and_limit_are_pushed_only_without_a_residual():
    ses = session()
    ses.type("Turbine").fetch(filter='status == "ACTIVE"', order=["-ratedPowerKw"], limit=2)
    ok(ses.last_stats.pushed_order)
    ok(ses.last_stats.pushed_limit)

    rows = ses.type("Turbine").fetch(
        filter='site.region == "north"', include=["site"], order=["-ratedPowerKw"], limit=2
    )
    ok(not ses.last_stats.pushed_order)
    ok(not ses.last_stats.pushed_limit)
    # The limit must be applied after the residual, or it would return the
    # wrong rows entirely.
    eq([r["id"] for r in rows], ["t1", "t2"])


def test_limit_after_residual_returns_the_right_rows():
    """Regression: applying LIMIT before a residual filter silently truncates."""
    ses = session()
    rows = ses.type("Turbine").fetch(
        filter='site.region == "south"', include=["site"], order=["id"], limit=5
    )
    eq([r["id"] for r in rows], ["t3", "t4"])


def test_reference_include_attaches_the_parent():
    ses = session()
    rows = ses.type("Turbine").fetch(filter='id == "t1"', include=["site"])
    eq(rows[0]["site"]["name"], "Alpha")


def test_reference_include_tolerates_a_dangling_key():
    ses = session()
    ses.type("Turbine").upsert(
        [{"id": "t9", "siteId": "missing", "serial": "Z", "status": "ACTIVE", "ratedPowerKw": 1.0, "version": 1}]
    )
    rows = ses.type("Turbine").fetch(filter='id == "t9"', include=["site"])
    eq(rows[0]["site"], None)


def test_collection_include_groups_children():
    ses = session()
    rows = ses.type("Site").fetch(include=["turbines"], order=["id"])
    eq([len(r["turbines"]) for r in rows], [2, 2, 1])


def test_collection_include_is_empty_not_missing():
    ses = session()
    ses.type("Site").upsert([{"id": "s9", "name": "Empty", "region": "x", "version": 1}])
    rows = ses.type("Site").fetch(filter='id == "s9"', include=["turbines"])
    eq(rows[0]["turbines"], [])


def test_nested_include_walks_two_levels():
    ses = session()
    rows = ses.type("Site").fetch(filter='id == "s1"', include=["turbines.sensors"])
    kids = {t["id"]: len(t["sensors"]) for t in rows[0]["turbines"]}
    eq(kids, {"t1": 2, "t2": 0})


def test_joins_are_batched_not_per_row():
    """N+1 regression guard: one query per include level, not per row."""
    ses = session()
    ses.reset_counters()
    ses.type("Site").fetch(include=["turbines.sensors"])
    # One for the roots, one for turbines, one for sensors.
    eq(ses.store.queries, 3)


def test_timeseries_include_attaches_points():
    ses = session()
    rows = ses.type("Sensor").fetch(filter='id == "t1-temp"', include=["readings"])
    eq(len(rows[0]["readings"]), 200)
    eq(rows[0]["readings"][0], (1000, 20.0))


def test_timeseries_window_limits_the_scan():
    ses = session()
    ses.reset_counters()
    rows = ses.type("Sensor").fetch(
        filter='id == "t1-temp"', include=["readings"], window=(1000, 1300)
    )
    eq(len(rows[0]["readings"]), 31)
    # 200 points in segments of 64 is 3 sealed segments plus an open tail; a
    # window covering the first 31 points can only need the first one.
    eq(ses.last_stats.series_scanned, 1)


def test_timeseries_include_on_a_sensor_with_no_points():
    ses = session()
    rows = ses.type("Sensor").fetch(filter='id == "t1-vib"', include=["readings"])
    eq(rows[0]["readings"], [])


def test_include_of_a_stored_column_is_rejected():
    ses = session()
    raises(QueryError, lambda: ses.type("Turbine").fetch(include=["serial"]), "stored column")


def test_include_of_an_unknown_field_is_rejected():
    ses = session()
    raises(QueryError, lambda: ses.type("Turbine").fetch(include=["nope"]), "no field")


def test_memory_backend_produces_the_same_rows():
    a = session("relational").type("Turbine").fetch(
        filter='status == "ACTIVE"', include=["site"], order=["id"]
    )
    b = session("memory").type("Turbine").fetch(
        filter='status == "ACTIVE"', include=["site"], order=["id"]
    )
    eq([r["id"] for r in a], [r["id"] for r in b])
    eq([r["site"]["name"] for r in a], [r["site"]["name"] for r in b])


def test_batched_in_clause_handles_more_keys_than_sqlite_allows():
    """SQLite caps bound parameters, so a wide IN has to be chunked."""
    ses = open_session(TINY, backend="relational")
    sites = [{"id": "s%04d" % i, "name": "S%d" % i, "region": "r", "version": 1} for i in range(2500)]
    turbines = [
        {
            "id": "t%04d" % i,
            "siteId": "s%04d" % i,
            "serial": "SN%04d" % i,
            "status": "ACTIVE",
            "ratedPowerKw": 1000.0,
            "version": 1,
        }
        for i in range(2500)
    ]
    ses.type("Site").create(sites)
    ses.type("Turbine").create(turbines)
    rows = ses.type("Turbine").fetch(include=["site"])
    eq(len(rows), 2500)
    ok(all(r["site"] is not None for r in rows))
    # 2500 distinct keys at 900 per statement is 3 chunks, plus the root query.
    eq(ses.store.queries, 4)
