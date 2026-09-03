# Results

Every number here is produced by `python bench/run.py` and written to
`results/bench.json`. Machine: Apple Silicon arm64, CPython 3.12, single core,
no third party packages. Timings are the best of several runs; counters come
from a separate clean run so the timing loop does not inflate them.

## Ingest

| | |
| --- | --- |
| Entity rows | 8,040 |
| Time | 0.022 s |
| Throughput | 370,507 rows/sec |

8,040 rows is 20 sites, 800 turbines, 20 substations, 3,200 sensors and 4,000
work orders. Writes go through `executemany` inside one transaction per batch.
Row by row autocommit was roughly two orders of magnitude slower in an early
version, which is why `create`/`upsert`/`merge` are batch shaped in the API
rather than taking a single row and looping.

| | |
| --- | --- |
| Time series points | 320,000 across 160 series |
| Time | 1.347 s |
| Throughput | 237,618 points/sec |
| Encoded | 6.92 bytes/point |
| Raw float64 pair | 16 bytes/point |
| Ratio | 2.31x |

## Filter pushdown

The same query through the same planner, changing only the entity store
underneath. The relational store gets a `WHERE`, an `ORDER BY` and a `LIMIT`;
the memory store cannot take any of them and has to hand back everything.

Query: `status == "ACTIVE" and ratedPowerKw > 3400`, ordered by
`-ratedPowerKw`, limit 20, over a fleet of 800 turbines.

| | rows scanned | time |
| --- | --- | --- |
| SQL pushdown | 20 | 0.0001 s |
| Full scan | 800 | 0.0005 s |
| | **40x fewer** | **5.0x faster** |

The rows-scanned ratio is the number that matters and the time ratio is the
one that flatters. 40x fewer rows only bought 5x wall clock because at this
size everything is in page cache and the Python interpreter overhead per row
dominates. The gap widens with the row count, and it is the difference between
`LIMIT 20` reaching the storage engine and not.

## Join batching

1,000 turbines, each with a `site` reference.

| | store queries | time |
| --- | --- | --- |
| Batched `IN` | 2 | 0.0028 s |
| One lookup per parent | 1,001 | 0.0149 s |
| | **500x fewer** | **5.3x faster** |

Two queries, not two per level: one for the roots and one for the parents,
with the key list chunked at 900 to stay under SQLite's bound parameter cap.
`tests/test_planner.py` has a regression test that loads 2,500 sites and
asserts the join takes exactly 4 queries, because chunking is the kind of
thing that silently stops working when someone reaches for a simpler `IN`.

## Segment skipping

20 sensors, 2,000 points each, 512 points per segment.

| | segments | points decoded | time |
| --- | --- | --- | --- |
| Whole series | 80 | 40,000 | 0.1466 s |
| 100 point window | 40 | 2,020 | 0.0739 s |
| | 2.0x fewer | **19.8x fewer** | 2.0x faster |

The segment count only halves because the requested window straddles a segment
boundary, so it needs two of the four segments per sensor. Decoded points drop
by 19.8x, which is the real saving: decoding is where the time goes, and the
`(min_ts, max_ts)` index is what lets a query skip it.

## Codec by signal shape

8,192 points each, one segment size, same codec.

| Signal | bytes/point | ratio |
| --- | --- | --- |
| constant | 0.32 | 50.57x |
| stepwise | 0.32 | 49.95x |
| slow random walk | 6.28 | 2.55x |
| diurnal cycle | 6.93 | 2.31x |
| jittered cadence | 7.16 | 2.24x |
| white noise | 8.43 | 1.90x |

This spread is the point. A constant signal at a fixed cadence costs about
two and a half bits per sample: one bit for a zero delta of delta, one for an
unchanged value, and the segment header amortized. White noise costs 8.43
bytes, which works out at roughly 67 bits per point: one bit of timestamp plus
about 66 bits of value, because two independent doubles drawn from the same
range still share around eleven leading bits and the rest has to be written
out along with a 13 bit header.

The uncomfortable version of this: **the codec's headline ratio is a property
of the test data.** If I had only generated constant signals this README would
claim 50x. The generator in `tools/generate.py` produces a mean reverting
random walk plus a diurnal term because that is what a real historian channel
looks like, and 2.31x is what that costs.

## Query latency

Amortized over repeated runs, because a single fetch finishes faster than
`perf_counter` can resolve. The first version of this benchmark divided by a
measured zero.

| Query | time | rows |
| --- | --- | --- |
| filter + limit | 0.143 ms | 50 |
| filter + order + limit | 0.091 ms | 20 |
| group by site | 0.068 ms | 10 |
| group by work order category | 0.161 ms | 6 |
| polymorphic collection include | 1.163 ms | 10 |

The polymorphic include is an order of magnitude slower than the rest and it
should be: it fans out over every concrete subtype of `Asset`, runs a chunked
`IN` per subtype, and materializes 1,400 child rows into buckets. The others
return their rows straight from one indexed statement.

## Correctness

195 tests, 37,517 assertions, 0 failures, 2.6 s.

| Module | what it covers |
| --- | --- |
| `test_lexer` | tokens, positions, comments, escapes |
| `test_parser` | every grammar production and its error message |
| `test_model` | flattening, routing, and 15 distinct rejections |
| `test_expr` | both compilers, including 10 filters checked against real SQLite |
| `test_tsdb` | codec roundtrips, including a 120 case property sweep |
| `test_relational` | DDL, batch write semantics, index usage, backend parity |
| `test_planner` | pushdown decisions, join batching, N+1 and chunking guards |
| `test_session` | the public API and aggregation |
| `test_migrate` | safe and unsafe changes, applied against a live store |
| `test_oracle` | differential sweep against brute force |

Two of these are worth calling out.

`test_expr.test_sql_and_predicate_agree_on_random_rows` runs ten filters over
400 generated rows through both compilers, one via a real SQLite `WHERE` and
one via the Python predicate, and asserts identical row sets. That is what
catches a disagreement about `NULL`, which SQL and Python handle differently
by default and which the predicate compiler has to emulate deliberately.

`test_oracle.test_randomized_query_sweep_on_both_backends` runs 160 randomized
query combinations against each backend and compares to brute force. It found
the abstract-collection bug described in the README. It also has a negative
control, `test_the_oracle_actually_disagrees_when_the_engine_is_wrong`, which
plants a phantom row in the oracle and asserts the comparison fails, because a
differential test that cannot fail is decoration.

## Not measured

- No comparison against a real ORM or a real time series database. The
  baselines here are internal: pushdown against full scan, batched against per
  row, windowed against whole series. Those isolate the thing being claimed,
  but they are not a claim that keelson is faster than SQLAlchemy or InfluxDB,
  and I have not run that comparison.
- No multi process or concurrent access. Everything is single threaded and in
  process.
- No disk persistence for the time series store, so nothing here says anything
  about durability or cold read latency.
- Only SQLite. The DDL generator maps types for a generic SQL dialect, but
  Postgres is not implemented and not measured.
