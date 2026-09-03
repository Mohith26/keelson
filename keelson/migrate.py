"""Schema migration planning.

Editing the .ks model is the normal way to change an application, so the
question "what does this edit do to a database that already has rows in it"
has to have a precise answer. This module diffs two resolved models and emits
an ordered list of Change records, each of which knows whether it is safe.

Safe, and applied automatically:
  * a new type          -> CREATE TABLE
  * a new column        -> ALTER TABLE ADD COLUMN
  * a new or changed index
  * a widened column    -> int to long, int/long to double

Unsafe, and refused unless allow_destructive=True:
  * dropping a type or a column, which throws away rows
  * narrowing a column, e.g. double to int, which silently truncates
  * renaming the table a type maps to, which orphans the old one

SQLite cannot change a column's declared type in place, so a narrowing or a
rename is reported rather than attempted. That is a real limitation of the
backend and the plan says so instead of pretending otherwise.
"""

from .errors import MigrationError

# Widening lattice: a value of the key type fits losslessly into any of the
# listed types.
_WIDENS_TO = {
    "int": ("int", "long", "double"),
    "long": ("long", "double"),
    "double": ("double",),
    "string": ("string",),
    "boolean": ("boolean", "int", "long"),
    "datetime": ("datetime", "long"),
}


class Change:
    __slots__ = ("kind", "target", "detail", "safe", "sql")

    def __init__(self, kind, target, detail, safe, sql=None):
        self.kind = kind
        self.target = target
        self.detail = detail
        self.safe = safe
        self.sql = sql or []

    def __repr__(self):
        return "Change(%s %s: %s%s)" % (
            self.kind,
            self.target,
            self.detail,
            "" if self.safe else " [UNSAFE]",
        )

    def as_dict(self):
        return {
            "kind": self.kind,
            "target": self.target,
            "detail": self.detail,
            "safe": self.safe,
            "sql": list(self.sql),
        }


class Plan:
    def __init__(self, changes):
        self.changes = changes

    @property
    def is_empty(self):
        return not self.changes

    @property
    def is_safe(self):
        return all(c.safe for c in self.changes)

    def unsafe(self):
        return [c for c in self.changes if not c.safe]

    def statements(self):
        out = []
        for c in self.changes:
            out.extend(c.sql)
        return out

    def summary(self):
        return [repr(c) for c in self.changes]

    def __len__(self):
        return len(self.changes)

    def __iter__(self):
        return iter(self.changes)


_SQL_TYPES = {
    "string": "TEXT",
    "int": "INTEGER",
    "long": "INTEGER",
    "double": "REAL",
    "boolean": "INTEGER",
    "datetime": "INTEGER",
}


