#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应用图标（不依赖 Pillow，直接输出 PNG）。

产物：
    ICON.PNG              64x64
    ICON_256.PNG          256x256
    app/ui/images/icon_64.png
    app/ui/images/icon_256.png
"""

import os
import struct
import sys
import zlib

BG = (0x18, 0x5F, 0xA5)
FG = (0xFF, 0xFF, 0xFF)
ACCENT = (0xEF, 0x9F, 0x27)

SUBSAMPLES = 4


def rounded_rect_contains(x, y, radius):
    if x < 0 or y < 0 or x > 1 or y > 1:
        return False
    cx = min(max(x, radius), 1 - radius)
    cy = min(max(y, radius), 1 - radius)
    dx = x - cx
    dy = y - cy
    return (dx * dx + dy * dy) <= radius * radius


def in_triangle(x, y):
    ax, ay = 0.33, 0.29
    bx, by = 0.33, 0.71
    cx, cy = 0.61, 0.50
    d1 = (x - bx) * (ay - by) - (ax - bx) * (y - by)
    d2 = (x - cx) * (by - cy) - (bx - cx) * (y - cy)
    d3 = (x - ax) * (cy - ay) - (cx - ax) * (y - ay)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


CUT_BARS = ((0.27, 0.40), (0.435, 0.565), (0.60, 0.73))


def in_cut_bar(x, y):
    if not (0.685 <= x <= 0.775):
        return False
    for top, bottom in CUT_BARS:
        if top <= y <= bottom:
            return True
    return False


def sample(x, y):
    if in_triangle(x, y):
        return FG
    if in_cut_bar(x, y):
        return ACCENT
    return BG


def render(size):
    radius = 0.19
    step = 1.0 / (size * SUBSAMPLES)
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            r = g = b = 0
            count = 0
            for sy in range(SUBSAMPLES):
                for sx in range(SUBSAMPLES):
                    x = (px * SUBSAMPLES + sx + 0.5) * step
                    y = (py * SUBSAMPLES + sy + 0.5) * step
                    if not rounded_rect_contains(x, y, radius):
                        continue
                    cr, cg, cb = sample(x, y)
                    r += cr
                    g += cg
                    b += cb
                    count += 1
            total = SUBSAMPLES * SUBSAMPLES
            if count == 0:
                row += bytes((0, 0, 0, 0))
            else:
                alpha = int(round(255.0 * count / total))
                row += bytes((
                    int(round(r / float(count))),
                    int(round(g / float(count))),
                    int(round(b / float(count))),
                    alpha,
                ))
        rows.append(bytes(row))
    return rows


def write_png(path, size, rows):
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    payload = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) +
               chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    with open(path, "wb") as handle:
        handle.write(payload)


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__)) + "/.."
    root = os.path.abspath(root)
    targets = [
        (64, os.path.join(root, "ICON.PNG")),
        (256, os.path.join(root, "ICON_256.PNG")),
        (64, os.path.join(root, "app", "ui", "images", "icon_64.png")),
        (256, os.path.join(root, "app", "ui", "images", "icon_256.png")),
    ]
    cache = {}
    for size, path in targets:
        if size not in cache:
            cache[size] = render(size)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_png(path, size, cache[size])
        print("生成图标 %s (%dx%d)" % (path, size, size))


if __name__ == "__main__":
    main()
