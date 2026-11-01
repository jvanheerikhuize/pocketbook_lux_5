"""Tiny image helpers (stdlib only): PGM parsing, raw framebuffer decoding, PNG writing."""

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
