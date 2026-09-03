"""Hand written tokenizer for the keelson type DSL.

The grammar is small enough that a regex-per-token scanner is clearer than a
generated lexer, and it lets every token carry a line/col so ModelError can
point back into the .ks source.
"""

import re

from ..errors import LexError

# `name` is deliberately absent. It only means something directly after
# `schema`, and making it a reserved word here would ban the single most
# common field name in any real model. The parser treats it contextually.
KEYWORDS = {
    "entity",
    "mixin",
    "abstract",
    "type",
    "extends",
    "mixes",
    "schema",
    "timeseries",
    "true",
    "false",
    "null",
}

# Order matters: longer operators must be tried before their prefixes.
_TOKEN_SPEC = [
    ("WS", r"[ \t\r]+"),
    ("NEWLINE", r"\n"),
    ("COMMENT", r"//[^\n]*"),
    ("BLOCKCOMMENT", r"/\*[\s\S]*?\*/"),
    ("FLOAT", r"-?\d+\.\d+"),
    ("INT", r"-?\d+"),
    ("STRING", r'"(?:[^"\\]|\\.)*"'),
    ("IDENT", r"[A-Za-z_][A-Za-z_0-9]*"),
    ("AT", r"@"),
    ("LBRACE", r"\{"),
    ("RBRACE", r"\}"),
    ("LBRACK", r"\["),
    ("RBRACK", r"\]"),
    ("LPAREN", r"\("),
    ("RPAREN", r"\)"),
    ("LT", r"<"),
    ("GT", r">"),
    ("COLON", r":"),
    ("COMMA", r","),
    ("EQ", r"="),
]

_MASTER = re.compile("|".join("(?P<%s>%s)" % pair for pair in _TOKEN_SPEC))

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


class Token:
    __slots__ = ("kind", "value", "line", "col")

    def __init__(self, kind, value, line, col):
        self.kind = kind
        self.value = value
        self.line = line
        self.col = col

    def __repr__(self):
        return "Token(%s, %r, %d:%d)" % (self.kind, self.value, self.line, self.col)

    def __eq__(self, other):
        if not isinstance(other, Token):
            return NotImplemented
        return (self.kind, self.value) == (other.kind, other.value)


def _unescape(raw):
    body = raw[1:-1]
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def tokenize(source, filename="<model>"):
    """Turn DSL source text into a list of Token, ending with an EOF token."""
    tokens = []
    line = 1
    line_start = 0
    pos = 0
    n = len(source)
    while pos < n:
        m = _MASTER.match(source, pos)
        if m is None:
            raise LexError(
                "unexpected character %r in %s" % (source[pos], filename),
                line,
                pos - line_start + 1,
            )
        kind = m.lastgroup
        text = m.group()
        col = pos - line_start + 1
        if kind == "NEWLINE":
            line += 1
            line_start = m.end()
        elif kind in ("WS", "COMMENT"):
            pass
        elif kind == "BLOCKCOMMENT":
            newlines = text.count("\n")
            if newlines:
                line += newlines
                line_start = m.start() + text.rfind("\n") + 1
        elif kind == "STRING":
            tokens.append(Token("STRING", _unescape(text), line, col))
        elif kind == "INT":
            tokens.append(Token("INT", int(text), line, col))
        elif kind == "FLOAT":
            tokens.append(Token("FLOAT", float(text), line, col))
        elif kind == "IDENT":
            if text in KEYWORDS:
                tokens.append(Token(text.upper(), text, line, col))
            else:
                tokens.append(Token("IDENT", text, line, col))
        else:
            tokens.append(Token(kind, text, line, col))
        pos = m.end()
    tokens.append(Token("EOF", None, line, pos - line_start + 1))
    return tokens
