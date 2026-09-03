"""The public API.

A Session binds a resolved Model to one entity store and one time series
store, and hands out a TypeProxy per type. Application code only ever talks to
the proxy:

    ses = keelson.open_session(model_source)
    ses.type("Turbine").create([{...}])
    hot = ses.type("Turbine").fetch(
        filter='status == "ACTIVE" and ratedPowerKw > 2000',
        include=["site", "sensors.readings"],
        order=["-ratedPowerKw"],
        limit=10,
    )

Nothing in that snippet names SQLite, a table, a column or a segment file, so
swapping the entity store is a one line change at construction time. The
tests exercise exactly that by running the same script against both backends
and comparing results.
"""

from . import agg
from .errors import QueryError, StoreError
from .expr import parse_filter, split_pushable, to_predicate
from .model import Model, load
from .planner import Planner, _compile
from .stores.memory import MemoryStore
from .stores.relational import RelationalStore
from .stores.tsdb import TimeSeriesStore


class TypeProxy:
    """Everything an application does to one entity type."""

    def __init__(self, session, rtype):
        self._session = session
        self._type = rtype

    @property
    def name(self):
        return self._type.name

    @property
    def type(self):
        return self._type

    # -- writes --------------------------------------------------------

    def create(self, rows):
        rows = _as_rows(rows)
        self._check_fields(rows)
        return self._session.store.create_batch(self._type, rows)

    def upsert(self, rows):
        rows = _as_rows(rows)
        self._check_fields(rows)
        return self._session.store.upsert_batch(self._type, rows)

    def merge(self, rows):
        rows = _as_rows(rows)
        self._check_fields(rows)
        return self._session.store.merge_batch(self._type, rows)

    def remove(self, ids):
        if not isinstance(ids, (list, tuple, set)):
            ids = [ids]
        return self._session.store.remove(self._type, list(ids))

    def _check_fields(self, rows):
        stored = {f.name for f in self._type.stored_fields()}
        for row in rows:
            unknown = set(row) - stored
            if unknown:
                raise QueryError(
                    "unknown field(s) %s on %s"
                    % (", ".join(sorted(unknown)), self._type.name)
                )
            if row.get("id") is None:
                raise StoreError("every %s row needs an id" % self._type.name)

    # -- time series ---------------------------------------------------

    def append_series(self, field_name, key, points):
        field = self._type.field(field_name)
        if field.kind != "timeseries":
            raise QueryError("%s.%s is not a time series" % (self._type.name, field_name))
        self._session.timeseries.append_many(self._type.name, field_name, key, points)
        return len(points)

    def series(self, field_name, key, start=None, end=None):
        field = self._type.field(field_name)
        if field.kind != "timeseries":
            raise QueryError("%s.%s is not a time series" % (self._type.name, field_name))
        return self._session.timeseries.range(self._type.name, field_name, key, start, end)

    # -- reads ---------------------------------------------------------

    def fetch(self, filter=None, include=None, order=None, limit=None, offset=0, window=None):
        rows, stats = self._session.planner.fetch(
            self._type.name,
            filter=filter,
            include=include,
            order=order,
            limit=limit,
            offset=offset,
            window=window,
        )
        self._session.last_stats = stats
        return rows

    def fetch_one(self, filter=None, include=None):
        rows = self.fetch(filter=filter, include=include, limit=1)
        return rows[0] if rows else None

    def get(self, row_id, include=None):
        rows = self.fetch(filter=None, include=include)
        for r in rows:
            if r.get("id") == row_id:
                return r
        return None

    def count(self, filter=None):
        node = parse_filter(filter)
        pushable, residual = split_pushable(node)
        store = self._session.store
        if residual is not None:
            raise QueryError("count() cannot use a filter that crosses a join")
        if hasattr(store, "set_predicate"):
            store.set_predicate(pushable)
            return store.count(self._type, None, ())
        where, params = _compile(self._type, pushable)
        return store.count(self._type, where, params)

    def evaluate(self, group=None, metrics=None, filter=None, order=None, limit=None):
        """Grouped aggregation. Pushed into SQL when the backend supports it."""
        specs = agg.parse_metrics(metrics)
        if not specs:
            raise QueryError("evaluate() needs at least one metric")
        group = list(group or [])
        for g in group:
            if self._type.field(g).kind != "primitive":
                raise QueryError("cannot group by %r, it is not a stored column" % g)

        node = parse_filter(filter)
        pushable, residual = split_pushable(node)
        if residual is not None:
            raise QueryError("evaluate() cannot use a filter that crosses a join")

        store = self._session.store
        if isinstance(store, RelationalStore):
            rows = self._evaluate_sql(store, specs, group, pushable)
        else:
            rows = self._evaluate_python(store, specs, group, pushable)

        if order:
            from .stores.relational import _parse_order

            for spec in reversed(order):
                key, direction = _parse_order(spec)
                rows.sort(
                    key=lambda r: (r.get(key) is not None, r.get(key) if r.get(key) is not None else 0),
                    reverse=(direction == "DESC"),
                )
        if limit is not None:
            rows = rows[: int(limit)]
        return rows

    def _evaluate_sql(self, store, specs, group, pushable):
        rtype = self._type
        select = [
            '"%s" AS "%s"' % (rtype.field(g).column, g) for g in group
        ] + agg.to_sql_projection(specs, rtype)
        sql = 'SELECT %s FROM "%s"' % (", ".join(select), rtype.table)
        where, params = _compile(rtype, pushable)
        if where:
            sql += " WHERE " + where
        if group:
            sql += " GROUP BY " + ", ".join('"%s"' % rtype.field(g).column for g in group)
        store.queries += 1
        raw = store.conn.execute(sql, tuple(params)).fetchall()
        out = []
        for r in raw:
            row = {g: r[g] for g in group}
            row.update(agg.finish_sql_row(specs, r))
            out.append(row)
        return out

    def _evaluate_python(self, store, specs, group, pushable):
        if hasattr(store, "set_predicate"):
            store.set_predicate(pushable)
            rows = store.fetch(self._type, None, ())
        else:
            where, params = _compile(self._type, pushable)
            rows = store.fetch(self._type, where, params)
        buckets = {}
        for r in rows:
            key = tuple(r.get(g) for g in group)
            buckets.setdefault(key, []).append(r)
        out = []
        for key, bucket in buckets.items():
            row = dict(zip(group, key))
            row.update(agg.reduce_rows(specs, bucket))
            out.append(row)
        return out


