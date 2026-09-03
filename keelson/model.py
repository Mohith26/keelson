"""Resolution: turn a parsed Module into a checked, flattened type model.

This is where the platform independent model actually becomes usable. The
parser gives back one record per `entity type` block; resolution has to

  * flatten `extends` chains and `mixes` lists into a single field list per
    type, with a deterministic ordering so generated DDL is stable,
  * reject the things that would silently corrupt a schema later (duplicate
    fields with conflicting types, cycles in the extends graph, references to
    types that do not exist, foreign keys that point at a field which is not
    a stored column),
  * decide, per field, which store owns it. That last decision is the whole
    point of the layer: primitives live in the relational store, timeseries
    fields live in the segment store, and references and collections live in
    neither because they are resolved by the planner at query time.
"""

from .dsl.nodes import PRIMITIVES
from .errors import ResolveError

# Sort order used when flattening so a type's own fields always come after
# inherited ones, and the identifier is always first.
_FIELD_ORDER = ("id",)


class ResolvedField:
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
        "owner",
        "declared_on",
        "line",
    )

    def __init__(self, decl, owner, declared_on):
        self.name = decl.name
        self.type_name = decl.type_name
        self.kind = decl.kind
        self.column = decl.column or decl.name
        self.default = decl.default
        self.fk_local = decl.fk_local
        self.fk_remote = decl.fk_remote
        self.series_key = decl.series_key
        self.value_type = decl.value_type
        self.owner = owner
        self.declared_on = declared_on
        self.line = decl.line

    @property
    def store(self):
        if self.kind == "primitive":
            return "relational"
        if self.kind == "timeseries":
            return "timeseries"
        return "planner"

    def signature(self):
        return (self.kind, self.type_name, self.fk_local, self.fk_remote, self.value_type)

    def __repr__(self):
        return "ResolvedField(%s.%s: %s)" % (self.owner, self.name, self.type_name)


class ResolvedType:
    __slots__ = (
        "name",
        "table",
        "is_abstract",
        "fields",
        "annotations",
        "bases",
        "mixins",
        "line",
    )

    def __init__(self, name, table, is_abstract, fields, annotations, bases, mixins, line):
        self.name = name
        self.table = table
        self.is_abstract = is_abstract
        self.fields = fields
        self.annotations = annotations
        self.bases = bases
        self.mixins = mixins
        self.line = line

    def field(self, name):
        f = self.fields.get(name)
        if f is None:
            raise ResolveError("type %s has no field %r" % (self.name, name))
        return f

    def has(self, name):
        return name in self.fields

    def stored_fields(self):
        return [f for f in self.fields.values() if f.kind == "primitive"]

    def series_fields(self):
        return [f for f in self.fields.values() if f.kind == "timeseries"]

    def references(self):
        return [f for f in self.fields.values() if f.kind == "reference"]

    def collections(self):
        return [f for f in self.fields.values() if f.kind == "collection"]

    def db(self, key, default=None):
        return self.annotations.get("db", {}).get(key, default)

    def indexes(self):
        """Index plan derived from @db annotations, as (name, cols, unique)."""
        out = []
        unique = self.db("unique")
        if unique:
            cols = [c.strip() for c in unique.split(",") if c.strip()]
            out.append(("ux_%s_%s" % (self.table, "_".join(cols)), cols, True))
        index = self.db("index")
        if index:
            cols = [c.strip() for c in index.split(",") if c.strip()]
            out.append(("ix_%s_%s" % (self.table, "_".join(cols)), cols, False))
        return out

    def isa(self, name):
        return self.name == name or name in self.bases

    def __repr__(self):
        return "ResolvedType(%s, %d fields)" % (self.name, len(self.fields))


class Model:
    """A resolved, validated set of entity types."""

    def __init__(self, types, source_name="<model>"):
        self.types = types
        self.source_name = source_name

    def __getitem__(self, name):
        try:
            return self.types[name]
        except KeyError:
            raise ResolveError("unknown type %r" % name)

    def __contains__(self, name):
        return name in self.types

    def __iter__(self):
        return iter(self.types.values())

    def concrete(self):
        return [t for t in self.types.values() if not t.is_abstract]

    def names(self):
        return sorted(self.types)

    def fingerprint(self):
        """Stable structural digest, used by the migration planner."""
        parts = []
        for t in sorted(self.types.values(), key=lambda x: x.name):
            cols = ["%s:%s" % (f.column, f.type_name) for f in t.stored_fields()]
            idx = ["%s(%s)%s" % (n, ",".join(c), "!" if u else "") for n, c, u in t.indexes()]
            parts.append("%s[%s][%s][%s]" % (t.name, t.table, ",".join(sorted(cols)), ",".join(sorted(idx))))
        return "|".join(parts)


def _linearize(name, decls, seen, stack):
    """Return the extends chain for `name`, base first, detecting cycles."""
    if name in stack:
        cycle = " -> ".join(stack[stack.index(name):] + [name])
        raise ResolveError("cyclic type hierarchy: %s" % cycle)
    if name in seen:
        return seen[name]
    decl = decls.get(name)
    if decl is None:
        raise ResolveError("unknown base type %r" % name)
    stack.append(name)
    chain = []
    if decl.extends:
        chain.extend(_linearize(decl.extends, decls, seen, stack))
    chain.append(name)
    stack.pop()
    seen[name] = chain
    return chain


