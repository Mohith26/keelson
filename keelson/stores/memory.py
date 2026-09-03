"""In memory entity store.

This exists to make the model-driven claim testable rather than rhetorical.
It implements the same interface as RelationalStore but keeps rows in plain
dicts and evaluates filters with the Python predicate compiler instead of SQL.
The session can be pointed at either one, and tests assert that the same
application code returns identical results from both.

It also gives the planner a backend with no pushdown at all, which is how the
pushdown benchmark gets an honest baseline.
"""

from ..errors import StoreError
from ..expr import to_predicate
from .relational import _parse_order


class MemoryStore:
    name = "memory"

    def __init__(self, model):
        self.model = model
        self._tables = {t.name: {} for t in model.concrete()}
        self.rows_scanned = 0
        self.queries = 0

    # -- schema --------------------------------------------------------

    def ddl_for(self, rtype):
        return []

    def table_columns(self, table):
        for t in self.model.concrete():
            if t.table == table:
                return [f.column for f in t.stored_fields()]
        return []

    def index_names(self, table):
        return []

    def execute_ddl(self, statements):
        return None

    # -- writes --------------------------------------------------------

    def _rows(self, rtype):
        try:
            return self._tables[rtype.name]
        except KeyError:
            raise StoreError("no table for %s" % rtype.name)

    def _project(self, rtype, row):
        out = {}
        for f in rtype.stored_fields():
            value = row.get(f.name, f.default)
            if f.type_name == "boolean" and value is not None:
                value = bool(value)
            out[f.name] = value
        return out

    def create_batch(self, rtype, rows):
        table = self._rows(rtype)
        for row in rows:
            rid = row.get("id")
            if rid in table:
                raise StoreError("duplicate id %r in %s" % (rid, rtype.name))
            table[rid] = self._project(rtype, row)
        return len(rows)

    def upsert_batch(self, rtype, rows):
        table = self._rows(rtype)
        for row in rows:
            table[row.get("id")] = self._project(rtype, row)
        return len(rows)

    def merge_batch(self, rtype, rows):
        table = self._rows(rtype)
        for row in rows:
            rid = row.get("id")
            incoming = self._project(rtype, row)
            if rid in table:
                existing = dict(table[rid])
                for k, v in incoming.items():
                    if v is not None:
                        existing[k] = v
                table[rid] = existing
            else:
                table[rid] = incoming
        return len(rows)

    def remove(self, rtype, ids):
        table = self._rows(rtype)
        for i in ids:
            table.pop(i, None)

    # -- reads ---------------------------------------------------------

    def fetch(self, rtype, where_sql=None, params=(), order=None, limit=None, offset=0, columns=None):
        # where_sql is ignored on purpose: this backend cannot push anything
        # down, so it receives the filter through `predicate` instead.
        predicate = getattr(self, "_pending_predicate", None)
        self._pending_predicate = None
        rows = list(self._rows(rtype).values())
        self.queries += 1
        self.rows_scanned += len(rows)
        if predicate is not None:
            rows = [r for r in rows if predicate(r)]
        if order:
            for spec in reversed(order):
                field, direction = _parse_order(spec)
                rows.sort(key=lambda r: _sort_key(r.get(field)), reverse=(direction == "DESC"))
        if limit is not None:
            rows = rows[offset : offset + int(limit)]
        if columns is not None:
            wanted = set(columns) | {"id"}
            rows = [{k: v for k, v in r.items() if k in wanted} for r in rows]
        return [dict(r) for r in rows]

    def set_predicate(self, node):
        self._pending_predicate = to_predicate(node) if node is not None else None

    def count(self, rtype, where_sql=None, params=()):
        predicate = getattr(self, "_pending_predicate", None)
        self._pending_predicate = None
        rows = list(self._rows(rtype).values())
        self.queries += 1
        if predicate is not None:
            rows = [r for r in rows if predicate(r)]
        return len(rows)

    def explain(self, rtype, where_sql=None, params=(), order=None):
        return ["SCAN %s (memory store, full scan)" % rtype.table]

    def reset_counters(self):
        self.rows_scanned = 0
        self.queries = 0

    def close(self):
        self._tables.clear()


def _sort_key(value):
    # None sorts before everything, matching SQLite's NULL ordering.
    return (value is not None, value if value is not None else 0)
