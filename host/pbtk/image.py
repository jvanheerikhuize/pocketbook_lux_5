"""Tiny image helpers (stdlib only): PGM parsing, raw framebuffer decoding, PNG writing and reading."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass


@dataclass
class Gray:
    """An 8-bit grayscale image, row-major."""

    width: int
    height: int
    pixels: bytes

    def at(self, x: int, y: int) -> int:
        return self.pixels[y * self.width + x]

    def png(self) -> bytes:
        return png_gray(self.width, self.height, self.pixels)

    def rotate_cw(self) -> "Gray":
        """Rotate 90° clockwise (the PB628 stores its portrait panel landscape)."""
        w, h, src = self.width, self.height, self.pixels
        out = bytearray(w * h)
        for ny in range(w):  # new height = old width
            row = ny * h
            for nx in range(h):  # new width = old height
                out[row + nx] = src[(h - 1 - nx) * w + ny]
        return Gray(h, w, bytes(out))

    def rotate_ccw(self) -> "Gray":
        """Rotate 90° counter-clockwise."""
        w, h, src = self.width, self.height, self.pixels
        out = bytearray(w * h)
        for ny in range(w):  # new height = old width
            row = ny * h
            col = w - 1 - ny
            for nx in range(h):  # new width = old height
                out[row + nx] = src[nx * w + col]
        return Gray(h, w, bytes(out))

    def turned(self, view: str) -> "Gray":
        """As seen in `view` (none, cw, ccw); see Device.view."""
        if view == "cw":
            return self.rotate_cw()
        if view == "ccw":
            return self.rotate_ccw()
        return self


def parse_pgm(data: bytes) -> Gray:
    """Parse a binary (P5) PGM with maxval 255, as the agent sends."""
    if not data.startswith(b"P5"):
        raise ValueError("not a P5 PGM")
    fields: list[bytes] = []
    i = 2
    while len(fields) < 3:
        while data[i : i + 1].isspace():
            i += 1
        if data[i : i + 1] == b"#":
            i = data.index(b"\n", i) + 1
            continue
        j = i
        while not data[j : j + 1].isspace():
            j += 1
        fields.append(data[i:j])
        i = j
    i += 1  # the single whitespace after maxval
    w, h, maxval = (int(f) for f in fields)
    if maxval != 255:
        raise ValueError("only maxval 255 is supported")
    pixels = data[i : i + w * h]
    if len(pixels) != w * h:
        raise ValueError("truncated PGM")
    return Gray(w, h, pixels)


def from_framebuffer(raw: bytes, width: int, height: int, bpp: int, stride: int) -> Gray:
    """Decode a raw /dev/fb0 dump (8, 16 = RGB565, 24 or 32 bpp) into gray."""
    out = bytearray(width * height)
    bytes_pp = max(bpp // 8, 1)
    for y in range(height):
        row = raw[y * stride : y * stride + width * bytes_pp]
        if len(row) < width * bytes_pp:
            break
        o = y * width
        if bpp == 8:
            out[o : o + width] = row
        elif bpp == 16:
            for x in range(width):
                v = row[2 * x] | (row[2 * x + 1] << 8)
                r, g, b = (v >> 11) << 3, ((v >> 5) & 63) << 2, (v & 31) << 3
                out[o + x] = (r * 299 + g * 587 + b * 114) // 1000
        else:
            for x in range(width):
                p = x * bytes_pp
                b, g, r = row[p], row[p + 1], row[p + 2]
                out[o + x] = (r * 299 + g * 587 + b * 114) // 1000
    return Gray(width, height, bytes(out))


def png_gray(width: int, height: int, pixels: bytes) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: none
        raw += pixels[y * width : (y + 1) * width]
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit grayscale
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height from a PNG header."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])


def read_png_gray(data: bytes) -> Gray:
    """Decode an 8-bit grayscale PNG (as png_gray writes; all row filters)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, width = 8, b"", 0
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if (depth, color, interlace) != (8, 0, 0):
                raise ValueError("only 8-bit grayscale, non-interlaced PNGs are supported")
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    out = bytearray(width * height)
    prev = bytearray(width)
    for y in range(height):
        f = raw[y * (width + 1)]
        row = bytearray(raw[y * (width + 1) + 1:(y + 1) * (width + 1)])
        for x in range(width):
            a = row[x - 1] if x else 0
            b = prev[x]
            c = prev[x - 1] if x else 0
            if f == 1:
                row[x] = (row[x] + a) & 0xFF
            elif f == 2:
                row[x] = (row[x] + b) & 0xFF
            elif f == 3:
                row[x] = (row[x] + (a + b) // 2) & 0xFF
            elif f == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                row[x] = (row[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        out[y * width:(y + 1) * width] = row
        prev = row
    return Gray(width, height, bytes(out))


def diff_pct(a: Gray, b: Gray, box: tuple[int, int, int, int] | None = None, tol: int = 24) -> float:
    """Percent of pixels (in box x, y, w, h) differing by more than tol gray levels."""
    if (a.width, a.height) != (b.width, b.height):
        raise ValueError(f"sizes differ: {a.width}x{a.height} vs {b.width}x{b.height}")
    x0, y0, w, h = box or (0, 0, a.width, a.height)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(a.width, x0 + w), min(a.height, y0 + h)
    n = diff = 0
    for y in range(y0, y1):
        ra = a.pixels[y * a.width + x0:y * a.width + x1]
        rb = b.pixels[y * b.width + x0:y * b.width + x1]
        diff += sum(1 for p, q in zip(ra, rb) if abs(p - q) > tol)
        n += x1 - x0
    return 100 * diff / n if n else 0.0
