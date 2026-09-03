import random

from keelson.errors import QueryError
from keelson.expr import (
    BoolOp,
    Compare,
    Literal,
    Path,
    parse_filter,
    split_pushable,
    to_predicate,
    to_sql,
)

from .runner import bump, eq, ok, raises

COLUMNS = {"status": "STATUS", "kw": "RATED_KW", "name": "name", "n": "n"}


def sql(text):
    return to_sql(parse_filter(text), COLUMNS)


def pred(text):
    return to_predicate(parse_filter(text))


def test_empty_filter_is_none():
    eq(parse_filter(None), None)
    eq(parse_filter("   "), None)
    ok(to_predicate(None)({"anything": 1}))


def test_simple_comparison_ast():
    node = parse_filter('status == "ACTIVE"')
    ok(isinstance(node, Compare))
    eq(node.op, "==")
    ok(isinstance(node.left, Path))
    ok(isinstance(node.right, Literal))


def test_and_flattens_into_one_boolop():
    node = parse_filter("n == 1 and n == 2 and n == 3")
    ok(isinstance(node, BoolOp))
    eq(node.op, "and")
    eq(len(node.operands), 3)


def test_precedence_and_binds_tighter_than_or():
    node = parse_filter("n == 1 or n == 2 and n == 3")
    eq(node.op, "or")
    eq(len(node.operands), 2)
    eq(node.operands[1].op, "and")


def test_parentheses_override_precedence():
    node = parse_filter("(n == 1 or n == 2) and n == 3")
    eq(node.op, "and")
    eq(node.operands[0].op, "or")


def test_sql_compilation_uses_mapped_columns():
    s, params = sql('status == "ACTIVE" and kw > 2000')
    ok('"STATUS" = ?' in s, s)
    ok('"RATED_KW" > ?' in s, s)
    eq(params, ["ACTIVE", 2000])


def test_sql_null_comparison():
    s, params = sql("name == null")
    eq(params, [])
    ok("IS NULL" in s)
    s2, _ = sql("name != null")
    ok("IS NOT NULL" in s2)


def test_sql_in_and_like():
    s, params = sql('status in ["A", "B"] and name like "T-%"')
    ok('"STATUS" IN (?, ?)' in s, s)
    ok('"name" LIKE ?' in s, s)
    eq(params, ["A", "B", "T-%"])


def test_sql_empty_in_is_always_false():
    s, params = sql("status in []")
    eq(params, [])
    ok("0 = 1" in s)


def test_sql_not():
    s, _ = sql("not (n == 1)")
    ok(s.startswith("(NOT "), s)


def test_sql_rejects_dotted_paths():
    raises(QueryError, lambda: to_sql(parse_filter('site.region == "north"'), COLUMNS), "cannot be pushed")


def test_sql_rejects_unknown_field():
    raises(QueryError, lambda: sql("nope == 1"), "unknown field")


def test_predicate_matches_rows():
    p = pred('status == "ACTIVE" and kw > 2000')
    ok(p({"status": "ACTIVE", "kw": 3000}))
    ok(not p({"status": "ACTIVE", "kw": 1000}))
    ok(not p({"status": "OFF", "kw": 3000}))


def test_predicate_handles_none_like_sql():
    p = pred("kw > 1")
    ok(not p({"kw": None}))
    eq_p = pred("kw == null")
    ok(eq_p({"kw": None}))
    ok(not eq_p({"kw": 5}))


def test_predicate_walks_dotted_paths():
    p = pred('site.region == "north"')
    ok(p({"site": {"region": "north"}}))
    ok(not p({"site": {"region": "south"}}))
    ok(not p({"site": None}))
    ok(not p({}))


def test_predicate_like_translates_wildcards():
    p = pred('name like "T-1%"')
    ok(p({"name": "T-100"}))
    ok(not p({"name": "X-100"}))
    single = pred('name like "T-_"')
    ok(single({"name": "T-4"}))
    ok(not single({"name": "T-44"}))


def test_predicate_like_escapes_regex_metacharacters():
    p = pred('name like "a.c"')
    ok(p({"name": "a.c"}))
    ok(not p({"name": "abc"}))


def test_predicate_in():
    p = pred('status in ["A", "B"]')
    ok(p({"status": "A"}))
    ok(not p({"status": "C"}))


def test_literal_on_the_left_is_supported():
    s, params = sql("2000 < kw")
    eq(params, [2000])
    ok(pred("2000 < kw")({"kw": 3000}))


def test_field_to_field_comparison():
    s, _ = sql("n == kw")
    ok('"n" = "RATED_KW"' in s, s)
    ok(pred("n == kw")({"n": 5, "kw": 5}))


def test_split_pushable_separates_joined_paths():
    node = parse_filter('status == "ACTIVE" and site.region == "north" and kw > 1')
    push, rest = split_pushable(node)
    eq(len(push.operands), 2)
    ok(rest is not None)
    eq(rest.left.dotted(), "site.region")


def test_split_pushable_all_local():
    push, rest = split_pushable(parse_filter("n == 1 and kw > 2"))
    ok(rest is None)
    ok(push is not None)


def test_split_pushable_all_remote():
    push, rest = split_pushable(parse_filter('site.region == "north"'))
    ok(push is None)
    ok(rest is not None)


def test_split_pushable_does_not_split_across_or():
    # An OR mixing local and joined paths cannot be partially pushed.
    push, rest = split_pushable(parse_filter('n == 1 or site.region == "north"'))
    ok(push is None)
    ok(rest is not None)


def test_syntax_errors():
    raises(QueryError, lambda: parse_filter("n =="), "unexpected token")
    raises(QueryError, lambda: parse_filter("n 1"), "comparison operator")
    raises(QueryError, lambda: parse_filter("(n == 1"), "expected RPAREN")
    raises(QueryError, lambda: parse_filter("n == 1)"), "trailing input")
    raises(QueryError, lambda: parse_filter('1 in ["a"]'), "requires a field")


def test_sql_and_predicate_agree_on_random_rows():
    """The two compilers must classify every row the same way."""
    import sqlite3

    rnd = random.Random(99)
    rows = [
        {
            "id": "r%d" % i,
            "status": rnd.choice(["ACTIVE", "DERATED", "OFFLINE"]),
            "kw": rnd.choice([None, 500.0, 2000.0, 3500.0]),
            "n": rnd.randint(0, 9),
            "name": rnd.choice(["T-1", "T-10", "X-2", "T-a"]),
        }
        for i in range(400)
    ]
    conn = sqlite3.connect(":memory:")
    conn.execute('CREATE TABLE t (id TEXT, "STATUS" TEXT, "RATED_KW" REAL, n INTEGER, name TEXT)')
    conn.executemany(
        "INSERT INTO t VALUES (?, ?, ?, ?, ?)",
        [(r["id"], r["status"], r["kw"], r["n"], r["name"]) for r in rows],
    )

    filters = [
        'status == "ACTIVE"',
        "kw > 1000",
        "kw >= 2000 and n < 5",
        'status in ["ACTIVE", "DERATED"]',
        'name like "T-%"',
        "not (n == 0)",
        'status == "ACTIVE" or kw > 3000',
        "kw == null",
        "kw != null and n >= 5",
        '(status == "OFFLINE" or n == 3) and kw > 400',
    ]
    for f in filters:
        where, params = sql(f)
        expected = {r[0] for r in conn.execute("SELECT id FROM t WHERE " + where, params)}
        p = to_predicate(parse_filter(f))
        actual = {r["id"] for r in rows if p(r)}
        eq(actual, expected, "filter %r disagreed" % f)
        bump()
    conn.close()
