from keelson.errors import ResolveError
from keelson.model import load

from .fixtures import TINY, fleet_source
from .runner import eq, ok, raises


def test_mixin_fields_are_flattened_in():
    m = load(TINY)
    ok(m["Site"].has("id"))
    ok(m["Site"].has("version"))
    eq(m["Site"].field("version").declared_on, "Persistable")
    eq(m["Site"].field("name").declared_on, "Site")


def test_id_is_always_the_first_field():
    m = load(TINY)
    for t in m:
        eq(list(t.fields)[0], "id", "on %s" % t.name)


def test_inheritance_chain_is_flattened():
    m = load(fleet_source())
    turbine = m["Turbine"]
    eq(turbine.bases, ["Asset"])
    for name in ("id", "version", "siteId", "serial", "status", "commissionedOn", "ratedPowerKw"):
        ok(turbine.has(name), "Turbine missing %s" % name)
    eq(turbine.field("siteId").declared_on, "Asset")
    eq(turbine.field("ratedPowerKw").declared_on, "Turbine")


def test_abstract_types_are_excluded_from_concrete():
    m = load(fleet_source())
    ok(m["Asset"].is_abstract)
    eq([t.name for t in m.concrete()].count("Asset"), 0)


def test_schema_names_win_over_field_names():
    m = load(fleet_source())
    eq(m["Turbine"].table, "WTG_MASTER")
    eq(m["Turbine"].field("ratedPowerKw").column, "RATED_KW")
    eq(m["Turbine"].field("serial").column, "serial")
    eq(m["Site"].table, "site")


def test_default_table_name_is_snake_cased():
    m = load("entity type WorkOrderLine { id: string }")
    eq(m["WorkOrderLine"].table, "work_order_line")


def test_field_store_routing():
    m = load(TINY)
    eq(m["Sensor"].field("channel").store, "relational")
    eq(m["Sensor"].field("readings").store, "timeseries")
    eq(m["Sensor"].field("turbine").store, "planner")


def test_index_plan_from_annotations():
    m = load(TINY)
    idx = m["Turbine"].indexes()
    eq(len(idx), 2)
    names = {n for n, _c, _u in idx}
    ok(any("serial" in n for n in names))
    uniques = {n: u for n, _c, u in idx}
    eq(sum(1 for u in uniques.values() if u), 1)


def test_composite_index_columns_are_split():
    m = load(fleet_source())
    idx = dict((n, c) for n, c, _u in m["WorkOrder"].indexes())
    ok(any(c == ["siteId", "openedAt"] for c in idx.values()))


def test_fingerprint_is_stable_and_sensitive():
    a = load(TINY)
    b = load(TINY)
    eq(a.fingerprint(), b.fingerprint())
    c = load(TINY.replace("channel: string", "channel: string\n  extra: int"))
    ok(a.fingerprint() != c.fingerprint())


def test_missing_id_is_rejected():
    raises(ResolveError, lambda: load("entity type A { name: string }"), "no 'id' field")


def test_duplicate_type_is_rejected():
    raises(
        ResolveError,
        lambda: load("entity type A { id: string } entity type A { id: string }"),
        "duplicate type",
    )


def test_cyclic_inheritance_is_rejected():
    src = "entity type A extends B { id: string } entity type B extends A { id: string }"
    raises(ResolveError, lambda: load(src), "cyclic")


def test_unknown_field_type_is_rejected():
    raises(ResolveError, lambda: load("entity type A { id: string x: widget }"), "unknown type")


def test_reference_to_unknown_type_is_rejected():
    src = "entity type A { id: string bId: string b: B(bId) }"
    raises(ResolveError, lambda: load(src), "unknown type")


def test_reference_with_missing_foreign_key_is_rejected():
    src = "entity type B { id: string } entity type A { id: string b: B(bId) }"
    raises(ResolveError, lambda: load(src), "foreign key")


def test_collection_pointing_at_a_missing_remote_field_is_rejected():
    src = "entity type B { id: string } entity type A { id: string bs: [B](aId, id) }"
    raises(ResolveError, lambda: load(src), "to have a 'aId' field")


def test_conflicting_inherited_field_is_rejected():
    src = """
    entity type A { id: string n: int }
    entity type B extends A { n: string }
    """
    raises(ResolveError, lambda: load(src), "conflicts")


def test_identical_redeclaration_is_allowed_and_can_rename_the_column():
    src = """
    entity type A { id: string n: int }
    entity type B extends A { n: int schema name "N_COL" }
    """
    m = load(src)
    eq(m["B"].field("n").column, "N_COL")
    eq(m["A"].field("n").column, "n")


def test_two_types_on_one_table_is_rejected():
    src = 'entity type A schema name "T" { id: string } entity type B schema name "T" { id: string }'
    raises(ResolveError, lambda: load(src), "both map to table")


def test_two_fields_on_one_column_is_rejected():
    src = 'entity type A { id: string a: int schema name "C" b: int schema name "C" }'
    raises(ResolveError, lambda: load(src), "both map to column")


def test_mixin_cannot_be_extended():
    src = "mixin type M { id: string } entity type A extends M { }"
    raises(ResolveError, lambda: load(src), "cannot extend mixin")


def test_non_mixin_cannot_be_mixed_in():
    src = "entity type M { id: string } entity type A mixes M { id: string }"
    raises(ResolveError, lambda: load(src), "not a mixin type")


def test_index_on_a_non_column_is_rejected():
    src = '@db(index="site") entity type A { id: string sId: string site: A(sId) }'
    raises(ResolveError, lambda: load(src), "not a stored column")


def test_timeseries_value_type_is_checked():
    src = "entity type A { id: string r: timeseries<string>(id) }"
    raises(ResolveError, lambda: load(src), "must hold double")


def test_timeseries_key_must_exist():
    src = "entity type A { id: string r: timeseries<double>(missing) }"
    raises(ResolveError, lambda: load(src), "keys on 'missing'")


def test_unknown_type_lookup_raises():
    m = load(TINY)
    raises(ResolveError, lambda: m["Nope"], "unknown type")
