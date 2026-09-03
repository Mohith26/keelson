"""Bit level reader and writer used by the time series codec.

The segment codec needs to emit things like "two control bits, then five bits
of leading zero count, then six bits of significant width" so a byte oriented
buffer is not enough. Both classes are deliberately allocation light: the
writer appends to a bytearray, the reader indexes into a bytes object.
"""


class BitWriter:
    __slots__ = ("_buf", "_acc", "_nbits")

    def __init__(self):
        self._buf = bytearray()
        self._acc = 0
        self._nbits = 0

    def write(self, value, width):
        """Append the low `width` bits of `value`, most significant bit first."""
        if width <= 0:
            return
        value &= (1 << width) - 1
        self._acc = (self._acc << width) | value
        self._nbits += width
        while self._nbits >= 8:
            self._nbits -= 8
            self._buf.append((self._acc >> self._nbits) & 0xFF)
        self._acc &= (1 << self._nbits) - 1

    def write_bit(self, bit):
        self.write(1 if bit else 0, 1)

    def write_varint(self, value):
        """Unsigned LEB128, byte aligned only in the sense that it is 7 bits at a time."""
        while True:
            chunk = value & 0x7F
            value >>= 7
            if value:
                self.write(chunk | 0x80, 8)
            else:
                self.write(chunk, 8)
                return

    def bit_length(self):
        return len(self._buf) * 8 + self._nbits

    def finish(self):
        if self._nbits:
            self._buf.append((self._acc << (8 - self._nbits)) & 0xFF)
            self._acc = 0
            self._nbits = 0
        return bytes(self._buf)


class BitReader:
    __slots__ = ("_data", "_pos")

    def __init__(self, data):
        self._data = data
        self._pos = 0

    def read(self, width):
        if width <= 0:
            return 0
        out = 0
        remaining = width
        while remaining:
            byte_index = self._pos >> 3
            if byte_index >= len(self._data):
                raise EOFError("bit stream exhausted")
            bit_offset = self._pos & 7
            avail = 8 - bit_offset
            take = avail if avail < remaining else remaining
            byte = self._data[byte_index]
            shift = avail - take
            mask = (1 << take) - 1
            out = (out << take) | ((byte >> shift) & mask)
            self._pos += take
            remaining -= take
        return out

    def read_bit(self):
        return self.read(1)

    def read_varint(self):
        out = 0
        shift = 0
        while True:
            byte = self.read(8)
            out |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return out
            shift += 7


def zigzag_encode(value):
    return (value << 1) ^ (value >> 63) if value >= 0 else ((-value) << 1) - 1


def zigzag_decode(value):
    return (value >> 1) ^ -(value & 1)
