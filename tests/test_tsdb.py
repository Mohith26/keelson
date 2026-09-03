"""Time series codec and segment store tests.

The codec is lossless for float64, so every roundtrip check is exact equality
on both the timestamp and the bit pattern of the value. The property style
tests hammer it with a few thousand generated series because the tricky cases
(reusing the previous XOR window, dod values that land exactly on a bucket
boundary) are hard to hit by hand.
"""

import math
import random
import struct

from keelson.stores.bits import BitReader, BitWriter, zigzag_decode, zigzag_encode
from keelson.stores.tsdb import Series, TimeSeriesStore, decode_segment, encode_segment, _Segment
from keelson.errors import StoreError

from .runner import bump, close, eq, ok, raises


def _roundtrip(points):
    blob = encode_segment(points)
    seg = _Segment(points[0][0], points[-1][0], len(points), blob, points[0][0], points[0][1])
    return decode_segment(seg)


def _bits(x):
    return struct.unpack(">Q", struct.pack(">d", float(x)))[0]


def test_bitwriter_roundtrips_arbitrary_widths():
    rnd = random.Random(11)
    for _ in range(300):
        fields = [(rnd.getrandbits(w), w) for w in (rnd.randint(1, 32) for _ in range(20))]
        w = BitWriter()
        for value, width in fields:
            w.write(value, width)
        r = BitReader(w.finish())
        for value, width in fields:
            eq(r.read(width), value, "width %d" % width)


def test_bitwriter_reports_bit_length():
    w = BitWriter()
    w.write(1, 3)
    eq(w.bit_length(), 3)
    w.write(0, 13)
    eq(w.bit_length(), 16)
    eq(len(w.finish()), 2)


def test_varint_roundtrip():
    w = BitWriter()
    values = [0, 1, 127, 128, 300, 16383, 16384, 1 << 40]
    for v in values:
        w.write_varint(v)
    r = BitReader(w.finish())
    for v in values:
        eq(r.read_varint(), v)


def test_zigzag_is_a_bijection_on_a_wide_range():
    rnd = random.Random(5)
    for _ in range(2000):
        v = rnd.randint(-(1 << 40), 1 << 40)
        eq(zigzag_decode(zigzag_encode(v)), v)
        ok(zigzag_encode(v) >= 0)


def test_single_point_segment():
    eq(_roundtrip([(1000, 3.5)]), [(1000, 3.5)])


def test_constant_cadence_constant_value_is_nearly_free():
    points = [(1000 + 10 * i, 42.0) for i in range(512)]
    eq(_roundtrip(points), points)
    blob = encode_segment(points)
    # 511 deltas at 1 bit plus 511 values at 1 bit is 1022 bits = 128 bytes.
    ok(len(blob) <= 130, "constant series took %d bytes" % len(blob))
    bump()


def test_roundtrip_exact_for_drifting_signal():
    rnd = random.Random(7)
    ts = 1_700_000_000
    value = 512.25
    points = []
    for _ in range(512):
        ts += 10
        value += rnd.uniform(-0.5, 0.5)
        points.append((ts, value))
    got = _roundtrip(points)
    eq(len(got), len(points))
    for (ta, va), (tb, vb) in zip(points, got):
        eq(ta, tb)
        eq(_bits(va), _bits(vb), "value bits differ")


def test_roundtrip_exact_for_jittered_cadence():
    rnd = random.Random(19)
    ts = 0
    points = []
    for i in range(400):
        ts += rnd.choice([9, 10, 10, 10, 11, 60, 1])
        points.append((ts, rnd.uniform(-1e6, 1e6)))
    got = _roundtrip(points)
    for (ta, va), (tb, vb) in zip(points, got):
        eq(ta, tb)
        eq(_bits(va), _bits(vb))


def test_roundtrip_handles_extreme_float_values():
    specials = [0.0, -0.0, 1e308, -1e308, 5e-324, math.pi, -math.pi, 1.0, -1.0]
    points = [(i * 10, v) for i, v in enumerate(specials)]
    got = _roundtrip(points)
    for (ta, va), (tb, vb) in zip(points, got):
        eq(ta, tb)
        eq(_bits(va), _bits(vb), "special %r" % va)


