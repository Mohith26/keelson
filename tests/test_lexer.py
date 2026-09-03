from keelson.dsl.lexer import Token, tokenize
from keelson.errors import LexError

from .runner import eq, ok, raises


def kinds(src):
    return [t.kind for t in tokenize(src)]


def test_keywords_are_promoted():
    eq(kinds("entity type X {}"), ["ENTITY", "TYPE", "IDENT", "LBRACE", "RBRACE", "EOF"])


def test_identifiers_are_not_keywords_when_embedded():
    toks = tokenize("entityish typeName")
    eq([t.kind for t in toks], ["IDENT", "IDENT", "EOF"])
    eq(toks[0].value, "entityish")


def test_numbers():
    toks = tokenize("1 -2 3.5 -4.25")
    eq([t.value for t in toks[:-1]], [1, -2, 3.5, -4.25])
    eq([t.kind for t in toks[:-1]], ["INT", "INT", "FLOAT", "FLOAT"])


def test_strings_are_unescaped():
    toks = tokenize(r'"a\"b" "c\\d" "e\nf"')
    eq([t.value for t in toks[:-1]], ['a"b', "c\\d", "e\nf"])


def test_line_and_column_tracking():
    toks = tokenize("entity\n  type\n\n   X")
    eq(toks[0].line, 1)
    eq(toks[1].line, 2)
    eq(toks[1].col, 3)
    eq(toks[2].line, 4)
    eq(toks[2].col, 4)


def test_line_comments_are_skipped():
    eq(kinds("// hello\nentity // trailing\n"), ["ENTITY", "EOF"])


def test_block_comments_are_skipped_and_keep_line_numbers():
    toks = tokenize("/* one\ntwo\nthree */ entity")
    eq(toks[0].kind, "ENTITY")
    eq(toks[0].line, 3)


def test_annotation_punctuation():
    eq(
        kinds('@db(index="a")'),
        ["AT", "IDENT", "LPAREN", "IDENT", "EQ", "STRING", "RPAREN", "EOF"],
    )


def test_timeseries_generic_punctuation():
    eq(
        kinds("timeseries<double>(id)"),
        ["TIMESERIES", "LT", "IDENT", "GT", "LPAREN", "IDENT", "RPAREN", "EOF"],
    )


def test_unknown_character_reports_position():
    err = raises(LexError, lambda: tokenize("entity\n  ?"))
    eq(err.line, 2)
    eq(err.col, 3)


def test_token_equality_ignores_position():
    ok(Token("IDENT", "a", 1, 1) == Token("IDENT", "a", 9, 9))
    ok(not (Token("IDENT", "a", 1, 1) == Token("IDENT", "b", 1, 1)))


def test_eof_is_always_last():
    for src in ["", "entity", "// only a comment"]:
        toks = tokenize(src)
        eq(toks[-1].kind, "EOF")
