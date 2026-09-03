"""Query planner.

A fetch names a root type, a filter, and a list of `include` paths that may
walk references, collections and time series. The planner turns that into a
sequence of store operations and is responsible for three decisions:

1. How much of the filter can be pushed into the relational store. A conjunct
   whose paths are all local to the root type becomes SQL; anything that
   crosses a join stays behind as a Python predicate applied afterwards.
2. How to fetch each included level. Everything is a hash join over a batched
   `IN (...)`, never a per row lookup, because the N+1 pattern is what makes
   naive object mappers unusable on a fleet of any size.
3. Whether ORDER BY and LIMIT can also go down. They can only go down when
   there is no residual predicate, since filtering after the fact would
   invalidate a limit applied before it. Getting this wrong silently returns
   too few rows, so the planner records the decision and the tests assert on it.

Every plan carries a PlanStats record so the benchmark can report what the
pushdown actually bought rather than asserting that it must have helped.
"""

from .errors import QueryError
from .expr import parse_filter, split_pushable, to_predicate, to_sql

# SQLite's default SQLITE_MAX_VARIABLE_NUMBER. Chunk IN lists below it.
_MAX_PARAMS = 900


class PlanStats:
    __slots__ = (
        "pushed_filter",
        "residual_filter",
        "pushed_order",
        "pushed_limit",
        "root_rows",
        "joined_rows",
        "store_queries",
        "series_scanned",
        "series_points",
    )

    def __init__(self):
        self.pushed_filter = None
        self.residual_filter = None
        self.pushed_order = False
        self.pushed_limit = False
        self.root_rows = 0
        self.joined_rows = 0
        self.store_queries = 0
        self.series_scanned = 0
        self.series_points = 0

    def as_dict(self):
        return {
            "pushed_filter": self.pushed_filter,
            "residual_filter": self.residual_filter,
            "pushed_order": self.pushed_order,
            "pushed_limit": self.pushed_limit,
            "root_rows": self.root_rows,
            "joined_rows": self.joined_rows,
            "store_queries": self.store_queries,
            "series_scanned": self.series_scanned,
            "series_points": self.series_points,
        }

    def __repr__(self):
        return "PlanStats(%r)" % (self.as_dict(),)


def _tree(paths):
    """Turn ['site', 'sensors.readings'] into a nested include tree."""
    root = {}
    for path in paths or ():
        node = root
        for part in str(path).split("."):
            node = node.setdefault(part, {})
    return root


def _chunks(values, size=_MAX_PARAMS):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i : i + size]