def test_roundtrip_property_sweep():
    rnd = random.Random(4242)
    for trial in range(120):
        n = rnd.randint(2, 200)
        ts = rnd.randint(0, 1 << 30)
        cadence = rnd.choice([1, 5, 10, 60, 900])
        points = []
        value = rnd.uniform(-1000, 1000)
        for _ in range(n):
            ts += cadence + (rnd.randint(-2, 2) if rnd.random() < 0.3 else 0)
            if rnd.random() < 0.2:
                value = rnd.uniform(-1e5, 1e5)
            else:
                value += rnd.uniform(-1, 1)
            points.append((ts, value))
        points.sort(key=lambda p: p[0])
        got = _roundtrip(points)
        eq(len(got), n, "trial %d" % trial)
        for (ta, va), (tb, vb) in zip(points, got):
            eq(ta, tb)
            eq(_bits(va), _bits(vb))


def test_large_timestamp_gaps_use_the_wide_bucket():
    points = [(0, 1.0), (1, 2.0), (1 << 34, 3.0), ((1 << 34) + 1, 4.0)]
    eq(_roundtrip(points), points)


def test_series_rejects_out_of_order_appends():
    s = Series(("Sensor", "readings", "s1"), segment_points=8)
    s.append(100, 1.0)
    s.append(200, 2.0)
    raises(StoreError, lambda: s.append(150, 3.0), "append only")


def test_series_range_spans_sealed_and_open_segments():
    s = Series(("Sensor", "readings", "s1"), segment_points=8)
    for i in range(30):
        s.append(i * 10, float(i))
    eq(len(s.range()), 30)
    eq(s.range(0, 25), [(0, 0.0), (10, 1.0), (20, 2.0)])
    eq(s.range(100, 130), [(100, 10.0), (110, 11.0), (120, 12.0), (130, 13.0)])
    eq(s.range(1000, 2000), [])
    eq(len(s.range(250, 400)), 5)


def test_range_scan_touches_only_overlapping_segments():
    s = Series(("Sensor", "readings", "s1"), segment_points=64)
    for i in range(64 * 20):
        s.append(i, float(i))
    s.flush()
    # 20 sealed segments of 64 points each; a 64 point window can overlap at
    # most two of them.
    eq(s.segments_scanned_for(500, 560), 2)
    eq(s.segments_scanned_for(0, 63), 1)
    ok(s.segments_scanned_for(0, 64 * 20) == 20)


def test_store_keys_series_independently():
    st = TimeSeriesStore(segment_points=16)
    st.append_many("Sensor", "readings", "a", [(i, float(i)) for i in range(20)])
    st.append_many("Sensor", "readings", "b", [(i, float(-i)) for i in range(20)])
    st.append_many("Sensor", "quality", "a", [(i, 1.0) for i in range(5)])
    eq(len(st.range("Sensor", "readings", "a")), 20)
    eq(st.range("Sensor", "readings", "b")[3], (3, -3.0))
    eq(len(st.range("Sensor", "quality", "a")), 5)
    eq(st.range("Sensor", "readings", "missing"), [])
    eq(st.stats()["series"], 3)


def test_store_compression_beats_raw_on_a_realistic_signal():
    rnd = random.Random(3)
    st = TimeSeriesStore(segment_points=512)
    ts = 1_700_000_000
    for sensor in range(4):
        value = 300.0 + sensor
        pts = []
        t = ts
        for _ in range(4096):
            t += 10
            value += rnd.uniform(-0.2, 0.2)
            pts.append((t, round(value, 3)))
        st.append_many("Sensor", "readings", "s%d" % sensor, pts)
    st.flush()
    stats = st.stats()
    eq(stats["points"], 4 * 4096)
    ok(stats["compression_ratio"] > 1.5, "ratio was %.2f" % stats["compression_ratio"])
    ok(stats["bytes_per_point"] < 16.0)


def test_encode_rejects_empty_segment():
    raises(StoreError, lambda: encode_segment([]), "empty segment")