class Session:
    def __init__(self, model, store=None, timeseries=None):
        if not isinstance(model, Model):
            raise TypeError("Session needs a resolved Model")
        self.model = model
        self.store = store if store is not None else RelationalStore(model)
        self.timeseries = timeseries if timeseries is not None else TimeSeriesStore()
        self.planner = Planner(self)
        self.last_stats = None
        self._proxies = {}

    def type(self, name):
        proxy = self._proxies.get(name)
        if proxy is None:
            rtype = self.model[name]
            if rtype.is_abstract:
                raise QueryError("%s is abstract, it has no rows" % name)
            proxy = TypeProxy(self, rtype)
            self._proxies[name] = proxy
        return proxy

    def __getitem__(self, name):
        return self.type(name)

    def flush(self):
        self.timeseries.flush()

    def reset_counters(self):
        self.store.reset_counters()
        self.timeseries.reset_counters()

    def stats(self):
        return {
            "store": {
                "backend": self.store.name,
                "queries": self.store.queries,
                "rows_scanned": self.store.rows_scanned,
            },
            "timeseries": self.timeseries.stats(),
        }

    def close(self):
        self.store.close()


def open_session(model_source, backend="relational", path=":memory:", segment_points=512):
    """Compile a model and bind it to a fresh pair of stores."""
    model = model_source if isinstance(model_source, Model) else load(model_source)
    if backend == "relational":
        store = RelationalStore(model, path)
    elif backend == "memory":
        store = MemoryStore(model)
    else:
        raise ValueError("unknown backend %r" % backend)
    return Session(model, store, TimeSeriesStore(segment_points))


def _as_rows(rows):
    if isinstance(rows, dict):
        return [rows]
    return list(rows)
