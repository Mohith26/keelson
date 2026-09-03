"""Aggregation specs for Session.evaluate.

An evaluate call names a grouping and a set of metrics written as short
expressions: count(), sum(kw), avg(kw), min(t), max(t), countDistinct(siteId).
Each spec compiles two ways, exactly like the filter language: to a SQL
projection for the relational store, and to a Python reducer for the in memory
store and the oracle.

avg is the one that needs care. Pushing AVG() into SQL and reducing with a
running mean in Python can disagree in the last bits, so both paths compute a
sum and a count and divide once at the end. That makes the differential test
meaningful instead of forcing a tolerance onto it.
"""

import re

from .errors import QueryError

_SPEC = re.compile(r"^\s*(\w+)\s*\(\s*([A-Za-z_][A-Za-z_0-9]*)?\s*\)\s*$")

_FUNCS = {"count", "sum", "avg", "min", "max", "countdistinct"}


class Metric:
    __slots__ = ("alias", "func", "field")

    def __init__(self, alias, func, field):
        self.alias = alias
        self.func = func
        self.field = field

    def __repr__(self):
        return "Metric(%s=%s(%s))" % (self.alias, self.func, self.field or "")


def parse_metrics(metrics):
    out = []
    for alias, spec in (metrics or {}).items():
        m = _SPEC.match(spec)
        if not m:
            raise QueryError("cannot parse metric %r for alias %r" % (spec, alias))
        func = m.group(1).lower()
        field = m.group(2)
        if func not in _FUNCS:
            raise QueryError("unknown aggregate %r" % m.group(1))
        if func == "count":
            if field:
                raise QueryError("count() takes no argument, use countDistinct(f)")
        elif not field:
            raise QueryError("%s requires a field" % func)
        out.append(Metric(alias, func, field))
    return out


def to_sql_projection(metrics, rtype):
    """Return the SELECT list for the metric part of an evaluate."""
    parts = []
    for m in metrics:
        if m.func == "count":
            parts.append('COUNT(*) AS "%s"' % m.alias)
            continue
        col = rtype.field(m.field).column
        if m.func == "avg":
            # Sum and count separately so the division happens in one place.
            parts.append('SUM("%s") AS "%s__sum"' % (col, m.alias))
            parts.append('COUNT("%s") AS "%s__n"' % (col, m.alias))
        elif m.func == "countdistinct":
            parts.append('COUNT(DISTINCT "%s") AS "%s"' % (col, m.alias))
        else:
            parts.append('%s("%s") AS "%s"' % (m.func.upper(), col, m.alias))
    return parts


def finish_sql_row(metrics, row):
    out = {}
    for m in metrics:
        if m.func == "avg":
            n = row["%s__n" % m.alias]
            out[m.alias] = (row["%s__sum" % m.alias] / n) if n else None
        else:
            out[m.alias] = row[m.alias]
    return out


def reduce_rows(metrics, rows):
    """Compute the metrics over a list of row dicts, in Python."""
    out = {}
    for m in metrics:
        if m.func == "count":
            out[m.alias] = len(rows)
            continue
        values = [r.get(m.field) for r in rows]
        present = [v for v in values if v is not None]
        if m.func == "sum":
            out[m.alias] = sum(present) if present else None
        elif m.func == "avg":
            out[m.alias] = (sum(present) / len(present)) if present else None
        elif m.func == "min":
            out[m.alias] = min(present) if present else None
        elif m.func == "max":
            out[m.alias] = max(present) if present else None
        elif m.func == "countdistinct":
            out[m.alias] = len(set(present))
    return out