def _merge_field(target, decl, owner, declared_on, order):
    """Insert a field, allowing an exact redeclaration but not a conflicting one."""
    existing = target.get(decl.name)
    incoming = ResolvedField(decl, owner, declared_on)
    if existing is not None:
        if existing.signature() != incoming.signature():
            raise ResolveError(
                "field %r on %s conflicts with the one inherited from %s "
                "(%s vs %s)"
                % (
                    decl.name,
                    owner,
                    existing.declared_on,
                    existing.type_name,
                    incoming.type_name,
                ),
                decl.line,
            )
        # A redeclaration is allowed to override the column name only.
        if decl.column:
            existing.column = decl.column
        return
    target[decl.name] = incoming
    order.append(decl.name)


def resolve(module):
    """Flatten and validate a parsed Module into a Model."""
    decls = {}
    for decl in module.types:
        if decl.name in decls:
            raise ResolveError("duplicate type %r" % decl.name, decl.line)
        decls[decl.name] = decl

    mixin_names = {n for n, d in decls.items() if d.is_mixin}
    resolved = {}
    seen_chains = {}

    for name, decl in decls.items():
        if decl.is_mixin:
            continue
        chain = _linearize(name, decls, seen_chains, [])
        for base in chain:
            if decls[base].is_mixin:
                raise ResolveError(
                    "%s cannot extend mixin %s, use 'mixes' instead" % (name, base),
                    decl.line,
                )

        fields = {}
        order = []
        applied_mixins = []
        for base in chain:
            bdecl = decls[base]
            for mx in bdecl.mixes:
                if mx not in decls:
                    raise ResolveError("unknown mixin %r on %s" % (mx, base), bdecl.line)
                if mx not in mixin_names:
                    raise ResolveError(
                        "%s is not a mixin type, it cannot be mixed into %s" % (mx, base),
                        bdecl.line,
                    )
                if mx in applied_mixins:
                    continue
                applied_mixins.append(mx)
                for f in decls[mx].fields:
                    _merge_field(fields, f, name, mx, order)
            for f in bdecl.fields:
                _merge_field(fields, f, name, base, order)

        ordered = {}
        for key in _FIELD_ORDER:
            if key in fields:
                ordered[key] = fields[key]
        for key in order:
            if key not in ordered:
                ordered[key] = fields[key]

        table = decl.table or _default_table(name)
        resolved[name] = ResolvedType(
            name=name,
            table=table,
            is_abstract=decl.is_abstract,
            fields=ordered,
            annotations=dict(decl.annotations),
            bases=[b for b in chain[:-1]],
            mixins=applied_mixins,
            line=decl.line,
        )

    model = Model(resolved, module.source_name)
    _validate(model)
    return model


def _default_table(name):
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _validate(model):
    tables = {}
    for t in model:
        if t.is_abstract:
            continue
        if t.table in tables:
            raise ResolveError(
                "types %s and %s both map to table %r"
                % (tables[t.table], t.name, t.table),
                t.line,
            )
        tables[t.table] = t.name

    for t in model:
        if not t.has("id"):
            raise ResolveError(
                "type %s has no 'id' field; mix in Persistable or declare one" % t.name,
                t.line,
            )
        if t.field("id").kind != "primitive":
            raise ResolveError("the 'id' field on %s must be a primitive" % t.name, t.line)

        columns = {}
        for f in t.stored_fields():
            if f.type_name not in PRIMITIVES:
                raise ResolveError(
                    "field %s.%s has unknown type %r" % (t.name, f.name, f.type_name),
                    f.line,
                )
            if f.column in columns:
                raise ResolveError(
                    "fields %s and %s on %s both map to column %r"
                    % (columns[f.column], f.name, t.name, f.column),
                    f.line,
                )
            columns[f.column] = f.name

        for f in t.references():
            if f.type_name not in model:
                raise ResolveError(
                    "field %s.%s references unknown type %r"
                    % (t.name, f.name, f.type_name),
                    f.line,
                )
            if not t.has(f.fk_local):
                raise ResolveError(
                    "field %s.%s declares foreign key %r which %s does not have"
                    % (t.name, f.name, f.fk_local, t.name),
                    f.line,
                )
            if t.field(f.fk_local).kind != "primitive":
                raise ResolveError(
                    "foreign key %s.%s must be a stored primitive" % (t.name, f.fk_local),
                    f.line,
                )

        for f in t.collections():
            if f.type_name not in model:
                raise ResolveError(
                    "collection %s.%s has unknown element type %r"
                    % (t.name, f.name, f.type_name),
                    f.line,
                )
            remote = model[f.type_name]
            if not remote.has(f.fk_local):
                raise ResolveError(
                    "collection %s.%s expects %s to have a %r field"
                    % (t.name, f.name, f.type_name, f.fk_local),
                    f.line,
                )
            if remote.field(f.fk_local).kind != "primitive":
                raise ResolveError(
                    "collection key %s.%s must be a stored primitive"
                    % (f.type_name, f.fk_local),
                    f.line,
                )
            if not t.has(f.fk_remote):
                raise ResolveError(
                    "collection %s.%s expects %s to have a %r field"
                    % (t.name, f.name, t.name, f.fk_remote),
                    f.line,
                )

        for f in t.series_fields():
            if f.value_type not in ("double", "int", "long"):
                raise ResolveError(
                    "timeseries %s.%s must hold double, int or long, not %r"
                    % (t.name, f.name, f.value_type),
                    f.line,
                )
            if not t.has(f.series_key):
                raise ResolveError(
                    "timeseries %s.%s keys on %r which %s does not have"
                    % (t.name, f.name, f.series_key, t.name),
                    f.line,
                )

        for name, cols, _unique in t.indexes():
            for c in cols:
                if not t.has(c) or t.field(c).kind != "primitive":
                    raise ResolveError(
                        "index %s on %s names %r which is not a stored column"
                        % (name, t.name, c),
                        t.line,
                    )


def load(source, source_name="<model>"):
    from .dsl.parser import parse

    return resolve(parse(source, source_name))
