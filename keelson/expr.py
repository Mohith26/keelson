"""Filter expression language.

One expression string is parsed once and then compiled twice: into a SQL
WHERE clause with bound parameters for the relational store, and into a plain
Python predicate for the in memory store and for the differential oracle.
Having both compilers hang off the same AST is what makes the oracle
meaningful, because a bug in either backend shows up as a disagreement rather
than as two backends being wrong in the same way.

Supported syntax:

    a == 1 and (b != "x" or c > 2.5)
    status in ["ACTIVE", "DERATED"]
    name like "T-1%"
    not (kw > 100)
    site.region == "north"        -- dotted paths, resolved after the join
"""

import re

from .errors import QueryError

_TOKEN = re.compile(
    r"""
    \s+
  | (?P<FLOAT>-?\d+\.\d+)
  | (?P<INT>-?\d+)
  | (?P<STRING>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<OP>==|!=|>=|<=|>|<)
  | (?P<LBRACK>\[)
  | (?P<RBRACK>\])
  | (?P<LPAREN>\()
  | (?P<RPAREN>\))
  | (?P<COMMA>,)
  | (?P<IDENT>[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)
    """,
    re.VERBOSE,
)

_KEYWORDS = {"and", "or", "not", "in", "like", "true", "false", "null"}


class Node:
    pass


class Literal(Node):
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def paths(self):
        return set()

    def __repr__(self):
        return "Literal(%r)" % (self.value,)


class Path(Node):
    __slots__ = ("parts",)

    def __init__(self, parts):
        self.parts = tuple(parts)

    @property
    def root(self):
        return self.parts[0]

    @property
    def is_local(self):
        return len(self.parts) == 1

    def dotted(self):
        return ".".join(self.parts)

    def paths(self):
        return {self.parts}

    def __repr__(self):
        return "Path(%s)" % self.dotted()


class Compare(Node):
    __slots__ = ("op", "left", "right")

    def __init__(self, op, left, right):
        self.op = op
        self.left = left
        self.right = right

    def paths(self):
        return self.left.paths() | self.right.paths()

    def __repr__(self):
        return "Compare(%s, %r, %r)" % (self.op, self.left, self.right)


class InSet(Node):
    __slots__ = ("path", "values", "negated")

    def __init__(self, path, values, negated=False):
        self.path = path
        self.values = values
        self.negated = negated

    def paths(self):
        return self.path.paths()

    def __repr__(self):
        return "InSet(%r, %r)" % (self.path, self.values)


class Like(Node):
    __slots__ = ("path", "pattern")

    def __init__(self, path, pattern):
        self.path = path
        self.pattern = pattern

    def paths(self):
        return self.path.paths()

    def __repr__(self):
        return "Like(%r, %r)" % (self.path, self.pattern)


class BoolOp(Node):
    __slots__ = ("op", "operands")

    def __init__(self, op, operands):
        self.op = op
        self.operands = operands

    def paths(self):
        out = set()
        for o in self.operands:
            out |= o.paths()
        return out

    def __repr__(self):
        return "BoolOp(%s, %r)" % (self.op, self.operands)


class Not(Node):
    __slots__ = ("operand",)

    def __init__(self, operand):
        self.operand = operand

    def paths(self):
        return self.operand.paths()

    def __repr__(self):
        return "Not(%r)" % (self.operand,)


def _unquote(raw):
    body = raw[1:-1]
    return body.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")


def _lex(text):
    tokens = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if m is None or m.end() == pos:
            raise QueryError("cannot parse filter near %r" % text[pos : pos + 20])
        kind = m.lastgroup
        if kind is None:
            pos = m.end()
            continue
        value = m.group()
        if kind == "IDENT":
            low = value.lower()
            if low in _KEYWORDS:
                tokens.append((low.upper(), low))
            else:
                tokens.append(("IDENT", value))
        elif kind == "STRING":
            tokens.append(("LIT", _unquote(value)))
        elif kind == "INT":
            tokens.append(("LIT", int(value)))
        elif kind == "FLOAT":
            tokens.append(("LIT", float(value)))
        else:
            tokens.append((kind, value))
        pos = m.end()
    tokens.append(("EOF", None))
    return tokens