class Planner:
    def __init__(self, session):
        self.session = session

    # -- entry point ---------------------------------------------------

    def fetch(
        self,
        type_name,
        filter=None,
        include=None,
        order=None,
        limit=None,
        offset=0,
        window=None,
    ):
        model = self.session.model
        rtype = model[type_name]
        stats = PlanStats()

        node = parse_filter(filter)
        pushable, residual = split_pushable(node)
        stats.pushed_filter = _render(pushable)
        stats.residual_filter = _render(residual)

        can_push_order = residual is None
        stats.pushed_order = bool(order) and can_push_order
        stats.pushed_limit = limit is not None and can_push_order

        rows = self._fetch_root(
            rtype,
            pushable,
            order if stats.pushed_order else None,
            limit if stats.pushed_limit else None,
            offset if stats.pushed_limit else 0,
        )
        stats.root_rows = len(rows)

        tree = _tree(include)
        if tree:
            self._expand(rtype, rows, tree, stats, window)

        if residual is not None:
            predicate = to_predicate(residual)
            rows = [r for r in rows if predicate(r)]

        if order and not stats.pushed_order:
            rows = _sort_rows(rows, order)
        if limit is not None and not stats.pushed_limit:
            rows = rows[offset : offset + int(limit)]

        stats.store_queries = self.session.store.queries
        return rows, stats

    # -- root ----------------------------------------------------------

    def _fetch_root(self, rtype, pushable, order, limit, offset):
        store = self.session.store
        if hasattr(store, "set_predicate"):
            store.set_predicate(pushable)
            return store.fetch(rtype, None, (), order=order, limit=limit, offset=offset)
        where, params = _compile(rtype, pushable)
        return store.fetch(rtype, where, params, order=order, limit=limit, offset=offset)

    def _fetch_where_in(self, target_name, field_name, values):
        """Batched `field IN (...)`, fanned out over concrete subtypes.

        Returns a list of (rtype, rows) pairs rather than one flat list,
        because a nested include has to be expanded against the type each row
        actually came from. Keys are chunked below SQLite's bound parameter
        cap, so a join over tens of thousands of parents is still a handful of
        statements rather than one per row.
        """
        store = self.session.store
        keys = sorted(set(v for v in values if v is not None))
        out = []
        for rtype in self.session.model.concrete_subtypes(target_name):
            column = rtype.field(field_name).column
            rows = []
            for chunk in _chunks(keys):
                if hasattr(store, "set_predicate"):
                    wanted = set(chunk)
                    store.set_predicate(None)
                    rows.extend(r for r in store.fetch(rtype, None, ()) if r.get(field_name) in wanted)
                else:
                    holes = ", ".join("?" for _ in chunk)
                    rows.extend(
                        store.fetch(rtype, '"%s" IN (%s)' % (column, holes), tuple(chunk))
                    )
            out.append((rtype, rows))
        return out

    # -- includes ------------------------------------------------------

    def _expand(self, rtype, rows, tree, stats, window):
        if not rows:
            return
        for name, subtree in tree.items():
            if not rtype.has(name):
                raise QueryError("type %s has no field %r to include" % (rtype.name, name))
            field = rtype.field(name)
            if field.kind == "reference":
                self._join_reference(rtype, field, rows, subtree, stats, window)
            elif field.kind == "collection":
                self._join_collection(rtype, field, rows, subtree, stats, window)
            elif field.kind == "timeseries":
                self._attach_series(rtype, field, rows, stats, window)
            else:
                raise QueryError(
                    "field %s.%s is a stored column, it cannot be included"
                    % (rtype.name, name)
                )

    def _join_reference(self, rtype, field, rows, subtree, stats, window):
        keys = [r.get(field.fk_local) for r in rows]
        groups = self._fetch_where_in(field.type_name, "id", keys)
        index = {}
        for _remote, remote_rows in groups:
            stats.joined_rows += len(remote_rows)
            for r in remote_rows:
                index[r["id"]] = r
        for r in rows:
            r[field.name] = index.get(r.get(field.fk_local))
        if subtree:
            for remote, remote_rows in groups:
                self._expand(remote, remote_rows, subtree, stats, window)

    def _join_collection(self, rtype, field, rows, subtree, stats, window):
        keys = [r.get(field.fk_remote) for r in rows]
        groups = self._fetch_where_in(field.type_name, field.fk_local, keys)
        buckets = {}
        for _remote, remote_rows in groups:
            stats.joined_rows += len(remote_rows)
            for r in remote_rows:
                buckets.setdefault(r.get(field.fk_local), []).append(r)
        for r in rows:
            r[field.name] = buckets.get(r.get(field.fk_remote), [])
        if subtree:
            for remote, remote_rows in groups:
                self._expand(remote, remote_rows, subtree, stats, window)

    def _attach_series(self, rtype, field, rows, stats, window):
        ts = self.session.timeseries
        start, end = (window or (None, None))
        before_segments = ts.segments_scanned
        before_points = ts.points_decoded
        for r in rows:
            key = r.get(field.series_key)
            r[field.name] = ts.range(rtype.name, field.name, key, start, end)
        stats.series_scanned += ts.segments_scanned - before_segments
        stats.series_points += ts.points_decoded - before_points


def _compile(rtype, node):
    if node is None:
        return None, ()
    columns = {f.name: f.column for f in rtype.stored_fields()}
    return to_sql(node, columns)


def _render(node):
    return None if node is None else repr(node)


def _sort_rows(rows, order):
    from .stores.relational import _parse_order

    out = list(rows)
    for spec in reversed(order):
        field, direction = _parse_order(spec)
        out.sort(
            key=lambda r: (r.get(field) is not None, r.get(field) if r.get(field) is not None else 0),
            reverse=(direction == "DESC"),
        )
    return out