def diff(old, new):
    """Return a Plan describing how to get from model `old` to model `new`."""
    changes = []
    old_types = {t.name: t for t in old.concrete()}
    new_types = {t.name: t for t in new.concrete()}

    for name in sorted(set(new_types) - set(old_types)):
        t = new_types[name]
        cols = []
        for f in t.stored_fields():
            piece = '"%s" %s' % (f.column, _SQL_TYPES[f.type_name])
            if f.name == "id":
                piece += " PRIMARY KEY"
            cols.append(piece)
        sql = ['CREATE TABLE "%s" (\n  %s\n)' % (t.table, ",\n  ".join(cols))]
        sql.extend(_index_sql(t))
        changes.append(Change("create_type", name, "new type %s" % name, True, sql))

    for name in sorted(set(old_types) - set(new_types)):
        t = old_types[name]
        changes.append(
            Change(
                "drop_type",
                name,
                "type %s was removed; dropping table %s discards its rows" % (name, t.table),
                False,
                ['DROP TABLE "%s"' % t.table],
            )
        )

    for name in sorted(set(old_types) & set(new_types)):
        told, tnew = old_types[name], new_types[name]
        if told.table != tnew.table:
            changes.append(
                Change(
                    "rename_table",
                    name,
                    "table for %s changed from %s to %s; SQLite cannot do this "
                    "without a copy" % (name, told.table, tnew.table),
                    False,
                    ['ALTER TABLE "%s" RENAME TO "%s"' % (told.table, tnew.table)],
                )
            )

        old_cols = {f.column: f for f in told.stored_fields()}
        new_cols = {f.column: f for f in tnew.stored_fields()}

        for col in sorted(set(new_cols) - set(old_cols)):
            f = new_cols[col]
            stmt = 'ALTER TABLE "%s" ADD COLUMN "%s" %s' % (
                tnew.table,
                col,
                _SQL_TYPES[f.type_name],
            )
            if f.default is not None:
                stmt += " DEFAULT %s" % _literal(f.default)
            changes.append(
                Change("add_column", "%s.%s" % (name, f.name), "new column %s" % col, True, [stmt])
            )

        for col in sorted(set(old_cols) - set(new_cols)):
            changes.append(
                Change(
                    "drop_column",
                    "%s.%s" % (name, old_cols[col].name),
                    "column %s was removed; its data would be lost" % col,
                    False,
                    ['ALTER TABLE "%s" DROP COLUMN "%s"' % (tnew.table, col)],
                )
            )

        for col in sorted(set(old_cols) & set(new_cols)):
            fo, fn = old_cols[col], new_cols[col]
            if fo.type_name == fn.type_name:
                continue
            if fn.type_name in _WIDENS_TO.get(fo.type_name, ()):
                changes.append(
                    Change(
                        "widen_column",
                        "%s.%s" % (name, fn.name),
                        "%s widened from %s to %s" % (col, fo.type_name, fn.type_name),
                        True,
                        [],
                    )
                )
            else:
                changes.append(
                    Change(
                        "narrow_column",
                        "%s.%s" % (name, fn.name),
                        "%s changed from %s to %s, which can truncate stored values"
                        % (col, fo.type_name, fn.type_name),
                        False,
                        [],
                    )
                )

        old_idx = {n: (tuple(c), u) for n, c, u in told.indexes()}
        new_idx = {n: (tuple(c), u) for n, c, u in tnew.indexes()}
        for n in sorted(set(new_idx) - set(old_idx)):
            cols, unique = new_idx[n]
            changes.append(
                Change(
                    "add_index",
                    "%s.%s" % (name, n),
                    "index on %s" % ", ".join(cols),
                    True,
                    _one_index_sql(tnew, n, cols, unique),
                )
            )
        for n in sorted(set(old_idx) - set(new_idx)):
            changes.append(
                Change("drop_index", "%s.%s" % (name, n), "index removed", True, ['DROP INDEX "%s"' % n])
            )
        for n in sorted(set(old_idx) & set(new_idx)):
            if old_idx[n] != new_idx[n]:
                cols, unique = new_idx[n]
                changes.append(
                    Change(
                        "rebuild_index",
                        "%s.%s" % (name, n),
                        "index definition changed",
                        True,
                        ['DROP INDEX "%s"' % n] + _one_index_sql(tnew, n, cols, unique),
                    )
                )

    return Plan(changes)


def _index_sql(rtype):
    out = []
    for n, cols, unique in rtype.indexes():
        out.extend(_one_index_sql(rtype, n, cols, unique))
    return out


def _one_index_sql(rtype, name, cols, unique):
    columns = ", ".join('"%s"' % rtype.field(c).column for c in cols)
    return [
        'CREATE %sINDEX "%s" ON "%s" (%s)'
        % ("UNIQUE " if unique else "", name, rtype.table, columns)
    ]


def _literal(value):
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def apply(session, new_model, allow_destructive=False):
    """Migrate a live session's store from its model to `new_model`."""
    plan = diff(session.model, new_model)
    if not plan.is_safe and not allow_destructive:
        detail = "; ".join(c.detail for c in plan.unsafe())
        raise MigrationError("refusing to apply an unsafe migration: %s" % detail)
    statements = plan.statements()
    if statements:
        session.store.execute_ddl(statements)
    session.model = new_model
    session.store.model = new_model
    session._proxies.clear()
    return plan
