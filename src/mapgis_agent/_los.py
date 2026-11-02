"""Python adaptation of GDAL v3.13.0 alg/los.cpp by Ryan Friedman.

Pinned upstream130cb4675a0b42bd32acad56e57022cca44fc008; original source and
license retained in third_party/gdal_los. Equality with terrain is visible,
following upstream PR14237. This adaptation records the complete sampled ray
and reports its first ordered obstruction instead of a callback's final hit.

SPDX-License-Identifier: MIT

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""
from __future__ import annotations
import math

UPSTREAM = {'project': 'OSGeo/gdal', 'tag': 'v3.13.0',
    'commit': '130cb4675a0b42bd32acad56e57022cca44fc008', 'file': 'alg/los.cpp',
    'sha256': '041ed18f3f00cee77267d8979e45d50492e32f3597fe0414150d93c12a07f963',
    'license': 'MIT', 'port': 'Python; full samples and first ordered obstruction'}


def cells(start, end):
    """The original dominant-axis Bresenham tie rule, including both endpoints."""
    x, y = start; xb, yb = end
    dx, dy = abs(xb-x), abs(yb-y)
    incx, incy = (1 if xb >= x else -1), (1 if yb >= y else -1)
    result = []
    if dx >= dy:
        balance = 2*dy-dx
        while x != xb:
            result.append((x, y))
            if balance >= 0:
                y += incy
                balance -= 2*dx
            balance += 2*dy
            x += incx
    else:
        balance = 2*dx-dy
        while y != yb:
            result.append((x, y))
            if balance >= 0:
                x += incx
                balance -= 2*dy
            balance += 2*dx
            y += incy
    result.append((x, y))
    return result


def trace(elevation, start, end, start_height, end_height):
    xa, ya = start; xb, yb = end
    denominator = (xb-xa)**2+(yb-ya)**2
    rows = []
    for x, y in cells(start, end):
        if not denominator:
            ratio = 0.
        elif xa == xb:
            ratio = (y-ya)/(yb-ya)
        elif ya == yb:
            ratio = (x-xa)/(xb-xa)
        else:
            ratio = math.sqrt(((x-xa)**2+(y-ya)**2)/denominator)
        line = start_height+ratio*(end_height-start_height)
        ground = float(elevation[y, x])
        rows.append({'column': x, 'row': y, 'distance_pixels': math.sqrt((x-xa)**2+(y-ya)**2),
                     'ground': ground, 'line': line, 'clearance': line-ground, 'above': line >= ground})
    first = next((i for i, row in enumerate(rows) if not row['above']), None)
    # Upstream checks both endpoint heights even when they share one pixel.
    if not denominator and end_height < float(elevation[ya, xa]):
        first = 0
    return {'visible': first is None, 'first_obstruction_index': first, 'samples': rows}
