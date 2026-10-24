"""Generate a full-cell small font without nearest-neighbor stroke thickening."""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys

from .build_font_patch import BANKS, ROOT, encode_glyph, read_bdf, read_charset, sha256

Grid = list[list[bool]]
OUTPUT_NAME = "mother3-traditional-full-cell-10px.bdf"


@dataclass(frozen=True)
class Expansion:
    grid: Grid
    x: int
    y: int
    lower_x: bool
    lower_y: bool


def component_count(grid: Grid, foreground: bool, diagonals: bool) -> int:
    points = {
        (x, y) for y, row in enumerate(grid) for x, value in enumerate(row)
        if value == foreground
    }
    offsets = ((1, 0), (-1, 0), (0, 1), (0, -1))
    if diagonals:
        offsets += ((1, 1), (1, -1), (-1, 1), (-1, -1))
    count = 0
    while points:
        count += 1
        pending = [points.pop()]
        while pending:
            x, y = pending.pop()
            for dx, dy in offsets:
                point = x + dx, y + dy
                if point in points:
                    points.remove(point)
                    pending.append(point)
    return count


def topology(grid: Grid) -> tuple[int, int, int, int]:
    border = [False] * (len(grid[0]) + 2)
    bordered = [border] + [[False] + row + [False] for row in grid] + [border]
    return (
        component_count(grid, True, False), component_count(grid, True, True),
        component_count(bordered, False, False), component_count(bordered, False, True),
    )


def insert_row(grid: Grid, cut: int, lower_bridge: bool) -> Grid:
    upper, lower = grid[cut - 1], grid[cut]
    inserted = [a and b for a, b in zip(upper, lower)]
    # Continue crossing strokes, including diagonals, without duplicating a row.
    for x, ink in enumerate(upper):
        if not ink:
            continue
        for z in (x - 1, x + 1):
            if 0 <= z < len(lower) and lower[z] and not (inserted[x] or inserted[z]):
                inserted[z if lower_bridge else x] = True
    return grid[:cut] + [inserted] + grid[cut:]


def transpose(grid: Grid) -> Grid:
    return [list(row) for row in zip(*grid)]


def expand_grid(grid: Grid) -> Expansion:
    if len(grid) != 9 or any(len(row) != 9 for row in grid) or not any(map(any, grid)):
        raise ValueError("Expected a nonblank 9x9 source glyph")
    signature = topology(grid)
    occupied = [(x, y) for y, row in enumerate(grid) for x, ink in enumerate(row) if ink]
    xs, ys = zip(*occupied)
    best_score = None
    best = None
    for y in range(min(ys) + 1, max(ys) + 1):
        for lower_y in (False, True):
            rows = insert_row(grid, y, lower_y)
            if topology(rows) != signature:
                continue
            for x in range(min(xs) + 1, max(xs) + 1):
                for lower_x in (False, True):
                    expanded = transpose(insert_row(transpose(rows), x, lower_x))
                    score = (
                        sum(map(sum, expanded)), abs(x - 4.5) + abs(y - 4.5),
                        y, x, lower_y, lower_x,
                    )
                    if best_score is not None and score >= best_score:
                        continue
                    if topology(expanded) != signature:
                        continue
                    best_score = score
                    best = Expansion(expanded, x, y, lower_x, lower_y)
    if best is None:
        raise ValueError("No stroke-topology-preserving expansion exists")
    restored = [
        row[:best.x] + row[best.x + 1:]
        for y, row in enumerate(best.grid) if y != best.y
    ]
    if restored != grid:
        raise ValueError("Expansion modified source pixels")
    return best


def generate_font() -> str:
    directory = ROOT / "data/fonts"
    checksums = json.loads((directory / "sha256.json").read_text())
    names = (
        "fusion-pixel-10px-monospaced-zh_hant.bdf",
        "mother3-traditional-supplement-10px.bdf",
    )
    for name in names:
        if sha256((directory / name).read_bytes()) != checksums[name]:
            raise ValueError(f"Source font checksum mismatch: {name}")
    font = read_bdf(directory / names[0])
    supplement = read_bdf(directory / names[1])
    if (font.size, font.ascent) != (supplement.size, supplement.ascent):
        raise ValueError("Source font metrics disagree")
    if font.glyphs.keys() & supplement.glyphs.keys():
        raise ValueError("Supplement replaces an existing source glyph")
    font.glyphs.update(supplement.glyphs)
    targets = sorted({
        entry.target for entry in read_charset(ROOT / "data/charset.tsv")
        if entry.source != entry.target
    })
    lines = [
        "STARTFONT 2.1",
        "FONT -M3Traditional-M3 Traditional Full Cell-Regular-R-Normal--10-100-75-75-M-100-ISO10646-1",
        "SIZE 10 75 75", "FONTBOUNDINGBOX 10 10 0 -1", "STARTPROPERTIES 6",
        "PIXEL_SIZE 10", "FONT_ASCENT 9", "FONT_DESCENT 1",
        'FAMILY_NAME "M3 Traditional Full Cell"',
        'COPYRIGHT "Derived from Fusion Pixel Font; Copyright (c) 2022, TakWolf and upstream contributors"',
        'LICENSE "SIL Open Font License 1.1; see licenses-10px/OFL.txt and its upstream LICENSES"',
        "ENDPROPERTIES", f"CHARS {len(targets)}",
    ]
    for char in targets:
        raw = encode_glyph(font, char, BANKS[1])
        if any(raw[:2]) or any(raw[y * 2 + 1] & 0x7F for y in range(10)):
            raise ValueError(f"Source glyph {char} is not in the expected 9x9 area")
        grid = [
            [bool(raw[y * 2 + x // 8] & (1 << (7 - x % 8))) for x in range(9)]
            for y in range(1, 10)
        ]
        expanded = expand_grid(grid)
        lines += [
            f"STARTCHAR uni{ord(char):04X}",
            f"COMMENT INSERT_X {expanded.x} INSERT_Y {expanded.y} "
            f"LOWER_X {int(expanded.lower_x)} LOWER_Y {int(expanded.lower_y)}",
            f"ENCODING {ord(char)}", "SWIDTH 1000 0", "DWIDTH 10 0",
            "BBX 10 10 0 -1", "BITMAP",
        ]
        lines += [
            f"{sum(int(ink) << (15 - x) for x, ink in enumerate(row)):04X}"
            for row in expanded.grid
        ]
        lines.append("ENDCHAR")
    return "\n".join(lines + ["ENDFONT", ""])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/fonts" / OUTPUT_NAME)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise FileExistsError(f"Refusing to overwrite {args.output}")
        contents = generate_font().encode("ascii")
        with args.output.open("xb") as handle:
            handle.write(contents)
        print(args.output)
        print(f"SHA-256: {sha256(contents)}")
    except (OSError, ValueError) as error:
        print(f"Font generation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
