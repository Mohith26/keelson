"""Recursive descent parser for the keelson type DSL.

Grammar, informally:

    module      := typedecl*
    typedecl    := annotation* ('mixin' | 'abstract'? 'entity') 'type' IDENT
                   ('extends' IDENT)? ('mixes' IDENT (',' IDENT)*)?
                   ('schema' 'name' STRING)? '{' field* '}'
    annotation  := '@' IDENT '(' (IDENT '=' literal (',' IDENT '=' literal)*)? ')'
    field       := IDENT ':' fieldtype ('schema' 'name' STRING)? ('=' literal)?
    fieldtype   := IDENT                                  -- primitive
                 | IDENT '(' IDENT ')'                    -- many to one reference
                 | '[' IDENT ']' '(' IDENT ',' IDENT ')'  -- one to many collection
                 | 'timeseries' '<' IDENT '>' '(' IDENT ')'
"""

from ..errors import ParseError
from .lexer import tokenize
from .nodes import FieldDecl, Module, TypeDecl


class Parser:
    def __init__(self, tokens, source_name="<model>"):
        self.tokens = tokens
        self.pos = 0
        self.source_name = source_name

    # -- token helpers -------------------------------------------------

    def peek(self, offset=0):
        idx = self.pos + offset
        if idx >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[idx]

    def at(self, kind):
        return self.peek().kind == kind

    def advance(self):
        tok = self.peek()
        self.pos += 1
        return tok

    def expect(self, kind, what=None):
        tok = self.peek()
        if tok.kind != kind:
            raise ParseError(
                "expected %s but found %s"
                % (what or kind, tok.value if tok.value is not None else "end of file"),
                tok.line,
                tok.col,
            )
        return self.advance()

    def accept(self, kind):
        if self.at(kind):
            return self.advance()
        return None

    # -- grammar -------------------------------------------------------

    def parse_module(self):
        types = []
        while not self.at("EOF"):
            types.append(self.parse_typedecl())
        return Module(types, self.source_name)

    def parse_typedecl(self):
        annotations = {}
        while self.at("AT"):
            key, value = self.parse_annotation()
            if key in annotations:
                annotations[key].update(value)
            else:
                annotations[key] = value

        start = self.peek()
        is_mixin = False
        is_abstract = False
        if self.accept("MIXIN"):
            is_mixin = True
        else:
            if self.accept("ABSTRACT"):
                is_abstract = True
            self.expect("ENTITY", "'entity' or 'mixin'")
        self.expect("TYPE", "'type'")
        name = self.expect("IDENT", "a type name").value

        extends = None
        if self.accept("EXTENDS"):
            extends = self.expect("IDENT", "a base type name").value

        mixes = []
        if self.accept("MIXES"):
            mixes.append(self.expect("IDENT", "a mixin name").value)
            while self.accept("COMMA"):
                mixes.append(self.expect("IDENT", "a mixin name").value)

        table = None
        if self.at("SCHEMA"):
            table = self.parse_schema_name()

        self.expect("LBRACE", "'{'")
        fields = []
        while not self.at("RBRACE"):
            if self.at("EOF"):
                raise ParseError("unterminated type body", start.line, start.col)
            fields.append(self.parse_field())
        self.expect("RBRACE", "'}'")

        return TypeDecl(
            name=name,
            is_mixin=is_mixin,
            is_abstract=is_abstract,
            extends=extends,
            mixes=mixes,
            table=table,
            fields=fields,
            annotations=annotations,
            line=start.line,
        )

    def parse_annotation(self):
        self.expect("AT")
        key = self.expect("IDENT", "an annotation name").value
        args = {}
        if self.accept("LPAREN"):
            if not self.at("RPAREN"):
                while True:
                    argname = self.expect("IDENT", "an annotation argument").value
                    self.expect("EQ", "'='")
                    args[argname] = self.parse_literal()
                    if not self.accept("COMMA"):
                        break
            self.expect("RPAREN", "')'")
        return key, args

    def parse_schema_name(self):
        self.expect("SCHEMA")
        self.expect("NAME", "'name' after 'schema'")
        return self.expect("STRING", "a quoted schema name").value

    def parse_literal(self):
        tok = self.peek()
        if tok.kind in ("STRING", "INT", "FLOAT"):
            return self.advance().value
        if tok.kind == "TRUE":
            self.advance()
            return True
        if tok.kind == "FALSE":
            self.advance()
            return False
        if tok.kind == "NULL":
            self.advance()
            return None
        raise ParseError("expected a literal value", tok.line, tok.col)

    def parse_field(self):
        name_tok = self.expect("IDENT", "a field name")
        self.expect("COLON", "':'")
        spec = self.parse_fieldtype(name_tok)

        column = None
        if self.at("SCHEMA"):
            column = self.parse_schema_name()

        default = None
        if self.accept("EQ"):
            default = self.parse_literal()

        spec.name = name_tok.value
        spec.column = column
        spec.default = default
        spec.line = name_tok.line
        return spec

    def parse_fieldtype(self, name_tok):
        if self.at("LBRACK"):
            self.advance()
            target = self.expect("IDENT", "a collection element type").value
            self.expect("RBRACK", "']'")
            self.expect("LPAREN", "'(' with the remote and local key fields")
            remote_key = self.expect("IDENT", "the foreign key field on " + target).value
            self.expect("COMMA", "','")
            local_key = self.expect("IDENT", "the key field on this type").value
            self.expect("RPAREN", "')'")
            return FieldDecl(
                name=None,
                type_name=target,
                kind="collection",
                fk_local=remote_key,
                fk_remote=local_key,
                line=name_tok.line,
            )

        if self.at("TIMESERIES"):
            self.advance()
            self.expect("LT", "'<'")
            value_type = self.expect("IDENT", "a time series value type").value
            self.expect("GT", "'>'")
            self.expect("LPAREN", "'(' with the series key field")
            series_key = self.expect("IDENT", "the series key field").value
            self.expect("RPAREN", "')'")
            return FieldDecl(
                name=None,
                type_name="timeseries",
                kind="timeseries",
                series_key=series_key,
                value_type=value_type,
                line=name_tok.line,
            )

        type_name = self.expect("IDENT", "a field type").value
        if self.at("LPAREN"):
            self.advance()
            fk = self.expect("IDENT", "the local foreign key field").value
            self.expect("RPAREN", "')'")
            return FieldDecl(
                name=None,
                type_name=type_name,
                kind="reference",
                fk_local=fk,
                line=name_tok.line,
            )

        return FieldDecl(
            name=None, type_name=type_name, kind="primitive", line=name_tok.line
        )


def parse(source, source_name="<model>"):
    return Parser(tokenize(source, source_name), source_name).parse_module()
