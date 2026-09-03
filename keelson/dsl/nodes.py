"""AST produced by the parser.

These are deliberately dumb records. Everything interesting (inheritance
flattening, foreign key wiring, validation) happens later in model.py so that
the parser stays a pure syntax layer.
"""

PRIMITIVES = {"string", "int", "long", "double", "boolean", "datetime"}


class FieldDecl:
    __slots__ = (
        "name",
        "type_name",
        "kind",
        "column",
        "default",
        "fk_local",
        "fk_remote",
        "series_key",
        "value_type",
        "line",
    )

    # kind is one of: primitive, reference, collection, timeseries
    def __init__(
        self,
        name,
        type_name,
        kind,
        column=None,
        default=None,
        fk_local=None,
        fk_remote=None,
        series_key=None,
        value_type=None,
        line=0,
    ):
        self.name = name
        self.type_name = type_name
        self.kind = kind
        self.column = column
        self.default = default
        # For a reference field, fk_local is the field on THIS type holding the
        # foreign key. For a collection, fk_local is the field on the REMOTE
        # type and fk_remote is the field on this type it points at.
        self.fk_local = fk_local
        self.fk_remote = fk_remote
        self.series_key = series_key
        self.value_type = value_type
        self.line = line

    @property
    def is_stored(self):
        """True when the field occupies a column in the relational table."""
        return self.kind == "primitive"

    def __repr__(self):
        return "FieldDecl(%s: %s [%s])" % (self.name, self.type_name, self.kind)


class TypeDecl:
    __slots__ = (
        "name",
        "is_mixin",
        "is_abstract",
        "extends",
        "mixes",
        "table",
        "fields",
        "annotations",
        "line",
    )

    def __init__(
        self,
        name,
        is_mixin=False,
        is_abstract=False,
        extends=None,
        mixes=None,
        table=None,
        fields=None,
        annotations=None,
        line=0,
    ):
        self.name = name
        self.is_mixin = is_mixin
        self.is_abstract = is_abstract
        self.extends = extends
        self.mixes = mixes or []
        self.table = table
        self.fields = fields or []
        self.annotations = annotations or {}
        self.line = line

    def __repr__(self):
        return "TypeDecl(%s, fields=%d)" % (self.name, len(self.fields))


class Module:
    __slots__ = ("types", "source_name")

    def __init__(self, types, source_name="<model>"):
        self.types = types
        self.source_name = source_name

    def by_name(self):
        return {t.name: t for t in self.types}
