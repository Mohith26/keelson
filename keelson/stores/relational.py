"""SQLite backed entity store.

The store never sees the DSL. It is handed a ResolvedType and works entirely
from the resolved column names, which is what lets the same application code
run against the in memory store in memory.py without any change: both classes
implement the same six methods.

Batch writes go through executemany inside a single transaction because the
alternative, one autocommit statement per row, dominates the runtime of any
realistic ingest. upsert and merge differ in their treatment of a None: an
upsert overwrites a column with NULL, a merge leaves the stored value alone.
"""

import sqlite3

from ..errors import StoreError

_SQL_TYPES = {
    "string": "TEXT",
    "int": "INTEGER",
    "long": "INTEGER",
    "double": "REAL",
    "boolean": "INTEGER",
    "datetime": "INTEGER",
}


class RelationalStore:
    name = "relational"

    def __init__(self, model, path=":memory:"):
        self.model = model
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.row_factory = sqlite3.Row
        self.rows_scanned = 0
        self.queries = 0
        self._create_all()

    # -- schema --------------------------------------------------------

    def ddl_for(self, rtype):
        cols = []
        for f in rtype.stored_fields():
            sqltype = _SQL_TYPES[f.type_name]
            piece = '"%s" %s' % (f.column, sqltype)
            if f.name == "id":
                piece += " PRIMARY KEY"
            if f.default is not None:
                piece += " DEFAULT %s" % _literal(f.default)
            cols.append(piece)
        stmts = ['CREATE TABLE IF NOT EXISTS "%s" (\n  %s\n)' % (rtype.table, ",\n  ".join(cols))]
        for index_name, fields, unique in rtype.indexes():
            columns = ", ".join('"%s"' % rtype.field(f).column for f in fields)
            stmts.append(
                'CREATE %sINDEX IF NOT EXISTS "%s" ON "%s" (%s)'
                % ("UNIQUE " if unique else "", index_name, rtype.table, columns)
            )
        return stmts

    def _create_all(self):
        cur = self.conn.cursor()
        for rtype in self.model.concrete():
            for stmt in self.ddl_for(rtype):
                cur.execute(stmt)
        self.conn.commit()

    def table_columns(self, table):
        rows = self.conn.execute('PRAGMA table_info("%s")' % table).fetchall()
        return [r["name"] for r in rows]

    def index_names(self, table):
        rows = self.conn.execute('PRAGMA index_list("%s")' % table).fetchall()
        return sorted(r["name"] for r in rows)

    def execute_ddl(self, statements):
        cur = self.conn.cursor()
        for stmt in statements:
            cur.execute(stmt)
        self.conn.commit()

    # -- writes --------------------------------------------------------

    def _columns(self, rtype):
        return [(f.name, f.column) for f in rtype.stored_fields()]

    def create_batch(self, rtype, rows):
        pairs = self._columns(rtype)
        cols = ", ".join('"%s"' % c for _, c in pairs)
        holes = ", ".join("?" for _ in pairs)
        sql = 'INSERT INTO "%s" (%s) VALUES (%s)' % (rtype.table, cols, holes)
        payload = [tuple(_encode(row.get(n)) for n, _ in pairs) for row in rows]
        try:
            self.conn.executemany(sql, payload)
        except sqlite3.IntegrityError as e:
            raise StoreError("insert into %s failed: %s" % (rtype.table, e))
        self.conn.commit()
        return len(payload)

    def upsert_batch(self, rtype, rows):
        pairs = self._columns(rtype)
        cols = ", ".join('"%s"' % c for _, c in pairs)
        holes = ", ".join("?" for _ in pairs)
        updates = ", ".join(
            '"%s" = excluded."%s"' % (c, c) for n, c in pairs if n != "id"
        )
        sql = 'INSERT INTO "%s" (%s) VALUES (%s) ON CONFLICT("%s") DO UPDATE SET %s' % (
            rtype.table,
            cols,
            holes,
            rtype.field("id").column,
            updates,
        )
        payload = [tuple(_encode(row.get(n)) for n, _ in pairs) for row in rows]
        self.conn.executemany(sql, payload)
        self.conn.commit()
        return len(payload)

    def merge_batch(self, rtype, rows):
        """Like upsert, but a None in the incoming row keeps the stored value."""
        pairs = self._columns(rtype)
        cols = ", ".join('"%s"' % c for _, c in pairs)
        holes = ", ".join("?" for _ in pairs)
        updates = ", ".join(
            '"%s" = COALESCE(excluded."%s", "%s"."%s")' % (c, c, rtype.table, c)
            for n, c in pairs
            if n != "id"
        )
        sql = 'INSERT INTO "%s" (%s) VALUES (%s) ON CONFLICT("%s") DO UPDATE SET %s' % (
            rtype.table,
            cols,
            holes,
            rtype.field("id").column,
            updates,
        )
        payload = [tuple(_encode(row.get(n)) for n, _ in pairs) for row in rows]
        self.conn.executemany(sql, payload)
        self.conn.commit()
        return len(payload)

    def remove(self, rtype, ids):
        idcol = rtype.field("id").column
        self.conn.executemany(
            'DELETE FROM "%s" WHERE "%s" = ?' % (rtype.table, idcol), [(i,) for i in ids]
        )
        self.conn.commit()

    # -- reads ---------------------------------------------------------

    def fetch(self, rtype, where_sql=None, params=(), order=None, limit=None, offset=0, columns=None):
        pairs = self._columns(rtype)
        if columns is not None:
            wanted = set(columns) | {"id"}
            pairs = [p for p in pairs if p[0] in wanted]
        select = ", ".join('"%s" AS "%s"' % (c, n) for n, c in pairs)
        sql = 'SELECT %s FROM "%s"' % (select, rtype.table)
        if where_sql:
            sql += " WHERE " + where_sql
        if order:
            parts = []
            for spec in order:
                field, direction = _parse_order(spec)
                parts.append('"%s" %s' % (rtype.field(field).column, direction))
            sql += " ORDER BY " + ", ".join(parts)
        if limit is not None:
            sql += " LIMIT %d" % int(limit)
            if offset:
                sql += " OFFSET %d" % int(offset)
        self.queries += 1
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        self.rows_scanned += len(rows)
        out = []
        for r in rows:
            row = {}
            for n, _c in pairs:
                row[n] = _decode(rtype.field(n).type_name, r[n])
            out.append(row)
        return out

    def count(self, rtype, where_sql=None, params=()):
        sql = 'SELECT COUNT(*) AS n FROM "%s"' % rtype.table
        if where_sql:
            sql += " WHERE " + where_sql
        self.queries += 1
        return self.conn.execute(sql, tuple(params)).fetchone()["n"]

    def explain(self, rtype, where_sql=None, params=(), order=None):
        sql = 'SELECT * FROM "%s"' % rtype.table
        if where_sql:
            sql += " WHERE " + where_sql
        if order:
            parts = []
            for spec in order:
                field, direction = _parse_order(spec)
                parts.append('"%s" %s' % (rtype.field(field).column, direction))
            sql += " ORDER BY " + ", ".join(parts)
        rows = self.conn.execute("EXPLAIN QUERY PLAN " + sql, tuple(params)).fetchall()
        return [r["detail"] for r in rows]

    def reset_counters(self):
        self.rows_scanned = 0
        self.queries = 0

    def close(self):
        self.conn.close()


def _parse_order(spec):
    if isinstance(spec, (tuple, list)):
        return spec[0], ("DESC" if str(spec[1]).lower().startswith("desc") else "ASC")
    text = str(spec).strip()
    if text.startswith("-"):
        return text[1:], "DESC"
    return text, "ASC"


def _encode(value):
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def _decode(type_name, value):
    if value is None:
        return None
    if type_name == "boolean":
        return bool(value)
    return value


def _literal(value):
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)