class _ExprParser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def accept(self, kind):
        if self.peek()[0] == kind:
            return self.next()
        return None

    def expect(self, kind):
        tok = self.next()
        if tok[0] != kind:
            raise QueryError("expected %s in filter, found %r" % (kind, tok[1]))
        return tok

    def parse(self):
        node = self.parse_or()
        if self.peek()[0] != "EOF":
            raise QueryError("trailing input in filter near %r" % (self.peek()[1],))
        return node

    def parse_or(self):
        left = self.parse_and()
        if self.peek()[0] != "OR":
            return left
        operands = [left]
        while self.accept("OR"):
            operands.append(self.parse_and())
        return BoolOp("or", operands)

    def parse_and(self):
        left = self.parse_unary()
        if self.peek()[0] != "AND":
            return left
        operands = [left]
        while self.accept("AND"):
            operands.append(self.parse_unary())
        return BoolOp("and", operands)

    def parse_unary(self):
        if self.accept("NOT"):
            return Not(self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        if self.accept("LPAREN"):
            node = self.parse_or()
            self.expect("RPAREN")
            return node

        left = self.parse_operand()

        kind, value = self.peek()
        if kind == "OP":
            self.next()
            right = self.parse_operand()
            return Compare(value, left, right)
        if kind == "IN":
            self.next()
            self.expect("LBRACK")
            values = []
            if self.peek()[0] != "RBRACK":
                while True:
                    values.append(self.expect("LIT")[1])
                    if not self.accept("COMMA"):
                        break
            self.expect("RBRACK")
            if not isinstance(left, Path):
                raise QueryError("'in' requires a field on the left hand side")
            return InSet(left, values)
        if kind == "LIKE":
            self.next()
            pattern = self.expect("LIT")[1]
            if not isinstance(left, Path):
                raise QueryError("'like' requires a field on the left hand side")
            return Like(left, pattern)

        raise QueryError("expected a comparison operator after %r" % (left,))

    def parse_operand(self):
        kind, value = self.next()
        if kind == "LIT":
            return Literal(value)
        if kind == "TRUE":
            return Literal(True)
        if kind == "FALSE":
            return Literal(False)
        if kind == "NULL":
            return Literal(None)
        if kind == "IDENT":
            return Path(value.split("."))
        raise QueryError("unexpected token %r in filter" % (value,))


def parse_filter(text):
    if text is None:
        return None
    if isinstance(text, Node):
        return text
    text = text.strip()
    if not text:
        return None
    return _ExprParser(_lex(text)).parse()


# -- SQL compilation ---------------------------------------------------

_SQL_OPS = {"==": "=", "!=": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<="}


def to_sql(node, columns):
    """Compile to (sql, params). `columns` maps a local field name to a column.

    Raises QueryError if the expression touches a dotted path, because those
    cannot be evaluated before the join has happened. The planner uses that to
    decide what is pushable.
    """
    params = []
    sql = _sql(node, columns, params)
    return sql, params


def _col(path, columns):
    if not path.is_local:
        raise QueryError("path %s cannot be pushed into a single table" % path.dotted())
    try:
        return columns[path.root]
    except KeyError:
        raise QueryError("unknown field %r" % path.root)


def _sql(node, columns, params):
    if isinstance(node, BoolOp):
        joiner = " AND " if node.op == "and" else " OR "
        return "(" + joiner.join(_sql(o, columns, params) for o in node.operands) + ")"
    if isinstance(node, Not):
        return "(NOT " + _sql(node.operand, columns, params) + ")"
    if isinstance(node, Compare):
        left, right = node.left, node.right
        if isinstance(left, Path) and isinstance(right, Literal):
            if right.value is None:
                return '("%s" IS %sNULL)' % (
                    _col(left, columns),
                    "" if node.op == "==" else "NOT ",
                )
            params.append(right.value)
            return '("%s" %s ?)' % (_col(left, columns), _SQL_OPS[node.op])
        if isinstance(left, Literal) and isinstance(right, Path):
            params.append(left.value)
            return '(? %s "%s")' % (_SQL_OPS[node.op], _col(right, columns))
        if isinstance(left, Path) and isinstance(right, Path):
            return '("%s" %s "%s")' % (
                _col(left, columns),
                _SQL_OPS[node.op],
                _col(right, columns),
            )
        raise QueryError("comparison of two literals is not useful")
    if isinstance(node, InSet):
        if not node.values:
            return "(0 = 1)"
        params.extend(node.values)
        holes = ", ".join("?" for _ in node.values)
        return '("%s" IN (%s))' % (_col(node.path, columns), holes)
    if isinstance(node, Like):
        params.append(node.pattern)
        return '("%s" LIKE ?)' % _col(node.path, columns)
    raise QueryError("cannot compile %r to SQL" % (node,))


# -- Python compilation ------------------------------------------------


def _get(row, parts):
    cur = row
    for part in parts:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
    return cur


def _like_to_regex(pattern):
    out = ["^"]
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    out.append("$")
    return re.compile("".join(out), re.DOTALL)


def to_predicate(node):
    """Compile to a callable taking a row mapping and returning a bool."""
    if node is None:
        return lambda row: True
    return _pred(node)


def _cmp(op, a, b):
    if a is None or b is None:
        if op == "==":
            return a is None and b is None
        if op == "!=":
            return (a is None) != (b is None)
        return False
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "<":
        return a < b
    return a <= b


def _pred(node):
    if isinstance(node, BoolOp):
        subs = [_pred(o) for o in node.operands]
        if node.op == "and":
            return lambda row: all(s(row) for s in subs)
        return lambda row: any(s(row) for s in subs)
    if isinstance(node, Not):
        sub = _pred(node.operand)
        return lambda row: not sub(row)
    if isinstance(node, Compare):
        left, right, op = node.left, node.right, node.op
        if isinstance(left, Path) and isinstance(right, Literal):
            parts, value = left.parts, right.value
            return lambda row: _cmp(op, _get(row, parts), value)
        if isinstance(left, Literal) and isinstance(right, Path):
            parts, value = right.parts, left.value
            return lambda row: _cmp(op, value, _get(row, parts))
        lp, rp = left.parts, right.parts
        return lambda row: _cmp(op, _get(row, lp), _get(row, rp))
    if isinstance(node, InSet):
        parts = node.path.parts
        values = set(node.values)
        return lambda row: _get(row, parts) in values
    if isinstance(node, Like):
        parts = node.path.parts
        rx = _like_to_regex(node.pattern)
        return lambda row: _get(row, parts) is not None and bool(rx.match(str(_get(row, parts))))
    raise QueryError("cannot compile %r to a predicate" % (node,))


def split_pushable(node):
    """Split a filter into (pushable, residual) around top level 'and'.

    A conjunct is pushable when every path it touches is local to the root
    type. Anything referring to a joined type stays behind as a residual
    predicate applied after the join. Returning None for either half means
    "nothing to do", which the planner treats as a free pass.
    """
    if node is None:
        return None, None
    conjuncts = node.operands if isinstance(node, BoolOp) and node.op == "and" else [node]
    push, rest = [], []
    for c in conjuncts:
        if all(len(p) == 1 for p in c.paths()):
            push.append(c)
        else:
            rest.append(c)

    def rebuild(parts):
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return BoolOp("and", parts)

    return rebuild(push), rebuild(rest)
