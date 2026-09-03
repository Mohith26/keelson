"""Append only, chunked time series store.

This is the second half of the split persistence model: entity rows live in a
relational table, while the measurement stream hanging off each entity lives
here, keyed by (type, field, series key). Points are buffered until a segment
fills, then encoded once and made immutable.

The codec is the Gorilla scheme from the Facebook in-memory TSDB paper:

  * timestamps are stored as a delta of deltas. Almost every industrial
    sensor reports on a fixed cadence, so the second difference is zero for
    long runs and costs a single bit,
  * values are XORed against the previous value. Slowly varying physical
    signals share most of their exponent and high mantissa bits, so the XOR
    has a long run of leading zeros and only the differing window is stored.

Range scans binary search a per segment (min_ts, max_ts) index so a query for
one hour out of a year touches only the segments that can possibly overlap,
rather than decoding the whole series.
"""

import bisect
import struct

from ..errors import StoreError
from .bits import BitReader, BitWriter, zigzag_decode, zigzag_encode

DEFAULT_SEGMENT_POINTS = 512

# Delta-of-delta bucket layout. Each entry is (control_bits, control_width,
# payload_width). A dod of zero is a single 0 bit; everything else is a
# prefix of ones followed by a fixed width zigzag payload.
_DOD_BUCKETS = (
    (0b10, 2, 7),
    (0b110, 3, 9),
    (0b1110, 4, 12),
    (0b11110, 5, 32),
    (0b11111, 5, 64),
)

# Number of leading 1 bits in a bucket control -> payload width. Derived from
# _DOD_BUCKETS rather than restated, so the two cannot drift apart.
_DOD_PAYLOAD_WIDTH = {
    (cwidth if control == 0b11111 else cwidth - 1): pwidth
    for control, cwidth, pwidth in _DOD_BUCKETS
}


class _Segment:
    __slots__ = ("min_ts", "max_ts", "count", "blob", "first_ts", "first_value")

    def __init__(self, min_ts, max_ts, count, blob, first_ts, first_value):
        self.min_ts = min_ts
        self.max_ts = max_ts
        self.count = count
        self.blob = blob
        self.first_ts = first_ts
        self.first_value = first_value

    def nbytes(self):
        return len(self.blob) + 32  # blob plus the index record it needs


def encode_segment(points):
    """Encode a list of (timestamp_int, float_value) into a compressed blob."""
    if not points:
        raise StoreError("cannot encode an empty segment")
    w = BitWriter()
    first_ts, first_value = points[0]
    prev_ts = first_ts
    prev_delta = 0
    prev_bits = struct.unpack(">Q", struct.pack(">d", float(first_value)))[0]
    prev_leading = -1
    prev_trailing = 0

    for ts, value in points[1:]:
        delta = ts - prev_ts
        dod = delta - prev_delta
        if dod == 0:
            w.write_bit(0)
        else:
            z = zigzag_encode(dod)
            for control, cwidth, pwidth in _DOD_BUCKETS:
                if z < (1 << pwidth):
                    w.write(control, cwidth)
                    w.write(z, pwidth)
                    break
            else:
                raise StoreError("timestamp delta out of range: %d" % dod)
        prev_delta = delta
        prev_ts = ts

        bits = struct.unpack(">Q", struct.pack(">d", float(value)))[0]
        xor = bits ^ prev_bits
        if xor == 0:
            w.write_bit(0)
        else:
            w.write_bit(1)
            leading = 0
            probe = xor
            while leading < 64 and not probe & (1 << 63):
                leading += 1
                probe <<= 1
                probe &= (1 << 64) - 1
            trailing = 0
            probe = xor
            while trailing < 64 and not probe & 1:
                trailing += 1
                probe >>= 1
            if leading > 31:
                leading = 31
            if (
                prev_leading >= 0
                and leading >= prev_leading
                and trailing >= prev_trailing
                and 64 - prev_leading - prev_trailing > 0
            ):
                # Reuse the previous window: one control bit, no header.
                w.write_bit(0)
                width = 64 - prev_leading - prev_trailing
                w.write(xor >> prev_trailing, width)
            else:
                w.write_bit(1)
                width = 64 - leading - trailing
                w.write(leading, 5)
                w.write(width - 1, 6)
                w.write(xor >> trailing, width)
                prev_leading = leading
                prev_trailing = trailing
        prev_bits = bits

    return w.finish()


