from keelson.errors import StoreError
from keelson.model import load
from keelson.stores.memory import MemoryStore
from keelson.stores.relational import RelationalStore

from .fixtures import SENSORS, SITES, TINY, TURBINES, fleet_source
from .runner import eq, ok, raises

MODEL = load(TINY)


def store():
    return RelationalStore(MODEL)


def test_ddl_uses_schema_names_and_types():
    st = store()
    ddl = "\n".join(st.ddl_for(MODEL["Turbine"]))
    ok('CREATE TABLE IF NOT EXISTS "WTG"' in ddl, ddl)
    ok('"SERIAL_NO" TEXT' in ddl, ddl)
    ok('"RATED_KW" REAL' in ddl, ddl)
    ok('"id" TEXT PRIMARY KEY' in ddl, ddl)


def test_ddl_emits_declared_defaults():
    ddl = "\n".join(store().ddl_for(MODEL["Turbine"]))
    ok("DEFAULT 'UNKNOWN'" in ddl, ddl)
    ok("DEFAULT 1" in ddl, ddl)


def test_tables_and_indexes_are_created():
    st = store()
    eq(sorted(st.table_columns("WTG")), ["RATED_KW", "SERIAL_NO", "id", "siteId", "status", "version"])
    names = st.index_names("WTG")
    ok(any("serial" in n for n in names), names)
    ok(any("status" in n for n in names), names)


def test_abstract_types_get_no_table():
    st = RelationalStore(load(fleet_source()))
    eq(st.table_columns("asset"), [])
    ok(st.table_columns("WTG_MASTER"))


def test_create_and_fetch_roundtrip():
    st = store()
    st.create_batch(MODEL["Site"], SITES)
    rows = st.fetch(MODEL["Site"], order=["id"])
    eq(len(rows), 3)
    eq(rows[0]["name"], "Alpha")
    eq(sorted(rows[0]), ["id", "name", "region", "version"])


def test_unique_index_is_enforced():
    st = store()
    st.create_batch(MODEL["Turbine"], TURBINES)
    dup = dict(TURBINES[0])
    dup["id"] = "tX"
    raises(StoreError, lambda: st.create_batch(MODEL["Turbine"], [dup]), "failed")


def test_upsert_overwrites_including_with_null():
    st = store()
    st.create_batch(MODEL["Site"], SITES)
    st.upsert_batch(MODEL["Site"], [{"id": "s1", "name": "Renamed", "region": None, "version": 9}])
    row = st.fetch(MODEL["Site"], '"id" = ?', ("s1",))[0]
    eq(row["name"], "Renamed")
    eq(row["region"], None)
    eq(row["version"], 9)


def test_merge_keeps_existing_values_when_given_none():
    st = store()
    st.create_batch(MODEL["Site"], SITES)
    st.merge_batch(MODEL["Site"], [{"id": "s1", "name": None, "region": "west", "version": None}])
    row = st.fetch(MODEL["Site"], '"id" = ?', ("s1",))[0]
    eq(row["name"], "Alpha")
    eq(row["region"], "west")
    eq(row["version"], 1)


def test_merge_inserts_when_absent():
    st = store()
    st.merge_batch(MODEL["Site"], [{"id": "new", "name": "N", "region": "r", "version": 1}])
    eq(st.count(MODEL["Site"]), 1)


def test_remove():
    st = store()
    st.create_batch(MODEL["Site"], SITES)
    st.remove(MODEL["Site"], ["s1", "s2"])
    eq(st.count(MODEL["Site"]), 1)


def test_order_limit_and_offset():
    st = store()
    st.create_batch(MODEL["Turbine"], TURBINES)
    desc = st.fetch(MODEL["Turbine"], order=["-ratedPowerKw", "id"], limit=2)
    eq([r["id"] for r in desc], ["t3", "t4"])
    page = st.fetch(MODEL["Turbine"], order=["id"], limit=2, offset=2)
    eq([r["id"] for r in page], ["t3", "t4"])


def test_column_projection():
    st = store()
    st.create_batch(MODEL["Turbine"], TURBINES)
    rows = st.fetch(MODEL["Turbine"], columns=["serial"], limit=1, order=["id"])
    eq(sorted(rows[0]), ["id", "serial"])


def test_count_with_predicate():
    st = store()
    st.create_batch(MODEL["Turbine"], TURBINES)
    eq(st.count(MODEL["Turbine"], '"status" = ?', ("ACTIVE",)), 3)


def test_explain_reports_index_usage():
    st = store()
    st.create_batch(MODEL["Turbine"], TURBINES)
    plan = " ".join(st.explain(MODEL["Turbine"], '"status" = ?', ("ACTIVE",)))
    ok("INDEX" in plan.upper(), plan)
    scan = " ".join(st.explain(MODEL["Turbine"], '"siteId" = ?', ("s1",)))
    ok("SCAN" in scan.upper(), scan)


def test_boolean_roundtrip():
    m = load("entity type A { id: string flag: boolean }")
    st = RelationalStore(m)
    st.create_batch(m["A"], [{"id": "a", "flag": True}, {"id": "b", "flag": False}])
    rows = {r["id"]: r["flag"] for r in st.fetch(m["A"])}
    eq(rows["a"], True)
    eq(rows["b"], False)
    ok(isinstance(rows["a"], bool))


def test_counters_track_work():
    st = store()
    st.create_batch(MODEL["Turbine"], TURBINES)
    st.reset_counters()
    st.fetch(MODEL["Turbine"])
    eq(st.queries, 1)
    eq(st.rows_scanned, 5)
    st.reset_counters()
    eq(st.queries, 0)


def test_memory_store_matches_relational_on_the_same_writes():
    rel = store()
    mem = MemoryStore(MODEL)
    for st in (rel, mem):
        st.create_batch(MODEL["Turbine"], TURBINES)
    a = rel.fetch(MODEL["Turbine"], order=["id"])
    b = mem.fetch(MODEL["Turbine"], order=["id"])
    eq(a, b)


def test_memory_store_rejects_duplicate_ids():
    mem = MemoryStore(MODEL)
    mem.create_batch(MODEL["Site"], SITES)
    raises(StoreError, lambda: mem.create_batch(MODEL["Site"], SITES[:1]), "duplicate id")


def test_memory_store_merge_semantics_match():
    rel, mem = store(), MemoryStore(MODEL)
    for st in (rel, mem):
        st.create_batch(MODEL["Site"], SITES)
        st.merge_batch(MODEL["Site"], [{"id": "s1", "name": None, "region": "west"}])
    eq(rel.fetch(MODEL["Site"], order=["id"]), mem.fetch(MODEL["Site"], order=["id"]))


def test_memory_store_null_ordering_matches_sqlite():
    m = load("entity type A { id: string n: int }")
    rel, mem = RelationalStore(m), MemoryStore(m)
    rows = [{"id": "a", "n": 3}, {"id": "b", "n": None}, {"id": "c", "n": 1}]
    for st in (rel, mem):
        st.create_batch(m["A"], rows)
    eq(
        [r["id"] for r in rel.fetch(m["A"], order=["n", "id"])],
        [r["id"] for r in mem.fetch(m["A"], order=["n", "id"])],
    )


def test_batched_write_is_a_single_transaction():
    st = store()
    n = st.create_batch(MODEL["Sensor"], SENSORS)
    eq(n, 3)
    eq(st.count(MODEL["Sensor"]), 3)
