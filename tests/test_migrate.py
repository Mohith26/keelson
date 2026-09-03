from keelson import migrate
from keelson.errors import MigrationError
from keelson.model import load
from keelson.session import open_session

from .fixtures import SITES, TINY
from .runner import eq, ok, raises

BASE = """
entity type Site {
  id: string
  name: string
  region: string
}
"""


def test_no_change_is_an_empty_plan():
    plan = migrate.diff(load(BASE), load(BASE))
    ok(plan.is_empty)
    ok(plan.is_safe)
    eq(len(plan), 0)


def test_new_type_creates_a_table():
    after = BASE + "\nentity type Turbine { id: string siteId: string }"
    plan = migrate.diff(load(BASE), load(after))
    eq([c.kind for c in plan], ["create_type"])
    ok(plan.is_safe)
    ok('CREATE TABLE "turbine"' in plan.statements()[0])


def test_new_column_is_a_safe_alter():
    after = BASE.replace("region: string", "region: string\n  capacityMw: double")
    plan = migrate.diff(load(BASE), load(after))
    eq([c.kind for c in plan], ["add_column"])
    ok(plan.is_safe)
    eq(plan.statements(), ['ALTER TABLE "site" ADD COLUMN "capacityMw" REAL'])


def test_new_column_default_is_emitted():
    after = BASE.replace("region: string", 'region: string\n  tier: string = "gold"')
    plan = migrate.diff(load(BASE), load(after))
    ok("DEFAULT 'gold'" in plan.statements()[0])


def test_widening_is_safe_and_needs_no_sql():
    after = BASE.replace("name: string", "name: string\n  n: long").replace(
        "entity type Site {", "entity type Site {"
    )
    before = load(BASE.replace("name: string", "name: string\n  n: int"))
    plan = migrate.diff(before, load(after))
    eq([c.kind for c in plan], ["widen_column"])
    ok(plan.is_safe)
    eq(plan.statements(), [])


def test_narrowing_is_unsafe():
    before = load(BASE.replace("name: string", "name: string\n  n: double"))
    after = load(BASE.replace("name: string", "name: string\n  n: int"))
    plan = migrate.diff(before, after)
    eq([c.kind for c in plan], ["narrow_column"])
    ok(not plan.is_safe)
    ok("truncate" in plan.unsafe()[0].detail)


def test_dropping_a_column_is_unsafe():
    after = BASE.replace("  region: string\n", "")
    plan = migrate.diff(load(BASE), load(after))
    eq([c.kind for c in plan], ["drop_column"])
    ok(not plan.is_safe)


def test_dropping_a_type_is_unsafe():
    before = load(BASE + "\nentity type Turbine { id: string }")
    plan = migrate.diff(before, load(BASE))
    eq([c.kind for c in plan], ["drop_type"])
    ok(not plan.is_safe)


def test_renaming_the_table_is_unsafe():
    after = BASE.replace("entity type Site {", 'entity type Site schema name "SITES" {')
    plan = migrate.diff(load(BASE), load(after))
    ok("rename_table" in [c.kind for c in plan])
    ok(not plan.is_safe)


def test_adding_and_dropping_indexes():
    after = '@db(index="region")\n' + BASE
    plan = migrate.diff(load(BASE), load(after))
    eq([c.kind for c in plan], ["add_index"])
    ok(plan.is_safe)
    ok('CREATE INDEX "ix_site_region"' in plan.statements()[0])

    back = migrate.diff(load(after), load(BASE))
    eq([c.kind for c in back], ["drop_index"])
    ok(back.is_safe)


def test_changing_an_index_definition_rebuilds_it():
    a = '@db(index="region")\n' + BASE
    b = '@db(index="region")\n' + BASE
    eq(len(migrate.diff(load(a), load(b))), 0)
    c = '@db(unique="region")\n' + BASE
    plan = migrate.diff(load(a), load(c))
    kinds = sorted(c.kind for c in plan)
    eq(kinds, ["add_index", "drop_index"])


def test_apply_runs_the_safe_statements_against_a_live_store():
    ses = open_session(BASE)
    ses.type("Site").create([{"id": s["id"], "name": s["name"], "region": s["region"]} for s in SITES])
    after = load(BASE.replace("region: string", "region: string\n  capacityMw: double = 0.0"))
    plan = migrate.apply(ses, after)
    eq(len(plan), 1)
    ok("capacityMw" in ses.store.table_columns("site"))
    rows = ses.type("Site").fetch(order=["id"])
    eq(len(rows), 3)
    eq(rows[0]["capacityMw"], 0.0)
    eq(rows[0]["name"], "Alpha")


def test_apply_refuses_an_unsafe_migration_by_default():
    ses = open_session(BASE)
    after = load(BASE.replace("  region: string\n", ""))
    raises(MigrationError, lambda: migrate.apply(ses, after), "refusing")
    ok("region" in ses.store.table_columns("site"))


def test_apply_can_be_forced():
    ses = open_session(BASE)
    ses.type("Site").create([{"id": "a", "name": "A", "region": "r"}])
    after = load(BASE.replace("  region: string\n", ""))
    migrate.apply(ses, after, allow_destructive=True)
    ok("region" not in ses.store.table_columns("site"))
    eq(ses.type("Site").fetch()[0]["name"], "A")


def test_apply_swaps_the_session_model_and_clears_proxies():
    ses = open_session(BASE)
    ses.type("Site")
    after = load(BASE + "\nentity type Turbine { id: string }")
    migrate.apply(ses, after)
    ok("Turbine" in ses.model)
    eq(ses.type("Turbine").count(), 0)


def test_plan_summary_is_readable():
    after = BASE.replace("  region: string\n", "")
    plan = migrate.diff(load(BASE), load(after))
    text = " ".join(plan.summary())
    ok("UNSAFE" in text, text)
    ok("drop_column" in text, text)


def test_change_as_dict_roundtrips_the_fields():
    after = BASE.replace("region: string", "region: string\n  x: int")
    d = migrate.diff(load(BASE), load(after)).changes[0].as_dict()
    eq(d["kind"], "add_column")
    eq(d["safe"], True)
    ok(d["sql"])


def test_fingerprint_agrees_with_the_diff():
    a, b = load(BASE), load(BASE.replace("region: string", "region: string\n  x: int"))
    ok(a.fingerprint() != b.fingerprint())
    ok(not migrate.diff(a, b).is_empty)
    ok(migrate.diff(load(TINY), load(TINY)).is_empty)