def decode_segment(seg):
    """Decode a segment back into a list of (timestamp, value)."""
    out = [(seg.first_ts, seg.first_value)]
    if seg.count == 1:
        return out
    r = BitReader(seg.blob)
    prev_ts = seg.first_ts
    prev_delta = 0
    prev_bits = struct.unpack(">Q", struct.pack(">d", float(seg.first_value)))[0]
    prev_leading = -1
    prev_trailing = 0

    for _ in range(seg.count - 1):
        if r.read_bit() == 0:
            dod = 0
        else:
            # The leading 1 is already consumed. Count how many more 1 bits
            # follow to identify the bucket: 10 -> 1, 110 -> 2, 1110 -> 3,
            # 11110 -> 4, 11111 -> 5. Every bucket except the last is
            # terminated by a 0 bit, which this loop consumes on exit.
            ones = 1
            while ones < 5 and r.read_bit() == 1:
                ones += 1
            pwidth = _DOD_PAYLOAD_WIDTH[ones]
            dod = zigzag_decode(r.read(pwidth))
        delta = prev_delta + dod
        ts = prev_ts + delta
        prev_delta = delta
        prev_ts = ts

        if r.read_bit() == 0:
            bits = prev_bits
        else:
            if r.read_bit() == 0:
                width = 64 - prev_leading - prev_trailing
                xor = r.read(width) << prev_trailing
            else:
                leading = r.read(5)
                width = r.read(6) + 1
                trailing = 64 - leading - width
                xor = r.read(width) << trailing
                prev_leading = leading
                prev_trailing = trailing
            bits = prev_bits ^ xor
        prev_bits = bits
        value = struct.unpack(">d", struct.pack(">Q", bits))[0]
        out.append((ts, value))
    return out


class Series:
    """One (type, field, key) stream: sealed segments plus an open tail."""

    __slots__ = ("key", "segment_points", "_segments", "_starts", "_open", "_last_ts", "points_written")

    def __init__(self, key, segment_points=DEFAULT_SEGMENT_POINTS):
        self.key = key
        self.segment_points = segment_points
        self._segments = []
        self._starts = []
        self._open = []
        self._last_ts = None
        self.points_written = 0

    def append(self, ts, value):
        if self._last_ts is not None and ts < self._last_ts:
            raise StoreError(
                "series %s is append only, got ts %d after %d" % (self.key, ts, self._last_ts)
            )
        self._last_ts = ts
        self._open.append((ts, float(value)))
        self.points_written += 1
        if len(self._open) >= self.segment_points:
            self._seal()

    def _seal(self):
        if not self._open:
            return
        pts = self._open
        blob = encode_segment(pts)
        seg = _Segment(pts[0][0], pts[-1][0], len(pts), blob, pts[0][0], pts[0][1])
        self._segments.append(seg)
        self._starts.append(seg.min_ts)
        self._open = []

    def flush(self):
        self._seal()

    def count(self):
        return self.points_written

    def encoded_bytes(self):
        return sum(s.nbytes() for s in self._segments) + len(self._open) * 16

    def segments_scanned_for(self, start, end):
        lo = bisect.bisect_right(self._starts, start) - 1
        if lo < 0:
            lo = 0
        n = 0
        for i in range(lo, len(self._segments)):
            seg = self._segments[i]
            if seg.min_ts > end:
                break
            if seg.max_ts >= start:
                n += 1
        return n

    def range(self, start=None, end=None):
        """Return points with start <= ts <= end, decoding only what overlaps."""
        start = -(1 << 62) if start is None else start
        end = (1 << 62) if end is None else end
        out = []
        lo = bisect.bisect_right(self._starts, start) - 1
        if lo < 0:
            lo = 0
        for i in range(lo, len(self._segments)):
            seg = self._segments[i]
            if seg.min_ts > end:
                break
            if seg.max_ts < start:
                continue
            for ts, value in decode_segment(seg):
                if start <= ts <= end:
                    out.append((ts, value))
        for ts, value in self._open:
            if start <= ts <= end:
                out.append((ts, value))
        return out


class TimeSeriesStore:
    """Holds every series in the model, addressed by (type, field, key)."""

    def __init__(self, segment_points=DEFAULT_SEGMENT_POINTS):
        self.segment_points = segment_points
        self._series = {}
        self.segments_scanned = 0
        self.points_decoded = 0

    def series(self, type_name, field_name, key, create=True):
        sk = (type_name, field_name, key)
        s = self._series.get(sk)
        if s is None:
            if not create:
                return None
            s = Series(sk, self.segment_points)
            self._series[sk] = s
        return s

    def append(self, type_name, field_name, key, ts, value):
        self.series(type_name, field_name, key).append(ts, value)

    def append_many(self, type_name, field_name, key, points):
        s = self.series(type_name, field_name, key)
        for ts, value in points:
            s.append(ts, value)

    def range(self, type_name, field_name, key, start=None, end=None):
        s = self.series(type_name, field_name, key, create=False)
        if s is None:
            return []
        self.segments_scanned += s.segments_scanned_for(
            -(1 << 62) if start is None else start,
            (1 << 62) if end is None else end,
        )
        pts = s.range(start, end)
        self.points_decoded += len(pts)
        return pts

    def flush(self):
        for s in self._series.values():
            s.flush()

    def stats(self):
        total_points = sum(s.count() for s in self._series.values())
        encoded = sum(s.encoded_bytes() for s in self._series.values())
        raw = total_points * 16
        return {
            "series": len(self._series),
            "points": total_points,
            "encoded_bytes": encoded,
            "raw_bytes": raw,
            "bytes_per_point": (encoded / total_points) if total_points else 0.0,
            "compression_ratio": (raw / encoded) if encoded else 0.0,
            "segments_scanned": self.segments_scanned,
            "points_decoded": self.points_decoded,
        }

    def reset_counters(self):
        self.segments_scanned = 0
        self.points_decoded = 0
