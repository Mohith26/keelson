# keelson

A model-driven data layer for industrial asset data. You describe your domain
once in a small typed DSL, and keelson compiles it into a relational schema, a
compressed time series store, and a query planner that knows which of the two
owns each field.

The problem it is built around is the one that shows up in every plant,
utility and fleet system I have looked at: the structured data (assets, sites,
work orders) and the measurement data (sensor streams) want completely
different storage, but the application wants to ask one question that spans
both. Wiring that up by hand means every query carries knowledge of table
names, join keys, chunk boundaries and which store to hit, and every schema
change means editing all of it.

```python
import keelson

ses = keelson.open_session(open("models/fleet.ks").read())

hot = ses.type("Turbine").fetch(
    filter='status == "ACTIVE" and site.region == "north"',
    include=["site", "sensors.readings"],
    order=["-ratedPowerKw"],
    limit=10,
    window=(t0, t1),
)
```

Nothing in that call names a table, a column, a join key, a segment or a
backend. The planner works out that `status` can be pushed into SQL, that
`site.region` cannot and has to stay behind as a residual, that `sensors` is a
batched join rather than a per row lookup, and that `readings` lives in the
segment store and only needs the chunks overlapping the window.

## The model

```
mixin type Persistable {
  id: string
  version: int = 1
}

@db(index="siteId")
abstract entity type Asset mixes Persistable {
  siteId: string
  serial: string
  status: string = "UNKNOWN"
  site: Site(siteId)
}

@db(unique="serial", index="status")
entity type Turbine extends Asset schema name "WTG_MASTER" {
  ratedPowerKw: double schema name "RATED_KW"
  model: string schema name "WTG_MODEL"
  sensors: [Sensor](assetId, id)
}

entity type Sensor mixes Persistable {
  assetId: string
  channel: string
  readings: timeseries<double>(id)
}
```

Four kinds of field, and the resolver routes each one to a different place:

| Declaration | Meaning | Owned by |
| --- | --- | --- |
| `ratedPowerKw: double` | a stored column | relational store |
| `site: Site(siteId)` | many to one, keyed on a local column | planner |
| `sensors: [Sensor](assetId, id)` | one to many, keyed on a remote column | planner |
| `readings: timeseries<double>(id)` | a measurement stream | segment store |

`schema name` maps onto a legacy naming scheme, which is the normal case when
the database predates the application. `@db(unique=..., index=...)` becomes
the index plan. `extends` and `mixes` flatten at compile time, and a
relationship is allowed to target an abstract type: `Site.assets` targets
`Asset`, which has no table of its own, so the planner fans the query out over
`Turbine` and `Substation` and unions the result.

## What is actually in here

- **`keelson/dsl/`** hand written lexer and recursive descent parser. `name` is
  a contextual keyword rather than a reserved word, so a field called `name`
  works.
- **`keelson/model.py`** flattens inheritance and mixins, then rejects the
  edits that would quietly corrupt a schema later: cycles, conflicting
  redeclarations, two types on one table, two fields on one column, indexes
  naming things that are not columns, foreign keys pointing at fields that do
  not exist.
- **`keelson/expr.py`** one filter grammar, compiled twice: to parameterised
  SQL, and to a Python predicate. `split_pushable` divides a filter around its
  top level `and` into the part that can go into the database and the part
  that has to wait for the join.
- **`keelson/stores/tsdb.py`** append only chunked time series with the Gorilla
  codec: delta of delta timestamps and XOR compressed float64 values, written
  through a bit level writer. Segments carry a `(min_ts, max_ts)` index so a
  range scan binary searches rather than decoding the series.
- **`keelson/planner.py`** pushdown, batched hash joins with the `IN` list
  chunked below SQLite's bound parameter cap, polymorphic fan out, and a
  `PlanStats` record per query so the benchmarks can report what the planner
  actually did.
- **`keelson/migrate.py`** diffs two models and classifies every change as safe
  or not. Adding columns and indexes applies automatically; dropping or
  narrowing a column is refused unless you ask for it explicitly.
- **`keelson/oracle.py`** an independent brute force implementation of the same
  query surface, used to check the real engine.

## Measured results

From `python bench/run.py` on Python 3.12, single core. Raw output in
`results/bench.json`, discussion in `RESULTS.md`.

