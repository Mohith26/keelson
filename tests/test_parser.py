from keelson.dsl.parser import parse
from keelson.errors import ParseError

from .fixtures import TINY, fleet_source
from .runner import eq, ok, raises


def one(src):
    return parse(src).types[0]


def test_parses_a_minimal_type():
    t = one("entity type A { id: string }")
    eq(t.name, "A")
    eq(len(t.fields), 1)
    eq(t.fields[0].name, "id")
    eq(t.fields[0].kind, "primitive")
    ok(not t.is_mixin)
    ok(not t.is_abstract)


def test_mixin_and_abstract_flags():
    ok(one("mixin type M { id: string }").is_mixin)
    ok(one("abstract entity type A { id: string }").is_abstract)


def test_extends_and_mixes():
    t = one("entity type C extends B mixes M, N { id: string }")
    eq(t.extends, "B")
    eq(t.mixes, ["M", "N"])


def test_schema_name_on_type_and_field():
    t = one('entity type A schema name "TBL" { id: string schema name "PK" }')
    eq(t.table, "TBL")
    eq(t.fields[0].column, "PK")


def test_defaults():
    t = one('entity type A { id: string, n: int = 7, s: string = "x", b: boolean = true }')
    eq([f.default for f in t.fields], [None, 7, "x", True])


def test_reference_field():
    f = one("entity type A { id: string site: Site(siteId) }").fields[1]
    eq(f.kind, "reference")
    eq(f.type_name, "Site")
    eq(f.fk_local, "siteId")


def test_collection_field():
    f = one("entity type A { id: string kids: [Kid](parentId, id) }").fields[1]
    eq(f.kind, "collection")
    eq(f.type_name, "Kid")
    eq(f.fk_local, "parentId")
    eq(f.fk_remote, "id")


def test_timeseries_field():
    f = one("entity type A { id: string r: timeseries<double>(id) }").fields[1]
    eq(f.kind, "timeseries")
    eq(f.value_type, "double")
    eq(f.series_key, "id")


def test_annotations_are_collected_and_merged():
    t = one('@db(index="a") @db(unique="b") @doc(text="hi") entity type A { id: string }')
    eq(t.annotations["db"], {"index": "a", "unique": "b"})
    eq(t.annotations["doc"], {"text": "hi"})


def test_annotation_with_no_arguments():
    t = one("@deprecated entity type A { id: string }")
    eq(t.annotations["deprecated"], {})


def test_multiple_types_in_one_module():
    mod = parse(TINY)
    eq(sorted(mod.by_name()), ["Persistable", "Sensor", "Site", "Turbine"])


def test_fleet_model_parses():
    mod = parse(fleet_source(), "fleet.ks")
    names = sorted(mod.by_name())
    eq(names, ["Asset", "Audited", "Persistable", "Sensor", "Site", "Substation", "Turbine", "WorkOrder"])


def test_missing_brace_is_reported_with_a_line():
    err = raises(ParseError, lambda: parse("entity type A {\n  id: string\n"))
    eq(err.line, 1)
    ok("unterminated" in str(err))


def test_bad_field_syntax():
    raises(ParseError, lambda: parse("entity type A { id string }"), "expected ':'")


def test_missing_type_keyword():
    raises(ParseError, lambda: parse("entity A { }"), "'type'")


def test_collection_needs_two_keys():
    raises(ParseError, lambda: parse("entity type A { id: string k: [B](x) }"), "','")


def test_trailing_garbage_is_rejected():
    raises(ParseError, lambda: parse("entity type A { id: string } }"))
