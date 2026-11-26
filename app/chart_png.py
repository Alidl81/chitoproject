"""Render a small, dependency-free grouped bar chart for messenger photos."""

import struct
import zlib

COLORS = ((67, 142, 245), (76, 202, 149), (255, 176, 86))
_DIGITS = ("111101101101111", "010110010010111", "111001111100111",
           "111001111001111", "101101111001001", "111100111001111",
           "111100111101111", "111001010010010", "111101111101111", "111101111001111")


def chart_png(rows: list[dict], *, hourly: bool = False) -> bytes:
    width, height = max(540, 36 * len(rows) + 58), 340
    lines = [bytearray(bytes((16, 24, 38)) * width) for _ in range(height)]
    metric = ("starts", "projects", "new_users")
    peak = max(1, *(row[key] for row in rows for key in metric))

    def rect(x, y, w, h, color):
        if w <= 0 or h <= 0:
            return
        block = bytes(color) * w
        for line in lines[y:y + h]:
            line[x * 3:(x + w) * 3] = block

    def number(x, y, value):
        for char in str(value):
            for i, bit in enumerate(_DIGITS[int(char)]):
                if bit == "1":
                    rect(x + i % 3 * 2, y + i // 3 * 2, 2, 2, (206, 221, 238))
            x += 8

    step = (width - 58) // len(rows)
    for mark in sorted({0, peak // 2, peak}):
        level = 300 - round(250 * mark / peak)
        rect(44, level, width - 52, 1, (66, 81, 105))
        number(4, level - 5, mark)
    for index, row in enumerate(rows):
        x = 44 + index * step
        for k, color in zip(metric, COLORS, strict=True):
            bar_width = max(3, min(20, (step - 5) // 3))
            size = round(250 * row[k] / peak)
            rect(x + 2, 300 - size, bar_width, size, color)
            x += bar_width + 1
        label = row["hour"] if hourly else int(row["day"].rsplit("/", 1)[1])
        number(44 + index * step + max(0, step // 2 - 7), 309, label)

    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))

    pixels = b"".join(b"\x00" + line for line in lines)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
