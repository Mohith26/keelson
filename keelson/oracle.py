"""A deliberately naive reference implementation, used as a differential oracle.

Nothing in here is fast and nothing in here is shared with the real engine.
It holds every row in a list, walks the whole list for every query, resolves
joins with nested loops, and evaluates filters with the Python predicate
compiler. That is the point: if the planner's pushdown, batched IN joins,
SQL projection and segment skipping are correct, they must agree with brute
force on every query, and if they disagree the oracle is the tiebreaker.

The one thing it shares with the engine is the filter parser, because
reimplementing the expression grammar twice would test the grammar rather than
the engine. Everything downstream of the AST is independent.
"""

from .expr import parse_filter, to_predicate


class Oracle:
    def __init__(self, model):
        self.model = model
        self.rows = {t.name: [] for t in model.concrete()}
        self.series = {}

    # -- loading -------------------------------------------------------

    def load(self, type_name, rows):
        table = self.rows[type_name]
        rtype = self.model[type_name]
        for row in rows:
            record = {}
            for f in rtype.stored_fields():
                value = row.get(f.name, f.default)
                if f.type_name == "boolean" and value is not None:
                    value = bool(value)
                record[f.name] = value
            existing = self._find(type_name, record["id"])
            if existing is None:
                table.append(record)
            else:
                existing.clear()
                existing.update(record)

    def load_series(self, type_name, field_name, key, points):
        bucket = self.series.setdefault((type_name, field_name, key), [])
        bucket.extend((int(t), float(v)) for t, v in points)
        bucket.sort(key=lambda p: p[0])

    def _find(self, type_name, row_id):
        for r in self.rows[type_name]:
            if r["id"] == row_id:
                return r
        return None

    # -- queries -------------------------------------------------------

    def fetch(self, type_name, filter=None, include=None, order=None, limit=None, offset=0, window=None):
        rtype = self.model[type_name]
        out = [dict(r) for r in self.rows[type_name]]

        for path in include or ():
            self._include(rtype, out, str(path).split("."), window)

        predicate = to_predicate(parse_filter(filter))
        out = [r for r in out if predicate(r)]

        for spec in reversed(order or ()):
            key, desc = _order(spec)
            out.sort(key=lambda r: _sortable(r.get(key)), reverse=desc)

        if limit is not None:
            out = out[offset : offset + int(limit)]
        return out

    def _include(self, rtype, rows, parts, window):
        name = parts[0]
        field = rtype.field(name)
        if field.kind == "reference":
            remote = self.model[field.type_name]
            attached = []
            for r in rows:
                target = None
                for candidate in self.rows[remote.name]:
                    if candidate["id"] == r.get(field.fk_local):
                        target = dict(candidate)
                        break
                r[name] = target
                if target is not None:
                    attached.append(target)
            if len(parts) > 1:
                self._include(remote, attached, parts[1:], window)
        elif field.kind == "collection":
            remote = self.model[field.type_name]
            attached = []
            for r in rows:
                kids = [
                    dict(c)
                    for c in self.rows[remote.name]
                    if c.get(field.fk_local) == r.get(field.fk_remote)
                ]
                r[name] = kids
                attached.extend(kids)
            if len(parts) > 1:
                self._include(remote, attached, parts[1:], window)
        elif field.kind == "timeseries":
            start, end = window or (None, None)
            for r in rows:
                pts = self.series.get((rtype.name, name, r.get(field.series_key)), [])
                r[name] = [
                    (t, v)
                    for t, v in pts
                    if (start is None or t >= start) and (end is None or t <= end)
                ]
        else:
            raise ValueError("cannot include stored column %s.%s" % (rtype.name, name))

    def count(self, type_name, filter=None):
        predicate = to_predicate(parse_filter(filter))
        return sum(1 for r in self.rows[type_name] if predicate(r))

    def evaluate(self, type_name, group=None, metrics=None, filter=None, order=None, limit=None):
        from .agg import parse_metrics, reduce_rows

        specs = parse_metrics(metrics)
        group = list(group or [])
        predicate = to_predicate(parse_filter(filter))
        rows = [r for r in self.rows[type_name] if predicate(r)]
        buckets = {}
        for r in rows:
            buckets.setdefault(tuple(r.get(g) for g in group), []).append(r)
        out = []
        for key, bucket in buckets.items():
            record = dict(zip(group, key))
            record.update(reduce_rows(specs, bucket))
            out.append(record)
        for spec in reversed(order or ()):
            k, desc = _order(spec)
            out.sort(key=lambda r: _sortable(r.get(k)), reverse=desc)
        if limit is not None:
            out = out[: int(limit)]
        return out

    def series_range(self, type_name, field_name, key, start=None, end=None):
        pts = self.series.get((type_name, field_name, key), [])
        return [
            (t, v)
            for t, v in pts
            if (start is None or t >= start) and (end is None or t <= end)
        ]


def _order(spec):
    if isinstance(spec, (tuple, list)):
        return spec[0], str(spec[1]).lower().startswith("desc")
    text = str(spec).strip()
    if text.startswith("-"):
        return text[1:], True
    return text, False


def _sortable(value):
    return (value is not None, value if value is not None else 0)


def mirror(session, oracle, type_name, rows):
    """Write the same rows to a session and to the oracle."""
    session.type(type_name).upsert(rows)
    oracle.load(type_name, rows)


def mirror_series(session, oracle, type_name, field_name, key, points):
    session.type(type_name).append_series(field_name, key, points)
    oracle.load_series(type_name, field_name, key, points)