| | |
| --- | --- |
| Entity ingest | 8,040 rows in 0.022 s (371k rows/sec) |
| Time series ingest | 320,000 points across 160 series (238k points/sec) |
| Encoded size | 6.92 bytes/point vs 16 raw, **2.31x** |
| Filter pushdown | 20 rows scanned vs 800, **40x fewer**, 5.0x faster |
| Join batching | 2 queries vs 1,001, **500x fewer**, 5.3x faster |
| Window scan | 2,020 points decoded vs 40,000, **19.8x fewer** |

Compression depends entirely on the signal, so the benchmark reports six
shapes rather than one flattering number:

| Signal | bytes/point | ratio |
| --- | --- | --- |
| constant | 0.32 | 50.6x |
| stepwise | 0.32 | 50.0x |
| slow random walk | 6.28 | 2.55x |
| diurnal cycle | 6.93 | 2.31x |
| jittered cadence | 7.16 | 2.24x |
| white noise | 8.43 | 1.90x |

White noise is the honest floor. Two random float64s share almost no high
order bits, so the XOR has nothing to strip and the codec only wins on the
timestamps. Any benchmark of a time series codec that reports a single number
is telling you about its test data, not its codec.

## Verification

195 tests, 37,517 assertions, no third party dependencies.

```
python run_tests.py          # everything
python run_tests.py tsdb     # one module
python demo.py               # the walkthrough
python bench/run.py          # the numbers above
```

The part I trust most is `tests/test_oracle.py`. It builds the same fleet in a
real session and in the brute force oracle, then runs a randomized sweep of
160 query combinations per backend, composing filters, includes, orders,
limits and offsets, and asserts the two agree exactly. It includes a negative
control that plants an extra row in the oracle and asserts the comparison
fails, because an oracle that can never disagree is not testing anything.

## Three bugs the tests found

**A collection could not target an abstract type.** `Site.assets` points at
`Asset`, which is abstract and therefore has no table, and the planner went
looking for one: `sqlite3.OperationalError: no such table: asset`. Only the
oracle's nested include test hit it, because that was the only query that
walked the polymorphic edge. The fix was `Model.concrete_subtypes()` plus
fanning the join out over the subtypes and unioning, which is a real feature
rather than a patch, and it is what makes `Site.assets` return turbines and
substations in one list.

**`name` was a reserved word.** The DSL needs `schema name "WTG"`, so `name`
went into the keyword set, which made a field called `name` a syntax error.
That is the most common field name there is. It is now contextual: the lexer
emits an ordinary identifier and the parser checks the value only in the one
position where it means something.

**A LIMIT without an ORDER BY is not a bug.** The randomized sweep failed with
the engine returning `wtg-001-003` and the oracle returning `wtg-000-004`, and
the first instinct was to go hunting in the planner. Both answers are correct:
an unordered `LIMIT` picks an arbitrary subset and SQL is entitled to pick a
different one than a Python list slice. The test was asserting on insertion
order, so the test was wrong, and it now only applies a limit when an order is
specified. Worth writing down because the reflex to blame the code first cost
me more time than the fix did.

## Where this stops

- SQLite only. The `Store` interface is small and the DDL generator is
  already type mapped, but Postgres is not implemented, so "swap the backing
  database" is demonstrated between SQLite and an in memory store, not
  between two real databases.
- The time series store is append only and in process. There is no
  compaction, no out of order write path, and nothing is persisted to disk.
- `evaluate()` groups on stored columns of one type. It does not aggregate
  across a join.
- The planner splits filters around top level `and` only. `a == 1 or
  site.x == 2` stays entirely residual, which is correct but not clever.
- Migration cannot narrow a column or rename a table in place, because SQLite
  cannot. It reports those rather than attempting them.

## Layout

```
keelson/
  dsl/          lexer, parser, AST
  stores/       bit IO, Gorilla time series codec, SQLite store, memory store
  model.py      resolution and validation
  expr.py       filter grammar, SQL and predicate compilers
  planner.py    pushdown, batched joins, polymorphic fan out
  agg.py        aggregation specs
  session.py    the public API
  migrate.py    schema diffing
  oracle.py     brute force reference implementation
models/fleet.ks the demo model
tools/generate.py seeded fleet generator
tests/          195 tests
bench/run.py    the benchmarks
demo.py         the walkthrough
```

A keelson is the timber that runs the length of a hull on top of the keel and
ties the frames to it. Seemed like the right name for a layer whose whole job
is holding two different things in one shape.
